"""Calendar-date access policy shared by SQL listings and individual assets."""
from datetime import date
import json

from flask import current_app, g, has_request_context

#: A new library starts with family members and guests seeing what is dated on
#: or after the cutoff, and administrators seeing every date (the household's
#: decision of 11 November 2026; docs/date-access.md). Undated media is then
#: with administrators only. Administrators are never limited by default: one
#: limited in the family app could not find, or release, media they are
#: responsible for.
#:
#: A library that was already in use when this became the default keeps what
#: it had, nobody limited (:data:`OPEN`): :func:`settle` writes it down the
#: first time, so an update never hides most of a library without a word.
DEFAULTS = {"cutoff": "2014-01-01", "admin": "all", "family": "after",
            "guest": "after"}
#: What a library had before the default changed: nobody limited by date.
OPEN = {**DEFAULTS, "family": "all", "guest": "all"}
KEY = "family_date_policy"


def settle(conn, *, new: bool) -> None:
    """Write the policy down once, so a later change of the defaults never
    changes who sees what: a *new* library gets the defaults, one already in
    use keeps nobody limited. A policy an administrator saved is left alone."""
    from ..storage import db
    if db.get_meta(conn, KEY, "") == "":
        db.set_meta(conn, KEY, json.dumps(DEFAULTS if new else OPEN))


def read(conn):
    from ..storage import db
    return {**DEFAULTS, **json.loads(db.get_meta(conn, KEY, "{}"))}


def validate(data):
    if not isinstance(data, dict) or set(data) != set(DEFAULTS):
        raise ValueError("Provide cutoff, admin, family and guest date ranges.")
    if not isinstance(data["cutoff"], str) or date.fromisoformat(data["cutoff"]).isoformat() != data["cutoff"]:
        raise ValueError("Use a cutoff date in YYYY-MM-DD format.")
    if any(data[role] not in ("before", "after", "all") for role in ("admin", "family", "guest")):
        raise ValueError("Choose before, after or all for each role.")
    return dict(data)


def current():
    if not has_request_context():
        return None
    preview_role = getattr(g, "date_preview_role", None)
    if current_app.config.get("NINAIVU_FACE") != "home" and preview_role is None:
        return None
    if not hasattr(g, "family_date_policy"):
        from ..storage import db
        g.family_date_policy = read(db.connect(current_app.config["MV_CONFIG"].db_path))
    from .auth import current_user
    policy = g.family_date_policy
    role = preview_role or current_user().role
    return policy["cutoff"], policy[role], role


def _undated_visible(policy) -> bool:
    """Undated media stays with administrators whatever their date range is.

    A file with no date cannot be placed either side of a cutoff, so for family
    and guests it is left out; an administrator still has to be able to reach
    it — to date it, hide it, or delete it.
    """
    from .auth import ROLE_ADMIN
    return policy[2] == ROLE_ADMIN


def for_role(conn, role):
    """The policy as :func:`current` gives it, for *role* rather than for
    whoever is asking: for what is shown with nobody signed in to ask (the TV
    album, server/tv_album.py)."""
    policy = read(conn)
    return policy["cutoff"], policy[role], role


def sql(alias, policy=None):
    """The date limit as SQL; *policy* defaults to the caller's own."""
    policy = current() if policy is None else policy
    if policy is None or policy[1] == "all":
        return "1=1"
    # Both tokens are validated on write; date.fromisoformat also defends a
    # database edited outside the application. Missing dates fail closed.
    cutoff = date.fromisoformat(policy[0]).isoformat()
    column = f"{alias}.date_key" if alias else "date_key"
    operator = "<" if policy[1] == "before" else ">="
    if _undated_visible(policy):
        return f"({column} = '' OR {column} {operator} '{cutoff}')"
    return f"({column} != '' AND {column} {operator} '{cutoff}')"


def restricted():
    policy = current()
    return policy is not None and policy[1] != "all"


def allows(asset, policy=None):
    """Whether *asset* is inside the date limit; *policy* defaults to the
    caller's own."""
    policy = current() if policy is None else policy
    if policy is None or policy[1] == "all":
        return True
    key = asset.get("date_key") or ""
    if not key:
        return _undated_visible(policy)
    return key < policy[0] if policy[1] == "before" else key >= policy[0]
