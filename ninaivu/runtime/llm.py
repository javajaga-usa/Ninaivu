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

Looking is the same model with its vision part (``vision-part``, about
850 MB, from the same repository) beside it: the capability ``look`` hands it
one photograph, as a private file the server wrote, with the chat about it::

    host.call("look", path="/…/talk/ab12.jpg", messages=[...], max_tokens=300)
    → {"text": "...", "model": "text-model", "tokens": 41, "took": 6.2}

Both capabilities share one loaded model in the host (:func:`_writer`), so
switching between writing and looking costs nothing but the vision part.

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
LOOK_VERSION = 1

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

#: Each writing model's vision part, by catalogue id.
VISION_PARTS: dict[str, str] = {"text-model": "vision-part"}
#: The largest picture handed to ``look``, in bytes. The server sends a
#: JPEG of at most a thousand pixels a side, far under this.
MAX_PICTURE_BYTES = 20 * 1024 * 1024


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


def vision_ready() -> str | None:
    """Why no photograph can be looked at here, or None when one can."""
    from ..media import model_catalog                          # noqa: PLC0415
    from ..words import said                                   # noqa: PLC0415
    if why := ready():
        return why
    part = VISION_PARTS.get(chosen_model() or "")
    if part is None or not model_catalog.installed(part):
        return said("the vision part of the writing model is not downloaded (AI models page)")
    return None


def vision_path(model_id: str | None = None) -> Path:
    from ..media import model_catalog                          # noqa: PLC0415
    part = VISION_PARTS[model_id or chosen_model() or MODELS[0]]
    return model_catalog.file_path(model_catalog.MODELS[part]["files"][0])


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
        #: The vision part, once ``look`` has been asked for (:meth:`open_eyes`).
        self.eyes: Any = None

    def open_eyes(self, projector: Path) -> None:
        """Load the vision part beside the model, once."""
        if self.eyes is not None:
            return
        from llama_cpp.llama_chat_format import MTMDChatHandler   # noqa: PLC0415
        # llama.cpp's own multimodal reader, which writes the prompt with
        # the model's own chat template (Gemma 3's, from the GGUF) and puts
        # the picture where the message has it.
        self.eyes = MTMDChatHandler(clip_model_path=str(projector), verbose=False)

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


    def look(self, picture: bytes, messages: Sequence[dict[str, str]], *, max_tokens: int,
             temperature: float) -> dict[str, Any]:
        """The model's answer to *messages*, the first of which from the
        person comes with *picture* (a JPEG) in front of its words."""
        import base64                                          # noqa: PLC0415
        if self.eyes is None:
            raise Unavailable("the vision part is not loaded")
        started = time.monotonic()
        shown = f"data:image/jpeg;base64,{base64.b64encode(picture).decode('ascii')}"
        chat: list[dict[str, Any]] = []
        given = False
        for message in messages:
            if message["role"] == "user" and not given:
                given = True
                chat.append({"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": shown}},
                    {"type": "text", "text": message["content"]}]})
            else:
                chat.append(dict(message))
        if not given:
            raise ValueError("a photograph needs a question from the person")
        # The reader is lent to the model for this one call: writing goes on
        # through the plain template, without the picture's cost.
        self.llama.chat_handler = self.eyes
        try:
            answer = self.llama.create_chat_completion(messages=chat, max_tokens=max_tokens,
                                                       temperature=temperature)
        finally:
            self.llama.chat_handler = None
            # What the model read for the picture is not a prefix the next
            # call can reuse: it starts from the beginning.
            self.llama.reset()
        choice = (answer.get("choices") or [{}])[0]
        text = ((choice.get("message") or {}).get("content") or "").strip()
        usage = answer.get("usage") or {}
        return {"text": text, "model": self.model_id,
                "tokens": int(usage.get("completion_tokens") or 0),
                "took": round(time.monotonic() - started, 3)}


#: The model loaded in this host process, shared by ``generate`` and
#: ``look``, with what it was loaded from.
_kept: tuple[tuple[str, int], Writer] | None = None


def _writer(setup: dict[str, Any]) -> Writer:
    """The writing model, loaded once in this process for both capabilities."""
    global _kept
    if why := ready():
        raise Unavailable(why)
    model_id = str(setup.get("text_model") or chosen_model() or MODELS[0])
    path = model_path(model_id)
    if not path.is_file():
        raise Unavailable(f"the writing model {model_id} is not on this computer")
    threads = int(setup.get("threads") or 4)
    key = (str(path), threads)
    if _kept is not None and _kept[0] == key:
        return _kept[1]
    writer = Writer(path, model_id=model_id, threads=threads)
    _kept = (key, writer)
    return writer


def _load(setup: dict[str, Any]) -> Writer:
    return _writer(setup)


def _load_eyes(setup: dict[str, Any]) -> Writer:
    if why := vision_ready():
        raise Unavailable(why)
    writer = _writer(setup)
    projector = vision_path(writer.model_id)
    if not projector.is_file():
        raise Unavailable("the vision part of the writing model is not on this computer")
    writer.open_eyes(projector)
    return writer


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


def look(model: Writer, *, path: str, messages: Any, max_tokens: int = 300,
         temperature: float = 0.2) -> dict[str, Any]:
    """One call: the model's answer to *messages* about the picture at *path*."""
    cleaned = clean_messages(messages)
    picture = Path(path)
    try:
        size = picture.stat().st_size
    except OSError:
        raise ValueError("the photograph to look at is not there") from None
    if not 0 < size <= MAX_PICTURE_BYTES:
        raise ValueError("the photograph to look at is empty or too large")
    tokens = max(16, min(int(max_tokens), MAX_TOKENS))
    heat = max(0.0, min(float(temperature), 1.5))
    return model.look(picture.read_bytes(), cleaned, max_tokens=tokens, temperature=heat)


LOOK = Capability("look", LOOK_VERSION, _load_eyes, look)
