"""Work that was going on when Ninaivu stopped carries on when it starts again.

"Analysing with AI" did not come back after a restart on the Mac, and neither
did a memory film, a photo book or an import that was being made: each looked
like something nobody had asked for. Every job that was running or waiting
when the server stopped is picked up at the next start, with the job engine
on and off; one somebody stopped stays stopped.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from ninaivu import Services
from ninaivu.media import books, films
from ninaivu.storage import books as book_store, db, films as film_store, resume


# -- "Analysing with AI": the scan's analysis ---------------------------------------

class SlowModel:
    """An image model a batch at a time slow enough to be stopped part way."""

    name = "fake"
    model_id = "fake/slow"
    semantic = False
    device = "cpu"

    def analyse(self, paths, prepared=None):
        time.sleep(0.3)
        return [{"tags": ["beach"]} for _ in paths]


def _owed(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM assets WHERE ai_version=0").fetchone()[0]


def _until(check, seconds: float = 30.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture()
def analysing(scanned, monkeypatch):
    import ninaivu.ai as ai

    cfg, conn, _ = scanned
    cfg.watch = True
    cfg.ai_enabled = True
    cfg.ai_engine = "clip"
    cfg.clip_batch_size = 2
    conn.execute("UPDATE assets SET ai_version=0")
    conn.commit()
    monkeypatch.setattr(ai, "build_engine", lambda cfg, force=False: SlowModel())
    monkeypatch.setattr(ai, "bind", lambda engine, cfg: engine)
    return cfg, conn


def test_analysis_stopped_by_a_restart_carries_on(analysing):
    cfg, conn = analysing
    first = Services(cfg)
    first.start()
    try:
        assert _until(lambda: first.scanner.progress.snapshot().get("status") == "tagging")
        assert _until(lambda: _owed(conn) < 15)
    finally:
        first.stop(timeout=10)
    left = _owed(conn)
    assert left > 0, "the analysis finished before the restart"

    second = Services(cfg)
    second.start()
    try:
        assert _until(lambda: _owed(conn) == 0), f"{_owed(conn)} of {left} still owed"
    finally:
        second.stop(timeout=10)


def test_analysis_held_for_the_daily_catch_up_waits_for_it_after_a_restart(analysing):
    """Held outside the catch-up's stretch before the restart, and still held
    after it: carried on, waiting for its turn, not run straight away."""
    cfg, conn = analysing
    cfg.daily_catchup = True
    cfg.daily_catchup_at = time.strftime("%H:%M", time.localtime(time.time() + 3 * 3600))
    cfg.daily_catchup_hours = 1
    first = Services(cfg)
    first.start()
    try:
        assert _until(lambda: first.scanner.progress.snapshot().get("held"))
    finally:
        first.stop(timeout=10)
    owed = _owed(conn)

    second = Services(cfg)
    second.start()
    try:
        assert _until(lambda: second.scanner.running
                      and second.scanner.progress.snapshot().get("held"))
        assert _owed(conn) == owed
    finally:
        second.stop(timeout=10)


# -- memory films and photo books ---------------------------------------------------

def _blocking(made: list, module):
    """A render that waits to be stopped the first time and finishes the next."""

    def render(spec, out, *, report=None, cancelled=None):
        made.append(spec)
        if len(made) == 1:
            while not cancelled():
                time.sleep(0.01)
            raise module.Cancelled()
        Path(out).write_bytes(b"made")
        return {"seconds": 3.0} if module is films else 2

    return render


@pytest.mark.parametrize("module", [films, books], ids=["film", "book"])
def test_a_film_or_book_being_made_is_made_again_after_a_restart(scanned, monkeypatch,
                                                                 module):
    cfg, conn, _ = scanned
    module.reset()
    made: list = []
    monkeypatch.setattr(module, "render" if module is films else "render_book",
                        _blocking(made, module))
    if module is films:
        made_id = film_store.create(conn, owner_id=1, title="Our year", subtitle="",
                                    source_kind="album", source_id=1, size="720p",
                                    subtitles="off", music_id=None, item_ids=[1],
                                    story_ids=[])
        store = film_store
    else:
        made_id = book_store.create(conn, owner_id=1, title="Our year", subtitle="",
                                    source_kind="album", source_id=1, template="classic",
                                    size="a4", bleed=False, captions=True, item_ids=[1])
        store = book_store
    spec = {"title": "Our year", "items": [{"id": 1, "path": "/x.jpg"}]}
    module.start(made_id, spec, db_path=cfg.db_path, state_dir=cfg.state_dir)
    assert _until(lambda: made)

    first = Services(cfg)
    first.stop(timeout=5)                        # Ninaivu stopping
    assert _until(lambda: module.progress(made_id) is None)
    assert store.get(conn, made_id)["state"] == "building", "recorded as failed"
    assert f"{module.RESUME_PREFIX}{made_id}" in resume.wanted(conn)

    second = Services(cfg)
    second.start()
    try:
        assert module.wait(made_id, 30) == "done"
        assert made[1] == spec
        assert store.get(conn, made_id)["state"] == "done"
        assert f"{module.RESUME_PREFIX}{made_id}" not in resume.wanted(conn)
    finally:
        second.stop(timeout=5)
        module.reset()


@pytest.mark.parametrize("module", [films, books], ids=["film", "book"])
def test_a_film_or_book_somebody_cancelled_is_not_made_again(scanned, monkeypatch, module):
    cfg, conn, _ = scanned
    module.reset()
    made: list = []
    monkeypatch.setattr(module, "render" if module is films else "render_book",
                        _blocking(made, module))
    if module is films:
        made_id = film_store.create(conn, owner_id=1, title="t", subtitle="",
                                    source_kind="album", source_id=1, size="720p",
                                    subtitles="off", music_id=None, item_ids=[1],
                                    story_ids=[])
    else:
        made_id = book_store.create(conn, owner_id=1, title="t", subtitle="",
                                    source_kind="album", source_id=1, template="classic",
                                    size="a4", bleed=False, captions=True, item_ids=[1])
    module.start(made_id, {"title": "t"}, db_path=cfg.db_path, state_dir=cfg.state_dir)
    assert _until(lambda: made)
    module.cancel(made_id)
    assert module.wait(made_id, 10) == "cancelled"
    assert f"{module.RESUME_PREFIX}{made_id}" not in resume.wanted(conn)
    module.reset()


# -- an import --------------------------------------------------------------------

def test_an_import_carries_on_after_a_restart_and_not_after_stop(scanned, monkeypatch,
                                                                 tmp_path):
    cfg, conn, _ = scanned
    export = tmp_path / "export"
    export.mkdir()
    services = Services(cfg)
    started: list[tuple] = []
    gate = threading.Event()

    def run(folder, root, user_id):
        started.append((str(folder), root, user_id))
        gate.wait(10)

    monkeypatch.setattr(services.importer, "_run", run)
    services.importer.start(str(export), cfg.active_root, 7)
    assert resume.wanted(conn)[services.importer.RESUME_NAME] == {
        "folder": str(export), "root": cfg.active_root, "user_id": 7}
    gate.set()
    services.importer.join(5)

    again = Services(cfg)
    monkeypatch.setattr(again.importer, "_run", run)
    again._resume_jobs(downloads=False)
    again.importer.join(5)
    assert started[-1] == (str(export), cfg.active_root, 7)

    # Stopped by somebody: crossed off, and not started again.
    again.importer.stop(join=True, forget=True)
    assert services.importer.RESUME_NAME not in resume.wanted(conn)
    started.clear()
    again._resume_jobs(downloads=False)
    assert started == []
    db.close_all()
