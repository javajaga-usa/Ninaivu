# Installers

How to put Ninaivu on a machine.

| Folder | For |
| --- | --- |
| `windows/` | The Windows installer (`build.ps1` → pynsist → `Ninaivu-<version>-windows-x64.exe`), the portable zip (`build-portable.ps1` → `Ninaivu-<version>-windows-x64-portable.zip`), the winget manifests, and `install-service.ps1` for running Ninaivu as a Windows service |
| `linux/` | The Linux and Raspberry Pi installer (`build.sh amd64\|arm64` → `Ninaivu-<version>-linux-<arch>.sh`): one file with its own Python and every wheel inside, run with `sh`, no root needed; `install.sh` is what it runs after unpacking |
| `strip_sources.py` | Run by every build on the Ninaivu and extension wheels: the Python is compiled to bytecode and the source dropped, so no installer carries source |
| `macos/` | The macOS app (`build.sh` → `Ninaivu.app` in a `.dmg`, signed and notarised when the signing secrets are set) and the Homebrew cask |
| `docker/` | `Dockerfile`, `docker-compose.yml`. Build context is the repository root: `docker compose -f installers/docker/docker-compose.yml up -d` |
| `systemd/` | `ninaivu.service` for Linux |
| `caddy/`, `nginx/` | Reverse-proxy examples with HTTPS |

## The Windows service (`install-service.ps1`)

Registers a scheduled task that starts Ninaivu at boot **as the account that
ran the script** (captured before it elevates; `-RunAsUser` chooses another),
with limited rights and logon type S4U: it runs whether or not that account is
signed in, and no password is stored. S4U tasks cannot reach network shares,
so the library has to be on a local disk. The task passes `--state-dir`
(that account's own state folder, which the tray also uses) and
`--supervised`, so a restart from the console exits and a keep-alive trigger
starts it again within a minute. `-Action Stop` disables the task until
`-Action Start`. See `docs/operations/production.md`, Option C.

## The Windows portable zip (`build-portable.ps1`)

For a computer where nothing may be installed, or to carry Ninaivu on a USB
drive. Extract the zip anywhere that can be written to and open `Ninaivu.exe`.
It holds three things at its top and nothing else:

| Entry | What it is |
| --- | --- |
| `Ninaivu.exe` | A small launcher (`windows/portable/Ninaivu.cs`, built with the `csc.exe` that ships with Windows). It opens the Control Panel with `NINAIVU_HOME`, `NINAIVU_STATE_DIR`, `NINAIVU_AI_MODELS_DIR` and `NINAIVU_LOG_DIR` pointed into the folder, and ends. |
| `app\` | The private Python and packages (the same ones the installer carries, taken from its build), the library's index and settings (`app\data`), the AI models (`app\ai-models`), the readme and licence |
| `logs\` | `ninaivu.log` (the server's own account) and `server.log` (what it printed), so a log is a thing a person can find and send |

Nothing is installed and nothing outside the folder is touched, with one
exception that is the person's to choose: *Start when I sign in* writes a
registry value of its own (`Ninaivu (portable)`, pointing at
`Ninaivu.exe --autostart`, beside an installed Ninaivu's `Ninaivu`), and
turning it off removes it. Otherwise deleting the folder removes everything it
made. `build-portable.ps1` runs
after `build.ps1` in the same job of the release workflow (it reuses the
Python and packages pynsist just assembled, so nothing is built or fetched
twice), signs `Ninaivu.exe` when the certificate is there, writes the zip
entry by entry with `/` in every name, and refuses to finish if its top level
is anything but those three.

## The desktop installers

Both put a private Python, Ninaivu, every wheel it needs and both extensions
(off until switched on) in one place, and open **the tray** — the icon in the
system tray or menu bar that starts and stops the server, opens the family
app and the console, checks for an update and can start at sign-in
(`docs/desktop-control.md`). Nothing is downloaded when they install or run.

`.github/workflows/release.yml` builds all of them on a version tag and
attaches them to the GitHub release: the Windows `.exe`, a `.dmg` for Apple
Silicon and one for Intel, the Homebrew cask with both hashes, and a
`SHA256SUMS.txt`. The winget manifests come out as a workflow artifact, ready
for a pull request to `microsoft/winget-pkgs`.

Signing needs the secrets the workflow lists at the top (a code-signing
certificate for Windows; a Developer ID certificate and an app-specific
password for macOS). Without them the workflow still builds everything,
unsigned, and the release notes say so — SmartScreen and Gatekeeper warn
about those. Whether a release is signed depends on those secrets, and its
notes say which; `SHA256SUMS.txt` is how to check a download either way
(docs/SECURITY.md).

A release is made only from a commit whose tests passed, and never over one
already published (the first job of `release.yml`). Each installer is built
from the versions in `requirements/constraints-tested.txt`, and the list of
what it carries, with hashes, goes out with it as `*-packages.txt`.

To build by hand: `installers\windows\build.ps1` on Windows (needs
`pip install pynsist`), `bash installers/macos/build.sh` on a Mac (needs
Xcode's command-line tools). Both print the checksum and write the manifest
for that version under their `build/` folder.

Until you have one of these, `start.cmd <folder>` (Windows) or
`sh launcher/start.sh <folder>` from the repository root runs Ninaivu from a
checkout, and `python -m ninaivu.desktop.tray` opens the tray from it.

See `docs/operations/production.md` for the full operations guide.
