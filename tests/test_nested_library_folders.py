"""A library folder inside another library folder.

A household had "NinaivuMaster" and "NinaivuMaster/Ninaivu Archive" both
listed: every photograph in the Archive was indexed twice, once under each
folder, and names, favourites, albums and voice stories landed on whichever
row somebody opened. Removing either folder deleted its rows and everything
on them. Now removing one folds what it shares with the other into the
other, adding a folder inside a library folder is refused, and adding a
folder around one takes it in.
"""

import json
import time
from pathlib import Path

import pytest
from PIL import Image

from conftest import ADMIN, login
from ninaivu import build_services, create_admin_app
from ninaivu.media.scanner import Scanner
from ninaivu.server import auth
from ninaivu.storage import nested_roots


def photo(path: Path, shade: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (220, 160), (shade, 90, 40)).save(path)


@pytest.fixture()
def nested(tmp_path):
    from ninaivu.server.config import Config
    from ninaivu.storage import db

    master = tmp_path / "NinaivuMaster"
    archive = master / "Ninaivu Archive"
    for i in range(3):
        photo(archive / "2024" / "01" / "02" / f"p{i}.jpg", 30 * i)
    photo(master / "loose" / "x.jpg", 200)

    cfg = Config()
    cfg.state_dir = tmp_path / "state"
    cfg.roots = [str(master), str(archive)]
    cfg.active_root = str(archive)
    cfg.ai_enabled = False
    cfg.ai_engine = "off"
    cfg.watch = False
    cfg.workers = 2
    cfg.min_media_bytes = 0
    cfg.ensure_dirs()

    conn = db.init_db(cfg.db_path)
    auth.init_auth_schema(conn)
    scanner = Scanner(cfg)
    for root in cfg.roots:
        scanner._run(Path(root), full=True)
    admin = auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    maya = auth.create_user(conn, "maya", "", display_name="Maya",
                            role=auth.ROLE_FAMILY, created_by=admin.id)
    services = build_services(cfg)
    services.scanner.stop()
    return {
        "cfg": cfg, "conn": conn, "admin": admin, "maya": maya,
        "master": str(master), "archive": str(archive),
        "console": login(create_admin_app(services).test_client(), *ADMIN),
    }


def row(conn, root, rel):
    return conn.execute("SELECT * FROM assets WHERE root=? AND rel_path=?",
                        (root, rel)).fetchone()


def count(conn, root):
    return conn.execute("SELECT COUNT(*) FROM assets WHERE root=?", (root,)).fetchone()[0]


def person(conn, name):
    return conn.execute("INSERT INTO people_clusters(name, created_at) VALUES (?, ?)",
                        (name, time.time())).lastrowid


def face(conn, asset_id, person_id=None):
    return conn.execute(
        "INSERT INTO faces(asset_id, person_id, source, bbox, embedding) "
        "VALUES (?, ?, ?, ?, ?)",
        (asset_id, person_id, "confirmed" if person_id else "none",
         json.dumps([10, 10, 50, 50]), b"\0" * 8)).lastrowid


def decorate(n, root, rel_prefix):
    """A story, an album place, a favourite, a caption and a named face on
    p0, as seen from *root*; a favourite on p1 from the other side."""
    conn = n["conn"]
    p0 = row(conn, root, f"{rel_prefix}2024/01/02/p0.jpg")["id"]
    conn.execute("INSERT INTO stories(asset_id, file, mime, created_at) "
                 "VALUES (?, 'grandma.m4a', 'audio/mp4', ?)", (p0, time.time()))
    album = conn.execute("INSERT INTO albums(name, created_at) VALUES ('Pongal', ?)",
                         (time.time(),)).lastrowid
    conn.execute("INSERT INTO album_items(album_id, asset_id, added_at) VALUES (?, ?, ?)",
                 (album, p0, time.time()))
    conn.execute("INSERT INTO user_assets(user_id, asset_id, favorite) VALUES (?, ?, 1)",
                 (n["admin"].id, p0))
    conn.execute("UPDATE assets SET caption='Pongal at home', caption_source='manual' "
                 "WHERE id=?", (p0,))
    conn.execute("INSERT INTO shares(token, scope, target_id, created_at) "
                 "VALUES ('tok', 'asset', ?, ?)", (p0, time.time()))
    face(conn, p0, person(conn, "Paati"))
    conn.commit()
    return album


def test_a_folder_inside_another_is_indexed_twice(nested):
    conn = nested["conn"]
    assert count(conn, nested["archive"]) == 3
    assert count(conn, nested["master"]) == 4


@pytest.mark.parametrize("going", ["master", "archive"])
def test_removing_either_keeps_what_people_did(nested, going):
    conn = nested["conn"]
    kept = "archive" if going == "master" else "master"
    prefix = {"master": "Ninaivu Archive/", "archive": ""}
    album = decorate(nested, nested[going], prefix[going])
    other_p1 = row(conn, nested[kept], f"{prefix[kept]}2024/01/02/p1.jpg")["id"]
    conn.execute("INSERT INTO user_assets(user_id, asset_id, favorite) VALUES (?, ?, 1)",
                 (nested["admin"].id, other_p1))
    twin_p0 = row(conn, nested[kept], f"{prefix[kept]}2024/01/02/p0.jpg")["id"]
    thumbs = Path(nested["cfg"].thumbs_dir)
    gone_thumb = row(conn, nested[going], f"{prefix[going]}2024/01/02/p0.jpg")["thumb"]
    kept_thumb = row(conn, nested[kept], f"{prefix[kept]}2024/01/02/p0.jpg")["thumb"]
    assert list(thumbs.rglob(f"{gone_thumb}_*")) and gone_thumb != kept_thumb
    unnamed = face(conn, twin_p0)
    conn.commit()

    response = nested["console"].delete("/api/admin/libraries",
                                        query_string={"path": nested[going]})
    assert response.status_code == 200, response.get_json()
    assert nested[going] not in nested["cfg"].roots
    assert response.get_json()["kept"] == 3

    p0 = row(conn, nested[kept], f"{prefix[kept]}2024/01/02/p0.jpg")
    assert p0["id"] == twin_p0
    assert p0["caption"] == "Pongal at home"
    assert conn.execute("SELECT file FROM stories WHERE asset_id=?",
                        (p0["id"],)).fetchone()["file"] == "grandma.m4a"
    assert not conn.execute("SELECT 1 FROM stories_gone").fetchone(), \
        "the recording must not be queued for deletion"
    assert conn.execute("SELECT 1 FROM album_items WHERE album_id=? AND asset_id=?",
                        (album, p0["id"])).fetchone()
    assert conn.execute("SELECT favorite FROM user_assets WHERE asset_id=?",
                        (p0["id"],)).fetchone()["favorite"] == 1
    assert conn.execute("SELECT favorite FROM user_assets WHERE asset_id=?",
                        (other_p1,)).fetchone()["favorite"] == 1
    assert conn.execute("SELECT target_id FROM shares WHERE token='tok'"
                        ).fetchone()["target_id"] == p0["id"]
    named = conn.execute("SELECT id, person_id FROM faces WHERE asset_id=?",
                         (p0["id"],)).fetchall()
    assert [f["id"] for f in named] == [unnamed], "the same face, not a second one"
    assert named[0]["person_id"] is not None

    assert not list(thumbs.rglob(f"{gone_thumb}_*")), "the second set of thumbnails goes"
    assert list(thumbs.rglob(f"{kept_thumb}_*"))

    assert count(conn, nested[kept]) == (3 if kept == "archive" else 4)
    if going == "master":
        assert count(conn, nested["master"]) == 0, "what only it held still goes"


def test_a_twin_never_walked_is_moved_not_lost(nested):
    """Rows of the folder going that the other folder has no row for yet
    are given the other folder's name for the same file."""
    conn = nested["conn"]
    conn.execute("DELETE FROM assets WHERE root=?", (nested["archive"],))
    conn.commit()
    p0 = row(conn, nested["master"], "Ninaivu Archive/2024/01/02/p0.jpg")["id"]

    folded = nested_roots.fold(conn, nested["master"], nested["archive"])
    assert folded == {"merged": 0, "moved": 3, "thumbs": []}
    moved = conn.execute("SELECT root, rel_path, folder FROM assets WHERE id=?",
                         (p0,)).fetchone()
    assert tuple(moved) == (nested["archive"], "2024/01/02/p0.jpg", "2024/01/02")


def test_adding_a_folder_inside_a_library_folder_is_refused(nested, tmp_path):
    cfg = nested["cfg"]
    cfg.roots.remove(nested["archive"])
    inner = Path(nested["archive"]) / "2024"
    response = nested["console"].post("/api/library/root", json={"path": str(inner)})
    assert response.status_code == 409
    assert "already a library folder" in response.get_json()["error"]
    assert str(inner) not in cfg.roots


def test_adding_a_folder_around_one_takes_it_in(nested):
    conn, cfg = nested["conn"], nested["cfg"]
    cfg.roots.remove(nested["master"])
    conn.execute("DELETE FROM assets WHERE root=?", (nested["master"],))
    conn.commit()
    decorate(nested, nested["archive"], "")

    response = nested["console"].post("/api/library/root",
                                      json={"path": nested["master"]})
    assert response.status_code == 200, response.get_json()
    assert cfg.roots == [nested["master"]]
    assert cfg.active_root == nested["master"]
    p0 = row(conn, nested["master"], "Ninaivu Archive/2024/01/02/p0.jpg")
    assert p0["caption"] == "Pongal at home"
    assert conn.execute("SELECT 1 FROM stories WHERE asset_id=?", (p0["id"],)).fetchone()


def test_no_warning_for_someone_still_covered(nested):
    """Maya is given the Archive. Removing the folder around it leaves her
    seeing the same photographs, so there is nothing to warn about."""
    console = nested["console"]
    console.post(f"/api/people/{nested['maya'].id}", json={"library": nested["archive"]})
    response = console.delete("/api/admin/libraries",
                              query_string={"path": nested["master"]})
    assert response.status_code == 200, response.get_json()


def test_relation():
    roots = ["/v/Master", "/v/Other"]
    assert nested_roots.relation("/v/Master/Archive", roots) == (["/v/Master"], [])
    assert nested_roots.relation("/v", roots) == ([], roots)
    assert nested_roots.relation("/v/Master-2", roots) == ([], [])
