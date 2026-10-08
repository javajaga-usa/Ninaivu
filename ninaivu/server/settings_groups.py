"""Every setting in :class:`Config`, in the six groups a household thinks in.

``Config`` is one flat dataclass, and stays one: a hundred and thirty names
are read all over the package as ``cfg.thumb_quality`` and a nested layout
would change every one of those reads for no gain the household can see.
What the household sees is here instead — which group a setting belongs to,
whether it is one of the few that matter on the first screen, what it means
(the ``#:`` comment above it in ``config.py``), and its default — so the
console's Advanced page can show all of them without a hand-kept list that
drifts. A test checks that every field is in exactly one group.

The groups:

* **Library** — what is indexed and how.
* **People** — who may see what, and how the household signs in.
* **Backup** — Mugil, restore tests, copies of the index.
* **Remote access** — how Ninaivu is reached from outside, and how it tells
  the household about things.
* **AI** — every model and every pass.
* **Advanced** — thresholds, tuning and the settings that describe how this
  process was started. Nothing here needs touching in an ordinary house.
"""
from __future__ import annotations

import math
import functools
import re
from dataclasses import MISSING, fields
from pathlib import Path
from typing import Any

from .config import RUNTIME_ONLY, Config
from ..words import said

GROUPS: dict[str, tuple[str, ...]] = {
    said("Library"): (
        "roots", "active_root", "new_files_folder", "follow_symlinks",
        "index_hidden", "min_media_bytes", "ignore_dirs", "watch",
        "rescan_on_change", "boot_scan_after", "watch_debounce",
        "bin_erase_after_days", "hide_screens", "duplicate_distance",
        "quality_scan", "occasion_scan", "southern_hemisphere",
        "date_phrase_search", "phrase_search", "xmp_sidecars", "house_name",
    ),
    said("People"): (
        "open_browsing", "lock_after_minutes", "nsfw_filter",
        "phone_backup_trusted", "lock_roots", "first_day_done",
        "home_lat", "home_lon", "home_radius_m", "strip_location",
    ),
    said("Backup"): (
        "cloud_enabled", "cloud_folder_name", "cloud_autostart", "cloud_rate_kbps",
        "cloud_window_start", "cloud_window_end", "cloud_parallel", "cloud_full_speed",
        "cloud_encrypt", "cloud_hidden", "cloud_kinds", "cloud_max_mb", "cloud_approval_mb",
        "cloud_skip_folders", "cloud_skip_words", "restore_test_days", "restore_test_files",
        "restore_test_max_mb", "cloud_index_every_hours",
        "mirror_enabled", "mirror_dir", "mirror_every_hours", "mirror_verify_days",
        "offsite_enabled", "offsite_kind", "offsite_folder", "offsite_endpoint",
        "offsite_region", "offsite_bucket", "offsite_prefix", "offsite_access_key",
        "offsite_every_hours", "scrub_every_days", "scrub_repair", "backup_every_hours",
        "backup_keep", "backup_dir",
    ),
    said("Remote access"): (
        "network_access", "console_on_network", "console_from_internet", "tailnet_https", "remote_access", "remote_networks",
        "remote_hostname", "allowed_hosts", "notify_webhook", "notify_webhook_format",
        "notify_smtp_host", "notify_smtp_port", "notify_smtp_user",
        "notify_smtp_password", "notify_smtp_to", "notify_smtp_tls", "notify_events",
        "notify_quiet_seconds", "digest_enabled", "digest_to", "digest_weekday",
        "digest_hour", "digest_link",
    ),
    "AI": (
        "ai_enabled", "ai_engine", "hardware_tier", "ai_gpu", "clip_model", "clip_pretrained",
        "clip_batch_size", "tag_threshold", "max_tags", "nsfw_threshold",
        "faces_enabled", "place_names", "place_max_km", "map_tiles", "ocr_enabled",
        "ocr_min_score", "ocr_max_chars", "video_keyframes", "detect_orientation",
        "orientation_ai", "straighten_requires_face", "straighten_auto",
        "straighten_auto_apply",
        "ai_models_dir", "extensions", "outside_ai_for_family", "ai_server_url", "ai_server_enabled",
        "ai_server_edit_workflow", "ai_server_remove_workflow",
        "ai_server_upscale_workflow", "ai_server_restore_workflow",
        "ai_server_colorize_workflow", "ai_server_inpaint_workflow", "ai_server_timeout", "ai_server_max_side",
    ),
    said("Advanced"): (
        "workers", "thumb_sizes", "thumb_quality", "thumb_format", "video_thumbs",
        "video_thumb_offset", "perceptual_hash", "blur_threshold", "dark_threshold",
        "bright_threshold", "highlight_clip_threshold", "low_res_pixels",
        "occasion_gap_hours", "occasion_radius_km", "occasion_min_items",
        "proxy_cache_mb", "workload_mode", "workload_night_start",
        "workload_night_end", "tuning_profile", "tuning", "page_size", "max_page_size",
        # How this process was started. Shown, never saved: see RUNTIME_ONLY.
        "state_dir", "host", "port", "admin_host", "admin_port", "debug",
        "server_threads", "trusted_proxies", "max_upload_mb",
    ),
}

#: The ten a household changes, on the first screen of the Advanced page
#: before anything is folded away. Everything else is under its group.
FIRST_SCREEN: tuple[str, ...] = (
    "house_name", "open_browsing", "lock_after_minutes", "cloud_enabled",
    "remote_access", "ai_enabled", "faces_enabled", "place_names", "hide_screens",
    "straighten_auto",
)

#: Shown here, changed only on the page that owns them, because changing
#: them means more than writing a value: a library folder is checked against
#: the system folders and joins the index; Network access needs a restart the
#: Server page walks through; ``lock_roots`` is the hardening for a console
#: exposed beyond the machine, and must not be undone by the console itself;
#: an extension is switched on where it says what it sends.
MANAGED: dict[str, str] = {
    "roots": said("Library settings"),
    "active_root": said("Library settings"),
    "lock_roots": said("the command line (--lock-roots)"),
    "network_access": said("System → Server"),
    "extensions": said("System → Home & extensions → Extensions"),
    "first_day_done": said("the first-day walk-through"),
    "cloud_encrypt": said("Mugil (after making the encryption key)"),
    # Renaming the Drive folder also forgets the old folder's id (CloudService.
    # set_folder); saved here, the uploads went on into the old one.
    "cloud_folder_name": said("Mugil"),
    # Checked against the library and the state folder when it is chosen
    # (cloud_api.mirror_settings); typed in here, a folder inside the library
    # would be copied into itself.
    "mirror_dir": said("Mugil"),
    # Each knob is checked against its bounds and the running server is
    # re-tuned when it is saved (server/tuning.py).
    "tuning_profile": said("System → Tuning"),
    "tuning": said("System → Tuning"),
}

#: Values a text setting may take, where it is one of a few.
CHOICES: dict[str, tuple[str, ...]] = {
    "ai_engine": ("auto", "clip", "light", "off"),
    "hardware_tier": ("auto", "basic", "full"),
    "tuning_profile": ("auto", "small", "medium", "large", "peak"),
    "thumb_format": ("WEBP", "JPEG"),
    "workload_mode": ("balanced", "quiet", "overnight"),
    "cloud_hidden": ("never", "encrypted", "always"),
    "cloud_kinds": ("all", "no_video", "pictures"),
    "offsite_kind": ("folder", "s3"),
    "strip_location": ("off", "home", "all"),
    "notify_webhook_format": ("json", "ntfy", "form"),
}

#: Inclusive bounds for numbers (None: unbounded on that side).
RANGES: dict[str, tuple[float | None, float | None]] = {
    "thumb_quality": (1, 100), "workers": (1, 64), "page_size": (1, 5000),
    "max_page_size": (1, 20000), "clip_batch_size": (1, 256), "max_tags": (0, 100),
    "video_keyframes": (0, 5), "lock_after_minutes": (0, 1440),
    "notify_smtp_port": (1, 65535), "digest_weekday": (0, 6), "digest_hour": (0, 23),
    "ocr_max_chars": (0, 100000), "duplicate_distance": (0, 64),
    "occasion_min_items": (1, 1000), "restore_test_files": (0, 1000),
    # The bound Mugil's own page keeps (cloud_api.py); unbounded here, 500
    # meant 500 upload threads.
    "cloud_parallel": (1, 6),
    # The bounds Mugil's own page keeps for the backup rules (cloud_api.py).
    "cloud_max_mb": (0, 1024 * 1024), "cloud_approval_mb": (0, 1024 * 1024),
    # And those the second copy, the off-site copy and the storage check keep.
    "mirror_every_hours": (0, 24 * 90), "mirror_verify_days": (0, 365),
    "offsite_every_hours": (1, 24 * 30), "scrub_every_days": (0, 365),
    "home_lat": (-90, 90), "home_lon": (-180, 180), "home_radius_m": (50, 20000),
    # Not a 0-1 score like the other thresholds: a sharpness measure whose
    # default is 45, which the rule for "_threshold" below refused outright.
    "blur_threshold": (0, 10000),
    # A score from 0 to 1 by another name; above 1 every line read was dropped.
    "ocr_min_score": (0, 1),
}

#: Settings that take a clock time, ``HH:MM``, or nothing.
CLOCK = frozenset({"cloud_window_start", "cloud_window_end", "workload_night_start",
                   "workload_night_end"})
#: Numbers that may also be empty (None), which their default is.
OPTIONAL_NUMBERS = frozenset({"home_lat", "home_lon"})
#: Settings that are an address to send something to.
URLS = frozenset({"notify_webhook", "ai_server_url", "digest_link", "offsite_endpoint"})

#: Written but never read back: the page shows whether one is set, not what.
#: A Slack, Discord or ntfy webhook address is a password too — whoever has
#: it can post into the household's channel, or read every alert from it.
SECRETS: frozenset[str] = frozenset({"notify_smtp_password", "notify_webhook"})


def masked_url(url: str) -> str:
    """Enough of a saved address to recognise it — the scheme and the host —
    and none of the part that is the secret. Empty when nothing is saved."""
    from urllib.parse import urlsplit

    if not url:
        return ""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return "…"
    return f"{parts.scheme}://{host}{port}/…" if parts.scheme and host else "…"


def keeps_secret(name: str, raw: Any, saved: Any) -> bool:
    """Whether *raw*, sent for the secret *name*, means "leave it as it is".

    A form cannot show a secret, so it sends back blank — or, for an address,
    the masked hint it was shown. Neither is a new value.
    """
    if name not in SECRETS or raw is None:
        return False
    text = str(raw).strip()
    return not text or (name in URLS and text == masked_url(str(saved or "")))

_DOC = re.compile(r"^\s*#:\s?(.*)$")
_FIELD = re.compile(r"^\s{4}([a-z_][a-z0-9_]*)\s*:")


@functools.cache
def _docs() -> dict[str, str]:
    """The ``#:`` comment above each field of ``Config``, by field name.

    Read once: config.py does not change while the server runs, and the
    Advanced page asked for this on every load.
    """
    out: dict[str, str] = {}
    pending: list[str] = []
    try:
        source = (Path(__file__).with_name("config.py")).read_text(encoding="utf-8")
    except OSError:
        return out
    for line in source.splitlines():
        doc = _DOC.match(line)
        if doc:
            pending.append(doc.group(1).rstrip())
            continue
        named = _FIELD.match(line)
        if named and pending:
            text = " ".join(p for p in pending if p).strip()
            # The comments are reStructuredText for the code; plain for the page.
            text = re.sub(r":(?:attr|class|mod|func):`~?([^`]+)`", r"\1", text)
            out[named.group(1)] = text.replace("``", "").replace("*", "")
        if not line.strip().startswith("#"):
            pending = []
    return out


def group_of(name: str) -> str | None:
    for group, names in GROUPS.items():
        if name in names:
            return group
    return None


def _kind(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, (list, tuple, set, frozenset, dict)):
        return "list"
    return "str"


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return [f"{key} = {value[key]}" for key in sorted(value)]
    if isinstance(value, (set, frozenset, tuple)):
        return sorted(value) if isinstance(value, (set, frozenset)) else list(value)
    if isinstance(value, Path):
        return str(value)
    return value


def describe(cfg: Config) -> dict[str, Any]:
    """Everything the Advanced page shows: groups in order, each setting with
    its value, default, kind and meaning. Secrets say only whether they are set."""
    docs = _docs()
    defaults = {}
    for f in fields(Config):
        if f.default is not MISSING:
            defaults[f.name] = f.default
        elif f.default_factory is not MISSING:            # type: ignore[misc]
            defaults[f.name] = f.default_factory()        # type: ignore[misc]
    groups = []
    for group, names in GROUPS.items():
        items = []
        for name in names:
            default = defaults.get(name)
            value = getattr(cfg, name, default)
            kind = ("float" if name in OPTIONAL_NUMBERS
                    else _kind(default if default is not None else value))
            item: dict[str, Any] = {
                "name": name,
                "kind": kind,
                "doc": docs.get(name, ""),
                "default": _plain(default),
                "first_screen": name in FIRST_SCREEN,
                "runtime": name in RUNTIME_ONLY,
                "managed_by": MANAGED.get(name),
                "choices": list(CHOICES.get(name, ())),
                "changed": _plain(value) != _plain(default),
            }
            if name in SECRETS:
                item["secret"] = True
                item["value"] = bool(value)
                item["changed"] = bool(value)
                if name in URLS:
                    item["hint"] = masked_url(str(value or ""))
            else:
                item["value"] = _plain(value)
            items.append(item)
        groups.append({"name": group, "settings": items})
    return {"groups": groups, "first_screen": list(FIRST_SCREEN)}


class BadValue(ValueError):
    pass


def _default(name: str) -> Any:
    for f in fields(Config):
        if f.name == name:
            if f.default is not MISSING:
                return f.default
            if f.default_factory is not MISSING:          # type: ignore[misc]
                return f.default_factory()                # type: ignore[misc]
    return None


def _bool(name: str, raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int) and raw in (0, 1):
        return bool(raw)
    if isinstance(raw, str):
        word = raw.strip().lower()
        if word in ("1", "true", "yes", "on"):
            return True
        if word in ("0", "false", "no", "off"):
            return False
    raise BadValue(f"{name} is on or off")


def _number(name: str, raw: Any, whole: bool) -> float | int:
    if isinstance(raw, bool):
        raise BadValue(f"{name} is a number")
    try:
        value = float(raw.strip()) if isinstance(raw, str) else float(raw)
    except (TypeError, ValueError) as exc:
        raise BadValue(f"{name} is a number") from exc
    if not math.isfinite(value):
        raise BadValue(f"{name} must be an ordinary number")
    if whole:
        if value != int(value):
            raise BadValue(f"{name} is a whole number")
        value = int(value)
    low, high = RANGES.get(name, (0, None))
    if name.endswith("_threshold") and name not in RANGES:
        low, high = 0.0, 1.0
    if low is not None and value < low:
        raise BadValue(f"{name} is at least {low:g}")
    if high is not None and value > high:
        raise BadValue(f"{name} is at most {high:g}")
    return value


def _text(name: str, raw: Any) -> str | None:
    from urllib.parse import urlsplit

    default = _default(name)
    if raw is None:
        if default is None:
            return None
        raise BadValue(f"{name} cannot be empty like that; send an empty text instead")
    if not isinstance(raw, (str, int, float)) or isinstance(raw, bool):
        raise BadValue(f"{name} is text")
    text = str(raw).strip()
    if len(text) > 2000:
        raise BadValue(f"{name} is too long")
    if name in CHOICES:
        wanted = {c.lower(): c for c in CHOICES[name]}
        if text.lower() not in wanted:
            raise BadValue(f"{name} is one of: {', '.join(CHOICES[name])}")
        return wanted[text.lower()]
    if name == "remote_access":
        from . import remote                                   # noqa: PLC0415
        if text.lower() not in remote.PROVIDERS:
            raise BadValue(f"remote_access is one of: {', '.join(remote.PROVIDERS)}")
        return text.lower()
    if name == "house_name":
        from .config import clean_home_name                   # noqa: PLC0415
        return clean_home_name(text)
    if name == "remote_hostname":
        host = text.lower()
        if host and (len(host) > 253 or any(c.isspace() or c in "/\\@:" for c in host)):
            raise BadValue("remote_hostname is a host name")
        return host
    if name == "clip_pretrained":
        # A tag, never a path: a file named here would be unpickled by torch.
        if any(c in text for c in "/\\") or text.startswith("."):
            raise BadValue("clip_pretrained is a tag such as laion2b_s34b_b79k, not a file")
        return text
    if name in CLOCK:
        if text and not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", text):
            raise BadValue(f"{name} is a time like 23:00")
        return text
    if name in URLS:
        if text and urlsplit(text).scheme not in ("http", "https"):
            raise BadValue(f"{name} is an http:// or https:// address")
        if name == "ai_server_url" and text:
            return _ai_server_address(text)
        return text
    return text


def _ai_server_address(text: str) -> str:
    """The AI server's address, held to the rules its own page keeps.

    Photographs are sent to it, so the Creative Studio's check applies here
    too: nowhere on the internet, and a host name it cannot place only over
    https. The check lives in the extension, which may not be installed;
    without it the address is never used, and the http(s) test above is all.
    """
    try:
        from ninaivu_studio.ai_server import comfyui           # noqa: PLC0415
    except ImportError:
        return text
    try:
        return comfyui.check_address(text)
    except ValueError as exc:
        raise BadValue(f"ai_server_url: {exc}") from exc


def _list(name: str, raw: Any) -> Any:
    import ipaddress

    default = _default(name)
    if isinstance(raw, str):
        # One per line: a comma is a legal character in a folder name.
        raw = [p.strip() for p in raw.splitlines() if p.strip()]
    if not isinstance(raw, list) or not all(isinstance(v, (str, int)) and not isinstance(v, bool)
                                            for v in raw):
        raise BadValue(f"{name} is a list, one per line")
    if name == "remote_networks":
        try:
            return [str(ipaddress.ip_network(str(v).strip(), strict=False)) for v in raw]
        except ValueError as exc:
            raise BadValue(f"remote_networks: {exc}") from exc
    if name == "thumb_sizes":
        sizes = tuple(int(_number(name, v, True)) for v in raw)
        if not sizes or min(sizes) < 64 or max(sizes) > 4096:
            raise BadValue("thumb_sizes are between 64 and 4096 pixels")
        return sizes
    if name == "notify_events":
        from ..utils import notify                            # noqa: PLC0415
        unknown = [v for v in raw if v not in notify.EVENTS]
        if unknown:
            raise BadValue(f"notify_events: unknown {', '.join(map(str, unknown))}")
    if name == "allowed_hosts":
        bad = [v for v in raw if not re.fullmatch(r"(\*\.)?[a-z0-9.-]+", str(v).strip().lower())]
        if bad:
            raise BadValue(f"allowed_hosts are host names: {', '.join(map(str, bad))}")
        return [str(v).strip().lower() for v in raw]
    if isinstance(default, (set, frozenset)):
        return type(default)(str(v) for v in raw)
    if isinstance(default, tuple):
        return tuple(str(v) for v in raw)
    return [str(v) for v in raw]


def coerce(name: str, raw: Any) -> Any:
    """*raw* (from JSON) as the type and within the bounds the field takes,
    or :class:`BadValue` saying what it should be."""
    default = _default(name)
    if name in OPTIONAL_NUMBERS:
        # A number, or nothing: the home zone is not set until somebody sets it.
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return None
        return float(_number(name, raw, whole=False))
    kind = _kind(default) if default is not None else "str"
    if kind == "bool":
        return _bool(name, raw)
    if kind in ("int", "float"):
        return _number(name, raw, whole=(kind == "int"))
    if kind == "list":
        return _list(name, raw)
    return _text(name, raw)


def apply(cfg: Config, changes: dict[str, Any]) -> list[str]:
    """Set the *changes* on *cfg*, checked one by one, and say which changed.
    Nothing is written until everything in the request is acceptable."""
    staged: dict[str, Any] = {}
    for name, raw in changes.items():
        if group_of(name) is None:
            raise BadValue(f"{name} is not a setting")
        if name in RUNTIME_ONLY:
            raise BadValue(f"{name} is set when Ninaivu starts, not here")
        if name in MANAGED:
            raise BadValue(f"{name} is changed on {MANAGED[name]}")
        if keeps_secret(name, raw, getattr(cfg, name, "")):
            continue                        # blank, or the hint: keep the saved one
        staged[name] = coerce(name, raw)
    if staged.get("backup_dir"):
        # The bundles hold the settings, the keys and every name in the index:
        # never inside a library folder (shown and uploaded like photographs)
        # or the second copy. Checked again each time one is written.
        from ..storage import backup                             # noqa: PLC0415
        try:
            backup.check_folder(staged["backup_dir"], cfg)
        except ValueError as exc:
            raise BadValue(f"backup_dir: {exc}") from exc
    changed = []
    for name, value in staged.items():
        if getattr(cfg, name, None) != value:
            setattr(cfg, name, value)
            changed.append(name)
    return changed
