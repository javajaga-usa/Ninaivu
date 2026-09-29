# Roadmap

Ninaivu 0.1.0 is Hearth with a new name and the household-specific parts
removed. This is the path from there to a product a stranger can install.
Phases are in order; the items inside each are not.

## Phase 0 — separate the product from the household

Done in 0.1.0:

- New repository, package and names (`ninaivu`, `NINAIVU_*`, `~/.ninaivu`, `ninaivu.local`).
- The Cloud tab is Mugil; the photo editor is Sudar.
- Household documents, audits, the illustrated PDF and the launcher scripts are gone.
- Python 3.12 floor; CI on 3.12 and 3.14 across Linux, Windows and macOS.
- `installers/` holds Docker, systemd, Caddy, nginx and the Windows service installer.
- Remote access is a provider (`server/remote.py`): Tailscale, WireGuard, a
  Cloudflare Tunnel, the household's own reverse proxy, or nothing, chosen
  on the Server page; each says what it needs.
- An update check, once a day, one plain request to GitHub's releases page
  with nothing about the machine in it, switchable off on the Server page.
- The icons and splash screens are Ninaivu's own mark (`tools/generate_icons.py`).
- The first-day walk-through in the console (`static/js/first-day.js`).
- Creative Studio (`extensions/creative-studio`): generative editing and the
  ComfyUI AI server out of the core, behind one `studio` object. The small
  models (faces, orientation, the local remover and upscaler, SigLIP) stay.
- The settings in six groups (`server/settings_groups.py`) and the console's
  All settings page, the ten a household changes first.
- `extensions/` exists with its contract, and the first extension: Gemini,
  moved out of the core with the switch, the statement of what leaves the
  machine, and the tests. Sudar's own routes no longer fall back to it.

Still to do in Phase 0:

- **Drop the Tk control panel** (`desktop/`) in favour of a tray application
  once an installer exists to ship it in.

## Phase 1 — installable by a stranger (1.0)

- Windows MSI (WiX or pynsist) and a winget manifest; signed.
- macOS `.app` with notarisation and a Homebrew cask; signed.
- Tray application replacing the Tk panel: start, stop, open, update.
- Basic and Full hardware tiers, both tested in CI: Basic is 2 GB RAM and no
  GPU (gallery, faces, search on a small model); Full is a GPU or Apple
  Silicon (everything).
- Docs site (MkDocs Material on GitHub Pages) replacing the long README:
  Install, First day, Family and roles, Backup, Remote access, AI,
  Troubleshooting.
- `HF_HUB_OFFLINE` once models are cached, so "no telemetry" is literal.

Release 1.0 when a person who has never seen the README reaches their gallery
in ten minutes.

## Phase 2 — leaving the big clouds is easy

- Mugil back-ends beyond Google Drive: S3-compatible (Backblaze B2, Wasabi),
  WebDAV (Nextcloud), a second disk or NAS path. One provider interface.
- iCloud Photos and Amazon Photos import.
- Translation platform (Weblate or Crowdin) with five languages.
- App-store listings for Unraid, TrueNAS and CasaOS.
- Share links with an expiry date and a download toggle in the dialog.
- Opt-in crash reports that show the person exactly what would be sent.

## Phase 3 — the paid layer

- A mobile companion app for background phone backup into Mugil.
- A hosted relay for remote access without setting up Tailscale.
- Support plans.

The server stays MIT and never gets a licence check.

## Not planned

- A front-end rewrite in a framework.
- Accounts beyond the household (teams, organisations).
- Hosted AI models.
