"""The third stability pass: hand-offs between jobs that waited for somebody.

A backup that had caught up never sent another photograph, a scan queued for
one drive came back as a walk of every drive, a hand-over dropped the library
folders the running scan had not reached, an import fought the indexer for
the disk, and start-up waited for the image model before noticing anything.
"""

import io
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from ninaivu.cloud.service import CloudService
from ninaivu.media import scanner as scanner_mod
from ninaivu.media.scanner import CLAIM_IMPORT, Scanner
from ninaivu.storage import db
from ninaivu.storage.importer import Importer


def _wait_for(predicate, timeout=5.0):
    ends = time.monotonic() + timeout
    while time.monotonic() < ends:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# -- the backup follows the library -------------------------------------------

def _service(cfg, monkeypatch):
    conn = db.init_db(cfg.db_path)
    service = CloudService(cfg, lambda: conn)
    monkeypatch.setattr(type(service.creds), "connected", property(lambda _self: True))
    started = []
    monkeypatch.setattr(service, "start", lambda: (started.append(True),
                                                   service._follow(True)))
    cfg.cloud_enabled = True
    return service, started


def test_a_backup_that_had_caught_up_sends_new_photographs(cfg, monkeypatch):
    service, started = _service(cfg, monkeypatch)
    service._follow(True)                 # Start was pressed once
    service.library_changed()             # a scan found new photographs
    assert started == [True]


def test_a_paused_backup_stays_paused(cfg, monkeypatch):
    service, started = _service(cfg, monkeypatch)
    service._follow(True)
    service.pause()
    service.library_changed()
    assert started == []


def test_a_backup_never_started_is_not_started_by_a_scan(cfg, monkeypatch):
    service, started = _service(cfg, monkeypatch)
    service.library_changed()
    assert started == []


def test_a_library_move_does_not_switch_the_backup_off(cfg, monkeypatch):
    service, started = _service(cfg, monkeypatch)
    service._follow(True)
    service.pause(stop_following=False)
    service.library_changed()
    assert started == [True]


# -- scans come back as what was asked for ---------------------------------------

def test_a_scan_queued_while_held_comes_back_for_its_own_folders(cfg, tmp_path):
    scanner = Scanner(cfg)
    asked = []
    scanner.defer("a test")
    scanner.start([tmp_path / "one-drive"])
    scanner.start = lambda roots=None, full=False: asked.append((list(roots), full))
    scanner.resume("a test")
    assert asked == [([tmp_path / "one-drive"], False)]


def test_a_hand_over_keeps_the_folders_the_scan_had_not_reached(cfg, tmp_path):
    scanner = Scanner(cfg)
    scanner.watch = lambda roots=(): None
    a, b, c = (tmp_path / name for name in "abc")
    for folder in (a, b, c):
        folder.mkdir()
    let_go = threading.Event()
    ran = []

    def fake_run(root, full, label=None):
        ran.append(root)
        if root == a and len(ran) == 1:
            scanner.progress.update(status="tagging")
            while not scanner._stop.is_set() and not let_go.is_set():
                time.sleep(0.01)

    scanner._run = fake_run
    scanner.start([a, b])
    assert _wait_for(lambda: scanner.progress.snapshot().get("status") == "tagging")
    scanner.start([c])                    # analysing: it hands over at once
    assert _wait_for(lambda: not scanner.running and len(ran) >= 4)
    assert ran[0] == a
    assert sorted(ran[1:]) == sorted([c, a, b])


# -- restores and the bin ----------------------------------------------------------

def test_a_restore_queues_its_scan_before_letting_the_indexer_go(cfg, tmp_path):
    calls = []
    service = CloudService(cfg, lambda: db.init_db(cfg.db_path))
    service.scanner = SimpleNamespace(
        start=lambda roots, full=False: calls.append(("start", roots)),
        resume=lambda reason: calls.append(("resume", reason)))
    cfg.roots = [str(tmp_path)]
    job = SimpleNamespace(touched_roots=[str(tmp_path)], destination=None)
    try:
        service._restore_done(job)
    except AttributeError:
        pass                              # the rest is the report, not asked here
    assert [c[0] for c in calls] == ["start", "resume"]


# -- imports ---------------------------------------------------------------------

def _jpeg(size=(400, 300)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(out, "JPEG", quality=95)
    return out.getvalue()


def test_an_import_holds_the_indexer_and_skips_what_the_library_leaves_out(
        scanned, tmp_path):
    cfg, conn, _ = scanned
    exports = tmp_path / "exports"
    exports.mkdir()
    big = _jpeg((1200, 900))
    with zipfile.ZipFile(exports / "WhatsApp Chat with Amma.zip", "w") as z:
        z.writestr("IMG-20240101-WA0001.jpg", big)
        z.writestr("STK-20240101-WA0002.webp", b"RIFF" + b"\0" * 200)
        z.writestr("WhatsApp Chat with Amma.txt", "01/01/2024, 10:00 - Amma: <attached>\n")
    cfg.min_media_bytes = len(big) - 1
    calls = []
    fake = SimpleNamespace(defer=lambda reason: calls.append(("defer", reason)),
                           resume=lambda reason: calls.append(("resume", reason)),
                           start=lambda roots, full=False: calls.append(("start", roots)),
                           add_listener=lambda _fn: None)
    importer = Importer(cfg, lambda: db.connect(cfg.db_path), scanner=fake)
    assert importer.start(str(exports), cfg.active_root, None)["started"]
    importer.join(60)
    status = importer.status()
    assert calls[0] == ("defer", CLAIM_IMPORT)
    assert calls[-1] == ("resume", CLAIM_IMPORT)
    assert ("start", [status["destination"]]) in calls
    assert status["copied"] == 1 and status["skipped"] >= 1


# -- a scan that found nothing new ----------------------------------------------

def test_an_unchanged_library_is_not_linked_again(scanned, monkeypatch):
    cfg, conn, scanner = scanned
    linked = []
    monkeypatch.setattr(scanner, "_link_duplicates", lambda c, r: linked.append(r))
    scanner._run(Path(cfg.active_root), full=False)
    assert linked == []                   # nothing changed since the fixture's scan

    conn.execute("UPDATE assets SET trashed=1 WHERE id=(SELECT MIN(id) FROM assets)")
    conn.commit()
    scanner._run(Path(cfg.active_root), full=False)
    assert linked == [cfg.active_root]    # something went to the bin


def test_the_place_name_list_is_tried_once_a_day(scanned, monkeypatch):
    cfg, conn, scanner = scanned
    from ninaivu.utils import places
    cfg.place_names = True
    tries = []
    monkeypatch.setattr(places, "installed", lambda _s: False)
    monkeypatch.setattr(places, "install", lambda _s: tries.append(1) or (_ for _ in ()).throw(OSError("offline")))
    scanner._name_places(conn, cfg.active_root)
    scanner._name_places(conn, cfg.active_root)
    assert tries == [1]
    db.set_meta(conn, scanner_mod.PLACES_TRIED, str(time.time() - 25 * 3600))
    scanner._name_places(conn, cfg.active_root)
    assert tries == [1, 1]


def test_place_names_do_not_wait_behind_tagging():
    import inspect
    source = inspect.getsource(Scanner._run)
    assert source.index("self._name_places, self._draw_audio_art") < source.index(
        "passes.append(self._tag)")


# -- start-up does not wait for the model ----------------------------------------

def test_the_scan_waits_for_the_model_only_where_it_needs_it(cfg):
    scanner = Scanner(cfg)
    ready = threading.Event()
    scanner.model_ready = ready
    waited = threading.Event()
    thread = threading.Thread(target=lambda: (scanner._wait_for_the_model(), waited.set()))
    thread.start()
    time.sleep(0.3)
    assert not waited.is_set()
    assert "image model" in scanner.progress.snapshot().get("message", "")
    ready.set()
    assert waited.wait(2)


def test_a_stop_ends_the_wait_for_the_model(cfg):
    scanner = Scanner(cfg)
    scanner.model_ready = threading.Event()
    scanner._stop.set()
    began = time.monotonic()
    scanner._wait_for_the_model()
    assert time.monotonic() - began < 2


# -- people appear even when the face pass was stopped at the end -----------------

def test_faces_found_by_a_stopped_pass_are_grouped_by_the_next_scan(scanned, monkeypatch):
    cfg, conn, scanner = scanned
    grouped = []
    remaining = [3]

    def detect_pass(conn, root, should_stop, on_progress):
        remaining[0] = 0
        scanner._stop.set()               # stopped just after the last photograph
        return {}

    fake = SimpleNamespace(engine=SimpleNamespace(available=True),
                           detect_pass=detect_pass,
                           regroup=lambda conn, root, **_: grouped.append(root) or {})
    monkeypatch.setattr(scanner, "_face_indexer", lambda: fake)
    monkeypatch.setattr(db, "count_assets_needing_faces", lambda *_a: remaining[0])
    scanner._find_faces(conn, cfg.active_root)
    assert grouped == []
    scanner._stop.clear()
    scanner._find_faces(conn, cfg.active_root)   # nothing left to look at
    assert grouped == [cfg.active_root]
    scanner._find_faces(conn, cfg.active_root)
    assert grouped == [cfg.active_root]          # and only once
