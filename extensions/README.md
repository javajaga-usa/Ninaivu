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

## Extensions in this repository

| Extension | Folder | What leaves the house when it is on |
| --- | --- | --- |
| `gemini` | `extensions/gemini/` | A re-encoded copy of the photograph and the instruction, to Google's Generative Language API |

Install one from a checkout with `pip install -e extensions/<name>`, then turn
it on in the console under **AI models → Extensions** and restart Ninaivu.

For development and the tests, `NINAIVU_EXTENSION_MODULES=ninaivu_gemini` with
`extensions/gemini` on `PYTHONPATH` loads an extension without installing it.

## How the core finds them

`ninaivu/extensions.py` reads the `ninaivu.extensions` entry-point group. An
extension module carries `NAME`, `TITLE`, `SUMMARY`, `DATA_LEAVES_THE_MACHINE`,
`DESTINATION`, `DOWNLOADS` and `register(app, face)`, and may offer an
`image_provider` that Sudar's own routes hand an edit to — only when a request
names it. `Config.extensions` lists the ones that are on.

## Planned extensions

Still inside the `ninaivu` package in 0.1.0; it only runs when a model has
been installed deliberately. Moving it here is Phase 0 of the roadmap.

| Extension | Today's modules | Why it is not core |
| --- | --- | --- |
| `creative-studio` | `media/generative_editing.py`, `media/inpaint.py`, `media/model_catalog.py`, `ai_server/`, `api/ai_server_api.py` | Multi-gigabyte models, needs a GPU |
