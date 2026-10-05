# Changelog

## Unreleased

From [Hearth](https://github.com/javajaga-usa/Hearth), the project Ninaivu
grew from: what Hearth added after the fork that Ninaivu did not have. None
of it changes the library's folder layout. The console pages for these are
still to come; until then they are switched on in **Settings** or through
the API.

- **Backup rules and large-file approval.** Mugil can leave out videos (or
  everything but pictures), files over a size, and folders or words in a
  path. A file over 1 GB (`cloud_approval_mb`) waits for an administrator's
  yes, and the household is told, at most twice a day, when files wait.
- **A second copy on another disk or NAS**, made on a schedule and checked
  against its own record, never removing what was removed at home; and a
  restore from it.
- **An encrypted off-site copy** to an S3-compatible bucket or a folder,
  encrypted with the Mugil key, with `ninaivu offsite-restore` to bring it
  back on any computer without Ninaivu running.
- **Repairing damaged files.** The storage check runs on a schedule
  (`scrub_every_days`, 30 by default) and, with `scrub_repair`, puts a file
  whose bytes changed without an edit back from the second copy or Mugil,
  but only from a copy that hashes to what the file was. The damaged file is
  kept in the state folder.
- **"Every photograph in more than one place"**, a new safety check, and a
  copies report saying which photographs exist only on this computer.
- **Search in plain words.** In "Maya and Arjun at Ooty", names the library
  knows become people and a town it has photographs from becomes a place
  (`phrase_search`); the rest goes to the ordinary search, and the results
  say what was understood.
- **Smart albums**: saved searches that fill themselves.
- **Storage report**: what takes the space, by folder, kind and year.
- **No home location leaves the house.** For family members, downloads, zips
  and the viewer drop the location from photographs and videos taken near
  home (`strip_location`, `home_lat`, `home_lon`, `home_radius_m`);
  administrators get the original, and guests never get a location.
- **XMP sidecars, opt-in** (`xmp_sidecars`, off by default): ratings,
  favourites, people and hand-written captions written beside each
  photograph as `IMG_1234.jpg.xmp` for other programs to read.
- **Anything plays.** Any video or sound the browser cannot open is
  converted, and `/api/stream` plays the conversion as it is made instead of
  waiting for the finished copy.
- **Importing Google Photos, iCloud and WhatsApp exports.** The zips as
  downloaded are read in place; new photographs are filed into the library's
  date folders, anything already here (by hash) is not copied again, and
  dates, places, descriptions, favourites, hidden and albums come across.
  Something deleted in iCloud stays deleted. Console only.

Fixed while porting, so not carried over from Hearth: a stopped backup run
no longer ends the schedule of the second copy, off-site copy, repair or
sidecars; a restore of a folder with `_` in its name no longer brings back
its neighbours; the large-file notice is actually sent; a generated caption
is no longer written into a sidecar as if somebody had typed it.

From [Ninaivu Lite](https://github.com/javajaga-usa/Ninaivu-lite): what Lite
learned that Ninaivu had not.

- **Moving up from Ninaivu Lite.** `ninaivu import-lite lite-export.json`
  reads the file Lite's `--export` writes and brings across people (with
  their passwords and PINs, which Lite hashes the way Ninaivu checks them),
  folder rules, each photograph's own visibility, favourites, albums and share
  links, whose tokens are kept so a link already sent keeps working. Run it
  with Ninaivu stopped, after its first scan of the same folders. Nothing in
  Ninaivu is overwritten, and photographs it cannot find are listed.
- **Importing old drives, cards and phone backups, as in Lite.** Before the
  first import the archive's folder is suggested as "Ninaivu Archive" inside
  the first library folder, so what comes in is shown to the family once it
  is indexed (it is stepped around when the library is itself a source).
  An archive inside a library counts as in the library, so it is not offered
  or added a second time. A source or destination typed without its full
  path is refused rather than read from wherever Ninaivu started. Every
  refusal and notice on the Import page now reaches the page as a sentence
  it can translate, and Tamil has them all.
- **A way back in for a forgotten administrator password.**
  `ninaivu reset-password NAME` on the computer Ninaivu runs on sets a new
  password, turns the profile back on and signs it out everywhere.
- **Share links show a turned photograph upright.** A photograph turned by
  hand, or put right during the scan, opened on its side for a visitor: the
  share page does not turn pictures, so the turn is now baked into the
  visitor's copy.
- **A video carries no location for a guest or a share-link visitor.** A
  phone writes where a video was shot into the file, as it does for a photo.
  With ffmpeg, guests and visitors get a copy with every metadata atom left
  behind and the streams copied as they are (kept in the state folder);
  without it, the original as before. An iPhone's live clip too.
- **A turn by hand survives a rescan in the thumbnails.** The index kept the
  turn, but a full rescan wrote the thumbnails and the shape from what the
  scan decided.
- **The server, tightened.** Waitress refuses an upload over the limit before
  reading it, a connection idle for a minute is closed (it was five), a JSON
  body over 4 MB is refused unread, a 413 answers as JSON, and a
  Permissions-Policy header denies the camera, microphone, location and
  payment.
- **Lighter first visit.** The pages, scripts, styles and Tamil strings are
  sent gzipped (once per version, kept in memory), not only the API's JSON.
- **Profile pictures.** A replaced picture gets a new address (it kept the
  old one for a day in every browser), is written whole before it is used,
  is turned the way the phone held it, and an administrator can take
  someone's picture down (`DELETE /api/people/<id>/avatar`).
- **Two thumbnail writers no longer share a temporary file.**
- **Windows system folders are refused on whichever drive Windows is on,**
  not only on C:.
- **Import:** an archive is never written into Ninaivu's own data folder,
  and a source that is already in the library says that adding the archive
  as well would show those photographs twice.
- **The update check waits to be asked.** It is off on a new installation
  until it is turned on on the Server page; the tray's *Check for an update*
  asks once.
- **Windows installer.** It asks a running Ninaivu to stop before writing
  anything and, while files are still in use, asks for the Control Panel to
  be closed, with Retry, instead of failing half-way; an upgrade removes the
  previous program files first (the data and the photographs are never
  touched); the uninstaller stops Ninaivu and its start at sign-in; the
  file's properties name the product, its version and
  © 2026 Jagadeesh Rajendran. The icon is plain bitmaps, which NSIS can read
  (it showed blank). `python -m ninaivu.desktop.control --stop | --status`
  is what it uses.
- **Launchers.** `start.cmd` waits on an error instead of closing with the
  reason, and the launcher brings an existing `.venv` up to date when the
  packages it asks for change (it only checked that each one imported).

## 0.1.2 — 1 October 2026

- **No black windows flashing up on Windows.** Opened from the Control Panel,
  the tray or at sign-in, or restarted from the console, Ninaivu runs without
  a console of its own, and Windows gave every tool it ran — ffprobe,
  PowerShell, netsh, OpenSSL and the rest — a console window that opened and
  closed with it. Those tools now run hidden. Started from `start.cmd`,
  nothing changes: their output still shows in that window.
- **The Control Panel opens at a size that fits.** It is sized to what is in
  it, at the display's scaling, rather than to fixed pixels. A running
  server's addresses wrap beside the buttons instead of stretching the window
  almost to the width of the screen, and the window grows once its first
  readings are in, and when the log opens, instead of cutting off the bottom
  row.

## 0.1.1 — 1 October 2026

- **The Control Panel is what the installers open.** The window that starts
  and stops Ninaivu and shows its readings and log is the Start menu entry
  and a Desktop shortcut on Windows (the installer now carries Tk for it), the
  Ninaivu app on a Mac (in the Dock while it is open), and *Ninaivu Control
  Panel* in the applications menu and on the Desktop on Linux and a Raspberry
  Pi. The tray is beside it for those who want it: *Ninaivu tray* in the
  Start menu folder, `open -a Ninaivu --args --tray`, `ninaivu-tray`. A
  Python without Tk opens the tray instead of nothing.
- **Installers for Linux and Raspberry Pi, and no source in any installer.**
  `Ninaivu-<version>-linux-amd64.sh` for a PC and `-arm64.sh` for a Raspberry
  Pi 4 or 5 with a 64-bit OS: one file, run with `sh`, no root needed, that
  carries its own relocatable Python (the machine needs none), every package
  for that architecture, and Ninaivu as bytecode. It makes a `ninaivu`
  command, a desktop entry and a systemd service pointed at the photographs
  folder, starts it, and upgrades in place. The Windows and macOS installers
  already carried their own Python; all three now ship Ninaivu and its
  extensions with the Python compiled away (`installers/strip_sources.py`) —
  the web files a browser needs stay as they are. The release workflow builds
  all of them on a tag and attaches them to the GitHub release. Published by
  Jagadeesh Rajendran.

- **Look younger, in one tap, with nothing redrawn.** A button with a strength
  slider at the top of the Skin panel, in the Photo Studio and in Sudar's
  retouch studio, for one person or everyone: smoother and more even skin,
  calmer shine, brighter under-eyes, a little of the skin's own colour back;
  grey covered, strands brought out, thin hair filled. It sets the existing
  sliders, so every one of them can still be changed afterwards, and a tool
  already set higher by hand is left alone. It has no face light and no tone
  in it, so it cannot lighten skin or turn its colour, and the engine's test
  holds it to that on a deep complexion. There is no age model in Ninaivu: the
  ones that exist redraw the face and lean towards lighter, Western features,
  which is the opposite of the promise these tools make.
- **Hair: added, not only thickened — three ways, checked on a real photograph.**
  On a portrait with dark hair in front of a dark background the hair finder
  declined, by design, and every Hair slider then did nothing; and nothing in
  Ninaivu could put hair where there was none. Now:
  - *Add hair, on this machine.* A third brush beside Add and Remove in the
    Hair panel of the Photo Studio and of Sudar's retouch studio: paint where
    there should be hair — a receding hairline, a thin crown — and it is drawn
    from the strands beside it. Each painted pixel takes the colour of its
    mirror image across the nearest edge of real hair, so the new hair runs on
    from the old with the same strands, colour and shine; an *Added hair* slider
    says how much shows, and the grown hair is hair for every other tool. No
    model is needed. Held by the engine's tests.
  - *Add hair, generative.* A new AI server purpose, *Paint and ask (inpaint)*
    — the photograph, a painted area and a request — and a Magic tool in Sudar
    that uses it: paint where the hair should be and the AI server draws this
    person's own hair there. Shown only when a workflow is assigned.
  - *A brush in Sudar's retouch studio* at all: it had none, only a note
    sending people to the Photo Studio. Add, Remove and Add hair, a brush size,
    a colour-following option and "Show what is selected"; a stroke that strays
    onto the face is kept off the skin by the skin and body maps.
  - *Hair the colour of its background is found with the background-removal
    model's help.* When colour cannot tell black hair from a dark wall and the
    household has downloaded Background removal (RMBG-1.4), its foreground
    says where the wall is and the colour decides the rest; the model is asked
    only for a face that needs it.
  - *Fuller hair, rebuilt.* A thin place is judged by its neighbourhood as well
    as the pixel — lighter than the dark strands, hair all round, strands
    crossing it — and gaps are brought most of the way down to the strands'
    tone with their texture kept. A smooth patch of shadowed skin, a stray dab
    or a lone pixel the maps missed no longer becomes a dark spot.
- **The Creative Studio, brought up to the rest of Sudar.** Photographs come
  from the library as well as from files (*Add from library*, with paging);
  a postcard, collage, selective-colour picture, versions sheet, recipe or
  calendar can be saved into the library like an edit, an administrator's at
  once and a family member's into the approval queue; the picture creations
  come in three shapes — landscape, square, and a 9:16 story; a collage has
  three layouts — grid, one large with the rest beside it, and Polaroid prints
  with their captions, each a little askew; and the postcard is a postcard,
  the photograph filling the card with the words on a band that darkens
  towards the foot. The browser test covers all of it.
- **Sudar's Enhance tools are always on the page, and Colourise works on this
  machine.** The *Enhance tools* card (Upscale, Restore faces, Colourise) was
  hidden whenever no model had been downloaded and no AI server workflow was
  assigned, so a household that had not set anything up never learned the
  tools existed; and Colourise had no local path at all, only a ComfyUI
  workflow. The card now shows every tool, greys out the ones that are not set
  up, and says under them which model each needs and who can add it. The
  capabilities route carries this as `enhance_tools`.
  - *Colourise on this machine* (`ninaivu/media/onnx_tools.py`): a DDColor
    ONNX model is shown the photograph's lightness as a grey square and
    answers with colour, which is laid under the photograph's own lightness at
    full size, so every edge and every grain is the original's. The catalogue
    cannot pin that model's bytes yet, so it is the one model placed by hand:
    `ddcolor.onnx` in the models folder's `onnx` subfolder, or wherever
    `NINAIVU_COLORIZE_MODEL` or the `colorize_model` setting points. Nothing
    leaves the machine.
  - *Revive old photo*, one tap when Restore faces and Colourise are both set
    up: the faces first, then colour for the whole print.
- **Every result Sudar makes can be saved into the library.** A generated
  preview, a cut-out, a blurred background, a colour pop, an upscaled or
  restored or colourised picture: each had only *Download*. Each now has
  *Save to library* beside it, on the same route as the slider edit, so an
  administrator's copy is published beside its source and a family member's
  waits in the administrator's queue of uploads awaiting approval. Object
  removal and Clothing colour gain *Apply to photo*, which puts their result
  on the canvas where the sliders, Save and Download already are.
- **Hair: Fuller hair, and hairstyles.** The skin-and-hair engine gains
  *Fuller hair*, which closes the light gaps where scalp shows between strands
  towards the hair's own tone, in proportion, leaving the glints and reaching
  nothing outside the hair map — in Sudar's retouch studio and the Photo
  Studio's Hair panel alike, with the engine's tests holding it to that. A new
  hairstyle (a bob, a fringe, curls, a braid, a bun, a beard, thicker hair) is
  drawn rather than adjusted, so Sudar offers ten of them as generative ideas
  when the Creative Studio extension or Gemini is on, and says so plainly when
  neither is; a request for one no longer lands in the retouch studio, which
  can only work on the hair that is there.
- **Clothing colour is back.** The brush-and-recolour studio was in the
  code with its tests and its Tamil, and nothing opened it. It is a Magic
  tool again, "make her sari red" opens it, and it knows a sari, a kurta, a
  veshti and a dupatta as well as a shirt.
- **The sliders people reach for first.** Sudar gains *Vibrance* (richer
  colour that leaves skin alone), *Clarity* and *Dehaze*, all from the shared
  engine; a *Story · 9:16* crop for stories and reels; an *HDR* look; a
  *Colour pop* background tool (the subject in colour, the rest in black and
  white); and a *Clear the haze* suggestion for a bright, flat scene. The
  built-in planner, the local language model and the Gemini planner speak the
  new words too ("dehaze, more vibrant and crop to 9:16"). *Improve colors*
  and *Auto enhance* now use vibrance rather than saturation. The slider that
  was labelled *Color tint* is *Saturation*, which is what it always did.
- **The guides, in both languages, brought up to date and rebuilt as PDFs.**
  The family guide said *Edit photo* opened Sudar; it opens the Photo Studio,
  which the guide never described. It now has its own section (light with the
  tone curve, colour with the mixer and Natural skin tone, detail, the ten
  looks, crop and export, and where a copy goes), and the Tamil pages carry
  everything the English ones gained since 0.1.0: the straightening that
  turns photographs by itself, Skin and hair, the Python install on Windows
  and Mac, and today's Sudar changes. Both PDFs are rebuilt from the pages.
- Reopening Sudar in the moment after closing it did nothing: the closed
  dialog was still in the page until its close event fired, and counted as
  open. Only an open one does now.

- **Real editing in the Photo Studio, on one engine shared with Sudar.** The
  Photo Studio did its sums on the numbers in the file rather than on light,
  and Sudar had a second, cruder engine of its own in which "saturation" meant
  something else. Both editors now hand one recipe to one engine
  (`ninaivu/static/js/studio/develop.mjs`, with the recipe and the looks in
  `recipe.mjs`), so a slider means the same thing wherever it is moved.
  - *Light that behaves like light.* Exposure and white balance are gains on
    linear light (40 on the exposure slider is one stop), and white balance
    changes the colour of a grey without changing its brightness. Positive tint
    is now magenta, as the slider's label always said.
  - *Shadows and highlights without halos.* Each part of the photograph is
    judged by its neighbourhood, through an edge-aware guided filter worked out
    once at low resolution, so a face in front of a bright window can be opened
    up without a pale ring round it, a grey sky above it, or its texture
    flattened; the lift is applied as a ratio, so the face keeps its colour.
    Whites, blacks, an S-curve contrast that holds black and white, and a new
    **Dehaze** (dark-channel) join them.
  - *A tone curve* over a live histogram — RGB and per channel, monotone, with
    points added by clicking and removed by dragging them off the square — and
    a histogram at the top of the Light panel.
  - *Colour in OKLab.* **Vibrance** and **Saturation** are separate sliders
    again; vibrance knows where brown skin of every complexion sits on the
    colour wheel and leaves it nearly alone. An eight-band **colour mixer**
    (hue, saturation, luminance), **black and white** whose tones come from the
    mixer's luminance sliders, and **split toning**.
  - *Detail sized to the real photograph.* Clarity (now also negative, to
    soften), sharpening on lightness only with radius and masking, luminance and
    colour noise reduction, film grain, and a vignette with midpoint and feather.
    A little dither before the last rounding keeps skies from banding.
  - *Looks for family photographs*, ten of them, shown as small pictures of the
    photograph being edited and applied with an amount slider on top of the
    person's own sliders: Natural, Festival, Golden hour, Soft portrait,
    Backlit rescue, Revive old print, Film, Monsoon, Classic and Warm black and
    white. The engine's tests hold every one of them to the promise about skin:
    none raises its lightness or drains its colour.
  - *Crop* gains flip across and flip upside down; *export* gains a size (full,
    3840, 2048 or 1080 pixels on the long edge), reduced in halving steps so
    fine patterns do not shimmer.
  - *24-megapixel photographs in strips.* The engine works a hundred rows at a
    time with only the neighbourhood each step needs, and the soften brush blurs
    only the painted rectangle: the old worker allocated about 400 MB of floating
    point for every blur at that size. Softening now also keeps every other
    adjustment, where it used to blend back towards the unedited photograph.
  - *Sudar* renders through the same engine, keeping its own names for
    adjustments (they are what Gemini and its suggestions speak) mapped onto the
    engine's. **Balance this photograph** (`ninaivu/media/enhance.py`) answers
    in the new vocabulary: exposure in stops, `vibrance` rather than
    `saturation`, and magenta for a green cast.
- **Edited copies keep what the camera wrote.** Saving an edit used to write a
  file with only its date, and a database row with only its folder and date, so
  the copy lost its camera, lens, exposure settings and place. The copy's EXIF
  now carries the source's make, model, lens, exposure, ISO, focal length,
  flash, white balance and the whole GPS block; orientation is set to 1 because
  the turn is already in the pixels, Software names the Photo Studio, and the
  maker note, thumbnails, serial numbers and owner name are left behind. The
  row copies camera, lens, ISO, aperture, shutter, focal length, coordinates,
  country and city. Both the administrator's direct save and a family member's
  save that waits for approval.
- **No more 404 for the Tamil font on Mac and Linux builds.** Every page named
  `fonts/NotoSansTamil.ttf`, which only the Windows build carries, so every
  other build asked for it and logged a 404 on every page. The stylesheet now
  names only the device's own Tamil font, and the faces that use the bundled
  file live in `css/tamil-font.css`, which the pages link only when the file
  is there; the share page does the same.
- The editing engines' Node tests now run with `pytest`
  (`tests/test_develop_engine.py`), so CI holds them too.

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
