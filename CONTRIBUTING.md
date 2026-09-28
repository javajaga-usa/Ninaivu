# Contributing to Ninaivu

## Development

Python 3.12 or newer, in a virtual environment. From the repository root:

```sh
python -m venv .venv
# activate it with your shell's command
python -m pip install -r requirements-dev.txt
python -m ruff check ninaivu tests tools start.py extensions
python -m pytest
```

Node.js is needed for the JavaScript checks under `tests/*.mjs`; the browser
tests additionally need Playwright and Chromium (`tests/run_browser_tests.py`).
The AI dependencies and model downloads are separate from the core development
environment and no test needs them.

## Changes

- Branch from `main`. One focused change per pull request.
- A fix that changes behaviour comes with a regression test that fails without
  the fix. Write the test first; it is the proof that the fault was real.
- Never modify, move or delete a person's source photographs. Anything that
  writes near a library writes into the recycle bin or a new file.
- Match the repository's existing formatting. Ruff checks correctness (the E,
  F, W and B rules); Black and isort are deliberately not run. Line breaks fall
  where the meaning does.
- Say in the pull request what a person will see differently, what you ran,
  and what you did not cover.
- Do not commit media, models, credentials, private keys, databases or logs.

## Extensions

Anything that sends data off the machine, needs a GPU, or downloads more than a
few hundred megabytes belongs under `extensions/`, off by default, and says so
to the person turning it on. See `extensions/README.md`.

## Releases

```sh
python -m build --wheel
python tools/check_distribution.py
```

Tag the release (`v0.1.0`) on `main` and publish a GitHub release from the tag.
Never move a published tag.
