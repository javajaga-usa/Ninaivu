"""Two rules that narrow what an administrator is able to do, on purpose.

**Nothing may rewrite the whole library.** A folder rule is a decision about a
folder; there is no decision anybody makes about *every photograph in the
house* that is worth having a button for. The control existed, it took a
password and warned twice, and it was still the one place where a slip cost
everything — so it is gone rather than guarded. Folders and individual items
are unaffected; only the library root is refused.

**Deleting asks for the password, and asks nothing else.** There was once a
second rule in front of it — a photograph had to be hidden before it could be
deleted — meant as a pause for thought. It was two chores to do one thing, and
the pause it was reaching for is already there: deleting moves the file into
the library's recycle bin, under its own name, and the console puts it back.

**Erasing from the bin asks again.** That one is final, so the password is
asked at the point the file actually stops existing, not only at the point it
was moved.
"""

import pytest

from conftest import ADMIN, ids_of, login
from ninaivu.storage import recycle


# --- the whole library is not a thing you can set -------------------------

@pytest.mark.parametrize("level", ["public", "family", "hidden"])
def test_the_whole_library_cannot_be_changed_at_all(as_admin, level):
    """In every direction, with or without a password. There is no argument
    that gets through — the route does not do this any more."""
    response = as_admin.post("/api/visibility/folder",
                             json={"folder": "", "visibility": level,
                                   "password": ADMIN[1], "confirm": True})
    assert response.status_code == 403, response.get_json()
    assert "whole library" in response.get_json()["error"].lower()


def test_the_refusal_explains_what_to_do_instead(as_admin):
    body = as_admin.post("/api/visibility/folder",
                         json={"folder": "", "visibility": "hidden"}).get_json()
    assert "folder" in body["error"].lower()


@pytest.mark.parametrize("sneaky", [None, "/", ".", "  ", "./", "\\"])
def test_a_folder_that_is_really_the_root_is_refused_too(as_admin, sneaky):
    """Every spelling of "nothing" has to land in the same place, or the rule
    is one URL away from being ignored."""
    response = as_admin.post("/api/visibility/folder",
                             json={"folder": sneaky, "visibility": "hidden"})
    assert response.status_code == 403, (sneaky, response.get_json())


def test_a_named_folder_still_works(as_admin, scanned):
    """The rule is about the root, not about folder rules in general."""
    response = as_admin.post("/api/visibility/folder",
                             json={"folder": "private", "visibility": "hidden"})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["updated"] >= 1


def test_single_items_still_work(as_admin):
    ids = ids_of(as_admin)[:2]
    response = as_admin.post("/api/visibility",
                             json={"ids": ids, "visibility": "hidden"})
    assert response.status_code == 200
    assert response.get_json()["updated"] == len(ids)


def test_the_refusal_is_written_down(as_admin, scanned):
    _, conn, _ = scanned
    as_admin.post("/api/visibility/folder",
                  json={"folder": "", "visibility": "public"})
    entries = [dict(r) for r in conn.execute(
        "SELECT action, detail FROM audit ORDER BY id DESC LIMIT 5")]
    assert any("whole" in (e["detail"] or "").lower() for e in entries), entries


# --- deleting takes a password, and takes nothing else ---------------------

def hide(client, ids):
    return client.post("/api/visibility",
                       json={"ids": list(ids), "visibility": "hidden"})


def test_a_visible_photograph_can_be_deleted(as_admin):
    """Straight to it. Nothing has to be hidden first."""
    target = ids_of(as_admin)[0]
    response = as_admin.post("/api/delete",
                             json={"ids": [target], "password": ADMIN[1]})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["deleted"] == 1


def test_a_hidden_photograph_can_be_deleted(as_admin):
    target = ids_of(as_admin)[0]
    assert hide(as_admin, [target]).status_code == 200
    response = as_admin.post("/api/delete",
                             json={"ids": [target], "password": ADMIN[1]})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["deleted"] == 1


def test_a_mixed_batch_goes_in_one_piece(as_admin):
    """Some hidden, some not: one password, one outcome, nothing left behind
    for the person to discover later."""
    ids = ids_of(as_admin)[:4]
    hide(as_admin, ids[:2])
    response = as_admin.post("/api/delete",
                             json={"ids": ids, "password": ADMIN[1]})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["deleted"] == len(ids)


def test_the_password_is_required(as_admin):
    target = ids_of(as_admin)[0]
    response = as_admin.post("/api/delete", json={"ids": [target]})
    assert response.status_code == 401
    assert response.get_json()["needs_password"] is True


def test_a_wrong_password_removes_nothing(as_admin, scanned):
    """The one thing that must never be true: a refusal that deleted anyway."""
    from pathlib import Path

    cfg, conn, _ = scanned
    target = ids_of(as_admin)[0]
    before = {str(p) for p in Path(cfg.active_root).rglob("*") if p.is_file()}
    rows = conn.execute("SELECT COUNT(*) n FROM assets").fetchone()["n"]

    response = as_admin.post("/api/delete",
                             json={"ids": [target], "password": "nope"})

    assert response.status_code == 401
    after = {str(p) for p in Path(cfg.active_root).rglob("*") if p.is_file()}
    assert after == before
    assert conn.execute("SELECT COUNT(*) n FROM assets").fetchone()["n"] == rows


def test_deleting_a_hidden_folder_still_works(as_admin, scanned):
    """The ordinary way somebody clears out a folder: hide it, look at what
    that hid, then delete. Still the sensible order — just no longer forced."""
    as_admin.post("/api/visibility/folder",
                  json={"folder": "private", "visibility": "hidden"})
    listing = as_admin.get("/api/assets?limit=200&visibility=hidden").get_json()
    ids = [i["id"] for i in listing["items"]][:2]
    assert ids, "hiding the folder did not hide anything"
    response = as_admin.post("/api/delete",
                             json={"ids": ids, "password": ADMIN[1]})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["deleted"] == len(ids)


# --- erasing from the bin, which is the part that cannot be taken back -----

def delete_one(client):
    """Delete a photograph and return its entry in the recycle bin."""
    target = ids_of(client)[0]
    assert client.post("/api/delete",
                       json={"ids": [target], "password": ADMIN[1]}).status_code == 200
    items = client.get("/api/recycle").get_json()["items"]
    assert items, "the deleted file is not in the bin"
    return items[0]


def test_erasing_asks_for_the_password(as_admin):
    from pathlib import Path

    entry = delete_one(as_admin)
    response = as_admin.post("/api/recycle/purge", json={"ids": [entry["id"]]})
    assert response.status_code == 401
    assert response.get_json()["needs_password"] is True
    assert Path(entry["bin_path"]).exists(), "a refusal erased the file anyway"


def test_a_wrong_password_erases_nothing(as_admin):
    from pathlib import Path

    entry = delete_one(as_admin)
    response = as_admin.post("/api/recycle/purge",
                             json={"ids": [entry["id"]], "password": "nope"})
    assert response.status_code == 401
    assert Path(entry["bin_path"]).exists()


def test_erasing_removes_the_file_and_the_entry(as_admin):
    from pathlib import Path

    entry = delete_one(as_admin)
    response = as_admin.post("/api/recycle/purge",
                             json={"ids": [entry["id"]], "password": ADMIN[1]})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["purged"] == 1
    assert not Path(entry["bin_path"]).exists()
    # The bin lists what is in it, and this is not in it.
    remaining = [row["id"] for row in as_admin.get("/api/recycle").get_json()["items"]]
    assert entry["id"] not in remaining


def test_erasing_cannot_reach_outside_the_bin(as_admin, scanned):
    """A row whose path points back into the library must be refused, however
    it came to say that. This is the guard that keeps a mangled bin_path from
    erasing a photograph that was never deleted."""
    from pathlib import Path

    cfg, conn, _ = scanned
    entry = delete_one(as_admin)
    victim = next(p for p in Path(cfg.active_root).rglob("*")
                  if p.is_file() and recycle.BIN_NAME not in p.parts)
    conn.execute("UPDATE recycled SET bin_path=? WHERE id=?",
                 (str(victim), entry["id"]))
    conn.commit()

    response = as_admin.post("/api/recycle/purge",
                             json={"ids": [entry["id"]], "password": ADMIN[1]})

    assert response.status_code == 200
    body = response.get_json()
    assert body["purged"] == 0
    assert body["failed"], "the refusal was silent"
    assert victim.exists(), "a photograph outside the bin was erased"


# --- hiding from the gallery, which is still how you take one out of sight -

def test_the_gallery_can_hide_items(app, people):
    """The family app's own "Show to" buttons post here.

    They were pointed at a console-only route and had never worked — which was
    survivable while hiding was a convenience, and is not now that it is the
    step in front of deleting.
    """
    client = login(app.test_client(), *ADMIN)
    ids = ids_of(client)[:2]
    response = client.post("/api/visibility",
                           json={"ids": ids, "visibility": "hidden"})
    assert response.status_code == 200, response.status_code
    assert response.get_json()["updated"] == len(ids)


def test_hiding_from_the_gallery_is_still_admin_only(app, people):
    from conftest import FAMILY
    client = login(app.test_client(), *FAMILY)
    assert client.post("/api/visibility",
                       json={"ids": [1], "visibility": "hidden"}
                       ).status_code in (401, 403)


# --- and a bin that can be told to empty itself ---------------------------

def test_nothing_expires_by_default(as_admin, scanned):
    """A bin that quietly erases things is not a bin."""
    from ninaivu.storage import recycle

    _, conn, _ = scanned
    delete_one(as_admin)
    assert recycle.expired(conn, 0) == []
    assert recycle.sweep(conn, 0)["purged"] == 0
    assert conn.execute(
        "SELECT COUNT(*) n FROM recycled WHERE restored_at IS NULL"
    ).fetchone()["n"] == 1


def test_what_is_older_than_the_policy_goes(as_admin, scanned):
    from pathlib import Path

    from ninaivu.storage import recycle

    _, conn, _ = scanned
    entry = delete_one(as_admin)
    conn.execute("UPDATE recycled SET deleted_at=deleted_at - ? WHERE id=?",
                 (40 * 86400, entry["id"]))
    conn.commit()

    result = recycle.sweep(conn, days=30)

    assert result["purged"] == 1
    assert not Path(entry["bin_path"]).exists()
    assert recycle.count(conn)["items"] == 0


def test_what_is_younger_stays(as_admin, scanned):
    from pathlib import Path

    from ninaivu.storage import recycle

    _, conn, _ = scanned
    entry = delete_one(as_admin)
    conn.execute("UPDATE recycled SET deleted_at=deleted_at - ? WHERE id=?",
                 (5 * 86400, entry["id"]))
    conn.commit()

    assert recycle.sweep(conn, days=30)["purged"] == 0
    assert Path(entry["bin_path"]).exists()


def test_the_bin_reports_its_size(as_admin):
    """A number nobody can see is not a limit anybody will act on."""
    delete_one(as_admin)
    body = as_admin.get("/api/recycle").get_json()
    assert body["summary"]["items"] == 1
    assert body["summary"]["bytes"] > 0
    assert "erase_after_days" in body
