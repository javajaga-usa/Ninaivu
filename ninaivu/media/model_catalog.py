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

import contextlib
import hashlib
import json
import logging
import os
import shutil
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

#: The speech detector both speech models use, one file shared between them.
_SILERO_VAD: dict[str, Any] = {
    "url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx",
    "name": "silero_vad.onnx", "folder": "speech", "bytes": 643854,
    "sha256": "9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6",
}

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
    # InsightFace's pair, for the `face_model` setting. One archive from the
    # project's GitHub release, pinned whole; only the detector and the
    # recogniser are taken out of it (the pack also holds a landmark model,
    # a 3D one and an age-and-gender one, none of which Ninaivu runs).
    "faces-arcface": {
        "label": said("Finding and grouping faces, more accurate (ArcFace)"),
        "used_for": said("The People page with InsightFace's SCRFD detector and ArcFace recogniser instead of YuNet and SFace: better at telling relatives apart and at a child's face over the years. Once downloaded, choose arcface for face_model on the console's Advanced settings page; the next scan looks at every photograph again and names already confirmed are kept."),
        "licence": "InsightFace model zoo - non-commercial research use only; for a household's own library",
        "source": "GitHub deepinsight/insightface release v0.7, buffalo_l (SCRFD-10G and ArcFace ResNet-50, WebFace600K)",
        "runs_on": "CPU",
        "requires": ["onnxruntime"],
        "version": 1,
        "files": [
            {"url": ("https://github.com/deepinsight/insightface/releases/download/"
                     "v0.7/buffalo_l.zip"),
             "name": "buffalo_l.zip", "folder": "buffalo_l",
             "bytes": 288621354,
             "sha256": "80ffe37d8a5940d59a7384c201a2a38d4741f2f3c51eef46ebb28218a7b0ca2f",
             "take": {
                 "det_10g.onnx": {"member": "det_10g.onnx", "bytes": 16923827},
                 "w600k_r50.onnx": {"member": "w600k_r50.onnx", "bytes": 174383860},
             }},
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
    # DDColor for black-and-white photographs: "Add colour" in the viewer
    # (colour_photos) and the Playground's Colourise. FaceFusion publishes
    # the ONNX export on GitHub (Hugging Face cannot be reached to pin from
    # where this is built); the file is the one a hand-placed model was
    # always looked for under (media/onnx_tools.py), so either works.
    "colour": {
        "label": said("Colour for black-and-white photographs (DDColor)"),
        "used_for": said("Add colour in the viewer of a black-and-white photograph, kept as a copy beside the original, and Colourise in the Playground. About 1 GB on disk."),
        "licence": "Apache-2.0 (DDColor); FaceFusion's ONNX export, published with its assets",
        "source": "GitHub facefusion/facefusion-assets release models-3.0.0, ddcolor.onnx",
        "runs_on": "CPU",
        "requires": ["onnxruntime"],
        "files": [
            {"url": ("https://github.com/facefusion/facefusion-assets/releases/download/"
                     "models-3.0.0/ddcolor.onnx"),
             "name": "ddcolor.onnx", "folder": "onnx", "bytes": 980103562,
             "sha256": "dd6156f056d0690891ef35f9ee87176d4d976ab19bbd36e57d18e841e064d797"},
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
    # The face-parsing network the Photo Studio's Skin and Hair tools use where
    # colour cannot tell (media/face_parser.py): black hair against a black
    # wall, a face half in shadow. Optional — without it they work as they
    # did. A BiSeNet on a ResNet-34, trained on CelebAMask-HQ's nineteen
    # labels; the project publishes it on GitHub, MIT, as an ONNX export run
    # by OpenCV, so nothing new is installed for it. The file is saved under
    # the name face_parser.py has always looked for.
    "faceparse": {
        "label": said("Skin and hair, more exact (face parsing)"),
        "used_for": said("The Photo Studio's Skin and Hair tools — finds skin and hair where colour alone cannot, such as dark hair against a dark background. Without it they use the built-in method. About 94 MB on disk."),
        "licence": "MIT (yakhyo/face-parsing); trained on CelebAMask-HQ, whose images are for non-commercial research - for a household's own library",
        "source": "GitHub yakhyo/face-parsing release v0.0.2, resnet34.onnx (BiSeNet, ResNet-34)",
        "runs_on": "CPU",
        "requires": ["cv2"],
        "files": [
            {"url": ("https://github.com/yakhyo/face-parsing/releases/download/"
                     "v0.0.2/resnet34.onnx"),
             "name": "face_parsing.onnx", "folder": "faceparse", "bytes": 93632554,
             "sha256": "5b805bba7b5660ab7070b5a381dcf75e5b3e04199f1e9387232a77a00095102e"},
        ],
    },
    # The on-device AI engine's image models (ninaivu/runtime/siglip.py and
    # florence.py), run by ONNX Runtime in the model host: no PyTorch.
    #
    # Search is SigLIP 2 B/16 exactly as the PyTorch engine runs it, in the
    # ONNX export Immich publishes of open_clip's model (its vectors are
    # open_clip's, so a library indexed with SigLIP 2 is not indexed again).
    # The tokenizer is byte for byte the one in "siglip2" above.
    "search-onnx": {
        "label": said("AI search without PyTorch (SigLIP 2, ViT-B/16)"),
        "used_for": said("Finding photos by what is in them, tags and the explicit-content check, run by the on-device AI engine (Advanced settings) instead of PyTorch. The same model as AI search above, so a library already indexed with it is not indexed again. About 1.5 GB on disk."),
        "licence": "Apache-2.0 (SigLIP 2); the ONNX export's repository states no licence of its own",
        "source": "Hugging Face immich-app/ViT-B-16-SigLIP2__webli",
        "runs_on": said("The processor on a Mac for now (Core ML is being checked against it), the graphics card through DirectML on Windows, otherwise the processor"),
        "requires": ["onnxruntime", "tokenizers"],
        "files": [
            {"repo": "immich-app/ViT-B-16-SigLIP2__webli",
             "revision": "5f3fc29104ebcf51e9d893af57604badd51a3ea6",
             "name": name, "folder": "siglip2-onnx", "bytes": size, "sha256": digest}
            for name, size, digest in (
                ("visual/model.onnx", 371674292,
                 "fee10c729875dd203d94f396ac3d664e301d39c977ef6d92e7f467a2a716f0b0"),
                ("textual/model.onnx", 1129435819,
                 "fc9991b415d7d0fac7ed540e875fd08a647c8a1abba99f12542531b004c4b68f"),
                ("textual/tokenizer.json", 34362885,
                 "220c63d496e0c14e63eb656c91e0215e926202e4c74b1f089e09f1920d779b04"),
            )
        ],
    },
    # Florence-2 base (fine-tuned) in the transformers.js project's ONNX
    # export: the image encoder at full precision, for the accelerators, and
    # the three text pieces at 8 bits, for the processor. The tokenizer is
    # not stored in Git LFS there, so Hugging Face lists no SHA-256 for it;
    # it is pinned by its Git blob id (SHA-1 over "blob <size>\0" and the
    # bytes) at the pinned revision instead, which is how Git itself names it.
    "captions": {
        "label": said("A description of every photo (Florence-2 base)"),
        "used_for": said("Writes a sentence or two about what is in each photo, so photos can be found by it and read about. Run by the on-device AI engine (Advanced settings) on the gallery's previews, in the background. About 550 MB on disk."),
        "licence": "MIT (Florence-2; the transformers.js ONNX export, MIT)",
        "source": "Hugging Face onnx-community/Florence-2-base-ft",
        "runs_on": said("The processor on a Mac for now (Core ML is being checked against it), the image half on the graphics card on Windows"),
        "requires": ["onnxruntime", "tokenizers"],
        "files": [
            {"repo": "onnx-community/Florence-2-base-ft",
             "revision": "e88a44eaf3791a35eae0c5a47b3dbcd36e67eb6f",
             "name": name, "folder": "florence-2-base", "bytes": size, **pin}
            for name, size, pin in (
                ("onnx/vision_encoder.onnx", 366549825,
                 {"sha256": "d67258cdfdebfa21285dad9e7bd4bd99725236d0aaef9e474a1b24a6ec471351"}),
                ("onnx/embed_tokens_int8.onnx", 39390433,
                 {"sha256": "6b2258db1c8ee9b160576ccde3cd3814d83a2edaed0dd1c6ca9ff3c38fa62214"}),
                ("onnx/encoder_model_int8.onnx", 43651493,
                 {"sha256": "f4ad7a68f1fb875d3bcf735ea14a7021b7ba7e83baf7cf10289881b4ed6d9b85"}),
                ("onnx/decoder_model_merged_int8.onnx", 98177697,
                 {"sha256": "f22f52f980c33df0efa15932c2f3db6d9d3595ce6387eca938b8cfe23dc4c641"}),
                ("tokenizer.json", 2297961,
                 {"git_sha1": "c6919f318f277e660b557212ed1edaf28b037fc8"}),
            )
        ],
    },
    # Florence-2 large (fine-tuned), the same export at full precision: three
    # times the base model's size and markedly better descriptions (more of
    # what is there, fewer things that are not). For a computer with 24 GB
    # of memory or more (runtime/florence.py LARGE_FROM_GB), which the AI
    # models page suggests it for; used instead of base whenever it is
    # downloaded. The tokenizer is the base model's, blob for blob.
    "captions-large": {
        "label": said("A description of every photo, more detailed (Florence-2 large)"),
        "used_for": said("Writes a sentence or two about what is in each photo, like the description model above but noticeably better, for a computer with 24 GB of memory or more. Used instead of it once downloaded; photos it has not described yet are described again. About 3.3 GB on disk, about 4 GB of memory while it works."),
        "licence": "MIT (Florence-2; the transformers.js ONNX export, MIT)",
        "source": "Hugging Face onnx-community/Florence-2-large-ft",
        "runs_on": said("The processor on a Mac for now (Core ML is being checked against it), the image half on the graphics card on Windows"),
        "requires": ["onnxruntime", "tokenizers"],
        "files": [
            {"repo": "onnx-community/Florence-2-large-ft",
             "revision": "04ace74913c28d7ec94af32cee5c111a10126b6f",
             "name": name, "folder": "florence-2-large", "bytes": size, **pin}
            for name, size, pin in (
                ("onnx/vision_encoder.onnx", 1453500335,
                 {"sha256": "cbe2b9e3bfd5f00f9c135c3c48e4e8a2ad2b35651309d213dbf0803cc862d491"}),
                ("onnx/embed_tokens.onnx", 210080043,
                 {"sha256": "8497716365d7636307b334a665b0fe3482c9dfc3cc3e31c1d110c25ec183e31d"}),
                ("onnx/encoder_model.onnx", 609049107,
                 {"sha256": "f4622b5203e353c0e660f8d2ccd50a1179a092cd2408b208bc9539f1572a321d"}),
                ("onnx/decoder_model_merged.onnx", 1021996421,
                 {"sha256": "a35016ed99f4260f584bdef0602617cf344dafe8d72631d2a7e5933f105f4ea6"}),
                ("tokenizer.json", 2297961,
                 {"git_sha1": "c6919f318f277e660b557212ed1edaf28b037fc8"}),
            )
        ],
    },
    # The on-device AI engine's language model (ninaivu/runtime/llm.py),
    # one for everything that writes: the chapters of a memory film now, Ask
    # Ninaivu and the interview mode next. Gemma 3 12B, instruction-tuned and
    # trained for 4-bit use (QAT), as llama.cpp's own project republishes it:
    # one GGUF file, not gated, so it is fetched like any other pinned file.
    # It writes Tamil far better than the small models of its size, and its
    # 7 GB sit comfortably beside the rest on a 48 GB Mac. On 16 GB it runs
    # only with the other models not loaded (admin guide, AI engine). The
    # image half (mmproj) is not taken.
    "text-model": {
        "label": said("Writing in Tamil and English (Gemma 3 12B)"),
        "used_for": said("The on-device AI engine's language model: drafts the chapters and titles of a memory film from what is in its photographs, and is what Ask Ninaivu will answer with. Runs on this computer, on the graphics processor of a Mac. About 7.1 GB on disk and 9 GB of memory while it works."),
        "licence": "Gemma Terms of Use (Google): free to use, with Google's use policy; the GGUF by ggml-org",
        "source": "Hugging Face ggml-org/gemma-3-12b-it-qat-GGUF",
        "runs_on": said("The graphics processor on a Mac (Metal), otherwise the processor"),
        "requires": ["llama_cpp"],
        "files": [
            {"repo": "ggml-org/gemma-3-12b-it-qat-GGUF",
             "revision": "05c2df468ad7a0bb1284b3d6fe2bdf495a885567",
             "name": "gemma-3-12b-it-qat-Q4_0.gguf", "folder": "gemma-3-12b",
             "bytes": 7131017792,
             "sha256": "7fffee00e76e4b75dc6f9acca43b991d181f2b9483307564938fd4b391aad364"},
        ],
    },
    # The on-device AI engine's speech models (ninaivu/runtime/speech.py):
    # Whisper exported to ONNX and quantised to 8 bits by the sherpa-onnx
    # project, which publishes them on GitHub as one archive per size. Each
    # archive is pinned like any other file; only the files named in `take`
    # are unpacked from it, and the archive itself is not kept. The speech
    # detector beside them finds where somebody is talking, so silence is not
    # handed to Whisper (which invents words for it) and a long story is cut
    # at pauses rather than mid-word.
    "speech": {
        "label": said("Voice stories as words (Whisper large-v3)"),
        "used_for": said("Writes down what is said in each voice story, in Tamil or English, with an English translation of a Tamil story, so stories can be read and searched. Used by the on-device AI engine (Advanced settings). About 1.8 GB on disk."),
        "licence": "MIT (Whisper; sherpa-onnx's ONNX export, Apache-2.0); Silero VAD MIT",
        "source": "k2-fsa/sherpa-onnx releases — sherpa-onnx-whisper-large-v3, silero_vad",
        "runs_on": "CPU",
        "requires": ["sherpa_onnx"],
        "files": [
            {"url": ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
                     "asr-models/sherpa-onnx-whisper-large-v3.tar.bz2"),
             "name": "sherpa-onnx-whisper-large-v3.tar.bz2", "folder": "whisper-large-v3",
             "bytes": 1068482488,
             "sha256": "2d0e134b3b5fc4a0533baf24a0c9d473b629aa47f030af0a165a05f461df7a03",
             "take": {
                 "encoder.int8.onnx": {"member": "sherpa-onnx-whisper-large-v3/large-v3-encoder.int8.onnx",
                                       "bytes": 766671985},
                 "decoder.int8.onnx": {"member": "sherpa-onnx-whisper-large-v3/large-v3-decoder.int8.onnx",
                                       "bytes": 1008265203},
                 "tokens.txt": {"member": "sherpa-onnx-whisper-large-v3/large-v3-tokens.txt",
                                "bytes": 816730},
             }},
            _SILERO_VAD,
        ],
    },
    "speech-quick": {
        "label": said("Voice stories as words, quicker (Whisper turbo)"),
        "used_for": said("Writes down what is said in each voice story, in Tamil or English, several times faster than large-v3 but with no English translation of a Tamil story. Used only when large-v3 is not downloaded. About 1 GB on disk."),
        "licence": "MIT (Whisper; sherpa-onnx's ONNX export, Apache-2.0); Silero VAD MIT",
        "source": "k2-fsa/sherpa-onnx releases — sherpa-onnx-whisper-turbo, silero_vad",
        "runs_on": "CPU",
        "requires": ["sherpa_onnx"],
        "files": [
            {"url": ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
                     "asr-models/sherpa-onnx-whisper-turbo.tar.bz2"),
             "name": "sherpa-onnx-whisper-turbo.tar.bz2", "folder": "whisper-turbo",
             "bytes": 563790207,
             "sha256": "b11acbbcd660b44a8e0df33724feb5aaa709cf65668f2823d59f656312544f22",
             "take": {
                 "encoder.int8.onnx": {"member": "sherpa-onnx-whisper-turbo/turbo-encoder.int8.onnx",
                                       "bytes": 674716297},
                 "decoder.int8.onnx": {"member": "sherpa-onnx-whisper-turbo/turbo-decoder.int8.onnx",
                                       "bytes": 361080764},
                 "tokens.txt": {"member": "sherpa-onnx-whisper-turbo/turbo-tokens.txt",
                                "bytes": 816730},
             }},
            _SILERO_VAD,
        ],
    },
    # The voices that read a letter aloud (``letters_aloud``,
    # runtime/voice.py): Meta's MMS text-to-speech, one small model per
    # language, each reading its own script. The voices are the models' own,
    # never anybody in the family's. English is the ONNX export sherpa-onnx
    # publishes on GitHub, run by the package voice stories already use.
    "voice-english": {
        "label": said("Letters read aloud in English (MMS voice)"),
        "used_for": said("Reads the words of an old letter or recipe card aloud in a calm English voice, from Read aloud in the viewer, when Letters read aloud is switched on (Advanced settings). Also reads the English words in a Tamil letter. About 115 MB on disk."),
        "licence": "CC-BY-NC 4.0 (Meta MMS): non-commercial use, which a household's own library is; sherpa-onnx's ONNX export, Apache-2.0",
        "source": "k2-fsa/sherpa-onnx releases — vits-mms-eng (facebook/mms-tts-eng)",
        "runs_on": "CPU",
        "requires": ["sherpa_onnx"],
        "files": [
            {"url": ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
                     "tts-models/vits-mms-eng.tar.bz2"),
             "name": "vits-mms-eng.tar.bz2", "folder": "mms-tts-english",
             "bytes": 107737708,
             "sha256": "8712cb52f71ee00bde27b8c18058d97a794fccf873c4629fbea0de87d31366b4",
             "take": {
                 "model.onnx": {"member": "vits-mms-eng/model.onnx", "bytes": 114016948},
                 "tokens.txt": {"member": "vits-mms-eng/tokens.txt", "bytes": 303},
             }},
        ],
    },
}


def file_path(entry: dict[str, Any]) -> Path:
    return models_root() / entry["folder"] / entry["name"]


def kept_files(entry: dict[str, Any]) -> list[tuple[Path, int]]:
    """What one catalogue entry leaves on disk, each with its size.

    A plain file is itself. An archive (an entry with ``take``) is not kept
    once it is unpacked: what stays is the files taken out of it, each under
    the name ``take`` gives it, in the entry's folder.
    """
    if "take" in entry:
        folder = models_root() / entry["folder"]
        return [(folder / name, int(spec["bytes"])) for name, spec in entry["take"].items()]
    return [(file_path(entry), int(entry["bytes"]))]


def entry_present(entry: dict[str, Any]) -> bool:
    """Every file this entry leaves is on disk at its pinned size."""
    return all(path.is_file() and path.stat().st_size == size
               for path, size in kept_files(entry))


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
    return all(entry_present(f) for f in MODELS[model_id]["files"])


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


def _hasher(entry: dict[str, Any]) -> tuple[Any, str]:
    """The hash a download is checked with, and the value it must come to.

    A SHA-256 wherever one is published. A small file a Hugging Face
    repository keeps in Git rather than Git LFS has none listed, only its Git
    blob id, which is a SHA-1 over a ``blob <size>\\0`` header and the bytes;
    the header is fed first here, so the same comparison serves.
    """
    if entry.get("sha256"):
        return hashlib.sha256(), str(entry["sha256"])
    digest = hashlib.sha1(usedforsecurity=True)
    digest.update(b"blob %d\0" % int(entry["bytes"]))
    return digest, str(entry["git_sha1"])


#: A partial download not written to for this long was cut off (a restart,
#: a crash): nothing will finish it, and Gemma's left up to 7 GB each time.
PARTIAL_STALE_SECONDS = 3600


#: Left free on the disk after a model is downloaded.
KEEP_FREE_BYTES = 1024 ** 3


def _clear_partials(folder: Path, base: str, now: float | None = None) -> int:
    """Delete this file's own partial downloads left behind (audit C-17).
    Only names :func:`download_file` makes, and only ones not written to for
    :data:`PARTIAL_STALE_SECONDS`, so a download running beside this one is
    left alone. Returns how many went."""
    import glob                                           # noqa: PLC0415
    import time                                           # noqa: PLC0415
    now = time.time() if now is None else now
    gone = 0
    for leftover in folder.glob(f".{glob.escape(base)}.*.part"):
        try:
            if now - leftover.stat().st_mtime > PARTIAL_STALE_SECONDS:
                leftover.unlink()
                gone += 1
        except OSError:
            continue
    if gone:
        log.info("removed %d partial download(s) of %s left from before", gone, base)
    return gone


def download_file(entry: dict[str, Any], on_bytes: Callable[[int], None] | None = None,
                  opener: Callable[..., Any] = urllib.request.urlopen) -> Path:
    """Fetch one pinned file; publish it only if its SHA-256 matches.

    For an archive (an entry with ``take``), what is published is the files
    taken out of it (:func:`_unpack`), and the folder they are in is returned.
    """
    target = file_path(entry)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Most models are a repository and a revision; the ones Ninaivu has always
    # fetched itself — the face detector, the recogniser, the orientation
    # model — are a plain pinned URL. Both are a name, a size and a checksum,
    # which is all the rest of this cares about.
    url = entry.get("url") or HF.format(
        repo=entry["repo"], revision=entry["revision"], name=entry["name"])
    digest, pinned = _hasher(entry)
    _clear_partials(target.parent, Path(entry["name"]).name)
    expected = int(entry.get("bytes") or 0)
    # The download and, for an archive, what is taken out of it, side by side,
    # with room left over: the writing model is 7.1 GB (audit C-34).
    needed = expected + sum(int(spec.get("bytes") or 0)
                            for spec in (entry.get("take") or {}).values()) + KEEP_FREE_BYTES
    if expected and shutil.disk_usage(target.parent).free < needed:
        raise OSError("There is not enough free space for this model. Free some space and try again.")
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
                written = 0
                while chunk := response.read(1 << 20):
                    written += len(chunk)
                    if expected and written > expected:
                        # More than the pinned size can never match: stop
                        # now rather than fill the disk first (audit C-34).
                        raise ChecksumMismatch(f"{entry['name']} was larger than its pinned "
                                               f"size; it was discarded")
                    output.write(chunk)
                    digest.update(chunk)
                    if on_bytes:
                        on_bytes(len(chunk))
        if digest.hexdigest() != pinned:
            raise ChecksumMismatch(f"{entry['name']} did not match its pinned checksum; it was discarded")
        if "take" in entry:
            _unpack(entry, pending)
            return target.parent
        os.replace(pending, target)
        pending = None
        return target
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


def _unpack(entry: dict[str, Any], archive: Path) -> None:
    """Take the files ``take`` names out of a verified archive, and nothing else.

    Only after the archive's own checksum matched. Each member is read by the
    name pinned here and written under the local name pinned here, so nothing
    in the archive decides where anything is written; a member that is
    missing, or not the size it was pinned at, fails the download rather than
    leaving a model that loads and misbehaves. Each file is written under a
    temporary name and renamed into place, so a stop half way leaves no file
    that looks whole.
    """
    folder = models_root() / entry["folder"]
    with _opened_archive(entry, archive) as members:
        for name, spec in entry["take"].items():
            source = members(spec["member"], int(spec["bytes"]))
            target = folder / name
            partial = target.with_name(f".{name}.part")
            try:
                with source, open(partial, "wb") as output:
                    while chunk := source.read(1 << 20):
                        output.write(chunk)
                os.replace(partial, target)
            finally:
                partial.unlink(missing_ok=True)


@contextlib.contextmanager
def _opened_archive(entry: dict[str, Any], archive: Path):
    """A tar (any compression) or a zip, as one ``members(name, size)`` that
    gives a readable file object for the pinned member, or refuses it."""
    import tarfile                                              # noqa: PLC0415
    import zipfile                                              # noqa: PLC0415

    name = str(entry["name"])
    if name.lower().endswith(".zip"):
        with zipfile.ZipFile(archive) as bundle:
            def members(member: str, size: int):
                try:
                    info = bundle.getinfo(member)
                except KeyError:
                    raise ChecksumMismatch(f"{member} is not in {name}") from None
                if info.is_dir() or info.file_size != size:
                    raise ChecksumMismatch(f"{member} in {name} is not the pinned file")
                return bundle.open(info)
            yield members
        return
    with tarfile.open(archive, "r:*") as bundle:
        def members(member: str, size: int):
            try:
                info = bundle.getmember(member)
            except KeyError:
                raise ChecksumMismatch(f"{member} is not in {name}") from None
            if not info.isfile() or info.size != size:
                raise ChecksumMismatch(f"{member} in {name} is not the pinned file")
            source = bundle.extractfile(info)
            if source is None:
                raise ChecksumMismatch(f"{member} in {name} cannot be read")
            return source
        yield members


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
                    if not force and entry_present(entry):
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
