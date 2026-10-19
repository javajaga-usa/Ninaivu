"""Write down one recording with the on-device AI engine, and say how long it took.

For trying the speech models on the family's own voices before trusting them
with every story: Whisper's Tamil is good, not perfect, and how good depends
on the speaker, the room and the phone. Run it on a few recordings of the
people who will tell stories, in Tamil and in English:

    python tools/try_speech.py recording.m4a [another.ogg …]
    python tools/try_speech.py --languages ta,en --no-translate memo.mp3

It uses the speech model downloaded on the AI models page (large-v3 when it
is there, otherwise turbo), in a model host process exactly as the server
does, with the models folder the server uses unless ``--models`` names
another. Nothing is written to the index and nothing leaves the computer.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv: list[str] | None = None) -> int:
    from ninaivu.media import media, model_catalog
    from ninaivu.runtime import HostError, ModelHost, speech
    from ninaivu.server.config import Config

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("recordings", nargs="+", type=Path)
    parser.add_argument("--languages", default=None,
                        help="the household's languages, most used first (default: the setting, or ta,en)")
    parser.add_argument("--no-translate", action="store_true", help="skip the English translation")
    parser.add_argument("--models", default=None, help="the AI models folder (default: the server's)")
    parser.add_argument("--threads", type=int, default=0, help="threads for the model (default: half the cores)")
    parser.add_argument("--json", action="store_true", help="print the whole answer as JSON")
    args = parser.parse_args(argv)

    cfg = Config.load()
    model_catalog.configure(args.models or cfg.ai_models_dir or None)
    model = speech.chosen_model()
    if model is None:
        print("No speech model is downloaded: download one on the AI models page, or run\n"
              "  python tools/fetch_ai_models.py --get speech", file=sys.stderr)
        return 1
    languages = speech.languages(args.languages or cfg.speech_languages or "ta,en")
    setup = {"models": str(model_catalog.models_root())}
    if args.threads:
        setup["threads"] = args.threads
    host = ModelHost(setup)
    print(f"model: {model}, languages: {', '.join(languages)}", flush=True)
    failed = 0
    try:
        for path in args.recordings:
            started = time.monotonic()
            try:
                result = host.call("transcribe", path=str(path.resolve()), ffmpeg=media.FFMPEG,
                                   languages=languages, translate=not args.no_translate)
            except HostError as exc:
                print(f"\n{path.name}: could not be written down: {exc}", file=sys.stderr)
                failed += 1
                continue
            took = time.monotonic() - started
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
                continue
            print(f"\n{path.name} — {result['seconds']} s of sound, {took:.1f} s "
                  f"(the first includes loading the model), heard as {result['language']}")
            for segment in result["segments"]:
                print(f"  [{segment['start']:7.2f} – {segment['end']:7.2f}] {segment['text']}")
                if segment["english"]:
                    print(f"  {'':19} {segment['english']}")
    finally:
        host.close()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
