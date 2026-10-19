"""Check search without PyTorch against search with it, on your own photographs.

The on-device AI engine runs SigLIP 2 through ONNX Runtime instead of PyTorch
(ninaivu/runtime/siglip.py) and keeps PyTorch's model id, so a library indexed
with SigLIP 2 is not indexed again. This says whether that is safe here: for
each photograph it makes the vector both ways and prints how alike they are
(1.0 is identical), does the same for a few searches, and times both:

    python tools/check_onnx_search.py "/Volumes/…/Ninaivu Archive/2024" --count 50

Each way of running the image half this computer offers is tried (the
processor, and on a Mac Core ML on every unit or on the processor and
graphics processor; ``--devices`` names fewer), so it also says which of
them gives PyTorch's vectors and how fast. It passes when the default
(``auto``) does.

Needs the search model on the AI models page ("AI search without PyTorch")
and the image engine (Extras). PyTorch's side needs PyTorch, open_clip and the
"AI search (SigLIP 2)" download; without them only the engine's timings are
printed. Nothing is written to the index and nothing leaves the computer.
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SEARCHES = ("a birthday cake with candles", "grandmother in a sari", "the beach at sunset",
            "a dog", "a temple", "kolam", "children playing in the rain", "a wedding")


def main(argv: list[str] | None = None) -> int:
    import numpy as np
    from PIL import Image, ImageOps

    from ninaivu import ai
    from ninaivu.media import model_catalog
    from ninaivu.runtime import ModelHost, devices, siglip
    from ninaivu.server.config import Config
    from try_captions import pictures

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("photos", nargs="+", type=Path, help="pictures, or folders of them")
    parser.add_argument("--count", type=int, default=30, help="pictures taken from each folder")
    parser.add_argument("--models", default=None, help="the AI models folder (default: the server's)")
    parser.add_argument("--devices", default="auto," + ",".join(devices.IMAGE_DEVICES),
                        help="ways of running the image half to try (default: all)")
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

    tried: dict[str, tuple[Any, float]] = {}
    onnx_texts, text_took = None, 0.0
    for device in [d.strip() for d in args.devices.split(",") if d.strip()]:
        host = ModelHost({"models": str(model_catalog.models_root()),
                          "threads": os.cpu_count() or 4, "image_device": device})
        try:
            host.call("search", op="texts", texts=["warming up"])
            host.call("search", op="pixels", pixels=[siglip.pixels(images[0][1])])
            started = time.monotonic()
            vectors = np.concatenate([
                host.call("search", op="pixels",
                          pixels=[siglip.pixels(img) for _, img in images[i:i + 16]])
                for i in range(0, len(images), 16)])
            tried[device] = (vectors, (time.monotonic() - started) / len(images))
            if onnx_texts is None:
                started = time.monotonic()
                onnx_texts = host.call("search", op="texts", texts=list(SEARCHES))
                text_took = (time.monotonic() - started) / len(SEARCHES)
        except Exception as exc:                              # noqa: BLE001 - say, try the next
            print(f"{device}: could not run ({exc})")
        finally:
            host.close()
    if not tried:
        return 1
    for device, (_, took) in tried.items():
        print(f"engine on {device} ({devices.image_providers(device)[0]}): "
              f"{took * 1000:.0f} ms a photograph")
    print(f"engine: {text_took * 1000:.0f} ms a search")

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

    words = (onnx_texts * torch_texts).sum(axis=1)
    for text, value in zip(SEARCHES, words):
        print(f"  {value:.5f}  “{text}”")
    passed: dict[str, bool] = {}
    for device, (onnx_images, _) in tried.items():
        alike = (onnx_images * torch_images).sum(axis=1)
        print(f"\n{device}:")
        for (path, _), value in zip(images, alike):
            print(f"  {value:.5f}  {path.name}")
        # The same photographs found for each search, in the same order?
        same = sum(int(np.array_equal(np.argsort(-(onnx_images @ t))[:5],
                                      np.argsort(-(torch_images @ u))[:5]))
                   for t, u in zip(onnx_texts, torch_texts))
        passed[device] = bool(alike.min() > 0.995 and words.min() > 0.995)
        print(f"{device}: photographs worst {alike.min():.5f}, median "
              f"{statistics.median(alike):.5f}; same top five for {same} of {len(SEARCHES)} "
              f"searches; {'matches' if passed[device] else 'DIFFERS'}")
    ok = passed.get("auto", all(passed.values()))
    print("\nThe default matches: the library need not be indexed again." if ok else
          "\nThe default differs more than rounding: keep the on-device engine off, so search "
          "stays on PyTorch, until this is looked into.")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
