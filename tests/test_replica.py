"""The Pi as a read-only replica (proposal F): the hand-over and the copy.

The Mac writes a hand-over (ninaivu/replica/publish.py); the Pi serves the
family app from it, never writing to the library, and sends people to the
mini while it is awake (receive.py, front.py). These run both ends in one
process: the "Pi" has its own state folder and reads the library at the same
path the "Mac" has it, as the Pi's systemd unit arranges with a read-only
bind mount.
"""

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from ninaivu import Services, create_home_app
from ninaivu.cli import replica as replica_cli
from ninaivu.replica import MANIFEST, front as front_mod
from ninaivu.replica.front import Front, magic_packet
from ninaivu.replica.publish import Publisher, publish
from ninaivu.replica.receive import Replica, ReplicaError, carry_sessions
from ninaivu.server import auth
from ninaivu.server.config import Config
from ninaivu.storage import db

from conftest import FAMILY


@pytest.fixture()
def mac(app, people, tmp_path):
    """The Mac's library with things in it that must not go to the Pi."""
    services = app.config["MV_SERVICES"]
    cfg = services.cfg
    conn = db.connect(cfg.db_path)
    auth.start_session(conn, people["admin"].id, "mac browser", face="home")
    first = conn.execute("SELECT id FROM assets ORDER BY id LIMIT 1").fetchone()["id"]
    db.store_embedding(conn, first, "m", 4, np.ones(4, "float32").tobytes())
    db.create_share(conn, "share-token-xyz", "asset", first, people["admin"].id)
    auth.audit(conn, people["admin"].id, "test", "typed something")
    cfg.notify_smtp_password = "hunter2-smtp"
    cfg.house_name = "Jagadeesh's house"
    cfg.save()
    dest = tmp_path / "handover"
    dest.mkdir()
    return SimpleNamespace(services=services, cfg=cfg, conn=conn, dest=dest, first=first)


def _pi_config(tmp_path: Path, dest: Path) -> Config:
    pi = Config()
    pi.state_dir = tmp_path / "pi-state"
    pi.ai_enabled = False
    pi.ai_engine = "off"
    pi.ai_models_dir = str(tmp_path / "pi-models")
    pi.watch = False
    pi.workers = 1
    pi.replica_from = str(dest)
    pi.ensure_dirs()
    return pi


@pytest.fixture()
def pi(mac, tmp_path):
    """The Pi, serving the Mac's first hand-over."""
    publish(mac.cfg, mac.dest)
    cfg = _pi_config(tmp_path, mac.dest)
    replica = Replica(cfg, mac.dest)
    assert replica.take_newest()
    services = Services(cfg)
    services.replica = replica
    front = Front(cfg, replica, opener=_asleep, sender=lambda packet: None)
    services.replica_front = front
    application = create_home_app(services)
    yield SimpleNamespace(cfg=cfg, replica=replica, front=front, app=application,
                          services=services)
    services.stop(timeout=5.0)


def _asleep(url, timeout=None):
    raise OSError("no route to host")


def _index(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


# -- the hand-over ---------------------------------------------------------------

def test_the_handover_is_whole_and_carries_no_secrets(mac):
    manifest = publish(mac.cfg, mac.dest)
    written = json.loads((mac.dest / MANIFEST).read_text())
    assert written["generation"] == manifest["generation"]
    index = mac.dest / manifest["index"]["file"]
    assert index.stat().st_size == manifest["index"]["size"]
    from ninaivu.utils.files import sha256_file
    assert sha256_file(index) == manifest["index"]["sha256"]
    with closing(_index(index)) as conn:
        for table in ("sessions", "shares", "embeddings", "audit", "auth_limits"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
        # The accounts go, so the family signs in on the Pi as on the mini.
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] > 10
    text = json.dumps(written)
    assert "hunter2" not in text and "share-token-xyz" not in text
    assert written["settings"]["house_name"] == "Jagadeesh's house"
    assert written["settings"]["roots"] == mac.cfg.roots
    assert "notify_smtp_password" not in written["settings"]


def test_the_library_is_never_written_by_the_handover(mac):
    root = Path(mac.cfg.active_root)
    before = {p: p.stat().st_mtime_ns for p in root.rglob("*")}
    publish(mac.cfg, mac.dest)
    assert {p: p.stat().st_mtime_ns for p in root.rglob("*")} == before


def test_only_changed_thumbnails_are_copied_again(mac):
    first = publish(mac.cfg, mac.dest)
    thumbs = first["folders"]["thumbs"]
    assert thumbs["copied"] == thumbs["files"] > 0
    assert sorted(p.relative_to(mac.dest / "thumbs") for p in (mac.dest / "thumbs").rglob("*")
                  if p.is_file()) == sorted(p.relative_to(mac.cfg.thumbs_dir)
                                            for p in mac.cfg.thumbs_dir.rglob("*") if p.is_file())
    second = publish(mac.cfg, mac.dest)
    assert second["folders"]["thumbs"]["copied"] == 0
    gone = next(p for p in mac.cfg.thumbs_dir.rglob("*") if p.is_file())
    gone.unlink()
    third = publish(mac.cfg, mac.dest)
    assert third["folders"]["thumbs"]["removed"] == 1
    assert not (mac.dest / "thumbs" / gone.relative_to(mac.cfg.thumbs_dir)).exists()


def test_a_new_destination_is_copied_to_from_the_beginning(mac, tmp_path):
    publish(mac.cfg, mac.dest)
    other = tmp_path / "other"
    other.mkdir()
    again = publish(mac.cfg, other)
    assert again["folders"]["thumbs"]["copied"] == again["folders"]["thumbs"]["files"] > 0


def test_old_snapshots_are_removed_but_the_one_before_is_kept(mac):
    names = [publish(mac.cfg, mac.dest)["index"]["file"] for _ in range(3)]
    left = sorted(p.name for p in mac.dest.glob("index-*.db"))
    assert left == sorted(names[1:])


def test_publishing_needs_the_folder_to_be_there(mac, tmp_path):
    with pytest.raises(FileNotFoundError):
        publish(mac.cfg, tmp_path / "share-not-connected")


def test_the_publisher_hands_over_when_the_index_changed(mac):
    mac.cfg.replica_dir = str(mac.dest)
    mac.cfg.replica_every_minutes = 30
    keeper = Publisher(mac.cfg)
    assert keeper.due()                       # never handed over to this folder
    keeper.start()
    keeper._thread.join(60)
    status = keeper.status()
    assert status["error"] == "" and status["generation"]
    assert not keeper.due()                   # just done
    record = keeper.last()
    record["at"] = time.time() - 3600
    (Path(mac.cfg.state_dir) / "replica-publish.json").write_text(json.dumps(record))
    assert not keeper.due()                   # time has passed, nothing changed
    db.update_asset(mac.conn, mac.first, caption="Amma's 60th birthday")
    mac.conn.commit()
    assert keeper.due()


def test_the_console_sets_the_handover_folder(mac, as_admin, tmp_path):
    inside = Path(mac.cfg.active_root) / "2023"
    refused = as_admin.post("/api/replica/handover/settings", json={"folder": str(inside)})
    assert refused.status_code == 400
    missing = as_admin.post("/api/replica/handover/settings",
                            json={"folder": str(tmp_path / "nope")})
    assert missing.status_code == 400
    ok = as_admin.post("/api/replica/handover/settings",
                       json={"folder": str(mac.dest), "every_minutes": 15})
    assert ok.status_code == 200, ok.get_json()
    assert mac.cfg.replica_dir == str(mac.dest) and mac.cfg.replica_every_minutes == 15
    started = as_admin.post("/api/replica/handover")
    assert started.status_code == 200
    mac.services.replica_publisher._thread.join(60)
    assert (mac.dest / MANIFEST).is_file()
    assert as_admin.get("/api/replica/handover").get_json()["generation"]


def test_the_handover_controls_are_the_consoles(mac, as_family):
    assert as_family.get("/api/replica/handover").status_code in (401, 403)


# -- the Pi ------------------------------------------------------------------------

def test_the_pi_shows_what_the_mac_shows(mac, pi, as_family):
    mac_ids = sorted(i["id"] for i in as_family.get("/api/assets?limit=500").get_json()["items"])
    client = pi.app.test_client()
    client.post("/api/auth/login", json={"username": FAMILY[0], "password": FAMILY[1]})
    pi_ids = sorted(i["id"] for i in client.get("/api/assets?limit=500").get_json()["items"])
    assert pi_ids == mac_ids
    thumb = client.get(f"/api/thumb/{pi_ids[0]}")
    assert thumb.status_code == 200 and thumb.data
    original = client.get(f"/api/file/{pi_ids[0]}")
    assert original.status_code == 200


def test_the_pi_takes_the_family_settings(pi):
    assert pi.cfg.house_name == "Jagadeesh's house"
    assert pi.cfg.notify_smtp_password == ""


def test_the_family_signs_in_on_the_pi_and_changes_are_refused(pi):
    client = pi.app.test_client()
    signed = client.post("/api/auth/login", json={"username": FAMILY[0], "password": FAMILY[1]})
    assert signed.status_code == 200, signed.get_json()
    assert client.get("/api/me").get_json()["username"] == FAMILY[0]
    refused = client.post("/api/albums", json={"name": "Madurai trip"})
    assert refused.status_code == 503
    body = refused.get_json()
    assert body["replica"] is True and "Pi" in body["error"]
    assert refused.headers["Retry-After"] == "60"
    assert client.post("/api/auth/logout").status_code == 200


def test_the_phone_inbox_is_refused_on_the_pi(pi):
    assert pi.app.test_client().put("/dav/photo.jpg", data=b"x").status_code in (401, 503)


def test_a_new_handover_is_taken_up_without_signing_anybody_out(mac, pi):
    client = pi.app.test_client()
    client.post("/api/auth/login", json={"username": FAMILY[0], "password": FAMILY[1]})
    before = pi.replica.generation
    first_file = Path(pi.cfg.db_path)
    db.update_asset(mac.conn, mac.first, caption="Amma's 60th birthday")
    mac.conn.commit()
    publish(mac.cfg, mac.dest)
    assert pi.replica.take_newest()
    assert pi.replica.generation > before
    assert Path(pi.cfg.db_path) != first_file
    assert client.get("/api/me").get_json()["username"] == FAMILY[0]
    asset = client.get(f"/api/asset/{mac.first}").get_json()
    assert asset["caption"] == "Amma's 60th birthday"
    assert not pi.replica.take_newest()           # nothing newer


def test_a_damaged_handover_is_not_taken_up(mac, pi):
    served = pi.replica.generation
    manifest = publish(mac.cfg, mac.dest)
    manifest["index"]["sha256"] = "0" * 64
    (mac.dest / MANIFEST).write_text(json.dumps(manifest))
    with pytest.raises(ReplicaError, match="does not match"):
        pi.replica.take_newest()
    assert pi.replica.generation == served
    assert pi.replica.look() is False and "does not match" in pi.replica.error


def test_a_handover_from_a_newer_ninaivu_is_refused(mac, pi):
    manifest = publish(mac.cfg, mac.dest)
    manifest["schema_version"] = db.SCHEMA_VERSION + 1
    (mac.dest / MANIFEST).write_text(json.dumps(manifest))
    with pytest.raises(ReplicaError, match="Update Ninaivu on the Pi"):
        pi.replica.take_newest()


def test_the_pi_starts_again_on_what_it_had(mac, pi, tmp_path):
    served = pi.replica.generation
    (mac.dest / MANIFEST).unlink()
    again = Replica(pi.cfg, mac.dest)
    assert again.resume()
    assert again.generation == served
    with pytest.raises(ReplicaError, match="No hand-over"):
        again.take_newest()


def test_sessions_of_removed_accounts_are_not_carried(tmp_path):
    for name in ("old.db", "new.db"):
        conn = db.init_db(tmp_path / name)
        auth.init_auth_schema(conn)
    old = db.connect(tmp_path / "old.db")
    keep = auth.create_user(old, "maya", "summerdays24", role=auth.ROLE_FAMILY)
    gone = auth.create_user(old, "ravi", "summerdays25", role=auth.ROLE_FAMILY)
    auth.start_session(old, keep.id)
    auth.start_session(old, gone.id)
    new = db.connect(tmp_path / "new.db")
    same = auth.create_user(new, "maya", "summerdays24", role=auth.ROLE_FAMILY)
    # The same account, as a hand-over carries it: made at the same moment.
    made = old.execute("SELECT created_at FROM users WHERE id=?", (keep.id,)).fetchone()[0]
    new.execute("UPDATE users SET created_at=? WHERE id=?", (made, same.id))
    new.commit()
    db.close_all()
    assert carry_sessions(tmp_path / "old.db", tmp_path / "new.db") == 1


def test_a_thread_lets_go_of_a_retired_index(tmp_path):
    first = db.init_db(tmp_path / "a.db")
    db.retire(tmp_path / "a.db")
    db.connect(tmp_path / "b.db")
    with pytest.raises(sqlite3.ProgrammingError):
        first.execute("SELECT 1")


def test_the_pi_page_says_it_is_the_copy(pi):
    page = pi.app.test_client().get("/")
    assert page.status_code == 200
    assert b'id="replica-banner"' in page.data


def test_the_mac_page_has_no_such_notice(client):
    assert b'id="replica-banner"' not in client.get("/").data


# -- the front door ----------------------------------------------------------------

class _Awake:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_pages_go_to_the_mini_while_it_is_awake(pi):
    pi.cfg.replica_primary_url = "http://mini.tail1234.ts.net/"
    client = pi.app.test_client()
    assert pi.front.look() is False                 # the opener says asleep
    assert client.get("/").status_code == 200
    pi.front._open = lambda url, timeout=None: _Awake()
    assert pi.front.look() is True
    sent = client.get("/share/abc?x=1")
    assert sent.status_code == 307
    assert sent.headers["Location"] == "http://mini.tail1234.ts.net/share/abc?x=1"
    assert sent.headers["Cache-Control"] == "no-store"
    # The API is answered here, so a page already open on the Pi carries on.
    assert client.get("/api/assets?limit=1").status_code == 200


def test_one_browser_can_stay_on_the_pi(pi):
    pi.cfg.replica_primary_url = "http://mini.local"
    pi.front._open = lambda url, timeout=None: _Awake()
    pi.front.look()
    client = pi.app.test_client()
    chose = client.get("/?pi=1")
    assert chose.status_code == 303 and chose.headers["Location"].endswith("/")
    assert client.get("/").status_code == 200
    client.get("/?pi=0")
    assert client.get("/").status_code == 307


def test_the_magic_packet():
    packet = magic_packet("3c:22:fb:12:34:56")
    assert len(packet) == 102 and packet[:6] == b"\xff" * 6
    assert packet[6:12] == bytes.fromhex("3c22fb123456")
    assert magic_packet("3C-22-FB-12-34-56") == packet
    with pytest.raises(ValueError):
        magic_packet("not-a-mac")


def test_a_refused_change_wakes_the_mini_once_a_minute(pi):
    sent = []
    pi.cfg.replica_wake_mac = "3c:22:fb:12:34:56"
    pi.front._send = sent.append
    client = pi.app.test_client()
    # Nobody signed in: no wake-up for a passer-by.
    assert client.post("/api/albums", json={"name": "x"}).get_json()["waking"] is False
    client.post("/api/auth/login", json={"username": FAMILY[0], "password": FAMILY[1]})
    body = client.post("/api/albums", json={"name": "x"}).get_json()
    assert body["waking"] is True and "wake up" in body["error"]
    assert client.post("/api/replica/wake").get_json() == {"woken": True}
    assert len(sent) == 1


def test_the_status_says_how_old_the_copy_is(pi):
    client = pi.app.test_client()
    # A passer-by learns only that this is the Pi, not where the Mac is.
    assert client.get("/api/replica").get_json() == {"replica": True}
    client.post("/api/auth/login", json={"username": FAMILY[0], "password": FAMILY[1]})
    status = client.get("/api/replica").get_json()
    assert status["replica"] is True and status["as_of"] and status["primary_awake"] is False


# -- the Pi's service ---------------------------------------------------------------

def test_the_unit_mounts_each_library_read_only_at_the_macs_path(mac, tmp_path, capsys):
    root = Path(mac.cfg.active_root)
    (root / ".ninaivu-library").write_text(json.dumps({"id": "lib-123"}))
    (Path(mac.cfg.state_dir) / "library-ids.json").write_text(json.dumps({str(root): "lib-123"}))
    publish(mac.cfg, mac.dest)
    mirror = tmp_path / "mirror"
    copy = mirror / "ninaivu" / "Ninaivu Archive"
    copy.mkdir(parents=True)
    (copy / ".ninaivu-library").write_text(json.dumps({"id": "lib-123"}))
    code = replica_cli.main(["unit", "--from", str(mac.dest), "--mirror", str(mirror),
                             "--primary", "http://mini.local", "--wake", "3c:22:fb:12:34:56"])
    assert code == 0
    out = capsys.readouterr()
    quote = replica_cli.systemd_quote
    assert f"BindReadOnlyPaths={quote(str(copy))}:{quote(str(root))}" in out.out
    assert "ProtectSystem=strict" in out.out and "--replica-primary http://mini.local" in out.out
    assert "sudo mkdir -p" in out.err


def test_the_unit_asks_for_a_library_it_cannot_find(mac, tmp_path, capsys):
    publish(mac.cfg, mac.dest)
    (tmp_path / "mirror").mkdir()
    code = replica_cli.main(["unit", "--from", str(mac.dest), "--mirror", str(tmp_path / "mirror")])
    assert code == 2 and "--map" in capsys.readouterr().err


def test_systemd_quoting():
    assert replica_cli.systemd_quote('/Volumes/J Apple/100% "x"') == \
        '"/Volumes/J Apple/100%% \\"x\\""'


def test_the_server_will_not_start_without_a_handover(tmp_path, capsys):
    from ninaivu import __main__ as main_mod
    cfg = _pi_config(tmp_path, tmp_path / "empty")
    args = main_mod.build_parser().parse_args(["--replica-of", str(tmp_path / "empty")])
    assert main_mod.take_up_replica(cfg, args) is False
    assert "No hand-over" in capsys.readouterr().err


def test_the_server_becomes_a_replica(mac, tmp_path):
    from ninaivu import __main__ as main_mod
    publish(mac.cfg, mac.dest)
    cfg = _pi_config(tmp_path, mac.dest)
    args = main_mod.build_parser().parse_args(
        ["--replica-of", str(mac.dest), "--replica-primary", "http://mini.local"])
    assert main_mod.take_up_replica(cfg, args) is True
    assert args.no_admin is True and args.name == "ninaivu-pi"
    assert cfg.replica_primary_url == "http://mini.local" and cfg.watch is False
    assert Path(cfg.db_path).name.startswith("index-")
    db.close_all()
    assert front_mod.OPEN_WRITES >= {"/api/auth/login", "/api/auth/logout"}


def test_a_handover_is_never_written_into_the_library(mac):
    inside = Path(mac.cfg.active_root) / "2023"
    with pytest.raises(OSError, match="inside the library"):
        publish(mac.cfg, inside)
    assert not (inside / MANIFEST).exists()


def test_films_play_on_the_pi_and_listing_them_writes_nothing(mac, people, tmp_path):
    """Audit C-06: films were listed on the Pi but not handed over, so playing
    one gave 404; and listing them marked one being made on the Mac as failed,
    a write the Pi is never meant to make."""
    from ninaivu.storage import films as film_store
    ids = [r["id"] for r in mac.conn.execute("SELECT id FROM assets ORDER BY id LIMIT 2")]
    owner = people["family"].id
    done = film_store.create(mac.conn, owner_id=owner, title="Madurai", subtitle="",
                             source_kind="album", source_id=None, size="landscape",
                             subtitles="none", music_id=None, item_ids=ids, story_ids=[])
    folder = film_store.films_dir(mac.cfg.state_dir)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "film-1-abc.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 64)
    (folder / ".film-2-def.mp4.part").write_bytes(b"half a film")
    film_store.set_state(mac.conn, done, "done", file="film-1-abc.mp4", size_bytes=80,
                         seconds=3.0)
    building = film_store.create(mac.conn, owner_id=owner, title="Still being made",
                                 subtitle="", source_kind="album", source_id=None,
                                 size="landscape", subtitles="none", music_id=None,
                                 item_ids=ids, story_ids=[])
    publish(mac.cfg, mac.dest)
    assert not (mac.dest / "films" / ".film-2-def.mp4.part").exists(), "half a film stays"
    cfg = _pi_config(tmp_path, mac.dest)
    cfg.memory_films = True
    replica = Replica(cfg, mac.dest)
    assert replica.take_newest()
    services = Services(cfg)
    services.replica = replica
    services.replica_front = Front(cfg, replica, opener=_asleep, sender=lambda packet: None)
    application = create_home_app(services)
    try:
        client = application.test_client()
        client.post("/api/auth/login", json={"username": FAMILY[0], "password": FAMILY[1]})
        listed = {f["id"]: f for f in client.get("/api/films").get_json()["films"]}
        assert listed[building]["state"] == "building"
        played = client.get(f"/api/films/{done}/video")
        assert played.status_code == 200 and played.data.startswith(b"\x00\x00\x00\x18ftyp")
        with closing(_index(Path(cfg.db_path))) as conn:
            row = conn.execute("SELECT state FROM memory_films WHERE id=?",
                               (building,)).fetchone()
            assert row["state"] == "building", "the Pi wrote to its index"
    finally:
        services.stop(timeout=5.0)
