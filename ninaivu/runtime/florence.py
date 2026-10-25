"""A description of every photograph, written on this computer: Florence-2.

Microsoft's Florence-2 (base, fine-tuned; MIT licence) in the ONNX export the
transformers.js project publishes, run by ONNX Runtime inside the model host.
Given a photograph it writes a sentence or two of what is in it ("A woman in
a red sari lighting a lamp in front of a temple"), which is kept with the
photograph (storage/descriptions.py) and searched with its other words.

The model is four pieces, each a file on the AI models page's ``captions``
entry: the image encoder (full precision, so the Neural Engine, the graphics
processor or a graphics card can take it), and the word table, the text
encoder and the decoder (8-bit, on the processor, where 8-bit is fastest;
see runtime/devices.py). About 550 MB on disk.

How one photograph is described, as Florence2Processor and ``generate`` do
it in the transformers library:

1. The 640-pixel preview the gallery already made is squashed to 768 by 768,
   scaled to [0, 1] and normalised with ImageNet's mean and spread; the
   image encoder makes 577 vectors of it.
2. The task, "<DETAILED_CAPTION>", is the sentence Florence-2 was trained to
   answer it with: "Describe in detail what is shown in the image." Its
   tokens, through the word table, follow the image's vectors into the text
   encoder.
3. The decoder writes the description a token at a time, keeping what it has
   worked out so far (its key-value cache) between tokens: it starts at
   end-of-text, is made to say beginning-of-text first, and stops at
   end-of-text or :data:`MAX_TOKENS`. The most likely token each time
   (greedy), never repeating three tokens in a row it has already written,
   as the model's own generation settings ask.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any, Sequence

from .host import Capability, Unavailable

log = logging.getLogger(__name__)

#: The AI models page's entries for this model (media/model_catalog.py),
#: best first: Florence-2 large at full precision, for a computer with room
#: for it (24 GB of memory or more), and base, for any other.
MODELS: tuple[str, ...] = ("captions-large", "captions")
#: The base model's entry, the one every computer can run.
MODEL = "captions"
_RANK = {"captions-large": 2, "captions": 1}
#: Memory, in GB, from which the large model is the one to download.
LARGE_FROM_GB = 24
#: The capability's own version: how photographs are described. Raised when a
#: change here should describe every photograph again.
DESCRIBE_VERSION = 1

SIZE = 768
MEAN = (0.485, 0.456, 0.406)
STD = (0.229, 0.224, 0.225)
#: The task "<DETAILED_CAPTION>" as Florence2Processor writes it out.
PROMPT = "Describe in detail what is shown in the image."
BOS, PAD, EOS = 0, 1, 2
DECODER_START = EOS
#: The longest description, in tokens. A detailed caption is 20 to 50.
MAX_TOKENS = 80
NO_REPEAT = 3
#: The most characters kept of one description.
MAX_CHARS = 600

#: The files each model is, by the part each one is. Base keeps its text
#: half at 8 bits, where a processor is fastest; large is at full precision
#: throughout, which a computer with the memory for it can afford and which
#: keeps every word the model would write.
PARTS = {
    "captions": {
        "vision": "onnx/vision_encoder.onnx",
        "embed": "onnx/embed_tokens_int8.onnx",
        "encoder": "onnx/encoder_model_int8.onnx",
        "decoder": "onnx/decoder_model_merged_int8.onnx",
        "tokenizer": "tokenizer.json",
    },
    "captions-large": {
        "vision": "onnx/vision_encoder.onnx",
        "embed": "onnx/embed_tokens.onnx",
        "encoder": "onnx/encoder_model.onnx",
        "decoder": "onnx/decoder_model_merged.onnx",
        "tokenizer": "tokenizer.json",
    },
}
#: The decoder's attention heads, for when the export leaves them unnamed.
HEADS = {"captions": 12, "captions-large": 16}


def chosen_model() -> str | None:
    """The description model the engine uses: the best one downloaded."""
    from ..media import model_catalog                          # noqa: PLC0415
    return next((m for m in MODELS if model_catalog.installed(m)), None)


def suggested_model(memory_bytes: int | None = None) -> str:
    """The description model to download on this computer: large with
    :data:`LARGE_FROM_GB` of memory or more, base otherwise."""
    if memory_bytes is None:
        try:
            import psutil                                      # noqa: PLC0415
            memory_bytes = int(psutil.virtual_memory().total)
        except Exception:                                      # noqa: BLE001 - say base
            memory_bytes = 0
    return "captions-large" if memory_bytes >= LARGE_FROM_GB * 1024 ** 3 else MODEL


def version_of(model_id: str | None) -> int:
    """What a description by *model_id* is stamped with; 0 for no model. A
    better model downloaded later makes every photograph owed again."""
    if not model_id:
        return 0
    return 10 * DESCRIBE_VERSION + _RANK.get(model_id, 0)


def version() -> int:
    """What a description is stamped with now: owed again when this changes."""
    return version_of(chosen_model())


def ready() -> str | None:
    """Why photographs cannot be described here, or None when they can."""
    from ..words import said                                   # noqa: PLC0415
    from .siglip import missing_packages                       # noqa: PLC0415
    if why := missing_packages():
        return why
    if chosen_model() is None:
        return said("the description model is not downloaded (AI models page)")
    return None


def folder(model_id: str | None = None) -> Path:
    from ..media import model_catalog                          # noqa: PLC0415
    entry = model_catalog.MODELS[model_id or chosen_model() or MODEL]
    return model_catalog.models_root() / entry["files"][0]["folder"]


# ---------------------------------------------------------------------------
# Photograph and prompt
# ---------------------------------------------------------------------------

def pixels(image: Any) -> Any:
    """The image encoder's input for one photograph: 1 x 3 x 768 x 768."""
    import numpy as np                                         # noqa: PLC0415
    from PIL import Image                                      # noqa: PLC0415
    rgb = image if image.mode == "RGB" else image.convert("RGB")
    array = np.asarray(rgb.resize((SIZE, SIZE), Image.Resampling.BICUBIC), dtype=np.float32)
    array = (array / 255.0 - np.asarray(MEAN, np.float32)) / np.asarray(STD, np.float32)
    return np.ascontiguousarray(array.transpose(2, 0, 1)[None], dtype=np.float32)


def prompt_ids(tokenizer: Any) -> list[int]:
    """The task's tokens, between beginning- and end-of-text, as BART's
    tokenizer gives them to Florence-2."""
    return [BOS, *tokenizer.encode(PROMPT, add_special_tokens=False).ids, EOS]


def banned(tokens: Sequence[int], size: int = NO_REPEAT) -> set[int]:
    """Tokens that would repeat a run of *size* already written: the last
    ``size - 1`` tokens followed by any token that followed them before."""
    if len(tokens) < size:
        return set()
    tail = tuple(tokens[len(tokens) - size + 1:])
    found = set()
    for at in range(len(tokens) - size + 1):
        if tuple(tokens[at:at + size - 1]) == tail:
            found.add(int(tokens[at + size - 1]))
    return found


_SPACES = re.compile(r"\s+")


def tidy(text: str) -> str:
    """The description as it is kept: special tokens gone, one line."""
    for token in ("<s>", "</s>", "<pad>", "<unk>"):
        text = text.replace(token, "")
    return _SPACES.sub(" ", text).strip()[:MAX_CHARS]


# ---------------------------------------------------------------------------
# The model, inside the host
# ---------------------------------------------------------------------------

def _dtype(arg: Any) -> Any:
    import numpy as np                                         # noqa: PLC0415
    kind = str(getattr(arg, "type", ""))
    return np.float16 if "float16" in kind else np.int32 if "int32" in kind \
        else np.int64 if "int64" in kind else np.bool_ if "bool" in kind else np.float32


class Florence:
    """Florence-2's four pieces and its tokenizer, loaded together."""

    def __init__(self, root: Path, threads: int = 2, model_id: str = MODEL,
                 device: str | None = None, cache: str | None = None) -> None:
        from tokenizers import Tokenizer                        # noqa: PLC0415
        from .devices import CPU, coreml_cache, image_providers, resolve_image_device  # noqa: PLC0415
        from .siglip import _session                            # noqa: PLC0415
        root = Path(root)
        parts = PARTS[model_id]
        for name in parts.values():
            if not (root / name).is_file():
                raise Unavailable("the description model is not downloaded (AI models page)")
        started = time.monotonic()
        self.model_id = model_id
        self.heads = HEADS[model_id]
        self.tokenizer = Tokenizer.from_file(str(root / parts["tokenizer"]))
        vision = root / parts["vision"]
        self.vision = _session(vision, threads,
                               image_providers(device, cache=coreml_cache(cache, vision)))
        self.embed = _session(root / parts["embed"], threads, [CPU])
        # The text encoder takes the same shape for every photograph, like the
        # image encoder, so at full precision (large) Core ML can take it too
        # when the image encoder is put there; an 8-bit one stays on the
        # processor (runtime/devices.py).
        encoder = root / parts["encoder"]
        on_coreml = resolve_image_device(device) in ("coreml", "coreml-gpu")
        self.encoder = _session(encoder, threads,
                                image_providers(device, cache=coreml_cache(cache, encoder))
                                if on_coreml and "int8" not in encoder.name else [CPU])
        self.decoder = _session(root / parts["decoder"], threads, [CPU])
        self.prompt = prompt_ids(self.tokenizer)
        self._words: Any = None                  # the task's vectors, worked out once
        #: Seconds spent on each part since loading, for tools/try_captions.py.
        self.stages = {"image": 0.0, "text": 0.0, "writing": 0.0}
        self._decoder_inputs = {a.name: a for a in self.decoder.get_inputs()}
        self._decoder_outputs = [o.name for o in self.decoder.get_outputs()]
        log.warning("description model %s loaded (images on %s, text on %s) in %.1f s", model_id,
                    self.vision.get_providers()[0], self.encoder.get_providers()[0],
                    time.monotonic() - started)

    # -- pieces -----------------------------------------------------------
    def _embed(self, rows: Sequence[Sequence[int]]) -> Any:
        """The word table's vectors for each row of tokens (rows of one length)."""
        import numpy as np                                     # noqa: PLC0415
        feed = self.embed.get_inputs()[0]
        ids = np.asarray([list(row) for row in rows], dtype=_dtype(feed))
        return self.embed.run(None, {feed.name: ids})[0]

    def _encode(self, image: Any) -> Any:
        """The text encoder's output for this photograph and the task.

        One photograph at a time: every photograph's input is the same
        shape, which is what Core ML is quickest with, and on the processor
        the image encoder and the text encoder are already busy with one.
        """
        import numpy as np                                     # noqa: PLC0415
        started = time.monotonic()
        feed = self.vision.get_inputs()[0]
        features = self.vision.run(None, {feed.name: pixels(image).astype(_dtype(feed))})[0]
        self.stages["image"] += time.monotonic() - started
        started = time.monotonic()
        if self._words is None:
            self._words = self._embed([self.prompt])
        words = self._words
        joined = np.concatenate([features.astype(words.dtype), words], axis=1)
        given = {a.name: a for a in self.encoder.get_inputs()}
        feeds: dict[str, Any] = {}
        for name, arg in given.items():
            if name == "attention_mask":
                feeds[name] = np.ones(joined.shape[:2], dtype=_dtype(arg))
            elif name == "inputs_embeds":
                feeds[name] = joined.astype(_dtype(arg))
        if "inputs_embeds" not in feeds:
            raise ValueError("the text encoder does not take vectors in")
        hidden = self.encoder.run(None, feeds)[0]
        self.stages["text"] += time.monotonic() - started
        return hidden

    def _empty_cache(self, rows: int = 1) -> dict[str, Any]:
        """The decoder's key-value cache before its first token: nothing,
        for each of *rows* photographs."""
        import numpy as np                                     # noqa: PLC0415
        cache = {}
        for name, arg in self._decoder_inputs.items():
            if not name.startswith("past_key_values."):
                continue
            shape = list(arg.shape)
            heads = shape[1] if len(shape) > 1 and isinstance(shape[1], int) else self.heads
            width = shape[3] if len(shape) > 3 and isinstance(shape[3], int) else 64
            cache[name] = np.zeros((rows, heads, 0, width), dtype=_dtype(arg))
        return cache

    def describe(self, image: Any) -> str:
        """A sentence or two about one photograph (an open PIL image)."""
        return self.describe_many([image])[0]

    def describe_many(self, images: Sequence[Any]) -> list[str]:
        """A sentence or two about each photograph, written together.

        The decoder writes a token for every photograph in one run rather
        than one run each: a run reads all of its weights whichever number of
        rows it is given, so several photographs cost little more than one.
        A photograph whose description has ended leaves the batch (its rows
        of the cache are dropped), so a long description does not keep the
        short ones' work going. Each photograph is written exactly as
        :meth:`describe` alone would write it: the same greedy choice, the
        same ban on repeats, the same stop.
        """
        import numpy as np                                     # noqa: PLC0415
        if not images:
            return []
        hidden = np.concatenate([self._encode(image) for image in images], axis=0)
        started = time.monotonic()
        inputs = self._decoder_inputs
        mask = np.ones(hidden.shape[:2], dtype=np.int64)
        cache = self._empty_cache(len(images))
        written: list[list[int]] = [[DECODER_START] for _ in images]
        alive = list(range(len(images)))         # which photograph each row is
        first = True
        while alive:
            feeds: dict[str, Any] = dict(cache)
            last = [[written[at][-1]] for at in alive]
            if "input_ids" in inputs:
                feeds["input_ids"] = np.asarray(last, dtype=_dtype(inputs["input_ids"]))
            else:
                feeds["inputs_embeds"] = self._embed(last).astype(_dtype(inputs["inputs_embeds"]))
            if "encoder_hidden_states" in inputs:
                feeds["encoder_hidden_states"] = hidden.astype(_dtype(inputs["encoder_hidden_states"]))
            if "encoder_attention_mask" in inputs:
                feeds["encoder_attention_mask"] = mask.astype(_dtype(inputs["encoder_attention_mask"]))
            if "use_cache_branch" in inputs:
                feeds["use_cache_branch"] = np.asarray([not first], dtype=np.bool_)
            outputs = dict(zip(self._decoder_outputs, self.decoder.run(None, feeds)))
            for name, value in outputs.items():
                if not name.startswith("present."):
                    continue
                past = "past_key_values." + name[len("present."):]
                # The encoder's half is worked out at the first token and
                # is the same for every one after it.
                if past in inputs and (first or ".encoder." not in past):
                    cache[past] = value
            first = False
            logits = np.asarray(outputs[self._decoder_outputs[0]])[:, -1].astype(np.float32)
            going = []
            for row, at in enumerate(alive):
                tokens = written[at]
                if len(tokens) == 1:
                    token = BOS                   # the model's forced first token
                else:
                    scores = logits[row]
                    for ban in banned(tokens[1:]):
                        scores[ban] = -np.inf
                    token = int(np.argmax(scores))
                tokens.append(token)
                if token != EOS and len(tokens) <= MAX_TOKENS:
                    going.append(row)
            if len(going) < len(alive):
                keep = np.asarray(going, dtype=np.int64)
                cache = {name: value[keep] for name, value in cache.items()}
                hidden, mask = hidden[keep], mask[keep]
                alive = [alive[row] for row in going]
        self.stages["writing"] += time.monotonic() - started
        return [tidy(self.tokenizer.decode(tokens, skip_special_tokens=True))
                for tokens in written]


def _load(setup: dict[str, Any]) -> Florence:
    if why := ready():
        raise Unavailable(why)
    # The best one downloaded, unless the caller names one (tools/try_captions.py).
    model_id = str(setup.get("describe_model") or chosen_model() or MODEL)
    if model_id not in PARTS:
        raise Unavailable(f"there is no description model called {model_id}")
    return Florence(folder(model_id), threads=int(setup.get("threads") or 2), model_id=model_id,
                    device=setup.get("image_device"), cache=setup.get("coreml_cache"))


def describe(model: Florence, *, paths: Sequence[str]) -> list[dict[str, Any]]:
    """One call: each photograph's description, or why it has none.

    *paths* are the gallery's previews, read here, and described together
    (:meth:`Florence.describe_many`). A picture that will not open is that
    picture's failure alone; so is one the model fails on, which is found by
    describing the batch again a photograph at a time.
    """
    from PIL import Image                                      # noqa: PLC0415
    out: list[dict[str, Any] | None] = [None] * len(paths)
    opened: list[tuple[int, Any]] = []
    started = time.monotonic()
    for at, path in enumerate(paths):
        try:
            with Image.open(path) as image:
                opened.append((at, image.convert("RGB")))
        except Exception as exc:                               # noqa: BLE001 - this picture only
            log.warning("could not open %s to describe it", path, exc_info=True)
            out[at] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    before = dict(model.stages)
    try:
        texts: list[str | Exception] = list(model.describe_many([image for _, image in opened]))
    except Exception:                                          # noqa: BLE001 - find which one
        log.warning("a batch of %d photographs failed together; describing each alone",
                    len(opened), exc_info=True)
        texts = []
        for at, image in opened:
            try:
                texts.append(model.describe(image))
            except Exception as exc:                           # noqa: BLE001 - this picture only
                log.warning("could not describe %s", paths[at], exc_info=True)
                texts.append(exc)
    share = max(1, len(opened))
    took = round((time.monotonic() - started) / share, 3)
    stages = {name: round((seconds - before.get(name, 0.0)) / share, 3)
              for name, seconds in model.stages.items()}
    for (at, _), text in zip(opened, texts):
        if isinstance(text, Exception):
            out[at] = {"error": f"{type(text).__name__}: {text}"[:300]}
        else:
            out[at] = {"text": text, "model": model.model_id, "took": took, "stages": stages}
    # Every photograph has its row by now: opened and described, or not opened.
    return [row for row in out if row is not None]


DESCRIBE = Capability("describe", DESCRIBE_VERSION, _load, describe)
