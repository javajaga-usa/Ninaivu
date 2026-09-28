"""OrientNet follows `orientnet.configure()`, on every thread.

`_model_dir` is set process-wide, and each thread keeps its own cached network.
When the directory changed, threads kept their old network loaded from the
previous directory. Like `archive.database`, threads must check `_local.path`
and discard stale networks when the configured directory changes.
"""

import threading
from pathlib import Path
from unittest.mock import MagicMock

from ninaivu.media import orientnet


def _make_dummy_model(state_dir: Path) -> Path:
    """Write a stand-in model, and return the folder to configure with.

    `orientnet.configure` takes the folder holding the model file: the shared
    AI model folder in a real install, one of these in a test.
    """
    models = state_dir / "models"
    models.mkdir(parents=True, exist_ok=True)
    (models / orientnet.MODEL["file"]).write_bytes(b"dummy onnx weights")
    return models


def test_a_thread_reloads_network_when_model_dir_changes_across_threads(
        tmp_path, monkeypatch):
    first, second = tmp_path / "one", tmp_path / "two"
    first_models = _make_dummy_model(first)
    second_models = _make_dummy_model(second)

    monkeypatch.setattr(orientnet, "_opencv_support",
                        lambda: {"available": True, "version": "4.10.0"})

    created_nets: dict[str, object] = {}

    def fake_read(path_str: str):
        net = MagicMock()
        net.path = path_str
        created_nets[path_str] = net
        return net

    mock_cv2 = MagicMock()
    mock_cv2.dnn.readNetFromONNX = fake_read
    monkeypatch.setattr(orientnet, "cv2", mock_cv2)

    orientnet.reset()
    orientnet.configure(first_models)

    seen: dict[str, object] = {}
    moved = threading.Event()
    done = threading.Event()

    def worker():
        seen["before"] = orientnet._net()
        moved.wait(5)
        seen["after"] = orientnet._net()
        orientnet.reset()
        done.set()

    thread = threading.Thread(target=worker)
    thread.start()

    for _ in range(200):
        if "before" in seen:
            break
        threading.Event().wait(0.01)

    # Main thread points orientnet at second directory
    orientnet.configure(second_models)
    moved.set()
    assert done.wait(5)
    thread.join(5)

    assert seen["before"] is not None
    assert seen["after"] is not None
    assert seen["before"] is not seen["after"], (
        "the thread kept using the network from the old directory")
    assert seen["before"].path.endswith("one" + "\\" + "models" + "\\" + orientnet.MODEL["file"]) or \
           seen["before"].path.endswith("one/models/" + orientnet.MODEL["file"])
    assert seen["after"].path.endswith("two" + "\\" + "models" + "\\" + orientnet.MODEL["file"]) or \
           seen["after"].path.endswith("two/models/" + orientnet.MODEL["file"])


def test_the_same_path_keeps_the_same_network(tmp_path, monkeypatch):
    """One network per thread, reused across predictions."""
    state_dir = tmp_path / "state"
    models = _make_dummy_model(state_dir)
    monkeypatch.setattr(orientnet, "_opencv_support",
                        lambda: {"available": True, "version": "4.10.0"})

    call_count = [0]

    def fake_read(path_str: str):
        call_count[0] += 1
        net = MagicMock()
        net.path = path_str
        return net

    mock_cv2 = MagicMock()
    mock_cv2.dnn.readNetFromONNX = fake_read
    monkeypatch.setattr(orientnet, "cv2", mock_cv2)

    orientnet.reset()
    orientnet.configure(models)
    try:
        first = orientnet._net()
        second = orientnet._net()
        assert first is second
        assert call_count[0] == 1
    finally:
        orientnet.reset()


def test_reset_clears_cached_net_and_path(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    models = _make_dummy_model(state_dir)
    monkeypatch.setattr(orientnet, "_opencv_support",
                        lambda: {"available": True, "version": "4.10.0"})
    mock_cv2 = MagicMock()
    mock_cv2.dnn.readNetFromONNX = lambda p: MagicMock()
    monkeypatch.setattr(orientnet, "cv2", mock_cv2)

    orientnet.reset()
    orientnet.configure(models)
    try:
        net = orientnet._net()
        assert net is not None
        assert getattr(orientnet._local, "net", None) is not None
        assert getattr(orientnet._local, "path", None) is not None

        orientnet.reset()
        assert getattr(orientnet._local, "net", None) is None
        assert getattr(orientnet._local, "path", None) is None
    finally:
        orientnet.reset()


def test_reconfiguring_to_missing_model_discards_stale_net(tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "empty"
    first_models = _make_dummy_model(first)
    (second / "models").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(orientnet, "_opencv_support",
                        lambda: {"available": True, "version": "4.10.0"})
    mock_cv2 = MagicMock()
    mock_cv2.dnn.readNetFromONNX = lambda p: MagicMock()
    monkeypatch.setattr(orientnet, "cv2", mock_cv2)

    orientnet.reset()
    orientnet.configure(first_models)
    try:
        assert orientnet._net() is not None

        orientnet.configure(second / 'models')
        assert orientnet._net() is None
        assert getattr(orientnet._local, "net", None) is None
    finally:
        orientnet.reset()

