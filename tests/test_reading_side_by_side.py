"""Text reading and face finding work on several photographs at once on a
machine with room, each with its own engine, and what they find is written
on the scan's own thread in the same order as before."""

import threading
from types import SimpleNamespace

import pytest

from ninaivu.media import faceindex, faces as faces_mod, ocr
from ninaivu.media.scanner import Scanner


def test_text_is_read_by_several_readers_and_stored_in_order(scanned, monkeypatch):
    cfg, conn, _ = scanned
    cfg.ocr_enabled = True
    cfg.workers = 4
    readers: set[str] = set()
    seen = threading.Barrier(2, timeout=5)

    def read(path, **_):
        readers.add(threading.current_thread().name)
        try:
            seen.wait()             # two readers are inside at once, or this times out
        except threading.BrokenBarrierError:
            pass
        return {"text": f"WORDS {path.name}"}

    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "read", read)
    conn.execute("UPDATE assets SET ocr_version=0")
    conn.commit()
    stored: list[int] = []
    from ninaivu.storage import db
    keep = db.store_ai_fields

    def store(c, asset_id, **fields):
        assert threading.current_thread() is threading.main_thread()
        stored.append(asset_id)
        return keep(c, asset_id, **fields)

    monkeypatch.setattr(db, "store_ai_fields", store)
    Scanner(cfg)._read_text(conn, cfg.active_root)

    assert len(readers) >= 2 and not any(n == "MainThread" for n in readers)
    assert stored and stored == sorted(stored), "written in the list's order"
    left = conn.execute("SELECT COUNT(*) FROM assets WHERE kind='picture' "
                        "AND thumb IS NOT NULL AND ocr_version=0").fetchone()[0]
    assert left == 0
    assert conn.execute("SELECT COUNT(*) FROM assets WHERE ocr_text LIKE 'WORDS %'"
                        ).fetchone()[0] == len(stored)


def test_a_reading_thread_builds_its_own_engine_on_a_share_of_the_cores(monkeypatch):
    made = []

    class Reader:
        def __init__(self, params=None):
            made.append(params)

        def __call__(self, array):
            return None

    monkeypatch.setattr(ocr, "_reader_class", lambda: Reader)
    out = {}

    def work():
        ocr.own_engine_per_thread(3)
        out["first"] = ocr._own_engine()
        out["again"] = ocr._own_engine()

    worker = threading.Thread(target=work)
    worker.start()
    worker.join()
    assert out["first"] is out["again"], "one engine per thread, kept"
    assert made == [{"EngineConfig.onnxruntime.intra_op_num_threads": 3,
                     "EngineConfig.onnxruntime.inter_op_num_threads": 1}]
    assert ocr._own_engine() is None, "other threads share the one engine"


@pytest.mark.parametrize("workers, finders", [(1, 1), (4, 4), (12, 12), (20, 20)])
def test_the_face_pass_finds_on_as_many_threads_as_the_scan_indexes_on(
        workers, finders, monkeypatch):
    import psutil
    monkeypatch.setattr(psutil, "virtual_memory",
                        lambda: SimpleNamespace(available=64 * 1024 ** 3))
    assert faceindex.finders_for(SimpleNamespace(workers=workers)) == finders


def test_no_more_face_finders_than_the_free_memory_holds(monkeypatch):
    import psutil
    monkeypatch.setattr(psutil, "virtual_memory",
                        lambda: SimpleNamespace(available=3 * faceindex.FINDER_BYTES))
    assert faceindex.finders_for(SimpleNamespace(workers=20)) == 1
    monkeypatch.setattr(psutil, "virtual_memory",
                        lambda: SimpleNamespace(available=12 * faceindex.FINDER_BYTES))
    assert faceindex.finders_for(SimpleNamespace(workers=20)) == 6


def test_with_the_real_engine_faces_are_found_on_the_reading_threads(scanned, monkeypatch):
    cfg, conn, _ = scanned
    cfg.workers = 3
    # Room for three finders whatever the runner has free (a macOS runner
    # had room for one, so the pass never went side by side).
    import psutil
    monkeypatch.setattr(psutil, "virtual_memory",
                        lambda: SimpleNamespace(available=12 * faceindex.FINDER_BYTES))
    threads: set[str] = set()
    engines: set[int] = set()

    def init(self, state_dir):
        self.state_dir = state_dir
        self._lock = threading.Lock()

    def detect(self, image):
        threads.add(threading.current_thread().name)
        engines.add(id(self))
        return []

    monkeypatch.setattr(faces_mod.FaceEngine, "__init__", init)
    monkeypatch.setattr(faces_mod.FaceEngine, "available", property(lambda self: True))
    monkeypatch.setattr(faces_mod.FaceEngine, "detect", detect)
    monkeypatch.setattr(faces_mod, "load_image_for_faces", lambda path, rot: object())

    indexer = faceindex.FaceIndexer(cfg, engine=faces_mod.FaceEngine(cfg.state_dir))
    result = indexer.detect_pass(conn, cfg.active_root)

    assert result["ok"] and result["scanned"] > 0 and result["failed"] == 0
    assert "MainThread" not in threads
    assert len(engines) >= 1 and id(indexer.engine) not in engines, \
        "each thread with its own copy, the shared one left for the console"
    assert faces_mod.cv2 is None or faces_mod.cv2.getNumThreads() == 1


def test_with_room_for_one_finder_originals_are_not_queued_eight_deep(
        scanned, monkeypatch):
    """Too little free memory for two finders fell back to the readers that
    open up to eight full-size originals ahead of the one detector."""
    import psutil
    import time
    cfg, conn, _ = scanned
    cfg.workers = 8
    monkeypatch.setattr(psutil, "virtual_memory",
                        lambda: SimpleNamespace(available=faceindex.FINDER_BYTES * 3))
    lock = threading.Lock()
    state = {"open": 0, "most": 0}

    def init(self, state_dir):
        self.state_dir = state_dir
        self._lock = threading.Lock()

    def load(path, rot):
        with lock:
            state["open"] += 1
            state["most"] = max(state["most"], state["open"])
        time.sleep(0.02)
        with lock:
            state["open"] -= 1
        return object()

    monkeypatch.setattr(faces_mod.FaceEngine, "__init__", init)
    monkeypatch.setattr(faces_mod.FaceEngine, "available", property(lambda self: True))
    monkeypatch.setattr(faces_mod.FaceEngine, "detect", lambda self, image: [])
    monkeypatch.setattr(faces_mod, "load_image_for_faces", load)
    indexer = faceindex.FaceIndexer(cfg, engine=faces_mod.FaceEngine(cfg.state_dir))
    result = indexer.detect_pass(conn, cfg.active_root)
    assert result["scanned"] > 2
    assert state["most"] == 1


def test_a_stand_in_engine_is_still_asked_from_the_scan_thread_only(scanned, monkeypatch):
    cfg, conn, _ = scanned
    cfg.workers = 3
    threads: set[str] = set()

    class Engine:
        available = True
        model_id = "fake"
        state_dir = cfg.state_dir

        def detect(self, image):
            threads.add(threading.current_thread().name)
            return []

    monkeypatch.setattr(faces_mod, "load_image_for_faces", lambda path, rot: object())
    result = faceindex.FaceIndexer(cfg, engine=Engine()).detect_pass(conn, cfg.active_root)
    assert result["scanned"] > 0
    assert threads == {threading.current_thread().name}


def test_readers_loading_together_leave_logging_as_they_found_it(monkeypatch):
    """Each engine loads with INFO muted; two loading at once used to put
    back each other's mute, and the process logged nothing at INFO after."""
    import logging
    import time
    from concurrent.futures import ThreadPoolExecutor

    class Reader:
        def __init__(self, params=None):
            time.sleep(0.05)

        def __call__(self, array):
            return None

    monkeypatch.setattr(ocr, "_reader_class", lambda: Reader)
    before = logging.root.manager.disable
    try:
        with ThreadPoolExecutor(4, initializer=ocr.own_engine_per_thread,
                                initargs=(1,)) as pool:
            engines = list(pool.map(lambda _: ocr._own_engine(), range(8)))
        assert all(engines)
        assert logging.root.manager.disable == before
    finally:
        logging.disable(before)


def test_no_more_text_readers_than_the_free_memory_holds(scanned, monkeypatch):
    """Each reader holds its own engine, measured at 400 to 600 MB; eight on
    a small machine with many cores took more than it had free."""
    import psutil
    cfg, conn, _ = scanned
    cfg.ocr_enabled = True
    cfg.workers = 8
    monkeypatch.setattr(psutil, "virtual_memory",
                        lambda: SimpleNamespace(available=3 * 500 * 1024 ** 2))
    readers: set[str] = set()

    def read(path, **_):
        readers.add(threading.current_thread().name)
        return {"text": ""}

    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "read", read)
    conn.execute("UPDATE assets SET ocr_version=0")
    conn.commit()
    Scanner(cfg)._read_text(conn, cfg.active_root)
    assert len(readers) == 1
