"""What came across from Ninaivu Lite: each test fails without its change."""

from __future__ import annotations

import gzip
import io
import json
import struct
from pathlib import Path

from PIL import Image

from conftest import ADMIN, FAMILY, login

ROOT = Path(__file__).resolve().parents[1]


def _same_origin(client):
    client.environ_base["HTTP_SEC_FETCH_SITE"] = "same-origin"
    return client


# --- the server -------------------------------------------------------------

def test_every_answer_denies_camera_microphone_location_and_payment(client):
    policy = client.get("/api/health").headers.get("Permissions-Policy", "")
    for feature in ("camera=()", "microphone=()", "geolocation=()", "payment=()"):
        assert feature in policy


def test_an_oversized_json_body_is_refused_unread_and_said_as_json(as_admin):
    _same_origin(as_admin)
    answer = as_admin.post("/api/admin/settings", data=b"{" + b" " * (5 * 1024 * 1024) + b"}",
                           content_type="application/json")
    assert answer.status_code == 413
    assert answer.get_json()["status"] == 413


def test_scripts_are_gzipped_and_a_revisit_is_still_a_304(client):
    first = client.get("/static/js/app.js", headers={"Accept-Encoding": "gzip"})
    assert first.status_code == 200 and first.headers["Content-Encoding"] == "gzip"
    plain = client.get("/static/js/app.js")
    assert "Content-Encoding" not in plain.headers
    assert gzip.decompress(first.get_data()) == plain.get_data()
    etag = first.headers["ETag"]
    assert etag.endswith('-gz"')
    again = client.get("/static/js/app.js", headers={"Accept-Encoding": "gzip",
                                                       "If-None-Match": etag})
    assert again.status_code == 304


def test_the_family_page_itself_is_gzipped(client):
    page = client.get("/", headers={"Accept-Encoding": "gzip"})
    assert page.status_code == 200 and page.headers.get("Content-Encoding") == "gzip"


def test_reset_password_from_the_command_line(scanned, people, monkeypatch):
    from ninaivu.cli import reset_password
    from ninaivu.server import auth
    from ninaivu.server.config import Config

    cfg, conn, _ = scanned
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))
    auth.set_active(conn, people["admin"].id, False)
    answers = iter(["a-brand-new-one-42", "a-brand-new-one-42"])
    assert reset_password.main([ADMIN[0]], ask=lambda _: next(answers)) == 0
    user = auth.get_user(conn, people["admin"].id)
    assert user.active
    assert auth.authenticate(conn, ADMIN[0], "a-brand-new-one-42") is not None
    assert reset_password.main(["nobody"], ask=lambda _: "x") == 2


def _picture(colour=(200, 40, 40)) -> io.BytesIO:
    data = io.BytesIO()
    Image.new("RGB", (64, 64), colour).save(data, "PNG")
    data.seek(0)
    return data


def test_a_replaced_profile_picture_gets_a_new_address(as_family):
    _same_origin(as_family)
    first = as_family.post("/api/me/avatar", data={"avatar": (_picture(), "a.png")},
                           content_type="multipart/form-data").get_json()
    second = as_family.post("/api/me/avatar",
                            data={"avatar": (_picture((10, 10, 200)), "b.png")},
                            content_type="multipart/form-data").get_json()
    assert first["user"]["avatar"] != second["user"]["avatar"]
    assert as_family.get("/api/me").get_json()["avatar"] == second["user"]["avatar"]


def test_an_administrator_can_take_someone_s_picture_down(app, people):
    family = _same_origin(login(app.test_client(), *FAMILY))
    family.post("/api/me/avatar", data={"avatar": (_picture(), "a.png")},
                content_type="multipart/form-data")
    admin = _same_origin(login(app.test_client(), *ADMIN))
    answer = admin.delete(f"/api/people/{people['family'].id}/avatar")
    assert answer.status_code == 200 and answer.get_json()["user"]["avatar"] is None


# --- photographs --------------------------------------------------------------

def _first_picture(conn):
    return dict(conn.execute(
        "SELECT * FROM assets WHERE kind='picture' AND ext='jpg' ORDER BY id LIMIT 1"
    ).fetchone())


def test_a_share_link_shows_a_turned_photograph_upright(app, scanned, people, as_admin):
    from ninaivu.storage import db

    _, conn, _ = scanned
    row = _first_picture(conn)
    _same_origin(as_admin)
    assert as_admin.post(f"/api/asset/{row['id']}/rotate",
                         json={"rotation": 90}).status_code == 200
    db.create_share(conn, "turned-token", "asset", row["id"], created_by=people["admin"].id)
    visitor = app.test_client()
    answer = visitor.get(f"/api/share/turned-token/file/{row['id']}")
    assert answer.status_code == 200
    width, height = Image.open(io.BytesIO(answer.get_data())).size
    assert height > width, "the turn the index holds is baked into the visitor's copy"


def test_a_manual_turn_survives_a_full_rescan_in_the_thumbnails(scanned, people, app,
                                                                as_admin):
    cfg, conn, scanner = scanned
    row = _first_picture(conn)
    _same_origin(as_admin)
    as_admin.post(f"/api/asset/{row['id']}/rotate", json={"rotation": 90})
    scanner._run(Path(cfg.active_root), full=True)
    after = dict(conn.execute("SELECT * FROM assets WHERE id=?", (row["id"],)).fetchone())
    assert after["rotation"] == 90 and after["rot_source"] == "manual"
    assert after["height"] > after["width"]
    from ninaivu.media import media
    thumb = cfg.thumbs_dir / media.thumb_file(after["thumb"], max(cfg.thumb_sizes),
                                              cfg.thumb_format)
    width, height = Image.open(thumb).size
    assert height > width


def test_thumbnail_writers_do_not_share_a_temporary_file(tmp_path, monkeypatch):
    from ninaivu.media import media

    seen = []
    real = Image.Image.save

    def spy(self, fp, *args, **kwargs):
        seen.append(str(fp))
        return real(self, fp, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "save", spy)
    media.write_thumbnails(Image.new("RGB", (100, 80)), tmp_path, "base", (64,))
    assert seen and all(".tmp" in name and str(__import__("os").getpid()) in name
                        for name in seen)
    assert not list(tmp_path.glob("*.tmp"))


def test_windows_system_folders_are_refused_on_whichever_drive_windows_is():
    from ninaivu.server.config import _windows_system_folders

    found = _windows_system_folders({"SystemRoot": r"D:\Windows",
                                     "ProgramFiles": r"D:\Program Files",
                                     "SystemDrive": "D:"})
    assert {r"d:\windows", r"d:\program files", "d:\\"} <= found
    assert _windows_system_folders({}) == set()


# --- import --------------------------------------------------------------------

def test_the_archive_is_never_written_into_ninaivu_s_own_data_folder(tmp_path):
    from ninaivu.archive.safety import validate_job

    source = tmp_path / "card"
    source.mkdir()
    state = tmp_path / "state"
    problems = validate_job([str(source)], str(state / "archive"), protected=[state])
    assert any("own data folder" in p for p in problems)
    assert not validate_job([str(source)], str(tmp_path / "archive"), protected=[state])


def test_a_source_already_in_the_library_is_pointed_out(tmp_path):
    from ninaivu.archive.safety import job_notices

    library = tmp_path / "Pictures"
    (library / "2019").mkdir(parents=True)
    notices = job_notices([str(library / "2019")], str(tmp_path / "archive"),
                          libraries=[str(library)])
    assert any("already in the library" in n for n in notices)


def test_import_refusals_are_english_that_can_also_be_translated(tmp_path):
    import json

    from ninaivu.archive.safety import job_notices, translatable, validate_job

    library = tmp_path / "Pictures"
    (library / "2019").mkdir(parents=True)
    source = library / "2019"
    problems = validate_job([str(source)], str(source))
    assert problems[0] == f"Destination is the same folder as source “{source}”. " \
        "The archive must be somewhere else entirely."
    [said] = translatable(problems)
    assert said["key"].startswith("Destination is the same folder as source “{path}”.")
    assert said["params"] == {"path": str(source)}
    notices = job_notices([str(source)], str(tmp_path / "archive"), libraries=[str(library)])
    assert translatable(notices)[0]["params"]["library"] == str(library)
    tamil = json.loads((ROOT / "ninaivu/static/i18n/ta.json").read_text(encoding="utf-8"))
    assert said["key"] in tamil


def test_a_path_without_its_drive_or_folder_is_refused(tmp_path):
    from ninaivu.archive.safety import validate_job

    card = tmp_path / "card"
    card.mkdir()
    problems = validate_job(["DCIM"], str(tmp_path / "archive"))
    assert problems == ["Give the full path of the source folder, not “DCIM”."]
    problems = validate_job([str(card)], "Photo Archive")
    assert len(problems) == 1 and problems[0].startswith(
        "Give the full path of the destination folder")


# --- moving up from Lite -------------------------------------------------------

def test_a_lite_export_comes_across(scanned, people):
    from ninaivu.server import auth
    from ninaivu.storage import lite_import

    cfg, conn, _ = scanned
    root = cfg.roots[0]
    rows = [dict(r) for r in conn.execute(
        "SELECT id, rel_path FROM assets WHERE kind='picture' "
        "AND rel_path NOT LIKE 'private/%' ORDER BY id LIMIT 3")]
    ref = [{"folder": root, "path": r["rel_path"]} for r in rows]
    lite_hash = auth.hash_password("grandma-loves-tea")
    data = {
        "format": "ninaivu-lite-export", "format_version": 1,
        "folders": [root, "/nowhere/else"],
        "people": [
            {"username": "paati", "name": "Paati", "role": "family",
             "password": lite_hash, "password_scheme": "scrypt",
             "pin": "pbkdf2$1$aa$bb", "pin_scheme": "pbkdf2",
             "library": None, "active": True, "created_by": ADMIN[0]},
            {"username": ADMIN[0], "name": "Someone else", "role": "guest"},
        ],
        "folder_rules": [{"folder": root, "path": "private", "level": 2}],
        "visibility": [{**ref[0], "level": 0, "source": "item"}],
        "favourites": [{**ref[1], "user": "paati"}],
        "albums": [{"id": 7, "name": "Pongal", "owner": "paati", "cover": ref[2],
                    "items": [ref[1], ref[2], {"folder": root, "path": "gone.jpg"}]}],
        "shares": [{"token": "lite-share-token", "kind": "album", "album_id": 7,
                    "owner": "paati", "password": None}],
    }
    report = lite_import.import_export(conn, list(cfg.roots), data)

    paati = auth.get_user_by_name(conn, "paati")
    assert paati is not None and paati.role == "family"
    assert auth.authenticate(conn, "paati", "grandma-loves-tea") is not None
    assert report["pins_left_off"] == ["paati"]
    assert report["people_already_here"] == [ADMIN[0]]
    assert auth.get_user_by_name(conn, ADMIN[0]).role == "admin"
    assert paati.id != people["admin"].id
    assert conn.execute("SELECT created_by FROM users WHERE id=?",
                        (paati.id,)).fetchone()[0] == people["admin"].id

    assert conn.execute("SELECT visibility FROM folder_rules WHERE folder='private'"
                        ).fetchone()[0] == 2
    hidden = conn.execute("SELECT DISTINCT visibility FROM assets WHERE "
                          "rel_path LIKE 'private/%'").fetchall()
    assert [r[0] for r in hidden] == [2]
    assert conn.execute("SELECT visibility FROM assets WHERE id=?",
                        (rows[0]["id"],)).fetchone()[0] == 0
    assert conn.execute("SELECT favorite FROM user_assets WHERE user_id=? AND asset_id=?",
                        (paati.id, rows[1]["id"])).fetchone()[0] == 1
    album = conn.execute("SELECT id, cover_id FROM albums WHERE name='Pongal'").fetchone()
    assert album["cover_id"] == rows[2]["id"]
    assert conn.execute("SELECT COUNT(*) FROM album_items WHERE album_id=?",
                        (album["id"],)).fetchone()[0] == 2
    share = conn.execute("SELECT scope, target_id FROM shares WHERE token='lite-share-token'"
                         ).fetchone()
    assert (share["scope"], share["target_id"]) == ("album", album["id"])
    assert report["photos_not_found"] == 1
    assert report["folders_not_in_library"] == ["/nowhere/else"]

    again = lite_import.import_export(conn, list(cfg.roots), data)
    assert again["people_added"] == [] and again["shares"] == 0


def test_a_file_that_is_not_a_lite_export_is_refused(tmp_path):
    import pytest

    from ninaivu.storage import lite_import

    wrong = tmp_path / "x.json"
    wrong.write_text(json.dumps({"format": "something"}))
    with pytest.raises(lite_import.LiteImportError):
        lite_import.load(wrong)
    newer = tmp_path / "y.json"
    newer.write_text(json.dumps({"format": "ninaivu-lite-export", "format_version": 99}))
    with pytest.raises(lite_import.LiteImportError):
        lite_import.load(newer)


# --- desktop, launchers, installers -------------------------------------------

def test_the_control_module_answers_status_for_installers(cfg, monkeypatch, capsys):
    from ninaivu.desktop import control

    monkeypatch.setattr(control.Config, "load", classmethod(lambda cls: cfg))
    assert control.main(["--status"]) == 0
    assert capsys.readouterr().out.strip() == "stopped"
    assert control.main(["--stop"]) == 0


def test_every_size_in_the_windows_icon_is_a_bitmap_not_a_png():
    """NSIS cannot read PNG-compressed icons: the installer showed a blank one."""
    data = (ROOT / "installers" / "windows" / "ninaivu.ico").read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind) == (0, 1) and count >= 4
    for i in range(count):
        size, offset = struct.unpack("<II", data[6 + 16 * i + 8:6 + 16 * i + 16])
        assert data[offset:offset + 4] != b"\x89PNG"
        assert struct.unpack("<I", data[offset:offset + 4])[0] == 40
        assert offset + size <= len(data)


def test_the_installer_template_stops_ninaivu_before_writing_and_is_ascii():
    text = (ROOT / "installers" / "windows" / "ninaivu.nsi").read_bytes().decode("ascii")
    assert "-m ninaivu.desktop.control --stop" in text
    assert "[% block install_pkgs %]" in text and 'RMDir /r "$INSTDIR\\Python"' in text
    assert "Jagadeesh Rajendran" in text


def test_the_launcher_brings_an_old_venv_up_to_date(tmp_path, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("lite_launcher", ROOT / "launcher" / "start.py")
    start = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(start)
    monkeypatch.setattr(start, "VENV_DIR", tmp_path / ".venv")
    start.VENV_DIR.mkdir()
    python = start.venv_python(start.VENV_DIR)
    asked = []
    monkeypatch.setattr(start, "pip_install", lambda py, specs, label, extra=None:
                        asked.append(list(specs)) or True)
    start.refresh_if_changed(python)
    assert asked == [[*start.CORE, *start.EXTRAS]]
    start.refresh_if_changed(python)
    assert len(asked) == 1, "asked once per change of the list, not every start"
    start.refresh_if_changed(Path("/usr/bin/python3"))
    assert len(asked) == 1, "a Python somebody else manages is left alone"
