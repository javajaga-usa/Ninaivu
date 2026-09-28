"""Choosing the library folder, scanning it, and the settings that go with it.

Split out of api.py. The routes, their paths and their blueprints are exactly
as they were."""

from __future__ import annotations

from pathlib import Path
import os
import sys

from flask import abort, jsonify, request
from ..server import auth
from ..server.auth import current_user, require_admin
from ..server.config import Config, default_browse_roots, is_browsable, is_forbidden_root, shell_namespace_hint
from ._body import json_object

# The blueprints and the shared helpers stay in api.py: these routes
# are registered on the same two blueprints they always were, so every
# path and every endpoint name is unchanged.
from .api import admin_only, _cfg, _conn, _safe_under, _scanner

#: What to do instead of reading a phone directly, in the words of the computer
#: Ninaivu runs on: a Mac has no Explorer and no Windows Import.
_COPY_FIRST = {
    "win32": "Copy the photographs into a folder first — in Explorer, or with "
             "Windows' own Import — and point Ninaivu at that.",
    "darwin": "Copy the photographs into a folder first — in Finder, or with "
              "Image Capture or the Photos app — and point Ninaivu at that.",
}.get(sys.platform, "Copy the photographs into a folder first and point "
                    "Ninaivu at that.")


# ---------------------------------------------------------------------------
# Library management
# ---------------------------------------------------------------------------

def _browse_roots(cfg: Config) -> list[Path]:
    """Folders the console's picker offers as starting points."""
    configured = [Path(r).expanduser() for r in cfg.roots]
    if cfg.lock_roots:
        return configured
    return configured + [p for p in default_browse_roots() if p not in configured]



def _is_network_path(path: Path) -> bool:
    """A UNC name, or a drive letter Windows has mapped to one."""
    text = str(path)
    if text.startswith("\\\\") or text.startswith("//"):
        return True
    try:
        from ..server.config import network_drives                # noqa: PLC0415

        root = os.path.splitdrive(os.path.abspath(text))[0]
        return bool(root) and f"{root}\\" in network_drives()
    except Exception:                                     # noqa: BLE001
        return False


def _network_aware_reason(path: Path, exc: Exception) -> str:
    """Why a folder would not open, in terms of what to do about it.

    A share refusing a program that the same share opens happily in Explorer
    is almost never a broken path — it is credentials. Windows keeps network
    logons per user and per session, so a Ninaivu running as a service, or as
    a different account, holds none of the ones Explorer is using.
    """
    if not _is_network_path(path):
        return f"That folder could not be opened: {exc}"
    return (
        f"“{path}” is a network location and it refused this connection "
        f"({exc}). Windows keeps network sign-ins per account: if Ninaivu runs "
        f"as a service or as a different user than the one that mapped the "
        f"drive, it holds none of your credentials and every folder on the "
        f"share is denied. Open the share once in Explorer as the same "
        f"account Ninaivu runs under, ticking “Remember my credentials”, or "
        f"run Ninaivu as that account."
    )


def _can_browse(path: Path, cfg: Config) -> bool:
    """Whether the picker may *list* a directory.

    Deliberately permissive: you have to pass through ``C:\\`` to reach
    ``C:\\Master``. The endpoint is already behind an admin sign-in, and
    listing folder names is not the same as indexing what is in them.
    """
    if not is_browsable(path):
        return False
    if not cfg.lock_roots:
        return True
    return any(_safe_under(path, root) or path == root.resolve()
               for root in _browse_roots(cfg) if root.exists())


def _can_be_library(path: Path, cfg: Config) -> bool:
    """Whether a directory may be *set* as the media library."""
    return _can_browse(path, cfg) and not is_forbidden_root(path)


def _shortcuts(cfg: Config) -> list[dict[str, str]]:
    """Named jumping-off points for the picker, most useful first.

    A network drive is listed without being touched. ``is_dir`` on a mapped
    drive whose server is asleep waits for SMB to give up, and doing that for
    every shortcut on every folder the picker opens is what made network
    drives unlistable in the first place. They are offered, and they answer
    for themselves when somebody actually clicks one.
    """
    from ..server.config import network_drives                      # noqa: PLC0415

    home = Path.home()
    network = network_drives()
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for root in _browse_roots(cfg):
        key = str(root)
        remote = key in network or key.startswith("\\\\")
        if not remote:
            try:
                if not root.is_dir():
                    continue
            except OSError:
                continue
        if key in seen:
            continue
        seen.add(key)
        if root == home:
            label = "Home"
        elif str(root) in {str(r) for r in [Path(x) for x in cfg.roots]}:
            label = f"{root.name or key} (current)"
        elif len(key) <= 3 and key[1:2] == ":":
            label = f"{key[0].upper()}: drive"
        else:
            label = root.name or key
        entry = {"name": label, "path": key}
        if remote:
            entry["name"] = f"{label} (network)"
            entry["network"] = "1"
        out.append(entry)

    try:
        from ..utils import devices                                   # noqa: PLC0415
        if devices.available():
            for dev in devices.list_devices():
                out.append({"name": f"{dev['name']} (device)", "path": dev["path"]})
    except Exception:
        pass

    return out[:14]


def _root_refusal(path: Path, cfg: Config) -> str:
    """A refusal that says what to do about it, not just that it happened."""
    if is_forbidden_root(path):
        return (f"“{path}” is a system folder, so Ninaivu won't index it. "
                f"Pick a folder that holds your media — for example "
                f"C:\\Master\\Photos rather than C:\\.")
    if cfg.lock_roots:
        allowed = ", ".join(str(r) for r in _browse_roots(cfg)) or "(none)"
        return (f"“{path}” is outside the folders this console may open, "
                f"because Ninaivu was started with --lock-roots. "
                f"Allowed: {allowed}. Restart with --allow \"{path}\" to add it.")
    return (f"“{path}” could not be opened. Check the path exists and that "
            f"Ninaivu has permission to read it.")


@admin_only.get("/api/library/browse")
@require_admin
def browse():
    """Server-side directory picker, so nobody has to type an absolute path."""
    cfg = _cfg()
    raw = request.args.get("path", "").strip()
    if raw:
        target = Path(raw).expanduser()
    elif cfg.active_root:
        target = Path(cfg.active_root)
    elif cfg.roots:
        target = Path(cfg.roots[0])
    else:
        target = _browse_roots(cfg)[0] if _browse_roots(cfg) else Path.home()

    try:
        from ..utils import devices                                   # noqa: PLC0415
    except Exception:
        devices = None

    # Only where devices can be read: elsewhere the hint below names the device
    # and says what to do, where this said only that it was a Windows feature.
    if devices and devices.available() and devices.looks_like_device_path(raw):
        try:
            device_entries = devices.list_folder(raw)
        except Exception as exc:                                  # noqa: BLE001
            # Locked, unplugged, or no shell to ask: say what to do instead,
            # not only what went wrong.
            abort(400, description=f"{exc} Or c{_COPY_FIRST[1:]}")

        norm = raw.replace("/", "\\").rstrip("\\")
        parts = [p for p in norm.split("\\") if p]
        parent = "\\".join(parts[:-1]) if len(parts) > 2 else ""

        dirs = [
            {"name": e["name"], "path": e["path"]}
            for e in device_entries
            if e.get("is_folder")
        ]
        return jsonify({
            "path": raw,
            "parent": parent,
            "dirs": dirs[:500],
            "shortcuts": _shortcuts(cfg),
            "locked": False,
            "selectable": True,
        })

    # Kernel filesystems are refused as typed, on any host: on Windows "/proc"
    # resolves to C:\proc, which the rules would no longer recognise.
    # A phone or camera over USB is not a folder on a disk. Say that, rather
    # than "not a directory", which is true and tells nobody anything.
    hint = shell_namespace_hint(raw)
    if hint:
        abort(400, description=f"{hint} {_COPY_FIRST}")

    if raw and not is_browsable(target):
        abort(403, description=_root_refusal(target, cfg))

    try:
        target = target.resolve()
    except OSError as exc:
        abort(400, description=_network_aware_reason(target, exc))

    if not _can_browse(target, cfg):
        abort(403, description=_root_refusal(target, cfg))
    if not target.is_dir():
        if raw and is_forbidden_root(Path(raw).expanduser()):
            # A system folder named in its own dialect on a host where it is
            # absent — "/etc" on Windows is C:\etc, which is nothing. The
            # answer about the folder asked for is "nothing to pick here",
            # not "not found".
            return jsonify({
                "path": raw,
                "parent": None,
                "dirs": [],
                "shortcuts": _shortcuts(cfg),
                "locked": cfg.lock_roots,
                "selectable": False,
            })
        abort(404, description="Not a directory")

    entries = []
    try:
        for entry in sorted(os.scandir(target), key=lambda e: e.name.lower()):
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    entries.append({"name": entry.name, "path": entry.path})
            except OSError:
                continue
    except PermissionError as exc:
        abort(403, description=_network_aware_reason(target, exc))

    parent = str(target.parent) if target.parent != target else None
    if parent and not _can_browse(Path(parent), cfg):
        parent = None
    return jsonify({
        "path": str(target),
        "parent": parent,
        "dirs": entries[:500],
        # One-click shortcuts, so nobody has to climb the tree from wherever
        # the current library happens to be.
        "shortcuts": _shortcuts(cfg),
        "locked": cfg.lock_roots,
        # Whether *this* folder could be used as the library, so the picker
        # can grey out "Use this folder" instead of failing on submit.
        "selectable": _can_be_library(target, cfg),
    })


@admin_only.post("/api/library/root")
@require_admin
def set_root():
    cfg = _cfg()
    data = json_object()
    raw = str(data.get("path", "")).strip()
    if not raw:
        return jsonify({"error": "Path required"}), 400

    target = Path(raw).expanduser()
    # Refused as typed, before anything else: a system folder is never a
    # library, whether or not it exists on this machine. On Windows "/etc"
    # resolves to C:\etc — likely absent — and checking existence first would
    # answer "not found" to what is really "not allowed".
    if is_forbidden_root(target):
        return jsonify({"error": _root_refusal(target, cfg)}), 403
    try:
        target = target.resolve()
    except OSError:
        return jsonify({"error": f"“{raw}” could not be read."}), 400
    if not target.exists():
        return jsonify({
            "error": f"“{target}” does not exist. Check the spelling — on "
                     f"Windows use a path like C:\\Master\\Photos."
        }), 400
    if not target.is_dir():
        return jsonify({"error": f"“{target}” is a file, not a folder."}), 400
    if not _can_be_library(target, cfg):
        return jsonify({"error": _root_refusal(target, cfg)}), 403

    added = cfg.add_library(str(target))
    cfg.active_root = str(target)
    cfg.save()
    _scanner().start(target, full=bool(data.get("full")))
    auth.audit(_conn(), current_user().id,
               "add_library" if added else "set_library", str(target))
    return jsonify({
        "ok": True, "root": cfg.active_root, "added": added,
        "folders": cfg.libraries,
    })


@admin_only.post("/api/scan")
@require_admin
def rescan():
    data = json_object()
    _scanner().start(None, full=bool(data.get("full")))
    return jsonify({
        "ok": True, "full": bool(data.get("full")),
        "folders": _cfg().libraries,
    })


@admin_only.post("/api/scan/stop")
@require_admin
def scan_stop():
    _scanner().stop()
    return jsonify({"ok": True})


@admin_only.post("/api/settings")
@require_admin
def settings():
    cfg = _cfg()
    data = json_object()
    changed = []
    for key in ("nsfw_filter", "watch", "ai_enabled"):
        if key in data:
            setattr(cfg, key, bool(data[key]))
            changed.append(key)
    if "ai_engine" in data and data["ai_engine"] in ("auto", "clip", "light", "off"):
        cfg.ai_engine = data["ai_engine"]
        changed.append("ai_engine")
    if changed:
        cfg.save()
    return jsonify({"ok": True, "changed": changed})
