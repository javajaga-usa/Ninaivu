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
| `creative-studio` | `extensions/creative-studio/` | Nothing; heavy edits run here with a GPU or on a ComfyUI server in the house |

Install one from a checkout with `pip install -e extensions/<name>`, then turn
it on in the console under **AI models → Extensions** and restart Ninaivu.

For development and the tests, `NINAIVU_EXTENSION_MODULES=ninaivu_gemini` with
`extensions/gemini` on `PYTHONPATH` loads an extension without installing it.

## How the core finds them

`ninaivu/extensions.py` reads the `ninaivu.extensions` entry-point group. An
extension module carries `NAME`, `TITLE`, `SUMMARY`, `DATA_LEAVES_THE_MACHINE`,
`DESTINATION`, `DOWNLOADS` and `register(app, face)`, and may offer an
`image_provider` that Sudar's own routes hand an edit to — only when a request
names it — or a `studio` that does Sudar's heavy edits (generate, remove,
enhance). `Config.extensions` lists the ones that are on.

## Where the line is

`media/model_catalog.py`, `media/onnx_tools.py` and `media/inpaint.py` stay in
the core on purpose: the models they run (faces, orientation, a small
upscaler, a small object remover, SigLIP for search) are megabytes, not
gigabytes, and run on a processor. The line is what a small always-on
machine can do.
