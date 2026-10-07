from ninaivu.archive.pacing import ArchivePacer, SystemConditions


class Gate:
    def __init__(self):
        self.checks = 0

    def check(self):
        self.checks += 1


def test_ac_and_cool_runs_at_full_speed():
    sleeps = []
    pacer = ArchivePacer(lambda: SystemConditions(False, 100, 45), sleeps.append, 0)
    pacer.checkpoint(Gate())
    assert pacer.snapshot()['mode'] == 'full-speed'
    assert sleeps == []


def test_battery_power_throttles_each_chunk():
    sleeps = []
    pacer = ArchivePacer(lambda: SystemConditions(True, 70, 45), sleeps.append, 0)
    pacer.checkpoint(Gate())
    assert pacer.snapshot()['mode'] == 'throttled'
    assert sleeps == [0.05]


def test_low_battery_parks_until_ac_returns():
    readings = iter([
        SystemConditions(True, 10, 45),
        SystemConditions(False, 10, 45),
    ])
    sleeps = []
    gate = Gate()
    pacer = ArchivePacer(lambda: next(readings), sleeps.append, 0)
    pacer.checkpoint(gate)
    assert sleeps == [2.0]
    assert gate.checks == 2
    assert pacer.snapshot()['mode'] == 'full-speed'


def test_critical_heat_parks_and_warm_machine_throttles():
    readings = iter([
        SystemConditions(False, None, 94),
        SystemConditions(False, None, 84),
    ])
    sleeps = []
    pacer = ArchivePacer(lambda: next(readings), sleeps.append, 0)
    pacer.checkpoint(Gate())
    assert sleeps == [2.0, 0.05]
    assert pacer.snapshot()['mode'] == 'throttled'
    assert 'warm' in pacer.snapshot()['reason']


MIB = 1 << 20


def test_on_battery_the_pause_is_charged_per_megabyte_read_not_per_read():
    """A small photo used to pay a flat 50 ms on every read, the empty one that
    finds the end of the file included: 53 s for 200 photos on battery against
    1-2 s on mains. The pause now follows the bytes."""
    sleeps = []
    pacer = ArchivePacer(lambda: SystemConditions(True, 70, 45), sleeps.append, 0)
    gate = Gate()
    # One 300 KB photo: a read that gets its bytes, then the empty read.
    pacer.checkpoint(gate, 0)
    pacer.checkpoint(gate, 300_000)
    assert sleeps == [], "a fraction of a megabyte owes less than one pause"
    # Its debt is not forgotten: enough small files add up to a pause.
    for _ in range(3):
        pacer.checkpoint(gate, 300_000)
    assert len(sleeps) == 1 and 0.05 <= sleeps[0] < 0.06, sleeps


def test_a_large_file_is_throttled_exactly_as_before():
    sleeps = []
    pacer = ArchivePacer(lambda: SystemConditions(True, 70, 45), sleeps.append, 0)
    for _ in range(10):
        pacer.checkpoint(Gate(), MIB)
    assert sleeps == [0.05] * 10


def test_mains_power_forgets_what_battery_was_owed():
    readings = iter([SystemConditions(True, 70, 45), SystemConditions(False, 70, 45),
                     SystemConditions(True, 70, 45)])
    sleeps = []
    pacer = ArchivePacer(lambda: next(readings), sleeps.append, 0)
    pacer.checkpoint(Gate(), 800_000)     # owes 0.038 s, below one pause
    pacer.checkpoint(Gate(), 800_000)     # on mains: nothing owed any more
    pacer.checkpoint(Gate(), 800_000)     # back on battery: starts from nothing
    assert sleeps == []


def test_a_real_copy_of_small_photos_on_battery_is_not_a_crawl(tmp_path):
    """End to end through the archive's own copy and verify loops."""
    import os
    from ninaivu import archive
    from ninaivu.archive import database as db
    from ninaivu.archive.scanner import ArchiveJob

    archive.configure(tmp_path)
    db.close_db()
    db.init_db()
    source = tmp_path / "card"
    source.mkdir()
    for i in range(40):
        # Distinct sizes, as real photos have: identical sizes would add a
        # duplicate-check read to every file after the first.
        (source / f"IMG_{i:04d}.jpg").write_bytes(b"\xff\xd8\xff\xe0" + os.urandom(300_000 + i * 97))
    slept = []
    job = ArchiveJob([str(source)], str(tmp_path / "Master"),
                     pacer=ArchivePacer(lambda: SystemConditions(True, 70, 45), slept.append, 0))
    try:
        job.run()
        verified = sum(1 for r in db.get_recent_files(100) if r["status"] == "verified")
    finally:
        db.close_db()
    assert verified == 40
    # 40 photos x 300 KB, charged once, for the copy: about 11.5 MiB, so about
    # 0.6 s of pauses. The read-back comes from the system's cache and is not
    # charged again (it was, at 1.1 s). Charged per read it was 40 x 4 x 50 ms
    # = 8 s.
    assert 0.45 < sum(slept) < 0.75, sum(slept)
