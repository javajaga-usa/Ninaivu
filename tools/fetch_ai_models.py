#!/usr/bin/env python3
"""List or download Ninaivu's optional AI models — the same catalogue as Admin → AI models.

    python tools/fetch_ai_models.py                  # what is available, and what is installed
    python tools/fetch_ai_models.py --get lama       # one model
    python tools/fetch_ai_models.py --get all        # everything in the catalogue

Every file is pinned to a repository revision and a SHA-256, and a download that
does not match is discarded. Models go to .ai-models/ in the project folder, or
to NINAIVU_AI_MODELS_DIR. Their Python packages are separate:
requirements/requirements-ai-local.txt (object removal, upscale, restore) and
requirements/requirements-ai.txt (AI search).

Exit codes: 0 done, 1 a download failed, 2 bad arguments.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ninaivu.media import model_catalog  # noqa: E402


def _mb(count: int) -> str:
    return f"{count / 2**20:,.1f} MB"


def show() -> None:
    print(f"Models folder: {model_catalog.models_root()}\n")
    for model_id in model_catalog.MODELS:
        info = model_catalog.describe(model_id)
        state = "installed" if info["installed"] else "not installed"
        print(f"  {model_id:9s} {info['label']} — {_mb(info['bytes'])}, {state}")
        print(f"            {info['used_for']}")
        print(f"            Licence: {info['licence']}. Source: {info['source']}.")
        if info["missing_packages"]:
            print(f"            Needs Python packages: {', '.join(info['missing_packages'])}")
    print("\nDownload with: python tools/fetch_ai_models.py --get <name> [<name> ...] | all")


def fetch(model_id: str) -> bool:
    model = model_catalog.MODELS[model_id]
    if model_catalog.installed(model_id):
        print(f"{model_id}: already installed")
        return True
    print(f"{model_id}: downloading {_mb(model_catalog.total_bytes(model_id))} ({model['licence']})")
    for entry in model["files"]:
        target = model_catalog.file_path(entry)
        if target.is_file() and target.stat().st_size == entry["bytes"]:
            continue
        seen = {"bytes": 0, "last": 0}

        def progress(count: int, entry=entry, seen=seen) -> None:
            seen["bytes"] += count
            percent = seen["bytes"] * 100 // max(1, entry["bytes"])
            if percent >= seen["last"] + 10:
                seen["last"] = percent - percent % 10
                print(f"  {entry['name']}: {percent}%", flush=True)

        try:
            model_catalog.download_file(entry, progress)
        except Exception as error:                  # noqa: BLE001 - reported, then exit 1
            print(f"  {entry['name']}: FAILED — {error}", file=sys.stderr)
            return False
    print(f"{model_id}: installed")
    return True


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--get", nargs="+", metavar="MODEL",
                        help=f"models to download: {', '.join(model_catalog.MODELS)}, or all")
    args = parser.parse_args(argv)
    if not args.get:
        show()
        return 0
    wanted = list(model_catalog.MODELS) if args.get == ["all"] else args.get
    unknown = [name for name in wanted if name not in model_catalog.MODELS]
    if unknown:
        print(f"Unknown model(s): {', '.join(unknown)}. Choose from: {', '.join(model_catalog.MODELS)}",
              file=sys.stderr)
        return 2
    ok = all([fetch(name) for name in wanted])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
