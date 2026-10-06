"""Memories, the map, uploading, and share links.

Split out of api.py. The routes, their paths and their blueprints are exactly
as they were."""

from __future__ import annotations

from pathlib import Path
from datetime import datetime
import hashlib
import hmac
import math
import mimetypes
import secrets
import time
from typing import Any

from flask import abort, current_app, jsonify, render_template, request, send_file
from ..server import auth
from ..storage import db
from ..media import media, stills
from ..server.auth import current_user, require_family
from ..utils.filenames import safe_filename
from ._body import json_body

# The blueprints and the shared helpers stay in api.py: these routes
# are registered on the same two blueprints they always were, so every
# path and every endpoint name is unchanged.
from .api import bp, _asset_file, _cfg, _conn, _guard, _int_arg, _own_album, _public, _roots, _safe_under, _viewer
from .api import (INLINE_TYPES, UPLOAD_EXTENSIONS, _location_may_ride_along,
                  _stripped_video, _viewing_copy)


# ---------------------------------------------------------------------------
# Ninaivu 5.0: Memories, Geo, Upload, Shares, People
# ---------------------------------------------------------------------------

@bp.get("/api/memories/on-this-day")
def memories_on_this_day():
    now = datetime.now()
    month = _int_arg("month", now.month)
    day = _int_arg("day", now.day)
    conn = _conn()
    result = db.query_on_this_day(
        conn,
        _roots(),
        month=month,
        day=day,
        **_viewer(),
    )
    result["items"] = [_public(item) for item in result["items"]]
    formatted_years = {}
    for yr, items in result["years"].items():
        formatted_years[yr] = [_public(item) for item in items]
    result["years"] = formatted_years
    return jsonify(result)


@bp.get("/api/geo/points")
@require_family
def geo_points():
    north = request.args.get("north", type=float)
    south = request.args.get("south", type=float)
    east = request.args.get("east", type=float)
    west = request.args.get("west", type=float)
    conn = _conn()
    points = db.query_geo_points(
        conn,
        _roots(),
        north=north,
        south=south,
        east=east,
        west=west,
        **_viewer(),
    )
    return jsonify({"points": points, "total": len(points)})


# -- the map ---------------------------------------------------------------------
#
# Where a photograph was taken is left out of everything a guest is sent (see
# tests/test_location_privacy.py), so the map is for family members and admins.

def _geo_limits() -> dict[str, Any]:
    """The viewer's limits, and the map's own filters: a year, a person."""
    limits = {**_viewer()}
    if year := _int_arg("year"):
        limits["year"] = year
    if person := _int_arg("person"):
        limits["person"] = person
    return limits


def _geo_bounds() -> tuple[float, float, float, float] | None:
    values = [request.args.get(k, type=float) for k in ("south", "north", "west", "east")]
    if any(v is None or not math.isfinite(v) for v in values):
        return None
    return values[0], values[1], values[2], values[3]


@bp.get("/api/geo/clusters")
@require_family
def geo_clusters():
    """What is on screen, grouped a pin's width at a time (storage/geo.py)."""
    from ..storage import geo

    found = geo.clusters(_conn(), _roots(), zoom=_int_arg("zoom", 2),
                         bounds=_geo_bounds(), **_geo_limits())
    return jsonify({"clusters": found, "total": sum(c["count"] for c in found)})


@bp.get("/api/geo/places")
@require_family
def geo_places():
    """Every place there are photographs from, with the years there are any for."""
    from ..storage import geo

    return jsonify(geo.places(_conn(), _roots(), **_geo_limits()))


@bp.get("/api/geo/photos")
@require_family
def geo_photos():
    """The photographs of one group (by its bounds) or one place (by name)."""
    from ..storage import geo

    place = None
    if "country" in request.args or "city" in request.args:
        place = (request.args.get("country", ""), request.args.get("city", ""))
    bounds = _geo_bounds()
    if place is None and bounds is None:
        abort(400, description="Say which place: its bounds, or its country and city")
    ids, total = geo.photo_ids(_conn(), _roots(), bounds=bounds, place=place, **_geo_limits())
    return jsonify({"ids": ids, "total": total})


@bp.get("/api/geo/trail")
@require_family
def geo_trail():
    """A year's photographs in the order they were taken, as stops on a route."""
    from ..storage import geo

    limits = _geo_limits()
    if "year" not in limits:
        abort(400, description="Choose a year to see its trips")
    stops = geo.trail(_conn(), _roots(), **limits)
    return jsonify({"stops": stops, "total": sum(s["count"] for s in stops)})


@bp.post("/api/upload")
@require_family
def upload_files():
    """Upload photos and videos directly into the library."""
    if "files" not in request.files and "file" not in request.files:
        abort(400, description="No files provided in upload payload")

    uploaded_files = request.files.getlist("files") or request.files.getlist("file")
    if not uploaded_files:
        abort(400, description="No valid files selected")

    cfg = _cfg()
    roots = _roots()
    if not roots:
        abort(403, description="Your assigned library is no longer available")
    root_str = cfg.active_root if cfg.active_root in roots else roots[0]
    root_path = Path(root_str)
    if not root_path.is_dir():
        abort(409, description="The library folder is unavailable")
    scope = _viewer()["scope"] or ""
    if not _safe_under(root_path / scope, root_path):
        abort(403, description="Upload folder must stay inside your library")
    from ..media import upload_review

    results = []
    errors = []
    conn = _conn()

    for f in uploaded_files:
        if not f.filename:
            continue
        # Letters in any script survive: werkzeug's secure_filename reduced a
        # Tamil name to "jpg", which then failed the extension check below.
        safe_name = safe_filename(f.filename)
        if not safe_name or safe_name.startswith("."):
            continue
        if Path(safe_name).suffix.lower() not in UPLOAD_EXTENSIONS:
            errors.append({"filename": f.filename,
                           "error": "Ninaivu takes photographs, video and audio. "
                                    "That file type is not one of them."})
            continue

        try:
            results.append(upload_review.stage(conn, cfg, f, safe_name, root_str,
                                               scope, current_user().id))
        except Exception as exc:
            errors.append({"filename": safe_name, "error": str(exc)})

    return jsonify({"uploaded": results, "errors": errors, "total": len(results),
                    "pending_approval": True})


@bp.post("/api/shares")
@require_family
def create_share():
    data = json_body() or {}
    if not isinstance(data, dict):
        abort(400, description="Share settings must be a JSON object")
    scope = data.get("scope", "album")
    if scope not in ("album", "asset"):
        abort(400, description="Share scope must be album or asset")
    raw_id = data.get("target_id", 0)
    try:
        target_id = int(raw_id)
    except (TypeError, ValueError, OverflowError):
        abort(400, description="Target ID must be a positive integer")
    if isinstance(raw_id, (bool, float)) or not 0 < target_id <= 2**63 - 1:
        abort(400, description="Target ID must be a positive 64-bit integer")

    expires_in_days = data.get("expires_in_days")
    expires_at = None
    if expires_in_days is not None:
        try:
            days = float(expires_in_days)
            candidate = time.time() + days * 86400
        except (TypeError, ValueError, OverflowError):
            abort(400, description="Share expiry must be a finite number of days")
        if isinstance(expires_in_days, bool) or not math.isfinite(candidate) or days < 0:
            abort(400, description="Share expiry must be a finite, non-negative number of days")
        if days > 0:
            expires_at = candidate

    password = data.get("password")
    if password is not None and not isinstance(password, str):
        abort(400, description="Share password must be text")
    # Only new links are held to this; a link already made with a shorter
    # password keeps working. The guess limits below are per link, so a
    # two-character password would fall to the allowance alone.
    if password and len(password) < SHARE_PASSWORD_MIN:
        abort(400, description=f"A share password needs at least "
                               f"{SHARE_PASSWORD_MIN} characters.")

    conn = _conn()
    # A share link needs no sign-in at all, so minting one must not reach
    # further than this person could already see in the ordinary gallery: an
    # album is theirs to share only if they own it, an asset only if the
    # normal visibility rules already show it to them. An admin may still
    # deliberately share hidden or NSFW-flagged content, the same way they
    # can already view it.
    if scope == "album":
        _own_album(target_id)
    else:
        _guard(db.get_asset(conn, target_id))

    pwd_hash = auth.hash_password(password) if password else None
    token = secrets.token_urlsafe(16)
    share = db.create_share(
        conn,
        token=token,
        scope=scope,
        target_id=target_id,
        created_by=current_user().id,
        expires_at=expires_at,
        password=pwd_hash,
    )
    # The stored hash is authentication material, never response metadata.
    share.pop("password", None)
    return jsonify({
        "token": token,
        "share_url": f"/share/{token}",
        "share": share,
    })


@bp.get("/api/shares")
@require_family
def list_shares():
    user = current_user()
    conn = _conn()
    shares = db.list_shares(conn, created_by=None if user.is_admin else user.id)
    return jsonify({"shares": shares})


@bp.delete("/api/shares/<token>")
@require_family
def delete_share(token: str):
    conn = _conn()
    share = db.get_share(conn, token)
    if not share:
        abort(404)
    user = current_user()
    if not user.is_admin and share.get("created_by") != user.id:
        abort(403)
    db.delete_share(conn, token)
    return jsonify({"ok": True})


# A share link is the one thing in Ninaivu that answers to somebody with no
# account at all, so everything it reaches is resolved from the token and
# never from the caller: `_share_assets` below is the only door, and it opens
# onto exactly the photographs that token was minted for.


#: Everything a share link tells its holder about one photograph, besides the
#: token-bearing addresses to fetch it from. Enough to lay it out and play it.
SHARED_FIELDS = ("id", "name", "ext", "kind", "width", "height", "duration",
                 "blurhash", "color", "rotation", "has_thumb", "playable")


def _share_or_404(token: str) -> dict[str, Any]:
    share = db.get_share(_conn(), token)
    if not share:
        abort(404, description="Share link not found or expired")
    if share.get("expired"):
        abort(410, description="This share link has expired")
    return share


#: The shortest password a new share link may be given.
SHARE_PASSWORD_MIN = 4


def _share_password_attempt(token: str, supplied: str, stored: str) -> bool | None:
    """Check a link's password within its guess limits: True if right,
    False if wrong, None if refused unchecked because the limits are spent.

    Limited per caller and, as for a profile's PIN, per link from anywhere —
    a household has as many IPv6 addresses as it likes, so a limit per address
    alone did not limit guessing. The attempt is reserved before the password
    is checked (``accounts_api.reserve``): checking, verifying and then
    recording let every guess already in flight through. A link that uses up
    its allowance is paused, for longer each time, like the picker's PINs.
    """
    from .accounts_api import (                               # noqa: PLC0415
        _ATTEMPTS, _MAX_ATTEMPTS, _PROFILE_MAX_ATTEMPTS, _PROFILE_WINDOW, _WINDOW,
        _attempts_lock, clear_lockout, locked_out, release, reserve, strike_if_spent)

    key = f"{request.remote_addr}|share:{token}"
    everywhere = f"*|share:{token}"
    if locked_out(everywhere) or not reserve([
            (key, _MAX_ATTEMPTS, _WINDOW),
            (everywhere, _PROFILE_MAX_ATTEMPTS, _PROFILE_WINDOW)]):
        return None
    if supplied and auth.verify_password(supplied, stored):
        release(everywhere)
        clear_lockout(everywhere)
        with _attempts_lock:
            _ATTEMPTS.pop(key, None)
        return True
    strike_if_spent(everywhere, _PROFILE_MAX_ATTEMPTS)
    return False


def _share_unlock_cookie(token: str) -> str:
    return f"ninaivu_share_{token[:16]}"


def _share_unlocked(share: dict[str, Any], token: str) -> bool:
    """Has this browser already answered this link's password?

    The proof is an HMAC over the token keyed by the stored password hash. It
    needs no secret of its own and no new table: only the server can produce
    it, it is worthless for any other share, and it dies with the password if
    the link's password is ever changed.
    """
    stored = share.get("password")
    if not stored:
        return True
    want = hmac.new(stored.encode(), token.encode(), hashlib.sha256).hexdigest()
    got = request.cookies.get(_share_unlock_cookie(token), "")
    if got and hmac.compare_digest(got.encode("utf-8"), want.encode("ascii")):
        return True

    # A caller that is not a browser — a script, a test — may present the
    # password on every request instead of holding a cookie. The header is
    # fine; it was only ever the query string that had to go, because that is
    # what ends up in proxy logs and browser history.
    supplied = request.headers.get("X-Share-Password")
    if not supplied:
        return False
    return bool(_share_password_attempt(token, supplied, stored))


def _share_assets(share: dict[str, Any], conn,
                  only: int | None = None) -> list[dict[str, Any]]:
    """Exactly what this token may show, and nothing else.

    A link can never show more than its creator could open *today*. Checking
    only when the link was minted let it outlive every later decision: a
    photograph an admin hid or flagged went on being served to strangers, and
    a member confined to one folder could publish an owner-less album holding
    the rest of the library. So the creator's current limits — folders,
    visibility ceiling, explicit content, administrator-only kinds — are
    applied on every request. Anybody short of an administrator is also held
    to the family ceiling, since the recipient is not signed in at all.

    ``only`` narrows the answer to one asset. Every thumbnail and file request
    asks about exactly one, and resolving the whole album for each made a
    shared album of N photographs cost N² to display.
    """
    from ..server import date_policy

    limits = _creator_limits(share, conn)
    if limits is None:
        return []
    if share["scope"] == "album":
        if not date_policy.allows({"date_key": db.album_date(conn, share["target_id"])}):
            return []
        if only is not None:
            item_ids = [int(only)] if conn.execute(
                "SELECT 1 FROM album_items WHERE album_id=? AND asset_id=?",
                (share["target_id"], int(only))).fetchone() else []
        else:
            item_ids = db.album_asset_ids(conn, share["target_id"])
        if not item_ids:
            return []
        # Deliberately narrower than the single-asset branch below: an admin
        # sharing one hidden photograph chose that photograph, but an album
        # link shows whatever the album holds, and a hidden photograph that
        # happened to be in it would go out to a link that needs no sign-in.
        # So hidden and NSFW-flagged items stay out of album links for
        # everybody, admins included.
        #
        # In pieces: every id is one bound variable, and an album can hold
        # more than SQLite's limit (32,766 on stock builds) — at which point
        # the link answered 500 for as long as the album stayed that size.
        assets: list[dict[str, Any]] = []
        for start in range(0, len(item_ids), 500):
            piece = item_ids[start:start + 500]
            found, _ = db.query_assets(
                conn, limits["roots"], ids=piece, limit=len(piece),
                max_visibility=min(limits["max_visibility"], auth.VIS_FAMILY),
                scope=limits["scope"])
            assets.extend(found)
        return assets
    if only is not None and int(only) != int(share["target_id"]):
        return []
    asset = db.get_asset(conn, share["target_id"])
    # Trash is not a display hint anywhere else in the app, and an old link
    # must not go on serving a photograph the household has since removed.
    if not asset or asset.get("trashed") or not date_policy.allows(asset):
        return []
    if asset.get("root") not in limits["roots"]:
        return []
    if not limits["admin"]:
        if asset.get("nsfw") or not db.visible_to(
                asset, limits["max_visibility"], limits["scope"]):
            return []
    return [asset]


def _creator_limits(share: dict[str, Any], conn) -> dict[str, Any] | None:
    """What the person who made this link may see now, or None for nothing.

    A link whose creator has been disabled or deleted stops working: nobody
    is left who is entitled to what it shows.
    """
    creator = auth.get_user(conn, int(share["created_by"])) \
        if share.get("created_by") is not None else None
    if creator is None or not creator.active or creator.is_guest:
        return None
    cfg = _cfg()
    libraries = cfg.libraries or ([cfg.active_root] if cfg.active_root else [])
    roots, sub = auth.resolve_library(creator.assigned_library, libraries)
    return {
        "roots": roots,
        "scope": sub or (creator.effective_scope or None),
        "max_visibility": creator.max_visibility,
        "admin": creator.is_admin,
    }


def _share_asset_or_404(token: str, asset_id: int) -> tuple[dict, dict]:
    share = _share_or_404(token)
    if not _share_unlocked(share, token):
        abort(401, description="This link needs its password.")
    conn = _conn()
    for asset in _share_assets(share, conn, only=asset_id):
        if int(asset["id"]) == int(asset_id):
            return share, asset
    abort(404)


@bp.get("/share/<token>")
def shared_page(token: str):
    """The page the link actually opens.

    This route is the whole reason share links worked in the console and not
    in the world: the viewer minted `/share/<token>`, handed it over, and
    nothing on the server had ever claimed that path. It is served from the
    family app and needs no sign-in — that is what a share link is for.
    """
    _share_or_404(token)          # 404 or 410 before rendering anything
    return render_template("share.html", token=token,
                           app_name=current_app.config.get("APP_NAME", "Ninaivu"))


@bp.post("/api/share/<token>/unlock")
def unlock_shared(token: str):
    """Answer a link's password once, and get a cookie that carries it."""
    share = _share_or_404(token)
    stored = share.get("password")
    if not stored:
        return jsonify({"ok": True})
    data = json_body()
    if not isinstance(data, dict):
        abort(400, description="Share credentials must be a JSON object")
    supplied = data.get("password", "")
    if not isinstance(supplied, str):
        abort(400, description="Share password must be text")
    answer = _share_password_attempt(token, supplied, stored)
    if answer is None:
        return jsonify({"error": "Too many attempts. Wait a while "
                                 "and try again."}), 429
    if not answer:
        return jsonify({"error": "That password isn't right."}), 401
    proof = hmac.new(stored.encode(), token.encode(), hashlib.sha256).hexdigest()
    response = jsonify({"ok": True})
    response.set_cookie(_share_unlock_cookie(token), proof, httponly=True,
                        samesite="Lax", secure=request.is_secure,
                        max_age=12 * 3600, path="/")
    return response


@bp.get("/api/share/<token>")
def view_shared(token: str):
    share = _share_or_404(token)
    conn = _conn()

    if share.get("password") and not _share_unlocked(share, token):
        return jsonify({
            "password_required": True,
            "token": token,
            "scope": share["scope"],
        }), 401

    db.increment_share_views(conn, token)
    assets = _share_assets(share, conn)
    if not assets:
        abort(404, description="Nothing here any more")

    def public(asset: dict[str, Any]) -> dict[str, Any]:
        # What a stranger is shown, named field by field. The gallery's own
        # payload carried the household's folder names, each photograph's
        # visibility and why, its tags, caption, city and country — none of it
        # on the share page, all of it readable by whoever held the link.
        full = _public(asset)
        item = {key: full.get(key) for key in SHARED_FIELDS}
        # The media URLs must carry the token, because the recipient is not
        # signed in and `/api/file/<id>` quite rightly refuses them. Serving
        # the *metadata* and then 404ing every photograph in it was the second
        # half of why this feature did not work.
        item["src"] = f"/api/share/{token}/file/{asset['id']}"
        item["thumb"] = f"/api/share/{token}/thumb/{asset['id']}"
        item["view"] = (f"/api/share/{token}/preview/{asset['id']}"
                        if stills.needs_rendition(asset["ext"], asset["kind"])
                        else item["src"])
        return item

    if share["scope"] == "album":
        album = conn.execute("SELECT name FROM albums WHERE id=?",
                             (share["target_id"],)).fetchone()
        return jsonify({
            "scope": "album",
            # The name, which the page shows — not who made it or when.
            "album": {"name": album["name"] if album else "Shared photographs"},
            "items": [public(a) for a in assets],
            "total": len(assets),
        })
    return jsonify({"scope": "asset", "item": public(assets[0])})


@bp.get("/api/share/<token>/thumb/<int:asset_id>")
def shared_thumb(token: str, asset_id: int):
    cfg = _cfg()
    _, row = _share_asset_or_404(token, asset_id)
    if not row.get("thumb"):
        abort(404)
    size = _int_arg("s", cfg.thumb_sizes[0])
    size = min(cfg.thumb_sizes, key=lambda candidate: abs(candidate - size))
    path = cfg.thumbs_dir / media.thumb_file(row["thumb"], size, cfg.thumb_format)
    if not path.is_file():
        abort(404)
    thumb_mime = "image/webp" if cfg.thumb_format.upper() == "WEBP" else "image/jpeg"
    response = send_file(path, conditional=True, mimetype=thumb_mime, max_age=3600)
    response.headers["Cache-Control"] = "private, max-age=3600"
    return response


@bp.get("/api/share/<token>/file/<int:asset_id>")
def shared_file(token: str, asset_id: int):
    _, row = _share_asset_or_404(token, asset_id)
    path = _asset_file(row)
    if _location_may_ride_along(row) or _turned_in_index(row):
        # Whoever holds the link is somebody Ninaivu has never met, and the
        # original's EXIF says where it was taken — for a photograph shot at
        # home, where the family lives. They get the picture, not that.
        # The share page does not turn pictures itself, so the turn the
        # index holds is baked in.
        return _viewing_copy(row, path, max_age=3600, turned=True)
    if row.get("kind") == "video":
        # A video carries the place it was shot as a photograph does.
        stripped = _stripped_video(row, path)
        if stripped is not None:
            return stripped
    mime, _ = mimetypes.guess_type(path.name)
    inline = mime in INLINE_TYPES
    response = send_file(
        path, conditional=True,
        mimetype=mime if inline else "application/octet-stream",
        max_age=3600, as_attachment=not inline,
        download_name=None if inline else row["filename"])
    response.headers["Accept-Ranges"] = "bytes"
    response.headers["Cache-Control"] = "private, max-age=3600"
    return response


@bp.get("/api/share/<token>/preview/<int:asset_id>")
def shared_preview(token: str, asset_id: int):
    _, row = _share_asset_or_404(token, asset_id)
    if row.get("kind") != "picture":
        abort(404)
    path = _asset_file(row)
    if not stills.needs_rendition(row["ext"], "picture") and not _turned_in_index(row):
        return shared_file(token, asset_id)
    return _viewing_copy(row, path, max_age=3600, turned=True)


def _turned_in_index(row: dict[str, Any]) -> bool:
    """A photograph the index turns (a sideways scan put right, or a turn by
    hand), which a copy for a visitor has to carry baked in."""
    return row.get("kind") == "picture" and int(row.get("rotation") or 0) % 360 != 0
