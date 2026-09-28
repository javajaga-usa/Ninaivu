# Repository structure

| Location | Responsibility |
| --- | --- |
| `ninaivu/` | Importable application package and bundled web assets |
| `ninaivu/api/` | HTTP endpoints and authorization boundaries |
| `ninaivu/server/` | Server lifecycle, configuration, authentication and routing |
| `ninaivu/desktop/` | Native control panel and process/resource monitoring |
| `ninaivu/media/`, `ninaivu/archive/`, `ninaivu/cloud/`, `ninaivu/storage/` | Media processing, archive workflows, cloud integration and persistence |
| `ninaivu/static/`, `ninaivu/templates/` | Browser application source and templates |
| `tests/` | Python, JavaScript and browser regression suites, with synthetic fixtures |
| `tools/` | Maintained setup, diagnostics, maintenance and development commands |
| `installers/` | Docker, systemd, reverse-proxy examples and the Windows service installer |
| `extensions/` | Optional pieces outside the core's promise; see its README |
| `docs/operations/` | Deployment and operations documentation |
| `docs/development/` | Contributor and architecture documentation |
| `.github/workflows/` | Executable CI definitions |

`start.py` at the root is the desktop way to run Ninaivu until the installers exist.

## Local/generated content

`.venv/`, `.ai-models/`, `.ninaivu-control/`, caches, `build/`, `dist/`, and package metadata directories are local outputs. Git excludes them. Docker also excludes model downloads, runtime state, and developer caches. Ninaivu's configured state directory and users' media libraries are not project cleanup targets.

The cleanup tool previews changes by default. It must not delete tracked source files, traverse model/environment folders, or follow links outside the checkout as routine cache cleanup.
