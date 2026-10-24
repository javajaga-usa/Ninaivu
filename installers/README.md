# Builds

How Ninaivu is put on a machine. It ships as one **portable folder** per
computer — nothing installed, its own Python and every package inside, and
deleting the folder removes everything it made:

| Download | For |
| --- | --- |
| `Ninaivu-<version>-windows-x64-portable.zip` | Windows 10 and 11, 64-bit |
| `Ninaivu-<version>-macos-arm64-portable.zip` | Macs with Apple silicon, macOS 12 or newer |
| `Ninaivu-<version>-linux-x64-portable.tar.gz` | 64-bit Linux PCs: Ubuntu, Debian and the like |

| Folder | What it holds |
| --- | --- |
| `windows/` | `build.ps1` (the private Python, the python-build-standalone build `python-version.txt` names, with Ninaivu and every wheel installed into it) and `build-portable.ps1` (→ the zip, with `Ninaivu.exe` from `portable/Ninaivu.cs`); `install-service.ps1` for running Ninaivu as a Windows service |
| `macos/` | `build.sh` (→ `Ninaivu.app` in the portable folder, zipped; signed and notarised when the signing secrets are set) and `launcher.sh`, the app's launcher |
| `linux/` | `build.sh` (→ the `.tar.gz`: the `ninaivu` script from `portable/ninaivu`, `app/` with the Python and every package installed, `logs/`) |
| `strip_sources.py` | Run by every build on the Ninaivu and extension wheels: the Python is compiled to bytecode and the source dropped, so no download carries source |
| `docker/` | `Dockerfile`, `docker-compose.yml` (x64). Build context is the repository root: `docker compose -f installers/docker/docker-compose.yml up -d` |
| `systemd/` | `ninaivu.service` for Linux, for a checkout or the portable folder |
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

## The Windows zip

For any Windows computer, including one where nothing may be installed, or to
carry Ninaivu on a USB drive. Extract the zip anywhere that can be written to
and open `Ninaivu.exe`. It holds three things at its top and nothing else:

| Entry | What it is |
| --- | --- |
| `Ninaivu.exe` | A small launcher (`windows/portable/Ninaivu.cs`, built with the `csc.exe` that ships with Windows). It opens the Control Panel with `NINAIVU_HOME`, `NINAIVU_STATE_DIR`, `NINAIVU_AI_MODELS_DIR` and `NINAIVU_LOG_DIR` pointed into the folder, and ends. On first open it makes `data\` beside `app\`: the library's index and settings, `data\ai-models` and `data\control`, apart from the program so a newer folder can take them over (`ninaivu/desktop/portable.py`). |
| `app\` | The program: the private Python with the packages installed into it (`build.ps1`), ffmpeg, the readme and licence |
| `logs\` | `ninaivu.log` (the server's own account) and `server.log` (what it printed), so a log is a thing a person can find and send |

Nothing is installed and nothing outside the folder is touched, with one
exception that is the person's to choose: *Start when I sign in* writes a
registry value of its own (`Ninaivu (portable)`, pointing at
`Ninaivu.exe --autostart`, beside the `Ninaivu` an earlier installed Ninaivu wrote), and
turning it off removes it. Otherwise deleting the folder removes everything it
made. `build-portable.ps1` runs
after `build.ps1` in the same job of the release workflow (it reuses the
Python and packages `build.ps1` just assembled, so nothing is built or fetched
twice), signs `Ninaivu.exe` when the certificate is there, writes the zip
entry by entry with `/` in every name, and refuses to finish if its top level
is anything but those three.

## The Mac and Linux folders

The Mac zip holds one folder: `Ninaivu.app`, `README-PORTABLE.txt` and a
hidden `.ninaivu-portable`. Beside that file, the app's launcher
(`macos/launcher.sh`) keeps the index and settings in `data/`, the models in
`data/ai-models`, the Control Panel's files in `data/control` and the logs in
`logs/`; dragged on its own into Applications it keeps to `~/.ninaivu` and
Application Support, as an installed app would — so it also replaces an app
installed from an earlier `.dmg` without losing anything. macOS runs a
downloaded app from a hidden, read-only copy (App Translocation) until it is
told the folder is wanted. The launcher asks the Security framework where
that copy came from (`original_path.py`, with the app's own Python); when it is
a portable folder, it clears the folder's quarantine and opens the app again
from there. When it cannot tell, it refuses to start and says how
(`xattr -dr com.apple.quarantine <folder>`), rather than open an empty
Ninaivu elsewhere. The build zips the folder with `ditto`, which keeps the
bundle's links and signature, and when the secrets are there notarises the
zip and staples the ticket to the app inside.

The Linux `.tar.gz` holds `Open Ninaivu`, `ninaivu`, `app/` and `logs/`:
`app/python` is a python-build-standalone build with every package already
installed into it (the build runs on Linux of the architecture it is for and
installs the wheels with that Python), and `ninaivu` sets the same variables
as `Ninaivu.exe` and opens the Control Panel (`--tray`, `--server`, `--where`,
and `--add-to-menu` for Ninaivu in the application menu and on the Desktop).
`Open Ninaivu` is the double-click: a file manager opens a script in a text
editor but runs a program, so it is a small static program
(`linux/portable/open-ninaivu.c`) that runs the script beside it.

Every download carries the same Python: the CPython build
`python-version.txt` names, from python-build-standalone, checked against
`python-build-standalone.sha256`. Each also carries the tools its system
would otherwise need installed: ffmpeg and ffprobe (pinned builds,
`tools/bundle_ffmpeg.py`; not yet on arm64 Linux), libheif inside
pillow-heif for iPhone photos, Tk for the Control Panel, OpenSSL inside
`cryptography`, FriBiDi for Tamil text on Windows, and pip, so the AI models
page can add what it needs. `tools/check_portable.py` checks a built
download end to end, and two of them paired, on each system's runner.

## Building and releasing

`.github/workflows/release.yml` builds all three on a version tag and attaches
them to the GitHub release with a `SHA256SUMS.txt` and, for each, a
`*-packages.txt` listing every wheel inside with its SHA-256. A release is
made only from a commit whose tests passed, and never over one already
published (the first job of `release.yml`). Each is built from the versions
in `requirements/constraints-tested.txt`.

Signing needs the secrets the workflow lists at the top (a code-signing
certificate for Windows; a Developer ID certificate and an app-specific
password for macOS). Without them the workflow still builds everything,
unsigned, and the release notes say so — SmartScreen and Gatekeeper warn
about those. `SHA256SUMS.txt` is how to check a download either way
(docs/SECURITY.md).

To build by hand: `installers\windows\build.ps1` then
`installers\windows\build-portable.ps1` on Windows (needs
Python 3.12 and, for Tamil text, MSYS2), `bash installers/macos/build.sh` on an Apple-silicon
Mac (needs Xcode's command-line tools), `bash installers/linux/build.sh` on
x86-64 Linux with Python 3.12. Each prints the checksum and writes the
package list under its `build/` folder.

Until you have one of these, `start.cmd <folder>` (Windows) or
`sh launcher/start.sh <folder>` from the repository root runs Ninaivu from a
checkout, and `python -m ninaivu.desktop.tray` opens the tray from it.

See `docs/operations/production.md` for the full operations guide.
