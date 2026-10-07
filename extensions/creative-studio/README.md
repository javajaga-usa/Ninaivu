# Creative Studio

An extension for Ninaivu: the heavy end of the Sudar photo studio —
generative edits from an instruction, object removal, upscaling, restoration
and colourising with large models. Either on this computer, with a diffusion
model installed by hand and a GPU to make it usable, or on a **ComfyUI server**
elsewhere in the house, which the console's **AI → AI server** page sets up
and assigns a workflow to each job.

**What leaves the house when it is on:** nothing. The AI server is an address
on the home network that the administrator typed, and the console refuses one
outside it.

**Why it is not core:** the core promises to run on a small machine without a
graphics card. Everything here wants the opposite.

Without it, Sudar still does light, colour, crops, straightening and looks;
removes objects with the small models under **AI models**; and upscales and
restores with the same. Those run on the core's own routes.

## Install and turn on

```sh
pip install -e extensions/creative-studio            # from a checkout
pip install -e "extensions/creative-studio[local]"   # also the local diffusion stack
```

Then in the console, **AI models → Extensions**, switch Creative Studio on and
restart Ninaivu. The **AI server** page appears under AI.

The `ai_server_*` settings this extension reads stay in Ninaivu's own
`config.json` (and on the Advanced settings page), so switching it off keeps what
was typed.

## What is in the package

| Module | What it does |
| --- | --- |
| `generative_editing.py` | An instruction-following diffusion model on this machine |
| `ai_server/comfyui.py` | The ComfyUI client: upload, queue, progress over a websocket, result |
| `ai_server/workflows.py` | Workflow files, their placeholders, and which job each serves |
| `ai_server/service.py` | Edit, remove and enhance, with the preview shrunk to the server's limit |
| `console_api.py` | The AI server page's endpoints, console only |

The core asks for these through one object, `ninaivu_studio.studio`
(`capabilities`, `edit`, `remove`, `job`); see `ninaivu/extensions.py`.

## Tests

The tests live in the main suite (`tests/test_ai_server.py`,
`tests/test_generative_editing.py`) and skip themselves when this package is
not importable. In a checkout, `PYTHONPATH=extensions/creative-studio` is
enough.
