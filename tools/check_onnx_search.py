"""Check search without PyTorch against search with it, on your own photographs.

The on-device AI engine runs SigLIP 2 through ONNX Runtime instead of PyTorch
(ninaivu/runtime/siglip.py) and keeps PyTorch's model id, so a library indexed
with SigLIP 2 is not indexed again. This says whether that is safe here: for
each photograph it makes the vector both ways and prints how alike they are
(1.0 is identical), does the same for a few searches, and times both:

    python tools/check_onnx_search.py "/Volumes/…/Ninaivu Archive/2024" --count 50

Needs the search model on the AI models page ("AI search without PyTorch")
and the image engine (Extras). PyTorch's side needs PyTorch, open_clip and the
"AI search (SigLIP 2)" download; without them only the engine's timings are
printed. Nothing is written to the index and nothing leaves the computer.
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SEARCHES = ("a birthday cake with candles", "grandmother in a sari", "the beach at sunset",
            "a dog", "a temple", "kolam", "children playing in the rain", "a wedding")


def main(argv: list[str] | None = None) -> int:
    import numpy as np
    from PIL import Image, ImageOps

    from ninaivu import ai
    from ninaivu.media import model_catalog
    from ninaivu.runtime import ModelHost, siglip
    from ninaivu.server.config import Config
    from try_captions import pictures

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("photos", nargs="+", type=Path, help="pictures, or folders of them")
    parser.add_argument("--count", type=int, default=30, help="pictures taken from each folder")
    parser.add_argument("--models", default=None, help="the AI models folder (default: the server's)")
    args = parser.parse_args(argv)

    cfg = Config.load()
    model_catalog.configure(args.models or cfg.ai_models_dir or None)
    if why := siglip.ready():
        print(f"Cannot search without PyTorch here: {why}", file=sys.stderr)
        return 1
    images = []
    for path in pictures(args.photos, args.count):
        try:
            with Image.open(path) as image:
                image = ImageOps.exif_transpose(image).convert("RGB")
                image.thumbnail((640, 640))
                images.append((path, image.copy()))
        except Exception as exc:                              # noqa: BLE001
            print(f"{path.name}: will not open ({exc})", file=sys.stderr)
    if not images:
        print("No photographs to compare.", file=sys.stderr)
        return 1

    host = ModelHost({"models": str(model_catalog.models_root())})
    try:
        host.call("search", op="texts", texts=["warming up"])
        started = time.monotonic()
        onnx_images = np.concatenate([
            host.call("search", op="pixels", pixels=[siglip.pixels(img) for _, img in images[i:i + 16]])
            for i in range(0, len(images), 16)])
        onnx_took = (time.monotonic() - started) / len(images)
        started = time.monotonic()
        onnx_texts = host.call("search", op="texts", texts=list(SEARCHES))
        text_took = (time.monotonic() - started) / len(SEARCHES)
    finally:
        host.close()
    print(f"engine: {onnx_took * 1000:.0f} ms a photograph, {text_took * 1000:.0f} ms a search")

    try:
        torch_engine = ai.ClipEngine(*ai.clip_choice(cfg))
    except Exception as exc:                                  # noqa: BLE001
        print(f"PyTorch's side not checked: {exc}")
        return 0
    if torch_engine.model_id != siglip.MODEL_ID:
        print(f"PyTorch's model here is {torch_engine.model_id}, not SigLIP 2: a library indexed "
              "with it is indexed again when the engine takes over search.")
        return 0
    started = time.monotonic()
    torch_images = np.concatenate([torch_engine.embed_images([img for _, img in images[i:i + 16]])
                                   for i in range(0, len(images), 16)])
    torch_took = (time.monotonic() - started) / len(images)
    torch_texts = torch_engine.embed_texts(list(SEARCHES))
    print(f"PyTorch: {torch_took * 1000:.0f} ms a photograph")

    alike = (onnx_images * torch_images).sum(axis=1)
    for (path, _), value in zip(images, alike):
        print(f"  {value:.5f}  {path.name}")
    words = (onnx_texts * torch_texts).sum(axis=1)
    for text, value in zip(SEARCHES, words):
        print(f"  {value:.5f}  “{text}”")
    # The same photographs found for each search, in the same order?
    same = sum(int(np.array_equal(np.argsort(-(onnx_images @ t))[:5], np.argsort(-(torch_images @ u))[:5]))
               for t, u in zip(onnx_texts, torch_texts))
    print(f"\nphotographs: worst {alike.min():.5f}, median {statistics.median(alike):.5f}; "
          f"searches: worst {words.min():.5f}; same top five for {same} of {len(SEARCHES)} searches")
    ok = alike.min() > 0.995 and words.min() > 0.995
    print("The vectors match: the library need not be indexed again." if ok else
          "The vectors differ more than rounding: keep the on-device engine off, so search "
          "stays on PyTorch, until this is looked into.")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
