# Repository structure

| Location | Responsibility |
| --- | --- |
| `start.cmd` | The one file at the root besides what Git, GitHub and pip need there: double-click to set up and start on Windows |
| `launcher/` | `start.py` (the launcher `start.cmd` runs) and `start.sh` for macOS and Linux |
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

`start.cmd` (Windows) and `launcher/start.sh` (macOS, Linux) are the desktop way to run Ninaivu until the installers exist.

## Local/generated content

`.venv/`, `.ai-models/`, `.ninaivu-control/`, caches, `build/`, `dist/`, and package metadata directories are local outputs. Git excludes them. Docker also excludes model downloads, runtime state, and developer caches. Ninaivu's configured state directory and users' media libraries are not project cleanup targets.

The cleanup tool previews changes by default. It must not delete tracked source files, traverse model/environment folders, or follow links outside the checkout as routine cache cleanup.
