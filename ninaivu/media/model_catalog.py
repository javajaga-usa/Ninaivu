"""The optional AI models Ninaivu can fetch, pinned to exact bytes.

A fresh checkout has none of these — they are hundreds of megabytes and each
has its own licence — so they are downloaded on request: from the admin
console's **AI models** tab, or with ``tools/setup_ai_models.py``. Both use this
catalogue, so both fetch the same files.

Every file is pinned to a repository revision *and* a SHA-256. A download that
does not match is discarded rather than used: a model is code-adjacent, and a
file that changed upstream is not the file these tools were tested with.
Nothing here runs during a normal request; nothing is fetched unless an
administrator asks.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

from ..cloud.tempfiles import create_new
from ..words import said

log = logging.getLogger(__name__)

#: Set by :func:`configure` from the household's settings, which is the only
#: thing that outranks the environment. One value, set once at start-up.
_root: Path | None = None


def configure(path: str | Path | None) -> None:
    """Point every model at *path*, or back at the default when it is empty.

    Called once from ``Services``. The settings win over the environment
    because the environment belongs to whoever started the process and the
    setting belongs to the household — and moving models to another disk is a
    decision about this installation, not about this launch.
    """
    global _root
    _root = Path(path).expanduser() if path else None


#: Where every AI model lives: the search model, the editing models, the face
#: detector and recogniser, the orientation model. One folder, so that moving
#: Ninaivu to another machine is three things to copy — this, the state folder
#: and the library — rather than a hunt for models tucked into each of them.
#:
#: The project folder by default, for a checkout. An installed copy keeps
#: them in a folder of the person's own instead (see `user_models_dir`): two
#: levels up from this file is then site-packages, the installer's ``pkgs``
#: or the inside of the Mac app, which every upgrade replaces whole, and
#: several gigabytes of models went with it each time.
#: ``NINAIVU_AI_MODELS_DIR`` moves it for one launch (a container volume,
#: another disk); the ``ai_models_dir`` setting moves it for good.
def models_root() -> Path:
    if _root is not None:
        return _root
    configured = os.environ.get("NINAIVU_AI_MODELS_DIR")
    if configured:
        return Path(configured)
    beside = Path(__file__).resolve().parents[2]
    return user_models_dir() if is_installed_copy(beside) else beside / ".ai-models"


def is_installed_copy(folder: Path) -> bool:
    """Whether *folder*, the one the ``ninaivu`` package is in, belongs to an
    installed program rather than a checkout: a Python's site-packages (the
    Linux installer, the Mac app, pip), pynsist's ``pkgs`` (the Windows
    installer), or anything inside an ``.app`` bundle."""
    return (folder.name.lower() in {"site-packages", "dist-packages", "pkgs"}
            or any(part.endswith(".app") for part in folder.parts))


def user_models_dir(platform: str | None = None) -> Path:
    """The models folder of an installed copy: beside the other per-user
    files Ninaivu keeps (ninaivu/desktop/control.py), never inside the
    program."""
    platform = platform or sys.platform
    if platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "Ninaivu" / "ai-models"
    if platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Ninaivu" / "ai-models"
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "ninaivu" / "ai-models"


HF = "https://huggingface.co/{repo}/resolve/{revision}/{name}"

MODELS: dict[str, dict[str, Any]] = {
    # The three Ninaivu used to fetch on its own, from the Faces and Straighten
    # pages, into a folder of their own in the state directory. They are
    # ordinary catalogue entries now, so that one page shows every model, one
    # folder holds every model, and re-downloading a file that is the right
    # size and the wrong bytes is a button rather than a hunt on disk.
    #
    # `essential` marks the ones the gallery's own features need, as opposed
    # to the Playground's: a household that never opens the editor still wants
    # these three.
    "faces": {
        "label": said("Finding and grouping faces"),
        "used_for": said("The People page — finds faces in photographs and works out which of them are the same person."),
        "licence": "Apache-2.0 (OpenCV Zoo)",
        "source": "OpenCV Zoo — YuNet and SFace",
        "runs_on": "CPU",
        "requires": ["cv2"],
        "essential": True,
        "version": 1,
        "files": [
            {"url": ("https://media.githubusercontent.com/media/opencv/opencv_zoo/"
                     "main/models/face_detection_yunet/"
                     "face_detection_yunet_2023mar.onnx"),
             "name": "face_detection_yunet_2023mar.onnx", "folder": "",
             "bytes": 232589,
             "sha256": "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"},
            {"url": ("https://media.githubusercontent.com/media/opencv/opencv_zoo/"
                     "main/models/face_recognition_sface/"
                     "face_recognition_sface_2021dec.onnx"),
             "name": "face_recognition_sface_2021dec.onnx", "folder": "",
             "bytes": 38696353,
             "sha256": "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"},
        ],
    },
    "orientation": {
        "label": said("Which way up a photograph goes"),
        "used_for": said("Straighten — works out the right way up for scans and photographs whose camera tag is missing."),
        "licence": "MIT",
        "source": "duartebarbosadev/deep-image-orientation-detection v2",
        "runs_on": "CPU",
        "requires": ["cv2"],
        "essential": True,
        "version": 2,
        #: The v1 file, for a household upgrading from it. Removed only once
        #: v2 is on disk: see `clear_superseded`.
        "replaces": ["orientation_efficientnetv2s.onnx"],
        "files": [
            {"url": ("https://github.com/duartebarbosadev/deep-image-orientation-detection"
                     "/releases/download/v2/orientation_model_v2_0.9882.onnx"),
             "name": "orientation_efficientnetv2s_v2.onnx", "folder": "",
             "bytes": 80578424,
             "sha256": "cffe911c1dff47fbfbbd90110aaab9c07134645c460d35b3ae8832079bea91ba"},
        ],
    },
    "lama": {
        "label": said("Object removal (LaMa)"),
        "used_for": said("Remove Object in the Playground — fills the painted area far better than the built-in method."),
        "licence": "Apache-2.0",
        "source": "Hugging Face Carve/LaMa-ONNX",
        "runs_on": "CPU",
        "requires": ["onnxruntime"],
        "files": [
            {"repo": "Carve/LaMa-ONNX", "revision": "c3c0c9e468934d62e79c329e35d82dd09ff8c444",
             "name": "lama_fp32.onnx", "folder": "onnx", "bytes": 208044816,
             "sha256": "1faef5301d78db7dda502fe59966957ec4b79dd64e16f03ed96913c7a4eb68d6"},
        ],
    },
    "upscale": {
        "label": said("Upscale ×4 (Real-ESRGAN)"),
        "used_for": said("Upscale in the Playground's Enhance tools."),
        "licence": "BSD-3-Clause (Real-ESRGAN); the ONNX conversion's repository states no licence of its own",
        "source": "Hugging Face facefusion/models-3.0.0",
        "runs_on": said("Graphics card through DirectML when available, otherwise CPU"),
        "requires": ["onnxruntime"],
        "files": [
            {"repo": "facefusion/models-3.0.0", "revision": "728b9659bd9691bf32cbf7f61af478d94b7ba81e",
             "name": "real_esrgan_x4_fp16.onnx", "folder": "onnx", "bytes": 36144084,
             "sha256": "223c117884ed92526ba1f89fbc09c663f15786c1049802c1a0ac8895b09a9cbc"},
        ],
    },
    "restore": {
        "label": said("Restore faces (GFPGAN 1.4)"),
        "used_for": said("Restore Faces in the Playground's Enhance tools."),
        "licence": "Apache-2.0 (GFPGAN); the ONNX conversion's repository states no licence of its own",
        "source": "Hugging Face facefusion/models-3.0.0",
        "runs_on": said("Graphics card through DirectML when available, otherwise CPU"),
        "requires": ["onnxruntime"],
        "files": [
            {"repo": "facefusion/models-3.0.0", "revision": "728b9659bd9691bf32cbf7f61af478d94b7ba81e",
             "name": "gfpgan_1.4.onnx", "folder": "onnx", "bytes": 340299087,
             "sha256": "accc4757b26bdb89b32b4d3500d4f79c9dff97c1dd7c7104bf9dcb95e3311385"},
        ],
    },
    "siglip2": {
        "label": said("AI search (SigLIP 2, ViT-B/16)"),
        "used_for": said("Describing photos and finding them by text. Replaces the smaller CLIP model; the library is re-indexed for search after Ninaivu restarts."),
        "licence": "Apache-2.0",
        "source": "Hugging Face timm/ViT-B-16-SigLIP2",
        "runs_on": "CPU",
        "requires": ["torch", "open_clip", "transformers"],
        "files": [
            {"repo": "timm/ViT-B-16-SigLIP2", "revision": "eee10eff6dd8cabae2d7f379d4e8cfcd352030aa",
             "name": name, "folder": "siglip2", "bytes": size, "sha256": digest}
            for name, size, digest in (
                ("open_clip_config.json", 944,
                 "d0b2ac6d4524cb95287138b9e5fa345211be7242e5da7eb55be894614be33987"),
                ("special_tokens_map.json", 327,
                 "d4671afd8f9b1ccd170443bd5478c0cc40fdbddc9343034caf6984ffd388b2c4"),
                ("tokenizer_config.json", 46382,
                 "513e778583a9ac0b74ae47784216f9d654d5132eee62d777597f5e842701323c"),
                ("tokenizer.json", 34362885,
                 "220c63d496e0c14e63eb656c91e0215e926202e4c74b1f089e09f1920d779b04"),
                ("open_clip_model.safetensors", 1500786832,
                 "d0a51069d2fa6c95b371b3bffd2a2c765c1815004798ee99f075290dbca73041"),
            )
        ],
    },
    "generative": {
        "label": said("Generative editing (MagicBrush)"),
        "used_for": said("Ask AI to Edit in the Playground - rewrites the whole photo from an instruction. Experimental: it can alter faces and fine detail."),
        "licence": "CreativeML Open RAIL-M",
        "source": "Hugging Face vinesmsuic/magicbrush-jul7 (the safetensors upload on pull request 2)",
        "runs_on": said("Graphics card in half precision when available, otherwise CPU"),
        "requires": ["torch", "diffusers", "transformers", "accelerate"],
        "settings": {"image_model": "magicbrush"},
        "files": [
            {"repo": "vinesmsuic/magicbrush-jul7", "revision": "105e768cb4167881474aee9b94e7db9c0b4a371d",
             "name": "feature_extractor/preprocessor_config.json", "folder": "magicbrush", "bytes": 520,
             "sha256": "6f638fb9401a6d6296feff533ee7efe657b787c49f954f82f5906b36ef2a1b1f"},
            {"repo": "vinesmsuic/magicbrush-jul7", "revision": "105e768cb4167881474aee9b94e7db9c0b4a371d",
             "name": "model_index.json", "folder": "magicbrush", "bytes": 579,
             "sha256": "ec001d78bfa495ca0755296356587737c3cc9bc5b12b75247369c1ce6175cf8c"},
            {"repo": "vinesmsuic/magicbrush-jul7", "revision": "105e768cb4167881474aee9b94e7db9c0b4a371d",
             "name": "safety_checker/config.json", "folder": "magicbrush", "bytes": 4584,
             "sha256": "65ac63a9dbf27e969ae069d85cfc00e399c109bfb4fe1114937105625254539d"},
            {"repo": "vinesmsuic/magicbrush-jul7", "revision": "105e768cb4167881474aee9b94e7db9c0b4a371d",
             "name": "safety_checker/model.safetensors", "folder": "magicbrush", "bytes": 1215981832,
             "sha256": "11cfe53105625af8c00faac32a430626641cce686454f3c39d837f14397d858b"},
            {"repo": "vinesmsuic/magicbrush-jul7", "revision": "105e768cb4167881474aee9b94e7db9c0b4a371d",
             "name": "scheduler/scheduler_config.json", "folder": "magicbrush", "bytes": 374,
             "sha256": "81cd31340529ea63278abe02f6b319059ca608fa22ff78969db8647c46696893"},
            {"repo": "vinesmsuic/magicbrush-jul7", "revision": "105e768cb4167881474aee9b94e7db9c0b4a371d",
             "name": "text_encoder/config.json", "folder": "magicbrush", "bytes": 612,
             "sha256": "bda028356343598b79afd82c2b31c9d52c049988044709f86e5266eb9db3d050"},
            {"repo": "vinesmsuic/magicbrush-jul7", "revision": "105e768cb4167881474aee9b94e7db9c0b4a371d",
             "name": "text_encoder/model.safetensors", "folder": "magicbrush", "bytes": 492265880,
             "sha256": "71c10601ece1342fede0300fb88db71c09ece8be491b71875d18b9799a5e6c15"},
            {"repo": "vinesmsuic/magicbrush-jul7", "revision": "105e768cb4167881474aee9b94e7db9c0b4a371d",
             "name": "tokenizer/merges.txt", "folder": "magicbrush", "bytes": 524619,
             "sha256": "9fd691f7c8039210e0fced15865466c65820d09b63988b0174bfe25de299051a"},
            {"repo": "vinesmsuic/magicbrush-jul7", "revision": "105e768cb4167881474aee9b94e7db9c0b4a371d",
             "name": "tokenizer/special_tokens_map.json", "folder": "magicbrush", "bytes": 472,
             "sha256": "c4864a9376a8401918425bed71fc14fc0e81f9b59ec45c1cf96cccb2df508eac"},
            {"repo": "vinesmsuic/magicbrush-jul7", "revision": "105e768cb4167881474aee9b94e7db9c0b4a371d",
             "name": "tokenizer/tokenizer_config.json", "folder": "magicbrush", "bytes": 737,
             "sha256": "19d7b034cb0cc3ce9766c2231373ab8aa8991fc72e2c8f76558bfaae3de0d563"},
            {"repo": "vinesmsuic/magicbrush-jul7", "revision": "105e768cb4167881474aee9b94e7db9c0b4a371d",
             "name": "tokenizer/vocab.json", "folder": "magicbrush", "bytes": 1059962,
             "sha256": "e089ad92ba36837a0d31433e555c8f45fe601ab5c221d4f607ded32d9f7a4349"},
            {"repo": "vinesmsuic/magicbrush-jul7", "revision": "105e768cb4167881474aee9b94e7db9c0b4a371d",
             "name": "unet/config.json", "folder": "magicbrush", "bytes": 1682,
             "sha256": "6ed52516c5fc8bcc5bff2ef3b702fc46bfdd60b07fd64df08a641e8ca2a66756"},
            {"repo": "vinesmsuic/magicbrush-jul7", "revision": "105e768cb4167881474aee9b94e7db9c0b4a371d",
             "name": "unet/diffusion_pytorch_model.safetensors", "folder": "magicbrush", "bytes": 3438213624,
             "sha256": "8db5a11ceeee6bfce999b51bba8d300ad8e9d4b22e6b6d0e7a3ef3ac3417ae41"},
            {"repo": "vinesmsuic/magicbrush-jul7", "revision": "105e768cb4167881474aee9b94e7db9c0b4a371d",
             "name": "vae/config.json", "folder": "magicbrush", "bytes": 577,
             "sha256": "691194995c7e9bfe261fc41d3d4a1920f2617005aa3510934e6fb46bedef044e"},
            {"repo": "vinesmsuic/magicbrush-jul7", "revision": "105e768cb4167881474aee9b94e7db9c0b4a371d",
             "name": "vae/diffusion_pytorch_model.safetensors", "folder": "magicbrush", "bytes": 334643268,
             "sha256": "b4d2b5932bb4151e54e694fd31ccf51fca908223c9485bd56cd0e1d83ad94c49"},
        ],
    },
    "segmentation": {
        "label": said("Background removal (RMBG-1.4)"),
        "used_for": said("Remove or blur the background in the Playground."),
        "licence": "BRIA RMBG-1.4 community licence - free for non-commercial use; see the model card",
        "source": "Hugging Face briaai/RMBG-1.4",
        "runs_on": said("Graphics card through DirectML when available, otherwise CPU"),
        "requires": ["onnxruntime"],
        "settings": {"segmentation_model": "rmbg-1.4/onnx/model.onnx"},
        "files": [
            # `name` is the path *inside the repository* and `folder` is where
            # it lands locally, so the two together have to spell both. Split
            # as "rmbg-1.4/onnx" + "model.onnx" they spelled the right local
            # path and the wrong URL — the file lives at `onnx/model.onnx` in
            # the repository — and every attempt to install background removal
            # 404ed. The local path is unchanged, which is what
            # `settings.segmentation_model` above points at.
            {"repo": "briaai/RMBG-1.4", "revision": "2ceba5a5efaec153162aedea169f76caf9b46cf8",
             "name": "onnx/model.onnx", "folder": "rmbg-1.4", "bytes": 176153355,
             "sha256": "8cafcf770b06757c4eaced21b1a88e57fd2b66de01b8045f35f01535ba742e0f"},
        ],
    },
}


def file_path(entry: dict[str, Any]) -> Path:
    return models_root() / entry["folder"] / entry["name"]


def settings_path() -> Path:
    return models_root() / "settings.json"


def write_settings(path: Path, data: dict[str, Any]) -> None:
    """``settings.json`` written owner-only and renamed into place.

    The file holds the model paths, which are nobody's secret. Until the
    audit of 10 October 2026 the Gemini extension kept the key typed into the
    console here too; it lives in the state folder now (``gemini.json``,
    extensions/gemini), and the extension takes a copy left here out the first
    time it reads one. Every write is still made 0600 from the start: a copy
    of the key may sit here until that first read, and a models folder on a
    shared disk deserves no less for the paths either. Created under a
    temporary name, exclusively (a leftover or a planted link at that name is
    removed rather than reused), then renamed over the real one, so there is
    no moment it is half-written. On Windows the mode is ignored and the
    folder's own permissions decide, as they always have.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".tmp")
    with create_new(partial) as handle:
        handle.write(json.dumps(data, indent=2).encode("utf-8"))
    os.replace(partial, path)


def apply_settings(model_id: str) -> None:
    """Point the runtime at a model that has just finished downloading.

    Downloading the bytes is only half of an install: the editing and
    segmentation code reads the path it should use from ``.ai-models/settings.json``
    (``tools/setup_ai_models.py`` writes the same file). Without this, a model
    fetched from the admin console would sit on disk and still report itself as
    not installed. Models with nothing to configure declare no ``settings``.
    """
    wanted = MODELS[model_id].get("settings")
    if not wanted:
        return
    target = settings_path()
    try:
        current = json.loads(target.read_text()) if target.is_file() else {}
        if not isinstance(current, dict):
            current = {}
    except (OSError, ValueError):
        current = {}
    for name, relative in wanted.items():
        current[name] = str(models_root() / relative)
    write_settings(target, current)


def ensure_settings() -> list[str]:
    """Point the runtime at every installed model whose setting has gone astray.

    Two things decide whether a model works, and they can disagree.
    :func:`installed` asks whether the files are on disk; the code that runs
    the model asks ``settings.json`` where they are. When the files are there
    and the setting is not, the AI models page says "installed", refuses to
    download it again because it is installed, and the Playground says the
    feature is unavailable — with nothing on either page that gets out of it.

    Two ordinary things cause that. ``settings.json`` is shared with
    ``tools/setup_ai_models.py`` and can lose a key to it; and it stores
    *absolute* paths, so a Ninaivu moved to another folder or another machine
    keeps pointing at where the models used to be. Run on every start, this
    repairs both: a setting that is missing, or that names a file that is not
    there, is pointed back at the model where it actually is. A setting that
    names a real file is left alone, even if it is not the catalogue's — that
    is somebody's deliberate choice.

    Returns the models it repaired.
    """
    target = settings_path()
    try:
        current = json.loads(target.read_text()) if target.is_file() else {}
        if not isinstance(current, dict):
            current = {}
    except (OSError, ValueError):
        current = {}

    repaired = []
    for model_id, model in MODELS.items():
        wanted = model.get("settings")
        if not wanted or not installed(model_id):
            continue
        for name, relative in wanted.items():
            stored = current.get(name)
            if stored and Path(stored).exists():
                continue
            current[name] = str(models_root() / relative)
            if model_id not in repaired:
                repaired.append(model_id)

    if repaired:
        write_settings(target, current)
    return repaired


def installed(model_id: str) -> bool:
    """Every file present at its expected size. (Checksums are checked on download.)"""
    model = MODELS[model_id]
    return all(file_path(f).is_file() and file_path(f).stat().st_size == f["bytes"]
               for f in model["files"])


def total_bytes(model_id: str) -> int:
    return sum(f["bytes"] for f in MODELS[model_id]["files"])


def missing_packages(model_id: str) -> list[str]:
    import importlib.util
    return [name for name in MODELS[model_id]["requires"] if importlib.util.find_spec(name) is None]


# ---------------------------------------------------------------------------
# Downloading
# ---------------------------------------------------------------------------

class ChecksumMismatch(ValueError):
    pass


def download_file(entry: dict[str, Any], on_bytes: Callable[[int], None] | None = None,
                  opener: Callable[..., Any] = urllib.request.urlopen) -> Path:
    """Fetch one pinned file; publish it only if its SHA-256 matches."""
    target = file_path(entry)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Most models are a repository and a revision; the ones Ninaivu has always
    # fetched itself — the face detector, the recogniser, the orientation
    # model — are a plain pinned URL. Both are a name, a size and a checksum,
    # which is all the rest of this cares about.
    url = entry.get("url") or HF.format(
        repo=entry["repo"], revision=entry["revision"], name=entry["name"])
    digest = hashlib.sha256()
    pending = None
    try:
        # The basename, not the whole `name`: that field is a path inside the
        # repository, and every model with its files in subfolders — the
        # editing model keeps all of them there — turned it into a prefix with
        # a separator in it, which is not a filename any platform will make.
        with tempfile.NamedTemporaryFile(dir=target.parent,
                                         prefix=f".{Path(entry['name']).name}.",
                                         suffix=".part", delete=False) as output:
            pending = Path(output.name)
            request = urllib.request.Request(url, headers={"User-Agent": "Ninaivu model setup"})
            with opener(request, timeout=60) as response:
                while chunk := response.read(1 << 20):
                    output.write(chunk)
                    digest.update(chunk)
                    if on_bytes:
                        on_bytes(len(chunk))
        if digest.hexdigest() != entry["sha256"]:
            raise ChecksumMismatch(f"{entry['name']} did not match its pinned checksum; it was discarded")
        os.replace(pending, target)
        pending = None
        return target
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


class Downloads:
    """At most one download per model at a time, with progress the console polls."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[str, dict[str, Any]] = {}

    def state(self, model_id: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._state.get(model_id, {}))

    def active(self) -> dict[str, dict[str, Any]]:
        """Every model downloading right now, for the activity strip."""
        with self._lock:
            return {model_id: dict(state)
                    for model_id, state in self._state.items()
                    if state.get("status") == "downloading"}

    def start(self, model_id: str, opener: Callable[..., Any] = urllib.request.urlopen,
              on_done: Callable[[str], None] | None = None,
              on_end: Callable[[str], None] | None = None,
              force: bool = False) -> bool:
        """Begin in the background. False if it is already downloading.

        *on_done* hears about an installed model; *on_end* about any ending,
        installed or failed — not about the process stopping part way.

        *force* fetches every file again even when one of the right size is
        already there. That is what "Download again" means: a model that
        loads but behaves oddly is usually a file that is the right length
        and the wrong bytes, and the only way out was deleting it by hand.
        """
        if model_id not in MODELS:
            raise KeyError(model_id)
        with self._lock:
            if self._state.get(model_id, {}).get("status") == "downloading":
                return False
            if installed(model_id) and not force:
                return False
            self._state[model_id] = {"status": "downloading", "done_bytes": 0,
                                     "total_bytes": total_bytes(model_id), "error": "",
                                     "started_at": time.time()}

        def progress(count: int) -> None:
            with self._lock:
                self._state[model_id]["done_bytes"] += count

        def run() -> None:
            try:
                for entry in MODELS[model_id]["files"]:
                    target = file_path(entry)
                    if (not force and target.is_file()
                            and target.stat().st_size == entry["bytes"]):
                        progress(entry["bytes"])
                        continue
                    download_file(entry, progress, opener)
                apply_settings(model_id)
                clear_superseded(model_id)
                outcome = {"status": "installed", "error": ""}
                if on_done:
                    on_done(model_id)
            except Exception as error:                  # noqa: BLE001 - reported to the console
                log.warning("could not download %s: %s", model_id, error)
                outcome = {"status": "failed", "error": str(error)[:300]}
            with self._lock:
                self._state[model_id].update(outcome, finished_at=time.time())
            if on_end:
                try:
                    on_end(model_id)
                except Exception:                           # noqa: BLE001
                    pass

        threading.Thread(target=run, name=f"model-download-{model_id}", daemon=True).start()
        return True


downloads = Downloads()


# ---------------------------------------------------------------------------
# Versions
#
# A model is pinned by checksum, so a newer one is a *new set of files* rather
# than the same names fetched again — and `installed` already checks that
# every file the catalogue names is present at its pinned size. So a model
# that reports itself installed is, by definition, at the catalogue's version;
# there is nothing to record and nothing to compare.
#
# What that leaves is the previous version's files, still on disk and still
# the right size, and in the case of the orientation model still the one being
# loaded. `replaces` names them: their presence beside a model that is *not*
# installed is what makes this an update rather than a first download, and
# they are removed once the new files are safely in place.
# ---------------------------------------------------------------------------

def version_of(model_id: str) -> int:
    return int(MODELS[model_id].get("version", 1))


def superseded(model_id: str) -> list[Path]:
    """Files an older version of this model left behind, that are still there."""
    root = models_root()
    return [path for name in MODELS[model_id].get("replaces", ())
            if (path := root / name).is_file()]


def update_available(model_id: str) -> bool:
    """Is this a newer version of something the household already had?

    Deliberately not a recorded version number. The first cut of this wrote
    the installed version into settings.json and assumed 1 for anything
    installed before that existed — which offered every household an 80 MB
    "update" to the orientation model they already had.
    """
    return not installed(model_id) and bool(superseded(model_id))


def clear_superseded(model_id: str) -> list[str]:
    """Remove the previous version's files. Only ever after a good install."""
    if not installed(model_id):
        return []
    gone = []
    for path in superseded(model_id):
        try:
            path.unlink()
            gone.append(path.name)
        except OSError as exc:
            log.warning("could not remove the superseded %s: %s", path.name, exc)
    if gone:
        log.info("removed the previous %s: %s", model_id, ", ".join(gone))
    return gone


def describe(model_id: str) -> dict[str, Any]:
    model = MODELS[model_id]
    return {
        "id": model_id, "label": model["label"], "used_for": model["used_for"],
        "licence": model["licence"], "source": model["source"], "runs_on": model["runs_on"],
        "bytes": total_bytes(model_id), "installed": installed(model_id),
        "missing_packages": missing_packages(model_id),
        "download": downloads.state(model_id),
        "essential": bool(model.get("essential")),
        "version": version_of(model_id),
        # Installed means every pinned file is there at its pinned size, so an
        # installed model is at the catalogue's version by definition.
        "installed_version": version_of(model_id) if installed(model_id) else 0,
        "update_available": update_available(model_id),
    }
