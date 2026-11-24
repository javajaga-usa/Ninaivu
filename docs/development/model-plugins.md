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
checksums, licence and the packages it needs. What is not yet done: faces and
picture search still live under `ninaivu/media` beside core code, and the
catalogue is a list rather than one manifest per plugin. Moving each feature
into its own package with its own manifest (the way `ninaivu/features` and
`extensions/` already work) is done one feature at a time, least connected
first (colour, the Playground's tools), faces last. Each step keeps the rules
above green.
