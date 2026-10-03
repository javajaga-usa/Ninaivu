"""Set a new password for someone, from the computer Ninaivu runs on.

    python -m ninaivu reset-password NAME

For the household whose only administrator has forgotten the password: the
console cannot help, because only an administrator can reset a password
there, and ``--admin`` only works while there is no administrator at all.
Whoever can run a command on this computer can already read the index, so
asking them for nothing more than the new password gives nothing away.

The person is signed out everywhere and their profile is turned back on, so
an administrator who was switched off by mistake is back as well. Taken from
Ninaivu Lite's ``--reset-password``.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Callable


def main(argv: list[str] | None = None, *,
         ask: Callable[[str], str] = getpass.getpass) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ninaivu reset-password",
        description="Set a new password for someone (a forgotten administrator "
                    "password, say), then stop.")
    parser.add_argument("name", help="the person's username")
    args = parser.parse_args(argv)

    from ..server import auth                                     # noqa: PLC0415
    from ..server.config import Config                            # noqa: PLC0415
    from ..storage import db                                      # noqa: PLC0415

    cfg = Config.load()
    conn = db.init_db(cfg.db_path)
    auth.init_auth_schema(conn)
    user = auth.get_user_by_name(conn, args.name)
    if user is None:
        names = ", ".join(u.username for u in auth.list_users(conn))
        print(f"  No one is called {args.name!r}. People: {names or '(none yet)'}",
              file=sys.stderr)
        return 2
    first = ask("  New password: ")
    if first != ask("  The same again: "):
        print("  The two passwords do not match.", file=sys.stderr)
        return 2
    try:
        auth.set_password(conn, user.id, first)
    except ValueError as exc:
        print(f"  {exc}", file=sys.stderr)
        return 2
    # set_active(True) leaves sessions alone; a reset ends the old ones, since
    # whoever knew the old password may be the reason for the reset.
    auth.set_active(conn, user.id, True)
    auth.end_all_sessions(conn, user.id)
    auth.audit(conn, user.id, "reset_password_cli", user.username)
    print(f"  Password changed for {user.username}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
