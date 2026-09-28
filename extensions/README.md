# Extensions

The core of Ninaivu makes one promise: a photograph never leaves the house
unless the administrator chose where it goes, and the server runs on a small
machine without a GPU. Anything that cannot keep that promise lives here, off
by default, and says plainly what it does when it is turned on.

## The contract

An extension:

- is a Python package under `extensions/<name>/` with its own `README.md`,
  `requirements.txt` and tests;
- registers its blueprints and settings through one entry point, and the core
  runs without it installed;
- is off until an administrator turns it on in the console, and the console
  shows, on the switch, where data goes and what will be downloaded;
- never touches a source photograph.

## Planned extensions

These are still inside the `ninaivu` package in 0.1.0 and only run when a
model or a key has been installed deliberately. Moving them here is Phase 0
of the roadmap.

| Extension | Today's modules | Why it is not core |
| --- | --- | --- |
| `creative-studio` | `media/generative_editing.py`, `media/inpaint.py`, `media/model_catalog.py`, `ai_server/`, `api/ai_server_api.py` | Multi-gigabyte models, needs a GPU |
| `gemini` | `media/gemini_media.py` | Sends a re-encoded picture to Google |
