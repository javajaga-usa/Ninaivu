# Repository structure

| Location | Responsibility |
| --- | --- |
| `launcher/` | Every starter for a checkout: `start.py` and the `start.cmd` (Windows) and `start.sh` (macOS, Linux) that run it, `Ninaivu.command` for the Finder, and the Control Panel's double-clicks (`Start - Ninaivu Control Panel.vbs` and `.sh`, which run `ninaivu_control.pyw`). The root holds only what Git, GitHub, pip and MkDocs look for by name. |
| `requirements/` | The core pins, the developer tools, and each optional AI extra |
| `ninaivu/` | Importable application package and bundled web assets |
| `ninaivu/api/` | HTTP endpoints and authorization boundaries |
| `ninaivu/server/` | Server lifecycle, configuration, authentication and routing |
| `ninaivu/desktop/` | Native control panel and process/resource monitoring |
| `ninaivu/media/`, `ninaivu/archive/`, `ninaivu/cloud/`, `ninaivu/storage/` | Media processing, archive workflows, cloud integration and persistence |
| `ninaivu/static/`, `ninaivu/templates/` | Browser application source and templates |
| `ninaivu/static/js/studio/` | The editing engines, as pure `.mjs` modules the workers and the Node tests both import: `develop.mjs` (light, tone, colour, curves, detail; used by both the Photo Studio and Sudar), `recipe.mjs` (what an edit is, with its ranges and the looks), and the face-retouching engine with the Skin and Hair panel shared by the Photo Studio and the AI studio; the server side of the faces is `ninaivu/media/portrait.py` and `ninaivu/api/api_portrait.py`. Tests: `tests/develop_engine.mjs`, `tests/studio_engine.mjs` |
| `tests/` | Python, JavaScript and browser regression suites, with synthetic fixtures |
| `tools/` | Maintained setup, diagnostics, maintenance and development commands |
| `installers/` | Docker, systemd, reverse-proxy examples and the Windows service installer |
| `extensions/` | Optional pieces outside the core's promise; see its README |
| `docs/` | CHANGELOG, ROADMAP, CONTRIBUTING and SECURITY, and the guides below |
| `docs/operations/` | Deployment and operations documentation |
| `docs/development/` | Contributor and architecture documentation |
| `.github/workflows/` | Executable CI definitions |

`python -m ninaivu` runs Ninaivu from a checkout, and `launcher\start.cmd` (Windows) and `launcher/start.sh` (macOS, Linux) set one up first; the installers under `installers/` are the way to run a release.

## Local/generated content

`.venv/`, `.ai-models/`, `.ninaivu-control/`, caches, `build/`, `dist/`, and package metadata directories are local outputs. Git excludes them. Docker also excludes model downloads, runtime state, and developer caches. Ninaivu's configured state directory and users' media libraries are not project cleanup targets.

The cleanup tool previews changes by default. It must not delete tracked source files, traverse model/environment folders, or follow links outside the checkout as routine cache cleanup.
