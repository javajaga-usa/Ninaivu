"""The visibility gate: what one caller may see, decided in one place.

"Every photograph has a visibility" is the product. Until 4.0 each of the
gallery's reads put that together for itself from the same pieces — the
caller's role, the library folders and sub-folder they were given, the date
policy, the kinds only an administrator hears, explicit content, the bin and
their own "Only to me" items — and the only way to know that two routes
agreed was to read both. A :class:`Viewer` holds every one of those pieces,
built once per request from :func:`auth.current_user`, the config and the
date policy (:func:`current`). It answers the two questions every read asks:

* :meth:`Viewer.sql` — the SQL a listing puts in its WHERE, with its
  parameters;
* :meth:`Viewer.may_see` — whether one item fetched by id may be served.

The rules are the ones the routes applied before (``db.visibility_clause``,
``db.visible_to`` and ``api._may_see``), unchanged: this module moves where
they are decided, not what they decide.
"""
from __future__ import annotations

from typing import Any, Sequence

from flask import abort, current_app, g

from . import auth, date_policy

#: What :meth:`Viewer.sql` adds for the caller's own "Only to me" items.
#: ``""`` none of them (a listing), ``"also"`` them beside everything else
#: (asked for by id), ``"only"`` them and nothing else (the private view).
MINE = ("", "also", "only")


class Viewer:
    """Everything that limits what one caller sees. Never read from a request
    parameter: a guest cannot see more by changing the query string."""

    def __init__(self, *, viewer_id: int, role: str, roots: Sequence[str] | None,
                 scope: str | None, policy: tuple[str, str, str] | None) -> None:
        #: Who is asking; 0 for nobody signed in.
        self.viewer_id = int(viewer_id or 0)
        self.role = role
        self.is_admin = role == auth.ROLE_ADMIN
        #: The highest visibility level this caller sees.
        self.max_visibility = auth.max_visibility_for(role)
        #: The library folders they may query; None when no library has been
        #: set up at all (then reading them is a 409, as it always was).
        self._roots = None if roots is None else list(roots)
        #: The sub-folder they are kept to, inside those roots.
        self.scope = scope or None
        #: The date limit: ``(cutoff, range, role)`` or None for none.
        self.policy = policy
        # Imported here: storage imports this package's date policy.
        from ..storage import db                                    # noqa: PLC0415
        #: Kinds only an administrator sees, whatever an item's visibility.
        self.admin_only_kinds = () if self.max_visibility >= 2 else tuple(db.ADMIN_ONLY_KINDS)
        #: Flagged (explicit) content and the bin are an administrator's alone.
        self.nsfw_shield = self.max_visibility < 2
        self._shield: dict[str, Any] | None = None

    # -- the pieces ----------------------------------------------------------

    @property
    def roots(self) -> list[str]:
        """The library folders this caller may query (409 when there are none
        set up, as ``api._roots`` answers)."""
        if self._roots is None:
            abort(409, description="No library folder has been set up yet.")
        return list(self._roots)

    def limits(self) -> dict[str, Any]:
        """The limits as the storage functions that predate the gate take them."""
        return {"viewer_id": self.viewer_id, "max_visibility": self.max_visibility,
                "scope": self.scope}

    def search_shield(self) -> dict[str, Any]:
        """What a search by this caller leaves out beyond their own limits.

        Nothing for an administrator. For everybody else no screenshot or
        document, no picture of people in only their underwear
        (media/revealing.py), and nothing scored near the explicit line: on
        the scale SigLIP gives (sigmoid scores, ai.py) half the line still
        sits far above an ordinary photograph. CLIP's softmax scores sit close
        to its line on ordinary photographs, so no nearer line is drawn for it.
        """
        if self.is_admin:
            return {}
        if self._shield is None:
            from .. import ai as ai_mod                             # noqa: PLC0415
            from ..media import revealing                          # noqa: PLC0415
            cfg = current_app.config["MV_CONFIG"]
            engine = current_app.config.get("MV_ENGINE")
            near = bare = 0.0
            if getattr(cfg, "nsfw_filter", False):
                if getattr(engine, "sigmoid_scores", False):
                    near = ai_mod.explicit_line(cfg, engine) * NEAR_EXPLICIT_SHARE
                if revealing.applies(str(getattr(engine, "model_id", "") or "")):
                    bare = revealing.line(cfg)
            self._shield = {"search_shield": True, "near_explicit": near, "revealing": bare}
        return dict(self._shield)

    # -- a listing -------------------------------------------------------------

    def _dates(self, alias: str) -> str:
        return date_policy.sql(alias, self.policy) if self.policy is not None else "1=1"

    def sql(self, alias: str = "a", *, mine: str = "") -> tuple[str, list[Any]]:
        """``(clause, params)``: the rows of ``assets`` (as *alias*, or
        unqualified for ``""``) this caller may see — their folders, the
        visibility ceiling, the date limit, the administrator-only kinds and
        explicit content, and their sub-folder. *mine* is one of :data:`MINE`.
        The bin and a pair's other half are each listing's own to leave out.
        """
        if mine not in MINE:
            raise ValueError(f"mine must be one of {MINE}: {mine!r}")
        from ..storage import db                                    # noqa: PLC0415
        dot = f"{alias}." if alias else ""
        roots = self.roots
        if not roots:
            # No roots at all means no rows — never "everything".
            parts, params = ["0 = 1"], []
        elif len(roots) == 1:
            parts, params = [f"{dot}root = ?"], [roots[0]]
        else:
            parts, params = [f"{dot}root IN ({','.join('?' * len(roots))})"], list(roots)

        guard = db.kind_guard(alias, self.max_visibility)
        visible = f"{dot}visibility <= ? AND {self._dates(alias)}"
        visible = f"{visible} AND {guard}" if guard else visible
        own = f"{dot}private_to = ? AND {self._dates(alias)}"
        own = f"{own} AND {guard}" if guard else own
        if mine == "only":
            if self.viewer_id:
                parts.append(own)
                params.append(self.viewer_id)
            else:
                parts.append("0=1")
        elif mine == "also" and self.viewer_id and self.max_visibility < 2:
            parts.append(f"(({visible}) OR ({own}))")
            params += [self.max_visibility, self.viewer_id]
        else:
            parts.append(visible)
            params.append(self.max_visibility)

        if self.scope:
            scope_sql, scope_params = db.folder_clause(f"{dot}folder", self.scope)
            parts.append(scope_sql)
            params += scope_params
        return " AND ".join(parts), params

    # -- one item --------------------------------------------------------------

    def may_see(self, asset: dict[str, Any] | None) -> bool:
        """Whether this caller may be served *asset* (a row fetched by id)."""
        if not asset:
            return False
        if self.policy is not None and not date_policy.allows(asset, self.policy):
            return False
        owner = asset.get("private_to")
        if self.viewer_id and owner is not None and int(owner) == self.viewer_id:
            ceiling = max(self.max_visibility, 2)       # their own "Only to me" item
        else:
            ceiling = self.max_visibility
        if int(asset.get("visibility", 1)) > ceiling:
            return False
        if str(asset.get("kind") or "") in self.admin_only_kinds:
            return False
        if self.nsfw_shield and asset.get("nsfw"):
            return False
        if self.scope:
            folder = asset.get("folder") or ""
            if folder != self.scope and not folder.startswith(f"{self.scope}/"):
                return False
        # …and it must live in a library folder they were given.
        if asset.get("root") not in self.roots:
            return False
        # Trash is not a display hint: every listing withholds it, so fetching
        # one by id answers the same way.
        if asset.get("trashed") and not self.is_admin:
            return False
        return True


#: A family member's search leaves out what the explicit check scored at this
#: share of its line or over (storage/db.py, search_shield_clause).
NEAR_EXPLICIT_SHARE = 0.5


def build() -> Viewer:
    """The Viewer for whoever is asking in this request."""
    cfg = current_app.config["MV_CONFIG"]
    user = auth.current_user()
    libraries = cfg.libraries or ([cfg.active_root] if cfg.active_root else [])
    roots, sub = auth.resolve_library(user.assigned_library, libraries)
    return Viewer(viewer_id=user.id, role=user.role,
                  roots=roots if libraries else None,
                  # An explicit assignment wins; the legacy scope column is the fallback.
                  scope=sub or (user.effective_scope or None),
                  policy=date_policy.current())


def current() -> Viewer:
    """The Viewer for this request, built the first time it is asked for.

    Built again if who is asking has changed since (a sign-in within the
    request, the console's preview of another role's dates).
    """
    key = (id(auth.current_user()), getattr(g, "date_preview_role", None))
    held = g.get("ninaivu_viewer")
    if held is None or held[0] != key:
        held = (key, build())
        g.ninaivu_viewer = held
    return held[1]


__all__ = ["MINE", "Viewer", "build", "current"]
