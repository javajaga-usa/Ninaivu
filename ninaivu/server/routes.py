"""The route manifest: every route, who may call it, and whether it changes anything.

"Every photograph has a visibility" is the product, and the about 510 routes
are where it is kept or lost. Until 4.0 each route said who could call it in
its own way — ``require_admin`` or ``require_family`` above it, a check
inside it, a guard on its blueprint — and the only way to know the whole was
to read every one. This module puts the whole in one place:

* A route behind :func:`auth.require` declares itself: that decorator marks
  the view with its role (``ninaivu_access``).
* Every other route is declared here, in :data:`OPEN` or :data:`BLUEPRINTS`,
  with what stands in for a sign-in and why, or by an extension with
  :func:`declare`. A core route declared nowhere is a mistake, and
  :func:`check` refuses to start an app that has one; an extension's is kept
  to the administrator instead (:func:`keep_to_the_administrator`).
* :func:`manifest` lists them all; :func:`render` prints that list, which is
  kept in the repository (``tests/data/route_manifest.txt``). A change that
  opens a route, or lets a family member call what only the administrator
  could, shows up in a pull request as a changed line there.

Who may call a route (``access``):

``admin`` / ``family``
    Signed in with at least that role (``require_admin``, ``require_family``).
``guest``
    No sign-in needed; the route answers with what the caller may see, guest
    included (the gallery's reads), or checks the caller itself before a change.
``anyone``
    Not about the library at all: signing in, the app's own files, health.
``link``
    A share or "who is this?" link: the token in the address is the permission,
    and it shares only what its maker could see.
``peer``
    The other computer of a pair: every request signed with the pairing key.
``machine``
    A program on this computer, with the token from the run file only it can read.
``phone``
    A phone's sync app, with a phone key on every request.

Changes (``writes``) are every method but GET, HEAD, OPTIONS and PROPFIND.
Every write is refused unless it comes from a page Ninaivu served
(``__init__._refuse_cross_origin_writes``), except the phone inbox, which a
sync app reaches with its phone key and no cookie.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

ACCESS = ("admin", "family", "guest", "anyone", "link", "peer", "machine", "phone")

#: Methods that do not change anything.
READS = frozenset({"GET", "HEAD", "OPTIONS", "PROPFIND"})

#: Why a gallery read may be called by anybody: it answers only with what
#: the caller may see, decided by the visibility gate.
GATE = "what the caller may see, decided by server/visibility.Viewer"

#: Routes with no ``require`` above them, by endpoint: who may call them and
#: what stands in for the sign-in.
OPEN: dict[str, tuple[str, str]] = {
    # Signing in and out, and the signed-in person's own account.
    "accounts.auth_state": ("anyone", "whether anybody is set up yet"),
    "accounts.setup": ("anyone", "makes the first administrator; refused once one exists"),
    "accounts.login": ("anyone", "signing in"),
    "accounts.logout": ("anyone", "signing out"),
    "accounts.session_state": ("anyone", "who is signed in, if anybody"),
    "accounts.forgot": ("anyone", "marks the person for an administrator; the same answer for any name"),
    "accounts.session_lock": ("guest", "locks the caller's own session"),
    "accounts.session_unlock": ("guest", "the caller's own password or PIN"),
    "accounts.session_active": ("guest", "the caller's own session is still in use"),
    "accounts.avatar": ("guest", "a profile picture, for the profile picker"),
    "accounts.me": ("guest", "the caller's own account; a guest is told so"),
    "accounts.update_me": ("guest", "the caller's own account; refused when nobody is signed in"),
    "accounts.upload_avatar": ("guest", "the caller's own account; refused when nobody is signed in"),
    "accounts.delete_avatar": ("guest", "the caller's own account; refused when nobody is signed in"),
    "accounts.change_password": ("guest", "the caller's own password, with the old one"),
    "home_accounts.profiles": ("anyone", "the profile picker; an administrator's tile still asks for the password"),
    "home_accounts.enter": ("anyone", "tap a face, with its PIN if it has one"),
    # The app's own files, and asking whether it is up.
    "static": ("anyone", "the app's scripts, styles and icons"),
    "api.index": ("anyone", "the page the app runs in"),
    "api.manifest": ("anyone", "the web app manifest"),
    "api.service_worker": ("anyone", "the service worker"),
    "api.healthz": ("anyone", "whether the server answers"),
    "api.readyz": ("anyone", "whether the server is ready"),
    "api.certificate_authority": ("anyone", "this computer's certificate authority, public by nature"),
    # The gallery's reads. Those marked GATE ask the one visibility gate
    # (server/visibility.Viewer, proposal 6); the rest still apply the
    # caller's visibility themselves until they are moved to it.
    "api.assets": ("guest", "the route applies the caller's visibility itself"),
    "api.asset_detail": ("guest", GATE),
    "api.asset_stories": ("guest", GATE),
    "api.facets": ("guest", "the route applies the caller's visibility itself"),
    "api.segments": ("guest", "the route applies the caller's visibility itself"),
    "api.timeline": ("guest", "the route applies the caller's visibility itself"),
    "api.suggest": ("guest", "the route applies the caller's visibility itself"),
    "api.similar": ("guest", "the route applies the caller's visibility itself"),
    "api.occasions": ("guest", "the route applies the caller's visibility itself"),
    "api.memories_on_this_day": ("guest", "the route applies the caller's visibility itself"),
    "api.thumb": ("guest", GATE),
    "api.still_preview": ("guest", GATE),
    "api.original": ("guest", GATE),
    "api.live_video": ("guest", GATE),
    "api.story_audio": ("guest", GATE),
    "api.status": ("guest", "what the server is doing; more for an administrator"),
    "api.activity": ("guest", "what the server is doing; more for an administrator"),
    "api.scan_progress": ("guest", "what the server is doing; more for an administrator"),
    "api.status_stats": ("guest", "the route applies the caller's visibility itself"),
    "api.frame_next": ("guest", "the route applies the caller's visibility itself"),
    "api.frame_picture": ("guest", GATE),
    "api.festivals_list": ("guest", "the route applies the caller's visibility itself"),
    "api.festivals_one": ("guest", "the route applies the caller's visibility itself"),
    "api.moments_search": ("guest", "the route applies the caller's visibility itself"),
    "api.moments_of_clip": ("guest", GATE),
    "api.moment_frame": ("guest", GATE),
    "api.our_year_list": ("guest", "the route applies the caller's visibility itself"),
    "api.our_year_one": ("guest", "the route applies the caller's visibility itself"),
    "api.our_year_words": ("guest", "refused for a guest; written from what the caller may see"),
    "api.then_and_now_all": ("guest", "the route applies the caller's visibility itself"),
    "api.then_and_now_today": ("guest", "the route applies the caller's visibility itself"),
    "api.stack": ("guest", GATE),
    "api.stack_top": ("guest", "refused unless the caller may change it (_may_change)"),
    "api.stack_apart": ("guest", "refused unless the caller may change it (_may_change)"),
    # Links: the token is the permission.
    "api.shared_page": ("link", "a share link"),
    "api.view_shared": ("link", "a share link"),
    "api.unlock_shared": ("link", "a share link's password"),
    "api.shared_thumb": ("link", "only what the link shares"),
    "api.shared_preview": ("link", "only what the link shares"),
    "api.shared_file": ("link", "only what the link shares, when downloads are allowed"),
    "api.shared_stories": ("link", "only for what the link shares"),
    "api.shared_story_audio": ("link", "only for what the link shares"),
    "api.ask_page": ("link", "a \"who is this?\" link"),
    "api.ask_view": ("link", "a \"who is this?\" link"),
    "api.ask_answer": ("link", "the one question the link asks"),
    "api.ask_face": ("link", "the faces the link asks about"),
    "api.ask_photo": ("link", "the photographs the link asks about"),
    # The phone inbox.
    "api.webdav": ("phone", "a phone key on every request"),
    # A program on this computer.
    "admin.shutdown": ("machine", "the run file's token; 404 without it"),
}

#: Blueprints whose every route is guarded by the blueprint itself.
BLUEPRINTS: dict[str, tuple[str, str]] = {
    "mugil_peer": ("peer", "signed with the pairing key (mugil_api._peer_only)"),
    "mugil_update_peer": ("peer", "signed with the pairing key, house network only"),
}


def declare(access: str, why: str):
    """Say who may call a view that is not behind ``require_admin`` or
    ``require_family``: for an extension's routes, which cannot be listed in
    :data:`OPEN`. *access* is one of :data:`ACCESS`; *why* is what stands in
    for the sign-in, in a few words."""
    if access not in ACCESS:
        raise ValueError(f"access must be one of {ACCESS}: {access!r}")

    def mark(view: Any) -> Any:
        view.ninaivu_access = access
        view.ninaivu_why = why
        return view
    return mark


def keep_to_the_administrator(app: Any, endpoints: Iterable[str], who: str) -> list[str]:
    """Put ``require_admin`` in front of any of *endpoints* that declares nothing.

    For an extension: a route it adds without saying who may call it is kept
    to the administrator, and said in the log, rather than left open or
    stopping the household's server. Returns the endpoints it closed.
    """
    import logging                                                # noqa: PLC0415
    from . import auth                                            # noqa: PLC0415
    closed = []
    for endpoint in endpoints:
        view = app.view_functions.get(endpoint)
        if view is None or _declared(endpoint, view) is not None:
            continue
        app.view_functions[endpoint] = auth.require_admin(view)
        closed.append(endpoint)
    if closed:
        logging.getLogger(__name__).warning(
            "%s added routes that do not say who may call them; kept to the "
            "administrator: %s", who, ", ".join(sorted(closed)))
    return closed


@dataclass(frozen=True)
class Route:
    face: str
    rule: str
    methods: tuple[str, ...]
    endpoint: str
    access: str
    writes: bool
    outside_ai: bool
    why: str

    @property
    def cross_origin(self) -> str:
        """What stops a page elsewhere from making this change."""
        if not self.writes:
            return "-"
        return "phone key" if self.access == "phone" else "same origin"


class Undeclared(RuntimeError):
    """Routes that say nobody may call them, or anybody may, without saying which."""


def _declared(endpoint: str, view: Any) -> tuple[str, str] | None:
    marked = getattr(view, "ninaivu_access", None)
    if marked is not None:
        return marked, getattr(view, "ninaivu_why", "")
    if endpoint in OPEN:
        return OPEN[endpoint]
    blueprint = endpoint.split(".", 1)[0] if "." in endpoint else ""
    if blueprint in BLUEPRINTS:
        return BLUEPRINTS[blueprint]
    return None


def routes(app: Any, face: str) -> tuple[list[Route], list[str]]:
    """Every route of *app*, and the endpoints nothing declares."""
    out: list[Route] = []
    missing: list[str] = []
    for rule in app.url_map.iter_rules():
        view = app.view_functions.get(rule.endpoint)
        declared = _declared(rule.endpoint, view)
        if declared is None:
            missing.append(f"{face} {rule.rule} ({rule.endpoint})")
            continue
        access, why = declared
        methods = tuple(sorted((rule.methods or set()) - {"HEAD", "OPTIONS"}))
        out.append(Route(
            face=face, rule=rule.rule, methods=methods, endpoint=rule.endpoint,
            access=access, writes=bool(set(methods) - READS),
            outside_ai=bool(getattr(view, "ninaivu_outside_ai", False)), why=why))
    return out, missing


def check(app: Any, face: str) -> None:
    """Refuse an app with a route nobody declared (called as each app is made)."""
    _, missing = routes(app, face)
    if missing:
        raise Undeclared(
            "these routes do not say who may call them; add require_admin or "
            "require_family, or declare them in ninaivu/server/routes.py: "
            + ", ".join(sorted(missing)))


def manifest(apps: Iterable[tuple[str, Any]]) -> list[Route]:
    found: list[Route] = []
    for face, app in apps:
        found.extend(routes(app, face)[0])
    return sorted(found, key=lambda r: (r.rule, r.methods, r.face))


def render(found: Iterable[Route]) -> str:
    """The manifest as text, one route per line, for the repository's copy."""
    lines = ["# Generated by tools/route_manifest.py. Do not edit; see ninaivu/server/routes.py.",
             "# face | methods | rule | access | writes | cross-origin | endpoint"]
    for r in found:
        flags = " outside-ai" if r.outside_ai else ""
        lines.append(f"{r.face:5} | {','.join(r.methods):<20} | {r.rule} | {r.access}{flags} | "
                     f"{'writes' if r.writes else 'reads'} | {r.cross_origin} | {r.endpoint}")
    return "\n".join(lines) + "\n"


def summary(found: Iterable[Route]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in found:
        counts[r.access] = counts.get(r.access, 0) + 1
    return counts
