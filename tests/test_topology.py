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
        apart.primary.plan(UNPAIR)                   # checked, not yet planned
    with pytest.raises(topology.Refused):
        apart.primary.plan("swap")


def test_replacing_the_secondary_disk_lists_what_only_it_holds(apart):
    apart.primary.plan(REPLACE_SECONDARY_DISK)
    record = finish(apart.primary)["record"]
    assert record["state"] == "planned"
    only = sorted(Path(m["file"]).name for m in record["plan"]["named"])
    assert only == ["IMG_1.jpg", "IMG_9.jpg", "deleted-on-mac.jpg", "linux-only.jpg",
                    "linux-only.jpg.xmp", "old.jpg"]
