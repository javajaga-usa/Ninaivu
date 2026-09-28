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
- `extensions/` exists with its contract, and the first extension: Gemini,
  moved out of the core with the switch, the statement of what leaves the
  machine, and the tests. Sudar's own routes no longer fall back to it.

Still to do in Phase 0:

- **Move generative editing and the ComfyUI server out of the core.**
  `media/generative_editing.py`, `media/inpaint.py`, `media/model_catalog.py`,
  `ai_server/` and `api/ai_server_api.py` are woven into `ai.py`,
  `server/config.py`, `api/admin_api.py` and `media/faces.py`. They stay in the
  package for 0.1.0 and only run when a model has been installed on purpose.
  The extraction is a refactor with its own tests, tracked as one issue.
- **Remote access as a provider, not an assumption.** `utils/tailnet.py` and
  the "away from home" rule in `server/workload.py` assume Tailscale. Define a
  small interface (is this address inside the house? what addresses work from
  outside?) with Tailscale, WireGuard, Cloudflare Tunnel and "my own reverse
  proxy" behind it.
- **Group the settings.** The console's switches now sit on the pages they
  govern (done in 0.1.0); what remains is `server/config.py` itself: 130
  fields into six groups (Library, People, Backup, Remote access, AI,
  Advanced), with the ten a household changes on the first screen and the
  rest under Advanced with the defaults they have today.
- **Drop the Tk control panel** (`desktop/`) in favour of a tray application
  once an installer exists to ship it in.

## Phase 1 — installable by a stranger (1.0)

- First-run wizard: library folder, first administrator, family members with
  PINs, backup on or off and where, AI models yes or no. Six screens.
- Windows MSI (WiX or pynsist) and a winget manifest; signed.
- macOS `.app` with notarisation and a Homebrew cask; signed.
- Tray application replacing the Tk panel: start, stop, open, update.
- Update check against the release feed (one request, no identifier) and an
  "update available" notice.
- Basic and Full hardware tiers, both tested in CI: Basic is 2 GB RAM and no
  GPU (gallery, faces, search on a small model); Full is a GPU or Apple
  Silicon (everything).
- Google Photos Takeout import, with its JSON sidecars for dates and albums.
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
