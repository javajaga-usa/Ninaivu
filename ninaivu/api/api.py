"""HTTP API.

Two security choices worth calling out:

* **Nothing is served by user-supplied path.** Originals, downloads and
  thumbnails are all addressed by integer asset id and resolved through the
  index, so path traversal has no surface to attack. Every resolved path is
  still re-checked against the active root before it is opened.
* **Root switching is admin-only, and system folders are refused.** The
  picker is behind an admin sign-in, so it may browse the machine; but a
  system directory can never become the library, and ``--lock-roots``
  confines it to ``--root``/``--allow`` when the console is exposed.
"""

from __future__ import annotations

import json
import zipfile
import mimetypes
import os
import queue
import secrets
import threading
import time
import weakref
from pathlib import Path
from typing import Any, Sequence


from flask import (
    Blueprint, Response, abort, current_app, g, jsonify, render_template,
    request, send_file, stream_with_context,
)

from PIL import Image

from .. import ai as ai_mod
from ..utils import proxies, query
from ..server import activity as activity_kit, auth, turn as turn_file
from ..storage import db, new_files, recycle
from ..media import media, stills, upright
from ..server.auth import (
    VIS_HIDDEN, VIS_NAMES, VIS_VALUES, current_user, require_admin, require_family,
)
from ..server.config import (
    BROWSER_NATIVE, Config, house_name,
)
from ._body import json_object

#: Routes both faces serve.
bp = Blueprint("api", __name__)


#: Library-level operations. Registered only on the admin console, so on the
#: family app these paths do not exist at all — a 404, not a 403.
admin_only = Blueprint("api_admin", __name__)

#: What an upload may be.
#:
#: The endpoint used to take anything at all: a family member could write an
#: .html or an .exe into the library root and Ninaivu would index it as an
#: asset. Allowing only what the scanner can actually read is both the
#: security fix and the honest description of the feature — this is a photo
#: library, and a file it cannot open is not a photograph it failed to show,
#: it is a file that should never have been accepted.
UPLOAD_EXTENSIONS = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff",
    ".heic", ".heif", ".avif",
    ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".3gp",
    ".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".opus",
    ".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2", ".raf", ".srw",
})

#: What ``/api/file`` will name a type as, and nothing else.
#:
#: The original is served inline with a type guessed from its name, so a file
#: called .html came back as text/html from Ninaivu's own origin — a page of
#: somebody else's choosing on the address the household has been taught to
#: trust. Anything not on this list is handed over as a download instead of
#: being rendered.
INLINE_TYPES = frozenset({
    "image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp",
    "image/tiff", "image/heic", "image/heif", "image/avif",
    "video/mp4", "video/quicktime", "video/x-m4v", "video/webm",
    "audio/mpeg", "audio/mp4", "audio/aac", "audio/flac", "audio/wav",
    "audio/ogg", "audio/opus",
})


KIND_CODES = {"picture": 0, "video": 1, "audio": 2}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg() -> Config:
    return current_app.config["MV_CONFIG"]


def _conn():
    return db.connect(_cfg().db_path)


def _scanner():
    return current_app.config["MV_SCANNER"]


def _engine():
    return current_app.config.get("MV_ENGINE")


def _roots() -> list[str]:
    """Library folders the caller may query — never more than they're assigned."""
    cfg = _cfg()
    libraries = cfg.libraries or ([cfg.active_root] if cfg.active_root else [])
    if not libraries:
        abort(409, description="No library folder has been set up yet.")
    roots, _ = auth.resolve_library(current_user().assigned_library, libraries)
    return roots


def _root() -> str:
    """The single folder the console operates on (admin-facing routes only)."""
    root = _cfg().active_root or (_cfg().libraries or [None])[0]
    if not root:
        abort(409, description="No library folder has been set up yet.")
    return root


def _bool_arg(name: str, default: bool = False) -> bool:
    raw = request.args.get(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _int_arg(name: str, default: int = 0) -> int:
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default
    # SQLite's integers are 64-bit. A larger one was a 500 wherever it reached
    # a query — ``?limit=99999999999999999999`` on the face pages.
    return max(-(2 ** 63), min(2 ** 63 - 1, value))


def _rating(value: Any) -> int:
    """A 0–5 star rating from a request body, or a 400 — never a 500."""
    if value is None or value == "":
        return 0
    try:
        if isinstance(value, bool):
            raise TypeError
        return max(0, min(5, int(value)))
    except (TypeError, ValueError, OverflowError):
        abort(400, description="A rating is a whole number from 0 to 5.")


#: The largest id SQLite can hold. Past it, sqlite3 raises OverflowError on
#: binding, which none of the routes expected from a number.
_MAX_ID = 2 ** 63 - 1


def _as_id(value: Any) -> int | None:
    """*value* as a positive item id, or ``None`` when it is not one.

    ``str.isdigit`` is true for more than ``int`` accepts — "²" and other
    Unicode digits — and for digit strings of any length, which ``int`` takes
    and SQLite then refuses. Either used to be a 500 on every route that reads
    ids, including the two GETs a guest can reach.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if 0 < value <= _MAX_ID else None
    text = str(value).strip()
    if not text or len(text) > 19 or not text.isascii() or not text.isdigit():
        return None
    number = int(text)
    return number if 0 < number <= _MAX_ID else None


def _ids_from_csv(raw: str | None, limit: int) -> list[int]:
    """The ids of a comma-separated query argument, at most *limit* of them."""
    if not raw:
        return []
    ids = [i for i in map(_as_id, raw.split(",")) if i is not None]
    return ids[:limit]


def _id_list(data: dict[str, Any], limit: int | None = 5000) -> list[int]:
    """The ``ids`` of a request body — a list of item ids — or a 400.

    Read loosely as ``for i in data.get("ids")``, a string was taken apart one
    character at a time: ``"ids": "12"`` acted on items 1 and 2, on the routes
    that delete, restore and purge among others, and ``null`` was a 500. Ids
    sent as numeric strings are still taken, and entries that are not whole
    numbers are still passed over, as before.
    """
    values = data.get("ids")
    if values is None:
        return []
    if not isinstance(values, list):
        abort(400, description="Send the item ids as a list.")
    ids = [i for i in map(_as_id, values) if i is not None]
    return ids[:limit]


def _float_arg(name: str, default: float = 0.0) -> float:
    try:
        return float(request.args.get(name, default))
    except (TypeError, ValueError):
        return default


def _safe_under(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath([path.resolve(), root.resolve()]) == str(root.resolve())
    except (ValueError, OSError):
        return False


def _asset_file(asset: dict[str, Any]) -> Path:
    """Resolve an asset's real path, refusing anything outside its root."""
    root = Path(asset["root"]).resolve()
    path = (root / asset["rel_path"]).resolve()
    if not _safe_under(path, root) or not path.is_file():
        abort(404)
    return path


def _viewer() -> dict[str, Any]:
    """The access limits applied to every read, derived from the caller.

    Never from a request parameter — a guest cannot ask to see more by
    changing the query string. Three limits stack: which library folders they
    were assigned, how deep inside one, and what visibility they may see.
    """
    cfg = _cfg()
    user = current_user()
    libraries = cfg.libraries or ([cfg.active_root] if cfg.active_root else [])
    _, sub = auth.resolve_library(user.assigned_library, libraries)
    return {
        "viewer_id": user.id,
        "max_visibility": user.max_visibility,
        # An explicit assignment wins; the legacy scope column is the fallback.
        "scope": sub or (user.effective_scope or None),
    }


def _viewer_limits() -> dict[str, Any]:
    """Just the visibility ceiling and folder scope, for queries that take them."""
    limits = _viewer()
    return {"max_visibility": limits["max_visibility"], "scope": limits["scope"]}


def _guard(asset: dict[str, Any] | None) -> dict[str, Any]:
    """404 unless the caller is allowed to see this asset."""
    if not asset:
        abort(404)
    limits = _viewer()
    if not db.visible_to(asset, limits["max_visibility"], limits["scope"]):
        abort(404)
    # …and it must live in a library folder they were assigned.
    if asset.get("root") not in _roots():
        abort(404)
    # Trash is not a display hint. Every listing withholds trashed rows and
    # only an admin may opt back into them, so fetching one by id has to
    # answer the same way — otherwise a photo the admin removed from every
    # family-facing view is still served in full to anyone holding its id.
    if asset.get("trashed") and not current_user().is_admin:
        abort(404)
    # Explicit / adult / NSFW content must never be served in the family app
    # or to non-admin users. Only admins may view and control it.
    if asset.get("nsfw") and not current_user().is_admin:
        abort(404)
    return asset


def _dates_from_phrase(text: str) -> tuple[str, str, str]:
    """Read a date range out of the search phrase, if there is one.

    Returns the phrase with the date words removed and the range they meant.
    An explicit ``from``/``to`` in the request always wins: a date picker is
    a decision, a sentence is only a hint.
    """
    g.date_hint = None
    explicit = request.args.get("from", "") or request.args.get("to", "")
    cfg = _cfg()
    if explicit or not text or not getattr(cfg, "date_phrase_search", True):
        return text, request.args.get("from", ""), request.args.get("to", "")

    remaining, window = query.parse(
        text, southern=getattr(cfg, "southern_hemisphere", False))
    if window is None:
        return text, "", ""
    g.date_hint = {"matched": window.label, "from": window.date_from,
                   "to": window.date_to}
    return remaining, window.date_from, window.date_to


#: How far "near this photograph" reaches by default, and the furthest a
#: caller may ask for. Five kilometres is a neighbourhood; two hundred is a
#: region, past which "near" stops meaning anything and the map is the better
#: tool anyway.
NEAR_DEFAULT_KM = 5.0
NEAR_MAX_KM = 200.0


def _near_from_request() -> tuple[tuple[float, float] | None, float]:
    """Resolve ``?near=<asset id>`` to the coordinates to search around.

    The anchor is an asset, not a raw latitude and longitude, and it is put
    through the same guard as fetching that asset would be. Otherwise the
    parameter would be a way to ask "what did anyone photograph here", one
    coordinate at a time, from an account that may see almost nothing.
    """
    anchor_id = _int_arg("near")
    # Nor to a guest, who is never told where a photograph was taken: which
    # others were taken near it would say a good part of it.
    if not anchor_id or current_user().is_guest:
        return None, NEAR_DEFAULT_KM
    anchor = _guard(db.get_asset(_conn(), anchor_id))
    lat, lon = anchor.get("gps_lat"), anchor.get("gps_lon")
    if lat is None or lon is None:
        abort(400, description="That photograph has no location recorded")
    radius = _float_arg("km", NEAR_DEFAULT_KM)
    return (float(lat), float(lon)), max(0.01, min(radius, NEAR_MAX_KM))


def _filters_from_request() -> dict[str, Any]:
    kinds = [k for k in request.args.getlist("kind") if k in KIND_CODES]
    if raw := request.args.get("kinds"):
        kinds += [k for k in raw.split(",") if k in KIND_CODES]
    text, date_from, date_to = _dates_from_phrase(
        request.args.get("q", "").strip())
    near, radius_km = _near_from_request()
    return {
        "text": text,
        "kinds": kinds or None,
        "favorites": _bool_arg("favorites"),
        "min_rating": _int_arg("min_rating"),
        "date_from": date_from,
        "date_to": date_to,
        "folder": request.args.get("folder", ""),
        "tag": request.args.get("tag", ""),
        "camera": request.args.get("camera", ""),
        "duplicates_only": _bool_arg("duplicates"),
        "is_live": _bool_arg("is_live"),
        "quality": _quality_arg(),
        "occasion": _int_arg("occasion") or None,
        "album": (_int_arg("album") or None) if not current_user().is_guest else None,
        "near": near,
        "radius_km": radius_km,
        # Filtering by person needs no special access rule of its own: the
        # query still applies this viewer's visibility ceiling and folder
        # scope, so ?person=N can only ever narrow what they could see anyway.
        "person": _int_arg("person") or None,
        "include_nsfw": _bool_arg("nsfw") and current_user().is_admin,
        "include_trashed": _bool_arg("trashed") and current_user().is_admin,
        "visibility": _visibility_arg(),
        "sort": request.args.get("sort", "date_desc"),
    }


#: The flag vocabulary a browse request may filter on. Fixed rather than
#: free text: the value goes into a LIKE against stored JSON, and an
#: unbounded pattern there is both a needless scan and a needless liberty.
QUALITY_FLAGS = ("blurry", "dark", "bright", "clipped", "lowres")


def _quality_arg() -> str:
    """`?quality=blurry` narrows the view to photographs flagged that way."""
    raw = (request.args.get("quality") or "").strip().lower()
    return raw if raw in QUALITY_FLAGS else ""


def _visibility_arg() -> int | None:
    """`?visibility=hidden` narrows the view; only admins may ask for it."""
    raw = (request.args.get("visibility") or "").strip().lower()
    if not raw or raw not in VIS_VALUES:
        return None
    wanted = VIS_VALUES[raw]
    if wanted > current_user().max_visibility:
        return None
    return wanted


def _semantic_ids(text: str, roots, conn) -> tuple[list[int] | None, dict[int, float]]:
    """Rank the library by CLIP similarity to *text*, if the engine supports it."""
    engine = _engine()
    if not text or engine is None or not getattr(engine, "semantic", False):
        return None, {}
    vector = engine.encode_text(text)
    if vector is None:
        return None, {}
    ids, buffer, dim, index = db.embedding_store(conn)
    if not ids:
        return None, {}
    # The vectors are shared; which of them this viewer may be told about is
    # not. That question is a cheap id-only query, and the answer picks rows
    # out of the matrix that is already in memory.
    rows = [index[i] for i in
            db.visible_embedding_ids(conn, roots, **_viewer_limits())
            if i in index]
    ranked = ai_mod.semantic_search(vector, ids, buffer, dim, top_k=4000, rows=rows,
                                    min_score=getattr(engine, "search_floor", 0.15))
    if not ranked:
        return None, {}
    return [i for i, _ in ranked], {i: s for i, s in ranked}


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@bp.get("/")
def index() -> str:
    """Each face has its own page: the gallery, or the admin console."""
    return render_template(current_app.config.get("NINAIVU_TEMPLATE", "index.html"))


@bp.get("/sw.js")
def service_worker():
    """The offline shell, served from the root.

    A worker can only control paths at or below its own URL, so one served
    from /static/ could never see /api/thumb/. Serving the same file from the
    root is the usual way round that, and is why this route exists rather than
    letting the static handler do it.
    """
    if current_app.config.get("NINAIVU_FACE") == "admin":
        abort(404)
    path = Path(current_app.static_folder or "") / "sw.js"
    if not path.exists():
        abort(404)
    response = send_file(path, mimetype="application/javascript", max_age=0)
    # Browsers already bypass the cache for this file, but say it anyway: a
    # stale worker is the one file you cannot fix by reloading.
    response.headers["Cache-Control"] = "no-cache"
    return response


@bp.get("/manifest.webmanifest")
def manifest():
    """What iOS and Android need to treat this as an installed app.

    Served rather than shipped as a static file so the name follows whatever
    the library is actually called, and so `start_url` keeps whatever address
    the phone reached it on — a manifest with an absolute URL baked in sends
    the icon to a machine that is not there.
    """
    cfg = _cfg()
    face = current_app.config.get("NINAIVU_FACE")
    house = house_name(cfg)

    if face == "admin":
        admin_title = f"{house} Admin" if house != "Ninaivu" else "Ninaivu Admin"
        response = jsonify({
            "id": "/admin",
            "name": admin_title,
            "short_name": "Ninaivu Admin",
            "description": "Ninaivu Admin Console and Library Management.",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "display_override": ["fullscreen", "standalone", "minimal-ui"],
            "orientation": "any",
            "background_color": "#12161c",
            "theme_color": "#12161c",
            "categories": ["utilities", "productivity"],
            "icons": [
                {"src": "/static/icons/admin/icon-180.png", "sizes": "180x180",
                 "type": "image/png", "purpose": "any"},
                {"src": "/static/icons/admin/icon-192.png", "sizes": "192x192",
                 "type": "image/png", "purpose": "any"},
                {"src": "/static/icons/admin/icon-512.png", "sizes": "512x512",
                 "type": "image/png", "purpose": "any"},
                {"src": "/static/icons/admin/icon-maskable-512.png", "sizes": "512x512",
                 "type": "image/png", "purpose": "maskable"},
            ],
        })
    else:
        name = house
        response = jsonify({
            "id": "/",
            "name": name,
            "short_name": name,
            "description": "Your family's media, at home.",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "display_override": ["fullscreen", "standalone", "minimal-ui"],
            "orientation": "any",
            "background_color": "#12161c",
            "theme_color": "#12161c",
            "categories": ["photo", "lifestyle"],
            "icons": [
                {"src": "/static/icons/icon-180.png", "sizes": "180x180",
                 "type": "image/png", "purpose": "any"},
                {"src": "/static/icons/icon-192.png", "sizes": "192x192",
                 "type": "image/png", "purpose": "any"},
                {"src": "/static/icons/icon-512.png", "sizes": "512x512",
                 "type": "image/png", "purpose": "any"},
                {"src": "/static/icons/icon-maskable-512.png", "sizes": "512x512",
                 "type": "image/png", "purpose": "maskable"},
            ],
        })
    response.headers["Content-Type"] = "application/manifest+json"
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


@bp.get("/healthz")
def healthz():
    return jsonify({"ok": True, "time": time.time()})


@bp.get("/readyz")
def readyz():
    """Report whether Ninaivu's core local dependencies can serve traffic.

    ``/healthz`` deliberately remains a cheap liveness probe: it answers as
    long as the web process is running.  This separate readiness probe checks
    the SQLite schema and, when a library has been configured, verifies that
    at least one configured folder is currently reachable.  Keeping the two
    contracts separate means an unplugged media drive can take an instance out
    of a load-balancer without prompting a container restart loop.

    The response exposes only coarse states, never filesystem paths or raw
    exception messages, because both ports allow probes before sign-in.
    """
    checks = {"database": "ok", "library": "not-configured"}
    ready = True

    try:
        # Query a table that init_db always creates.  ``SELECT 1`` alone would
        # also succeed against a newly-created but uninitialised SQLite file.
        _conn().execute("SELECT 1 FROM meta LIMIT 1").fetchone()
    except Exception as exc:  # noqa: BLE001
        current_app.logger.warning("readiness database check failed: %s", exc)
        checks["database"] = "unavailable"
        ready = False

    cfg = _cfg()
    if cfg.roots or cfg.active_root:
        try:
            available = bool(cfg.libraries)
            # ``active_root`` without a matching ``roots`` entry is legacy
            # configuration, but the rest of the API still supports it.
            if not available and cfg.active_root:
                available = Path(cfg.active_root).is_dir()
            checks["library"] = "ok" if available else "unavailable"
        except Exception as exc:  # noqa: BLE001
            current_app.logger.warning("readiness library check failed: %s", exc)
            checks["library"] = "unavailable"
        if checks["library"] != "ok":
            ready = False

    response = jsonify({"ok": ready, "checks": checks, "time": time.time()})
    response.headers["Cache-Control"] = "no-store"
    return response, 200 if ready else 503


@bp.get("/ninaivu-ca.crt")
@bp.get("/cert")
def certificate_authority():
    """Ninaivu's own CA certificate, so a device can be told to trust it.

    Public by design and by nature: a CA *certificate* is the half meant to be
    handed out, and the private key that signs with it never leaves the state
    directory. Serving it here is what turns "type this address into your
    phone" into a working padlock rather than a warning people learn to
    dismiss.

    A 404 when Ninaivu is not running its own CA — behind a real certificate
    there is nothing here to install.
    """
    from ..utils import tls

    path = tls.ca_certificate_path(_cfg().state_dir)
    if not current_app.config.get("MV_SERVE_LOCAL_CA", True) or not path.is_file():
        return jsonify({
            "error": "This server is not using a Ninaivu-issued certificate.",
        }), 404

    response = send_file(path, mimetype="application/x-x509-ca-cert",
                         as_attachment=True, download_name="ninaivu-ca.crt")
    response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------------------
# Status / config
# ---------------------------------------------------------------------------

@bp.get("/api/status")
def status():
    cfg = _cfg()
    engine = _engine()
    user = current_user()
    payload: dict[str, Any] = {
        # The library path is infrastructure detail: admins only. Everyone
        # gets `has_library`, so the UI can tell "not set up" from "not shown".
        "has_library": bool(cfg.libraries or cfg.active_root),
        "root": cfg.active_root if user.is_admin else None,
        "root_label": _root_label(cfg, user),
        "roots": cfg.roots if user.is_admin else [],
        # A configured library folder that is not reachable. The count is for
        # everybody, because a gallery that is quietly short needs to say why;
        # the paths are infrastructure detail, so they are for admins, like
        # `roots` above.
        "libraries_away": len(cfg.libraries_away),
        "libraries_away_paths": cfg.libraries_away if user.is_admin else [],
        "restricted": cfg.lock_roots,
        "user": user.public(),
        "scan": _scanner().progress.snapshot() if user.is_admin else {
            "status": "idle", "running": False, "percent": 100,
        },
        "ai": engine.info if engine is not None else {"engine": "off"},
        "nsfw_filter": cfg.nsfw_filter,
        # The two ports, so each app can link to the other. Bound to 0.0.0.0 the
        # server cannot know which of its addresses this browser used, so the
        # page composes the link from its own hostname and these. Without them
        # the console link on the home page had nothing to build from and sat
        # at href="#", looking enabled and doing nothing.
        "home_port": cfg.port,
        "admin_port": cfg.admin_port,
        "scheme": current_app.config.get("MV_SCHEME") or ("https" if cfg.port == 443 else "http"),
        "hostnames": current_app.config.get("MV_HOSTNAMES", {}),
        "thumb_sizes": list(cfg.thumb_sizes),
        "capabilities": {
            "ffmpeg": bool(media.FFMPEG),
            "heif": media.HEIF_OK,
            "opencv": media.cv2 is not None,
            "watch": cfg.watch,
        },
    }
    # The counts are the one part of this that grows with the library, and
    # the gallery does not need them to draw a photograph. `?stats=0` leaves
    # them out so the page can render and fill the sidebar afterwards; see
    # /api/status/stats.
    if (cfg.libraries or cfg.active_root) and _bool_arg("stats", True):
        try:
            payload["stats"] = db.library_stats(
                _conn(), _roots(), **_viewer()
            )
        except Exception as exc:  # noqa: BLE001
            current_app.logger.warning("library_stats failed: %s", exc)
            payload["stats"] = None
    return jsonify(payload)


@bp.get("/api/status/stats")
def status_stats():
    """The sidebar's counts, on their own.

    Split out because they are what the rest of /api/status is not: work
    proportional to the size of the library. The gallery asks for everything
    else first, draws, and fills these in when they arrive — so the number of
    photographs somebody owns no longer decides how long they look at an
    empty page.
    """
    cfg = _cfg()
    if not (cfg.libraries or cfg.active_root):
        return jsonify({"stats": None})
    try:
        stats = db.library_stats(_conn(), _roots(), **_viewer())
    except Exception as exc:  # noqa: BLE001
        current_app.logger.warning("library_stats failed: %s", exc)
        stats = None
    response = jsonify({"stats": stats})
    response.headers["Cache-Control"] = "no-store"
    return response


def _root_label(cfg: Config, user) -> str:
    """What to show a non-admin in place of an absolute path."""
    libraries = cfg.libraries or ([cfg.active_root] if cfg.active_root else [])
    if user.is_admin:
        if len(libraries) > 1:
            return f"{len(libraries)} library folders"
        return cfg.active_root or ""
    if not libraries:
        return ""
    if assigned := user.assigned_library:
        return Path(assigned).name or assigned
    if user.effective_scope:
        return user.effective_scope
    if len(libraries) > 1:
        return "Your library"
    return Path(libraries[0]).name or "Library"


#: How often a running scan's counters are looked at for the progress stream.
PROGRESS_TICK = 2.0


def _moved(now: dict[str, Any], before: dict[str, Any]) -> bool:
    """Has anything worth redrawing changed? The clock alone is not."""
    ignore = {"elapsed", "phase"}
    return any(now.get(k) != before.get(k) for k in now if k not in ignore)


@bp.get("/api/status/scan")
def scan_progress():
    """Only the scan's counters, for a page that has to ask for them.

    The gallery cannot use the event stream (it is a console route), and it
    used to ask /api/status every two seconds instead — which counts the whole
    library each time, against the same database the scan is writing to.
    This is a copy of a few integers under a lock.

    Under /api/status, not /api/scan: it is a part of the status both ports
    already serve, and /api/scan* is library management, which the family
    port must not route at all.
    """
    if not current_user().is_admin:
        return jsonify({"status": "idle", "running": False, "percent": 100})
    response = jsonify(_scanner().progress.snapshot())
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.get("/api/status/activity")
def activity():
    """Everything running in the background right now, not just the scan.

    The strip showed the library scan alone, so a consolidation, a cloud
    upload, a straightening pass or a storage check held the disk with nothing
    anywhere to say so — "why is this machine busy?" had no answer short of
    opening every console page in turn.

    The scan snapshot rides along so the gallery can ask one question rather
    than two: it still drives the completion refresh and the added/removed
    toast, which the job list deliberately knows nothing about.

    Admin-only, like the scan it contains: which drives are being read and
    what is going to which cloud account is not the family's business.
    """
    if not current_user().is_admin:
        return jsonify({"jobs": [], "running": False,
                        "scan": {"status": "idle", "running": False,
                                 "percent": 100}})
    scan = _scanner().progress.snapshot()
    jobs = activity_kit.running(current_app.config["MV_SERVICES"],
                                current_app.config)
    response = jsonify({
        "jobs": jobs,
        "running": bool(jobs),
        # What the machine is spending, gathered across every job, so the
        # strip can answer the disk-or-processor question in a word.
        "uses": sorted({use for job in jobs for use in job["uses"]}),
        "scan": scan,
    })
    response.headers["Cache-Control"] = "no-store"
    return response


@admin_only.get("/api/events")
@require_admin
def events():
    """Server-sent scan progress — no polling loop in the browser."""
    scanner = _scanner()
    stream: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=32)

    def listener(payload: dict[str, Any]) -> None:
        try:
            stream.put_nowait(payload)
        except queue.Full:
            pass

    scanner.add_listener(listener)

    @stream_with_context
    def generate():
        try:
            last = scanner.progress.snapshot()
            yield f"data: {json.dumps(last)}\n\n"
            while True:
                # The scanner only announces itself between batches — every
                # 64 files, every 16 photographs tagged — and on a slow drive
                # or a CPU-only model one batch is minutes, which on screen is
                # a bar that has stopped. So while a scan runs, this looks at
                # the counters itself every couple of seconds and sends them
                # if they moved. It reads a snapshot and nothing else: the
                # scan thread does no extra work, however many consoles are
                # open, and an idle library still costs one line in 20s.
                wait = PROGRESS_TICK if last.get("running") else 20
                try:
                    payload = stream.get(timeout=wait)
                except queue.Empty:
                    now = scanner.progress.snapshot()
                    if now.get("running") or last.get("running"):
                        if _moved(now, last):
                            last = now
                            yield f"data: {json.dumps(now)}\n\n"
                            continue
                    last = now
                    yield ": keep-alive\n\n"
                    continue
                last = payload
                yield f"data: {json.dumps(payload)}\n\n"
        finally:
            scanner.remove_listener(listener)

    # Deliberately no "Connection: keep-alive". It is a hop-by-hop header,
    # which PEP 3333 forbids a WSGI application from setting — the connection
    # belongs to the server, not to the application. Werkzeug let it through;
    # waitress asserts on it, which killed every event stream at the first
    # byte. Nothing is lost: HTTP/1.1 keeps the connection alive by default,
    # and the server decides either way.
    return Response(generate(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


# ---------------------------------------------------------------------------
# The fast path: layout segments
# ---------------------------------------------------------------------------

#: Everything a segment item is built from, and nothing else: the layout needs
#: shape, kind and flags, never tags, EXIF or paths.
_SEGMENT_COLUMNS = ("id", "width", "height", "kind", "thumb", "nsfw", "dup_group",
                    "gps_lat", "visibility", "date_key", "duration", "indexed_at",
                    "rotation", "color")


def _swatch(color: str | None) -> str:
    """"#a3b4c5" as "abc": a tile's colour until its picture arrives.

    A placeholder needs no more than twelve bits, and at three characters a
    colour costs a library of 200,000 items about a megabyte of layout less
    than the full six would.
    """
    if not color or len(color) != 7 or color[0] != "#":
        return ""
    return color[1] + color[3] + color[5]


@bp.get("/api/segments")
def segments():
    """Compact layout payload: everything the grid needs, nothing it doesn't.

    Each item is ``[id, aspect*100, kind, flags, duration, thumb version]``.
    That is enough to
    compute a pixel-exact justified layout, and thumbnail URLs are derived
    from the id, so drawing a page needs no further round trip.

    A large library arrives in pieces: ``offset`` picks up where the last
    piece ended and ``next_offset`` says where the next one starts, or is
    null when there is nothing more. The grid appends each piece as the
    scroll nears the end of what it holds. Two orders cannot be continued —
    a random order is drawn afresh by every query, and an AI search is one
    ranked list already — so those always come back as a single piece.
    """
    cfg = _cfg()
    roots = _roots()
    conn = _conn()
    filters = _filters_from_request()
    limit = max(1, min(_int_arg("limit", 100_000), 200_000))
    offset = max(0, _int_arg("offset"))

    ranked_ids, scores = _semantic_ids(filters["text"], roots, conn)
    order_map: dict[int, int] = {}
    if ranked_ids is not None:
        filters["text"] = ""
        filters["ids"] = ranked_ids
        order_map = {aid: i for i, aid in enumerate(ranked_ids)}
    pageable = not order_map and filters.get("sort") != "random"
    if not pageable:
        # One piece is all there will be, so it is as large as it ever was,
        # whatever piece size the grid asked for.
        offset = 0
        limit = max(limit, 100_000)

    rows, total = db.query_assets(
        conn, roots, limit=limit, offset=offset, columns=_SEGMENT_COLUMNS,
        **_viewer(), **filters
    )
    reached = offset + len(rows)

    if order_map:
        rows.sort(key=lambda r: order_map.get(r["id"], 1 << 30))

    grouped: dict[str, list[list[Any]]] = {}
    order: list[str] = []
    # Once for the page, not once a row: asked through the app context for
    # each of 25,000 rows, the same token cost a sixth of the request.
    recipe = _recipe()
    thumb_version = media.thumb_version
    for row in rows:
        width, height = row.get("width") or 0, row.get("height") or 0
        aspect = round((width / height) * 100) if width and height else 100
        aspect = max(30, min(400, aspect))
        flags = (
            (1 if row["favorite"] else 0)
            | (2 if row.get("thumb") else 0)
            | (4 if row["nsfw"] else 0)
            | (8 if row.get("dup_group") else 0)
            | (16 if row.get("gps_lat") is not None else 0)
            | (32 if (row.get("rating") or 0) > 0 else 0)
            | (64 if row.get("visibility") == 0 else 0)
            | (128 if row.get("visibility") == VIS_HIDDEN else 0)
        )
        key = "match" if order_map else (row.get("date_key") or "unknown")
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append([
            row["id"], aspect, KIND_CODES.get(row["kind"], 0), flags,
            int(row.get("duration") or 0), thumb_version(row, recipe),
            _swatch(row.get("color")) if row.get("thumb") else "",
        ])

    return jsonify({
        "total": total,
        "offset": offset,
        "returned": len(rows),
        "next_offset": reached if pageable and rows and reached < total else None,
        "truncated": total > reached,
        "semantic": bool(order_map),
        "segments": [{"key": k, "items": grouped[k]} for k in order],
        "thumb_sizes": list(cfg.thumb_sizes),
    })


@bp.get("/api/assets")
def assets():
    """Full metadata, paginated — used for detail views and exports."""
    cfg = _cfg()
    roots = _roots()
    conn = _conn()
    filters = _filters_from_request()
    limit = max(1, min(_int_arg("limit", cfg.page_size), cfg.max_page_size))
    offset = max(0, _int_arg("offset"))

    if request.args.get("ids"):
        # Asked for particular items: only those, and none when none of the
        # ids is one (not the first page of everything).
        wanted = _ids_from_csv(request.args.get("ids"), 1000)
        rows, total = db.query_assets(
            conn, roots, ids=wanted, include_nsfw=current_user().is_admin,
            include_trashed=current_user().is_admin,
            limit=len(wanted) or 1, offset=0, **_viewer(),
        )
        rank = {v: i for i, v in enumerate(wanted)}
        rows.sort(key=lambda r: rank.get(r["id"], 0))
        return jsonify({"items": [_public(r) for r in rows], "total": total})

    ranked_ids, scores = _semantic_ids(filters["text"], roots, conn)
    if ranked_ids is not None:
        # The other filters — kind, folder, dates, favourites — narrow the
        # ranking *before* it is paged, the way /api/segments does. Paging
        # first handed out a page of the whole ranking with the filters applied
        # to that page alone: "beach" among videos came back as a few items a
        # page and a total counting every photograph that matched.
        filters["text"] = ""
        filters["ids"] = ranked_ids
        matching, _ = db.query_assets(
            conn, roots, limit=len(ranked_ids), offset=0, columns=("id",),
            **_viewer(), **filters
        )
        rank = {aid: i for i, aid in enumerate(ranked_ids)}
        kept = sorted((r["id"] for r in matching), key=rank.__getitem__)
        filters["ids"] = kept[offset:offset + limit]
        rows, _ = db.query_assets(
            conn, roots, limit=limit, offset=0, **_viewer(), **filters
        )
        rows.sort(key=lambda r: rank[r["id"]])
        total = len(kept)
    else:
        rows, total = db.query_assets(
            conn, roots, limit=limit, offset=offset, **_viewer(), **filters
        )

    return jsonify({
        "items": [_public(r, scores) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "semantic": ranked_ids is not None,
        # What a date phrase in the query was read as, so the interface can
        # show it. A search that silently narrowed to three days looks
        # exactly like a library that lost everything else.
        "dates": g.get("date_hint"),
    })


#: Every way of writing "the library root". A rule may be set on any folder
#: inside the library and on none of these — the whole-library control was the
#: one place where a slip cost everything, and it is refused rather than
#: guarded. Spelling it out matters: "" and "/" and "." all mean the root, and
#: a check that caught only the empty string would be one URL away from being
#: no check at all.
_ROOT_SPELLINGS = {"", "/", "\\", ".", "./", ".\\"}


def _is_root(folder: Any) -> bool:
    if folder is None:
        return True
    text = str(folder).strip()
    return text in _ROOT_SPELLINGS


def _recipe() -> str:
    """This server's thumbnail settings, as a token. Cached: it cannot change
    without a restart, and it is asked for once per item in a listing."""
    cfg = _cfg()
    cached = current_app.config.get("NINAIVU_THUMB_RECIPE")
    if not cached:
        cached = media.thumb_recipe(cfg.thumb_sizes, cfg.thumb_format,
                                    cfg.thumb_quality)
        current_app.config["NINAIVU_THUMB_RECIPE"] = cached
    return cached


def _thumb_version(row: dict[str, Any]) -> str:
    """A token that changes exactly when an asset's thumbnails are rewritten.

    The reasoning lives with the thumbnails themselves, in
    :func:`media.thumb_version`.
    """
    return media.thumb_version(row, _recipe())


def _public(row: dict[str, Any], scores: dict[int, float] | None = None) -> dict[str, Any]:
    """Shape a DB row for the client — no absolute paths ever leave the server.

    Guests get a deliberately thinner payload: no EXIF, no camera, no GPS, no
    download link, since they are not entitled to that detail.
    """
    user = current_user()
    out = {
        "id": row["id"],
        "name": row["filename"],
        "folder": row["folder"],
        "ext": row["ext"],
        "kind": row["kind"],
        "size": row["size"],
        "size_h": media.human_size(row["size"]),
        "date": row["date_key"],
        "date_source": row["date_source"],
        "captured_at": row["captured_at"],
        "width": row["width"],
        "height": row["height"],
        "duration": row["duration"],
        "tags": row["tags"],
        "caption": row["caption"],
        "favorite": row["favorite"],
        "rating": row["rating"],
        "nsfw": row["nsfw"],
        "blurhash": row["blurhash"],
        "color": row["color"],
        "has_thumb": bool(row["thumb"]),
        "duplicate": bool(row.get("dup_group")),
        # Plain words — blurry · dark · bright · clipped · lowres. The raw
        # measurements stay server-side; a viewer needs the verdict, not the
        # Laplacian variance behind it.
        "quality": row.get("quality") or [],
        "playable": f".{row['ext']}" in BROWSER_NATIVE,
        # Where to fetch something the browser can actually decode. For an
        # ordinary JPEG this is the file itself; for HEIC or raw it is the
        # converted viewing copy.
        "view": (f"/api/file/{row['id']}" if f".{row['ext']}" in BROWSER_NATIVE
                 else f"/api/preview/{row['id']}"),
        # A video the browser cannot open can still be watched through a
        # converted copy. The viewer asks for one rather than giving up.
        "needs_proxy": proxies.needs_proxy(row.get("ext") or "",
                                           row.get("kind") or ""),
        "src": f"/api/file/{row['id']}",
        "visibility": VIS_NAMES.get(int(row.get("visibility", 1)), "family"),
        # Why it has that visibility: "default", "folder" (an admin's rule),
        # "hidden" (it was hidden on disk) or "item" (set on this one file).
        # Without it an admin sees a hidden photo and cannot tell whether they
        # hid it or the filesystem did.
        "visibility_source": row.get("vis_source") or "default",
        # The clockwise turn that puts this the right way up. Thumbnails are
        # written with it already applied; the *original* served by /api/file
        # is untouched, so the browser has to be told. Zero for the ordinary
        # case of a photograph whose own EXIF tag the browser already honours.
        "rotation": int(row.get("rotation") or 0),
        # Who decided: exif · faces · ai · manual · none. An admin looking at a
        # photograph that came out sideways can tell a bad guess from a bad tag
        # and so correct the right thing.
        "rotation_source": row.get("rot_source") or "none",
        # A version for the thumbnail URL.
        #
        # Thumbnails are served `immutable` with a year's lifetime, which is
        # right for a derived file addressed by asset id — but `immutable` is a
        # promise that the bytes at that URL never change, and rotating breaks
        # it. Rewriting the file changed nothing any browser would look at
        # again: the rotation saved, the file on disk was correct, and the grid
        # went on drawing yesterday's bytes. So the URL changes instead.
        "thumb_v": _thumb_version(row),
        "is_live": bool(row.get("is_live", 0)),
        # `is_live` alone isn't enough to promise a companion clip: an
        # iPhone-style pairing sets `live_video_path` too, but a Google
        # Motion Photo (video muxed inside the JPEG/HEIC itself, detected by
        # `media.detect_motion_photo()`) only sets `is_live` -- nothing
        # extracts the embedded clip, so `live_video_path` stays empty and
        # `/api/live-video/<id>` would 404. Gating on both means the viewer's
        # `item.is_live && item.live_src` check (static/js/viewer.js) simply
        # doesn't show the LIVE badge for one of these, rather than showing
        # it and then failing silently on tap.
        "live_src": (f"/api/live-video/{row['id']}"
                     if row.get("is_live") and row.get("live_video_path")
                     else None),
        "city": row.get("city"),
        "country": row.get("country"),
    }

    if user.can_download:
        out["download"] = f"/api/download/{row['id']}"
        out.update({
            "camera": row["camera"],
            "lens": row["lens"],
            "iso": row["iso"],
            "f_number": row["f_number"],
            "exposure": row["exposure"],
            "focal_length": row["focal_length"],
            "gps": ([row["gps_lat"], row["gps_lon"]]
                    if row["gps_lat"] is not None else None),
        })
    if scores and row["id"] in scores:
        out["score"] = round(scores[row["id"]], 4)
    return out


@bp.get("/api/asset/<int:asset_id>")
def asset_detail(asset_id: int):
    row = _guard(db.get_asset(_conn(), asset_id, current_user().id))
    return jsonify(_public(row))


# ---------------------------------------------------------------------------
# Binary endpoints
# ---------------------------------------------------------------------------

@bp.get("/api/thumb/<int:asset_id>")
def thumb(asset_id: int):
    cfg = _cfg()
    row = _guard(db.get_asset(_conn(), asset_id))
    if not row.get("thumb"):
        abort(404)
    size = _int_arg("s", cfg.thumb_sizes[0])
    size = min(cfg.thumb_sizes, key=lambda s: abs(s - size))
    path = cfg.thumbs_dir / media.thumb_file(row["thumb"], size, cfg.thumb_format)
    if not path.is_file():
        abort(404)

    # Do not leave generated-image MIME detection to the host registry.
    # A clean Windows install commonly has no .webp association, which makes
    # Flask fall back to application/octet-stream; ``nosniff`` then prevents
    # browsers from displaying a perfectly valid thumbnail.
    thumb_mime = "image/webp" if cfg.thumb_format.upper() == "WEBP" \
        else "image/jpeg"
    # The ETag is passed *in*, not assigned afterwards.
    #
    # `conditional=True` compares the request's If-None-Match against the tag
    # send_file itself computed, and it does that inside this call. Overwriting
    # the header afterwards handed the browser one tag while the comparison
    # kept using another, so every revalidation missed and re-sent the whole
    # file — a reload of a full grid was tens of megabytes of thumbnails that
    # the browser already had, instead of a handful of 304s.
    response = send_file(
        path, conditional=True, mimetype=thumb_mime, max_age=31536000,
        etag=f'{int(row["mtime"])}-{size}')
    # Derivatives are content-addressed by (root, path) and rewritten in place
    # on change, so the long max-age is safe. "private" because what a viewer
    # may fetch depends on who they are signed in as.
    response.headers["Cache-Control"] = "private, max-age=31536000, immutable"
    return response


#: Picture formats with nowhere to keep a location, whose originals may go to
#: anybody who may see them. Every other picture carries EXIF or XMP, which is
#: where a phone writes the place it was taken — GPS to a few metres, usually
#: someone's home. GIF is here too because a re-encoded copy would stop it
#: moving.
NO_LOCATION_EXTS = frozenset({"gif", "bmp"})


def _location_may_ride_along(row: dict[str, Any]) -> bool:
    """Whether this picture's original may carry where it was taken."""
    ext = str(row.get("ext") or "").lower().lstrip(".")
    return (row.get("kind") or "") == "picture" and ext not in NO_LOCATION_EXTS


def _viewing_copy(row: dict[str, Any], path: Path, max_age: int = 86400):
    """The picture re-encoded for viewing: upright, at most 2560 px, and with
    no metadata at all — so nothing about where or with what it was taken."""
    asset_id = int(row["id"])
    store = _still_store()
    ready = store.ready(asset_id, path) or store.build(
        asset_id, path, orient=media._open_oriented)      # noqa: SLF001
    if ready is None:
        abort(415, description="This photograph could not be converted for "
                               "the browser.")
    # Same rule as the thumbnails above: the tag goes in, so that a
    # revalidation can actually match it and come back 304. The file's time is
    # in it so a photograph replaced in place is not answered with a 304 for
    # the copy of the one before.
    response = send_file(ready, conditional=True, mimetype="image/jpeg",
                         max_age=max_age,
                         etag=f"p{asset_id}-v{stills.RENDITION_VERSION}"
                              f"-{int(row.get('mtime') or 0)}")
    response.headers["Cache-Control"] = f"private, max-age={max_age}"
    return response


@bp.get("/api/file/<int:asset_id>")
def original(asset_id: int):
    row = _guard(db.get_asset(_conn(), asset_id))
    path = _asset_file(row)
    if current_user().is_guest and _location_may_ride_along(row):
        # A guest is given the gallery's view of a photograph, never its EXIF:
        # the payload already leaves out the GPS and the camera, and the
        # original's bytes had both in them for anyone who saved the image.
        # The address is the same one family members use, so the gallery
        # needs no second code path — which is why the answer varies by who
        # is asking, and says so.
        response = _viewing_copy(row, path, max_age=3600)
        response.headers["Vary"] = "Cookie"
        return response
    mime, _ = mimetypes.guess_type(path.name)
    # A type this route will render, or a download. A library indexed before
    # uploads were filtered can still hold an .html or an .svg, and the answer
    # for those is a file to save rather than a page to run: nosniff plus a
    # neutral type plus an attachment disposition, so the browser cannot be
    # talked into treating it as a document on Ninaivu's own origin.
    inline = mime in INLINE_TYPES
    response = send_file(
        path,
        conditional=True,                 # HTTP Range, so video seeking works
        mimetype=mime if inline else "application/octet-stream",
        max_age=3600,
        as_attachment=not inline,
        download_name=None if inline else row["filename"],
    )
    response.headers["Accept-Ranges"] = "bytes"
    response.headers["Cache-Control"] = "private, max-age=3600"
    response.headers["Vary"] = "Cookie"
    return response


def _still_store():
    """One store per process, so two viewers do not convert the same file twice."""
    store = current_app.config.get("MV_STILLS")
    if store is None:
        cfg = _cfg()
        store = stills.StillStore(cfg.state_dir,
                                  getattr(cfg, "rendition_cache_mb",
                                          stills.DEFAULT_CACHE_MB))
        current_app.config["MV_STILLS"] = store
    return store


@bp.get("/api/preview/<int:asset_id>")
def still_preview(asset_id: int):
    """A viewable copy of a photograph the browser cannot decode.

    HEIC from a phone, raw from a camera, the odd TIFF. Ninaivu can already
    read all of them — that is where the thumbnails come from — so the only
    thing missing was a full-size copy in a format a browser accepts.

    Deliberately at the same permission level as the thumbnail rather than the
    download: this is a resized, re-encoded, metadata-free viewing copy of a
    picture the person is already being shown in the grid, and treating it as
    a download would mean a guest could see a photograph in the gallery and
    not open it. The original is still behind ``/api/file``, for family
    members and admins; a guest is given this same copy there too.
    """
    row = _guard(db.get_asset(_conn(), asset_id))
    if (row.get("kind") or "") != "picture":
        abort(404)
    path = _asset_file(row)
    if not stills.needs_rendition(row["ext"], "picture"):
        # Already something a browser reads; no second copy needed.
        return original(asset_id)
    return _viewing_copy(row, path)


@bp.get("/api/download/<int:asset_id>")
@require_family
def download(asset_id: int):
    """Guests may look, but not take copies away."""
    row = _guard(db.get_asset(_conn(), asset_id))
    path = _asset_file(row)
    return send_file(path, as_attachment=True, download_name=row["filename"])


#: How many files one download may contain. Not a technical limit — a guard
#: against somebody selecting a hundred thousand photographs and waiting an
#: hour for a ZIP their filesystem may refuse to open.
MAX_ZIP_FILES = 2000

#: How many files one bulk turn may rewrite. The same ceiling as a bulk
#: download, and for a stronger reason: this one writes to the library.
MAX_ROTATE = 2000


def _unique_zip_name(taken: set[str], filename: str) -> str:
    """A name for this entry that nothing else in the archive is using.

    Two photographs from different folders very often share a filename —
    IMG_0001.jpg is not distinctive — and a ZIP with duplicates extracts to
    one file silently overwriting another.
    """
    candidate = Path(filename).name or "file"
    if candidate not in taken:
        taken.add(candidate)
        return candidate
    stem, suffix = Path(candidate).stem, Path(candidate).suffix
    for counter in range(2, 100000):
        attempt = f"{stem} ({counter}){suffix}"
        if attempt not in taken:
            taken.add(attempt)
            return attempt
    taken.add(candidate)
    return candidate


def _attachment(name: str, fallback: str) -> str:
    """A ``Content-Disposition`` value that names *name*, whatever it is.

    Header values go out as Latin-1, so a photograph called நாள்.jpg written
    straight into the header made the server fail the whole response with a
    500. The name travels in ``filename*`` (RFC 5987), which every current
    browser prefers; ``filename`` carries an ASCII stand-in for anything
    older, and *fallback* when nothing of the name survives in ASCII.
    """
    import unicodedata                                   # noqa: PLC0415
    from urllib.parse import quote                       # noqa: PLC0415
    from werkzeug.http import dump_options_header        # noqa: PLC0415

    # A filename may legally hold a newline on some systems; a header may not.
    name = "".join(" " if unicodedata.category(c) == "Cc" else c for c in name)
    try:
        name.encode("ascii")
    except UnicodeEncodeError:
        simple = (unicodedata.normalize("NFKD", name)
                  .encode("ascii", "ignore").decode("ascii"))
        if not simple.rsplit(".", 1)[0].strip(" ._-"):
            simple = fallback
        return dump_options_header("attachment", {
            "filename": simple,
            "filename*": "UTF-8''" + quote(name, safe="!#$&+^`|~"),
        })
    return dump_options_header("attachment", {"filename": name})


@bp.get("/api/download/zip")
@require_family
def download_zip():
    """Several photographs as one download, streamed rather than assembled.

    The archive is written straight to the response as it is read, so a four
    gigabyte selection needs no temporary file and no four gigabytes of memory,
    and the browser starts saving immediately instead of waiting on a spinner.

    Media is stored, not deflated. JPEG, HEIC, MP4 and friends are already
    compressed; running them through zlib costs real CPU to make the file
    fractionally larger.

    Which files may be included is decided by the same query the gallery uses,
    so a guest asking for a hidden photograph's id simply gets an archive that
    does not contain it. Nothing here reads a path from the request.
    """
    wanted = _ids_from_csv(request.args.get("ids"), MAX_ZIP_FILES)
    if not wanted:
        abort(400, description="Choose some photographs first.")

    conn = _conn()
    rows, _ = db.query_assets(conn, _roots(), ids=wanted,
                              limit=len(wanted), offset=0, **_viewer())
    if not rows:
        abort(404, description="None of those are yours to download.")

    # Resolve every path before streaming starts: once the first byte is out
    # the status code is already sent, and a failure then can only truncate
    # the file rather than report anything.
    entries: list[tuple[Path, str]] = []
    taken: set[str] = set()
    # Files that could not be included. Skipping them keeps the rest of the
    # download worth having; naming them, inside the ZIP, is what stops someone
    # believing they have every photo they picked.
    missing: list[str] = []
    for row in rows:
        if db.hidden_from(row, current_user().max_visibility):
            continue
        try:
            path = _asset_file(row)
        except Exception:  # noqa: BLE001 - a missing file skips, never 500s
            missing.append(row["filename"])
            continue
        entries.append((path, _unique_zip_name(taken, row["filename"])))

    if not entries:
        abort(404, description="Nothing in that selection could be read.")

    stamp = time.strftime("%Y-%m-%d")
    name = (f"ninaivu-{stamp}.zip" if len(entries) > 1
            else f"{Path(entries[0][1]).stem}.zip")

    def generate():
        buffer = _ZipStream()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED,
                             allowZip64=True) as archive:
            for path, arcname in entries:
                try:
                    with archive.open(arcname, "w") as target, \
                            open(path, "rb") as source:
                        while chunk := source.read(1 << 20):
                            target.write(chunk)
                            if (out := buffer.take()):
                                yield out
                except (OSError, ValueError):
                    # A file that vanished mid-download is skipped; the rest
                    # of the archive is still worth having.
                    missing.append(arcname)
                    continue
                if (out := buffer.take()):
                    yield out
            if missing:
                note = ("These files were selected but could not be read, so they "
                        "are not in this download:\n\n" + "\n".join(missing) + "\n")
                archive.writestr(_unique_zip_name(taken, "NOT INCLUDED.txt"),
                                 note.encode("utf-8"))
                import logging
                logging.getLogger(__name__).warning(
                    "download left out %d unreadable file(s): %s",
                    len(missing), ", ".join(missing[:10]))
        if (out := buffer.take()):
            yield out

    response = Response(stream_with_context(generate()), mimetype="application/zip")
    response.headers["Content-Disposition"] = _attachment(
        name, fallback=f"ninaivu-{stamp}.zip")
    # Length is unknowable while streaming, and guessing it truncates the file.
    response.headers["X-Accel-Buffering"] = "no"
    return response


class _ZipStream:
    """A file-like sink that hands whatever ZipFile wrote to the response.

    ZipFile needs something with write() and tell(); it only needs seek() when
    it wants to rewrite a header, which ZIP64 streaming avoids. Buffering here
    rather than in memory is the whole point: the archive is produced and sent
    in chunks instead of built and then delivered.
    """

    def __init__(self) -> None:
        self._parts: list[bytes] = []
        self._position = 0

    def write(self, data: bytes) -> int:
        self._parts.append(bytes(data))
        self._position += len(data)
        return len(data)

    def tell(self) -> int:
        return self._position

    def flush(self) -> None:
        return None

    def take(self) -> bytes:
        if not self._parts:
            return b""
        out = b"".join(self._parts)
        self._parts.clear()
        return out


def _proxy_store():
    """One store per process, so conversions are not started twice."""
    store = current_app.config.get("MV_PROXIES")
    if store is None:
        cfg = _cfg()
        store = proxies.ProxyStore(cfg.state_dir,
                                   getattr(cfg, "proxy_cache_mb",
                                           proxies.DEFAULT_CACHE_MB))
        current_app.config["MV_PROXIES"] = store
    return store


@bp.get("/api/proxy/<int:asset_id>")
@require_family
def video_proxy(asset_id: int):
    """A playable copy of a video the browser cannot open.

    One endpoint does the whole job. Ready: the file, with Range support so
    seeking works. Not ready: 202 and a progress figure, having started the
    conversion. The viewer polls the same address until it stops getting 202.

    Downloading is family-and-above, and so is this — it is the same footage
    in a different container, and pretending otherwise would put a copy of a
    hidden video behind a slightly different URL.
    """
    conn = _conn()
    asset = _guard(db.get_asset(conn, asset_id, current_user().id))
    if (asset.get("kind") or "") != "video":
        abort(404)
    if not current_user().can_download:
        abort(403, description="Guests cannot play converted videos.")

    store = _proxy_store()
    source = _asset_file(asset)
    ready = store.ready(asset_id, source)
    if ready is not None:
        response = send_file(ready, conditional=True, mimetype="video/mp4",
                             max_age=3600)
        response.headers["Accept-Ranges"] = "bytes"
        return response

    source = Path(asset["root"]) / asset["rel_path"]
    state = store.start(asset_id, source, asset.get("duration"))
    if state.state == "ready" and store.ready(asset_id, source):
        return video_proxy(asset_id)
    status = 503 if state.state in ("failed", "unavailable") else 202
    return jsonify(state.payload()), status


@bp.get("/api/live-video/<int:asset_id>")
def live_video(asset_id: int):
    """Stream companion live photo video clip."""
    row = _guard(db.get_asset(_conn(), asset_id))
    live_path = row.get("live_video_path")
    if not live_path:
        abort(404, description="No companion live video for this item")
    root = Path(row["root"]).resolve()
    full_path = (root / live_path).resolve()
    # The same two rules as /api/file: never outside the library, and never a
    # type the browser would treat as a page on Ninaivu's own origin.
    if not _safe_under(full_path, root) or not full_path.is_file():
        abort(404, description="Companion video file not found")
    mime, _ = mimetypes.guess_type(full_path.name)
    if mime not in INLINE_TYPES or not mime.startswith("video/"):
        abort(404, description="Companion video file not found")
    response = send_file(full_path, conditional=True, mimetype=mime, max_age=3600)
    response.headers["Accept-Ranges"] = "bytes"
    response.headers["Cache-Control"] = "private, max-age=3600"
    return response



# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------

@bp.post("/api/asset/<int:asset_id>")
@require_family
def update_asset(asset_id: int):
    """Favourites, ratings and tags. Visibility is a separate, admin-only route."""
    data = json_object()
    conn = _conn()
    user = current_user()
    # Called for the check, not the row: _guard aborts unless this caller
    # may see this asset. The binding is gone, the guard is not.
    _guard(db.get_asset(conn, asset_id, user.id))

    # Refused before anything is saved: answering 403 with the favourite
    # already written told the page nothing had changed when something had.
    if not user.is_admin and any(k in data for k in ("nsfw", "trashed", "tags", "caption")):
        return jsonify({
            "error": "Only an admin can change tags or visibility.",
            "status": 403,
        }), 403

    # Per-viewer state: my favourites are not your favourites.
    mine: dict[str, Any] = {}
    if "favorite" in data:
        mine["favorite"] = int(bool(data["favorite"]))
    if "rating" in data:
        mine["rating"] = _rating(data["rating"])
    if mine:
        db.set_user_asset(conn, user.id, asset_id, **mine)

    # Shared metadata: admins only, so one member cannot retag the library.
    shared: dict[str, Any] = {}
    if user.is_admin:
        if "nsfw" in data:
            shared["nsfw"] = int(bool(data["nsfw"]))
        if "trashed" in data:
            shared["trashed"] = int(bool(data["trashed"]))
        if "tags" in data and isinstance(data["tags"], list):
            shared["tags"] = [
                str(t).strip().lower() for t in data["tags"] if str(t).strip()
            ]
        if "caption" in data:
            shared["caption"] = str(data["caption"])[:500]
        if shared:
            db.update_asset(conn, asset_id, **shared)

    return jsonify(_public(db.get_asset(conn, asset_id, user.id)))


@bp.post("/api/assets/bulk")
@require_family
def bulk_update():
    data = json_object()
    ids = _id_list(data)
    if not ids:
        return jsonify({"updated": 0})

    conn = _conn()
    user = current_user()
    # Silently drop anything this viewer isn't allowed to touch.
    allowed, _ = db.query_assets(
        conn, _roots(), ids=ids, include_nsfw=user.is_admin,
        include_trashed=user.is_admin, limit=len(ids), offset=0, **_viewer(),
    )
    allowed_ids = [row["id"] for row in allowed]
    if not allowed_ids:
        return jsonify({"updated": 0})

    mine: dict[str, Any] = {}
    if "favorite" in data:
        mine["favorite"] = int(bool(data["favorite"]))
    if "rating" in data:
        mine["rating"] = _rating(data["rating"])
    if mine:
        db.bulk_set_user_asset(conn, user.id, allowed_ids, **mine)

    if user.is_admin:
        shared = {}
        for key in ("nsfw", "trashed"):
            if key in data:
                shared[key] = int(bool(data[key]))
        if "nsfw" in shared:
            # Marked by hand, so neither a rescan nor the next tagging pass
            # may put it back to what the model thought (db._KEEP_DESCRIBED).
            shared["nsfw_source"] = "manual"
        if shared:
            assignments = ", ".join(f"{k}=?" for k in shared)
            with db._write_lock:
                conn.execute(
                    f"UPDATE assets SET {assignments} WHERE id IN "
                    f"({','.join('?' * len(allowed_ids))})",
                    (*shared.values(), *allowed_ids),
                )
                conn.commit()

    return jsonify({"updated": len(allowed_ids), "skipped": len(ids) - len(allowed_ids)})


# ---------------------------------------------------------------------------
# Which way up — signed-in household members
# ---------------------------------------------------------------------------

# Registered on `bp` alongside the editor it serves, and family-level for the
# same reason: whoever may correct a photograph may ask where the person in it
# is. It proposes a selection and changes nothing.
@bp.get("/api/asset/<int:asset_id>/enhance")
@require_family
def enhance_suggestion(asset_id: int):
    """What the Auto button should do to *this* photograph.

    Read off the file rather than the thumbnail: white balance and the black
    and white points are exactly the things a re-encode moves, and the whole
    point of the endpoint is that the numbers belong to this picture.

    Advisory only. Nothing is written, and an empty ``settings`` means the
    editor should leave every slider where it is rather than apply a
    house average.
    """
    from ..media import enhance                          # noqa: PLC0415

    asset = _guard(db.get_asset(_conn(), asset_id))
    if asset.get("kind") != "picture":
        abort(400, description="Only photographs can be enhanced")
    try:
        with Image.open(_asset_file(asset)) as img:
            settings = enhance.suggest(img)
    except (OSError, ValueError):
        abort(404)
    return jsonify({"settings": settings, "summary": enhance.describe(settings)})


@bp.post("/api/asset/<int:asset_id>/portrait-masks")
@require_family
def portrait_masks(asset_id: int):
    """Propose skin and hair selections for the Photo Studio's brush.

    Returned as two greyscale PNGs, base64 in JSON, because the client needs
    them as image data it can draw straight into a mask canvas and this keeps
    it to one request.
    """
    import base64                                       # noqa: PLC0415
    import io                                           # noqa: PLC0415

    from ..media import portrait                        # noqa: PLC0415

    if not portrait.available():
        return jsonify({"error": "Automatic selection needs OpenCV, which "
                                 "this installation does not have."}), 501

    cfg = _cfg()
    row = _guard(db.get_asset(_conn(), asset_id))
    if row["kind"] != "picture":
        return jsonify({"error": "Only photographs have a face to find."}), 400

    try:
        with media._open_oriented(_asset_file(row)) as source:  # noqa: SLF001
            work = source.convert("RGB")
            work.thumbnail((portrait.WORK_SIZE * 2, portrait.WORK_SIZE * 2))
            found = portrait.auto_masks(work, cfg.state_dir)
    except Exception as exc:                            # noqa: BLE001
        return jsonify({"error": f"That photograph could not be read: {exc}"}), 400

    if not found.get("masks"):
        return jsonify({
            "faces": found.get("faces", 0),
            "reason": found.get("reason", "no face found"),
            "masks": {},
        })

    encoded = {}
    for name, mask in found["masks"].items():
        buffer = io.BytesIO()
        Image.fromarray(mask, mode="L").save(buffer, format="PNG", optimize=True)
        encoded[name] = base64.b64encode(buffer.getvalue()).decode("ascii")
    return jsonify({"faces": found["faces"], "box": found.get("box"),
                    "masks": encoded})


# Registered on `bp`, so it exists on the family app too. Family members and
# administrators may correct a shared photograph; guests keep the harmless
# in-view CSS turn, but may not rewrite what the household sees.
class _StagedBytes:
    """Presents finished bytes with the ``save`` that upload staging expects."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def save(self, stream) -> None:
        stream.write(self._data)


def _stage_edited_copy(conn, cfg, source, payload, name):
    """Hold a family member's edit for an administrator instead of publishing it.

    A family member may edit any photograph they can see, and saving the result
    used to put it straight into the library beside its source. Uploading a file
    has always waited for an administrator, and an edit saved back is the same
    act with an extra step, so it waits too. Approval puts it exactly where the
    direct save would have: beside its source, with the source's visibility.
    """
    from ..media import upload_review  # noqa: PLC0415

    folder = source.get("folder") or ""
    overrides = {
        "kind": "picture", "rotation": 0, "rot_source": "manual",
        "captured_at": source.get("captured_at"), "date_key": source.get("date_key"),
        "date_source": source.get("date_source"),
        "nsfw": source.get("nsfw"), "nsfw_score": source.get("nsfw_score"),
        # Explicit content stays hidden however the source's folder is ruled.
        "visibility": VIS_HIDDEN if source.get("nsfw") else source.get("visibility"),
        "vis_source": "item",
        "caption": f"Edited copy of {source['filename']}",
        "_pin_visibility": True,
        # Named so the review queue can say what this is and where it goes,
        # rather than showing an administrator an opaque generated filename.
        "_edited_from": source["filename"],
    }
    staged = upload_review.stage(conn, cfg, _StagedBytes(payload), name,
                                 source["root"], folder, current_user().id,
                                 overrides=overrides, place=folder)
    return jsonify(status="pending", pending_id=staged["id"], filename=name,
                   message="Saved for an administrator to approve. It joins the "
                           "library once they do."), 202


@bp.post("/api/asset/<int:asset_id>/edited-copy")
@require_family
def save_edited_copy(asset_id: int):
    """Create a derivative beside its source; never accept an output path."""
    import io

    conn = _conn()
    source = _guard(db.get_asset(conn, asset_id))
    if source["kind"] != "picture" or source.get("trashed"):
        return jsonify(error="Choose a photograph from the library."), 400
    path = _asset_file(source)
    if request.mimetype != "image/png":
        return jsonify(error="The edited copy must be a PNG image."), 400
    limit = 100 * 1024 * 1024
    if request.content_length and request.content_length > limit:
        abort(413)
    payload = request.stream.read(limit + 1)
    if len(payload) > limit:
        abort(413)
    try:
        with Image.open(io.BytesIO(payload)) as uploaded:
            if uploaded.format != "PNG" or uploaded.width * uploaded.height > 24_000_000:
                raise ValueError()
            uploaded.verify()
        with Image.open(io.BytesIO(payload)) as uploaded:
            has_alpha = "A" in uploaded.getbands() or "transparency" in uploaded.info
            edited = uploaded.convert("RGBA" if has_alpha else "RGB")
    except (ValueError, OSError, Image.DecompressionBombError):
        return jsonify(error="Use a valid PNG of up to 24 megapixels."), 400

    # Same folder retains the member's library scope. A random name and 'xb'
    # make replacement impossible, including when requests arrive together.
    name = f"{path.stem[:100]}-edited-{secrets.token_hex(12)}.png"
    cfg = _cfg()
    exif = Image.Exif()
    if source.get("captured_at"):
        exif[0x9003] = media.capture_dates.from_timestamp(
            source["captured_at"]).strftime("%Y:%m:%d %H:%M:%S")
    # An administrator publishes; anyone else waits to be reviewed.
    if not current_user().is_admin:
        staged = io.BytesIO()
        edited.save(staged, "PNG", exif=exif)
        return _stage_edited_copy(conn, cfg, source, staged.getvalue(), name)
    # Beside its source — or, when the source's library folder cannot be
    # written (an NTFS drive on a Mac), in the same folder of the new-files
    # folder, which joins the library the first time. See new_files.py.
    try:
        root = new_files.destination(cfg, source["root"])
    except OSError as exc:
        return jsonify(error=f"Could not save the copy: {exc}"), 500
    if root == source["root"]:
        output = path.parent / name
    else:
        output = Path(root) / (source.get("folder") or "") / name
        output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".png.tmp")
    created = False
    rel = output.relative_to(Path(root).resolve()).as_posix()
    base = media.thumb_base(root, rel)
    try:
        # Seed the access policy BEFORE the file appears to a watching scanner.
        record = {k: source.get(k) for k in (
            "folder", "captured_at", "date_key", "date_source",
            "visibility", "nsfw", "nsfw_score",
        )}
        record.update(root=root, rel_path=rel, filename=name, ext="png", kind="picture",
                      width=edited.width, height=edited.height, thumb=base,
                      vis_source="item", rotation=0, rot_source="manual",
                      caption=f"Edited copy of {source['filename']}")
        if source.get("nsfw"):
            record["visibility"] = VIS_HIDDEN
        new_id = db.upsert_asset(conn, record)
        with temporary.open("xb") as stream:
            edited.save(stream, "PNG", exif=exif)
        # Hard-link publication is atomic and refuses an existing destination.
        os.link(temporary, output)
        created = True
        temporary.unlink()
        media.write_thumbnails(edited, cfg.thumbs_dir, base, cfg.thumb_sizes,
                               cfg.thumb_format)
        stat = output.stat()
        db.update_asset(conn, new_id, size=stat.st_size, mtime=stat.st_mtime)
    except Exception:
        # Only this request's exclusive, random destination can be removed.
        if 'new_id' in locals():
            if created:
                output.unlink(missing_ok=True)
            temporary.unlink(missing_ok=True)
            conn.execute("DELETE FROM assets WHERE id=?", (new_id,))
            conn.commit()
            media.remove_thumbnails(cfg.thumbs_dir, base, cfg.thumb_sizes, cfg.thumb_format)
        current_app.logger.exception("Could not save edited copy")
        return jsonify(error="Could not save the copy. Check available disk space and folder permissions."), 500
    return jsonify(_public(db.get_asset(conn, new_id))), 201


# One turn at a time per photograph. Every turn rewrites the same thumbnail
# files through the same `.tmp` names, and three clicks in a second on Windows
# had two requests renaming one temporary file: a sharing violation, a 500, and
# a saved angle decided by which thread finished last rather than which click
# came last. Held weakly, so photographs nobody is turning cost nothing.
_turning: weakref.WeakValueDictionary[int, threading.Lock] = weakref.WeakValueDictionary()
_turning_guard = threading.Lock()


def _turn_lock(asset_id: int) -> threading.Lock:
    with _turning_guard:
        lock = _turning.get(asset_id)
        if lock is None:
            lock = _turning[asset_id] = threading.Lock()
        return lock


@bp.post("/api/asset/<int:asset_id>/rotate")
@require_family
def rotate_asset(asset_id: int):
    """Set the turn that puts this photograph the right way up, for good.

    The viewer has had a rotate button for a long time and it was worth very
    little: a CSS transform that reset the moment the photograph was closed, so
    a scanned print came out sideways again tomorrow. This is the version that
    sticks — the thumbnails everybody sees are rewritten, the recorded shape
    follows the turn so the grid lays out a cell of the right proportions, and
    it is marked ``manual`` so no later rescan and no detector overrules it.

    Family members and administrators may save this shared correction. Guests
    cannot, because Ninaivu has no notion of a guest's private library view.
    """
    data = json_object()
    raw = data.get("rotation")
    # A quarter turn or nothing. Anything else means a resample, a loss of
    # sharpness and a shape no layout can predict — for a fault that is always
    # exactly a quarter turn in the first place.
    try:
        if isinstance(raw, bool) or raw is None:
            raise TypeError
        turn = int(raw)
        if turn != float(raw):
            raise ValueError
    except (TypeError, ValueError, OverflowError):   # Infinity, 1e400
        return jsonify({"error": "Rotation must be 0, 90, 180 or 270."}), 400
    turn %= 360
    if turn not in (0, 90, 180, 270):
        return jsonify({"error": "Rotation must be 0, 90, 180 or 270."}), 400

    conn = _conn()
    user = current_user()
    row = _guard(db.get_asset(conn, asset_id, user.id))
    if row["kind"] != "picture":
        # A video carries its rotation in the container, which every browser
        # already honours; re-encoding one to turn it is a different and much
        # larger promise than this endpoint makes.
        return jsonify({"error": "Only photographs can be turned."}), 400

    cfg = _cfg()
    lock = _turn_lock(asset_id)
    with lock:
        try:
            source = media._open_oriented(Path(row["root"]) / row["rel_path"])
        except Exception as exc:                   # noqa: BLE001
            return jsonify({"error": f"That file could not be read: {exc}"}), 400

        # `indexed_at` is the thumbnail's version, and the thumbnails are about
        # to be rewritten — without this the new file sits behind a URL every
        # browser has been told will never change. Stamped inside the lock, so
        # the turn written last also carries the newest version.
        fields: dict[str, Any] = {"rotation": turn, "rot_source": "manual",
                                  "indexed_at": time.time()}
        turned = upright.apply(source, turn)
        fields["width"], fields["height"] = turned.size
        if row["thumb"]:
            try:
                media.write_thumbnails(turned, cfg.thumbs_dir, row["thumb"],
                                       cfg.thumb_sizes, cfg.thumb_format,
                                       cfg.thumb_quality)
            except OSError as exc:
                return jsonify({"error": f"The thumbnails could not be "
                                         f"rewritten: {exc}"}), 500

        db.update_asset(conn, asset_id, **fields)
    auth.audit(conn, user.id, "rotate", f"{row['filename']} → {turn}°")
    return jsonify({"ok": True, "rotation": turn, "rotation_source": "manual",
                    "width": fields["width"], "height": fields["height"],
                    "thumb_v": media.thumb_version(
                        {"indexed_at": fields["indexed_at"], "rotation": turn},
                        _recipe())})


# ---------------------------------------------------------------------------
# Turning the files themselves — admin only
# ---------------------------------------------------------------------------

def _reshoot(row: dict[str, Any], cfg: Config) -> dict[str, Any]:
    """Rebuild the derivatives of a file that has just been turned on disk.

    The turn now lives in the file, so everything Ninaivu derived from it is a
    picture of how it used to be: the thumbnails, the shape the grid lays out,
    the blur placeholder, the colour. All of them are made again from the file
    as it is now.
    """
    path = Path(row["root"]) / row["rel_path"]
    fields: dict[str, Any] = {"indexed_at": time.time()}
    try:
        stat = path.stat()
        fields["size"], fields["mtime"] = stat.st_size, stat.st_mtime
    except OSError:
        pass

    source: Image.Image | None = None
    if row["kind"] == "video":
        source = media.extract_video_frame(path, cfg.video_thumb_offset)
        if source is not None and not media.FFMPEG:
            # ffmpeg applies the container's rotation as it decodes; OpenCV,
            # the fallback, hands back the raw frame and leaves it to us.
            source = upright.apply(source, turn_file.video_rotation(path))
    else:
        try:
            source = media._open_oriented(path)      # noqa: SLF001 — same package
        except Exception:                            # noqa: BLE001
            source = None

    if source is None:
        return fields

    try:
        fields["width"], fields["height"] = source.size
        if row.get("thumb"):
            media.write_thumbnails(source, cfg.thumbs_dir, row["thumb"],
                                   cfg.thumb_sizes, cfg.thumb_format,
                                   cfg.thumb_quality)
            fields["blurhash"] = media.blurhash_encode(source)
            fields["color"] = media.dominant_color(source)
    except Exception as exc:                         # noqa: BLE001
        # The change itself still stands; what failed is the refreshed
        # thumbnail, which would otherwise go on showing the old picture with
        # nothing anywhere to say why.
        import logging
        logging.getLogger(__name__).warning(
            "thumbnails for asset %s were not refreshed: %s: %s",
            row.get("id"), type(exc).__name__, exc)
    finally:
        try:
            source.close()
        except Exception:                            # noqa: BLE001
            pass
    return fields


@bp.post("/api/rotate")
@require_admin
def rotate_originals():
    """Turn a selection of photographs and videos — in the files themselves.

    The viewer's rotate button writes a number into the index and leaves the
    file alone. That is right for a correction one person makes to one
    photograph: it is instant, it is undoable, and nothing on the disk moves.

    It is not enough for a shoebox of scanned prints that all came out
    sideways, because the moment one of them leaves Ninaivu — copied to a
    stick, attached to an email, opened in Explorer — it is sideways again.
    This endpoint is for that: it writes the turn into the file, so the
    correction travels with the photograph.

    Everything it does is lossless. JPEG and TIFF get two bytes changed and
    are not decoded at all; MP4 and MOV are remuxed with the streams copied
    across untouched; PNG and BMP are turned and re-saved, which those formats
    survive exactly. Anything that would need re-encoding is refused by name
    and left alone, and the file's untouched original is kept in the library's
    bin before the first time Ninaivu writes to it.

    Admin only, and on the shared blueprint like deleting and hiding, for the
    same reason: deciding that forty photographs are sideways means looking at
    them, and the gallery is where you look.
    """
    data = json_object()
    step = turn_file.quarter(data.get("rotation"))
    if step is None:
        return jsonify({"error": "Rotation must be 0, 90, 180 or 270."}), 400

    ids = _id_list(data, MAX_ROTATE)
    if not ids:
        return jsonify({"rotated": 0, "skipped": []})
    if step == 0:
        return jsonify({"rotated": 0, "skipped": []})

    conn = _conn()
    user = current_user()
    cfg = _cfg()

    # Narrow to what this admin can actually see, exactly as deleting does, so
    # an id typed into a console cannot reach a file outside their library.
    allowed, _ = db.query_assets(
        conn, _roots(), ids=ids, include_nsfw=True, include_trashed=True,
        limit=len(ids), offset=0, **_viewer())
    if not allowed:
        return jsonify({"rotated": 0, "skipped": []})

    rotated: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []

    for row in allowed:
        row = dict(row)
        name = row["filename"]
        path = Path(row["root"]) / row["rel_path"]

        if not new_files.writable(row["root"]) and Path(row["root"]).is_dir():
            skipped.append({"id": row["id"], "name": name, "why": new_files.READ_ONLY})
            continue
        if row["kind"] == "audio" or not turn_file.can_rotate_original(path):
            skipped.append({"id": row["id"], "name": name,
                            "why": turn_file.rotate_original(path, step,
                                                             row["kind"]).why
                                   or "this kind of file cannot be turned"})
            continue

        # The safety net goes up before anything is written, and a failure to
        # put it up stops the write. A bulk turn over four thousand
        # photographs is exactly the operation somebody wants to take back.
        try:
            kept = (recycle.keep_original(row["root"], row["rel_path"])
                    or recycle.kept_copy_for(row["root"], row["rel_path"]))
        except OSError as exc:
            skipped.append({"id": row["id"], "name": name,
                            "why": f"a copy of the original could not be kept, "
                                   f"so it was left alone: {exc}"})
            continue

        result = turn_file.rotate_original(path, step, row["kind"])
        if not result.ok:
            skipped.append({"id": row["id"], "name": name, "why": result.why})
            continue
        try:
            recycle.note_rewritten(kept, row["root"], row["rel_path"])
        except OSError:
            pass                      # the turn stands; only the note is missing

        # One photograph that cannot be re-read must not cost the rest of the
        # batch its turn, nor the admin the list of what was done. The file is
        # already turned; its changed modified time brings it back into the
        # index at the next scan.
        try:
            fields = _reshoot(row, cfg)
            # The file now says which way up it goes, so Ninaivu must stop
            # saying it too — otherwise the turn is applied twice, once by the
            # file and once by the index, and the photograph comes out
            # sideways the other way.
            fields["rotation"] = 0
            fields["rot_source"] = "exif" if result.how == "exif" else "file"
            if result.how == "exif":
                fields["orientation"] = result.orientation
            db.update_asset(conn, row["id"], **fields)
        except Exception as exc:                            # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception(
                "Turned %s, but could not update the index", path)
            skipped.append({"id": row["id"], "name": name,
                            "why": f"it was turned, but the index could not be "
                                   f"updated until the next scan: {exc}"})
            continue
        rotated.append({
            "id": row["id"], "how": result.how,
            "width": fields.get("width"), "height": fields.get("height"),
            "thumb_v": media.thumb_version(
                {"indexed_at": fields["indexed_at"], "rotation": 0}, _recipe()),
        })

    auth.audit(conn, user.id, "rotate_files",
               f"{len(rotated)} files turned {step}°"
               + (f", {len(skipped)} left alone" if skipped else ""))
    return jsonify({"rotated": len(rotated), "items": rotated,
                    "skipped": skipped, "rotation": step})


# ---------------------------------------------------------------------------
# Visibility — admin only
# ---------------------------------------------------------------------------

# On the shared blueprint, like deleting and rotating and for the same reason:
# choosing which photographs to hide means looking at them, and the gallery is
# where you look. It was console-only, which meant the "Show to" buttons on the
# gallery's own selection bar posted to a route that does not exist there —
# they had never once worked. That matters more now than it did: hiding is the
# step that has to happen before anything can be deleted.
#
# `/api/visibility/folder` stays console-only. A folder rule is library
# management, and the console is where folders are managed.
@bp.post("/api/visibility")
@require_admin
def set_visibility():
    """Mark items public, family-only or hidden."""
    data = json_object()
    level = str(data.get("visibility", "")).lower()
    if level not in VIS_VALUES:
        return jsonify({"error": "Visibility must be public, family or hidden."}), 400

    ids = _id_list(data)
    if not ids:
        return jsonify({"updated": 0})

    conn = _conn()
    count = db.set_visibility(conn, ids, VIS_VALUES[level],
                              source="item", user_id=current_user().id)
    auth.audit(conn, current_user().id, "set_visibility",
               f"{count} items → {level}")

    payload: dict[str, Any] = {"updated": count, "visibility": level}
    # Sound recordings are administrator-only and stay that way. Saying so is
    # the point: an admin who selected fourteen items and saw "12 changed"
    # deserves to know which two did not move, and why.
    if count < len(ids):
        payload["kept_back"] = len(ids) - count
        payload["note"] = ("Sound recordings stay administrator-only and were "
                           "left as they are.")
    return jsonify(payload)


@admin_only.post("/api/visibility/folder")
@require_admin
def set_folder_visibility():
    """Mark a folder — and anything scanned into it later.

    The library root is not a folder for this purpose. See :func:`_is_root`.
    """
    data = json_object()
    level = str(data.get("visibility", "")).lower()
    if level not in VIS_VALUES:
        return jsonify({"error": "Visibility must be public, family or hidden."}), 400

    folder = auth.normalise_scope(data.get("folder")) or ""
    conn = _conn()
    root = _root()
    wanted = VIS_VALUES[level]

    if _is_root(folder):
        auth.audit(conn, current_user().id, "visibility_refused",
                   f"whole-library change to {level} refused")
        return jsonify({
            "error": ("The whole library cannot be changed in one go. Set the "
                      "visibility on a folder, or on the photographs "
                      "themselves."),
            "folder": folder,
            "visibility": level,
        }), 403

    impact = db.preview_folder_visibility(conn, root, folder, wanted)

    # A change that would make currently-hidden files visible to more people
    # has to be asked for twice. The client sends `confirm` only after it has
    # shown the reader what the first answer said, so a mis-click on a control
    # that sits one pixel from the right one cannot expose anything.
    if impact["exposed"] and not data.get("confirm"):
        return jsonify({
            "needs_confirmation": True,
            "impact": impact,
            "folder": folder,
            "visibility": level,
        }), 409

    count = db.set_folder_visibility(
        conn, root, folder, wanted, current_user().id
    )
    auth.audit(conn, current_user().id, "set_folder_visibility",
               f"{folder or '(whole library)'} → {level} ({count} items"
               + (f", {impact['exposed']} newly visible)" if impact["exposed"] else ")"))
    return jsonify({
        "updated": count,
        "folder": folder,
        "visibility": level,
        "impact": impact,
        "undo": db.undo_batches(conn, limit=1),
        "rules": db.folder_rules(conn, root),
    })


@bp.post("/api/delete")
@require_admin
def delete_items():
    """Move the chosen items into the library's recycle bin.

    Lives on the shared blueprint on purpose: this is the one management action
    that belongs in the gallery, because choosing what to delete means looking
    at the photographs. It is still admin-only, and it still asks for the
    password every time — the gallery is the app left open on the sofa.

    The password is the whole gate. What this does is reversible: the file is
    moved into the library's recycle bin, keeping its name, and the console
    can put it back. Erasing for good is a separate, deliberate act there.
    """
    data = json_object()
    ids = _id_list(data)
    if not ids:
        return jsonify({"deleted": 0, "failed": []})

    conn = _conn()
    user = current_user()

    # Narrow to what this admin can actually see, exactly as bulk_update does,
    # so an id typed into a console cannot reach outside their library.
    allowed, _ = db.query_assets(
        conn, _roots(), ids=ids, include_nsfw=True, include_trashed=True,
        limit=len(ids), offset=0, **_viewer())
    allowed_ids = [row["id"] for row in allowed]
    if not allowed_ids:
        return jsonify({"deleted": 0, "failed": []})

    # The password is the gate, and it is the only one.
    #
    # There used to be a second: a photograph had to be hidden before it could
    # be deleted. It was meant as a pause for thought, and in practice it was
    # two chores to do one thing — nobody choosing a picture to delete is in
    # any doubt about which picture it is. What follows is a move into the
    # recycle bin, where the file keeps its name and can be put back, so the
    # pause for thought is already there and does not need staging.
    #
    # Limited like every other password prompt, and on the same allowance as
    # changing the password: this one had no limit at all.
    from .accounts_api import reauthenticate_limited              # noqa: PLC0415
    answer = reauthenticate_limited(conn, user.id, str(data.get("password", "")))
    if answer is None:
        return jsonify({"error": "Too many attempts. Wait a few minutes "
                                 "and try again."}), 429
    if not answer:
        auth.audit(conn, user.id, "delete_refused",
                   f"{len(allowed_ids)} items — password not given or wrong")
        return jsonify({
            "needs_password": True,
            "count": len(allowed_ids),
            "error": ("Enter your password to delete." if not data.get("password")
                      else "That password is not right."),
        }), 401

    result = recycle.recycle(conn, allowed_ids, user_id=user.id, roots=_roots())

    auth.audit(conn, user.id, "delete",
               f"{result['deleted']} items moved to the recycle bin")
    return jsonify({
        "deleted": result["deleted"],
        "failed": result["failed"],
        "bin": recycle.count(conn),
    })


@admin_only.get("/api/recycle")
@require_admin
def recycle_bin():
    """What is in the bin, and where it is on disk."""
    conn = _conn()
    items = recycle.listing(conn)
    for item in items:
        item["thumbnail"] = (f"/api/recycle/thumb/{item['id']}"
                             if item.get("thumb") and item["present"] else None)
    cfg = _cfg()
    return jsonify({
        "items": items,
        "summary": recycle.count(conn),
        "folders": [str(recycle.bin_path(r)) for r in cfg.roots],
        # What the household chose, so the screen can say it rather than
        # leaving somebody to wonder whether anything is being erased.
        "erase_after_days": float(getattr(cfg, "bin_erase_after_days", 0) or 0),
    })


@admin_only.get("/api/recycle/thumb/<int:entry_id>")
@require_admin
def recycle_thumbnail(entry_id: int):
    """The retained gallery thumbnail for a file in the recycle bin."""
    row = _conn().execute(
        "SELECT thumb FROM recycled WHERE id=? AND restored_at IS NULL",
        (entry_id,),
    ).fetchone()
    if row is None or not row["thumb"]:
        abort(404)

    cfg = _cfg()
    size = _int_arg("s", cfg.thumb_sizes[0])
    size = min(cfg.thumb_sizes, key=lambda value: abs(value - size))
    path = cfg.thumbs_dir / media.thumb_file(row["thumb"], size, cfg.thumb_format)
    if not path.is_file():
        abort(404)
    mime = "image/webp" if cfg.thumb_format.upper() == "WEBP" else "image/jpeg"
    response = send_file(path, conditional=True, mimetype=mime, max_age=86400)
    response.headers["Cache-Control"] = "private, max-age=86400"
    return response


@admin_only.post("/api/recycle/restore")
@require_admin
def recycle_restore():
    """Put files back where they came from, then re-index them."""
    data = json_object()
    ids = _id_list(data)            # the usual 5000, as purge already has
    conn = _conn()
    result = recycle.restore(conn, ids)
    if result["restored"]:
        auth.audit(conn, current_user().id, "recycle_restore",
                   f"{result['restored']} items put back")
        # The files are on disk again but not in the index; a scan is what
        # makes them visible, so start one rather than making somebody find it.
        scanner = _scanner()
        if not scanner.progress.snapshot().get("running"):
            root = _cfg().active_root
            if root:
                scanner.start(root)
    return jsonify(result)


@admin_only.post("/api/recycle/purge")
@require_admin
def recycle_purge():
    """Erase files from the bin for good, after asking for the password.

    Deleting from the gallery is reversible and asks for the password. This is
    not reversible, so it asks again — the console being open is not evidence
    that the person in front of it meant to lose a photograph permanently.
    """
    data = json_object()
    ids = _id_list(data)
    if not ids:
        return jsonify({"purged": 0, "failed": []})

    conn = _conn()
    user = current_user()
    # The same allowance as deleting and changing the password.
    from .accounts_api import reauthenticate_limited              # noqa: PLC0415
    answer = reauthenticate_limited(conn, user.id, str(data.get("password", "")))
    if answer is None:
        return jsonify({"error": "Too many attempts. Wait a few minutes "
                                 "and try again."}), 429
    if not answer:
        auth.audit(conn, user.id, "recycle_purge_refused",
                   f"{len(ids)} items — password not given or wrong")
        return jsonify({
            "needs_password": True,
            "count": len(ids),
            "error": ("Enter your password to erase these."
                      if not data.get("password") else "That password is not right."),
        }), 401

    result = recycle.purge(conn, ids)

    # The gallery thumbnail outlived the file so the bin could show what was
    # in it. Nothing is going to show it now.
    cfg = _cfg()
    for base in result.pop("thumbs", []):
        try:
            media.remove_thumbnails(cfg.thumbs_dir, base, cfg.thumb_sizes,
                                    cfg.thumb_format)
        except OSError:
            pass

    auth.audit(conn, user.id, "recycle_purge",
               f"{result['purged']} items erased from the recycle bin")
    result["bin"] = recycle.count(conn)
    return jsonify(result)


@admin_only.post("/api/visibility/preview")
@require_admin
def preview_folder_visibility():
    """What a folder rule would do, without doing any of it."""
    data = json_object()
    level = str(data.get("visibility", "")).lower()
    if level not in VIS_VALUES:
        return jsonify({"error": "Visibility must be public, family or hidden."}), 400
    folder = auth.normalise_scope(data.get("folder")) or ""
    return jsonify(db.preview_folder_visibility(
        _conn(), _root(), folder, VIS_VALUES[level]))


@admin_only.get("/api/visibility/history")
@require_admin
def visibility_history():
    """Recent bulk visibility changes, so one can be picked out and undone."""
    return jsonify({"changes": db.undo_batches(_conn(), limit=10),
                    "names": VIS_NAMES})


@admin_only.post("/api/visibility/undo")
@require_admin
def undo_visibility():
    """Put a bulk visibility change back exactly as it was."""
    data = json_object()
    batch = data.get("batch_id")
    try:
        batch = int(batch) if batch else None
    except (TypeError, ValueError, OverflowError):
        return jsonify({"error": "batch_id must be a number."}), 400
    conn = _conn()
    result = db.undo_visibility_batch(conn, batch)
    if not result.get("ok"):
        return jsonify(result), 409
    auth.audit(conn, current_user().id, "undo_visibility",
               f"restored {result['restored']} items to how they were before "
               f"{result['folder'] or '(whole library)'} was changed")
    result["rules"] = db.folder_rules(conn, _root())
    return jsonify(result)


@admin_only.get("/api/visibility/rules")
@require_admin
def visibility_rules():
    return jsonify({"rules": [
        {**rule, "visibility_name": VIS_NAMES[int(rule["visibility"])]}
        for rule in db.folder_rules(_conn(), _root())
    ]})


@admin_only.delete("/api/visibility/folder")
@require_admin
def clear_folder_visibility():
    folder = auth.normalise_scope(request.args.get("folder")) or ""
    db.clear_folder_rule(_conn(), _root(), folder)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

@bp.get("/api/facets")
def facets():
    limits = _viewer()
    return jsonify(db.facets(
        _conn(), _roots(),
        max_visibility=limits["max_visibility"], scope=limits["scope"],
    ))


@bp.get("/api/occasions")
def occasions():
    """The occasions the library groups itself into.

    Read-only and derived: every scan rebuilds these, so nothing here is a
    place to put something a person wants to keep. Hand-made albums are what
    that is for.
    """
    limits = _viewer()
    return jsonify({"occasions": db.list_occasions(
        _conn(), _roots(),
        viewer_id=limits["viewer_id"], max_visibility=limits["max_visibility"],
        scope=limits["scope"], limit=max(1, min(_int_arg("limit", 200), 1000)),
    )})


@bp.get("/api/timeline")
def timeline():
    limits = _viewer()
    return jsonify({"days": db.timeline(
        _conn(), _roots(),
        max_visibility=limits["max_visibility"], scope=limits["scope"],
    )})


@bp.get("/api/duplicates")
@require_family
def duplicates():
    groups = db.duplicate_groups(_conn(), _roots(), **_viewer())
    return jsonify({
        "groups": [
            {"group": g["group"], "wasted": g["wasted"],
             "wasted_h": media.human_size(g["wasted"]),
             # The frame worth keeping, by focus then resolution — but never
             # over a favourite or a starred shot. Advisory: nothing is
             # deleted or hidden on the strength of it.
             "best": g["best"],
             "items": [_public(i) for i in g["items"]]}
            for g in groups
        ],
        "total_wasted": sum(g["wasted"] for g in groups),
    })


@bp.get("/api/similar/<int:asset_id>")
def similar(asset_id: int):
    conn = _conn()
    _guard(db.get_asset(conn, asset_id))
    row = conn.execute(
        "SELECT vector, dim FROM embeddings WHERE asset_id=?", (asset_id,)
    ).fetchone()
    if not row:
        return jsonify({"items": [], "reason": "no-embedding"})
    ids, buffer, dim, _ = db.embedding_store(conn)
    ranked = ai_mod.similar_to(row["vector"], ids, buffer, dim, top_k=60)
    ranked = [(i, s) for i, s in ranked if i != asset_id]
    if not ranked:
        return jsonify({"items": []})
    wanted = [i for i, _ in ranked]
    rows, _ = db.query_assets(
        conn, _roots(), ids=wanted, limit=len(wanted), offset=0, **_viewer()
    )
    scores = dict(ranked)
    rows.sort(key=lambda r: -scores.get(r["id"], 0))
    return jsonify({"items": [_public(r, scores) for r in rows]})


@bp.get("/api/suggest")
def suggest():
    """Autocomplete for the search box: tags, folders, cameras."""
    text = request.args.get("q", "").strip().lower()
    limits = _viewer()
    data = db.facets(_conn(), _roots(), limit=200,
                     max_visibility=limits["max_visibility"], scope=limits["scope"])
    out: list[dict[str, Any]] = []
    for kind, items, field in (
        ("tag", data["tags"], "name"),
        ("folder", data["folders"], "name"),
        ("camera", data["cameras"], "name"),
    ):
        for item in items:
            value = str(item[field] or "")
            if value and (not text or text in value.lower()):
                out.append({"type": kind, "value": value, "count": item["count"]})
    out.sort(key=lambda x: (-x["count"], x["value"]))
    return jsonify({"suggestions": out[:20], "concepts": [
        c for c in ai_mod.LABELS if text and text in c
    ][:8]})


# ---------------------------------------------------------------------------
# Albums
# ---------------------------------------------------------------------------

def _own_album(album_id: int):
    """404 for an album that is not there; 403 for one that is not theirs.

    Albums made before Ninaivu recorded an owner (``created_by`` NULL) stay
    editable by any family member, so upgrading does not lock the household
    out of its own albums. An admin can always edit.
    """
    exists, owner = db.album_owner(_conn(), album_id)
    if not exists:
        abort(404)
    from ..server import date_policy
    album = db.get_album(_conn(), album_id)
    if album["item_ids"] and not date_policy.allows(album):
        abort(404)
    user = current_user()
    if owner is not None and owner != user.id and not user.is_admin:
        abort(403)


#: How many ids go into one ``IN (...)`` — well under SQLite's variable limit.
_IN_CHUNK = 500


def _visible_ids(ids: Sequence[int]) -> list[int]:
    """Narrow caller-supplied asset ids to the ones this viewer may see."""
    if not ids:
        return []
    # In pieces: every id becomes one bound variable in ``a.id IN (...)``, and
    # SQLite's ceiling is 32,766 on stock builds. An album that grew past it —
    # a family member adding a whole library to one, a few thousand at a time
    # — could then not be read, patched or added to at all.
    ids = list(ids)
    conn, roots, viewer = _conn(), _roots(), _viewer()
    seen: list[int] = []
    for start in range(0, len(ids), _IN_CHUNK):
        piece = ids[start:start + _IN_CHUNK]
        rows, _ = db.query_assets(conn, roots, ids=piece, limit=len(piece),
                                 offset=0, **viewer)
        seen.extend(int(r["id"]) for r in rows)
    return seen


@bp.get("/api/albums")
@require_family
def albums():
    user = current_user()
    return jsonify({"albums": db.list_albums(
        _conn(), _roots(), **_viewer_limits(),
        viewer_id=user.id, is_admin=user.is_admin
    )})


@bp.get("/api/albums/<int:album_id>")
@require_family
def album_get(album_id: int):
    album = db.get_album(_conn(), album_id)
    if not album:
        abort(404)
    user = current_user()
    visible_ids = _visible_ids(album.get("item_ids", []))
    from ..server import date_policy
    if not date_policy.allows(album):
        abort(404)
    if not visible_ids and (date_policy.restricted() or (album.get("created_by") != user.id and not user.is_admin)):
        abort(404)
    album["item_ids"] = visible_ids
    album["n"] = len(visible_ids)
    if album.get("cover_id") not in visible_ids:
        album["cover_id"] = next(iter(visible_ids), None)
    return jsonify({"album": album})


@bp.patch("/api/albums/<int:album_id>")
@require_family
def album_update(album_id: int):
    _own_album(album_id)
    data = _album_payload()
    name = data.get("name")
    cover_id = data.get("cover_id")
    if name is not None and (not isinstance(name, str) or not name.strip()):
        abort(400, description="Album name must be non-empty text")
    if cover_id is not None:
        cover_id = _album_id(cover_id, allow_zero=True)
    if cover_id:
        if not _visible_ids([cover_id]):
            abort(400, description="Cover asset not accessible")
    db.update_album(_conn(), album_id, name=name, cover_id=cover_id)
    album = db.get_album(_conn(), album_id)
    album["item_ids"] = _visible_ids(album["item_ids"])
    album["n"] = len(album["item_ids"])
    if album.get("cover_id") not in album["item_ids"]:
        album["cover_id"] = next(iter(album["item_ids"]), None)
    return jsonify({"ok": True, "album": album})


@bp.post("/api/albums")
@require_family
def album_create():
    data = _album_payload()
    name = data.get("name", "")
    if not isinstance(name, str):
        abort(400, description="Album name must be text")
    name = name.strip()[:120]
    if not name:
        return jsonify({"error": "Name required"}), 400
    ids = _visible_ids(_album_ids(data))
    album_id = db.create_album(_conn(), name, created_by=current_user().id)
    # Only assets the caller can actually see may go in, so an album cannot be
    # used to collect ids the caller was never allowed to know exist.
    if ids:
        db.album_add(_conn(), album_id, ids)
    return jsonify({"id": album_id, "name": name})


@bp.post("/api/albums/<int:album_id>/items")
@require_family
def album_items(album_id: int):
    _own_album(album_id)
    data = _album_payload()
    if "remove" in data and not isinstance(data["remove"], bool):
        abort(400, description="Remove must be true or false")
    ids = _visible_ids(_album_ids(data))
    if data.get("remove"):
        db.album_remove(_conn(), album_id, ids)
    else:
        db.album_add(_conn(), album_id, ids)
    return jsonify({"ok": True, "count": len(_visible_ids(db.album_asset_ids(_conn(), album_id)))})


def _album_payload() -> dict[str, Any]:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400, description="Album settings must be a JSON object")
    return data


def _album_id(value: Any, *, allow_zero: bool = False) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        abort(400, description="Asset IDs must be integers")
    minimum = 0 if allow_zero else 1
    if isinstance(value, (bool, float)) or not minimum <= parsed <= 2**63 - 1:
        abort(400, description="Asset ID is out of range")
    return parsed


def _album_ids(data: dict[str, Any]) -> list[int]:
    values = data.get("ids", [])
    if not isinstance(values, list):
        abort(400, description="Asset IDs must be a list")
    if len(values) > 5000:
        abort(400, description="Send at most 5000 asset IDs at a time")
    return [_album_id(value) for value in values]


@bp.delete("/api/albums/<int:album_id>")
@require_family
def album_delete(album_id: int):
    _own_album(album_id)
    db.delete_album(_conn(), album_id)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

# Every status the API actually raises, not most of them.
#
# 400 was missing, which meant `abort(400, description=...)` — the way this
# module explains a bad folder, an empty upload or a malformed request —
# returned Flask's HTML error page. The client asked for JSON, got markup,
# and showed nothing at all: the careful sentence explaining that a phone is
# not a folder never reached anybody.
@bp.app_errorhandler(400)
@bp.app_errorhandler(401)
@bp.app_errorhandler(403)
@bp.app_errorhandler(404)
@bp.app_errorhandler(409)
@bp.app_errorhandler(410)
@bp.app_errorhandler(415)
@bp.app_errorhandler(429)
@bp.app_errorhandler(500)
def json_errors(error):  # noqa: ANN001
    if request.path.startswith("/api/"):
        return jsonify({
            "error": getattr(error, "description", str(error)),
            "status": getattr(error, "code", 500),
        }), getattr(error, "code", 500)
    return error

# ---------------------------------------------------------------------------
# The rest of the API
# ---------------------------------------------------------------------------
#
# Three thousand lines in one module was one module too many. These four hold
# the sections that were easiest to lift out whole — faces, straightening and
# phones, the library folder, and shares with what grew up beside them. They
# register on the blueprints above, so they must be imported after those exist,
# which is why this sits at the foot of the file rather than the head.
from . import (api_faces, api_library, api_phone_backup,             # noqa: E402,F401
               api_share, api_straighten)


# The AI Playground's routes live in their own module and register on `bp`.
from . import ai_playground_api  # noqa: E402,F401 - registers routes
