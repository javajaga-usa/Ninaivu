# Installers

How to put Ninaivu on a machine.

| Folder | For |
| --- | --- |
| `windows/` | The Windows installer (`build.ps1` → pynsist → `Ninaivu-<version>-windows-x64.exe`), the winget manifests, and `install-service.ps1` for running Ninaivu as a Windows service |
| `macos/` | The macOS app (`build.sh` → `Ninaivu.app` in a `.dmg`, signed and notarised on a release) and the Homebrew cask |
| `docker/` | `Dockerfile`, `docker-compose.yml`. Build context is the repository root: `docker compose -f installers/docker/docker-compose.yml up -d` |
| `systemd/` | `ninaivu.service` for Linux |
| `caddy/`, `nginx/` | Reverse-proxy examples with HTTPS |

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
unsigned, and says so — SmartScreen and Gatekeeper will warn about those, so
releases are made with the secrets in place.

To build by hand: `installers\windows\build.ps1` on Windows (needs
`pip install pynsist`), `bash installers/macos/build.sh` on a Mac (needs
Xcode's command-line tools). Both print the checksum and write the manifest
for that version under their `build/` folder.

Until you have one of these, `start.cmd <folder>` (Windows) or
`sh launcher/start.sh <folder>` from the repository root runs Ninaivu from a
checkout, and `python -m ninaivu.desktop.tray` opens the tray from it.

See `docs/operations/production.md` for the full operations guide.
