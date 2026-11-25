# The core and the models

Ninaivu has two parts that fail in different ways.

**The core** is the library, the index, people and roles, visibility, import,
albums, sharing, copies and backups, the two computers, the family app and the
console. It has to be the most stable part: it starts, answers and keeps
photographs safe with **no AI package installed**, and nothing a model does
can stop it.

**The models** are everything that downloads a model file or runs one: finding
faces, searching by picture, describing photographs, voice stories and speech,
colour, the writing model, and the Playground's tools. They arrive on request
from the AI models page, run in the model host (`ninaivu/runtime/host.py`, a
process of its own that exits when idle), and each can be deleted again
without touching the core (`DELETE /api/admin/ai-models/<id>`).

## The rules, and what checks them

1. **No module imports a model package at its top level.** A package such as
   `torch`, `onnxruntime`, `open_clip`, `transformers` or `sherpa_onnx` is
   imported inside the function that uses it, so importing Ninaivu never loads
   one. `tests/test_core_without_models.py` reads every module's imports and
   fails the pull request that adds one at the top. OpenCV is not counted as a
   model package: it is a recommended requirement of the core.
2. **The whole app starts and answers with every model package unimportable.**
   The same test starts both faces with each package made "not installed" and
   asks for their first pages.
3. **A model that is missing, deleted or fails turns its own feature off.**
   `server/features.py` decides what is on from which model is installed; a
   search that cannot encode a phrase falls back to word search
   (`api._semantic_ids`). A new model feature follows the same shape: it asks
   `features.on(cfg, name)`, and its absence is a normal answer.
4. **A new dependency of the core needs the household's yes.** The core's
   requirements are `requirements/requirements.txt`; models and their packages
   are in the `requirements-*.txt` files beside it and in the Extras on the
   AI models page.
5. **The core's own tests run with no model package installed.** The first CI
   job installs `requirements-dev.txt` only; the model stack is tried on the
   Apple-silicon "Full tier" job.

## Where the line is today, and what is left

The catalogue (`media/model_catalog.py`) lists each model with its files,
checksums, licence and the packages it needs, and `tests/test_model_manifests.py`
checks that every entry is a complete manifest (what it is for, source, licence,
what runs it, and every file pinned to its bytes and a SHA-256, or the git blob id of a pinned revision).

Each model feature moves into a package of its own under `ninaivu/models/`,
one at a time, the least connected first. Three are there (5.2):

| Package | Holds | Was |
|---|---|---|
| `models/colour` | `files` (where DDColor is looked for), `lab` (the arithmetic that lays its colour under a photograph), `runner` (the capability the model host loads) | `runtime.colour`, and the same names in `media.onnx_tools` |
| `models/search` | `runner`, SigLIP 2 through ONNX Runtime in the model host | `runtime.siglip` |
| `models/faces` | `engine` (detector, recogniser, quality gate, crops), `arcface` (the second pair) | `media.faces`, `media.faces_arcface` |

The old names are gone (nothing stands in for a moved module; `tests/test_migrations.py` keeps it that way), so every import names the package. What more than one model uses stays outside a
plugin: `runtime/ort_session.py` opens an ONNX Runtime session for all of them,
and who a face *is* (`media/facematch.py`, `faceindex.py`) is the core's.

What is left: the description, speech, voice and writing models are still in
`ninaivu/runtime/` (they already run only in the model host, and florence still
takes `missing_packages` from the search runner), and the Playground's tools
are in `media/onnx_tools.py`. Each step keeps the rules above green.
