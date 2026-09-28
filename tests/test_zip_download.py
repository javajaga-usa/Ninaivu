"""Downloading a selection as one archive.

Selecting a day's photographs and downloading them used to fire one request
per file, 320 ms apart, capped at twenty — browsers block that after the first
few and everything past the twentieth was dropped without saying so.

The archive is streamed as it is read, so the interesting properties are not
about zipping. They are about access: which files a request can put inside it
is decided by the same query the gallery uses, and nothing in the request ever
names a path.
"""

import io
import zipfile

import pytest

from conftest import ADMIN, FAMILY, GUEST, login
from ninaivu.storage import db


def open_zip(response) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(response.get_data()))


def all_ids(client, path="/api/assets?limit=200"):
    return [item["id"] for item in client.get(path).get_json()["items"]]


# --- it works --------------------------------------------------------------

def test_a_selection_arrives_as_one_archive(app, people):
    client = app.test_client()
    login(client, *FAMILY)
    ids = all_ids(client)[:3]

    response = client.get(f"/api/download/zip?ids={','.join(map(str, ids))}")
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "application/zip"
    assert "attachment" in response.headers["Content-Disposition"]

    with open_zip(response) as archive:
        assert archive.testzip() is None, "the archive is corrupt"
        assert len(archive.namelist()) == len(ids)


def test_the_bytes_inside_match_the_originals(app, people, scanned):
    """A download that quietly corrupts photographs would be worse than none."""
    from pathlib import Path
    cfg, conn, _ = scanned
    client = app.test_client()
    login(client, *FAMILY)
    ids = all_ids(client)[:2]

    response = client.get(f"/api/download/zip?ids={','.join(map(str, ids))}")
    with open_zip(response) as archive:
        for asset_id in ids:
            row = db.get_asset(conn, asset_id)
            original = (Path(row["root"]) / row["rel_path"]).read_bytes()
            stored = archive.read(row["filename"])
            assert stored == original, f"{row['filename']} came out different"


def test_media_is_stored_not_recompressed(app, people):
    """JPEG and MP4 are already compressed; deflating them burns CPU to make
    the archive slightly larger."""
    client = app.test_client()
    login(client, *FAMILY)
    ids = all_ids(client)[:2]

    response = client.get(f"/api/download/zip?ids={','.join(map(str, ids))}")
    with open_zip(response) as archive:
        for info in archive.infolist():
            assert info.compress_type == zipfile.ZIP_STORED


def test_two_files_with_the_same_name_both_survive(app, people, scanned):
    """IMG_0001.jpg is not a distinctive name. Without this the second one
    silently replaces the first on extraction."""
    from pathlib import Path
    import shutil
    cfg, conn, _ = scanned
    library = Path(cfg.active_root)
    source = next(library.rglob("*.jpg"))
    for folder in ("trip-a", "trip-b"):
        (library / folder).mkdir(exist_ok=True)
        shutil.copy(source, library / folder / "IMG_0001.jpg")

    from ninaivu.media.scanner import Scanner
    Scanner(cfg)._run(library, full=True)

    client = app.test_client()
    login(client, *ADMIN)
    rows = conn.execute(
        "SELECT id FROM assets WHERE filename='IMG_0001.jpg'").fetchall()
    assert len(rows) == 2
    ids = ",".join(str(r["id"]) for r in rows)

    response = client.get(f"/api/download/zip?ids={ids}")
    with open_zip(response) as archive:
        names = archive.namelist()
    assert len(names) == 2
    assert len(set(names)) == 2, f"one file would overwrite the other: {names}"


@pytest.mark.parametrize("filename, plain", [
    ("நாள்.jpg", "ninaivu-"),          # nothing survives in ASCII: the fallback
    ("Café.jpg", "Cafe.zip"),         # accents fold to their letters
    ('say "cheese".jpg', None),       # quotes must not end the header value
])
def test_one_photograph_with_any_name_downloads(app, people, scanned,
                                                 filename, plain):
    """A single photograph's archive is named after it, and header values are
    Latin-1 on the wire — a Tamil filename used to make the server answer the
    download with a 500."""
    import re
    from urllib.parse import unquote
    cfg, conn, _ = scanned
    client = app.test_client()
    login(client, *FAMILY)
    target = all_ids(client)[0]
    conn.execute("UPDATE assets SET filename=? WHERE id=?", (filename, target))
    conn.commit()

    response = client.get(f"/api/download/zip?ids={target}")
    assert response.status_code == 200
    with open_zip(response) as archive:
        assert archive.namelist() == [filename]
    header = response.headers["Content-Disposition"]
    header.encode("latin-1")          # what waitress does, and failed on
    assert header.startswith("attachment;")
    stem = filename.rsplit(".", 1)[0]
    # Quoted only when it has to be, which RFC 6266 allows either way.
    ascii_name = re.search(r'filename=("(?:[^"\\]|\\.)*"|[^;\s]+)', header).group(1)
    if ascii_name.startswith('"'):
        ascii_name = ascii_name[1:-1]
    utf8_name = re.search(r"filename\*=UTF-8''(\S+)", header)
    if utf8_name:
        assert unquote(utf8_name.group(1)) == f"{stem}.zip"
    else:
        assert ascii_name.replace('\\"', '"') == f"{stem}.zip"
    if plain:
        assert ascii_name.startswith(plain)


# --- who may put what inside it -------------------------------------------

def test_a_guest_cannot_download_an_archive_at_all(app, people):
    client = app.test_client()
    login(client, *GUEST)
    assert client.get("/api/download/zip?ids=1,2,3").status_code in (401, 403)


def test_asking_for_a_hidden_photograph_does_not_include_it(app, people, scanned):
    """The request names ids, so this is the interesting case: a family member
    who guesses a hidden photograph's id must get an archive without it, not
    an archive with it."""
    cfg, conn, _ = scanned
    admin = app.test_client()
    login(admin, *ADMIN)
    everything = all_ids(admin, "/api/assets?limit=200&visibility=hidden")
    if not everything:
        target = conn.execute("SELECT id FROM assets LIMIT 1").fetchone()["id"]
        conn.execute("UPDATE assets SET visibility=2 WHERE id=?", (target,))
        conn.commit()
        everything = [target]
    hidden_id = everything[0]

    family = app.test_client()
    login(family, *FAMILY)
    visible = all_ids(family)[:2]
    asked = ",".join(str(i) for i in [*visible, hidden_id])

    response = family.get(f"/api/download/zip?ids={asked}")
    if response.status_code == 200:
        with open_zip(response) as archive:
            assert len(archive.namelist()) == len(visible), (
                "a hidden photograph was included in a family member's archive")


def test_an_empty_request_is_refused(app, people):
    client = app.test_client()
    login(client, *FAMILY)
    assert client.get("/api/download/zip?ids=").status_code == 400
    assert client.get("/api/download/zip").status_code == 400


def test_ids_that_are_not_numbers_are_ignored(app, people):
    client = app.test_client()
    login(client, *FAMILY)
    assert client.get("/api/download/zip?ids=../../etc/passwd").status_code == 400


def test_a_selection_of_nothing_you_own_is_a_404(app, people):
    client = app.test_client()
    login(client, *FAMILY)
    assert client.get("/api/download/zip?ids=999999").status_code == 404


# --- what it could not include, it says --------------------------------------

def test_a_file_that_cannot_be_read_is_named_inside_the_download(app, people, scanned):
    """Skipping an unreadable photo keeps the rest of the download worth having.
    Skipping it silently left someone believing they had every photo they picked."""
    from pathlib import Path
    _, conn, _ = scanned
    client = app.test_client()
    login(client, *FAMILY)
    ids = all_ids(client)[:3]
    gone = db.get_asset(conn, ids[0])
    (Path(gone["root"]) / gone["rel_path"]).unlink()

    response = client.get(f"/api/download/zip?ids={','.join(map(str, ids))}")
    assert response.status_code == 200
    with open_zip(response) as archive:
        names = archive.namelist()
        assert "NOT INCLUDED.txt" in names
        assert gone["filename"] not in names
        note = archive.read("NOT INCLUDED.txt").decode("utf-8")
        assert gone["filename"] in note
        assert len(names) == 3, "the two readable photos plus the note"


def test_a_complete_download_carries_no_note(app, people):
    client = app.test_client()
    login(client, *FAMILY)
    ids = all_ids(client)[:2]
    with open_zip(client.get(f"/api/download/zip?ids={','.join(map(str, ids))}")) as archive:
        assert "NOT INCLUDED.txt" not in archive.namelist()
