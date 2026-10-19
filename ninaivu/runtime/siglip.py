"""Search by description without PyTorch: SigLIP 2 through ONNX Runtime.

The gallery's image model (ninaivu/ai.py) has been SigLIP 2 ViT-B/16 run by
open_clip on PyTorch: about 2 GB of packages before the model itself, which
is why no portable download carries it. This is the same model exported to
ONNX (Immich's export of open_clip's ``ViT-B-16-SigLIP2``/``webli``, pinned
on the AI models page as ``search-onnx``) run by ONNX Runtime, about 20 MB
of package, inside the model host. The server's half is ``ai.OnnxEngine``.

**The same vectors.** Images are prepared as open_clip prepares them for this
model (squashed to 224 by 224 with bicubic resampling, scaled to [-1, 1]) and
text is cleaned and tokenised as open_clip does (lower case, punctuation
taken out, Gemma's tokenizer, end-of-text, padded to 64), so the vectors are
open_clip's within rounding and keep its model id. A library indexed with
SigLIP 2 on PyTorch is not indexed again when the engine changes; one
indexed with the smaller ViT-B/32 is, as it is when SigLIP 2 is downloaded
for PyTorch. ``tools/check_onnx_search.py`` measures how close the two are on
the household's own photographs.

**Where it runs.** The image half on the Neural Engine and graphics processor
on a Mac (Core ML) and on the graphics card on Windows (DirectML), when the
installed ONNX Runtime has them, the processor otherwise. The text half,
which reads one search at a time and is mostly a 256,000-word table, on the
processor: it is quick there, and Core ML would spend longer compiling it
than it saves.

**Tags and the content check** are worked out as the PyTorch engine works
them out: each photograph against the same descriptions, with SigLIP's own
scale and bias for the explicit-content probability. The descriptions'
vectors cost half a minute of the processor to make, so they are made once
and kept beside the model.
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import string
import time
import unicodedata
from pathlib import Path
from typing import Any, Sequence

from .host import Capability, Unavailable

log = logging.getLogger(__name__)

#: The AI models page's entry for this model (media/model_catalog.py).
MODEL = "search-onnx"
#: The id its vectors are stored under: open_clip's, because they are its.
MODEL_ID = "ViT-B-16-SigLIP2/webli"
#: The capability's own version: how photographs are prepared and scored.
SEARCH_VERSION = 1

SIZE = 224
CONTEXT = 64
PAD, EOS = 0, 1
#: SigLIP 2 B/16's learned scale (as a logarithm) and bias, from Google's own
#: checkpoint (big_vision siglip2_b16_224.npz, ``params/t`` and ``params/b``),
#: which open_clip's weights are converted from. Only the explicit-content
#: probability uses them; the export carries the two towers alone.
LOG_SCALE = 4.7244534
BIAS = -16.771725

_PUNCTUATION = str.maketrans("", "", string.punctuation)


def folder() -> Path:
    from ..media import model_catalog                          # noqa: PLC0415
    return model_catalog.models_root() / model_catalog.MODELS[MODEL]["files"][0]["folder"]


#: The packages the image engine needs: ONNX Runtime and Hugging Face's
#: tokenizer library, about 25 MB together (Extras, "image_engine").
PACKAGES = ("onnxruntime", "tokenizers")


def missing_packages() -> str | None:
    """Why the image engine's packages cannot be used here, or None."""
    import importlib.util                                      # noqa: PLC0415
    from ..words import said                                   # noqa: PLC0415
    if any(importlib.util.find_spec(name) is None for name in PACKAGES):
        return said("the image engine is not installed (Search and descriptions without PyTorch, under Extras on the Home & extensions page)")
    return None


def ready() -> str | None:
    """Why search cannot run through ONNX here, or None when it can."""
    from ..media import model_catalog                          # noqa: PLC0415
    from ..words import said                                   # noqa: PLC0415
    if why := missing_packages():
        return why
    if not model_catalog.installed(MODEL):
        return said("the search model is not downloaded (AI models page)")
    return None


# ---------------------------------------------------------------------------
# Photographs and words, as open_clip hands them to the model
# ---------------------------------------------------------------------------

def pixels(image: Any) -> Any:
    """One photograph as the model's input: 224 x 224 x 3 bytes.

    open_clip's "squash": the whole picture, stretched to a square, resized
    with bicubic resampling. Made in the server, where the photographs are
    opened, so what crosses to the host is 150 KB a photograph.
    """
    import numpy as np                                         # noqa: PLC0415
    from PIL import Image                                      # noqa: PLC0415
    rgb = image if image.mode == "RGB" else image.convert("RGB")
    return np.asarray(rgb.resize((SIZE, SIZE), Image.Resampling.BICUBIC), dtype=np.uint8)


def normalise(batch: Any) -> Any:
    """Bytes in, the model's input out: channels first, scaled to [-1, 1]."""
    import numpy as np                                         # noqa: PLC0415
    array = np.asarray(batch, dtype=np.float32)
    if array.ndim == 3:
        array = array[None]
    return np.ascontiguousarray(((array / 255.0) - 0.5) / 0.5).transpose(0, 3, 1, 2).copy()


def clean(text: str) -> str:
    """open_clip's "canonicalize" for SigLIP: entities undone, underscores as
    spaces, punctuation out, lower case, single spaces."""
    text = html.unescape(html.unescape(unicodedata.normalize("NFC", str(text)))).strip()
    text = text.replace("_", " ").translate(_PUNCTUATION).lower()
    return " ".join(text.split())


def token_ids(tokenizer: Any, text: str) -> list[int]:
    """The 64 token ids for *text*: its pieces, end-of-text, then padding.

    As Gemma's tokenizer is set up for SigLIP 2 (no beginning-of-text, an
    end-of-text, padded on the right); a text too long keeps its first 63
    pieces and still ends with end-of-text.
    """
    ids = list(tokenizer.encode(clean(text), add_special_tokens=False).ids)[:CONTEXT - 1]
    ids.append(EOS)
    return ids + [PAD] * (CONTEXT - len(ids))


def unit(rows: Any) -> Any:
    import numpy as np                                         # noqa: PLC0415
    rows = np.asarray(rows, dtype=np.float32)
    norms = np.linalg.norm(rows, axis=-1, keepdims=True)
    return (rows / np.maximum(norms, 1e-12)).astype(np.float32)


# ---------------------------------------------------------------------------
# The model, inside the host
# ---------------------------------------------------------------------------

def _session(path: Path, threads: int, providers: Sequence[str]) -> Any:
    import onnxruntime                                         # noqa: PLC0415
    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = max(1, int(threads))
    options.inter_op_num_threads = 1
    try:
        return onnxruntime.InferenceSession(str(path), options, providers=list(providers))
    except Exception:                                          # noqa: BLE001
        if list(providers) == ["CPUExecutionProvider"]:
            raise
        # An accelerator that cannot take this model fails at load, not
        # quietly: the processor takes anything.
        log.warning("%s would not load on %s; using the processor", path.name, providers[0],
                    exc_info=True)
        return onnxruntime.InferenceSession(str(path), options,
                                            providers=["CPUExecutionProvider"])


class Siglip:
    """SigLIP 2's two halves, each loaded the first time it is needed."""

    def __init__(self, root: Path, threads: int = 2) -> None:
        from .devices import CPU, onnx_providers                # noqa: PLC0415
        self.root = Path(root)
        self.threads = threads
        self._image_providers = onnx_providers()
        self._text_providers = [CPU]
        self._visual: Any = None
        self._textual: Any = None
        self._tokenizer: Any = None
        self._prompts: dict[str, Any] | None = None

    # -- the halves -------------------------------------------------------
    def _file(self, name: str) -> Path:
        path = self.root / name
        if not path.is_file():
            raise Unavailable("the search model is not downloaded (AI models page)")
        return path

    @property
    def visual(self) -> Any:
        if self._visual is None:
            started = time.monotonic()
            self._visual = _session(self._file("visual/model.onnx"), self.threads,
                                    self._image_providers)
            log.warning("search model (images) loaded on %s in %.1f s",
                        self._visual.get_providers()[0], time.monotonic() - started)
        return self._visual

    @property
    def textual(self) -> Any:
        if self._textual is None:
            from tokenizers import Tokenizer                    # noqa: PLC0415
            started = time.monotonic()
            self._tokenizer = Tokenizer.from_file(str(self._file("textual/tokenizer.json")))
            self._textual = _session(self._file("textual/model.onnx"), self.threads,
                                     self._text_providers)
            log.warning("search model (words) loaded in %.1f s", time.monotonic() - started)
        return self._textual

    # -- vectors ----------------------------------------------------------
    def embed_pixels(self, batch: Any) -> Any:
        """Unit vectors for photographs already made into :func:`pixels`."""
        import numpy as np                                     # noqa: PLC0415
        session = self.visual
        feed = session.get_inputs()[0]
        images = normalise(batch)
        if not len(images):
            return np.zeros((0, 0), dtype=np.float32)
        if isinstance(feed.shape[0], int) and feed.shape[0] == 1 and len(images) > 1:
            # An export with its batch fixed at one: one photograph at a time.
            rows = [session.run(None, {feed.name: images[i:i + 1]})[0] for i in range(len(images))]
            return unit(np.concatenate(rows))
        return unit(session.run(None, {feed.name: images})[0])

    def embed_texts(self, texts: Sequence[str], batch: int = 32) -> Any:
        """Unit vectors for phrases, one row each."""
        import numpy as np                                     # noqa: PLC0415
        session = self.textual
        feed = session.get_inputs()[0]
        kind = np.int32 if "int32" in str(feed.type) else np.int64
        rows = []
        texts = list(texts)
        fixed = isinstance(feed.shape[0], int) and feed.shape[0] == 1
        step = 1 if fixed else batch
        for at in range(0, len(texts), step):
            ids = np.asarray([token_ids(self._tokenizer, t) for t in texts[at:at + step]],
                             dtype=kind)
            rows.append(session.run(None, {feed.name: ids})[0])
        if not rows:
            return np.zeros((0, 0), dtype=np.float32)
        return unit(np.concatenate(rows))

    # -- the descriptions tags are scored against -------------------------
    def prompts(self) -> dict[str, Any]:
        """The tag and content-check descriptions as vectors, made once."""
        if self._prompts is None:
            self._prompts = _prompt_vectors(self)
        return self._prompts


def _prompt_key() -> str:
    from .. import ai                                          # noqa: PLC0415
    spec = json.dumps([SEARCH_VERSION, ai.FLAT_CONCEPTS, list(ai.TEMPLATES), ai.NSFW_PROMPTS])
    return hashlib.sha256(spec.encode("utf-8")).hexdigest()[:16]


def _prompt_vectors(model: Siglip) -> dict[str, Any]:
    """Each tag's vector (the mean of its phrasings) and each content-check
    description's, from the file beside the model when it is the one for
    these descriptions; made and written there when it is not."""
    import numpy as np                                         # noqa: PLC0415
    from .. import ai                                          # noqa: PLC0415
    kept = model.root / f"prompts-{_prompt_key()}.npz"
    try:
        with np.load(kept) as saved:
            return {"concepts": saved["concepts"], "nsfw": saved["nsfw"]}
    except (OSError, KeyError, ValueError):
        pass
    started = time.monotonic()
    phrasings = [t.format(c) for c in ai.FLAT_CONCEPTS for t in ai.TEMPLATES]
    vectors = model.embed_texts(phrasings).reshape(len(ai.FLAT_CONCEPTS), len(ai.TEMPLATES), -1)
    concepts = unit(vectors.mean(axis=1))
    nsfw = model.embed_texts(ai.NSFW_PROMPTS)
    try:
        partial = kept.with_name(kept.name + ".part.npz")
        np.savez(partial, concepts=concepts, nsfw=nsfw)
        partial.replace(kept)
    except OSError:
        log.warning("could not keep the tag descriptions beside the model", exc_info=True)
    log.warning("made the tag descriptions' vectors in %.1f s", time.monotonic() - started)
    return {"concepts": concepts, "nsfw": nsfw}


def score(vectors: Any, prompts: dict[str, Any], *, tag_threshold: float,
          max_tags: int) -> list[dict[str, Any]]:
    """Tags and the explicit-content probability for each photograph's vector,
    as ai.ClipEngine works them out for SigLIP."""
    import numpy as np                                         # noqa: PLC0415
    from .. import ai                                          # noqa: PLC0415
    if not len(vectors):
        return []
    sims = vectors @ prompts["concepts"].T
    logits = np.exp(LOG_SCALE) * (vectors @ prompts["nsfw"][ai.NSFW_UNSAFE_FROM:].T) + BIAS
    nsfw = (1.0 / (1.0 + np.exp(-logits))).max(axis=-1)
    out = []
    for row, unsafe in zip(sims, nsfw):
        tags: list[str] = []
        for index in row.argsort()[::-1][: max_tags * 2]:
            if float(row[index]) < tag_threshold:
                break
            label = ai.LABELS[index]
            if label not in tags:
                tags.append(label)
            if len(tags) >= max_tags:
                break
        out.append({"tags": tags, "nsfw_score": round(float(unsafe), 4)})
    return out


def _load(setup: dict[str, Any]) -> Siglip:
    if why := ready():
        raise Unavailable(why)
    return Siglip(folder(), threads=int(setup.get("threads") or 2))


def search(model: Siglip, *, op: str, **kwargs: Any) -> Any:
    """One call from the server's ``ai.OnnxEngine``.

    ``op="images"``: *pixels* (a list of :func:`pixels` arrays) with
    *tag_threshold* and *max_tags*; each photograph's tags, content-check
    score and vector as bytes. ``op="pixels"``: the vectors alone, as one
    array. ``op="texts"``: *texts*, a vector for each, as one array.
    """
    import numpy as np                                         # noqa: PLC0415
    if op == "texts":
        return model.embed_texts([str(t) for t in kwargs.get("texts") or []])
    batch = kwargs.get("pixels") or []
    if op == "pixels":
        return model.embed_pixels(np.stack(batch)) if batch else np.zeros((0, 0), np.float32)
    if op != "images":
        raise ValueError(f"the search model has no operation called {op}")
    if not batch:
        return []
    vectors = model.embed_pixels(np.stack(batch))
    scored = score(vectors, model.prompts(),
                   tag_threshold=float(kwargs.get("tag_threshold", 0.085)),
                   max_tags=int(kwargs.get("max_tags", 8)))
    for result, vector in zip(scored, vectors):
        result["embedding"] = vector.astype(np.float32).tobytes()
    return scored


SEARCH = Capability("search", SEARCH_VERSION, _load, search)
