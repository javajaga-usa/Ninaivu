"""Turn one photograph into a few seconds of video with Wan 2.2, on this computer.

A try-out for living photos (plan B of the memory films): how a still from
the family's library moves, and how long a clip takes here, before any of
it is wired into Ninaivu.

    python tools/try_living_photo.py photo.jpg -o photo.mp4
    python tools/try_living_photo.py photo.jpg --prompt "she smiles and the wind moves her hair"
    python tools/try_living_photo.py photo.jpg --model a14b --frames 49

Two models, both Apache-2.0, both downloaded from Hugging Face into its cache
the first time this tool is run and never otherwise:

``5b`` (the default)
    Wan 2.2 TI2V-5B, about 10 GB in half precision. Comfortable on a 48 GB
    Mac; 720p at 24 frames a second is what it was made for.

``a14b``
    Wan 2.2 I2V-A14B, the better model: two 14-billion-parameter experts,
    one for the rough motion and one for the detail. At full precision they
    are 56 GB, so each is loaded from a 4-bit GGUF file (about 9 GB each)
    and the text encoder in half precision (about 11 GB): roughly 30 GB in
    all, and several minutes a clip on Apple silicon.

Needs the editing packages (requirements/requirements-ai-editing.txt, with
diffusers 0.41 or later), ``gguf`` for the 14B model, and ffmpeg or imageio
to write the video. The photograph never leaves the computer.

The repository and file names below are the ones published in October 2026;
check the model cards (``--repo``, ``--gguf-repo``) if a download is refused.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPOS = {
    "5b": "Wan-AI/Wan2.2-TI2V-5B-Diffusers",
    "a14b": "Wan-AI/Wan2.2-I2V-A14B-Diffusers",
}
GGUF_REPO = "QuantStack/Wan2.2-I2V-A14B-GGUF"
GGUF_FILES = {
    "high": "HighNoise/Wan2.2-I2V-A14B-HighNoise-Q4_K_M.gguf",
    "low": "LowNoise/Wan2.2-I2V-A14B-LowNoise-Q4_K_M.gguf",
}
DEFAULT_PROMPT = ("A natural, gentle movement: the people breathe and blink, hair and "
                  "clothes move a little in the breeze, the camera is still.")
NEGATIVE = ("blurry, distorted faces, extra limbs, text, watermark, jerky motion, "
            "flicker, overexposed")


def pick_device():
    import torch
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("photo", type=Path)
    parser.add_argument("-o", "--out", type=Path, default=None, help="the .mp4 to write (default: beside the photo)")
    parser.add_argument("--model", choices=tuple(REPOS), default="5b")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--frames", type=int, default=49, help="frames to make (about 3 s at 16 a second)")
    parser.add_argument("--fps", type=int, default=16)
    parser.add_argument("--width", type=int, default=832)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--steps", type=int, default=30)
    parser.add_argument("--guidance", type=float, default=4.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--repo", default=None, help="the diffusers repository (default: the model's)")
    parser.add_argument("--gguf-repo", default=GGUF_REPO)
    parser.add_argument("--full", action="store_true",
                        help="load the 14B experts at full precision (needs 64 GB or more)")
    parser.add_argument("--device", default=None, help="mps, cuda or cpu (default: the best here)")
    args = parser.parse_args(argv)

    try:
        import torch
        from diffusers import WanImageToVideoPipeline
        from diffusers.utils import export_to_video
        from PIL import Image, ImageOps
    except ImportError as exc:
        print(f"Missing package: {exc}\n  pip install -r requirements/requirements-ai-editing.txt gguf imageio-ffmpeg",
              file=sys.stderr)
        return 1

    repo = args.repo or REPOS[args.model]
    device = args.device or pick_device()
    dtype = torch.bfloat16 if device != "cpu" else torch.float32
    print(f"Loading {repo} on {device}", flush=True)
    started = time.monotonic()
    extra = {}
    if args.model == "a14b" and not args.full:
        from diffusers import GGUFQuantizationConfig, WanTransformer3DModel
        from huggingface_hub import hf_hub_download
        quant = GGUFQuantizationConfig(compute_dtype=dtype)
        extra["transformer"] = WanTransformer3DModel.from_single_file(
            hf_hub_download(args.gguf_repo, GGUF_FILES["high"]), quantization_config=quant,
            torch_dtype=dtype, config=repo, subfolder="transformer")
        extra["transformer_2"] = WanTransformer3DModel.from_single_file(
            hf_hub_download(args.gguf_repo, GGUF_FILES["low"]), quantization_config=quant,
            torch_dtype=dtype, config=repo, subfolder="transformer_2")
    pipe = WanImageToVideoPipeline.from_pretrained(repo, torch_dtype=dtype, **extra)
    pipe.to(device)
    print(f"Loaded in {time.monotonic() - started:.0f} s", flush=True)

    with Image.open(args.photo) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
    # The model wants sides that are multiples of 16; the photograph is
    # fitted inside the asked-for frame, keeping its shape.
    scale = min(args.width / image.width, args.height / image.height)
    width = max(16, int(image.width * scale) // 16 * 16)
    height = max(16, int(image.height * scale) // 16 * 16)
    image = image.resize((width, height), Image.LANCZOS)
    generator = torch.Generator(device="cpu").manual_seed(args.seed)
    started = time.monotonic()
    frames = pipe(image=image, prompt=args.prompt, negative_prompt=NEGATIVE,
                  height=height, width=width, num_frames=args.frames,
                  num_inference_steps=args.steps, guidance_scale=args.guidance,
                  generator=generator).frames[0]
    took = time.monotonic() - started
    out = args.out or args.photo.with_name(f"{args.photo.stem}-living.mp4")
    export_to_video(frames, str(out), fps=args.fps)
    print(f"Wrote {out}: {len(frames)} frames at {width}x{height}, {args.fps} a second, "
          f"in {took:.0f} s ({took / max(1, args.steps):.1f} s a step)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
