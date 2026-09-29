"""Calendar-date access policy shared by SQL listings and individual assets."""
from datetime import date
import json

from flask import current_app, g, has_request_context

#: Nobody is limited by date until the administrator says so (People &
#: access → Visibility). The limit is there for a household that wants one —
#: guests see only this decade, say — not a default: Hearth shipped with
#: family members and guests seeing nothing before 2014 and nothing undated,
#: which on a stranger's library hid most of it without a word. The cutoff
#: below is only where the date field starts when somebody does set a limit.
#: Administrators are never limited by default: one limited in the family app
#: could not find, or release, media they are responsible for.
DEFAULTS = {"cutoff": "2014-01-01", "admin": "all", "family": "all",
            "guest": "all"}
KEY = "family_date_policy"


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


def sql(alias):
    policy = current()
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


def allows(asset):
    policy = current()
    if policy is None or policy[1] == "all":
        return True
    key = asset.get("date_key") or ""
    if not key:
        return _undated_visible(policy)
    return key < policy[0] if policy[1] == "before" else key >= policy[0]
