"""Two computers run apart for a while, then paired: what only the Secondary
has is found there and brought into the Primary's library, and nothing on
the Secondary's disk is changed, moved or removed.

On the household's own: a Mac (the Primary) and a Linux PC with an N300
disk (the Secondary), each a one-computer Ninaivu for a time, with more
photographs imported on the Linux PC.
"""

import os
import time
from pathlib import Path

import pytest

from ninaivu.mugil.hold import SET_ASIDE
from ninaivu.storage import library_id
from test_mugil_two_computers import _paired, partner_through, two_apps  # noqa: F401


def _files(folder: Path) -> dict[str, tuple[bytes, float]]:
    return {p.relative_to(folder).as_posix(): (p.read_bytes(), p.stat().st_mtime)
            for p in folder.rglob("*") if p.is_file() and not p.name.startswith(".ninaivu-originals")}


@pytest.fixture()
def apart(two_apps, tmp_path, scanned):  # noqa: F811
    """Paired after a time apart: the Secondary has files of its own."""
    primary_app, secondary_app, other = two_apps
    first, services, folder = _paired(two_apps, tmp_path)
    through = partner_through(secondary_app.test_client())
    services.mugil_bring_in.partner = through
    services.mugil_courier.last = {"complete": True, "finished": time.time() - 5}
    root = Path(scanned[0].roots[0])
    kept = folder / root.name
    some = sorted(p for p in kept.rglob("*.jpg"))[0]
    rel_some = some.relative_to(kept).as_posix()
    when = 1_700_000_000.0

    def put(path: Path, data: bytes) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        os.utime(path, (when, when))
        return path

    made = {
        # Imported on the Linux PC: the Mac never had them.
        "new": put(kept / "2026/11/03/linux-only.jpg", b"\xff\xd8 a photograph from the linux pc"),
        "sidecar": put(kept / "2026/11/03/linux-only.jpg.xmp", b"<x:xmpmeta ninaivu:Writer=\"Ninaivu\"/>"),
        "video": put(kept / "2026/11/04/clip.mp4", b"\x00\x00\x00\x18ftyp a video"),
        # The same picture as one the Mac has, under another name.
        "duplicate": put(kept / "copies/same-as-mac.jpg", some.read_bytes()),
        # In the recycle bin there.
        "binned": put(kept / "_deleted/2026-10-01/old.jpg", b"\xff\xd8 binned"),
        # A folder that is no library of the Mac's.
        "stray": put(folder / "From the phone/IMG_9.jpg", b"\xff\xd8 stray"),
        # The Linux PC's own version of a path the Mac also has: set aside
        # when the Mac's arrived, as a copy made by hand is.
        "version": put(folder / SET_ASIDE / "2026-11-05 101010" / root.name / rel_some,
                       b"\xff\xd8 the linux pc's own edit"),
        # A photograph somebody on the Mac deleted: still on the Linux PC.
        "deleted_on_mac": put(kept / "2025/01/01/deleted-on-mac.jpg", b"\xff\xd8 deleted on the mac"),
    }
    conn = scanned[1]
    conn.execute("INSERT INTO recycled (root, rel_path, filename, size, bin_path, deleted_at) "
                 "VALUES (?,?,?,?,?,?)", (str(root), "2025/01/01/deleted-on-mac.jpg",
                                          "deleted-on-mac.jpg", 10, "_deleted/x", time.time()))
    conn.commit()
    return first, services, secondary_app, folder, root, made, rel_some


def _look(first, secondary_app):
    answer = first.post("/api/admin/mugil/only-on-secondary/look")
    assert answer.status_code == 200, answer.get_json()
    secondary_app.config["MV_SERVICES"].mugil_survey._thread.join(timeout=30)
    return first.get("/api/admin/mugil/only-on-secondary").get_json()


def test_what_only_the_secondary_has_is_found_by_kind(apart):
    first, services, secondary_app, folder, root, made, _ = apart
    status = _look(first, secondary_app)
    counts = status["found"]["counts"]
    assert counts["only"]["files"] == 4          # the photograph, its sidecar, the video, deleted-on-mac
    assert counts["other_version"]["files"] == 1
    assert counts["duplicate"]["files"] == 1
    assert counts["deleted_folder"]["files"] == 1
    assert counts["not_a_library"]["files"] == 1
    assert status["can_bring"] is True


def test_they_are_brought_in_and_nothing_on_the_secondary_changes(apart):
    first, services, secondary_app, folder, root, made, rel_some = apart
    before = _files(folder)
    mac_before = (root / rel_some).read_bytes()
    _look(first, secondary_app)
    started = first.post("/api/admin/mugil/only-on-secondary/bring")
    assert started.status_code == 200, started.get_json()
    services.mugil_bring_in._thread.join(timeout=30)
    done = services.mugil_bring_in.status()
    assert done["complete"] is True and not done.get("error"), done

    # Brought in, byte for byte, with their own times.
    for key, rel in (("new", "2026/11/03/linux-only.jpg"), ("sidecar", "2026/11/03/linux-only.jpg.xmp"),
                     ("video", "2026/11/04/clip.mp4")):
        here = root / rel
        assert here.read_bytes() == made[key].read_bytes(), key
        assert here.stat().st_mtime == pytest.approx(made[key].stat().st_mtime, abs=1e-3)
    # The Linux PC's version is beside the Mac's, and the Mac's is untouched.
    assert (root / rel_some).read_bytes() == mac_before
    beside = [p for p in (root / rel_some).parent.iterdir() if "(from " in p.name]
    assert len(beside) == 1 and beside[0].read_bytes() == made["version"].read_bytes()
    # Not brought: the duplicate, the bin, the stray folder, the one deleted on the Mac.
    assert not (root / "copies/same-as-mac.jpg").exists()
    assert not (root / "_deleted/2026-10-01/old.jpg").exists()
    assert not (root / "2025/01/01/deleted-on-mac.jpg").exists()
    assert done["deleted_here"] == 1 and done["beside"] == 1 and done["copied"] == 4
    assert not list(root.rglob("*.ninaivu-partial-*"))

    # Nothing on the Secondary changed, moved or went.
    after = _files(folder)
    assert {k: v for k, v in before.items()} == {k: v for k, v in after.items() if k in before}
    assert set(before) <= set(after)


def test_the_next_run_keeps_them_without_sending_them_back(apart):
    first, services, secondary_app, folder, root, made, _ = apart
    _look(first, secondary_app)
    first.post("/api/admin/mugil/only-on-secondary/bring")
    services.mugil_bring_in._thread.join(timeout=30)
    from test_mugil_two_computers import run
    services.mugil_courier.partner = partner_through(secondary_app.test_client())
    again = run(services.mugil_courier)
    assert again["complete"] is True
    # At most the version put beside crosses the network (once it is
    # indexed); what came from the Secondary's own place is known there.
    assert again["sent"] <= 1 and again["bytes"] <= len(made["version"].read_bytes()), again


def test_bringing_in_waits_for_a_complete_run_and_a_look(apart):
    first, services, secondary_app, *_ = apart
    services.mugil_courier.last = {}
    status = first.get("/api/admin/mugil/only-on-secondary").get_json()
    assert status["can_bring"] is False and status["why_not"]
    assert first.post("/api/admin/mugil/only-on-secondary/look").status_code == 409
    assert first.post("/api/admin/mugil/only-on-secondary/bring").status_code == 409
    services.mugil_courier.last = {"complete": True, "finished": time.time() + 60}
    # Looked before the last run: looked again first.
    assert first.post("/api/admin/mugil/only-on-secondary/bring").status_code == 409


def test_a_second_bring_in_copies_nothing_again(apart):
    first, services, secondary_app, folder, root, *_ = apart
    _look(first, secondary_app)
    first.post("/api/admin/mugil/only-on-secondary/bring")
    services.mugil_bring_in._thread.join(timeout=30)
    again = services.mugil_bring_in.run()
    assert again["copied"] == 0 and again["already"] >= 4, again


def test_only_the_paired_secondary_answers(app, as_admin):
    assert as_admin.get("/api/admin/mugil/only-on-secondary").status_code == 404
    assert app.test_client().get("/api/mugil/survey").status_code == 404
    assert app.test_client().get("/api/mugil/survey/file?path=x").status_code == 404


def test_beside_never_takes_a_name_that_is_there(tmp_path):
    from ninaivu.mugil.bring_in import beside
    target = tmp_path / "IMG_1.jpg"
    target.write_bytes(b"mac")
    first = beside(target, "Linux PC")
    assert first.name == "IMG_1 (from Linux PC).jpg"
    first.write_bytes(b"x")
    assert beside(target, "Linux PC").name == "IMG_1 (from Linux PC 2).jpg"
    assert beside(tmp_path / ".hidden", "Linux PC").name == ".hidden (from Linux PC)"
    assert library_id  # the module the tests above rely on


# --- a one-computer Ninaivu becoming the Secondary, its library the originals ---

def _n300_library(cfg, tmp_path):
    """The Linux PC's library as the household has it: on the N300, in
    NinaivuMaster/Ninaivu Archive."""
    holder = tmp_path / "media" / "jaga" / "Toshiba" / "NinaivuMaster"
    root = holder / "Ninaivu Archive"
    (root / "2026" / "11").mkdir(parents=True)
    (root / "2026" / "11" / "linux.jpg").write_bytes(b"\xff\xd8 photo")
    cfg.add_library(str(root))
    return holder, Path(str(root.resolve()))


def test_its_own_library_becomes_the_originals_only_when_asked(app, as_admin, scanned, tmp_path):
    cfg = app.config["MV_CONFIG"]
    holder, root = _n300_library(cfg, tmp_path)
    files = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assets = scanned[1].execute("SELECT COUNT(*) FROM assets").fetchone()[0]

    asked = as_admin.post("/api/admin/mugil/role",
                          json={"role": "secondary", "originals_dir": str(holder)})
    assert asked.status_code == 409
    assert asked.get_json()["library_inside"] == [str(root)]
    assert str(root) in cfg.roots                     # nothing changed yet

    done = as_admin.post("/api/admin/mugil/role", json={
        "role": "secondary", "originals_dir": str(holder), "library_into_originals": True})
    assert done.status_code == 200, done.get_json()
    assert str(root) not in cfg.roots and cfg.mugil_originals_dir == str(holder)
    # Not a file moved, changed or removed; the index is as it was.
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == files
    assert scanned[1].execute("SELECT COUNT(*) FROM assets").fetchone()[0] == assets


def test_it_waits_for_the_sidecars_to_be_written(app, as_admin, tmp_path):
    cfg = app.config["MV_CONFIG"]
    holder, root = _n300_library(cfg, tmp_path)
    writer = app.config["MV_SERVICES"].xmp

    class Busy:
        running = True
    app.config["MV_SERVICES"].xmp = Busy()
    try:
        answer = as_admin.post("/api/admin/mugil/role", json={
            "role": "secondary", "originals_dir": str(holder), "library_into_originals": True})
    finally:
        app.config["MV_SERVICES"].xmp = writer
    assert answer.status_code == 409 and "sidecars" in answer.get_json()["error"]
    assert str(root) in cfg.roots


def test_a_folder_inside_the_library_is_still_refused(app, as_admin):
    cfg = app.config["MV_CONFIG"]
    inside = Path(cfg.roots[0]) / "sub"
    inside.mkdir(exist_ok=True)
    answer = as_admin.post("/api/admin/mugil/role", json={
        "role": "secondary", "originals_dir": str(inside), "library_into_originals": True})
    assert answer.status_code == 400
