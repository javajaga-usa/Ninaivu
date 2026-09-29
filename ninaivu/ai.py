"""Tiered AI engine.

Three tiers, chosen automatically:

``clip``
    OpenCLIP image/text embeddings. Gives natural-language search
    ("sunset over water", "my dog on a beach"), zero-shot subject tags and a
    prompt-based explicit-content score. Model downloads once (~350 MB) and
    then runs locally on CPU.

``light``
    No torch: colour/composition heuristics only. Search stays keyword based
    over filenames, folders and heuristic tags.

``off``
    Nothing runs; the gallery works exactly the same, just without tags.

Every tier exposes the same :meth:`Engine.analyse` contract, so the scanner
never branches on which one is loaded.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Sequence

from PIL import Image

log = logging.getLogger(__name__)

# The odd operation Apple's GPU backend has no kernel for runs on the processor
# instead of stopping the scan. Read when torch starts, so it is set on import.
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

#: Photographs are opened and shrunk for the model on this many threads.
#: Decoding releases the interpreter, and one thread at a time was slower than
#: the model itself once the model ran on a graphics processor.
DECODE_THREADS = max(2, min(4, (os.cpu_count() or 2) // 2))
_decoders: ThreadPoolExecutor | None = None
_decoders_lock = threading.Lock()


def _decode_pool() -> ThreadPoolExecutor:
    global _decoders
    with _decoders_lock:
        if _decoders is None:
            _decoders = ThreadPoolExecutor(max_workers=DECODE_THREADS,
                                           thread_name_prefix="ninaivu-decode")
        return _decoders

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None  # type: ignore


# ---------------------------------------------------------------------------
# Zero-shot vocabulary
# ---------------------------------------------------------------------------

#: Concepts CLIP scores each image against. Grouped only for readability —
#: at runtime it is one flat list of prompts.
CONCEPTS: dict[str, list[str]] = {
    "scene": [
        "beach", "mountains", "forest", "desert", "lake", "river", "ocean",
        "waterfall", "field", "garden", "park", "city street", "skyline",
        "bridge", "harbour", "countryside", "snow", "sunset", "sunrise",
        "night sky", "stars", "clouds", "rain", "fog", "rainbow", "cave",
        "island", "canyon", "volcano", "glacier",
    ],
    "place": [
        "kitchen", "living room", "bedroom", "office", "restaurant", "cafe",
        "bar", "museum", "church", "temple", "stadium", "airport", "train station",
        "hotel room", "classroom", "library", "gym", "hospital", "shop",
        "market", "swimming pool", "playground", "construction site", "farm",
    ],
    "subject": [
        "portrait of a person", "group of people", "crowd", "child", "baby",
        "wedding", "birthday party", "concert", "graduation", "family gathering",
        "selfie", "couple",
    ],
    "animal": [
        "dog", "cat", "bird", "horse", "cow", "sheep", "fish", "butterfly",
        "insect", "squirrel", "deer", "wildlife", "pet",
    ],
    "object": [
        "car", "bicycle", "motorcycle", "boat", "airplane", "train", "bus",
        "truck", "building", "house", "flower", "tree", "plant", "book",
        "computer", "phone", "camera", "guitar", "piano", "painting",
        "sculpture", "clothing", "shoes", "jewellery", "furniture", "toy",
        "sign", "map", "whiteboard", "screenshot of a screen", "document",
        "receipt", "chart or graph",
    ],
    "food": [
        "food", "coffee", "cake", "pizza", "salad", "fruit", "vegetables",
        "breakfast", "dinner table", "cocktail", "wine", "beer", "dessert",
        "barbecue",
    ],
    "activity": [
        "hiking", "running", "cycling", "swimming", "skiing", "surfing",
        "dancing", "cooking", "reading", "playing football", "playing basketball",
        "fishing", "camping", "travelling", "shopping", "working at a desk",
        "meeting", "presentation",
    ],
    "style": [
        "black and white photograph", "aerial drone photograph", "macro close-up",
        "long exposure photograph", "silhouette", "panorama", "illustration",
        "cartoon", "logo", "poster", "abstract pattern", "texture",
    ],
}

FLAT_CONCEPTS: list[str] = [c for group in CONCEPTS.values() for c in group]

#: Short label shown in the UI for each concept prompt.
LABELS: list[str] = [
    c.replace("photograph", "photo")
     .replace("portrait of a person", "portrait")
     .replace("screenshot of a screen", "screenshot")
     .replace("chart or graph", "chart")
     .strip()
    for c in FLAT_CONCEPTS
]

TEMPLATES = ("a photo of {}", "a picture of {}", "{}")

NSFW_PROMPTS = [
    "a safe for work photograph",
    "an ordinary everyday photograph",
    "a landscape or object photograph",
    "a nude person, explicit nudity, exposed private body parts",
    "explicit sexual content, pornography, sex, adult",
    "nude photo, naked body, genitals, genitalia",
]
NSFW_UNSAFE_FROM = 3  # indices >= this are the unsafe class


# ---------------------------------------------------------------------------
# Base engine
# ---------------------------------------------------------------------------

class Engine:
    name = "none"
    model_id = "none"
    semantic = False

    def analyse(self, paths: Sequence[Path], **_: Any) -> list[dict[str, Any]]:
        return [{} for _ in paths]

    def encode_text(self, query: str) -> "np.ndarray | None":  # noqa: F821
        return None

    def close(self) -> None:
        pass

    @property
    def info(self) -> dict[str, Any]:
        return {"engine": self.name, "model": self.model_id, "semantic": self.semantic}


# ---------------------------------------------------------------------------
# Heuristic tags — cheap, always applied
# ---------------------------------------------------------------------------

def heuristic_tags(img: Image.Image) -> list[str]:
    """Composition/colour tags that need no model at all."""
    tags: list[str] = []
    try:
        w, h = img.size
        ratio = w / h if h else 1.0
        if ratio > 1.6:
            tags.append("wide")
        elif ratio < 0.7:
            tags.append("tall")
        else:
            tags.append("square" if 0.95 < ratio < 1.05 else "standard")
        if ratio > 2.2:
            tags.append("panorama")

        from .media.media import _rgb_pixels

        small = img.convert("RGB").resize((32, 32), Image.Resampling.BILINEAR)
        pixels = _rgb_pixels(small)
        n = len(pixels)
        lum = sum(0.299 * r + 0.587 * g + 0.114 * b for r, g, b in pixels) / n
        if lum < 60:
            tags.append("dark")
        elif lum > 195:
            tags.append("bright")

        sat = sum(max(p) - min(p) for p in pixels) / n
        if sat < 12:
            tags.append("monochrome")
        elif sat > 90:
            tags.append("colourful")

        if w * h >= 8_000_000:
            tags.append("high resolution")
    except Exception:
        pass
    return tags


# ---------------------------------------------------------------------------
# Light engine (no torch)
# ---------------------------------------------------------------------------

class LightEngine(Engine):
    name = "light"
    model_id = "heuristic-v1"
    semantic = False

    def analyse(self, paths: Sequence[Path], **_: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for path in paths:
            try:
                with Image.open(path) as img:
                    out.append({"tags": heuristic_tags(img)})
            except Exception:
                out.append({})
        return out


# ---------------------------------------------------------------------------
# CLIP engine
# ---------------------------------------------------------------------------

def usable_device(torch: Any, *, gpu: bool = True, apple: bool = False) -> str:
    """``"cuda"`` or ``"mps"`` only if it can really run a kernel, else ``"cpu"``.

    ``torch.cuda.is_available()`` answers a narrower question than it looks:
    it reports a driver and a device, not that this build carries kernels for
    *this* card. A ROCm build installed for the wrong gfx target answers yes
    and then fails on the first real operation -- which would be part-way
    through a scan, not at startup. One tiny multiply settles it up front.

    ``apple`` lets a caller that has been checked on it use the graphics
    processor in a Mac (``"mps"``). The image model has: identical tags and
    vectors to the processor's, 2.4 times as fast on an M6. ``gpu=False`` is
    the household's switch for keeping everything on the processor.
    """
    if not gpu:
        return "cpu"
    for device, present in (
            ("cuda", lambda: torch.cuda.is_available()),
            ("mps", lambda: apple and torch.backends.mps.is_available())):
        try:
            if not present():
                continue
            probe = torch.ones(8, 8, device=device)
            float((probe @ probe).sum())
            return device
        except Exception:  # noqa: BLE001 - any failure here means: not this one.
            continue
    return "cpu"


#: How a pretrained tag says images are prepared, and the argument that says
#: the same to open_clip when it is given the weights file instead of the tag.
_TAG_PREPARATION = {"mean": "image_mean", "std": "image_std",
                    "interpolation": "image_interpolation",
                    "resize_mode": "image_resize_mode"}


def cached_weights(model_name: str, pretrained: str) -> tuple[str, dict[str, Any]] | None:
    """A pretrained tag's weights already on this computer, with how to prepare
    images for them; None when they have to come from the internet.

    Given a tag, open_clip asks huggingface.co whether its weights have changed
    every time it loads them, downloaded or not: a request on every start, and
    a wait at a start without internet. Given the file, it asks nothing, but
    prepares images its own way, so what the tag says is passed on with it. A
    tag saying anything else (quick_gelu is only ever checked, never applied)
    is loaded as a tag.
    """
    try:
        import open_clip  # noqa: PLC0415
        from huggingface_hub import try_to_load_from_cache  # noqa: PLC0415
        from open_clip.constants import HF_SAFE_WEIGHTS_NAME, HF_WEIGHTS_NAME  # noqa: PLC0415
    except ImportError:
        return None
    cfg = open_clip.get_pretrained_cfg(model_name, pretrained) or {}
    hub = cfg.get("hf_hub") or ""
    if not hub or set(cfg) - {"url", "hf_hub", "quick_gelu", *_TAG_PREPARATION}:
        return None
    # As open_clip reads it: "org/model/" is the default file, and a
    # safetensors copy is preferred to a pickled one.
    repo, filename = os.path.split(hub)
    filename = filename or HF_WEIGHTS_NAME
    names = [filename]
    if filename == HF_WEIGHTS_NAME:
        names.insert(0, HF_SAFE_WEIGHTS_NAME)
    elif filename.endswith((".bin", ".pth")):
        names.insert(0, filename[:-4] + ".safetensors")
    for name in names:
        try:
            path = try_to_load_from_cache(repo, name)
        except Exception:  # noqa: BLE001 - a cache it cannot read: load as a tag
            return None
        if isinstance(path, str) and os.path.isfile(path):
            return path, {arg: cfg[key] for key, arg in _TAG_PREPARATION.items() if key in cfg}
    return None


class ClipEngine(Engine):
    name = "clip"
    semantic = True
    #: Cosine floors for this model family. CLIP's cosines for a real match sit
    #: around 0.2-0.3; SigLIP's (see below) are much lower.
    search_floor = 0.15
    tag_threshold: float | None = None      # None: use the configured tag_threshold
    nsfw_threshold: float | None = None     # None: use the configured nsfw_threshold
    #: SigLIP scores the explicit-content prompts as independent probabilities.
    sigmoid_scores = False

    def __init__(self, model_name: str, pretrained: str, device: str | None = None,
                 tokenizer_dir: str | None = None, pretrained_label: str | None = None,
                 *, gpu: bool = True) -> None:
        import torch  # noqa: PLC0415
        import open_clip  # noqa: PLC0415

        self.torch = torch
        self.device = device or usable_device(torch, gpu=gpu, apple=True)
        # A graphics processor takes one piece of work at a time from this
        # process. The scan's photographs and a search from the gallery used to
        # meet only on the processor, where that is harmless; on the GPU they
        # take turns, which costs nothing because it would serialise them anyway.
        self._gpu_lock = (threading.Lock() if self.device != "cpu"
                          else contextlib.nullcontext())
        torch.set_num_threads(max(1, (torch.get_num_threads() or 4)))

        weights, preparation = pretrained, {}
        cached = (cached_weights(model_name, pretrained)
                  if pretrained and not os.path.isfile(pretrained) else None)
        if cached is not None:
            weights, preparation = cached
            pretrained_label = pretrained_label or pretrained
        if cached is not None or os.path.isfile(str(pretrained)) or tokenizer_dir:
            # Everything the model needs is on this computer, so "no telemetry"
            # can be literal: with this set the Hugging Face libraries make no
            # request of any kind — not a version check, not a "does this file
            # still exist". Set only for this process, and only once the
            # weights are known to be here, so a first download still works.
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
        self.model, _, self.preprocess = open_clip.create_model_and_transforms(
            model_name, pretrained=weights, device=self.device, **preparation
        )
        self.model.eval()
        if tokenizer_dir:
            # A downloaded model reads its tokenizer from its own folder, so
            # nothing is fetched from the internet when the engine starts.
            from open_clip.tokenizer import HFTokenizer  # noqa: PLC0415
            text_cfg = (open_clip.get_model_config(model_name) or {}).get("text_cfg", {})
            self.tokenizer = HFTokenizer(tokenizer_dir,
                                         context_length=text_cfg.get("context_length", 64),
                                         **text_cfg.get("tokenizer_kwargs", {}))
        else:
            self.tokenizer = open_clip.get_tokenizer(model_name)
        # Stable across machines: a local weights path must not become part of it.
        self.model_id = f"{model_name}/{pretrained_label or pretrained}"
        if getattr(self.model, "logit_bias", None) is not None:
            # SigLIP. Measured on real photographs with ViT-B-16-SigLIP2: a
            # correct description scores a cosine of about 0.08-0.16, unrelated
            # text -0.03-0.07 (CLIP: roughly 0.25 against 0.15). These keep the
            # matches and drop most of the rest.
            self.search_floor = 0.075
            self.tag_threshold = 0.085
            # CLIP's explicit-content check compares a photo against safe and
            # unsafe descriptions with a softmax. SigLIP's cosines are so close
            # together that the softmax is nearly even: a plain blue image
            # scored 0.70 and an ordinary face 0.76, both over the 0.6 line.
            # SigLIP is trained to give each description its own probability,
            # so the score is the highest probability of any unsafe one. On
            # ordinary photographs, faces and flat colours that was at most
            # 0.0003; flagging at 0.01 leaves a wide margin. How reliably it
            # catches explicit photographs was not measured.
            self.sigmoid_scores = True
            self.nsfw_threshold = 0.01
        self._text_lock = threading.Lock()

        self.concept_matrix = self._embed_prompts(FLAT_CONCEPTS)
        self.nsfw_matrix = self._embed_prompts(NSFW_PROMPTS, templates=("{}",))

    # -- helpers ----------------------------------------------------------
    def _embed_prompts(self, prompts: Sequence[str],
                       templates: Sequence[str] = TEMPLATES):
        torch = self.torch
        vectors = []
        with torch.no_grad():
            for prompt in prompts:
                texts = [tpl.format(prompt) for tpl in templates]
                tokens = self.tokenizer(texts).to(self.device)
                feats = self.model.encode_text(tokens)
                feats = feats / feats.norm(dim=-1, keepdim=True)
                mean = feats.mean(dim=0)
                vectors.append(mean / mean.norm())
        return torch.stack(vectors)

    def encode_text(self, query: str):
        torch = self.torch
        with self._text_lock, self._gpu_lock, torch.no_grad():
            tokens = self.tokenizer([query]).to(self.device)
            feats = self.model.encode_text(tokens)
            feats = feats / feats.norm(dim=-1, keepdim=True)
        return feats[0].cpu().numpy().astype("float32")

    def embed_texts(self, texts: Sequence[str]):
        """Unit vectors for several phrases at once, one row each."""
        torch = self.torch
        with self._text_lock, self._gpu_lock, torch.no_grad():
            tokens = self.tokenizer(list(texts)).to(self.device)
            feats = self.model.encode_text(tokens)
            feats = feats / feats.norm(dim=-1, keepdim=True)
        return feats.cpu().numpy().astype("float32")

    def embed_images(self, images: Sequence[Any]):
        """Unit vectors for already-open PIL images, one row each."""
        torch = self.torch
        batch = torch.stack([self.preprocess(image) for image in images])
        with self._gpu_lock, torch.no_grad():
            feats = self.model.encode_image(batch.to(self.device))
            feats = feats / feats.norm(dim=-1, keepdim=True)
            return feats.cpu().numpy().astype("float32")

    def _load(self, path: Path):
        """(heuristic tags, model input) for one photograph, or None."""
        try:
            with Image.open(path) as img:
                rgb = img.convert("RGB")
                return heuristic_tags(rgb), self.preprocess(rgb)
        except Exception:  # noqa: BLE001 - unreadable: no tags, not a failed batch
            return None

    # -- main -------------------------------------------------------------
    def analyse(self, paths: Sequence[Path], *, tag_threshold: float = 0.18,
                max_tags: int = 8) -> list[dict[str, Any]]:
        torch = self.torch
        tensors, valid, heuristics = [], [], []
        for loaded in _decode_pool().map(self._load, paths):
            if loaded is None:
                heuristics.append([])
                valid.append(False)
            else:
                heuristics.append(loaded[0])
                tensors.append(loaded[1])
                valid.append(True)

        results: list[dict[str, Any]] = [{} for _ in paths]
        if not tensors:
            return results

        with self._gpu_lock, torch.no_grad():
            batch = torch.stack(tensors).to(self.device)
            feats = self.model.encode_image(batch)
            feats = feats / feats.norm(dim=-1, keepdim=True)

            concept_sim = feats @ self.concept_matrix.T
            if self.sigmoid_scores:
                logits = (self.model.logit_scale.exp() * (feats @ self.nsfw_matrix[NSFW_UNSAFE_FROM:].T)
                          + self.model.logit_bias)
                nsfw_t = torch.sigmoid(logits).max(dim=-1).values
            else:
                nsfw_logits = 100.0 * (feats @ self.nsfw_matrix.T)
                nsfw_t = nsfw_logits.softmax(dim=-1)[:, NSFW_UNSAFE_FROM:].sum(dim=-1)

        feats_np = feats.cpu().numpy().astype("float32")
        sims = concept_sim.cpu().numpy()
        nsfw = nsfw_t.cpu().numpy()

        cursor = 0
        for i, ok in enumerate(valid):
            if not ok:
                results[i] = {"tags": heuristics[i]}
                continue
            row = sims[cursor]
            order = row.argsort()[::-1][: max_tags * 2]
            tags: list[str] = []
            for idx in order:
                if float(row[idx]) < tag_threshold:
                    break
                label = LABELS[idx]
                if label not in tags:
                    tags.append(label)
                if len(tags) >= max_tags:
                    break
            for extra in heuristics[i]:
                if extra not in tags:
                    tags.append(extra)

            results[i] = {
                "tags": tags,
                "caption": ", ".join(tags[:3]) if tags else None,
                "nsfw_score": round(float(nsfw[cursor]), 4),
                "embedding": feats_np[cursor].tobytes(),
            }
            cursor += 1
        return results


# ---------------------------------------------------------------------------
# Factory + semantic search
# ---------------------------------------------------------------------------

_engine_lock = threading.Lock()
_engine: Engine | None = None


def build_engine(cfg: Any, force: bool = False) -> Engine:
    """Return the process-wide engine, loading it lazily and at most once."""
    global _engine
    with _engine_lock:
        if _engine is not None and not force:
            return _engine

        choice = (cfg.ai_engine or "auto").lower()
        if not cfg.ai_enabled or choice == "off":
            _engine = Engine()
            return _engine

        # "auto" means what the hardware tier says (server/tiers.py): the
        # image model on a Full machine, the light engine on a Basic one —
        # a 2 GB box would otherwise spend its start trying to load torch
        # and fail slowly. Saying "clip" outright still tries it anywhere.
        if choice == "auto":
            choice = getattr(cfg, "ai_engine_resolved", None) or "clip"

        if choice == "clip":
            try:
                _engine = ClipEngine(*clip_choice(cfg),
                                     gpu=bool(getattr(cfg, "ai_gpu", True)))
                log.info("image model ready: %s on %s", _engine.model_id, _engine.device)
                return _engine
            except Exception as exc:  # noqa: BLE001
                # A warning with the reason, not a line on a console nobody is
                # watching: on the light engine, searching by description finds
                # nothing, and "auto" used to blame a missing torch even when
                # torch was there and the model file itself would not load.
                log.warning("the image model could not be loaded, so search by "
                            "description is off (the light engine is in use): %s",
                            exc)

        _engine = LightEngine()
        return _engine


def clip_choice(cfg: Any) -> tuple[Any, ...]:
    """The CLIP-family model to load: ``(name, pretrained, device, tokenizer_dir, label)``.

    ``clip_model = "auto"`` (the default) uses SigLIP 2 when it has been
    downloaded (Admin → AI models, or tools/setup_ai_models.py), and the
    smaller ViT-B-32 otherwise. Any other value is used as given.
    """
    if (cfg.clip_model or "auto") != "auto":
        return cfg.clip_model, cfg.clip_pretrained, None, None, None
    from .media import model_catalog  # noqa: PLC0415
    if model_catalog.installed("siglip2") and not model_catalog.missing_packages("siglip2"):
        folder = model_catalog.models_root() / "siglip2"
        return ("ViT-B-16-SigLIP2", str(folder / "open_clip_model.safetensors"), None,
                str(folder), "webli")
    return "ViT-B-32", cfg.clip_pretrained or "laion2b_s34b_b79k", None, None, None


def get_engine() -> Engine | None:
    return _engine


class _ThresholdedEngine:
    """Binds the configured thresholds to :meth:`ClipEngine.analyse`."""

    def __init__(self, engine: Engine, cfg: Any) -> None:
        self._engine = engine
        self._cfg = cfg

    def __getattr__(self, item: str) -> Any:
        return getattr(self._engine, item)

    def analyse(self, paths: Sequence[Path]) -> list[dict[str, Any]]:
        if isinstance(self._engine, ClipEngine):
            return self._engine.analyse(
                paths,
                tag_threshold=(self._engine.tag_threshold
                               if self._engine.tag_threshold is not None
                               else self._cfg.tag_threshold),
                max_tags=self._cfg.max_tags,
            )
        return self._engine.analyse(paths)


def bind(engine: Engine, cfg: Any) -> Any:
    return _ThresholdedEngine(engine, cfg)


def _matrix(ids: Sequence[int], blobs, dim: int):
    """One float32 view over the vectors, whatever shape they arrive in.

    `blobs` is either the contiguous block :func:`db.embedding_store` holds — in
    which case this copies nothing at all — or a list of separate vectors, which
    is what the older callers and the tests pass.
    """
    buffer = blobs if isinstance(blobs, (bytes, bytearray, memoryview)) \
        else b"".join(blobs)
    return np.frombuffer(buffer, dtype="float32").reshape(len(ids), dim)


def _top(scores, k: int):
    """Positions of the *k* highest scores, best first.

    ``argsort`` orders every score to keep a few hundred; at a million photos
    that is most of a search's time. ``argpartition`` finds the top *k* in one
    linear pass, and only those few are sorted. Ties may come back in a
    different order than a full sort would give; the scores are the same.
    """
    k = int(k)
    if k <= 0:
        return scores[:0].astype(np.int64)
    if k >= scores.shape[0]:
        return np.argsort(-scores)
    part = np.argpartition(-scores, k - 1)[:k]
    return part[np.argsort(-scores[part])]


def semantic_search(query_vector, ids: Sequence[int], blobs,
                    dim: int, top_k: int = 500, min_score: float = 0.15,
                    rows: Sequence[int] | None = None) -> list[tuple[int, float]]:
    """Cosine-rank stored embeddings against a query vector.

    `rows` narrows the answer to the positions a viewer is allowed to see. The
    multiply still runs across the whole matrix — it is one pass of arithmetic
    over memory that is already here, and far cheaper than assembling a second
    matrix per viewer — and only the scores that count are gathered afterwards.
    """
    if np is None or query_vector is None or not ids:
        return []
    matrix = _matrix(ids, blobs, dim)
    scores = matrix @ np.asarray(query_vector, dtype="float32")

    if rows is None:
        order = _top(scores, top_k)
        return [(int(ids[i]), float(scores[i])) for i in order
                if float(scores[i]) >= min_score]

    picked = np.asarray(rows, dtype=np.int64)
    if not picked.size:
        return []
    mine = scores[picked]
    order = _top(mine, top_k)
    return [(int(ids[picked[i]]), float(mine[i])) for i in order
            if float(mine[i]) >= min_score]


def similar_to(vector_blob: bytes, ids: Sequence[int], blobs: Sequence[bytes],
               dim: int, top_k: int = 60) -> list[tuple[int, float]]:
    """Nearest neighbours of one asset's embedding."""
    if np is None or not ids:
        return []
    query = np.frombuffer(vector_blob, dtype="float32")
    matrix = _matrix(ids, blobs, dim)
    scores = matrix @ query
    order = _top(scores, top_k + 1)
    return [(int(ids[i]), float(scores[i])) for i in order]
