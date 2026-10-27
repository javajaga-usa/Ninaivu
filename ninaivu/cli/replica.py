"""The Pi as a read-only replica of the Mac's library: the commands.

    ninaivu replica publish --to "/Volumes/Ninaivu/replica"     (on the Mac)
    ninaivu replica status  --from /srv/ninaivu/replica          (anywhere)
    ninaivu replica unit    --from /srv/ninaivu/replica \\
                            --mirror /srv/ninaivu/current        (on the Pi)

``publish`` writes one hand-over now, the same one the running server writes
by itself when ``replica_dir`` is set. ``status`` says what a hand-over
folder holds. ``unit`` prints the systemd service for the Pi, with each of
the Mac's library folders mounted read-only at the Mac's own path from the
Pi's mirror, found by the id in its ``.ninaivu-library`` marker.

The server side is ``ninaivu --replica-of <folder>``; see docs/pi-replica.md.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import time
from pathlib import Path

from ..replica import MANIFEST

#: How deep under the mirror a library folder is looked for.
SEARCH_DEPTH = 4


def use_utf8_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _manifest(folder: Path) -> dict:
    try:
        data = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"{folder} holds no hand-over yet (no {MANIFEST}). Run "
                         f"`ninaivu replica publish` on the Mac first.") from None
    except (OSError, ValueError) as exc:
        raise SystemExit(f"{folder / MANIFEST} could not be read: {exc}") from None
    if not isinstance(data, dict):
        raise SystemExit(f"{folder / MANIFEST} is not a hand-over.")
    return data


# -- publish -----------------------------------------------------------------

def cmd_publish(args) -> int:
    from ..replica.publish import publish                         # noqa: PLC0415
    from ..server.config import Config                           # noqa: PLC0415

    if args.state_dir:
        # Through the environment, as the server does: config.json is read
        # from the state folder before any override is applied.
        os.environ["NINAIVU_STATE_DIR"] = str(Path(args.state_dir).expanduser())
    cfg = Config.load()
    target = args.to or cfg.replica_dir
    if not target:
        raise SystemExit("Say where to write it: --to <folder>, or set replica_dir.")
    try:
        manifest = publish(cfg, Path(target))
    except (OSError, FileNotFoundError) as exc:
        raise SystemExit(f"error: {exc}") from None
    folders = manifest["folders"]
    print(f"Handed the library over to {target}:")
    print(f"  index    {manifest['index']['size']:,} bytes ({manifest['index']['file']})")
    for name, counts in folders.items():
        print(f"  {name:<8} {counts['files']:,} files, {counts['copied']:,} copied, "
              f"{counts['removed']:,} removed")
    return 0


# -- status ------------------------------------------------------------------

def cmd_status(args) -> int:
    folder = Path(args.source).expanduser()
    manifest = _manifest(folder)
    made = manifest.get("made_at")
    print(f"Hand-over in {folder}")
    print(f"  made        {time.strftime('%Y-%m-%d %H:%M', time.localtime(made)) if made else '?'}"
          f" by Ninaivu {manifest.get('version', '?')}")
    index = manifest.get("index") or {}
    print(f"  index       {index.get('file', '?')}, {int(index.get('size') or 0):,} bytes")
    for root, ident in (manifest.get("libraries") or {}).items():
        print(f"  library     {root}  (id {ident})")
    return 0


# -- unit --------------------------------------------------------------------

def find_library(mirror: Path, ident: str, depth: int = SEARCH_DEPTH) -> Path | None:
    """The folder under *mirror* whose ``.ninaivu-library`` carries *ident*."""
    from ..storage.library_id import MARKER, read_id                # noqa: PLC0415

    level = [mirror]
    for _ in range(depth + 1):
        following: list[Path] = []
        for folder in level:
            if (folder / MARKER).is_file() and read_id(folder) == ident:
                return folder
            try:
                following += sorted(p for p in folder.iterdir()
                                    if p.is_dir() and not p.is_symlink()
                                    and not p.name.startswith("."))
            except OSError:
                continue
        level = following
    return None


def systemd_quote(path: str) -> str:
    """One path for a unit file: quoted, with systemd's % specifiers escaped."""
    text = str(path).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{text}"'


def unit_text(*, python: str, source: str, state: str, binds: list[tuple[str, str]],
              user: str, port: int, primary: str = "", wake: str = "",
              workdir: str = "/opt/ninaivu") -> str:
    command = [python, "-m", "ninaivu", "--replica-of", source, "--state-dir", state,
               "--port", str(port), "--http"]
    if primary:
        command += ["--replica-primary", primary]
    if wake:
        command += ["--replica-wake", wake]
    exec_start = " ".join(systemd_quote(c) if (" " in c or '"' in c or "%" in c) else c
                          for c in command)
    lines = [
        "[Unit]",
        "Description=Ninaivu read-only replica (the Pi's copy of the Mac's library)",
        "Documentation=https://javajaga-usa.github.io/Ninaivu-releases/pi-replica/",
        "After=network-online.target local-fs.target",
        "Wants=network-online.target",
        f"RequiresMountsFor={systemd_quote(source)} {systemd_quote(state)}",
        "",
        "[Service]",
        "Type=simple",
        f"User={user}",
        f"Group={user}",
        f"WorkingDirectory={workdir}",
        f"ExecStart={exec_start}",
        "Restart=always",
        "RestartSec=5s",
        "TimeoutStopSec=45s",
        "# Port 80 without running as root.",
        "AmbientCapabilities=CAP_NET_BIND_SERVICE",
        "CapabilityBoundingSet=CAP_NET_BIND_SERVICE",
        "",
        "# Everything is read-only to this service but its own state folder: the",
        "# hand-over the Mac writes, and the library below, which is mounted at the",
        "# Mac's own path so the index and the thumbnail names fit as they are.",
        "NoNewPrivileges=true",
        "ProtectSystem=strict",
        "ProtectHome=read-only",
        "PrivateTmp=true",
        "PrivateDevices=true",
        "ProtectKernelTunables=true",
        "ProtectKernelModules=true",
        "ProtectControlGroups=true",
        "RestrictRealtime=true",
        "RestrictSUIDSGID=true",
        "LockPersonality=true",
        f"ReadWritePaths={systemd_quote(state)}",
    ]
    for mirror_path, mac_path in binds:
        lines.append(f"BindReadOnlyPaths={systemd_quote(mirror_path)}:{systemd_quote(mac_path)}")
    lines += [
        "# Under the Pi's 8 GB, leaving room for Samba, restic and the system.",
        "MemoryMax=2G",
        "StandardOutput=journal",
        "StandardError=journal",
        "SyslogIdentifier=ninaivu-replica",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ]
    return "\n".join(lines)


def cmd_unit(args) -> int:
    source = Path(args.source).expanduser()
    manifest = _manifest(source)
    libraries: dict[str, str] = dict(manifest.get("libraries") or {})
    roots = list((manifest.get("settings") or {}).get("roots") or libraries)
    given = {}
    for item in args.map or []:
        mac, sep, here = item.partition("=")
        if not sep:
            raise SystemExit(f"--map takes MAC_FOLDER=PI_FOLDER, not {item!r}")
        given[mac] = here
    if not roots:
        raise SystemExit("The hand-over names no library folder; is a library set on the Mac?")
    binds: list[tuple[str, str]] = []
    missing: list[str] = []
    for root in roots:
        if root in given:
            binds.append((given[root], root))
            continue
        ident = libraries.get(root)
        found = find_library(Path(args.mirror).expanduser(), ident) if (ident and args.mirror) else None
        if found is None:
            missing.append(root)
        else:
            binds.append((str(found), root))
    if missing:
        print("These library folders were not found under the mirror by their marker:",
              file=sys.stderr)
        for root in missing:
            print(f"  {root}", file=sys.stderr)
        print("Give each one with --map \"<Mac folder>=<folder on the Pi>\".", file=sys.stderr)
        return 2
    text = unit_text(python=args.python, source=str(source), state=args.state, binds=binds,
                     user=args.user, port=args.port, primary=args.primary or "",
                     wake=args.wake or "", workdir=args.workdir)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    print("\nBefore the first start, as root, make the empty folders the library is "
          "mounted on and the state folder:", file=sys.stderr)
    for _, mac in binds:
        print(f"  sudo mkdir -p {shlex.quote(mac)}", file=sys.stderr)
    print(f"  sudo install -d -o {args.user} -g {args.user} -m 700 {shlex.quote(args.state)}",
          file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    use_utf8_output()
    parser = argparse.ArgumentParser(prog="ninaivu replica",
                                     description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    pub = sub.add_parser("publish", help="write a hand-over for the Pi now (on the Mac)")
    pub.add_argument("--to", default=None, help="the hand-over folder (default: replica_dir)")
    pub.add_argument("--state-dir", default=None, help="Ninaivu's state folder")
    pub.set_defaults(run=cmd_publish)

    st = sub.add_parser("status", help="what a hand-over folder holds")
    st.add_argument("--from", dest="source", required=True)
    st.set_defaults(run=cmd_status)

    un = sub.add_parser("unit", help="print the Pi's systemd service")
    un.add_argument("--from", dest="source", required=True, help="the hand-over folder")
    un.add_argument("--mirror", default=None,
                    help="where the Pi keeps its copy of the library (searched for markers)")
    un.add_argument("--map", action="append", metavar="MAC=PI",
                    help="a library folder the marker search cannot find")
    un.add_argument("--state", default="/srv/ninaivu/replica-state",
                    help="the Pi's own state folder (writable, not on the SD card)")
    un.add_argument("--user", default="ninaivu")
    un.add_argument("--port", type=int, default=80)
    un.add_argument("--primary", default=None, help="the Mac mini's family app address")
    un.add_argument("--wake", default=None, help="the Mac mini's network (MAC) address")
    un.add_argument("--python", default="/opt/ninaivu/.venv/bin/python")
    un.add_argument("--workdir", default="/opt/ninaivu")
    un.add_argument("--output", default=None, help="write the unit here instead of printing it")
    un.set_defaults(run=cmd_unit)

    args = parser.parse_args(argv)
    return int(args.run(args) or 0)


if __name__ == "__main__":                       # pragma: no cover
    raise SystemExit(main())
