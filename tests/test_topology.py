"""Two one-computer Ninaivus becoming a pair, with a plan beforehand and a
proof afterwards that every file seen before still exists somewhere.

On the household's own: a Mac (the Primary) and a Linux PC whose N300 holds
NinaivuMaster/Ninaivu Archive, with files imported only there. Each computer
here is a state folder and a library in a temporary folder; the Primary asks
the Secondary over a fake of the signed peer API that calls the Secondary's
own Topology, as the routes do.
"""

import gzip
import json
import os
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from ninaivu.mugil import pairing, topology
from ninaivu.mugil.courier import PartnerError
from test_mugil_two_computers import two_apps  # noqa: F401
from ninaivu.mugil.topology import (MOVE_PRIMARY, PAIR, REPLACE_SECONDARY_DISK, UNPAIR, Standing,
                                    Topology, after, why_not)

WHEN = 1_700_000_000.0

INDEX = """
CREATE TABLE assets (id INTEGER PRIMARY KEY, root TEXT, rel_path TEXT, size INTEGER,
                     mtime REAL, trashed INTEGER DEFAULT 0);
CREATE TABLE content_fingerprints (asset_id INTEGER PRIMARY KEY, algo TEXT, digest TEXT,
                                   size INTEGER, mtime REAL, taken_at REAL DEFAULT 0);
CREATE TABLE recycled (root TEXT, rel_path TEXT, restored_at REAL);
CREATE TABLE erased (root TEXT, rel_path TEXT, size INTEGER);
"""


def put(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (WHEN, WHEN))
    return path


def computer(tmp_path: Path, name: str, roots: list[Path]) -> SimpleNamespace:
    state = tmp_path / f"{name}-state"
    state.mkdir()
    cfg = SimpleNamespace(state_dir=state, roots=[str(r) for r in roots], mugil_role="single",
                          mugil_originals_dir="", mugil_partner_url="",
                          ignore_dirs={"_deleted", ".ninaivu"}, house_name=name,
                          db_path=state / "index.db")
    conn = sqlite3.connect(cfg.db_path)
    conn.executescript(INDEX)
    for root in roots:
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root).as_posix()
            if path.is_file() and not rel.startswith("_deleted/"):
                conn.execute("INSERT INTO assets (root, rel_path, size, mtime) VALUES (?,?,?,?)",
                             (str(root), rel, path.stat().st_size, path.stat().st_mtime))
    conn.commit()
    conn.close()
    return cfg


def topo(cfg, partner=None) -> Topology:
    made = Topology(cfg, lambda: sqlite3.connect(cfg.db_path), wait=0.01,
                    **({"partner": partner} if partner else {}))
    return made


def finish(t: Topology) -> dict:
    t._thread.join(timeout=60)
    status = t.status()
    assert not status.get("error"), status
    return status


def through(other: Topology):
    """The Secondary's peer routes, without the network."""
    class Fake:
        def __init__(self, url, key, timeout=None):
            assert key == "the-pairing-key"

        def request(self, method, path, query=None, body=None):
            assert path == "/api/mugil/census"
            if method == "POST":
                try:
                    return other.take_census()
                except topology.Refused as exc:
                    raise PartnerError(str(exc), 409) from exc
            return other.status()

        def download(self, path, query, write):
            data = (other.state_dir / query["name"]).read_bytes()
            write(data)
            return len(data)
    return Fake


def pair(mac, linux, n300: Path) -> None:
    """What the household does: the Linux PC keeps its library as the
    originals (adopt), and the two are paired."""
    linux.mugil_role, linux.mugil_originals_dir, linux.roots = "secondary", str(n300), []
    mac.mugil_role, mac.mugil_partner_url = "primary", "http://linux-pc"
    pairing.save(mac.state_dir, {"key": "the-pairing-key", "role": "primary"})
    pairing.save(linux.state_dir, {"key": "the-pairing-key", "role": "secondary"})


@pytest.fixture()
def apart(tmp_path):
    """Two computers that ran apart, each with its census taken first."""
    mac_lib = tmp_path / "Volumes" / "JApple Extreme SSD" / "NinaivuMaster" / "Ninaivu Archive"
    n300 = tmp_path / "media" / "jaga" / "Toshiba" / "NinaivuMaster"
    lin_lib, stray = n300 / "Ninaivu Archive", n300 / "From the phone"
    put(mac_lib / "2026/11/01/mac.jpg", b"\xff\xd8 on the mac only")
    put(mac_lib / "2026/11/02/same.jpg", b"\xff\xd8 on both")
    put(mac_lib / "2026/11/03/IMG_1.jpg", b"\xff\xd8 the mac's version")
    put(mac_lib / "_deleted/2026-10-01/binned-on-mac.jpg", b"\xff\xd8 in the mac's bin")
    put(lin_lib / "2026/11/02/same.jpg", b"\xff\xd8 on both")
    put(lin_lib / "2026/11/03/IMG_1.jpg", b"\xff\xd8 the linux pc's own edit")
    put(lin_lib / "2026/11/04/linux-only.jpg", b"\xff\xd8 imported on the linux pc")
    put(lin_lib / "2026/11/04/linux-only.jpg.xmp", b"<x:xmpmeta/>")
    put(lin_lib / "2025/01/01/deleted-on-mac.jpg", b"\xff\xd8 deleted on the mac")
    put(lin_lib / "_deleted/2026-10-01/old.jpg", b"\xff\xd8 in the linux pc's bin")
    put(lin_lib / ".DS_Store", b"junk")
    put(stray / "IMG_9.jpg", b"\xff\xd8 not in a library")
    mac = computer(tmp_path, "Mac mini", [mac_lib])
    linux = computer(tmp_path, "Linux PC", [lin_lib, stray])
    conn = sqlite3.connect(mac.db_path)
    conn.execute("INSERT INTO recycled VALUES (?,?,NULL)", (str(mac_lib), "2025/01/01/deleted-on-mac.jpg"))
    conn.commit()
    conn.close()
    for cfg in (mac, linux):
        t = topo(cfg)
        t.take_census()
        finish(t)
    pair(mac, linux, n300)
    secondary = topo(linux)
    primary = topo(mac, through(secondary))
    return SimpleNamespace(mac=mac, linux=linux, mac_lib=mac_lib, lin_lib=lin_lib, n300=n300,
                           primary=primary, secondary=secondary)


def planned(world) -> dict[str, dict]:
    world.primary.plan(PAIR)
    status = finish(world.primary)
    assert status["record"]["state"] == "planned", status
    page = world.primary.plan_page(limit=100)
    return {Path(i["file"]).name + ("@mac" if i["on"] == "primary" else ""): i
            for i in page["items"]}


# -- the census ------------------------------------------------------------------

def test_a_census_lists_every_file_with_its_fingerprint(apart):
    census = topology.newest(apart.linux.state_dir)
    assert census["role"] == "single" and census["paired"] is False
    lines = list(topology.entries(apart.linux.state_dir / census["file"]))
    paths = sorted(e["p"] for e in lines)
    assert "2026/11/04/linux-only.jpg" in paths and "_deleted/2026-10-01/old.jpg" in paths
    assert "IMG_9.jpg" in paths and ".DS_Store" not in paths
    assert all(e["d"] for e in lines)
    assert {e["a"] for e in lines if e["p"].startswith("_deleted/")} == {"recycle"}
    whole = topology.check_census(apart.linux.state_dir / census["file"])
    assert whole["files"] == len(lines) == 7


def test_a_census_cut_short_is_never_taken_for_a_whole_one(apart, tmp_path):
    census = apart.linux.state_dir / topology.newest(apart.linux.state_dir)["file"]
    lines = gzip.decompress(census.read_bytes()).decode().splitlines()
    cut = tmp_path / "cut.jsonl.gz"
    cut.write_bytes(gzip.compress("\n".join(lines[:-1]).encode()))
    with pytest.raises(ValueError):
        topology.check_census(cut)


# -- the plan ----------------------------------------------------------------------

def test_a_file_only_on_the_secondary_is_brought_in(apart):
    plan = planned(apart)
    assert plan["linux-only.jpg"]["outcome"] == "brought_in"
    assert plan["linux-only.jpg"]["to"] == "Ninaivu Archive/2026/11/04/linux-only.jpg"
    assert plan["mac.jpg@mac"]["outcome"] == "sent"
    assert plan["same.jpg"]["outcome"] == plan["same.jpg@mac"]["outcome"] == "both"


def test_a_differing_version_of_the_same_name_is_kept_beside(apart):
    plan = planned(apart)
    assert plan["IMG_1.jpg"]["outcome"] == "beside"
    assert plan["IMG_1.jpg"]["to"] == "Ninaivu Archive/2026/11/03/IMG_1 (from Linux PC).jpg"


def test_a_file_deleted_on_the_primary_is_left_alone(apart):
    plan = planned(apart)
    assert plan["deleted-on-mac.jpg"]["outcome"] == "left_alone"
    assert plan["deleted-on-mac.jpg"]["reason"] == "deleted_on_primary"


def test_a_file_in_the_secondarys_recycle_bin_is_left_alone(apart):
    plan = planned(apart)
    assert (plan["old.jpg"]["outcome"], plan["old.jpg"]["reason"]) == ("left_alone", "recycle_bin")
    assert plan["IMG_9.jpg"]["reason"] == "outside_library"
    assert plan["binned-on-mac.jpg@mac"]["reason"] == "recycle_bin"
    totals = apart.primary.status()["record"]["plan"]
    assert totals["secondary"]["brought_in"]["files"] == 2          # the photograph and its sidecar
    assert totals["reasons"]["recycle_bin"]["files"] == 2


def test_the_plan_needs_a_census_taken_before_pairing(apart):
    for census in topology.censuses(apart.mac.state_dir):
        (apart.mac.state_dir / census["file"]).unlink()
    with pytest.raises(topology.Refused):
        apart.primary.plan(PAIR)


def test_the_plan_changes_nothing_on_either_computer(apart):
    def snapshot():
        return {p: p.read_bytes() for root in (apart.mac_lib, apart.n300)
                for p in root.rglob("*") if p.is_file()}
    before = snapshot()
    planned(apart)
    assert snapshot() == before


# -- the proof -----------------------------------------------------------------------

def bring_in(world) -> None:
    """What Bring them in does, as the plan says it will."""
    (world.mac_lib / "2026/11/04").mkdir(parents=True, exist_ok=True)
    for rel in ("2026/11/04/linux-only.jpg", "2026/11/04/linux-only.jpg.xmp"):
        shutil.copy2(world.lin_lib / rel, world.mac_lib / rel)
    shutil.copy2(world.lin_lib / "2026/11/03/IMG_1.jpg",
                 world.mac_lib / "2026/11/03/IMG_1 (from Linux PC).jpg")


def test_a_proof_that_passes_marks_the_pairing_done(apart):
    planned(apart)
    bring_in(apart)
    apart.primary.prove()
    record = finish(apart.primary)["record"]
    assert record["state"] == "done", record
    assert record["proof"]["passed"] is True and record["proof"]["missing_count"] == 0
    assert record["proof"]["checked"] == 11
    with pytest.raises(topology.Refused):
        apart.primary.prove()                        # done is done


def test_a_proof_that_fails_names_the_file(apart):
    planned(apart)
    bring_in(apart)
    gone = apart.lin_lib / "2025/01/01/deleted-on-mac.jpg"
    gone.unlink()                                    # only the Secondary had it
    apart.primary.prove()
    record = finish(apart.primary)["record"]
    assert record["state"] == "failed"
    assert record["proof"]["passed"] is False
    assert [m["file"] for m in record["proof"]["missing"]] == [str(gone)]
    with gzip.open(apart.mac.state_dir / record["missing_file"], "rt") as handle:
        assert [json.loads(line)["file"] for line in handle] == [str(gone)]
    # Put back, the proof can be run again, and passes.
    put(gone, b"\xff\xd8 deleted on the mac")
    apart.primary.prove()
    assert finish(apart.primary)["record"]["state"] == "done"


def test_a_file_moved_but_kept_still_counts(apart):
    planned(apart)
    bring_in(apart)
    # The Secondary's only copy moved within its disk: same bytes, new place.
    moved = apart.n300 / "Ninaivu Archive/2026/kept-elsewhere.jpg"
    shutil.move(apart.lin_lib / "2025/01/01/deleted-on-mac.jpg", moved)
    apart.primary.prove()
    assert finish(apart.primary)["record"]["state"] == "done"


def test_a_disk_away_in_the_census_before_never_passes(apart, tmp_path):
    first = topology.newest(apart.mac.state_dir)["file"]
    header = topology.read_header(apart.mac.state_dir / first)
    header["tops"][0]["away"] = True
    lines = gzip.decompress((apart.mac.state_dir / first).read_bytes()).decode().splitlines()
    away = tmp_path / "away.jsonl.gz"
    away.write_bytes(gzip.compress("\n".join([json.dumps(header), lines[-1]]).encode()))
    result = topology.prove([away], [apart.mac.state_dir / first])
    assert result["passed"] is False and result["away_before"]


def test_the_proof_waits_until_the_change_is_carried_out(apart):
    planned(apart)
    pairing.forget(apart.mac.state_dir)              # not where pairing leaves it
    with pytest.raises(topology.Refused):
        apart.primary.prove()


# -- the transitions -------------------------------------------------------------------

@pytest.mark.parametrize("transition, here, part, ends", [
    (PAIR, Standing("single", False), "primary", Standing("primary", True)),
    (PAIR, Standing("single", False), "secondary", Standing("secondary", True)),
    (PAIR, Standing("secondary", False), None, Standing("secondary", True)),
    (MOVE_PRIMARY, Standing("primary", True), None, Standing("single", False)),
    (MOVE_PRIMARY, Standing("single", False), None, Standing("primary", True)),
    (REPLACE_SECONDARY_DISK, Standing("secondary", True), None, Standing("secondary", True)),
    (REPLACE_SECONDARY_DISK, Standing("primary", True), None, Standing("primary", True)),
    (UNPAIR, Standing("primary", True), None, Standing("primary", False)),
    (UNPAIR, Standing("secondary", True), None, Standing("secondary", False)),
])
def test_where_each_transition_leaves_a_computer(transition, here, part, ends):
    assert why_not(transition, here, part) is None
    assert after(transition, here, part) == ends


@pytest.mark.parametrize("transition, here, part", [
    (PAIR, Standing("primary", True), None),          # paired already
    (PAIR, Standing("single", False), None),          # which part?
    (PAIR, Standing("primary", False), "secondary"),  # its part is chosen
    (MOVE_PRIMARY, Standing("secondary", True), None),
    (MOVE_PRIMARY, Standing("primary", False), None),
    (REPLACE_SECONDARY_DISK, Standing("single", False), None),
    (REPLACE_SECONDARY_DISK, Standing("secondary", False), None),
    (UNPAIR, Standing("single", False), None),
    (UNPAIR, Standing("primary", False), None),
    ("swap", Standing("single", False), None),
])
def test_a_transition_from_the_wrong_place_is_refused(transition, here, part):
    assert why_not(transition, here, part)
    with pytest.raises(ValueError):
        after(transition, here, part)


def test_only_the_paired_primary_makes_a_plan(apart):
    with pytest.raises(topology.Refused):
        apart.secondary.plan(PAIR)
    with pytest.raises(topology.Refused):
        apart.secondary.plan(UNPAIR)                 # the Primary plans it
    with pytest.raises(topology.Refused):
        apart.primary.plan("swap")


def test_replacing_the_secondary_disk_lists_what_only_it_holds(apart):
    apart.primary.plan(REPLACE_SECONDARY_DISK)
    record = finish(apart.primary)["record"]
    assert record["state"] == "planned"
    only = sorted(Path(m["file"]).name for m in record["plan"]["named"])
    assert only == ["IMG_1.jpg", "IMG_9.jpg", "deleted-on-mac.jpg", "linux-only.jpg",
                    "linux-only.jpg.xmp", "old.jpg"]


# -- unpairing: each computer keeps its part, and proves it ------------------------------

def unpaired_plan(world) -> dict[str, dict]:
    world.primary.plan(UNPAIR)
    record = finish(world.primary)["record"]
    assert record["state"] == "planned" and record["to"] == {"role": "primary", "paired": False}
    page = world.primary.plan_page(limit=100)
    return {Path(i["file"]).name + ("@mac" if i["on"] == "primary" else ""): i
            for i in page["items"]}


def test_unpairing_plans_what_each_computer_keeps(apart):
    plan = unpaired_plan(apart)
    assert (plan["mac.jpg@mac"]["outcome"], plan["mac.jpg@mac"]["reason"]) == ("kept", "only_on_primary")
    assert plan["same.jpg"]["outcome"] == plan["same.jpg@mac"]["outcome"] == "both"
    assert (plan["linux-only.jpg"]["outcome"], plan["linux-only.jpg"]["reason"]) == (
        "kept", "only_on_secondary")
    totals = apart.primary.status()["record"]["plan"]
    assert totals["primary"]["both"]["files"] == 1 and totals["secondary"]["kept"]["files"] == 6


def test_after_unpairing_each_computer_proves_its_own_side(apart):
    unpaired_plan(apart)
    assert apart.primary.status()["can_prove"] is False           # still paired
    for cfg in (apart.mac, apart.linux):
        pairing.forget(cfg.state_dir)
    assert apart.primary.status()["can_prove"] and apart.secondary.status()["can_prove"]
    gone = apart.lin_lib / "2026/11/04/linux-only.jpg"
    gone.unlink()                                    # lost on the Secondary's side
    for side in (apart.primary, apart.secondary):
        side.prove()
    mac, linux = finish(apart.primary)["record"], finish(apart.secondary)["record"]
    assert mac["state"] == "done" and mac["proof"]["checked"] == 4
    assert linux["transition"] == "unpair" and linux["state"] == "failed"
    assert [m["file"] for m in linux["proof"]["missing"]] == [str(gone)]
    put(gone, b"\xff\xd8 imported on the linux pc")
    apart.secondary.prove()
    assert finish(apart.secondary)["record"]["state"] == "done"
    with pytest.raises(topology.Refused):
        apart.secondary.prove()                      # done is done


# -- moving the Primary: the plan goes with the index -----------------------------------

def moved(world, tmp_path, monkeypatch, *, leave_behind: str | None = None):
    """Plan the move on the old Mac, export its index, copy its library to the
    new Mac by hand, and import the index there, as the household does."""
    from ninaivu.cli import backup_restore
    from ninaivu.storage import backup, full_export
    world.primary.plan(MOVE_PRIMARY)
    record = finish(world.primary)["record"]
    assert record["state"] == "planned" and topology.moving(world.mac.state_dir)
    (world.mac.state_dir / "config.json").write_text(json.dumps({"home_name": "Mac mini"}))
    bundle = backup.snapshot(world.mac.state_dir, tmp_path / "export", full=True)
    new_lib = tmp_path / "New Mac" / "NinaivuMaster" / "Ninaivu Archive"
    shutil.copytree(world.mac_lib, new_lib, copy_function=shutil.copy2)
    if leave_behind:
        (new_lib / leave_behind).unlink()
    new = computer(tmp_path, "New Mac", [new_lib])
    (new.state_dir / "config.json").write_text(json.dumps({"home_name": "New Mac"}))
    monkeypatch.setattr(backup_restore.Config, "load", lambda: SimpleNamespace(state_dir=new.state_dir))
    full_export.ask_for_import(new.state_dir, bundle)
    assert full_export.apply_pending(new.state_dir) is True
    new.mugil_role, new.mugil_partner_url = "primary", "http://linux-pc"
    return new, Topology(new, lambda: sqlite3.connect(new.db_path), wait=0.01,
                         partner=through(world.secondary)), bundle


def test_moving_the_primary_plans_what_must_go_with_the_index(apart):
    apart.primary.plan(MOVE_PRIMARY)
    record = finish(apart.primary)["record"]
    assert record["to"] == {"role": "single", "paired": False}
    page = apart.primary.plan_page(limit=100)
    plan = {Path(i["file"]).name + ("@mac" if i["on"] == "primary" else ""): i for i in page["items"]}
    assert (plan["mac.jpg@mac"]["outcome"], plan["mac.jpg@mac"]["reason"]) == (
        "carried", "carry_with_library")
    assert plan["same.jpg@mac"]["outcome"] == "both"
    assert plan["linux-only.jpg"]["outcome"] == "kept" and "reason" not in plan["linux-only.jpg"]
    assert record["plan"]["primary"]["carried"]["files"] == 3     # with the one in its bin
    # The old Primary is not where it is proved.
    with pytest.raises(topology.Refused, match="computer the index went to"):
        apart.primary.prove()


def test_the_export_carries_the_plan_and_the_new_primary_proves_it(apart, tmp_path, monkeypatch):
    new, there, bundle = moved(apart, tmp_path, monkeypatch)
    import tarfile
    with tarfile.open(bundle) as tar:
        names = {m.name for m in tar.getmembers()}
    assert "state/mugil-move/record.json" in names
    record = there.status()["record"]
    assert record["transition"] == "move_primary" and record["moved_in"] and record["state"] == "planned"
    assert not topology.moving(new.state_dir)                      # put in place, and gone
    assert there.status()["can_prove"]
    there.prove()
    record = finish(there)["record"]
    assert record["state"] == "done", record
    assert record["proof"]["passed"] and record["proof"]["checked"] == 11


def test_a_file_left_on_the_old_primary_fails_the_proof_by_name(apart, tmp_path, monkeypatch):
    new, there, _ = moved(apart, tmp_path, monkeypatch, leave_behind="2026/11/01/mac.jpg")
    there.prove()
    record = finish(there)["record"]
    assert record["state"] == "failed"
    assert [m["file"] for m in record["proof"]["missing"]] == [str(apart.mac_lib / "2026/11/01/mac.jpg")]


def test_an_export_without_a_plan_arrives_with_nothing_to_prove(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    assert topology.arrive(state) is None
    (state / topology.MOVING).mkdir()
    (state / topology.MOVING / "record.json").write_text(json.dumps(
        {"transition": "move_primary", "before": {"here": "../mugil-pair.json", "partner": "x"}}))
    assert topology.arrive(state) is None and topology.load_record(state) == {}


# -- the routes, between two whole Ninaivus ----------------------------------------------

def test_the_console_plans_and_proves_a_pairing_over_the_signed_api(two_apps, tmp_path):  # noqa: F811
    from conftest import ADMIN, login
    from test_mugil_two_computers import _paired
    primary_app, secondary_app, _ = two_apps
    for application in (primary_app, secondary_app):
        admin = login(application.test_client(), *ADMIN)
        assert admin.post("/api/admin/mugil/census", json={}).status_code == 200
        application.config["MV_SERVICES"].mugil_topology._thread.join(timeout=60)
        assert admin.get("/api/admin/mugil/topology").get_json()["censuses"][0]["role"] == "single"
    # Only the paired Primary may ask, and only signed.
    assert secondary_app.test_client().get("/api/mugil/census").status_code == 404
    first, services, _ = _paired(two_apps, tmp_path)
    assert secondary_app.test_client().get("/api/mugil/census").status_code == 401
    assert secondary_app.test_client().get(
        "/api/mugil/census/file?name=../mugil-pair.json").status_code == 401
    services.mugil_topology.wait = 0.05

    assert first.post("/api/admin/mugil/topology/plan", json={"transition": "swap"}).status_code == 409
    assert first.post("/api/admin/mugil/topology/plan", json={"transition": "pair"}).status_code == 200
    services.mugil_topology._thread.join(timeout=60)
    status = first.get("/api/admin/mugil/topology").get_json()
    assert status["record"]["state"] == "planned", status
    page = first.get("/api/admin/mugil/topology/plan?on=primary&limit=5").get_json()
    assert page["items"] and all(i["on"] == "primary" for i in page["items"])
    assert page["record"]["plan"]["primary"]

    assert first.post("/api/admin/mugil/topology/prove").status_code == 200
    services.mugil_topology._thread.join(timeout=120)
    record = first.get("/api/admin/mugil/topology").get_json()["record"]
    assert record["state"] == "done" and record["proof"]["passed"] is True, record
    assert first.post("/api/admin/mugil/topology/prove").status_code == 409


def test_only_an_admin_takes_a_census(app, people, as_family):
    assert as_family.post("/api/admin/mugil/census", json={}).status_code in (401, 403)
    assert as_family.get("/api/admin/mugil/topology").status_code in (401, 403)


def test_a_census_request_reaches_only_a_census_kept_here(tmp_path):
    from ninaivu.mugil.topology import census_file
    state = tmp_path / "state"
    state.mkdir()
    kept = state / "mugil-census-20261108-120000.jsonl.gz"
    kept.write_bytes(b"x")
    (tmp_path / "secret.txt").write_text("no")
    assert census_file(state, kept.name) == kept
    for name in ("../secret.txt", "mugil-census-20261108-120000.jsonl.gz/../../secret.txt",
                 "/etc/passwd", "mugil-census-20261108-120001.jsonl.gz", "", None,
                 "mugil-topology.json"):
        assert census_file(state, name) is None, name
