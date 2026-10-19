"""Compare the two pairs of face models on the family's own photographs.

For deciding whether to switch ``face_model`` to ``arcface`` before the next
scan looks at every photograph again:

    python tools/try_faces.py "/Volumes/…/Ninaivu Archive/2024/08" --count 40
    python tools/try_faces.py photo1.jpg photo2.jpg --pairs

Each picture is opened the right way up, as the face pass opens it, and both
pairs look at it: OpenCV's YuNet and SFace (``sface``) and InsightFace's
SCRFD and ArcFace (``arcface``, downloaded on the AI models page). For each
pair the tool prints how many faces it found, how long it took, and with
``--pairs`` the similarity of every face to every other, so the household can
see where its own relatives land against each recogniser's thresholds.
Nothing is written to the index and nothing leaves the computer.
"""
from __future__ import annotations

import argparse
import statistics
import sys
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


def main(argv: list[str] | None = None) -> int:
    from ninaivu.media import facematch, faces, model_catalog
    from ninaivu.server.config import Config

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("photos", nargs="+", type=Path, help="pictures, or folders of them")
    parser.add_argument("--count", type=int, default=20, help="pictures taken from each folder")
    parser.add_argument("--models", default=None, help="the AI models folder (default: the server's)")
    parser.add_argument("--only", choices=("sface", "arcface"), default=None,
                        help="one pair only (default: both)")
    parser.add_argument("--pairs", action="store_true",
                        help="print the similarity of every face to every other")
    args = parser.parse_args(argv)

    cfg = Config.load()
    model_catalog.configure(args.models or cfg.ai_models_dir or None)
    choices = [args.only] if args.only else ["sface", "arcface"]
    found = pictures(args.photos, args.count)
    if not found:
        print("No pictures found.", file=sys.stderr)
        return 1

    for choice in choices:
        engine = faces.FaceEngine(cfg.state_dir, model=choice)
        if not engine.available:
            print(f"{choice}: not available here: {engine.unavailable_reason}\n"
                  f"  python tools/fetch_ai_models.py --get {faces.CATALOG_ENTRY[choice]}",
                  file=sys.stderr)
            continue
        rules = facematch.thresholds_for(engine.model_id)
        print(f"\n== {choice} ({engine.model_id}): auto from {rules.auto}, "
              f"ask from {rules.suggest}, group from {rules.cluster}")
        times: list[float] = []
        seen: list[tuple[str, int, object]] = []
        total = 0
        for path in found:
            image = faces.load_image_for_faces(path, 0)
            if image is None:
                print(f"  {path.name}: will not open", file=sys.stderr)
                continue
            started = time.monotonic()
            detected = engine.detect(image)
            took = time.monotonic() - started
            times.append(took)
            total += len(detected)
            sizes = ", ".join(f"{f.bbox[2]}px q{f.quality:.2f}" for f in detected) or "none"
            print(f"  {path.name}: {len(detected)} face(s) in {took * 1000:.0f} ms ({sizes})")
            for index, face in enumerate(detected):
                seen.append((path.name, index, face.embedding))
        if times:
            print(f"  {total} faces in {len(times)} pictures, "
                  f"{statistics.median(times) * 1000:.0f} ms a picture (median), "
                  f"{sum(times):.1f} s in all")
        if args.pairs and len(seen) > 1:
            print("  similarities (same person is above the auto bar, a stranger below ask):")
            for i, (name_a, idx_a, vec_a) in enumerate(seen):
                for name_b, idx_b, vec_b in seen[i + 1:]:
                    score = facematch.similarity(vec_a, vec_b)
                    verdict = ("same" if score >= rules.auto else
                               "ask" if score >= rules.suggest else "different")
                    print(f"    {name_a}#{idx_a} ~ {name_b}#{idx_b}: {score:.3f} {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
