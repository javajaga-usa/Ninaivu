"""Describe a few photographs with the on-device AI engine, and time it.

For reading what Florence-2 says about the family's own photographs before
letting it describe the whole library, and for knowing how long that would
take on this computer:

    python tools/try_captions.py photo.jpg [another.heic …]
    python tools/try_captions.py "/Volumes/…/Ninaivu Archive/2024/08" --count 20

A folder is read for its first ``--count`` pictures. Each is shrunk to the
640-pixel preview the gallery would hand the engine, then described in a
model host process exactly as the server does, using the description model
downloaded on the AI models page, or the one ``--model`` names
(``captions`` is Florence-2 base, ``captions-large`` large; ``--models``
names another folder).
Nothing is written to the index and nothing leaves the computer.
"""
from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PICTURES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".tif", ".tiff", ".bmp"}


def pictures(paths: list[Path], count: int) -> list[Path]:
    found: list[Path] = []
    for path in paths:
        if path.is_dir():
            found += sorted(p for p in path.rglob("*") if p.suffix.lower() in PICTURES)[:count]
        else:
            found.append(path)
    return found


def preview(path: Path, folder: Path, size: int = 640) -> Path:
    """The picture as the gallery's preview: its longer side *size* pixels."""
    from PIL import Image, ImageOps
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        pass
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((size, size))
        target = folder / f"{len(list(folder.iterdir()))}.jpg"
        image.save(target, quality=85)
    return target


def main(argv: list[str] | None = None) -> int:
    from ninaivu.media import model_catalog
    from ninaivu.runtime import HostError, ModelHost, florence
    from ninaivu.server.config import Config

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("photos", nargs="+", type=Path, help="pictures, or folders of them")
    parser.add_argument("--count", type=int, default=10, help="pictures taken from each folder")
    parser.add_argument("--models", default=None, help="the AI models folder (default: the server's)")
    parser.add_argument("--model", choices=florence.MODELS, default=None,
                        help="which description model (default: the best one downloaded)")
    parser.add_argument("--device", default=None,
                        help="how to run the image encoder: auto, cpu, coreml or coreml-gpu")
    parser.add_argument("--threads", type=int, default=0, help="threads for the model (default: half the cores)")
    args = parser.parse_args(argv)

    cfg = Config.load()
    model_catalog.configure(args.models or cfg.ai_models_dir or None)
    model_id = args.model or florence.chosen_model()
    why = florence.ready() or (None if model_catalog.installed(model_id or "") else "not downloaded")
    if why:
        print(f"Cannot describe photographs here: {why}\n"
              f"  python tools/fetch_ai_models.py --get {florence.suggested_model()}",
              file=sys.stderr)
        return 1
    print(f"Describing with {model_id} ({model_catalog.MODELS[model_id]['label']})", flush=True)
    setup = {"models": str(model_catalog.models_root()), "describe_model": model_id,
             "image_device": args.device}
    if args.threads:
        setup["threads"] = args.threads
    host = ModelHost(setup)
    times: list[float] = []
    failed = 0
    try:
        with tempfile.TemporaryDirectory() as scratch:
            for path in pictures(args.photos, args.count):
                try:
                    small = preview(path, Path(scratch))
                except Exception as exc:                      # noqa: BLE001
                    print(f"{path.name}: will not open ({exc})", file=sys.stderr)
                    failed += 1
                    continue
                started = time.monotonic()
                try:
                    [result] = host.call("describe", paths=[str(small)])
                except HostError as exc:
                    print(f"{path.name}: could not be described: {exc}", file=sys.stderr)
                    failed += 1
                    continue
                took = time.monotonic() - started
                if result.get("error"):
                    print(f"{path.name}: {result['error']}", file=sys.stderr)
                    failed += 1
                    continue
                times.append(took)
                print(f"{path.name} ({took:.1f} s): {result['text']}", flush=True)
    finally:
        host.close()
    if len(times) > 1:
        steady = statistics.median(times[1:])
        print(f"\nThe first took {times[0]:.1f} s with loading the model; after that "
              f"{steady:.2f} s a photograph, so 10,000 photographs in about "
              f"{steady * 10000 / 3600:.1f} hours of this computer's time.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
