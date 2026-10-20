"""Writing: one language model, on this computer, for everything that writes.

The chapters and titles of a memory film (media/storyboard.py), Ask Ninaivu
and the interview mode all need the same thing: a model that reads a little
of what the library knows and writes a few sentences of Tamil or English.
They share one model and one capability, ``generate``, in the model host
(runtime/host.py), so a household downloads one model and holds one in
memory, and a better model later improves all of them at once.

The model is Gemma 3 12B, instruction-tuned and trained for 4-bit use, as one
GGUF file (``text-model`` on the AI models page, media/model_catalog.py), run
by llama.cpp (``llama-cpp-python``, under Extras). On a Mac every layer goes
to the graphics processor through Metal; elsewhere it runs on the processor.

A call is a chat::

    host.call("generate", messages=[{"role": "system", "content": "..."},
                                    {"role": "user", "content": "..."}],
              max_tokens=600, temperature=0.4, json_schema=None, timeout=240)
    → {"text": "...", "model": "text-model", "tokens": 212, "took": 9.8}

With ``json_schema`` the answer is held to that JSON shape by llama.cpp's
grammar while it is written, so it parses; the caller still checks what is
in it (:func:`parse_json`), since a shape is not a meaning. A model that is
not downloaded, or a runner that is not installed, is :class:`Unavailable`,
and each caller has a way of its own to do without (the films' chapters fall
back to dates and places).

Nothing here talks to the network: the model is read from the models folder.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Sequence

from .host import Capability, Unavailable

log = logging.getLogger(__name__)

#: The capability's own version, apart from the model.
GENERATE_VERSION = 1

#: The writing models, best first. One for now.
MODELS: tuple[str, ...] = ("text-model",)

#: Tokens the model reads and writes at most in one call: the prompt and its
#: answer together. 16,384 holds a long film's moments with their
#: descriptions and stories, Tamil included (storyboard.PROMPT_CHARS keeps
#: them inside it); the memory it costs grows with it: for Gemma 3 12B
#: between about half a gigabyte and three more than 8,192 (depending on
#: whether llama.cpp keeps the sliding-window layers short), which a 48 GB
#: Mac has.
CONTEXT = 16384
#: The longest answer asked for.
MAX_TOKENS = 2048
#: The roles a message may have.
ROLES = ("system", "user", "assistant")


def chosen_model() -> str | None:
    """The catalogue id of the writing model in use: the best downloaded, or None."""
    from ..media import model_catalog                          # noqa: PLC0415
    return next((m for m in MODELS if model_catalog.installed(m)), None)


def model_path(model_id: str | None = None) -> Path:
    from ..media import model_catalog                          # noqa: PLC0415
    entry = model_catalog.MODELS[model_id or chosen_model() or MODELS[0]]["files"][0]
    return model_catalog.file_path(entry)


def missing_package() -> str | None:
    """Why llama.cpp cannot be used here, or None."""
    import importlib.util                                      # noqa: PLC0415
    from ..words import said                                   # noqa: PLC0415
    if importlib.util.find_spec("llama_cpp") is None:
        return said("the language runner is not installed (Writing in Tamil and English, under Extras on the Home & extensions page)")
    return None


def ready() -> str | None:
    """Why nothing can be written here, or None when it can."""
    from ..words import said                                   # noqa: PLC0415
    if why := missing_package():
        return why
    if chosen_model() is None:
        return said("the writing model is not downloaded (AI models page)")
    return None


def clean_messages(messages: Any) -> list[dict[str, str]]:
    """*messages* as plain role and content pairs, or ValueError."""
    if not isinstance(messages, (list, tuple)) or not messages:
        raise ValueError("messages must be a non-empty list")
    out = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in ROLES \
                or not isinstance(message.get("content"), str):
            raise ValueError("each message needs a role and its text")
        out.append({"role": message["role"], "content": message["content"]})
    return out


def parse_json(text: str) -> Any:
    """The JSON value in a model's answer, tolerating a fence around it."""
    body = (text or "").strip()
    if body.startswith("```"):
        body = body.strip("`")
        body = body[body.find("\n") + 1:] if "\n" in body else body
    start = min((i for i in (body.find("{"), body.find("[")) if i >= 0), default=-1)
    if start > 0:
        body = body[start:]
    return json.loads(body)


class Writer:
    """One loaded model."""

    def __init__(self, path: Path, *, model_id: str, threads: int = 4,
                 context: int = CONTEXT) -> None:
        import llama_cpp                                       # noqa: PLC0415
        self.model_id = model_id
        # Every layer on the graphics processor where llama.cpp was built for
        # one (Metal on a Mac); a build for the processor ignores it.
        self.llama = llama_cpp.Llama(model_path=str(path), n_ctx=context, n_gpu_layers=-1,
                                     n_threads=max(1, threads), verbose=False)

    def generate(self, messages: Sequence[dict[str, str]], *, max_tokens: int,
                 temperature: float, json_schema: dict[str, Any] | None) -> dict[str, Any]:
        started = time.monotonic()
        options: dict[str, Any] = {"messages": list(messages), "max_tokens": max_tokens,
                                   "temperature": temperature}
        if json_schema:
            options["response_format"] = {"type": "json_object", "schema": json_schema}
        answer = self.llama.create_chat_completion(**options)
        choice = (answer.get("choices") or [{}])[0]
        text = ((choice.get("message") or {}).get("content") or "").strip()
        usage = answer.get("usage") or {}
        return {"text": text, "model": self.model_id,
                "tokens": int(usage.get("completion_tokens") or 0),
                "took": round(time.monotonic() - started, 3)}


def _load(setup: dict[str, Any]) -> Writer:
    if why := ready():
        raise Unavailable(why)
    model_id = str(setup.get("text_model") or chosen_model() or MODELS[0])
    path = model_path(model_id)
    if not path.is_file():
        raise Unavailable(f"the writing model {model_id} is not on this computer")
    return Writer(path, model_id=model_id, threads=int(setup.get("threads") or 4))


def generate(model: Writer, *, messages: Any, max_tokens: int = 600, temperature: float = 0.4,
             json_schema: dict[str, Any] | None = None) -> dict[str, Any]:
    """One call: the model's answer to *messages*."""
    cleaned = clean_messages(messages)
    tokens = max(16, min(int(max_tokens), MAX_TOKENS))
    heat = max(0.0, min(float(temperature), 1.5))
    if json_schema is not None and not isinstance(json_schema, dict):
        raise ValueError("json_schema must be an object")
    return model.generate(cleaned, max_tokens=tokens, temperature=heat, json_schema=json_schema)


GENERATE = Capability("generate", GENERATE_VERSION, _load, generate)
