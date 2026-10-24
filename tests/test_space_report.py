"""The Space report: where the bytes are and what a light library would save.

It only reads. These check the classes a file falls in, the sums and the
estimate's direction, that duplicates and the state folder are counted, that
only an administrator may ask, and that ``ninaivu space`` prints it from a
read-only index.
"""

import json

import pytest

from conftest import FAMILY, GUEST, login
from ninaivu.storage import space

MB = 1024 * 1024


def set_asset(conn, asset_id, **fields):
    columns = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE assets SET {columns} WHERE id=?", (*fields.values(), asset_id))
    conn.commit()


def ids(conn, root):
    return [int(r["id"]) for r in conn.execute(
        "SELECT id FROM assets WHERE root=? ORDER BY id", (root,))]


# --- classes -----------------------------------------------------------------

@pytest.mark.parametrize("ext, kind, camera, expected", [
    ("jpg", "picture", "Canon EOS 80D", "jpeg"),
    ("JPEG", "picture", None, "jpeg"),
    ("heic", "picture", "Apple iPhone 13", "heic"),
    ("dng", "picture", None, "raw"),
    ("png", "picture", None, "png"),
    ("gif", "picture", None, "other_picture"),
    ("mov", "video", "iPhone 12", "phone_video"),
    ("mp4", "video", "Apple iPhone 15 Pro", "phone_video"),
    ("mov", "video", "Canon EOS 80D", "video"),
    ("mp4", "video", None, "video"),
    ("avi", "video", None, "legacy_video"),
    ("mts", "video", "Sony HDR", "legacy_video"),
    ("mp3", "audio", None, "audio"),
    ("pdf", "unknown", None, "other"),
])
def test_each_file_falls_in_the_class_its_light_copy_would_be(ext, kind, camera, expected):
    assert space.classify(ext, kind, camera) == expected


def test_kept_classes_keep_their_whole_size():
    shares = space.light_shares()
    for key in ("heic", "phone_video", "other_picture", "audio", "other"):
        assert shares[key] == (1.0, 1.0)
    for key in ("jpeg", "raw", "png", "video", "legacy_video"):
        low, high = shares[key]
        assert 0 < low <= high < 1


# --- the library ---------------------------------------------------------------

def test_the_report_adds_up_and_the_estimate_points_the_right_way(scanned):
    cfg, conn, _ = scanned
    root = cfg.active_root
    first, second, *rest = ids(conn, root)
    set_asset(conn, first, ext="jpg", kind="picture", size=10 * MB, date_key="2019-05-01")
    set_asset(conn, second, ext="heic", kind="picture", size=4 * MB, date_key="2024-01-02")
    for other in rest:
        set_asset(conn, other, size=0)

    report = space.library_report(conn, [root])
    by_class = {c["class"]: c for c in report["classes"]}
    assert by_class["jpeg"]["bytes"] == 10 * MB
    assert by_class["heic"]["bytes"] == 4 * MB
    assert by_class["heic"]["light_bytes_low"] == by_class["heic"]["light_bytes_high"] == 4 * MB
    assert report["bytes"] == 14 * MB
    assert report["files"] == len(rest) + 2
    assert report["light_bytes_low"] <= report["light_bytes_high"] <= report["bytes"]
    assert report["saving_bytes_low"] == report["bytes"] - report["light_bytes_high"]
    assert report["saving_bytes_high"] == report["bytes"] - report["light_bytes_low"]
    years = {y["year"]: y["bytes"] for y in report["years"]}
    assert years["2019"] == 10 * MB and years["2024"] == 4 * MB
    sizes = [f["bytes"] for f in report["folders"]]
    assert sizes == sorted(sizes, reverse=True) and sum(sizes) == 14 * MB


def test_files_in_the_bin_are_left_out(scanned):
    cfg, conn, _ = scanned
    root = cfg.active_root
    before = space.library_report(conn, [root])
    one = ids(conn, root)[0]
    size = conn.execute("SELECT size FROM assets WHERE id=?", (one,)).fetchone()[0]
    set_asset(conn, one, trashed=1)
    after = space.library_report(conn, [root])
    assert after["files"] == before["files"] - 1
    assert after["bytes"] == before["bytes"] - size


def test_hidden_files_are_counted(scanned):
    cfg, conn, _ = scanned
    root = cfg.active_root
    before = space.library_report(conn, [root])
    set_asset(conn, ids(conn, root)[0], visibility=2)
    assert space.library_report(conn, [root])["files"] == before["files"]


def test_no_library_is_an_empty_report(scanned):
    _, conn, _ = scanned
    report = space.library_report(conn, [])
    assert report["files"] == 0 and report["bytes"] == 0


# --- duplicates ---------------------------------------------------------------

def test_exact_copies_are_counted_from_the_fingerprints(scanned):
    from ninaivu.storage import fingerprints
    cfg, conn, _ = scanned
    root = cfg.active_root
    a, b, c, *_ = ids(conn, root)
    for asset_id in (a, b, c):
        set_asset(conn, asset_id, size=3 * MB, mtime=1000.0)
    conn.executemany(
        "INSERT OR REPLACE INTO content_fingerprints(asset_id, algo, digest, size, mtime) "
        "VALUES (?, ?, ?, ?, ?)",
        [(a, fingerprints.ALGO, "same", 3 * MB, 1000.0),
         (b, fingerprints.ALGO, "same", 3 * MB, 1000.0),
         (c, fingerprints.ALGO, "other", 3 * MB, 1000.0)])
    conn.commit()

    exact = space.duplicates_report(conn, [root])["exact"]
    assert exact["available"] is True
    assert exact == {"available": True, "groups": 1, "spare_files": 1, "spare_bytes": 3 * MB}


def test_near_duplicates_are_counted_from_the_perceptual_hash(scanned):
    cfg, conn, _ = scanned
    root = cfg.active_root
    a, b, c, *_ = ids(conn, root)
    conn.execute("UPDATE assets SET dup_group=NULL")
    set_asset(conn, a, dup_group="g1")
    set_asset(conn, b, dup_group="g1")
    set_asset(conn, c, dup_group="g2")          # alone: not a group
    similar = space.duplicates_report(conn, [root])["similar"]
    assert similar == {"groups": 1, "files": 2}


# --- the state folder ------------------------------------------------------------

def test_the_state_folder_is_measured_largest_first(tmp_path):
    state = tmp_path / "state"
    (state / "thumbs" / "ab").mkdir(parents=True)
    (state / "thumbs" / "ab" / "x.webp").write_bytes(b"x" * 5000)
    (state / "thumbs" / "y.webp").write_bytes(b"y" * 3000)
    (state / "index.db").write_bytes(b"i" * 1000)
    models = tmp_path / "models"
    models.mkdir()
    (models / "m.onnx").write_bytes(b"m" * 7000)

    report = space.state_report(state, elsewhere=[("ai_models", str(models)),
                                                  ("backups", ""),
                                                  ("inside", str(state / "thumbs"))])
    assert [e["name"] for e in report["entries"]] == ["thumbs", "index.db"]
    assert report["entries"][0] == {"name": "thumbs", "files": 2, "bytes": 8000}
    assert report["bytes"] == 9000
    assert report["elsewhere"] == [{"what": "ai_models", "path": str(models),
                                    "name": "models", "files": 1, "bytes": 7000}]


def test_the_state_walk_is_kept_for_a_while(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    (state / "a").write_bytes(b"a" * 10)
    first = space.cached_state_report(state, fresh=True)
    (state / "b").write_bytes(b"b" * 10)
    assert space.cached_state_report(state)["bytes"] == first["bytes"]
    assert space.cached_state_report(state, fresh=True)["bytes"] == 20


# --- the console's answer -------------------------------------------------------

@pytest.mark.parametrize("who", ["family", "guest"])
def test_only_an_admin_can_ask(app, people, who):
    client = login(app.test_client(), *(FAMILY if who == "family" else GUEST))
    assert client.get("/api/admin/space").status_code in (401, 403)


def test_the_console_gets_the_whole_report(as_admin):
    body = as_admin.get("/api/admin/space").get_json()
    assert set(body) == {"library", "duplicates", "state", "shares"}
    assert body["library"]["files"] > 0
    assert body["library"]["estimate"] is True
    assert {c["class"] for c in body["library"]["classes"]} == {k for k, _, _ in space.CLASSES}
    assert body["state"]["bytes"] > 0
    assert body["shares"]["heic"] == [1.0, 1.0]


def test_asking_changes_nothing(as_admin, scanned):
    cfg, conn, _ = scanned
    before = [tuple(r) for r in conn.execute("SELECT * FROM assets ORDER BY id")]
    as_admin.get("/api/admin/space?fresh=1")
    assert [tuple(r) for r in conn.execute("SELECT * FROM assets ORDER BY id")] == before


# --- ninaivu space ------------------------------------------------------------

def test_the_command_prints_the_report_from_a_read_only_index(scanned, capsys):
    from ninaivu.cli import space as cli
    cfg, _, _ = scanned
    data = cli.report(cfg, fresh=True)
    assert data["library"]["files"] > 0
    cli.print_report(data)
    out = capsys.readouterr().out
    assert "A light library would take" in out
    assert "Ninaivu's own files" in out
    json.dumps(data)                            # the --json form can be written


def test_the_command_says_when_there_is_no_index(tmp_path):
    from ninaivu.cli import space as cli
    from ninaivu.server.config import Config
    cfg = Config()
    cfg.state_dir = tmp_path / "empty"
    with pytest.raises(SystemExit, match="No index"):
        cli.report(cfg)


@pytest.mark.parametrize("n, text", [(0, "0 bytes"), (2048, "2.0 KB"),
                                     (5 * MB, "5.0 MB"), (3 * 1024 ** 4, "3.0 TB")])
def test_sizes_read_naturally(n, text):
    from ninaivu.cli.space import size
    assert size(n) == text
