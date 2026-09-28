"""The library scan saying which folder it is in.

The scan has always reported counts and a percentage. What it has never
reported is *where*, and on a library big enough for the first scan to take
half an hour that is the difference between "it is working through the 2014
folder" and "it has been on 41% for ten minutes and I do not know if the drive
is still answering".

The archive's estimate was given this treatment first; this is the same thing
for the other half of the app, held to the same promises: the folder is
published while the walk runs, it is short enough for a status line, and it is
cleared when the scan stops so a finished scan never looks like a stuck one.
"""

import re
import time
from pathlib import Path

import pytest

from ninaivu.server import auth
from ninaivu.storage import db
from ninaivu.media.scanner import (
    ITEM_PHASES, ScanProgress, Scanner, short_folder)


# --- shortening ------------------------------------------------------------

def test_a_shallow_folder_is_left_alone():
    assert short_folder("/photos/2019", "/photos") == "2019"


def test_the_library_root_itself_reads_as_the_top():
    assert short_folder("/photos", "/photos") == "the top folder"


def test_a_deep_folder_keeps_the_end_which_is_the_useful_part():
    where = short_folder("/photos/a/b/c/d/e/2019/07/Cornwall", "/photos")
    assert where.endswith("2019/07/Cornwall"), where
    assert len(where) < 40, where


def test_a_folder_outside_the_root_still_produces_something_readable():
    """A root that moved, a symlink, a Windows drive letter mismatch — none of
    them should make the status line throw or go blank."""
    assert short_folder("/somewhere/else/2019", "/photos")


def test_backslashes_are_understood():
    where = short_folder(r"D:\Photos\2019\07\Cornwall", r"D:\Photos")
    assert "Cornwall" in where


# --- the progress record ---------------------------------------------------

def test_a_new_scan_reports_no_folder():
    assert ScanProgress().snapshot()["folder"] == ""


def test_the_folder_survives_a_snapshot():
    progress = ScanProgress()
    progress.update(folder="2019/07/Cornwall")
    assert progress.snapshot()["folder"] == "2019/07/Cornwall"


def test_finishing_clears_the_folder(tmp_path):
    """A scan that has stopped is not "in" anywhere. Leaving the last folder on
    screen would make a finished scan read as one frozen mid-walk."""
    progress = ScanProgress()
    progress.update(folder="2019/07/Cornwall", status="indexing")
    progress.update(status="done", folder="")
    assert progress.snapshot()["folder"] == ""


# --- end to end ------------------------------------------------------------

@pytest.fixture()
def library(tmp_path):
    """A tree deep enough that "where is it" is a real question."""
    from PIL import Image
    root = tmp_path / "Photos"
    for year in ("2019", "2021", "2023"):
        for month in ("04", "09"):
            folder = root / year / month / "Holiday"
            folder.mkdir(parents=True)
            for i in range(4):
                Image.new("RGB", (80, 60), (i * 30, 90, 140)).save(
                    folder / f"IMG_{i}.jpg")
    return root


def test_the_scan_names_folders_as_it_walks(library, tmp_path, monkeypatch):
    """Collected from the live progress record rather than asserted after the
    fact, so this fails if the folder is only ever set at the end."""
    from pathlib import Path

    from ninaivu.server.config import Config

    cfg = Config(state_dir=tmp_path / "state", min_media_bytes=0)
    cfg.active_root = str(library)
    cfg.roots = [str(library)]
    cfg.watch = False
    cfg.ensure_dirs()
    conn = db.init_db(cfg.db_path)
    auth.init_auth_schema(conn)          # folder rules live in the auth schema

    scanner = Scanner(cfg)
    seen: list[str] = []
    # Patched on the class: the scan makes itself a fresh progress record, so
    # anything attached to the old one would never be called.
    real_update = ScanProgress.update

    def watch(self, **fields):
        if fields.get("folder"):
            seen.append(fields["folder"])
        return real_update(self, **fields)

    monkeypatch.setattr(ScanProgress, "update", watch)
    scanner._run(Path(library), full=True)

    assert seen, "the scan never said where it was"
    assert any("2019" in w or "2021" in w or "2023" in w for w in seen), seen
    # And it is not left pointing at the last folder it happened to touch.
    assert scanner.progress.snapshot()["folder"] == ""
    assert scanner.progress.snapshot()["status"] == "done"


# --- and saying how far through a pass it is -------------------------------
#
# Every pass after indexing counts items rather than files walked, and the
# snapshot has measured them that way for a while. Both progress strips,
# though, only built a moving "384 / 17,685" line for tagging; every other
# phase fell back to the message the pass wrote once when it started. On
# 17,685 videos that is one unchanging sentence over a bar whose whole-number
# percentage moves once every 177 clips — ten minutes at 0% before the first
# tick, which is indistinguishable from a scan that has hung.

SCRIPTS = Path(__file__).resolve().parent.parent / "ninaivu" / "static" / "js"


def counting_phases(script: str) -> set[str]:
    """The phases `script`'s SCAN_COUNTS map knows how to count."""
    body = re.search(r"(?:export\s+)?const SCAN_COUNTS = \{(.*?)\n\};", script, re.S)
    assert body, "SCAN_COUNTS is gone from the strip"
    return set(re.findall(r"^\s*(\w+):", body.group(1), re.M))


#: The scripts that put a scan's counters on screen. Both apps draw the
#: activity strip out of activity.js now; admin.js still draws the indexing
#: bar on its Settings page, which is the one place a scan has a bar of its
#: own. app.js reaches the words through activity.js, which is checked
#: separately below.
STRIP_SCRIPTS = ("activity.js", "admin.js")


def test_every_item_counted_phase_can_say_how_far_through_it_is():
    """The guard that matters when a pass is added.

    A phase the scanner counts in items but the strip has no words for falls
    straight back to a sentence that never changes — the bug this is here to
    stop coming back, one new pass at a time.
    """
    phases = counting_phases((SCRIPTS / "api.js").read_text(encoding="utf-8"))
    assert ITEM_PHASES <= phases, f"api.js cannot count {ITEM_PHASES - phases}"
    for name in STRIP_SCRIPTS:
        content = (SCRIPTS / name).read_text(encoding="utf-8")
        assert "SCAN_COUNTS" in content, f"{name} does not use SCAN_COUNTS"


def test_the_gallery_reaches_the_strip_it_no_longer_draws_itself():
    """app.js hands the strip to activity.js; it must still ask it.

    The words moved out of both apps when the strip became a list of every
    running job. That is only an improvement while the gallery is actually
    wired to it — a page that quietly stopped importing it would show no
    progress at all, which is the failure this whole file exists to catch.
    """
    app = (SCRIPTS / "app.js").read_text(encoding="utf-8")
    assert "from './activity.js'" in app, "app.js no longer imports the strip"
    assert "renderActivity" in app, "app.js no longer draws the strip"


def test_each_line_carries_both_numbers():
    """A count with no total is a number nobody can place."""
    body = re.search(r"(?:export\s+)?const SCAN_COUNTS = \{(.*?)\n\};",
                     (SCRIPTS / "api.js").read_text(encoding="utf-8"), re.S)
    assert body, "SCAN_COUNTS is missing from api.js"
    for line in re.findall(r"^\s*\w+: \(done, total\) =>(.*)$",
                           body.group(1), re.M):
        assert "${done}" in line and "${total}" in line, line


def test_the_phases_the_strip_counts_are_the_ones_the_snapshot_measures():
    """`percent` comes from tagged/tag_total for exactly these phases. A
    phase in the strip's map but not the snapshot's set would draw a count
    beside a percentage computed from files walked, which is worse than
    either on its own."""
    phases = counting_phases((SCRIPTS / "api.js").read_text(encoding="utf-8"))
    assert phases == set(ITEM_PHASES), f"api.js: {phases ^ set(ITEM_PHASES)}"


# --- and how long is left --------------------------------------------------

def test_a_pass_says_nothing_until_it_has_seen_enough():
    """The first few items are a model loading and a drive spinning up. A wrong
    number early is worse than none: people believe the first one they see."""
    p = ScanProgress(status="videos", tag_total=17685, tagged=3,
                     phase_started_at=time.time() - 30)
    assert p.snapshot()["eta"] is None


def test_the_time_left_is_measured_from_the_pass_not_the_scan():
    """By the time a scan reaches the video pass it may have been walking and
    tagging for an hour. Dividing that hour by the videos described so far
    would put the finish a day out."""
    now = time.time()
    p = ScanProgress(status="videos", tag_total=1000, tagged=100,
                     started_at=now - 7200,          # the scan, two hours old
                     phase_started_at=now - 100)     # this pass, 100s old
    eta = p.snapshot()["eta"]
    assert 800 < eta < 1000, eta       # 1s an item, 900 left -- not hours


def test_a_phase_that_counts_nothing_has_nothing_to_say():
    p = ScanProgress(status="finishing", tag_total=0, tagged=0,
                     phase_started_at=time.time() - 60)
    assert p.snapshot()["eta"] is None


def test_a_finished_scan_has_no_time_left():
    p = ScanProgress(status="done", tag_total=100, tagged=100,
                     phase_started_at=time.time() - 60)
    assert p.snapshot()["eta"] is None


def test_the_pass_clock_restarts_when_the_next_pass_announces_itself():
    """Each pass sets the count it is working towards; that is its start."""
    p = ScanProgress()
    p.update(status="tagging", tag_total=500, tagged=0)
    first = p.phase_started_at
    assert first > 0
    p.update(status="videos", tag_total=17685, tagged=0)
    assert p.phase_started_at >= first


def test_the_words_for_a_long_pass_are_checked_in_the_strip():
    """The rounding lives in the browser; defined in api.js and shared."""
    api_script = (SCRIPTS / "api.js").read_text(encoding="utf-8")
    assert "function timeLeft(" in api_script, "api.js"
    assert "hours left" in api_script and "minutes left" in api_script, "api.js"
    for name in STRIP_SCRIPTS:
        script = (SCRIPTS / name).read_text(encoding="utf-8")
        assert "timeLeft" in script, f"{name} does not use timeLeft"


# --- and does the cheap work first -----------------------------------------

PASSES = ("_tag", "_name_places", "_tag_video_keyframes", "_read_text",
          "_find_faces")


def _pass_order(scanned, monkeypatch, *, faces: bool) -> list[str]:
    """Which analysis passes a scan runs, in the order it runs them.

    Each is replaced by something that only writes its name down, so the walk
    is real and nothing after it costs anything.
    """
    cfg, _conn, _ = scanned
    cfg.ai_enabled = True
    cfg.faces_enabled = faces
    order: list[str] = []
    for name in PASSES:
        monkeypatch.setattr(
            Scanner, name,
            (lambda n: lambda self, *a, **k: order.append(n))(name))
    monkeypatch.setattr(Scanner, "_judge_screens", lambda self, *a, **k: None)

    scanner = Scanner(cfg)
    scanner.ai = object()          # enough for the "is there a model" checks
    scanner._run(Path(cfg.active_root), full=True)
    return order

def test_naming_places_runs_before_the_passes_that_take_hours(scanned,
                                                             monkeypatch):
    """Naming is a local lookup — twenty thousand photographs in a minute — and
    it used to run after describing videos, which on a large library is most of
    a day. Locations arrived the morning after the scan that found them."""
    order = _pass_order(scanned, monkeypatch, faces=True)

    assert "_name_places" in order and "_tag_video_keyframes" in order, order
    assert order.index("_name_places") < order.index("_tag_video_keyframes"), order
    assert order.index("_name_places") < order.index("_find_faces"), order


def test_tagging_still_comes_first(scanned, monkeypatch):
    """Everything downstream reads the vectors it makes."""
    order = _pass_order(scanned, monkeypatch, faces=False)
    assert order and order[0] == "_tag", order


def test_a_pass_that_reports_its_total_every_time_keeps_its_clock(monkeypatch):
    """The face pass sends its total with every progress report, not only when
    it starts. The clock used to restart whenever a total arrived, so it ran
    for the last ten photographs only and was divided by all of them: on a
    library with 141,148 faces still to find, the strip said three minutes,
    then five, then two — and meant about seventeen hours.
    """
    from ninaivu.media import scanner as scanner_mod

    now = [1_000.0]
    monkeypatch.setattr(scanner_mod.time, "time", lambda: now[0])

    p = ScanProgress()
    p.update(status="faces", tag_total=142_361, tagged=0)
    for done in range(10, 1_220, 10):
        now[0] += 4.5                              # ten photographs, 0.45 s each
        p.update(tagged=done, tag_total=142_361)   # exactly as the face pass does

    eta = p.snapshot()["eta"]
    hours = eta / 3600
    assert 16 < hours < 19, f"said {eta / 60:.0f} minutes"


def test_a_new_pass_still_starts_a_new_clock(monkeypatch):
    """The other half: the video pass must not inherit an hour of tagging."""
    from ninaivu.media import scanner as scanner_mod

    now = [1_000.0]
    monkeypatch.setattr(scanner_mod.time, "time", lambda: now[0])

    p = ScanProgress()
    p.update(status="tagging", tag_total=100, tagged=0)
    now[0] += 3600                                 # an hour of tagging
    p.update(status="videos", tag_total=1_000, tagged=0)
    now[0] += 100
    p.update(tagged=100)                           # 1 s a video since it began
    eta = p.snapshot()["eta"]
    assert 800 < eta < 1_000, eta
