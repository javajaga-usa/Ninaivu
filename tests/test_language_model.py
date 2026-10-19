"""The on-device writing model (ninaivu/runtime/llm.py), without the model.

llama.cpp and a 7 GB model are not in the test environment, so the runner is
a stand-in module that records what it was asked; what is checked is the
shape both callers rely on (the films' chapters and Ask Ninaivu), the pin of
the model, and that a missing runner or model is said in words."""
from __future__ import annotations

import sys
import types

import pytest

from ninaivu.media import model_catalog
from ninaivu.runtime import CAPABILITIES, Unavailable, host, llm


class FakeLlama:
    made: list[dict] = []
    asked: list[dict] = []

    def __init__(self, **kwargs):
        FakeLlama.made.append(kwargs)

    def create_chat_completion(self, **kwargs):
        FakeLlama.asked.append(kwargs)
        text = '{"title": "Madurai"}' if kwargs.get("response_format") else "வணக்கம்"
        return {"choices": [{"message": {"content": text}}],
                "usage": {"completion_tokens": 7}}


@pytest.fixture
def runner(monkeypatch, tmp_path):
    module = types.ModuleType("llama_cpp")
    module.Llama = FakeLlama
    module.__spec__ = types.SimpleNamespace(name="llama_cpp")
    monkeypatch.setitem(sys.modules, "llama_cpp", module)
    monkeypatch.setattr(llm, "missing_package", lambda: None)
    FakeLlama.made.clear()
    FakeLlama.asked.clear()
    model_catalog.configure(tmp_path)
    entry = model_catalog.MODELS["text-model"]["files"][0]
    # A small stand-in size: a file truncated to the real 7.1 GB is sparse on
    # Linux and macOS but takes its whole size on Windows, which filled the
    # disk of the Windows test machines.
    monkeypatch.setitem(entry, "bytes", 4096)
    path = model_catalog.file_path(entry)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\0" * entry["bytes"])
    yield path
    model_catalog.configure(None)


def test_the_capability_is_called_generate():
    assert CAPABILITIES["generate"] == "ninaivu.runtime.llm:GENERATE"
    assert host.resolve(CAPABILITIES["generate"]).name == "generate"


def test_the_model_is_pinned_and_not_gated():
    model = model_catalog.MODELS["text-model"]
    (entry,) = model["files"]
    assert entry["repo"] == "ggml-org/gemma-3-12b-it-qat-GGUF"
    assert len(entry["revision"]) == 40 and len(entry["sha256"]) == 64
    assert entry["bytes"] == 7131017792
    assert model["requires"] == ["llama_cpp"]


def test_nothing_downloaded_is_said(tmp_path, monkeypatch):
    model_catalog.configure(tmp_path)
    monkeypatch.setattr(llm, "missing_package", lambda: None)
    try:
        assert llm.chosen_model() is None
        assert "not downloaded" in llm.ready()
        with pytest.raises(Unavailable):
            llm._load({})
    finally:
        model_catalog.configure(None)


def test_a_missing_runner_is_said(monkeypatch):
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    assert "Writing in Tamil and English" in llm.missing_package()


def test_an_answer_has_the_shape_callers_rely_on(runner):
    assert llm.chosen_model() == "text-model"
    model = llm._load({"threads": 6})
    assert FakeLlama.made[0]["model_path"] == str(runner)
    assert FakeLlama.made[0]["n_gpu_layers"] == -1 and FakeLlama.made[0]["n_threads"] == 6
    answer = llm.generate(model, messages=[{"role": "system", "content": "Be kind."},
                                           {"role": "user", "content": "வணக்கம் சொல்"}],
                          max_tokens=99999, temperature=9)
    assert answer["text"] == "வணக்கம்" and answer["model"] == "text-model"
    assert answer["tokens"] == 7 and answer["took"] >= 0
    asked = FakeLlama.asked[-1]
    assert asked["max_tokens"] == llm.MAX_TOKENS and asked["temperature"] == 1.5
    assert "response_format" not in asked


def test_a_json_answer_is_held_to_its_shape(runner):
    model = llm._load({})
    schema = {"type": "object", "properties": {"title": {"type": "string"}}}
    answer = llm.generate(model, messages=[{"role": "user", "content": "A title?"}],
                          json_schema=schema)
    assert FakeLlama.asked[-1]["response_format"] == {"type": "json_object", "schema": schema}
    assert llm.parse_json(answer["text"]) == {"title": "Madurai"}


@pytest.mark.parametrize("messages", [[], "hello", [{"role": "robot", "content": "x"}],
                                      [{"role": "user", "content": 3}]])
def test_messages_must_be_a_chat(runner, messages):
    with pytest.raises(ValueError):
        llm.generate(llm._load({}), messages=messages)


def test_a_fenced_answer_still_parses():
    assert llm.parse_json('```json\n{"a": [1, 2]}\n```') == {"a": [1, 2]}
    assert llm.parse_json('Here it is: {"a": 1}') == {"a": 1}
