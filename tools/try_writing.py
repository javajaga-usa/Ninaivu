"""Ask the on-device writing model something, and say how long it took.

For trying the language model (Gemma 3 12B, the AI models page's "Writing in
Tamil and English") on this computer before the films' chapters and Ask
Ninaivu rely on it: how good its Tamil is, and how long an answer takes.

    python tools/try_writing.py "Write two warm sentences about a family trip to Madurai."
    python tools/try_writing.py --tamil "மதுரைப் பயணம் பற்றி இரண்டு வரிகள் எழுது."

It runs the model in a model host process exactly as the server does, with
the models folder the server uses unless ``--models`` names another. Nothing
is written anywhere and nothing leaves the computer.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv: list[str] | None = None) -> int:
    from ninaivu.media import model_catalog
    from ninaivu.runtime import HostError, ModelHost, llm
    from ninaivu.server.config import Config

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("prompt", nargs="+", help="what to ask")
    parser.add_argument("--tamil", action="store_true", help="ask for the answer in Tamil")
    parser.add_argument("--max-tokens", type=int, default=400)
    parser.add_argument("--models", default=None, help="the AI models folder (default: the server's)")
    parser.add_argument("--threads", type=int, default=0, help="threads for the model (default: half the cores)")
    args = parser.parse_args(argv)

    cfg = Config.load()
    model_catalog.configure(args.models or cfg.ai_models_dir or None)
    if why := llm.ready():
        print(f"Cannot write here: {why}.\n"
              "  python tools/fetch_ai_models.py --get text-model", file=sys.stderr)
        return 1
    setup = {"models": str(model_catalog.models_root())}
    if args.threads:
        setup["threads"] = args.threads
    language = "Tamil" if args.tamil else "the language the question is in"
    messages = [{"role": "system", "content": f"You are Ninaivu, a family's photo library. Answer in {language}."},
                {"role": "user", "content": " ".join(args.prompt)}]
    host = ModelHost(setup)
    started = time.monotonic()
    try:
        answer = host.call("generate", messages=messages, max_tokens=args.max_tokens,
                           temperature=0.4, timeout=900)
    except HostError as exc:
        print(f"Could not write: {exc}", file=sys.stderr)
        return 1
    finally:
        host.close()
    took = time.monotonic() - started
    print(answer["text"])
    print(f"\n— {answer['model']}, {answer['tokens']} tokens in {took:.1f} s "
          f"(including loading the model; {answer['took']:.1f} s writing)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
