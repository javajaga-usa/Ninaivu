"""Command line entry point: ``python -m ninaivu``."""

from __future__ import annotations

import argparse
import errno
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from . import (
    APP_NAME, TAGLINE, __version__, build_services, create_admin_app,
    create_home_app,
)
from .server import auth, runfile
from .server.config import Config
from .storage import db


def use_utf8_output() -> None:
    """Make sure the startup banner cannot kill the launcher.

    Nearly every line this module prints carries typographic characters — em
    dashes, arrows, curly quotes. Python on Windows handles those on its own
    only while stdout is an interactive console: redirect it to a file or a
    pipe and the encoding falls back to the locale's, which here is cp1252, and
    the first arrow raises :class:`UnicodeEncodeError`. That happens *after*
    the ports are bound and before the address is printed, so the server dies
    of its own success message — and it does it in exactly the situations
    nobody is watching: ``start.bat > log.txt``, a service supervisor, Task
    Scheduler, or any wrapper that captures output.

    A banner is not worth a crash, so the streams are asked for UTF-8 and, if
    they cannot manage that, told to replace what they cannot encode. Losing a
    dash beats losing the server.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            try:
                stream.reconfigure(errors="replace")
            except (AttributeError, OSError, ValueError):
                # Something has replaced the stream with an object that cannot
                # be retuned at all. Nothing to do but carry on.
                pass


def lan_addresses() -> list[str]:
    """Every address of this machine another device could reach it on."""
    from .utils import tls

    return tls.lan_addresses()


def lan_address() -> str | None:
    """The single most likely one, or None."""
    found = lan_addresses()
    return found[0] if found else None


def port_is_free(host: str, port: int) -> bool | None:
    """True if *port* can be bound on *host* right now, False if something holds
    it, and None when this user may not bind it on *host* at all."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        # Asked the way the server will bind. On Windows that is exclusive. On
        # macOS and Linux the server sets SO_REUSEADDR, which a listener still
        # refuses but the closed connections of a restart do not: without it
        # here, a phone that had just been connected left 443 in TIME_WAIT,
        # 443 read as busy, and the family app moved to 444.
        if sys.platform == "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except PermissionError:
            return None
        except OSError:
            return False


#: How long the preferred port is waited for before moving on. A restart
#: starts the new server as the old one is letting go of its port.
PREFERRED_WAIT = 8.0


def pick_port(host: str, preferred: int, tries: int = 40) -> int:
    """Return *preferred* if free, else the next free port, else any free port.

    Keeps the app startable on every platform without the user having to hunt
    down whatever is squatting on the preferred port (IIS on port 80 on
    Windows, macOS AirPlay Receiver on port 5000, most often).
    """
    bind_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host

    def free_now(candidate: int) -> bool | None:
        free = port_is_free(bind_host, candidate)
        if free is None and bind_host != host:
            free = port_is_free(host, candidate)
        return free

    until = time.monotonic() + PREFERRED_WAIT
    while free_now(preferred) is False and time.monotonic() < until:
        time.sleep(0.25)
    for offset in range(tries):
        candidate = preferred + offset
        if candidate > 65535:
            break
        free = port_is_free(bind_host, candidate)
        # macOS lets an ordinary user bind a port below 1024 on every address
        # but not on 127.0.0.1 alone, so asked only there, 443 always looked
        # busy and the family app moved to a random port. start.py asks the
        # same way; see find_port there.
        if free is None and bind_host != host:
            free = port_is_free(host, candidate)
        if free:
            return candidate
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((bind_host, 0))
        return int(sock.getsockname()[1])


def format_url(host: str, port: int, scheme: str = "http") -> str:
    """A URL with no port shown when *port* is that scheme's default.

    So a family app running on port 80 (start.py's default) prints as
    ``http://ninaivu.local`` rather than ``http://ninaivu.local:80`` — the
    whole reason for defaulting to that port in the first place. Any other
    port still shows, same as always. start.py has its own copy of this,
    used before this process even exists.
    """
    default_port = 443 if scheme == "https" else 80
    suffix = "" if port == default_port else f":{port}"
    return f"{scheme}://{host}{suffix}"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ninaivu",
        description=f"{APP_NAME} — {TAGLINE}",
    )
    p.add_argument("root", nargs="?", help="Library folder to open")
    p.add_argument("--root", dest="root_opt", help="Library folder to open")
    p.add_argument("--allow", action="append", default=[], metavar="DIR",
                   help="Additional folder the UI may switch to (repeatable)")
    p.add_argument("--host", default=None,
                   help="Address to serve on (default 0.0.0.0 — the whole home network)")
    p.add_argument("--local-only", action="store_true",
                   help="Serve to this machine only, not to the rest of the network")
    p.add_argument("--allow-sleep", action="store_true",
                   help="Let the machine sleep normally while Ninaivu is running")
    p.add_argument("--name", default="ninaivu", metavar="NAME",
                   help="Name to answer to on the network, so devices can use "
                        "http://NAME.local instead of an IP (default: ninaivu)")
    p.add_argument("--admin-name", default=None, metavar="NAME",
                   help="Name the admin console answers to on the network "
                        "(default: NAME-admin, e.g. ninaivu-admin, so the "
                        "family app and the console resolve to two different "
                        "names; pass the same value as --name to share one "
                        "hostname across both ports instead)")
    p.add_argument("--no-mdns", action="store_true",
                   help="Do not announce a name on the network")
    p.add_argument("--https", action="store_true",
                   help="Serve over HTTPS, creating a local CA and certificate "
                        "on first use")
    p.add_argument("--cert", default=None, metavar="FILE",
                   help="Use this certificate (implies --https)")
    p.add_argument("--key", default=None, metavar="FILE",
                   help="Private key for --cert")
    p.add_argument("--port", type=int, default=None,
                   help="Family app port (default 443 with HTTPS, otherwise 80; steps up if taken)")
    p.add_argument("--admin-port", type=int, default=None,
                   help="Admin console port (default 3000; steps up if taken)")
    p.add_argument("--admin-host", default=None,
                   help="Address the admin console binds to (default: same as "
                        "--host). Set to 127.0.0.1 to serve the family app to "
                        "the network while keeping the console local")
    p.add_argument("--no-admin", action="store_true",
                   help="Do not start the admin console at all")
    p.add_argument("--strict-port", action="store_true",
                   help="Fail instead of falling back to another port")
    p.add_argument("--workers", type=int, default=None,
                   help="Scanner thread count (default: CPU count, max 8)")
    p.add_argument("--ai", choices=["auto", "clip", "light", "off"], default=None,
                   help="AI tier: clip enables natural-language search")
    p.add_argument("--no-nsfw-filter", action="store_true",
                   help="Do not hide explicit content behind a toggle")
    p.add_argument("--faces", action="store_true",
                   help="Detect and group the faces in your photographs")
    p.add_argument("--ocr", action="store_true",
                   help="Read the words in your photographs so they can be "
                        "searched for (needs requirements/requirements-ocr.txt)")
    p.add_argument("--places", action="store_true",
                   help="Name the places your photographs were taken "
                        "(fetches a gazetteer once, then works offline)")
    p.add_argument("--map-tiles", action="store_true",
                   help="Draw the map on tiles from OpenStreetMap instead of "
                        "the outline Ninaivu ships. This tells a tile server "
                        "which part of the world your photographs are of")
    p.add_argument("--no-watch", action="store_true",
                   help="Do not watch the library for changes")
    p.add_argument("--private", action="store_true",
                   help="Require a sign-in for everything; no open guest browsing")
    p.add_argument("--open-browsing", action="store_true",
                   help="Let visitors see public media without signing in (default)")
    p.add_argument("--admin", metavar="USER:PASSWORD",
                   help="Create the first administrator without using the browser")
    p.add_argument("--rescan", action="store_true",
                   help="Force a full re-index, ignoring cached signatures")
    p.add_argument("--lock-roots", action="store_true",
                   help="Confine the folder picker to --root/--allow (for exposed consoles)")
    p.add_argument("--open", action="store_true", help="Open a browser on start")
    p.add_argument("--debug", action="store_true")
    p.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    # Before anything is printed, including argparse's own errors.
    use_utf8_output()
    args = build_parser().parse_args(argv)

    cfg = Config.load()
    try:
        with runfile.server_lock(cfg.state_dir):
            return _run(cfg, args)
    except runfile.AlreadyRunning as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def resolve_hosts(cfg, args) -> None:
    """Decide the addresses the family app and the console listen on."""
    if args.host:
        cfg.host = args.host
    if args.local_only:
        cfg.host = "127.0.0.1"
    if args.admin_host:
        cfg.admin_host = args.admin_host
    if not getattr(cfg, "network_access", True):
        # Switched off on the console's Server page. That is a decision about
        # this machine, so it outranks whatever the launcher passed - start.py
        # and the desktop panel both pass --host 0.0.0.0 on every start.
        cfg.host = "127.0.0.1"
        cfg.admin_host = "127.0.0.1"
        args.no_mdns = True
    # Unset means "same as the family app", which is today's behaviour and
    # keeps an upgraded install's reachable addresses unchanged. Resolved
    # once, here, so port-picking, the startup banner and _serve() all agree
    # on where the console is actually going to listen.
    cfg.admin_host = cfg.admin_host or cfg.host


def _run(cfg, args) -> int:
    root = args.root_opt or args.root
    if root:
        resolved = Path(root).expanduser().resolve()
        if not resolved.is_dir():
            print(f"error: {resolved} is not a directory", file=sys.stderr)
            return 2
        cfg.active_root = str(resolved)
        if str(resolved) not in cfg.roots:
            cfg.roots.append(str(resolved))
    for extra in args.allow:
        path = str(Path(extra).expanduser().resolve())
        if path not in cfg.roots:
            cfg.roots.append(path)

    resolve_hosts(cfg, args)
    if args.port:
        cfg.port = args.port
    elif (args.https or args.cert) and cfg.port == 80:
        cfg.port = 443
    if args.admin_port:
        cfg.admin_port = args.admin_port
    if args.workers:
        cfg.workers = args.workers
    if args.ai:
        cfg.ai_engine = args.ai
        cfg.ai_enabled = args.ai != "off"
    if args.no_nsfw_filter:
        cfg.nsfw_filter = False
    if args.faces:
        cfg.faces_enabled = True
    if args.ocr:
        cfg.ocr_enabled = True
    if args.places:
        cfg.place_names = True
    if args.map_tiles:
        cfg.map_tiles = True
    if args.no_watch:
        cfg.watch = False
    if args.private:
        cfg.open_browsing = False
    if args.open_browsing:
        cfg.open_browsing = True
    if args.lock_roots:
        cfg.lock_roots = True
    if args.debug:
        cfg.debug = True
    cfg.save()

    if not cfg.active_root:
        print("No library folder set yet — open the app and pick one, or run:")
        print("  python -m ninaivu /path/to/photos")

    requested = cfg.port
    requested_admin = cfg.admin_port
    if args.strict_port:
        if not port_is_free("127.0.0.1" if cfg.host in ("0.0.0.0", "::") else cfg.host,
                            requested):
            print(f"error: port {requested} is already in use", file=sys.stderr)
            return 1
    else:
        cfg.port = pick_port(cfg.host, requested)
        if not args.no_admin:
            # Skip the family port when hunting for the console's, and probe
            # on the address the console will actually bind to -- which is
            # not always cfg.host, now that the two can be split.
            cfg.admin_port = pick_port(cfg.admin_host, requested_admin)
            if cfg.admin_port == cfg.port:
                cfg.admin_port = pick_port(cfg.admin_host, cfg.port + 1)

    services = build_services(cfg)
    home = create_home_app(services)
    admin = None if args.no_admin else create_admin_app(services)
    for app in (home, admin):
        if app is not None:
            app.config["APP_VERSION"] = __version__
            # A leftover CA from an earlier setup cannot establish trust in
            # an explicitly supplied certificate from another issuer.
            app.config["MV_SERVE_LOCAL_CA"] = not bool(args.cert)

    conn = db.init_db(cfg.db_path)
    auth.init_auth_schema(conn)
    if args.admin:
        username, _, password = args.admin.partition(":")
        if not password:
            print("error: use --admin USER:PASSWORD", file=sys.stderr)
            return 2
        try:
            created = auth.bootstrap_admin(conn, username.strip(), password)
            print(f"  created administrator \u201c{created.username}\u201d")
        except (ValueError, PermissionError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    first_run = auth.needs_setup(conn)

    ssl_files = _resolve_tls(cfg, args)
    if ssl_files is False:                       # a refusal, already explained
        return 2
    scheme = "https" if ssl_files else "http"
    from .server.host_router import admin_hostname
    try:
        shared_admin = admin_hostname(cfg, args) if admin is not None else None
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    named_admin_port = cfg.port if shared_admin else cfg.admin_port

    # Announce a name on the LAN, so nobody has to type an address. Only when
    # actually serving the house — a localhost-only server has no audience.
    announcement = None
    mdns_note = None
    if cfg.host in ("0.0.0.0", "::") and not args.no_mdns and args.name:
        from .utils import discovery

        try:
            wanted = discovery.normalise_name(args.name)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        admin_wanted = None
        if admin is not None:
            # Two different names by default (ninaivu / ninaivu-admin), so the
            # name alone hints which app you're opening — --admin-name ninaivu
            # (i.e. the same as --name) goes back to one shared hostname.
            admin_raw = args.admin_name if args.admin_name is not None else f"{wanted}-admin"
            try:
                admin_wanted = discovery.normalise_name(admin_raw)
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
        if discovery.available():
            announcement = discovery.advertise(
                wanted,
                {"family": cfg.port, **({"admin": named_admin_port}
                                        if admin is not None else {})},
                lan_addresses(),
                scheme=scheme,
                admin_name=admin_wanted,
                watch_fn=lan_addresses,
            )
            if announcement is None:
                mdns_note = ("could not announce a name — carrying on with "
                             "addresses only")
        else:
            mdns_note = ("install 'zeroconf' to use a name instead of an "
                         "address (pip install zeroconf)")

    display_host = "localhost" if cfg.host in ("0.0.0.0", "127.0.0.1", "::") else cfg.host
    admin_display_host = ("localhost" if cfg.admin_host in ("0.0.0.0", "127.0.0.1", "::")
                          else cfg.admin_host)
    home_url = format_url(display_host, cfg.port, scheme)
    admin_url = format_url(admin_display_host, cfg.admin_port, scheme)

    print(f"\n  {APP_NAME} {__version__}")
    print(f"  family    → {home_url}")
    if admin is not None:
        print(f"  admin     → {admin_url}")

    # Serving the house: say which address the other devices should type.
    if cfg.host in ("0.0.0.0", "::"):
        candidates = lan_addresses()
        lan = candidates[0] if candidates else None
        if lan:
            print("  on your network:")
            print(f"    family  → {format_url(lan, cfg.port, scheme)}"
                  f"   ← open this on phones and tablets")
            if admin is not None and cfg.admin_host in ("0.0.0.0", "::"):
                print(f"    admin   → {format_url(lan, cfg.admin_port, scheme)}")

            # A machine with a VPN, a virtual-machine adapter or WSL running has
            # several addresses and only one of them is on the family's Wi-Fi.
            # Printing the runners-up costs a line and saves a support session.
            if announcement is not None:
                print(f"    or by name → "
                      f"{format_url(announcement.hostnames['family'], cfg.port, scheme)}"
                      f"   ← easier to remember, works on most devices")
                admin_hostname = announcement.hostnames.get("admin")
                if (admin is not None and admin_hostname
                        and admin_hostname != announcement.hostnames["family"]
                        and cfg.admin_host in ("0.0.0.0", "::")):
                    print(f"                 "
                          f"{format_url(admin_hostname, named_admin_port, scheme)}"
                          f"   (admin console)")
                try:
                    comp_name = socket.gethostname().split('.')[0].lower()
                    native_name = f"{comp_name}.local"
                    if native_name != announcement.hostnames["family"]:
                        print(f"    native name → "
                              f"{format_url(native_name, cfg.port, scheme)}"
                              f"   (built-in Windows hostname)")
                except Exception:
                    pass
            elif mdns_note:
                print(f"    ({mdns_note})")

            others = candidates[1:4]
            if others:
                print(f"    if that one does not work, this machine is also at: "
                      f"{', '.join(others)}")
            hint = _firewall_warning(cfg.port)
            if hint:
                for line in hint:
                    print(line)
            else:
                print("    (if nothing loads from another device, it is almost "
                      "always the firewall — allow Python on private networks)")
        else:
            print("  on your network: listening on every interface, but this "
                  "machine has no network address yet — check it is connected.")
    else:
        if not cfg.network_access:
            print("  on your network: no — this machine only. Network access is "
                  "switched off; turn it on in the console under Server.")
        else:
            print("  on your network: no — this machine only. Drop --local-only "
                  "(or --host 127.0.0.1) to let phones and tablets in.")

    if admin is not None and cfg.admin_host in ("0.0.0.0", "::"):
        print()
        print("  WARNING: the admin console is reachable from your network, not")
        print("  just this machine — anyone who can reach it sees the sign-in")
        print("  page and can try a password against it. Restrict it with")
        print("  --admin-host 127.0.0.1 (or NINAIVU_ADMIN_HOST=127.0.0.1), or")
        print(f"  firewall port {cfg.admin_port} to this machine.")

    if cfg.port != requested:
        print(f"    (port {requested} was busy, using {cfg.port})")
    if admin is not None and cfg.admin_port != requested_admin:
        print(f"    (port {requested_admin} was busy, using {cfg.admin_port})")
    if ssl_files:
        from .utils import tls as tls_mod

        print(f"  https:    on — {tls_mod.describe(ssl_files[0])}")
        if not (args.cert and args.key):
            print(f"    trust it once per device:  "
                  f"{format_url(lan_address() or display_host, cfg.port, scheme)}"
                  f"/cert")
            print(f"    (that file is {tls_mod.ca_certificate_path(cfg.state_dir)})")
    from .utils.awake import KeepAwake

    # Started here rather than inside _serve because the banner has to report
    # what actually happened, and because on Windows the request belongs to the
    # thread that made it — which is this one, the same thread that goes on to
    # serve. A library that falls asleep swallows connections rather than
    # refusing them, so a phone waits and then times out, and it reads as a
    # network fault rather than a sleeping PC.
    awake = KeepAwake("Serving the family library")
    if not args.allow_sleep:
        awake.start()
        print(f"  {awake.describe()}")
    else:
        print("  sleep:    allowed — the library goes away when this machine sleeps")

    print(f"  library:  {cfg.active_root or '(not set)'}")
    print(f"  state:    {cfg.state_dir}")
    print(f"  access:   {'open browsing + profiles' if cfg.open_browsing else 'private (sign-in required)'}")
    if first_run:
        print()
        if admin is not None:
            print(f"  First run — open {admin_url} to create the admin profile.")
        else:
            print(f"  First run — open {home_url} to create the admin profile.")
    print()

    services.start(rescan=args.rescan)

    if args.open:
        threading.Timer(1.0, lambda: webbrowser.open(
            admin_url if first_run and admin is not None else home_url)).start()

    try:
        return _serve(home, admin, cfg, args, ssl_files, awake, services, announcement=announcement)
    finally:
        services.guardian.stop()
        services.power.stop()
        # The run file names a process that no longer exists the moment this
        # returns, and a stale one is worse than none: a stop script would
        # report failure for a server that stopped days ago.
        runfile.clear(cfg.state_dir)
        if announcement is not None:
            announcement.close()


def _resolve_tls(cfg, args):
    """Return ``(certfile, keyfile)``, ``None`` for plain HTTP, or False to stop.

    Supplying your own certificate skips Ninaivu's own CA entirely — which is
    what you want behind a reverse proxy or with a real domain.
    """
    from .utils import tls as tls_mod

    if args.cert or args.key:
        if not (args.cert and args.key):
            print("error: --cert and --key must be given together.", file=sys.stderr)
            return False
        certificate, private_key = Path(args.cert), Path(args.key)
        for path in (certificate, private_key):
            if not path.is_file():
                print(f"error: no such file: {path}", file=sys.stderr)
                return False
        return str(certificate), str(private_key)

    if not args.https:
        return None

    try:
        # The name has to be in the certificate, or using it produces a
        # warning that using the bare address does not.
        extra = [cfg.host]
        if not args.no_mdns and args.name:
            try:
                from .utils import discovery

                wanted = discovery.normalise_name(args.name)
                extra.append(f"{wanted}.local")
                if not args.no_admin:
                    admin_raw = args.admin_name if args.admin_name is not None \
                        else f"{wanted}-admin"
                    extra.append(f"{discovery.normalise_name(admin_raw)}.local")
            except ValueError:
                pass                             # reported properly below
        certificate, private_key = tls_mod.ensure_certificate(
            cfg.state_dir, extra_hosts=extra)
    except tls_mod.CertificateUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return False
    return str(certificate), str(private_key)


def _firewall_warning(port: int) -> list[str]:
    """On Windows, say so *before* a phone spends 30 seconds timing out.

    Windows Firewall drops blocked connections rather than refusing them, so a
    phone gets no error — it waits, and then reports a timeout that looks like
    Ninaivu is broken. Checking for the rule at startup turns that into one line
    of text and a file to double-click. Returns [] when there is nothing to say
    or when the check cannot run.

    ``port`` is the family app's *actual* serving port — not assumed to be any
    particular default — since `tools/allow-network.bat` opens the required TCP
    ports (80, 443, 5000 and 3000) and UDP port 5353 for mDNS discovery.
    """
    if sys.platform != "win32":
        return []
    missing: list[str] = []
    try:
        result = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", "name=Ninaivu"],
            capture_output=True, text=True, timeout=6, check=False)
        stdout = result.stdout or ""
        if str(port) not in stdout:
            missing.append(f"port {port} (TCP)")
    except (OSError, subprocess.SubprocessError):
        pass

    try:
        mdns_result = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", "name=Ninaivu mDNS"],
            capture_output=True, text=True, timeout=6, check=False)
        mdns_stdout = mdns_result.stdout or ""
        if "5353" not in mdns_stdout:
            missing.append("mDNS discovery port 5353 (UDP)")
    except (OSError, subprocess.SubprocessError):
        pass

    if not missing:
        return []

    detail = " and ".join(missing)
    return [
        f"    ! Windows Firewall has no rule covering {detail}, so other",
        "      devices will time out or cannot resolve ninaivu.local.",
        "      Double-click this once to open both:",
        "          tools\\allow-network.bat",
        "      It asks for administrator rights itself.",
    ]


def _serve(home, admin, cfg, args, ssl_files=None, awake=None,
           services=None, announcement=None) -> int:
    """Run both apps: the console on a daemon thread, the family app in front.

    Also the one place that knows how to stop: it writes the run file naming
    this process and its ports, and hands both apps a way to bring themselves
    down. Ctrl+C is still the right way to stop a server you are looking at —
    this is for everything that is not looking at it.
    """
    from .server.http import make_threaded_server

    # A browser asking for this computer's Tailscale name is given Tailscale's
    # certificate for it (utils/tailnet.py); every other name, Ninaivu's own.
    tailnet_cert = None
    if ssl_files and getattr(cfg, "tailnet_https", True):
        from .utils import tailnet, tls as tls_mod                # noqa: PLC0415
        tailnet_cert = tailnet.TailnetCertificate(tls_mod.tls_dir(cfg.state_dir))
        tailnet_cert.load_saved()
        tailnet_cert.watch()

    def _tls_for_one_port():
        if tailnet_cert is None:
            return ssl_files
        from werkzeug.serving import load_ssl_context             # noqa: PLC0415
        return tailnet_cert.attach(load_ssl_context(*ssl_files))

    def _serve_on(host, port, application, name):
        """A server for one port, on a thread *pool* where one can be had.

        Werkzeug's `threaded=True` spawns a fresh thread per request. Two
        things follow, and both of them bite a library server. The database
        connection cache in `ninaivu.db` is keyed per thread, so a brand-new
        thread means a brand-new SQLite connection and five PRAGMAs for every
        request — a grid of two hundred thumbnails opened two hundred
        connections. And nothing bounds the thread count, so a handful of
        clients pulling video is enough to make the machine spawn threads
        until it cannot.

        Waitress fixes both by reusing a fixed pool, and it is the one WSGI
        server that behaves the same on Windows as on everything else, which
        matters for a program people run on the family computer. It is
        optional: without it, the old behaviour is exactly what happens, and
        the only cost is the one Ninaivu has always paid.
        """
        if ssl_files is None:
            try:
                from waitress.server import create_server  # noqa: PLC0415

                return ("waitress", create_server(
                    application, host=host, port=port,
                    threads=cfg.server_threads, ident="Ninaivu",
                    connection_limit=200, channel_timeout=300))
            except ImportError:
                pass
        # TLS, or no waitress: Werkzeug, as before.
        return ("werkzeug", make_threaded_server(host, port, application,
                                                 _tls_for_one_port() if ssl_files else None))

    servers = []
    stopping = threading.Event()

    def stop_gracefully() -> None:
        """Take the whole thing down, from a request thread, without blocking it."""
        if stopping.is_set():
            return
        stopping.set()

        def finish() -> None:
            # A moment for the answer to reach whoever asked, so they are told
            # the request was accepted rather than left with a dropped
            # connection to interpret.
            time.sleep(0.3)
            print("\n  Stopping — finishing what is in hand…")
            if services is not None:
                for problem in services.stop():
                    print(f"    ! {problem}")
            for server in list(servers):
                try:
                    # Werkzeug says shutdown(); waitress says close().
                    (getattr(server, "shutdown", None) or server.close)()
                except Exception:                        # noqa: BLE001
                    pass
            print("  Stopped.")

        threading.Thread(target=finish, name="ninaivu-stop", daemon=True).start()

    token = runfile.new_token()
    scheme = "https" if ssl_files else "http"
    hostnames_map = announcement.hostnames if (announcement and getattr(announcement, "hostnames", None)) else {}
    if not hostnames_map and not args.no_mdns and args.name:
        try:
            from .utils import discovery
            f_name = f"{discovery.normalise_name(args.name)}.local"
            a_name = f"{discovery.normalise_name(args.admin_name if args.admin_name is not None else f'{args.name}-admin')}.local"
            hostnames_map = {"family": f_name, "admin": a_name}
        except Exception:
            pass

    for application in (home, admin):
        if application is not None:
            application.config["MV_SHUTDOWN"] = stop_gracefully
            application.config["MV_STOP_TOKEN"] = token
            application.config["MV_SCHEME"] = scheme
            application.config["MV_HOSTNAMES"] = hostnames_map

    try:
        if admin is not None:
            _, admin_server = _serve_on(cfg.admin_host, cfg.admin_port,
                                        admin, "ninaivu-admin")
            servers.append(admin_server)
            thread = threading.Thread(
                target=admin_server.run if hasattr(admin_server, "run")
                else admin_server.serve_forever,
                name="ninaivu-admin", daemon=True)
            thread.start()

        from .server.host_router import HostRouter, admin_hostname
        hostname = admin_hostname(cfg, args) if admin is not None else None
        family_app = HostRouter(home, admin, hostname) if hostname else home
        kind, home_server = _serve_on(cfg.host, cfg.port, family_app, "ninaivu-home")
        servers.append(home_server)
        runfile.write(cfg.state_dir, port=cfg.port, admin_port=cfg.admin_port,
                      host=cfg.host, version=__version__, token=token,
                      scheme="https" if ssl_files else "http")
        if kind == "waitress":
            home_server.run()
        else:
            home_server.serve_forever()
    except OSError as exc:
        if exc.errno not in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", -1)) \
                or args.strict_port:
            raise
        print(f"error: a port was taken at the last moment ({exc})", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        # The same orderly stop Ctrl+C always deserved. It was a bare print
        # before, which left a scan and an upload to be killed with the
        # process and picked apart on the next run.
        print("\n  Stopping — finishing what is in hand…")
        if services is not None:
            for problem in services.stop():
                print(f"    ! {problem}")
        print("  Stopped.")
    finally:
        for server in servers:
            try:
                (getattr(server, "shutdown", None) or server.close)()
            except Exception:
                pass
        if awake is not None:
            awake.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
