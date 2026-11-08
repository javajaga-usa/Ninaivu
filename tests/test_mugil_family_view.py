"""The family view served by the Secondary's own Ninaivu (mugil/family_view.py).

Two computers in one process, as tests/test_replica.py and
tests/test_mugil_two_computers.py fake them: the app's library is the
Primary's, and a second Config in a folder of its own is the Secondary, with
the originals kept by its own Holding and the hand-over sent the way the
Primary sends it. No systemd unit, no --map and no bind mount: the
Secondary's paths are its own.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import FAMILY
from ninaivu import Services, create_home_app
from ninaivu.mugil import family_view, handover, pairing
from ninaivu.mugil.hold import Holding, hasher
from ninaivu.server.config import Config
from ninaivu.storage import fingerprints, library_id
from test_replica import _Awake, _asleep, publish
from test_replica import mac  # noqa: F401  (fixture)


def _secondary_config(tmp_path: Path) -> Config:
    cfg = Config()
    cfg.state_dir = tmp_path / "secondary-state"
    cfg.ai_enabled = False
    cfg.ai_engine = "off"
    cfg.ai_models_dir = str(tmp_path / "secondary-models")
    cfg.watch = False
    cfg.workers = 1
    cfg.roots = []
    cfg.active_root = ""
    cfg.mugil_role = "secondary"
    cfg.mugil_originals_dir = str(tmp_path / "n300" / "originals")
    Path(cfg.mugil_originals_dir).mkdir(parents=True)
    cfg.ensure_dirs()
    return cfg


def _keep_originals(primary_cfg, holding: Holding) -> dict[str, str]:
    """Every original of the Primary kept on the Secondary, as the courier
    leaves them; and the Primary's record of its library ids."""
    known = {}
    for root in primary_cfg.roots:
        library = library_id.ensure_marker(root) or library_id.read_id(root)
        known[root] = library
        for path in sorted(Path(root).rglob("*")):
            if not path.is_file() or path.name.startswith("."):
                continue
            data = path.read_bytes()
            fresh = hasher(fingerprints.ALGO)
            fresh.update(data)
            with open(path, "rb") as body:
                holding.receive(library, Path(root).name, path.relative_to(root).as_posix(),
                                size=len(data), algo=fingerprints.ALGO,
                                digest=fresh.hexdigest(), mtime=path.stat().st_mtime, body=body)
    (Path(primary_cfg.state_dir) / library_id.KNOWN).write_text(json.dumps(known))
    return known


def _snapshot(folder: Path) -> dict[str, tuple[int, float, str]]:
    return {p.relative_to(folder).as_posix(): (p.stat().st_size, p.stat().st_mtime,
                                               hashlib.sha256(p.read_bytes()).hexdigest())
            for p in sorted(folder.rglob("*")) if p.is_file()}


@pytest.fixture()
def two(mac, tmp_path):  # noqa: F811
    """The Primary (the app) and the Secondary (a Services of its own),
    paired, the originals kept on the Secondary, one hand-over sent."""
    mac.cfg.port = 80
    sec = _secondary_config(tmp_path)
    pairing.save(sec.state_dir, {"key": "ab" * 32, "role": "secondary",
                                 "partner_address": "192.168.1.5",
                                 "partner_url": "http://192.168.1.5:3000"})
    holding = Holding(sec.mugil_originals_dir)
    known = _keep_originals(mac.cfg, holding)
    publish(mac.cfg, handover.folder(sec.state_dir))
    services = Services(sec)
    view = services.mugil_family_view
    view._opener = _asleep                                       # noqa: SLF001
    app = create_home_app(services)
    yield SimpleNamespace(mac=mac, cfg=sec, services=services, view=view, app=app,
                          holding=holding, known=known)
    view.stop()
    for part in (view.front,):
        if part is not None:
            part.stop()
    services.stop(timeout=5.0)


def _signed_in(app):
    client = app.test_client()
    answer = client.post("/api/auth/login", json={"username": FAMILY[0], "password": FAMILY[1]})
    assert answer.status_code == 200, answer.get_json()
    return client


# -- serving it ------------------------------------------------------------------

def test_the_secondary_serves_the_family_view_from_its_own_process(two, as_family):
    assert two.view.look() is True and not two.view.error
    mac_ids = sorted(i["id"] for i in as_family.get("/api/assets?limit=500").get_json()["items"])
    client = _signed_in(two.app)
    ids = sorted(i["id"] for i in client.get("/api/assets?limit=500").get_json()["items"])
    assert ids == mac_ids
    assert client.get(f"/api/thumb/{ids[0]}").status_code == 200
    assert client.get("/api/replica").get_json()["replica"] is True


def test_the_photographs_come_from_the_secondarys_own_folder(two):
    """Re-rooted through the Secondary's record of each library, not a bind
    mount: with the Primary's library gone, every original still opens."""
    assert two.view.look() is True
    folders = two.holding.library_folders()
    for root in list(two.mac.cfg.roots):
        shutil.move(root, f"{root}-away")
    client = _signed_in(two.app)
    items = client.get("/api/assets?limit=500").get_json()["items"]
    assert items
    roots = set(two.view.view_cfg.roots)
    assert roots == {str(folders[library]) for library in two.known.values()}
    with closing(sqlite3.connect(str(two.view.view_cfg.db_path))) as conn:
        assert {r[0] for r in conn.execute("SELECT DISTINCT root FROM assets")} <= roots
    for item in items[:5]:
        answer = client.get(f"/api/file/{item['id']}")
        assert answer.status_code == 200, item


def test_nothing_on_the_originals_disk_changes(two):
    before = _snapshot(Path(two.cfg.mugil_originals_dir))
    assert two.view.look() is True
    client = _signed_in(two.app)
    for item in client.get("/api/assets?limit=500").get_json()["items"][:5]:
        client.get(f"/api/file/{item['id']}")
    client.post("/api/albums", json={"name": "Madurai trip"})
    assert _snapshot(Path(two.cfg.mugil_originals_dir)) == before


def test_changes_are_refused_but_signing_in_and_out(two):
    assert two.view.look() is True
    client = _signed_in(two.app)
    refused = client.post("/api/albums", json={"name": "Madurai trip"})
    assert refused.status_code == 503 and refused.get_json()["replica"] is True
    assert client.post("/api/auth/logout").status_code == 200


def test_while_the_primary_is_awake_visitors_go_to_it(two):
    assert two.view.look() is True
    client = two.app.test_client()
    assert two.view.front.look() is False
    assert client.get("/").status_code == 200
    two.view.front._open = lambda url, timeout=None: _Awake()     # noqa: SLF001
    assert two.view.front.look() is True
    sent = client.get("/share/abc?x=1")
    assert sent.status_code == 307
    assert sent.headers["Location"] == "http://192.168.1.5/share/abc?x=1"


def test_the_secondarys_own_routes_stay_its_own(two):
    assert two.view.look() is True
    client = two.app.test_client()
    # The phone inbox: the Secondary's own, which asks for a key; the view
    # would have refused it as a change.
    assert client.put("/dav/photo.jpg", data=b"x").status_code == 401
    # Between the two computers: the Secondary's own answer, signed or not.
    assert client.get("/api/mugil/hello").status_code != 404
    assert client.get("/healthz").status_code == 200


def test_without_a_pairing_or_a_handover_the_secondary_is_itself(two):
    pairing.forget(two.cfg.state_dir)
    two.view._wanted = (0.0, False)                               # noqa: SLF001
    assert two.view.look() is False and two.view.app is None
    assert two.app.test_client().get("/api/replica").status_code == 404


def test_a_single_computer_never_serves_one(cfg, scanned):
    services = Services(cfg)
    assert services.mugil_family_view.look() is False
    assert services.mugil_family_view.app is None


def test_a_newer_handover_is_taken_up_and_rerooted(two):
    assert two.view.look() is True
    first = two.view.replica.generation
    publish(two.mac.cfg, handover.folder(two.cfg.state_dir))
    assert two.view.look() is True
    assert two.view.replica.generation > first
    roots = set(two.view.view_cfg.roots)
    with closing(sqlite3.connect(str(two.view.view_cfg.db_path))) as conn:
        assert {r[0] for r in conn.execute("SELECT DISTINCT root FROM assets")} <= roots


def test_after_a_restart_the_copy_taken_up_is_served_again(two):
    assert two.view.look() is True
    served = two.view.replica.generation
    again = family_view.FamilyView(two.cfg, opener=_asleep)
    try:
        assert again.look() is True
        assert again.replica.generation == served
        assert again.view_cfg.roots == two.view.view_cfg.roots
    finally:
        again.stop()


def test_rerooting_rewrites_every_table_that_names_a_library(tmp_path):
    index = tmp_path / "index.db"
    with closing(sqlite3.connect(str(index))) as conn:
        conn.execute("CREATE TABLE assets (root TEXT, rel_path TEXT, UNIQUE(root, rel_path))")
        conn.execute("CREATE TABLE occasions (root TEXT)")
        conn.execute("CREATE TABLE unrelated (root TEXT)")
        conn.executemany("INSERT INTO assets VALUES (?, ?)", [("/Volumes/A", "x.jpg"),
                                                             ("/Volumes/B", "y.jpg")])
        conn.execute("INSERT INTO occasions VALUES ('/Volumes/A')")
        conn.execute("INSERT INTO unrelated VALUES ('/Volumes/A')")
        conn.commit()
    assert family_view.reroot_index(index, {"/Volumes/A": "/srv/originals/A"}) == 2
    with closing(sqlite3.connect(str(index))) as conn:
        assert conn.execute("SELECT root FROM assets ORDER BY rel_path").fetchall() == [
            ("/srv/originals/A",), ("/Volumes/B",)]
        assert conn.execute("SELECT root FROM unrelated").fetchone() == ("/Volumes/A",)
