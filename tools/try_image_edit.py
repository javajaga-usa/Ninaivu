"""Edit a photograph by instruction with Qwen-Image-Edit, on this computer.

A try-out, not a feature yet: for seeing what Qwen-Image-Edit makes of the
family's own photographs, and how long one edit takes, before deciding
whether it should sit beside MagicBrush in the Playground.

    python tools/try_image_edit.py photo.jpg "make it a rainy evening" -o out.png
    python tools/try_image_edit.py photo.jpg "remove the people in the background" --steps 30

Qwen-Image-Edit (Apache-2.0) keeps faces and lettering intact far better than
MagicBrush, at a price: it is a 20-billion-parameter model with a 7-billion
text encoder. At full precision that is over 50 GB, more than a 48 GB Mac
holds, so the model proper is loaded from a 4-bit GGUF file (about 12 GB)
and the text encoder in half precision (about 15 GB): roughly 30 GB in all,
and minutes per edit on Apple silicon. Everything is downloaded from Hugging
Face the first time, into its cache, and only when this tool is run; the
photograph itself never leaves the computer.

Needs the editing packages (requirements/requirements-ai-editing.txt, with
diffusers 0.41 or later for the GGUF loader and ``gguf``):

    pip install -r requirements/requirements-ai-editing.txt gguf

The repository and file names below are the ones published in October 2026;
check the model cards (``--repo``, ``--gguf-repo``, ``--gguf-file``) if a
download is refused.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

#: The pipeline (configs, VAE, text encoder, tokeniser, scheduler).
REPO = "Qwen/Qwen-Image-Edit-2509"
#: The transformer alone, quantised to 4 bits, from the community GGUF build.
GGUF_REPO = "QuantStack/Qwen-Image-Edit-2509-GGUF"
GGUF_FILE = "Qwen-Image-Edit-2509-Q4_K_M.gguf"


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
    parser.add_argument("instruction")
    parser.add_argument("-o", "--out", type=Path, default=None, help="where to write (default: beside the photo)")
    parser.add_argument("--steps", type=int, default=40)
    parser.add_argument("--guidance", type=float, default=4.0, help="true CFG scale")
    parser.add_argument("--side", type=int, default=1024, help="longer side the edit is made at")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--repo", default=REPO)
    parser.add_argument("--gguf-repo", default=GGUF_REPO)
    parser.add_argument("--gguf-file", default=GGUF_FILE)
    parser.add_argument("--full", action="store_true",
                        help="load the transformer at full precision instead of the GGUF (needs 64 GB or more)")
    parser.add_argument("--device", default=None, help="mps, cuda or cpu (default: the best here)")
    args = parser.parse_args(argv)

    try:
        import torch
        from diffusers import QwenImageEditPipeline
        from PIL import Image, ImageOps
    except ImportError as exc:
        print(f"Missing package: {exc}\n  pip install -r requirements/requirements-ai-editing.txt gguf",
              file=sys.stderr)
        return 1

    device = args.device or pick_device()
    dtype = torch.bfloat16 if device != "cpu" else torch.float32
    print(f"Loading {args.repo} on {device} ({'full precision' if args.full else args.gguf_file})",
          flush=True)
    started = time.monotonic()
    if args.full:
        pipe = QwenImageEditPipeline.from_pretrained(args.repo, torch_dtype=dtype)
    else:
        from diffusers import GGUFQuantizationConfig, QwenImageTransformer2DModel
        from huggingface_hub import hf_hub_download
        gguf = hf_hub_download(args.gguf_repo, args.gguf_file)
        transformer = QwenImageTransformer2DModel.from_single_file(
            gguf, quantization_config=GGUFQuantizationConfig(compute_dtype=dtype),
            torch_dtype=dtype, config=args.repo, subfolder="transformer")
        pipe = QwenImageEditPipeline.from_pretrained(args.repo, transformer=transformer, torch_dtype=dtype)
    pipe.to(device)
    print(f"Loaded in {time.monotonic() - started:.0f} s", flush=True)

    with Image.open(args.photo) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
    image.thumbnail((args.side, args.side))
    generator = torch.Generator(device="cpu").manual_seed(args.seed)
    started = time.monotonic()
    result = pipe(image=image, prompt=args.instruction, negative_prompt=" ",
                  num_inference_steps=args.steps, true_cfg_scale=args.guidance,
                  generator=generator).images[0]
    took = time.monotonic() - started
    out = args.out or args.photo.with_name(f"{args.photo.stem}-edited.png")
    result.save(out)
    print(f"Wrote {out} ({result.size[0]}x{result.size[1]}) in {took:.0f} s "
          f"({took / max(1, args.steps):.1f} s a step)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
