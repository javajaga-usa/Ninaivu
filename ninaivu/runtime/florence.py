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

#: The AI models page's entry for this model (media/model_catalog.py).
MODEL = "captions"
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

#: The files the model is, by the part each one is.
PARTS = {
    "vision": "onnx/vision_encoder.onnx",
    "embed": "onnx/embed_tokens_int8.onnx",
    "encoder": "onnx/encoder_model_int8.onnx",
    "decoder": "onnx/decoder_model_merged_int8.onnx",
    "tokenizer": "tokenizer.json",
}


def version() -> int:
    """What a description is stamped with: owed again when this changes."""
    return DESCRIBE_VERSION


def ready() -> str | None:
    """Why photographs cannot be described here, or None when they can."""
    from ..media import model_catalog                          # noqa: PLC0415
    from ..words import said                                   # noqa: PLC0415
    from .siglip import missing_packages                       # noqa: PLC0415
    if why := missing_packages():
        return why
    if not model_catalog.installed(MODEL):
        return said("the description model is not downloaded (AI models page)")
    return None


def folder() -> Path:
    from ..media import model_catalog                          # noqa: PLC0415
    return model_catalog.models_root() / model_catalog.MODELS[MODEL]["files"][0]["folder"]


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

    def __init__(self, root: Path, threads: int = 2) -> None:
        from tokenizers import Tokenizer                        # noqa: PLC0415
        from .devices import CPU, onnx_providers                # noqa: PLC0415
        from .siglip import _session                            # noqa: PLC0415
        root = Path(root)
        for name in PARTS.values():
            if not (root / name).is_file():
                raise Unavailable("the description model is not downloaded (AI models page)")
        started = time.monotonic()
        self.tokenizer = Tokenizer.from_file(str(root / PARTS["tokenizer"]))
        self.vision = _session(root / PARTS["vision"], threads, onnx_providers())
        self.embed = _session(root / PARTS["embed"], threads, [CPU])
        self.encoder = _session(root / PARTS["encoder"], threads, [CPU])
        self.decoder = _session(root / PARTS["decoder"], threads, [CPU])
        self.prompt = prompt_ids(self.tokenizer)
        self._decoder_inputs = {a.name: a for a in self.decoder.get_inputs()}
        self._decoder_outputs = [o.name for o in self.decoder.get_outputs()]
        log.warning("description model loaded (images on %s) in %.1f s",
                    self.vision.get_providers()[0], time.monotonic() - started)

    # -- pieces -----------------------------------------------------------
    def _embed(self, ids: Sequence[int]) -> Any:
        import numpy as np                                     # noqa: PLC0415
        feed = self.embed.get_inputs()[0]
        return self.embed.run(None, {feed.name: np.asarray([list(ids)], dtype=_dtype(feed))})[0]

    def _encode(self, image: Any) -> tuple[Any, Any]:
        """The text encoder's output for this photograph and the task, and
        its attention mask."""
        import numpy as np                                     # noqa: PLC0415
        feed = self.vision.get_inputs()[0]
        features = self.vision.run(None, {feed.name: pixels(image).astype(_dtype(feed))})[0]
        words = self._embed(self.prompt)
        joined = np.concatenate([features.astype(words.dtype), words], axis=1)
        given = {a.name: a for a in self.encoder.get_inputs()}
        mask = np.ones(joined.shape[:2], dtype=np.int64)
        feeds: dict[str, Any] = {}
        for name, arg in given.items():
            if name == "attention_mask":
                feeds[name] = mask.astype(_dtype(arg))
            elif name == "inputs_embeds":
                feeds[name] = joined.astype(_dtype(arg))
        if "inputs_embeds" not in feeds:
            raise ValueError("the text encoder does not take vectors in")
        return self.encoder.run(None, feeds)[0], mask

    def _empty_cache(self) -> dict[str, Any]:
        """The decoder's key-value cache before its first token: nothing."""
        import numpy as np                                     # noqa: PLC0415
        cache = {}
        for name, arg in self._decoder_inputs.items():
            if not name.startswith("past_key_values."):
                continue
            shape = list(arg.shape)
            heads = shape[1] if len(shape) > 1 and isinstance(shape[1], int) else 12
            width = shape[3] if len(shape) > 3 and isinstance(shape[3], int) else 64
            cache[name] = np.zeros((1, heads, 0, width), dtype=_dtype(arg))
        return cache

    def describe(self, image: Any) -> str:
        """A sentence or two about one photograph (an open PIL image)."""
        import numpy as np                                     # noqa: PLC0415
        hidden, mask = self._encode(image)
        cache = self._empty_cache()
        inputs = self._decoder_inputs
        written: list[int] = [DECODER_START]
        first = True
        while len(written) <= MAX_TOKENS:
            feeds: dict[str, Any] = dict(cache)
            last = [written[-1]]
            if "input_ids" in inputs:
                feeds["input_ids"] = np.asarray([last], dtype=_dtype(inputs["input_ids"]))
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
            logits = np.asarray(outputs[self._decoder_outputs[0]])[0, -1].astype(np.float32)
            if len(written) == 1:
                token = BOS                       # the model's forced first token
            else:
                for ban in banned(written[1:]):
                    logits[ban] = -np.inf
                token = int(np.argmax(logits))
            written.append(token)
            if token == EOS:
                break
        return tidy(self.tokenizer.decode(written, skip_special_tokens=True))


def _load(setup: dict[str, Any]) -> Florence:
    if why := ready():
        raise Unavailable(why)
    return Florence(folder(), threads=int(setup.get("threads") or 2))


def describe(model: Florence, *, paths: Sequence[str]) -> list[dict[str, Any]]:
    """One call: each photograph's description, or why it has none.

    *paths* are the gallery's previews, read here. A picture that will not
    open, or that the model fails on, is that picture's failure alone.
    """
    from PIL import Image                                      # noqa: PLC0415
    out: list[dict[str, Any]] = []
    for path in paths:
        started = time.monotonic()
        try:
            with Image.open(path) as image:
                text = model.describe(image.convert("RGB"))
            out.append({"text": text, "took": round(time.monotonic() - started, 3)})
        except Exception as exc:                               # noqa: BLE001 - this picture only
            log.warning("could not describe %s", path, exc_info=True)
            out.append({"error": f"{type(exc).__name__}: {exc}"[:300]})
    return out


DESCRIBE = Capability("describe", DESCRIBE_VERSION, _load, describe)
