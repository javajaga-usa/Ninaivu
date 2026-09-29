"""The image model loads from this computer without asking huggingface.co.

Given a tag, open_clip asks huggingface.co whether the weights have changed on
every load, downloaded or not: a request each time Ninaivu started, and a wait
at a start without internet. Ninaivu now hands it the downloaded file, with the
tag's image preparation, which loads the same model to the last bit.
"""
import os
import socket

import pytest

from ninaivu import ai

TAG = ("ViT-B-32", "laion2b_s34b_b79k")
REPO = "laion/CLIP-ViT-B-32-laion2B-s34B-b79K"


@pytest.fixture()
def cache(monkeypatch, tmp_path):
    """A stand-in for the Hugging Face cache: repo/filename -> a file, if there."""
    present: dict[tuple[str, str], str] = {}

    def lookup(repo, filename, *args, **kwargs):
        return present.get((repo, filename))

    # Part of search by description, which the core install leaves out.
    huggingface_hub = pytest.importorskip("huggingface_hub")
    monkeypatch.setattr(huggingface_hub, "try_to_load_from_cache", lookup)

    def put(repo, filename):
        path = tmp_path / filename
        path.write_bytes(b"weights")
        present[(repo, filename)] = str(path)
        return str(path)
    return put


def test_downloaded_weights_are_used_with_the_tags_preparation(cache):
    path = cache(REPO, "open_clip_model.safetensors")
    found, preparation = ai.cached_weights(*TAG)
    assert found == path
    assert preparation == {
        "image_mean": (0.48145466, 0.4578275, 0.40821073),
        "image_std": (0.26862954, 0.26130258, 0.27577711),
        "image_interpolation": "bicubic", "image_resize_mode": "shortest"}


def test_the_safetensors_copy_is_preferred_as_open_clip_prefers_it(cache):
    cache(REPO, "open_clip_pytorch_model.bin")
    assert ai.cached_weights(*TAG)[0].endswith(".bin")
    cache(REPO, "open_clip_model.safetensors")
    assert ai.cached_weights(*TAG)[0].endswith(".safetensors")


def test_weights_not_downloaded_yet_are_loaded_as_a_tag(cache):
    assert ai.cached_weights(*TAG) is None


def test_a_name_that_is_not_a_tag_is_left_alone(cache):
    assert ai.cached_weights("ViT-B-32", "no-such-tag") is None


def test_a_tag_that_says_more_than_how_to_prepare_images_is_loaded_as_a_tag(cache, monkeypatch):
    import open_clip
    cache(REPO, "open_clip_model.safetensors")
    real = open_clip.get_pretrained_cfg
    monkeypatch.setattr(open_clip, "get_pretrained_cfg",
                        lambda *a: {**real(*a), "fill_color": 128})
    assert ai.cached_weights(*TAG) is None


@pytest.mark.real_models
def test_the_model_starts_with_the_network_unreachable(monkeypatch):
    if ai.cached_weights(*TAG) is None:
        pytest.skip("ViT-B-32 has not been downloaded on this machine")

    def refuse(*args, **kwargs):
        raise OSError("no network in this test")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    engine = ai.ClipEngine(*TAG, device="cpu")
    assert engine.model_id == "ViT-B-32/laion2b_s34b_b79k", "the id names the tag, not a path"
    assert os.sep not in engine.model_id.split("/", 1)[1]
