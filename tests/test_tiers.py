"""Basic and Full: which kind of computer this is, and what that decides."""

import pytest

from ninaivu.server import tiers
from ninaivu.server.config import Config


@pytest.fixture(autouse=True)
def _tier_not_set_outright(request, monkeypatch):
    """CI runs the suite with NINAIVU_HARDWARE_TIER set (tests.yml, the two
    tier jobs); these tests are about what happens when it is not."""
    if request.node.get_closest_marker("tier_full") is None:
        monkeypatch.delenv("NINAIVU_HARDWARE_TIER", raising=False)


@pytest.mark.parametrize("memory_gb,graphics,apple,expected", [
    (2, None, False, "basic"),           # the small always-on box
    (16, None, False, "basic"),          # plenty of memory, no GPU: still no image model
    (16, "cuda", False, "full"),
    (4, "cuda", False, "basic"),         # a GPU in a box too small to feed it
    (8, "mps", True, "full"),            # Apple silicon
    (None, "cuda", False, "full"),       # memory unknown, a GPU present
])
def test_the_tier_follows_memory_and_graphics(memory_gb, graphics, apple, expected):
    memory = None if memory_gb is None else memory_gb * 1024 ** 3
    assert tiers.detect(memory, graphics, apple) == expected


@pytest.mark.parametrize("memory_gb,expected", [(16, "full"), (8, "full"), (4, "basic"), (2, "basic")])
def test_without_a_gpu_an_installed_model_with_enough_memory_is_full(memory_gb, expected):
    """A 16 GB PC — or the Docker image — that installed the image model ran it
    on the processor before the tiers existed; Basic would take it away."""
    assert tiers.detect(memory_gb * 1024 ** 3, None, False, model_installed=True) == expected


def test_the_household_or_the_environment_can_say_outright(monkeypatch):
    cfg = Config()
    assert tiers.chosen(cfg) is None
    cfg.hardware_tier = "basic"
    assert tiers.chosen(cfg) == "basic"
    monkeypatch.setenv("NINAIVU_HARDWARE_TIER", "full")
    assert tiers.chosen(cfg) == "full", "the environment wins, as it does for every setting"
    monkeypatch.setenv("NINAIVU_HARDWARE_TIER", "huge")
    assert tiers.chosen(cfg) == "basic", "nonsense is ignored"


def test_auto_means_what_the_tier_says_and_nothing_else_is_touched():
    cfg = Config()
    assert tiers.apply(cfg, "basic") == ["ai_engine", "video_keyframes", "clip_batch_size"]
    assert cfg.ai_engine == "auto", "the setting itself is left alone, so nothing is saved"
    assert cfg.ai_engine_resolved == "light"
    assert cfg.video_keyframes == 1 and cfg.clip_batch_size == 4

    chosen = Config()
    chosen.ai_engine = "clip"
    chosen.video_keyframes = 3
    assert tiers.apply(chosen, "basic") == ["clip_batch_size"]
    assert chosen.ai_engine == "clip" and chosen.video_keyframes == 3, "a household's choice stands"


def test_a_full_machine_keeps_the_shipped_defaults():
    cfg = Config()
    assert tiers.apply(cfg, "full") == ["ai_engine"]
    assert cfg.ai_engine_resolved == "clip"


def test_the_engine_reads_the_resolved_choice(monkeypatch):
    from ninaivu import ai
    cfg = Config()
    cfg.ai_enabled = True
    tiers.apply(cfg, "basic")
    monkeypatch.setattr(ai, "_engine", None)
    tried = []
    monkeypatch.setattr(ai, "ClipEngine", lambda *a, **k: tried.append(1) or (_ for _ in ()).throw(RuntimeError()))
    engine = ai.build_engine(cfg, force=True)
    assert tried == [], "a Basic machine never tries to load the image model"
    assert type(engine).__name__ == "LightEngine"
    monkeypatch.setattr(ai, "_engine", None)


def test_the_performance_page_says_which_and_why(scanned, monkeypatch):
    from conftest import ADMIN, login
    from ninaivu import build_services, create_admin_app
    from ninaivu.server import auth, capacity

    cfg, conn, _ = scanned
    cfg.watch = False
    monkeypatch.setattr(capacity, "machine", lambda: {
        "system": "x", "processor": "p", "logical_cores": 2, "physical_cores": 2, "core_kinds": [],
        "memory_bytes": 2 * 1024 ** 3, "apple_silicon": False})
    monkeypatch.setattr(capacity, "graphics", lambda engine: {"available": None, "name": None,
                                                              "ai_device": None, "in_use": None})
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    assert services.tier["tier"] == "basic"
    client = login(create_admin_app(services).test_client(), *ADMIN)
    tier = client.get("/api/admin/performance").get_json()["tier"]
    assert tier["tier"] == "basic" and tier["why"].startswith("no graphics processor")
    assert tier["missing"], "the page says what a Full machine would add"


@pytest.mark.tier_full
def test_on_a_full_machine_the_image_model_loads():
    """Run by the Full job in CI (an Apple-silicon runner with the model
    stack installed); skipped everywhere torch is not there."""
    torch = pytest.importorskip("torch")
    from ninaivu import ai
    device = ai.usable_device(torch, gpu=True, apple=True)
    assert device in ("cuda", "mps", "cpu")
