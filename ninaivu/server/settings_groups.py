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

import re
from dataclasses import MISSING, fields
from pathlib import Path
from typing import Any

from .config import RUNTIME_ONLY, Config

GROUPS: dict[str, tuple[str, ...]] = {
    "Library": (
        "roots", "active_root", "new_files_folder", "follow_symlinks",
        "index_hidden", "min_media_bytes", "ignore_dirs", "watch",
        "rescan_on_change", "boot_scan_after", "watch_debounce",
        "bin_erase_after_days", "hide_screens", "duplicate_distance",
        "quality_scan", "occasion_scan", "southern_hemisphere",
        "date_phrase_search", "house_name",
    ),
    "People": (
        "open_browsing", "lock_after_minutes", "nsfw_filter",
        "phone_backup_trusted", "lock_roots", "first_day_done",
    ),
    "Backup": (
        "cloud_enabled", "cloud_folder_name", "cloud_autostart", "cloud_rate_kbps",
        "cloud_window_start", "cloud_window_end", "cloud_parallel", "cloud_full_speed",
        "cloud_encrypt", "cloud_hidden", "restore_test_days", "restore_test_files",
        "restore_test_max_mb", "cloud_index_every_hours", "backup_every_hours",
        "backup_keep", "backup_dir",
    ),
    "Remote access": (
        "network_access", "tailnet_https", "remote_access", "remote_networks",
        "remote_hostname", "update_check", "notify_webhook", "notify_webhook_format",
        "notify_smtp_host", "notify_smtp_port", "notify_smtp_user",
        "notify_smtp_password", "notify_smtp_to", "notify_smtp_tls", "notify_events",
        "notify_quiet_seconds", "digest_enabled", "digest_to", "digest_weekday",
        "digest_hour", "digest_link",
    ),
    "AI": (
        "ai_enabled", "ai_engine", "ai_gpu", "clip_model", "clip_pretrained",
        "clip_batch_size", "tag_threshold", "max_tags", "nsfw_threshold",
        "faces_enabled", "place_names", "place_max_km", "map_tiles", "ocr_enabled",
        "ocr_min_score", "ocr_max_chars", "video_keyframes", "detect_orientation",
        "orientation_ai", "straighten_requires_face", "straighten_auto",
        "ai_models_dir", "extensions", "ai_server_url", "ai_server_enabled",
        "ai_server_edit_workflow", "ai_server_remove_workflow",
        "ai_server_upscale_workflow", "ai_server_restore_workflow",
        "ai_server_colorize_workflow", "ai_server_timeout", "ai_server_max_side",
    ),
    "Advanced": (
        "workers", "thumb_sizes", "thumb_quality", "thumb_format", "video_thumbs",
        "video_thumb_offset", "perceptual_hash", "blur_threshold", "dark_threshold",
        "bright_threshold", "highlight_clip_threshold", "low_res_pixels",
        "occasion_gap_hours", "occasion_radius_km", "occasion_min_items",
        "proxy_cache_mb", "workload_mode", "workload_night_start",
        "workload_night_end", "page_size", "max_page_size",
        # How this process was started. Shown, never saved: see RUNTIME_ONLY.
        "state_dir", "host", "port", "admin_host", "admin_port", "debug",
        "server_threads", "trusted_proxies", "max_upload_mb",
    ),
}

#: The ten a household changes, on the first screen of the Advanced page
#: before anything is folded away. Everything else is under its group.
FIRST_SCREEN: tuple[str, ...] = (
    "house_name", "roots", "open_browsing", "cloud_enabled", "network_access",
    "remote_access", "ai_enabled", "faces_enabled", "update_check", "straighten_auto",
)

#: Written but never read back: the page shows whether one is set, not what.
SECRETS: frozenset[str] = frozenset({"notify_smtp_password"})

_DOC = re.compile(r"^\s*#:\s?(.*)$")
_FIELD = re.compile(r"^\s{4}([a-z_][a-z0-9_]*)\s*:")


def _docs() -> dict[str, str]:
    """The ``#:`` comment above each field of ``Config``, by field name."""
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
    if isinstance(value, (list, tuple, set, frozenset)):
        return "list"
    return "str"


def _plain(value: Any) -> Any:
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
            kind = _kind(default if default is not None else value)
            item: dict[str, Any] = {
                "name": name,
                "kind": kind,
                "doc": docs.get(name, ""),
                "default": _plain(default),
                "first_screen": name in FIRST_SCREEN,
                "runtime": name in RUNTIME_ONLY,
                "changed": _plain(value) != _plain(default),
            }
            if name in SECRETS:
                item["secret"] = True
                item["value"] = bool(value)
                item["changed"] = bool(value)
            else:
                item["value"] = _plain(value)
            items.append(item)
        groups.append({"name": group, "settings": items})
    return {"groups": groups, "first_screen": list(FIRST_SCREEN)}


class BadValue(ValueError):
    pass


def coerce(name: str, raw: Any) -> Any:
    """*raw* (from JSON) as the type the field holds, or :class:`BadValue`."""
    default = None
    for f in fields(Config):
        if f.name == name:
            default = f.default if f.default is not MISSING else (
                f.default_factory() if f.default_factory is not MISSING else None)  # type: ignore[misc]
            break
    kind = _kind(default) if default is not None else _kind(raw)
    try:
        if kind == "bool":
            if isinstance(raw, str):
                return raw.strip().lower() in ("1", "true", "yes", "on")
            return bool(raw)
        if kind == "int":
            return int(raw)
        if kind == "float":
            return float(raw)
        if kind == "list":
            if isinstance(raw, str):
                raw = [p.strip() for p in raw.split(",") if p.strip()]
            if not isinstance(raw, list):
                raise BadValue(f"{name} is a list")
            if isinstance(default, (set, frozenset)):
                return type(default)(str(v) for v in raw)
            if isinstance(default, tuple):
                return tuple(int(v) for v in raw)
            return [str(v) for v in raw]
        if raw is None:
            return None
        return str(raw)
    except (TypeError, ValueError) as exc:
        raise BadValue(f"{name}: {exc}") from exc


def apply(cfg: Config, changes: dict[str, Any]) -> list[str]:
    """Set the *changes* on *cfg*, checked one by one, and say which changed.
    Nothing is written until everything in the request is acceptable."""
    staged: dict[str, Any] = {}
    for name, raw in changes.items():
        if group_of(name) is None:
            raise BadValue(f"{name} is not a setting")
        if name in RUNTIME_ONLY:
            raise BadValue(f"{name} is set when Ninaivu starts, not here")
        staged[name] = coerce(name, raw)
    changed = []
    for name, value in staged.items():
        if getattr(cfg, name, None) != value:
            setattr(cfg, name, value)
            changed.append(name)
    return changed
