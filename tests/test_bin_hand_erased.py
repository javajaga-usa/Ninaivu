"""Files taken out of the recycle bin folder by hand left rows nobody could clear.

The bin listed them as "Moved or erased" with their boxes greyed out, so
neither "Choose all" nor "Erase for good" reached them.
"""

import shutil
from pathlib import Path

import pytest

from conftest import ADMIN, ids_of, login
from ninaivu.storage import recycle


@pytest.fixture()
def admin(app, people):
    client = login(app.test_client(), *ADMIN)
    client.environ_base["HTTP_SEC_FETCH_SITE"] = "same-origin"
    return client


def _bin_two(admin, conn):
    targets = ids_of(admin)[:2]
    response = admin.post("/api/delete", json={"ids": targets, "password": ADMIN[1]})
    assert response.status_code == 200, response.get_json()
    return [dict(r) for r in conn.execute(
        "SELECT * FROM recycled WHERE restored_at IS NULL ORDER BY id")]


def test_the_bin_drops_files_erased_by_hand(admin, people):
    conn = people["conn"]
    rows = _bin_two(admin, conn)
    assert len(rows) == 2
    Path(rows[0]["bin_path"]).unlink()

    listed = admin.get("/api/recycle").get_json()

    assert [item["id"] for item in listed["items"]] == [rows[1]["id"]]
    assert all(item["present"] for item in listed["items"])
    assert listed["summary"]["items"] == 1
    # Remembered as erased, so a restore from the second copy holds it back.
    assert conn.execute("SELECT 1 FROM erased WHERE root=? AND rel_path=?",
                        (rows[0]["root"], rows[0]["rel_path"])).fetchone()


def test_a_file_moved_back_by_hand_is_not_counted_as_erased(admin, people):
    conn = people["conn"]
    row = _bin_two(admin, conn)[0]
    shutil.move(row["bin_path"], Path(row["root"]) / row["rel_path"])

    assert recycle.forget_vanished(conn)["purged"] == 1
    assert not conn.execute("SELECT 1 FROM recycled WHERE id=?", (row["id"],)).fetchone()
    assert not conn.execute("SELECT 1 FROM erased WHERE root=? AND rel_path=?",
                            (row["root"], row["rel_path"])).fetchone()


def test_a_drive_that_is_away_keeps_its_entries(admin, people):
    conn = people["conn"]
    rows = _bin_two(admin, conn)
    # The whole _deleted folder is missing, as when the drive is unplugged.
    shutil.rmtree(recycle.bin_path(rows[0]["root"]))

    assert recycle.forget_vanished(conn)["purged"] == 0
    listed = admin.get("/api/recycle").get_json()
    assert len(listed["items"]) == 2
    assert not any(item["present"] for item in listed["items"])
