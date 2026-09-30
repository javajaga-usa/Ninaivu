# Changelog

## Unreleased

- **Skin and Hair, rebuilt for the people in a family photograph.** The two
  tools in Sudar's Photo Studio, and the AI studio's portrait dialog that
  duplicated them badly, are now one engine and one panel. The old ones
  selected only the largest face, with an ellipse that included the eyes and
  the lips, found no hair at all, and in the AI studio took everything the
  colour of skin for skin and everything else that was not too bright for
  hair — the wall behind a head as much as the head. What replaced them:
  - *Every face, each against its own skin.* The household's face detector
    finds every face; each is judged against a model of *its own* skin, so a
    fair face and a very dark one in one frame are not pushed towards the same
    colour. A strip of faces across the top of the panel chooses **Everyone**
    or one person; a setting chosen for one person is theirs alone.
  - *Only skin.* Eyes, brows, lips and teeth are left out, by where they are
    and how far they are from that person's skin. Grey and white brows and
    moustaches are found by position, not by being dark.
  - *Marks worn on purpose are never touched.* A bindi (red or black), kumkum,
    sindoor in a parting, and sacred ash or sandal paste across the forehead
    are found and protected from every smoothing, recolouring and blurring
    operation — byte for byte — and are kept out of every average, so their red
    cannot leak into the skin beside them.
  - *No seams.* Light is brought to the face, the ears and the neck as one
    piece, and follows the real edge of the person rather than an ellipse round
    them (which left a pale halo on the wall beside the jaw); a colour is turned
    on the neck and ears as well as the face, so there is no line at the jaw;
    smoothing stops at the face. Skin in shade — the far cheek of a face seen a
    little from the side — is skin.
  - *Nothing lightens skin.* There is no fairness, whitening or "porcelain"
    setting anywhere, and the old presets of those names are gone. **Face
    light** is an exposure change for a face that is in shadow: it is offered
    only when the whites of that person's own eyes say the face is dim — never
    from how dark the skin is, so a dark face in front of a bright wall is not
    offered a lift — and it lifts skin of every colour by the same number of
    stops, so a dark face stays as dark as it is.
  - *Tools from what a family photograph needs, measured on the household's own
    faces:* face light, **skin tone** (turn a colour cast back towards skin, by
    hue only), even out the light (one side brighter), calm the shine, even out
    the tone, brighten under-eyes, soften (blemishes, not pores; folds, lids and
    moles kept), richness; for hair and beard, strands and shine, cover grey,
    and colour — black, dark brown, brown, chestnut, henna, burgundy or any
    other. Hair that cannot be told from what is behind it is *declined*, not
    guessed, and painted in by hand.
  - **Improve faces** gives each person what stood out about them and only
    that, in one step of Undo.
  - **Natural skin tone** in the Colour panel turns the cast of a whole
    photograph back by judging the skin in it, only as far as the edge of what
    skin of any complexion looks like.
  - Painting by hand where the tools missed or erred (Add, Remove), and all
    painted areas now belong to the photograph, not the window: they go with
    the picture when it is cropped, turned or straightened, which they did not
    before.
  - The retouch works a face at a time, in CIE Lab, on a window of the picture,
    so its cost follows the faces and not the photograph's size. The faces are
    found by `POST /api/portrait/analyse` (which replaces
    `/api/asset/<id>/portrait-masks`): the picture the studio is showing goes
    to the household's own computer and four small maps and some numbers come
    back. Nothing is stored and nothing leaves the house.
  - *An optional face-parsing network* is used for where skin and hair are — at
    the places colour cannot tell, like black hair against a black wall — when
    its ONNX model is installed as `faceparse/face_parsing.onnx` in the AI
    models folder. It runs through OpenCV, so nothing new is needed to run it;
    every answer is checked against the face it is for and a face it gets wrong
    is done the built-in way. Nothing downloads it yet, and nothing needs it.
  - In Tamil throughout.
- **Straightening no longer waits for approval.** What the automatic survey
  finds after a scan is now turned straight away, as one batch that **Undo
  last straighten** puts back exactly. Only what that survey has just found is
  turned, so a batch somebody undid is not turned again by the next scan, and
  a survey started with the button still waits for review. A second switch on
  Review → Straighten (`straighten_auto_apply`, on by default) keeps the old
  behaviour.
- **The code review of 29 September**, fixed:
  - *Library.* A rescan or a re-tag no longer clears an explicit-content flag,
    tags or a caption set by hand. Face regrouping no longer overwrites a
    confirmed or rejected face. An interrupted search rebuild finishes at the
    next start. A delete that fails to record puts the files back.
  - *Sign-in and sharing.* First-time setup through a tunnel or reverse proxy
    asks for the setup code. Share-link passwords, screen unlock, password
    changes and the password asked before deleting are all rate-limited
    without races. Signing out of the console leaves the gallery signed in.
    A deleted profile's albums and phone backups no longer pass to the next
    profile made. File names in Tamil are kept.
  - *Mugil.* A new computer no longer replaces the index copy in Drive, and a
    restore from Drive no longer writes outside the libraries. Resumed uploads
    are checked against Drive's checksum. Google's rate limits pause the run
    instead of failing files.
  - *Server.* Restarting no longer replays `--admin` or `--rescan`. The
    Windows service runs as the installing user, not SYSTEM. A restart under a
    service, systemd or Docker is left to the supervisor. Network access can
    no longer be switched off inside a container. Uploads are checked for
    decompression bombs before they are read.
- `start.cmd` installs Python 3.12 for the current user when none is found.
- The Control Panel window is back beside the tray: **Start - Ninaivu Control
  Panel.vbs** on Windows, and **Ninaivu Control Panel** on a Mac. On a Mac,
  **Setup Ninaivu.command** installs Python 3.12 if needed and builds both
  apps; **Ninaivu.command** starts Ninaivu.

## 0.1.0 — 29 September 2026

The first cut of Ninaivu as a product, carried forward from Hearth 2.0.0 (with
the fixes of the 28 September 2026 code review) under a new name.

- The package, the settings, the environment variables (`NINAIVU_*`), the state
  directory (`~/.ninaivu`) and the mDNS name (`ninaivu.local`) all carry the new
  name. There is no upgrade path from a Hearth state directory in this release.
- The Cloud tab is **Mugil** and the photo editor is **Sudar**.
- Hearth's Windows, macOS and shell launchers are gone. Ninaivu installs
  from the Windows installer or the macOS disk image, or runs from
  `launcher/start.py`, Docker or the service examples under `installers/`.
- The household-specific documents and audits were not carried over; the
  guide is new, in English and Tamil, on the docs site and as two PDFs.
- Python 3.12 is the floor.
- **The product audit of 29 September**, fixed:
  - *First run.* Making the first administrator from any device but the
    server itself asks for the setup code on both ports — the console used
    to count as local by itself, so anyone at home could claim a new install
    through port 3000. The code is ten characters and guesses are limited.
    The console now answers on this computer only until *Open this console
    from other devices at home too* is ticked on the Server page. A new
    install no longer greets you with "1 thing went wrong".
  - *Privacy.* Mugil encrypts by default and uploads nothing until the key is
    made, so neither photographs nor the index copy with everybody's names
    go up in the clear; switching it off says so. The launcher no longer
    installs PyTorch and fetches a model at the first start — search by
    description is offered on the first day and in Extras. Only an
    administrator can send a photograph to an extension that goes outside
    the house, unless family members are allowed on Settings.
  - *Install.* Stop and Restart work in installed copies (the stop helper is
    in the package). `ninaivu backup`, `restore`, `list-backups` and
    `reroot` replace the `tools/` scripts an installed copy did not have.
    Environment values no longer undo choices made in the console at every
    restart, and the Docker `.env` holds only folders and ports. Linux
    without root gets port 8080, not a random one. The optional
    requirements no longer put a second OpenCV or ONNX Runtime over the
    first. The launcher checks for Python 3.12. The Windows firewall hint
    prints the commands instead of a script that was never shipped.
  - *Screens.* The keyboard help is in Tamil too; English dates follow the
    browser's English; a tap-to-enter profile says who can open it; PIN
    lockouts grow each time; the Import page's engine stamp moved to a
    tooltip; Tamil day headings stay on one line on a phone; the version is
    on Settings; folder hints no longer name the Hearth household's drives.
  - *Docs.* Every claim the audit found wrong is corrected, a list of
    everything Ninaivu sends out on its own is on Remote access, and
    third-party notices are in `docs/THIRD_PARTY_NOTICES.md`.
- **The language follows the person.** Choosing English or Tamil is saved
  on the profile and applied at sign-in on every device; the browser's own
  language only decides what a device shows before anyone has signed in.
  The lock screen now has the language switch the sign-in card has, so a
  locked screen in a language you cannot read is no longer a locked door
  with no handle.
- **Two kinds of computer.** Ninaivu works out at start whether this is a
  Basic machine (2 GB, no graphics processor) or a Full one (a GPU or Apple
  silicon), and `--ai auto` means the light engine on Basic and the image
  model on Full; the expensive passes take smaller defaults on Basic. The
  Performance page says which and why; `hardware_tier` overrides it. CI
  runs the suite as both: in a 2 GB memory cgroup, and on an Apple-silicon
  runner with the model stack installed.
- **The docs site.** `docs/site/` — Install, The first day, Family and
  roles, Backup, Remote access, AI, Troubleshooting, and the guide (the
  family app, the console), rewritten from the current screens — built
  with MkDocs Material and published to GitHub Pages on every push to
  `main` (`.github/workflows/docs.yml`). The older documents are under
  *More*; the Hearth-era `user-guide.html` and `handbook.html` are gone.
- **No request once the model is here.** `HF_HUB_OFFLINE` and
  `HF_HUB_DISABLE_TELEMETRY` are set for the process as soon as the image
  model's weights are known to be on disk, so "nothing leaves the machine"
  is literal for the model libraries too.
- **Installers.** `installers/windows/build.ps1` makes a Windows installer
  with pynsist (a private Python, Ninaivu, the wheels, both extensions; the
  tray in the Start menu and at sign-in) and the winget manifests;
  `installers/macos/build.sh` makes `Ninaivu.app` in a `.dmg` for Apple
  Silicon and Intel and the Homebrew cask. `release.yml` builds all of
  them on a version tag, signs and notarises when the secrets exist, and
  attaches them to the GitHub release with checksums.
- **A tray instead of a window.** `python -m ninaivu.desktop.tray` puts
  Ninaivu in the system tray or menu bar with one menu: running or not,
  open, start, stop, restart, check for an update, the log, the HTTPS
  certificate, start at sign-in. The Tk control panel with its graphs and
  log pane is gone; the console's Server page has both. Needs `pystray`
  (`requirements/requirements-desktop.txt`).
- **Creative Studio is an extension.** The diffusion-model editor and the
  ComfyUI AI server — `generative_editing.py`, `ai_server/`, the AI server
  page — moved out of the core into `extensions/creative-studio`, found
  through the same entry point as Gemini and off until switched on. The core
  asks it for the heavy end of Sudar through one object (`studio`); without
  it, Sudar still edits light, colour, crops and looks, and removes objects
  and upscales with the small models it carries. Background image jobs
  (`media/jobs.py`) stay in the core, since the local upscaler uses them.
- **All settings, in one place.** System → All settings shows every setting
  in six groups (Library, People, Backup, Remote access, AI, Advanced) with
  its meaning and its default, the ten a household changes first; each is
  editable there, checked before anything is written. `server/config.py`
  stays one flat dataclass — the grouping lives in
  `server/settings_groups.py`, and a test keeps every field in exactly one
  group with a `#:` comment above it.
- **Straightening looks by itself.** After every scan, the survey runs over
  what the scan indexed and puts what it finds on Review → Straighten for
  approval; a switch on that page (on by default) turns it off, leaving the
  button. It remembers how far it has looked, so a second pass — or the one
  after every scan — no longer puts the whole library through the model
  again, and it decodes each photograph only as large as it looks at it
  (about 40 ms instead of 280 ms on a 24 MP JPEG, before the model runs).
- **Import is the first Library page**, ahead of Library settings: a library
  usually begins with the drives.
- **Google Photos comes across whole.** Importing a Takeout export already
  took the dates from its JSON sidecars; now the location (often the only
  place it survives, since the export strips it from many files) and the
  description typed under a photograph go into the index too, and the
  albums come back: once the archive is in the library, the Import page
  offers to make every album folder it found as an album, from the copies
  the import kept, without reading the export a second time.
- **The first day.** Right after the administrator is made, the console
  walks through the five things a new library needs — the folder, the
  household, what the scan works out by itself, a copy outside the house,
  the address for the phones — each step skippable, each using the page's
  own routes. It opens once; an established library never sees it.
- **Remote access is a choice, not an assumption.** Server → Away from home
  picks Tailscale, WireGuard (with its subnets), a Cloudflare Tunnel, the
  household's own reverse proxy, or nothing; each says what it still needs.
  Which addresses count as away from home, which are listed for the
  household, and whose certificate is served all follow the choice. The
  default works out Tailscale by itself, as before.
- **An update check.** Once a day Ninaivu asks GitHub's releases page whether
  a newer version is out — one plain request with nothing about this computer
  in it — and the Server page says so with a link. A switch there turns it off.
- **The console says only what is true on this machine.** Each AI switch
  shows the model it needs and whether it is installed, with a link to it;
  the search box promises "last summer", which a calendar reads, and not a
  place name until place names are on; every Overview card opens the page
  behind its number; Live Photos and On This Day appear in the family sidebar
  only once there is something behind them.
- **Ninaivu's own mark**: a roof over a photograph, drawn in code
  (`tools/generate_icons.py`), in every size and splash screen.
- **A tour of the screens** (`docs/screens.md`): every page pictured from a
  generated sample library, with what it is for. The Overview puts "Needs
  you" — the queues, counted — above the numbers, and the safety checks
  point at the Health page they are about.
- **A tidy root.** `start.cmd` is the one file at the root besides what Git,
  GitHub and pip need there. The launcher is `launcher/start.py` (with
  `start.sh` beside it), the pins are under `requirements/`, and the
  changelog, roadmap, contributing and security notes are under `docs/`.
- **The console, arranged by what each page is for.** Every switch sits on
  the page of the thing it governs: the rules for everyone (who may browse
  without signing in, what is screened, how far back each role sees) open the
  Visibility page; indexing is on Library settings; the AI passes (places,
  text, faces, video moments) are on AI models beside the models they use;
  System → Settings keeps this home's name, the extensions and what is
  installed. Restoring from the cloud copy has its own page under Backup &
  health, apart from the everyday Mugil page. Library comes straight after
  Home in the sidebar, with Import as its first page.
- **Extensions.** `ninaivu/extensions.py` finds extension packages through the
  `ninaivu.extensions` entry point; the console lists them under AI models →
  Extensions with what each sends off the machine, and every one is off until
  an administrator turns it on. **Gemini is the first**, moved out of the core
  into `extensions/gemini/`. Sudar's generate route no longer falls back to
  Gemini when no local model is installed: a request that names no provider
  stays on this machine.
