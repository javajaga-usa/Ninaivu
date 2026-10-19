"""Reading text and finding faces run beside an image model on a graphics
processor, instead of waiting for it to describe the whole library.

On a first scan with the model on a graphics processor the processor sat all
but idle for hours, then the text and face passes ran on it after. On a
twenty-core machine that was most of the machine doing nothing for most of
the scan. Only the timing changes: the same passes run on the same
photographs; on the processor alone they still run after the model.
"""

import threading
import time
from pathlib import Path

from ninaivu.media.scanner import Scanner


class _Model:
    name = "stand-in"
    device = "gpu"
    model_id = "stand-in"
    semantic = False

    def __init__(self, on_graphics: bool) -> None:
        self.reads_ahead = on_graphics

    def prepare(self, paths):
        return None

    def analyse(self, paths, prepared=None):
        return [{"tags": ["x"]} for _ in paths]


def _scan(scanned, monkeypatch, on_graphics: bool):
    cfg, conn, _ = scanned
    conn.execute("UPDATE assets SET ai_version=0")
    conn.commit()
    cfg.ai_enabled = True
    cfg.ocr_enabled = True
    cfg.faces_enabled = True
    seen: dict[str, tuple[str, float, float]] = {}
    tagging = threading.Event()

    def recorded(name, wait_for_tagging=False):
        def run(self, conn, root):
            began = time.monotonic()
            if wait_for_tagging:
                # Beside the model, the text pass starts while it is still
                # describing: this waits to see it.
                tagging.wait(5)
            if name == "_tag":
                tagging.set()
                time.sleep(0.3)
            self.progress.bump(errors=1 if name == "_read_text" else 0)
            seen[name] = (threading.current_thread().name, began, time.monotonic())
        run.__name__ = name
        return run

    monkeypatch.setattr(Scanner, "_tag", recorded("_tag"))
    monkeypatch.setattr(Scanner, "_tag_video_keyframes", recorded("_tag_video_keyframes"))
    monkeypatch.setattr(Scanner, "_read_text", recorded("_read_text", on_graphics))
    monkeypatch.setattr(Scanner, "_find_faces", recorded("_find_faces"))
    scanner = Scanner(cfg, _Model(on_graphics))
    scanner._run(Path(cfg.active_root), full=False)
    return scanner, seen


def test_with_the_model_on_a_graphics_processor_they_run_beside_it(scanned, monkeypatch):
    scanner, seen = _scan(scanned, monkeypatch, on_graphics=True)
    model_thread = seen["_tag"][0]
    assert seen["_read_text"][0] != model_thread
    assert seen["_find_faces"][0] == seen["_read_text"][0]
    assert seen["_read_text"][1] < seen["_tag"][2], "text reading waited for the model"
    snapshot = scanner.progress.snapshot()
    assert snapshot["status"] == "done"
    # An error the pass beside the model counted is still the scan's.
    assert snapshot["errors"] >= 1


def test_on_the_processor_alone_they_still_run_after_it(scanned, monkeypatch):
    scanner, seen = _scan(scanned, monkeypatch, on_graphics=False)
    threads = {who for who, _, _ in seen.values()}
    assert len(threads) == 1
    assert seen["_read_text"][1] >= seen["_tag"][2]
    assert scanner.progress.snapshot()["errors"] >= 1


def test_a_stopped_scan_waits_for_the_passes_beside_it(scanned, monkeypatch):
    """Never a scan said to be over with a pass of it still running."""
    cfg, conn, _ = scanned
    conn.execute("UPDATE assets SET ai_version=0")
    conn.commit()
    cfg.ai_enabled = cfg.ocr_enabled = True
    cfg.faces_enabled = False
    started, finished = threading.Event(), threading.Event()
    scanner = Scanner(cfg, _Model(True))

    def tag(self, conn, root):
        started.wait(5)
        self._stop.set()

    def read_text(self, conn, root):
        started.set()
        time.sleep(0.2)
        finished.set()

    read_text.__name__ = "_read_text"
    monkeypatch.setattr(Scanner, "_tag", tag)
    monkeypatch.setattr(Scanner, "_read_text", read_text)
    scanner._run(Path(cfg.active_root), full=False)
    assert finished.is_set()
    assert not any(t.name == "ninaivu-analysis-beside" for t in threading.enumerate())
