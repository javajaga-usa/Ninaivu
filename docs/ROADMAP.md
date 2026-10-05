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
- The tray (`desktop/tray.py`, pystray) in place of the Tk control panel:
  start, stop, restart, open, update check, log, certificate, start at
  sign-in. `desktop/control.py` is still what starts and stops the server.
- The settings in six groups (`server/settings_groups.py`) and the console's
  All settings page, the ten a household changes first.
- `extensions/` exists with its contract, and the first extension: Gemini,
  moved out of the core with the switch, the statement of what leaves the
  machine, and the tests. Sudar's own routes no longer fall back to it.

Still to do in Phase 0:


## Phase 1 — installable by a stranger (1.0, released)

- ~~Windows installer and a winget manifest; signed.~~ Done: `installers/windows`
  (pynsist), signed by `release.yml` when the certificate secret is set;
  the winget manifests come out of the same build.
- ~~macOS `.app` with notarisation and a Homebrew cask; signed.~~ Done:
  `installers/macos`, both architectures, notarised by `release.yml` when
  the Developer ID secrets are set. Still to do for either: the first
  signed release itself, which needs the certificates bought and the
  secrets added.
- ~~Basic and Full hardware tiers, both tested in CI.~~ Done:
  `server/tiers.py` measures the tier at start (Basic: 2 GB, no GPU; Full: a
  GPU or Apple silicon) and decides what `--ai auto` means; the Performance
  page says which and why; `tests.yml` runs the suite in a 2 GB cgroup and
  on an Apple-silicon runner with the model stack.
- ~~Docs site (MkDocs Material on GitHub Pages).~~ Done: `mkdocs.yml`,
  `docs/site/` (Install, The first day, Family and roles, Backup, Remote
  access, AI, Troubleshooting), published by `docs.yml`. The README is the
  short version and points there.
- ~~`HF_HUB_OFFLINE` once models are cached.~~ Done: set for the process the
  moment the weights are known to be on disk.

1.0.0 was released on 5 October 2026, unsigned. Still open in Phase 1: the
first signed release (certificates and secrets), and the winget and Homebrew
submissions once it exists. The test for it stays the same: a person who has
never seen the README reaches their gallery in ten minutes.

## Phase 2 — leaving the big clouds is easy

- Mugil back-ends beyond Google Drive: S3-compatible (Backblaze B2, Wasabi),
  WebDAV (Nextcloud), a second disk or NAS path. One provider interface.
  Partly done: a second copy on another disk or NAS, and an encrypted
  off-site copy to any S3-compatible bucket, run beside Mugil as their own
  copies. WebDAV, and Mugil itself on more than Drive, are still open.
- ~~iCloud Photos~~ and Amazon Photos import. iCloud, Google Photos and
  WhatsApp exports are done (`storage/importer.py`); Amazon Photos is open.
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
