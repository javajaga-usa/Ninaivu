"""Where the image model runs: a graphics processor when there is one that
works, the processor otherwise, and the processor whenever the household says.

On an Apple M6 the image model gave identical tags and vectors on the Mac's
graphics processor (Metal, "mps") and ran 2.4 times as fast — and Ninaivu had
been asking only whether there was an NVIDIA card, so every Mac ran it on the
processor.
"""
from types import SimpleNamespace

from ninaivu import ai


class Probe:
    def __matmul__(self, other):
        return self

    def sum(self):
        return 1.0


def torch_with(*, cuda=False, mps=False, broken=()):
    def ones(*shape, device):
        if device in broken:
            raise RuntimeError(f"no kernels for this {device}")
        return Probe()
    return SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: cuda),
        backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: mps)),
        ones=ones)


def test_a_mac_uses_its_graphics_processor_when_the_caller_has_been_checked_on_it():
    assert ai.usable_device(torch_with(mps=True), apple=True) == "mps"


def test_callers_that_have_not_been_checked_on_it_keep_to_the_processor():
    """Generative editing runs in half precision, which is another question."""
    assert ai.usable_device(torch_with(mps=True)) == "cpu"


def test_an_nvidia_card_still_comes_first():
    assert ai.usable_device(torch_with(cuda=True, mps=True), apple=True) == "cuda"


def test_one_that_fails_its_first_multiply_is_passed_over():
    assert ai.usable_device(torch_with(cuda=True, broken={"cuda"})) == "cpu"
    assert ai.usable_device(torch_with(mps=True, broken={"mps"}), apple=True) == "cpu"


def test_switched_off_everything_runs_on_the_processor():
    assert ai.usable_device(torch_with(cuda=True, mps=True), gpu=False, apple=True) == "cpu"


def test_the_setting_reaches_the_model(monkeypatch):
    seen = {}

    class Engine:
        model_id, device = "test", "cpu"

        def __init__(self, *args, gpu=True):
            seen["gpu"] = gpu

    monkeypatch.setattr(ai, "ClipEngine", Engine)
    monkeypatch.setattr(ai, "clip_choice", lambda cfg: ("m", "p", None, None, None))
    cfg = SimpleNamespace(ai_enabled=True, ai_engine="clip", ai_gpu=False)
    ai.build_engine(cfg, force=True)
    assert seen["gpu"] is False
    ai.build_engine(SimpleNamespace(ai_enabled=False, ai_engine="off"), force=True)


def test_photographs_are_opened_in_parallel_and_keep_their_order(tmp_path):
    """Opening them one at a time was slower than the model on a GPU."""
    from PIL import Image

    paths = []
    for index in range(12):
        path = tmp_path / f"{index}.png"
        Image.new("RGB", (8, 8), (index * 20, 0, 0)).save(path)
        paths.append(path)
    paths.insert(5, tmp_path / "missing.png")
    engine = ai.ClipEngine.__new__(ai.ClipEngine)
    engine.preprocess = lambda image: image.getpixel((0, 0))[0]
    loaded = list(ai._decode_pool().map(engine._load, paths))
    assert loaded[5] is None
    assert [item[1] for item in loaded if item] == [i * 20 for i in range(12)]


def test_the_setting_defaults_on_and_can_be_turned_off_from_the_environment(
        tmp_path, monkeypatch):
    from ninaivu.server.config import Config
    assert Config().ai_gpu is True
    # This machine's own settings are none of the test's business.
    monkeypatch.setenv("NINAIVU_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("NINAIVU_AI_GPU", "0")
    assert Config.load().ai_gpu is False
