"""What the tenth audit (2026-10-24) noted and left for later, in the two-
computer code: a file set aside on the Secondary is still found, the locks
are one per folder, the inbox's MOVE leaves no file behind, and the record
of signatures already seen lets old ones go from the front."""
from __future__ import annotations

import hashlib
import io
import threading
from pathlib import Path

from ninaivu.mugil import inbox as inbox_mod, pairing
from ninaivu.mugil.hold import SET_ASIDE, Holding, lock_for

LIBRARY = "0123456789abcdef0123456789abcdef"


def _keep(holding: Holding, rel: str, data: bytes, mtime: float = 1_600_000_000.0):
    return holding.receive(LIBRARY, "Photos", rel, size=len(data), algo="blake2b",
                           digest=hashlib.blake2b(data, digest_size=32).hexdigest(), mtime=mtime,
                           body=io.BytesIO(data))


def test_a_file_set_aside_is_still_found_by_its_fingerprint(tmp_path):
    holding = Holding(tmp_path / "originals")
    holding.folder.mkdir()
    old, new = b"the first photograph", b"another photograph, later, same name"
    assert _keep(holding, "2019/beach.jpg", old)["kept"] == "new"
    assert _keep(holding, "2019/beach.jpg", new)["kept"] == "set_aside_older"
    old_digest = hashlib.blake2b(old, digest_size=32).hexdigest()

    # Asked for by path alone: the file there now.
    path, record = holding.kept_file(LIBRARY, "2019/beach.jpg")
    assert path.read_bytes() == new
    # Asked for by the fingerprint of the one that was there: the one set aside.
    path, record = holding.kept_file(LIBRARY, "2019/beach.jpg", old_digest)
    assert path.read_bytes() == old and SET_ASIDE in path.parts
    assert record["digest"] == old_digest and record["size"] == len(old)
    assert "aside" not in record
    # The one there now, asked for by its own fingerprint, is the one there.
    path, _ = holding.kept_file(LIBRARY, "2019/beach.jpg", hashlib.blake2b(new, digest_size=32).hexdigest())
    assert path.read_bytes() == new
    # A fingerprint never kept there: nothing.
    assert holding.kept_file(LIBRARY, "2019/beach.jpg", "0" * 64) is None


def test_every_holding_of_one_folder_shares_one_lock(tmp_path):
    one, two = Holding(tmp_path / "originals"), Holding(tmp_path / "originals" / ".")
    assert one._lock is two._lock
    assert Holding(tmp_path / "elsewhere")._lock is not one._lock
    assert lock_for("inbox", tmp_path / "originals") is not one._lock
    box_one = inbox_mod.Inbox(tmp_path / "originals")
    box_two = inbox_mod.Inbox(tmp_path / "originals")
    assert box_one._lock is box_two._lock


def test_a_move_onto_a_waiting_file_leaves_no_file_behind(tmp_path):
    box = inbox_mod.Inbox(tmp_path / "originals")
    for path, data in (("a.jpg", b"first"), ("b.jpg", b"second")):
        box.receive(1, path, path, io.BytesIO(data), length=len(data), limit=1 << 20)
    files = box.base / "files"
    before = sorted(p for p in files.rglob("*") if p.is_file())
    assert len(before) == 2
    assert box.move(1, "a.jpg", "b.jpg")
    after = sorted(p for p in files.rglob("*") if p.is_file())
    assert len(after) == 1 and after[0].read_bytes() == b"first"
    assert [w["path"] for w in box.waiting()] == ["b.jpg"]
    # A move of something not there changes nothing.
    assert not box.move(1, "gone.jpg", "b.jpg")
    assert [p.read_bytes() for p in files.rglob("*") if p.is_file()] == [b"first"]


def test_signatures_seen_long_ago_are_let_go_and_new_ones_are_not_taken_twice():
    seen = pairing.Seen()
    start = 1_000_000.0
    for n in range(2000):
        assert seen.first_time(f"sig-{n}", start + n * 0.01)
    assert not seen.first_time("sig-5", start + 30)
    later = start + 30 + 2 * pairing.SKEW
    assert seen.first_time("sig-new", later)
    assert len(seen._seen) == 1                           # the old ones went
    assert seen.first_time("sig-5", later)                # out of time: refused elsewhere
    assert not seen.first_time("sig-new", later + 1)


def test_two_threads_never_keep_the_same_signature_twice():
    seen = pairing.Seen()
    taken: list[bool] = []
    ready = threading.Barrier(8)

    def take():
        ready.wait()
        taken.append(seen.first_time("same", 5_000.0))
    threads = [threading.Thread(target=take) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert taken.count(True) == 1


def test_the_primary_asks_for_an_original_by_its_fingerprint(tmp_path, monkeypatch):
    """So a file set aside on the Secondary is the one sent back."""
    from types import SimpleNamespace
    from ninaivu.mugil import fetch
    asked = {}

    class Partner:
        def __init__(self, *a, **k):
            pass

        def download(self, target, *, query, write):
            asked.update(query)
            write(b"x")
            return 1
    monkeypatch.setattr(fetch, "role", lambda cfg: fetch.PRIMARY)
    monkeypatch.setattr(fetch.pairing, "load", lambda state: {"key": "k"})
    monkeypatch.setattr(fetch, "record_of", lambda *a: {
        "digest": hashlib.blake2b(b"x", digest_size=32).hexdigest(), "size": 1, "algo": "blake2b",
        "mtime": 1.0, "library": LIBRARY})
    cfg = SimpleNamespace(state_dir=str(tmp_path), mugil_partner_url="http://pc")
    got = fetch.original(cfg, str(tmp_path / "lib"), "a.jpg", partner=Partner)
    assert got is not None and Path(got).read_bytes() == b"x"
    assert asked["digest"] == hashlib.blake2b(b"x", digest_size=32).hexdigest()


def test_the_wake_is_at_the_primarys_hour_whatever_the_secondarys_time_zone(tmp_path):
    import datetime as dt
    from ninaivu.mugil import wake
    # The Primary is five and a half hours east of UTC and wants waking at 01:55.
    kept = wake.remember(tmp_path, {"macs": ["a4:83:e7:01:02:03"], "at": "01:55",
                                    "utc_offset": 19800})
    assert kept["utc_offset"] == 19800
    there = dt.timezone(dt.timedelta(seconds=19800))
    night = dt.datetime(2026, 10, 25, 1, 56, tzinfo=there).timestamp()
    assert wake.due(kept, night)
    assert not wake.due(kept, night - 120)
    # 01:56 UTC is 07:26 at the Primary: long past its wake.
    assert not wake.due(kept, dt.datetime(2026, 10, 25, 1, 56, tzinfo=dt.timezone.utc).timestamp())
    # A nonsense offset, or none from an older Primary, falls back to this clock.
    assert wake.remember(tmp_path, {"macs": [], "at": "01:55", "utc_offset": 10 ** 9})["utc_offset"] is None
    assert wake.remember(tmp_path, {"macs": [], "at": "01:55"})["utc_offset"] is None


def test_the_space_report_reuses_the_optimize_plan(as_admin, monkeypatch):
    import ninaivu.mugil as mugil
    from ninaivu.mugil import optimize
    calls = []

    def plan(*a, **k):
        calls.append(1)
        return {"ready": {"files": 1, "bytes": 5}}
    monkeypatch.setattr(mugil, "role", lambda cfg: mugil.PRIMARY)
    monkeypatch.setattr(optimize, "plan", plan)
    for _ in range(3):
        assert as_admin.get("/api/admin/space").status_code == 200
    assert len(calls) == 1
