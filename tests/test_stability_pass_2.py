"""The second stability pass of October 2026: a storage check that can be
stopped and lets new files be indexed, a library move that stops every job
writing under the old folder, and an HTTPS listener with a bound.
"""

import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ninaivu.api import admin_api, migration_api
from ninaivu.media.scanner import CLAIM_STORAGE_CHECK, Scanner
from ninaivu.server import http
from ninaivu.storage import db, resume


def _wait_for(predicate, timeout=5.0):
    ends = time.monotonic() + timeout
    while time.monotonic() < ends:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _asset(conn, root: Path, name: str) -> int:
    path = root / name
    path.write_bytes(name.encode() * 10)
    st = path.stat()
    cur = conn.execute(
        "INSERT INTO assets(root, rel_path, filename, kind, size, mtime, "
        "date_key, visibility, indexed_at) "
        "VALUES(?,?,?,'picture',?,?,'2024/01/01',1,?)",
        (str(root), name, name, st.st_size, st.st_mtime, time.time()))
    conn.commit()
    return int(cur.lastrowid)


@pytest.fixture()
def checked(tmp_path, cfg):
    conn = db.init_db(cfg.db_path)
    root = tmp_path / "lib"
    root.mkdir(exist_ok=True)
    ids = [_asset(conn, root, f"p{i}.jpg") for i in range(5)]
    yield cfg, conn, ids
    admin_api.stop_scrubber(timeout=5)


class _Gate:
    """A workload that holds the check before each file until let through."""

    def __init__(self):
        self.go = threading.Event()
        self.asked = 0

    def wait_turn(self, _kind, on_hold=None):
        self.asked += 1
        if self.asked > 2:
            self.go.wait(5)


# -- stopping the storage check ---------------------------------------------------

def test_the_storage_check_stops_between_files_and_is_not_resumed(checked):
    cfg, conn, ids = checked
    gate = _Gate()
    scanner = SimpleNamespace(workload=gate, defer=lambda _r: True,
                              resume=lambda _r=None: False)
    finished = []
    assert admin_api.start_scrubber_job(cfg.db_path, scanner=scanner,
                                        on_done=lambda: finished.append(True))
    assert _wait_for(lambda: gate.asked >= 3)

    assert admin_api.stop_scrubber() is True
    gate.go.set()
    admin_api.stop_scrubber(timeout=5)

    assert not admin_api.scrubber_running()
    assert finished == []                     # no repair after a stopped check
    assert admin_api.SCRUBBER_RESUME not in resume.wanted(conn)
    assert admin_api._SCRUBBER_PROGRESS["stopped_at"] == ids[2]


def test_stopping_when_nothing_runs_says_so():
    assert admin_api.stop_scrubber() is False


def test_the_check_holds_the_indexer_and_lets_it_go(checked):
    cfg, _conn, _ids = checked
    calls = []
    scanner = SimpleNamespace(defer=lambda r: calls.append(("defer", r)),
                              resume=lambda r=None: calls.append(("resume", r)))
    assert admin_api.start_scrubber_job(cfg.db_path, scanner=scanner)
    assert _wait_for(lambda: not admin_api.scrubber_running())
    assert calls == [("defer", CLAIM_STORAGE_CHECK), ("resume", CLAIM_STORAGE_CHECK)]


# -- letting the indexer through -------------------------------------------------

class _FakeScanner:
    def __init__(self, held_elsewhere=False):
        self.waiting = True
        self.queued_requests = 1
        self.running = False
        self.calls = []
        self.held_elsewhere = held_elsewhere
        self.status = "paused"
        self.progress = SimpleNamespace(snapshot=lambda: {"status": self.status})

    def resume(self, claim):
        self.calls.append("resume")
        if self.held_elsewhere:
            return False
        self.running = True
        self.status = "walking"
        # The scan reads the new files, then moves on to analysis.
        threading.Timer(0.3, lambda: setattr(self, "status", "tagging")).start()
        return True

    def defer(self, claim):
        self.calls.append("defer")
        self.waiting = True


def test_a_scan_asked_for_runs_its_indexing_then_the_check_takes_over():
    scanner = _FakeScanner()
    between = admin_api._letting_the_indexer_through(scanner, CLAIM_STORAGE_CHECK)
    began = time.monotonic()
    between()
    assert scanner.calls == ["resume", "defer"]
    assert time.monotonic() - began >= 0.25    # waited while it indexed

    # Nothing new asked for since: the analysis it stood down stays queued.
    between()
    assert scanner.calls == ["resume", "defer"]

    scanner.queued_requests += 1               # a photo copied in
    between()
    assert scanner.calls == ["resume", "defer"] * 2


def test_nothing_is_let_through_while_another_job_holds_the_indexer():
    scanner = _FakeScanner(held_elsewhere=True)
    between = admin_api._letting_the_indexer_through(scanner, CLAIM_STORAGE_CHECK)
    between()
    between()
    assert scanner.calls == ["resume", "defer"]


def test_the_scanner_counts_scans_queued_while_it_is_held(cfg):
    scanner = Scanner(cfg)
    scanner.watch = lambda roots=(): None
    scanner.defer(CLAIM_STORAGE_CHECK)
    assert not scanner.waiting and scanner.queued_requests == 0
    scanner.start(full=False)
    assert scanner.waiting and scanner.queued_requests == 1
    scanner._defer_pending = None
    scanner.resume(CLAIM_STORAGE_CHECK)


# -- a library move stops every job ----------------------------------------------

class _Job:
    def __init__(self):
        self.stopped = threading.Event()
        self._thread = threading.Thread(target=self.stopped.wait, args=(5,),
                                        daemon=True)
        self._thread.start()

    @property
    def running(self):
        return self._thread.is_alive()

    def stop(self, join=False):
        self.stopped.set()


def test_moving_the_library_stops_and_waits_for_every_job():
    engine = _Job()
    cloud = SimpleNamespace(_engine=engine, pause=lambda: engine.stop())
    services = SimpleNamespace(
        straightener=None, cloud=cloud, mirror=_Job(), offsite=_Job(),
        repairer=_Job(), importer=SimpleNamespace(running=False))
    stopped = migration_api._stand_everything_down(services)
    assert stopped == ["the cloud upload", "the second copy",
                       "the off-site copy", "the repair"]
    for job in (engine, services.mirror, services.offsite, services.repairer):
        assert not job.running


# -- the HTTPS listener ----------------------------------------------------------

def _app(environ, start_response):
    start_response("200 OK", [("Content-Type", "text/plain"),
                              ("Content-Length", "2")])
    return [b"ok"]


def _get(port, timeout=5.0):
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        return s.recv(1024)


def test_the_listener_bound_follows_the_web_threads():
    server = http.make_threaded_server("127.0.0.1", 0, _app, threads=2)
    assert server.max_connections == http.DEFAULT_CONNECTIONS
    server.server_close()
    server = http.make_threaded_server("127.0.0.1", 0, _app, threads=16)
    assert server.max_connections == 64
    server.server_close()


def test_connections_beyond_the_bound_wait_and_idle_ones_are_closed(monkeypatch):
    monkeypatch.setattr(http.IdleClosingHandler, "timeout", 1)
    server = http.make_threaded_server("127.0.0.1", 0, _app)
    server.max_connections = 2
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_port
        idle = [socket.create_connection(("127.0.0.1", port)) for _ in range(2)]
        time.sleep(0.2)
        began = time.monotonic()
        assert _get(port).startswith(b"HTTP/1.1 200")
        # Served only once an idle connection was closed for being idle.
        assert time.monotonic() - began >= 0.5
        for s in idle:
            s.close()
        # Slots come back: many requests in a row are all answered.
        for _ in range(10):
            assert _get(port).startswith(b"HTTP/1.1 200")
    finally:
        server.shutdown()
        server.server_close()


def test_a_full_listener_still_stops():
    server = http.make_threaded_server("127.0.0.1", 0, _app)
    server.max_connections = 1
    loop = threading.Thread(target=server.serve_forever, daemon=True)
    loop.start()
    held = [socket.create_connection(("127.0.0.1", server.server_port))
            for _ in range(2)]
    time.sleep(0.3)
    began = time.monotonic()
    server.shutdown()
    assert time.monotonic() - began < 3
    for s in held:
        s.close()
    server.server_close()
