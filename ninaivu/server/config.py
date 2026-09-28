"""Configuration for Ninaivu.

Settings resolve in this order (later wins):
  1. Defaults below
  2. ``config.json`` in the state directory
  3. ``NINAIVU_*`` environment variables
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import MISSING, dataclass, field, fields, asdict
from pathlib import Path
from typing import Any


#: Settings that say how *this process* was started rather than what the
#: household wants kept. The command line and the ``NINAIVU_*`` variables own
#: these, and writing them down would make a one-off ``--port 8080`` permanent
#: — and a state directory written into a file inside itself.
RUNTIME_ONLY = frozenset({
    "state_dir", "host", "port", "admin_host", "admin_port", "debug",
    "server_threads", "trusted_proxies", "max_upload_mb",
})


#: Serialises ``Config.save`` across threads; see there.
_SAVE_LOCK = threading.Lock()


def _field_defaults() -> dict[str, Any]:
    """Every setting's name and the value it has when nobody has chosen one."""
    out: dict[str, Any] = {}
    for f in fields(Config):
        if f.default is not MISSING:
            out[f.name] = f.default
        elif f.default_factory is not MISSING:
            out[f.name] = f.default_factory()
    return out


def _jsonable(value: Any) -> Any:
    """A value as JSON will hold it, so that comparing to a default works.

    A tuple and the list it is read back as are not equal, and neither are a
    set and its contents in some other order — which without this would make
    ``thumb_sizes`` and ``ignore_dirs`` look changed on every single save.
    """
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(str(v) for v in value)
    if isinstance(value, (tuple, list)):
        return [_jsonable(v) for v in value]
    return value


def _default_state_dir() -> Path:
    env = os.environ.get("NINAIVU_STATE_DIR")
    if env:
        return Path(env).expanduser().resolve()
    base = os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base).expanduser().resolve() / "ninaivu"
    return Path.home() / ".ninaivu"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif",
    ".heic", ".heif", ".avif", ".jfif", ".ico",
}
RAW_EXTS = {".cr2", ".cr3", ".nef", ".arw", ".dng", ".orf", ".rw2", ".raf", ".srw"}
VIDEO_EXTS = {
    ".mp4", ".m4v", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv",
    ".mpg", ".mpeg", ".3gp", ".ts", ".mts", ".m2ts",
}
AUDIO_EXTS = {
    ".mp3", ".wav", ".flac", ".aac", ".ogg", ".oga", ".m4a", ".wma",
    ".opus", ".aiff", ".alac",
}

#: Extensions a browser can play/render directly.
BROWSER_NATIVE = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif", ".ico", ".jfif",
    ".mp4", ".m4v", ".webm", ".mov",
    ".mp3", ".wav", ".ogg", ".oga", ".m4a", ".opus", ".flac", ".aac",
}


@dataclass
class Config:
    """Runtime configuration."""

    # --- Library ---------------------------------------------------------
    #: Every library folder Ninaivu indexes. Members can be assigned one of
    #: these, or any folder inside one.
    roots: list[str] = field(default_factory=list)
    #: The folder the console shows by default. Always one of :attr:`roots`.
    active_root: str | None = None
    #: Confine the folder picker to :attr:`roots` and nothing else. Off by
    #: default: the picker is behind an admin sign-in, and an admin who cannot
    #: reach their own Pictures folder cannot set the library up. Turn it on
    #: with ``--lock-roots`` when the console is exposed beyond your machine.
    lock_roots: bool = False
    #: Where new files go when the library folder they belong in cannot be
    #: written — an NTFS drive on a Mac, which macOS mounts read-only. Edited
    #: copies, approved uploads and phone backups land here instead, in the
    #: same folder layout, and it joins the library folders on first use so
    #: they appear in the gallery. See ninaivu/storage/new_files.py.
    new_files_folder: str = "~/Pictures/Ninaivu"
    follow_symlinks: bool = False
    #: Index media found in hidden files and folders, but hide it by default.
    #:
    #: Somebody went to the trouble of hiding it. Skipping it entirely loses
    #: photographs the household may still want; showing it in the gallery
    #: publishes what was deliberately put out of sight. Indexing it as
    #: *hidden* — admin-only, and visible in the console with a reason — is the
    #: only reading that respects both. Set False to skip hidden items again.
    index_hidden: bool = True
    #: Smallest file, in bytes, that counts as a real photo or video.
    #:
    #: A folder of old media is full of things that are technically images:
    #: icons, sprites, email signatures, web-page furniture, thumbnails left
    #: behind by other software. They are not anybody's photographs, and they
    #: pad the library and the archive with noise. Anything smaller than this
    #: is passed over as if it were not media at all.
    min_media_bytes: int = 60 * 1024
    #: Work out which way up a photograph goes when the file does not say.
    #:
    #: Only ever for files with no usable EXIF orientation — a camera's own tag
    #: is never second-guessed. Scanned prints, photographs a messaging app
    #: stripped on the way through, anything re-saved by an editor that dropped
    #: the tag: those are the cases. Set False to leave every untagged
    #: photograph exactly as it sits on disk.
    #: Answer this computer's Tailscale name (``<computer>.<tailnet>.ts.net``)
    #: with the certificate Tailscale gives it, so the household's devices on
    #: the tailnet reach Ninaivu from anywhere without a certificate warning
    #: (ninaivu/utils/tailnet.py). Does nothing without Tailscale, or until the
    #: tailnet owner turns HTTPS certificates on.
    tailnet_https: bool = True
    #: How many reverse proxies sit in front of Ninaivu.
    #:
    #: Zero — the default — means the client address and the scheme are read
    #: from the connection itself, which is right for a machine in the house
    #: answering the household directly. Behind nginx or Caddy that address is
    #: always 127.0.0.1, which silently collapses the login rate limiter into
    #: one shared bucket for the whole world and leaves the session cookie
    #: without its Secure flag on an HTTPS site. Setting this to 1 makes
    #: Ninaivu read X-Forwarded-For and X-Forwarded-Proto instead.
    #:
    #: It is off by default because on a directly-exposed port those headers
    #: are written by whoever is calling, and trusting them there would let an
    #: attacker forge a new address for every attempt and defeat the very
    #: limiter this is meant to protect.
    #: Size of the request thread pool. Eight is generous for a household
    #: and small enough that the SQLite connection cache stays a cache.
    server_threads: int = 8

    trusted_proxies: int = 0

    #: Only ever straighten a photograph with a person in it.
    #:
    #: The orientation model is happy to judge a landscape, a document or a
    #: plate of food, and it is usually right — but "usually" is not the bar
    #: for rewriting somebody's library unattended. A found face is a second,
    #: independent witness that the picture is of people and that this way up
    #: is the way a person stands. Off, it will straighten everything it is
    #: confident about.
    straighten_requires_face: bool = True

    detect_orientation: bool = True
    #: Also ask CLIP when the faces found nothing. Off by default.
    #:
    #: Faces are decisive and nearly free. CLIP is neither: it has to embed all
    #: four rotations of a photograph, and the photographs that reach it are
    #: the ones with no faces in them — landscapes, mostly, which it has least
    #: to say about. On a fifty-thousand-item library that is minutes of work
    #: for a handful of corrections, so it is offered rather than assumed.
    orientation_ai: bool = False
    #: Detect and group faces. Off unless the models have been fetched, and
    #: independent of the AI tier: face grouping is OpenCV, not CLIP, so it
    #: works on a machine with no torch at all.
    faces_enabled: bool = False
    #: Directory names skipped during a scan.
    ignore_dirs: set[str] = field(default_factory=lambda: {
        # Ninaivu's own recycle bin. Deleted files are moved here, so a scan
        # must step over it or the next pass would put them straight back.
        "_deleted",
        ".ninaivu", ".mediavault", ".cache", "@eaDir", ".git", ".svn", "node_modules",
        "__pycache__", ".Trash", "$RECYCLE.BIN", "System Volume Information",
    })

    # --- Cloud backup ----------------------------------------------------
    #: Copy the library up to Google Drive, one way, in the background.
    #:
    #: Off until somebody turns it on, and it stays a *copy*: nothing in Drive
    #: can change, move or remove anything here. Deleting a photograph from
    #: Drive does not delete it from the library and does not cause it to be
    #: uploaded again — the record of what has gone up is kept here and is
    #: never cleared by anything the cloud does.
    cloud_enabled: bool = False
    #: The single Drive folder Ninaivu owns. Everything it uploads goes inside
    #: this folder, and it touches nothing else in the account.
    cloud_folder_name: str = "Ninaivu"
    #: Start uploading by itself when the server starts. Off by default: a
    #: household on a metered connection chooses when the upload begins.
    cloud_autostart: bool = False
    #: Ceiling on the upload, in kilobytes per second. Zero means no limit,
    #: which is the default because a limit nobody chose is its own surprise —
    #: but one file at a time is not a cap, and on a slow or metered line the
    #: difference matters. Applied between chunks, so it takes effect on a
    #: running upload without restarting it.
    cloud_rate_kbps: int = 0
    #: The hours uploading may run, in local time, as ``HH:MM``. Both empty
    #: means any time. A start later than the end crosses midnight, which is
    #: the case this is for: ``22:00`` to ``07:00`` is "overnight". An upload
    #: caught by the closing time stops where it is and resumes the next night.
    cloud_window_start: str = ""
    cloud_window_end: str = ""
    #: How many files the backup sends at once. Each waits about two seconds
    #: on Google before any of it moves, so one at a time a library of
    #: photographs used a third of a 32 Mbit/s line. 1 is one at a time.
    cloud_parallel: int = 3
    #: Keep uploading at full speed while people use Ninaivu at home, whatever
    #: the workload mode says for background work: the backup does not make way
    #: for the household, and sends in its larger pieces. It still makes way
    #: for anybody using Ninaivu from outside the house, whose photographs come
    #: up the same internet connection. The upload hours and the speed cap
    #: above still apply.
    cloud_full_speed: bool = False
    #: Encrypt files before they go to Drive, with the key in
    #: cloud-encryption.json (made once in the console). Applies to files
    #: uploaded from then on; what already went up is not sent again.
    cloud_encrypt: bool = False
    #: Whether hidden things are backed up too: "never" (the default: what
    #: the household hid does not leave the house), "encrypted" (only while
    #: the backup is encrypted), or "always". Hidden includes every sound file
    #: — call recordings, voice notes — and the screenshots and documents
    #: Ninaivu hides by itself, so with "never" those have no copy in Drive.
    cloud_hidden: str = "never"
    #: Restore a few random files from the backup this often, in days, and
    #: check them against the originals (ninaivu/cloud/restore_test.py). 0 is off.
    restore_test_days: int = 7
    #: How many files each test restores, and the largest it will pick.
    restore_test_files: int = 5
    restore_test_max_mb: int = 1024
    #: Send a copy of the index (faces, albums, visibility, full paths — no
    #: secrets) to Drive at most this often, in hours, when it has changed.
    #: 0 turns it off. See ninaivu/cloud/index_copy.py.
    cloud_index_every_hours: float = 24

    # --- Sharing the machine with the household ----------------------------
    #: How background work — indexing, analysis, uploads, storage checks —
    #: shares the machine with people using Ninaivu. ``balanced`` gives way to
    #: video playback; ``quiet`` waits whenever anyone is using the app;
    #: ``overnight`` keeps the heavy work for the night hours below and runs
    #: it flat out then. See ninaivu/server/workload.py.
    workload_mode: str = "balanced"
    #: The night, for ``overnight``, as local ``HH:MM``. Crosses midnight.
    workload_night_start: str = "23:00"
    workload_night_end: str = "06:00"

    # --- State -----------------------------------------------------------
    state_dir: Path = field(default_factory=_default_state_dir)

    # --- Scanner ---------------------------------------------------------
    workers: int = field(default_factory=lambda: min(8, (os.cpu_count() or 4)))
    thumb_sizes: tuple[int, ...] = (256, 640)
    thumb_quality: int = 82
    thumb_format: str = "WEBP"
    #: Extract video poster frames (needs ffmpeg or opencv).
    video_thumbs: bool = True
    #: Seconds into a video to grab the poster frame.
    video_thumb_offset: float = 1.0
    # --- Place names -------------------------------------------------------
    #: Turn the coordinates a camera recorded into the name of a place, so an
    #: occasion can be called "Ooty, 12–14 May 2023" rather than a date alone.
    #:
    #: Off by default because turning it on fetches a gazetteer once — about
    #: 11 MB from GeoNames. That request is the only one: after it, every
    #: lookup is arithmetic on a table in the state directory, and no
    #: coordinate of yours is ever sent anywhere.
    place_names: bool = False
    #: Further than this from the nearest known place and the photograph is
    #: left unnamed, rather than given a name it has no real claim to.
    place_max_km: float = 50.0

    # --- The map -----------------------------------------------------------
    #: Draw the map on tiles fetched from OpenStreetMap instead of the outline
    #: Ninaivu ships.
    #:
    #: Off by default, and this is the one setting in Ninaivu that decides
    #: whether anything about your photographs leaves the machine. A tile
    #: server is sent the square of the world you are looking at, one request
    #: per tile: open the map on last summer's holiday and somebody else's
    #: logs know where your family spent it, and roughly when you looked.
    #:
    #: With it off the map is drawn from `static/data/world.geojson`, a
    #: coastline-and-borders outline of the whole world that ships in the
    #: repository. It shows which country and which coast, which is what a map
    #: of a family library is usually being asked; it does not show streets.
    #: Turning this on trades that privacy for streets, knowingly.
    map_tiles: bool = False

    # --- Reading text in pictures ------------------------------------------
    #: Read the words in photographs — signs, menus, screenshots, the back of
    #: a postcard — and make them searchable. Off by default: it needs the
    #: optional OCR package (see requirements-ocr.txt), and it is a second or
    #: so per photograph the first time through a library.
    #:
    #: Like faces, this is a capability rather than an AI tier: it answers a
    #: different question from tagging and is turned on separately.
    ocr_enabled: bool = False
    #: Below this the reader is guessing. Foliage, brickwork and carpet all
    #: produce confident-looking nonsense, and one wrong line in the index is
    #: worse than a missing one.
    ocr_min_score: float = 0.5
    #: Characters kept per photograph. A scan of a newspaper page would
    #: otherwise put a whole article into a column meant for a shop sign.
    ocr_max_chars: int = 4000

    #: How many moments across a clip to tag, rather than the poster frame
    #: alone. A holiday video is a beach, a restaurant and a car park; one
    #: frame at the one-second mark describes none of them. Zero or one keeps
    #: the old single-frame behaviour.
    video_keyframes: int = 5
    #: Compute perceptual hashes for duplicate detection.
    perceptual_hash: bool = True
    #: Re-check files whose size or mtime changed.
    rescan_on_change: bool = True
    #: Watch the library for changes and index incrementally.
    watch: bool = True
    #: Seconds. If the watcher is on and a scan finished this recently, start-up
    #: does not walk the library again — that gap is a restart, not a day away.
    #: Zero always scans.
    boot_scan_after: float = 900.0

    #: The index is the one thing the library itself does not carry: names,
    #: albums, visibility, share links, corrected dates. Hours between
    #: snapshots; zero turns it off. They land in the state directory unless
    #: `backup_dir` says otherwise — put that on another disk if you have one.
    backup_every_hours: float = 24.0
    backup_keep: int = 7
    backup_dir: str = ""

    #: Where every AI model lives — the search model, the editing models, the
    #: face detector and recogniser, the orientation model. Empty means the
    #: `.ai-models` folder beside the application, which is where they already
    #: are. One folder on purpose: moving Ninaivu to another machine is then
    #: this, the state folder and the library, and nothing else to hunt for.
    ai_models_dir: str = ""

    #: Days after which a file in the recycle bin is erased for good. Zero —
    #: the default — keeps everything until somebody says otherwise, which is
    #: what a bin is for.
    bin_erase_after_days: float = 0.0
    watch_debounce: float = 2.0

    # --- AI server ---------------------------------------------------------
    #: A ComfyUI instance on another machine in the house, for image edits the
    #: Ninaivu machine is too small to run. Off until an administrator sets the
    #: address in the console; photographs go to that address and nowhere else.
    ai_server_url: str = ""
    ai_server_enabled: bool = False
    #: Workflow (by its id in the console) that serves each Playground job;
    #: empty leaves that job on the local model, as before.
    ai_server_edit_workflow: str = ""
    ai_server_remove_workflow: str = ""
    ai_server_upscale_workflow: str = ""
    ai_server_restore_workflow: str = ""
    ai_server_colorize_workflow: str = ""
    #: Seconds to wait for one job, queue time included.
    ai_server_timeout: int = 180
    #: Longest side of the preview the Playground sends for a server job.
    ai_server_max_side: int = 1024

    # --- AI --------------------------------------------------------------
    ai_enabled: bool = True
    #: "auto" picks CLIP when torch is importable, else the light tagger.
    ai_engine: str = "auto"  # auto | clip | light | off
    #: Run the image model on a graphics processor when there is one that
    #: works: NVIDIA's (CUDA) or a Mac's own (Metal). Off keeps it on the
    #: processor. Takes effect when Ninaivu next starts.
    ai_gpu: bool = True
    #: "auto": SigLIP 2 once downloaded (Admin → AI models), else ViT-B-32.
    clip_model: str = "auto"
    clip_pretrained: str = "laion2b_s34b_b79k"
    clip_batch_size: int = 16
    #: Minimum cosine similarity for a zero-shot tag to be kept.
    tag_threshold: float = 0.18
    max_tags: int = 8
    #: Screen for explicit content and hide it behind a toggle.
    nsfw_filter: bool = True
    nsfw_threshold: float = 0.6
    #: Hide screenshots, documents and photographs of screens from everyone but
    #: administrators (media/screens.py). By name on every install; by picture
    #: once the search model has looked at it.
    hide_screens: bool = True
    #: Hamming distance under which two pHashes count as near-duplicates.
    duplicate_distance: int = 6

    # --- Picture quality --------------------------------------------------
    #: Measure focus and exposure while scanning. Cheap: one 256px greyscale
    #: pass over a thumbnail Ninaivu has already decoded.
    quality_scan: bool = True
    #: Laplacian variance below which a photograph reads as soft. Measured at
    #: a fixed size (see media.QUALITY_EDGE) so one number holds across
    #: cameras. Deliberately low — a wrongly flagged keeper is worse than a
    #: soft frame that slips through.
    blur_threshold: float = 45.0
    #: Mean luma, 0-1. Below the first is underexposed, above the second is
    #: washed out.
    dark_threshold: float = 0.16
    bright_threshold: float = 0.86
    #: Fraction of pixels at the top of the range before highlights count as
    #: blown.
    highlight_clip_threshold: float = 0.12
    #: Pixel count below which a still is flagged as low resolution.
    low_res_pixels: int = 640 * 480

    # --- Occasions (auto-albums) ------------------------------------------
    #: Group the library into occasions after each scan. Metadata only — no
    #: pixels are read and no model is loaded, so this runs in every AI tier.
    occasion_scan: bool = True
    #: A silence longer than this ends one occasion and starts the next. Six
    #: hours keeps a day out and the evening that followed it together, and
    #: still separates two consecutive days.
    occasion_gap_hours: float = 6.0
    #: Kilometres. A jump further than this between two photographs that both
    #: carry coordinates also ends an occasion. Zero groups on time alone.
    occasion_radius_km: float = 60.0
    #: Runs shorter than this are not occasions, just stray frames.
    occasion_min_items: int = 4

    # --- Search -----------------------------------------------------------
    #: Read a date range out of what somebody typed — "last summer", "may
    #: 2019", "last week" — and narrow the search with it.
    date_phrase_search: bool = True
    #: Which half of the world the seasons in those phrases belong to. June
    #: is summer in Atlanta and winter in Adelaide, and a search engine that
    #: assumes one of them is wrong for half its households.
    southern_hemisphere: bool = False

    # --- People ----------------------------------------------------------
    #: Let anyone who reaches the server browse public media without signing
    #: in. Family and admin features still require a login. Admins can turn
    #: this off to make the whole library private.
    open_browsing: bool = True
    #: Cover the screen after this many minutes with nobody there, in both the
    #: family app and the console, until the person unlocks it with their
    #: password or PIN. Nobody is signed out and nothing in progress stops.
    #: 0 turns the lock off. See the screen lock in ninaivu/server/auth.py.
    lock_after_minutes: int = 15
    #: What the household calls this library. The admin sets it once and it is
    #: what everyone sees; each family member may then keep their own name for
    #: it instead. Empty means "Ninaivu".
    house_name: str = ""
    #: The extensions switched on, by name (see ninaivu/extensions.py). Every
    #: extension is off until an administrator turns it on in the console, and
    #: a change takes effect at the next start. Empty by default, always.
    extensions: list = field(default_factory=list)
    #: Ceiling for the folder of converted videos, in megabytes. They are
    #: derivatives, so losing them costs only the time to make them again.
    proxy_cache_mb: int = 4096

    # --- Notifications ---------------------------------------------------
    #: Off unless one of these is set. Nothing phones anywhere by default.
    notify_webhook: str = ""
    notify_webhook_format: str = "json"     # json | ntfy | form
    notify_smtp_host: str = ""
    notify_smtp_port: int = 587
    notify_smtp_user: str = ""
    notify_smtp_password: str = ""
    notify_smtp_to: str = ""
    notify_smtp_tls: bool = True
    notify_events: list[str] = field(default_factory=list)
    notify_quiet_seconds: int = 6 * 60 * 60

    # --- The weekly photograph -------------------------------------------
    #: One photograph from this day in a past year, sent to the family.
    #:
    #: Off by default, and it borrows the mail server above rather than
    #: asking for it twice — but nothing else. Alerts go to whoever looks
    #: after the machine; photographs go to the household, which is a
    #: different list of people and a different promise about what may be
    #: put in a message. See `ninaivu/utils/digest.py`.
    digest_enabled: bool = False
    #: Who it goes to. Comma-separated; empty means nobody, which is off.
    digest_to: str = ""
    #: Monday is 0, as `datetime.weekday()` counts. Sunday morning by
    #: default: the time a household is most likely to have a minute for it.
    digest_weekday: int = 6
    digest_hour: int = 9
    #: Where "see the rest" points. A home server is only reachable from the
    #: house, so this is offered rather than assumed.
    digest_link: str = ""

    # --- Server ----------------------------------------------------------
    #: Which addresses to serve on. ``0.0.0.0`` means "every address this
    #: machine has", which is what makes phones and tablets on the same Wi-Fi
    #: able to reach it — the whole point of a family library. Pass
    #: ``--local-only`` (or set ``host`` to ``127.0.0.1``) to keep it to this
    #: machine.
    host: str = "0.0.0.0"
    #: The Network access switch on the console's Server page. Off, Ninaivu
    #: answers only on this computer - the family app and the console both
    #: bind to 127.0.0.1 and no name is announced - however it was started:
    #: an explicit ``--host 0.0.0.0`` from start.py or the desktop panel does
    #: not override it. On, ``host`` and ``--local-only`` decide as before.
    network_access: bool = True
    #: The family app — the one everyone in the house opens. 80 is the
    #: standard HTTP port, so an mDNS name resolves with nothing typed after
    #: it (``http://ninaivu.local``, not ``http://ninaivu.local:5000``).
    #: start.py's own ``--port`` default is what actually governs a normal
    #: launch (it always passes an explicit value through), so this default
    #: mainly matters for `python -m ninaivu` run directly.
    port: int = 80
    #: The admin console, deliberately on a different port so it can be
    #: firewalled off or bound to localhost independently -- set via
    #: ``admin_host`` below, or ``--admin-host``/``NINAIVU_ADMIN_HOST``.
    admin_port: int = 3000
    #: Address the admin console binds to. ``None`` means "the same as
    #: ``host``", which is today's behaviour and keeps an existing
    #: deployment's reachable addresses unchanged on upgrade. Set this to
    #: ``127.0.0.1`` to serve the family app to the whole house while keeping
    #: the console reachable only from this machine -- the split the two
    #: ports exist for in the first place. main() warns at startup whenever
    #: the console ends up bound beyond loopback.
    admin_host: str | None = None
    #: Ceiling on a single request body, in megabytes. Covers /api/upload --
    #: raise it if the household shoots video larger than this.
    max_upload_mb: int = 512
    #: Put family members' phone backups straight into the library instead of
    #: the review queue. Off by default: a family upload waits for an
    #: administrator, and a backup is an upload. An administrator's own phone
    #: is always filed straight away.
    phone_backup_trusted: bool = False
    debug: bool = False
    #: Page size for the gallery API.
    page_size: int = 200
    max_page_size: int = 1000

    # ---------------------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.state_dir / "index.db"

    @property
    def thumbs_dir(self) -> Path:
        return self.state_dir / "thumbs"

    @property
    def config_path(self) -> Path:
        return self.state_dir / "config.json"

    def ensure_dirs(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.thumbs_dir.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["state_dir"] = str(self.state_dir)
        data["ignore_dirs"] = sorted(self.ignore_dirs)
        data["thumb_sizes"] = list(self.thumb_sizes)
        return data

    @property
    def libraries(self) -> list[str]:
        """Configured library folders that are on disk right now.

        Callers that are about to *read files* want this. Callers that are
        about to answer "what is in this library" want :attr:`roots`, because
        a drive that is unplugged has not stopped being one of the household's
        library folders — and with two roots and one of them away, filtering
        here used to make that library's photographs vanish from the gallery
        with nothing on screen to say why.
        """
        from ..storage import roots as roots_kit          # noqa: PLC0415

        return [root for root in self.roots if roots_kit.available(root)]

    @property
    def libraries_away(self) -> list[str]:
        """Configured library folders that are not reachable at the moment."""
        from ..storage import roots as roots_kit          # noqa: PLC0415

        return [root for root in self.roots if not roots_kit.available(root)]

    def add_library(self, path: str) -> bool:
        """Register a library folder. Returns True if it was new."""
        resolved = str(Path(path).expanduser().resolve())
        if any(str(Path(r)) == resolved for r in self.roots):
            return False
        self.roots.append(resolved)
        if not self.active_root:
            self.active_root = resolved
        return True

    def remove_library(self, path: str) -> bool:
        # The folder exactly as it is listed, as well as resolved. A library
        # brought over from another kind of machine — "E:\MasterArchive" on a
        # Mac — is not a path here at all: resolving it makes it relative to
        # wherever Ninaivu was started, it matched nothing, and the one folder
        # that most needed removing could not be.
        wanted = {path, str(Path(path).expanduser().resolve())}

        def matches(root: str) -> bool:
            return root in wanted or str(Path(root)) in wanted

        before = len(self.roots)
        self.roots = [r for r in self.roots if not matches(r)]
        if self.active_root and matches(self.active_root):
            self.active_root = self.roots[0] if self.roots else None
        return len(self.roots) != before

    def save(self) -> None:
        """Persist everything the household has changed to ``config.json``.

        This used to be a hand-written list of keys, and a setting left off it
        was silently dropped the next time anything at all was saved: set it by
        hand, touch one switch in the console, and it was gone. Forty-two of
        the ninety-seven settings were in that position, including whether to
        read text in photographs and how many moments of a video to describe —
        which is fifteen hours of work on a large library, quietly reinstated.

        So the list is inverted. Everything is written except the settings that
        describe how this process was started rather than what the household
        wants, and a setting is written only when it differs from the default —
        so a household that never touched a threshold still picks up a better
        one in a later version, instead of having today's default frozen into
        their file for ever.
        """
        self.ensure_dirs()
        persisted = {}
        for name, default in _field_defaults().items():
            if name in RUNTIME_ONLY:
                continue
            value = _jsonable(getattr(self, name))
            if value != _jsonable(default):
                persisted[name] = value
        # One writer at a time. Saves come from the console's request threads
        # and from the upload and phone-backup paths at once; two writing the
        # same ``.tmp`` left the longer one's tail after the shorter one's
        # text, and the second rename found nothing to rename.
        with _SAVE_LOCK:
            tmp = self.config_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(persisted, indent=2), encoding="utf-8")
            # This file holds the SMTP password for the notification emails, so
            # it is not for anybody else with an account on this machine to
            # read. Set on the temporary file, before the rename, so there is
            # never a moment where the real file exists at the umask's default.
            try:
                os.chmod(tmp, 0o600)
            except OSError:                             # Windows, and that is fine
                pass
            tmp.replace(self.config_path)

    @classmethod
    def load(cls, **overrides: Any) -> "Config":
        cfg = cls()
        # Only the settings themselves are read from the file or the overrides.
        # ``hasattr`` was the test before, and it is true of the properties
        # (``db_path``, ``libraries``) and the methods (``save``) as well: a
        # hand-edited key with one of those names made ``setattr`` raise at
        # start, or replaced a method with a string that failed at first use.
        names = {f.name for f in fields(cls)}

        stored = cfg.config_path
        if stored.exists():
            try:
                data = json.loads(stored.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                # Not "carry on as if nobody had set anything": the next save
                # would then replace the household's only copy — the library
                # folders, the Drive backup, the mail settings — with a file
                # holding the defaults. The damaged file is set aside under a
                # name that says what happened, and Ninaivu starts fresh beside
                # it, so a person can still read it back by hand.
                import logging
                aside = stored.with_name(f"{stored.name}.broken-{int(time.time())}")
                try:
                    stored.replace(aside)
                except OSError:
                    aside = None
                logging.getLogger(__name__).error(
                    "%s could not be read (%s); starting with default settings%s",
                    stored, exc,
                    f" — the damaged file is kept at {aside}" if aside else "")
                data = {}
            if not isinstance(data, dict):
                data = {}
            for key, value in data.items():
                if key in names:
                    setattr(cfg, key, value)

        # Environment overrides
        if env_roots := os.environ.get("NINAIVU_ROOTS"):
            cfg.roots = [p for p in env_roots.split(os.pathsep) if p]
        if env_root := os.environ.get("NINAIVU_ROOT"):
            cfg.active_root = env_root
            if env_root not in cfg.roots:
                cfg.roots.append(env_root)
        cfg.host = os.environ.get("NINAIVU_HOST", cfg.host)
        cfg.port = _env_int("NINAIVU_PORT", cfg.port)
        cfg.admin_port = _env_int("NINAIVU_ADMIN_PORT", cfg.admin_port)
        cfg.admin_host = os.environ.get("NINAIVU_ADMIN_HOST", cfg.admin_host)
        cfg.max_upload_mb = _env_int("NINAIVU_MAX_UPLOAD_MB", cfg.max_upload_mb)
        cfg.straighten_requires_face = _env_bool(
            "NINAIVU_STRAIGHTEN_REQUIRES_FACE", cfg.straighten_requires_face)
        cfg.debug = _env_bool("NINAIVU_DEBUG", cfg.debug)
        cfg.server_threads = _env_int("NINAIVU_SERVER_THREADS",
                                      cfg.server_threads)
        cfg.ai_enabled = _env_bool("NINAIVU_AI", cfg.ai_enabled)
        cfg.ai_engine = os.environ.get("NINAIVU_AI_ENGINE", cfg.ai_engine)
        cfg.ai_gpu = _env_bool("NINAIVU_AI_GPU", cfg.ai_gpu)
        cfg.nsfw_filter = _env_bool("NINAIVU_NSFW_FILTER", cfg.nsfw_filter)
        cfg.hide_screens = _env_bool("NINAIVU_HIDE_SCREENS", cfg.hide_screens)
        cfg.min_media_bytes = _env_int("NINAIVU_MIN_MEDIA_BYTES", cfg.min_media_bytes)
        cfg.index_hidden = _env_bool("NINAIVU_INDEX_HIDDEN", cfg.index_hidden)
        cfg.trusted_proxies = _env_int("NINAIVU_TRUSTED_PROXIES",
                                       cfg.trusted_proxies)
        cfg.detect_orientation = _env_bool("NINAIVU_DETECT_ORIENTATION",
                                           cfg.detect_orientation)
        cfg.orientation_ai = _env_bool("NINAIVU_ORIENTATION_AI",
                                       cfg.orientation_ai)
        cfg.faces_enabled = _env_bool("NINAIVU_FACES", cfg.faces_enabled)
        cfg.house_name = os.environ.get("NINAIVU_HOUSE_NAME", cfg.house_name)
        cfg.proxy_cache_mb = _env_int("NINAIVU_PROXY_CACHE_MB", cfg.proxy_cache_mb)
        cfg.notify_webhook = os.environ.get("NINAIVU_NOTIFY_WEBHOOK",
                                            cfg.notify_webhook)
        cfg.notify_smtp_host = os.environ.get("NINAIVU_NOTIFY_SMTP_HOST",
                                              cfg.notify_smtp_host)
        cfg.notify_smtp_to = os.environ.get("NINAIVU_NOTIFY_SMTP_TO",
                                            cfg.notify_smtp_to)
        cfg.watch = _env_bool("NINAIVU_WATCH", cfg.watch)
        cfg.open_browsing = _env_bool("NINAIVU_OPEN_BROWSING", cfg.open_browsing)
        cfg.lock_after_minutes = max(0, _env_int("NINAIVU_LOCK_AFTER_MINUTES",
                                                 cfg.lock_after_minutes))
        cfg.workers = _env_int("NINAIVU_WORKERS", cfg.workers)
        cfg.lock_roots = _env_bool("NINAIVU_LOCK_ROOTS", cfg.lock_roots)
        cfg.cloud_enabled = _env_bool("NINAIVU_CLOUD", cfg.cloud_enabled)
        cfg.cloud_folder_name = os.environ.get("NINAIVU_CLOUD_FOLDER",
                                               cfg.cloud_folder_name)
        cfg.cloud_autostart = _env_bool("NINAIVU_CLOUD_AUTOSTART",
                                        cfg.cloud_autostart)
        cfg.cloud_rate_kbps = _env_int("NINAIVU_CLOUD_RATE_KBPS",
                                       cfg.cloud_rate_kbps)
        cfg.cloud_window_start = os.environ.get("NINAIVU_CLOUD_WINDOW_START",
                                                cfg.cloud_window_start)
        cfg.cloud_window_end = os.environ.get("NINAIVU_CLOUD_WINDOW_END",
                                              cfg.cloud_window_end)
        cfg.backup_dir = os.environ.get("NINAIVU_BACKUP_DIR", cfg.backup_dir)
        cfg.ai_models_dir = os.environ.get("NINAIVU_AI_MODELS_DIR",
                                           cfg.ai_models_dir)
        cfg.backup_keep = _env_int("NINAIVU_BACKUP_KEEP", cfg.backup_keep)

        for key, value in overrides.items():
            if value is not None and key in names:
                setattr(cfg, key, value)

        cfg.state_dir = Path(cfg.state_dir).expanduser()
        cfg.ignore_dirs = set(cfg.ignore_dirs)
        cfg.thumb_sizes = tuple(cfg.thumb_sizes)
        cfg.roots = [str(Path(r).expanduser()) for r in cfg.roots]
        if cfg.active_root:
            cfg.active_root = str(Path(cfg.active_root).expanduser())
        return cfg


#: Directories that must never *become* a library root. Browsing through them
#: is fine — you have to pass through ``C:\\`` to reach ``C:\\Master`` — but
#: pointing a media scanner at one is never what anybody meant.
FORBIDDEN_LIBRARY_ROOTS = {
    "/", "/bin", "/boot", "/dev", "/etc", "/lib", "/lib32", "/lib64", "/proc",
    "/sbin", "/sys", "/usr", "/var", "/opt", "/private", "/system",
    # macOS. /Volumes is where external drives are *mounted*, so it is a
    # parent of good library folders and never one itself — picking it would
    # make the library follow whatever happens to be plugged in.
    "/System", "/Library", "/Applications", "/Volumes", "/cores", "/Network",
    # macOS keeps /etc and /var under /private and leaves a symlink at the top
    # level, so "/etc" resolves to "/private/etc". The link's own name is
    # caught as typed (see _forms); these catch the real folder, reached by
    # the picker through /private or by any other symlink that leads there.
    "/private/etc", "/private/var",
    "c:\\", "d:\\", "e:\\", "f:\\",
    "c:\\windows", "c:\\program files", "c:\\program files (x86)",
    "c:\\programdata", "c:\\$recycle.bin",
}

#: Never readable, never useful, and walking them can hang.
UNBROWSABLE = ("/proc", "/sys", "/dev")


def _looks_like_windows(text: str) -> bool:
    return len(text) > 1 and text[1] == ":" and text[0].isalpha()


def _normalise(path: Path, resolve: bool = True) -> str:
    """A comparable form of a path: case-folded and separator-normalised on
    Windows, resolved on POSIX. Windows paths are recognised by their drive
    letter rather than by the host OS, so the rules are testable anywhere.

    With ``resolve=False`` a POSIX path is made absolute and tidied, but no
    symlink in it is followed."""
    raw = str(path)
    if _looks_like_windows(raw):
        text = raw.replace("/", "\\").rstrip("\\").lower()
        if len(text) == 2 and text[1] == ":":   # "c:" means the drive root
            text += "\\"
        elif len(text) == 2:
            text += "\\"
        return text or "\\"
    if os.name == "nt":
        # A POSIX-form path on a Windows host — "/etc" typed by an admin, or
        # written in a test. Resolving it would nail it to the current drive
        # ("C:\etc") and the system-folder rules would stop recognising it,
        # so it is compared in the form it was given.
        return raw.replace("\\", "/").rstrip("/") or "/"
    try:
        return str(path.resolve()) if resolve else os.path.abspath(raw)
    except OSError:
        return raw


def _forms(path: Path) -> set[str]:
    """Every form of a path the system-folder rules look at: as typed, and
    where it leads.

    Resolving alone misses a system folder that is itself a symlink. On macOS
    "/etc" leads to "/private/etc", and on a Linux with a merged /usr "/bin"
    leads to "/usr/bin"; neither destination is the name in the list, so the
    folder is judged by the name it was given as well."""
    return {_normalise(path), _normalise(path, resolve=False)}


def is_browsable(path: Path) -> bool:
    """Whether the folder picker may list this directory at all."""
    for text in _forms(path):
        for blocked in UNBROWSABLE:
            if text == blocked or text.startswith(blocked + os.sep) or \
                    text.startswith(blocked + "/"):
                return False
    return True


def is_forbidden_root(path: Path) -> bool:
    """True for directories that must not be used as the media library."""
    if not is_browsable(path):
        return True
    return not _forms(path).isdisjoint(FORBIDDEN_LIBRARY_ROOTS)


#: Windows drive-type codes from GetDriveTypeW.
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_REMOTE = 4
DRIVE_CDROM = 5

#: The picker asks for these on every navigation, and the answer changes only
#: when somebody plugs something in. Recomputing it per click is what made the
#: folder picker sit on "Loading…".
_BROWSE_CACHE: dict[str, Any] = {"at": 0.0, "roots": None}
BROWSE_CACHE_SECONDS = 20.0


def windows_drives() -> list[Path]:
    """Drive letters this machine has, without touching any of them.

    The obvious loop — ``Path(f"{letter}:\\").exists()`` for A to Z — is a trap
    on Windows. A mapped network drive whose server is off blocks until SMB
    gives up, an empty optical drive spins up, and a card reader with no card
    can take seconds; twenty-four of those ran on *every* folder the picker
    opened. ``GetLogicalDrives`` returns a bitmask of which letters exist and
    performs no I/O at all, and ``GetDriveTypeW`` then says what each one is
    from the same table.

    Network drives *are* included, and they were not always. They were left
    out because a mapped drive whose server is asleep blocks for as long as
    SMB takes to give up, and twenty-four of those on every folder the picker
    opened made it unusable. That reasoning was sound and the conclusion was
    wrong: some households keep their photographs on a NAS, and hiding the
    drive meant the Archive tab simply could not see them.

    The two calls below perform no I/O — ``GetLogicalDrives`` reads a bitmask
    and ``GetDriveTypeW`` reads a table — so listing a network drive costs
    nothing. What costs is *touching* one, so nothing here does: the caller
    gets the letter and its kind, and the decision to knock on the door is
    made once, later, by somebody who can afford to wait.
    """
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        mask = kernel32.GetLogicalDrives()
    except Exception:                            # noqa: BLE001 — not Windows
        return []

    found: list[Path] = []
    for index in range(26):
        if not mask & (1 << index):
            continue
        letter = chr(ord("A") + index)
        root = f"{letter}:\\"
        try:
            kind = kernel32.GetDriveTypeW(root)
        except Exception:                        # noqa: BLE001
            kind = DRIVE_FIXED
        if kind == DRIVE_CDROM:
            continue
        if kind == DRIVE_REMOVABLE and not _drive_is_ready(root):
            continue                             # card reader with no card
        found.append(Path(root))
    return found


def network_drives() -> set[str]:
    """Which drive letters are network mappings. No I/O, same as above.

    The picker uses this to show them without probing them, and to explain
    itself when one does not answer.
    """
    out: set[str] = set()
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        mask = kernel32.GetLogicalDrives()
    except Exception:                            # noqa: BLE001 — not Windows
        return out
    for index in range(26):
        if not mask & (1 << index):
            continue
        root = f"{chr(ord('A') + index)}:\\"
        try:
            if kernel32.GetDriveTypeW(root) == DRIVE_REMOTE:
                out.add(root)
        except Exception:                        # noqa: BLE001
            continue
    return out


#: What the Windows shell shows that is not a folder on any disk.
#:
#: "This PC\Apple iPhone\Internal Storage" looks like a path and is not one.
#: A phone connected over USB appears in Explorer through MTP, which is a
#: protocol for asking a device for files one at a time — there is no drive
#: letter, no UNC name, and nothing `os.path` can open. Pasting it into a
#: folder box produces "folder does not exist", which is true and useless.
_SHELL_PREFIXES = ("this pc", "computer", "my computer", "dieser pc",
                   "ce pc", "questo pc", "este equipo")


def shell_namespace_hint(raw: str) -> str | None:
    """Explain a path that Windows shows but no program can open, or None."""
    text = str(raw or "").strip().strip('"')
    if not text:
        return None
    if text.startswith("::{") or "::{" in text:
        return ("That is a Windows shell location rather than a folder on a "
                "disk, so Ninaivu cannot read it directly.")
    first = text.replace("/", "\\").lstrip("\\").split("\\", 1)[0].lower()
    if first in _SHELL_PREFIXES:
        rest = text.replace("/", "\\").split("\\")
        device = rest[1] if len(rest) > 1 else "the device"
        return (f"“{device}” is shown by Windows through the shell rather than "
                f"as a folder on a disk — a phone or camera connected by USB "
                f"appears this way. There is no path underneath it for Ninaivu "
                f"to read.")
    return None


def _drive_is_ready(root: str) -> bool:
    """Is there actually media in this removable drive? Asked without waiting.

    ``GetVolumeInformationW`` returns false immediately for an empty slot,
    where opening the drive would wait for it to spin up.
    """
    try:
        import ctypes

        return bool(ctypes.windll.kernel32.GetVolumeInformationW(
            root, None, 0, None, None, None, None, 0))
    except Exception:                            # noqa: BLE001
        return False


def default_browse_roots() -> list[Path]:
    """Sensible starting points for the folder picker on this machine."""
    now = time.time()
    cached = _BROWSE_CACHE.get("roots")
    if cached is not None and now - _BROWSE_CACHE["at"] < BROWSE_CACHE_SECONDS:
        return list(cached)

    home = Path.home()
    candidates = [home]
    for name in ("Pictures", "Photos", "Videos", "Movies", "Music", "Downloads",
                 "Documents", "Desktop", "OneDrive"):
        candidate = home / name
        if candidate.is_dir():
            candidates.append(candidate)

    if os.name == "nt":
        candidates.extend(windows_drives())
    else:
        for mount in ("/Volumes", "/media", "/mnt", "/run/media", "/srv", "/home"):
            path = Path(mount)
            if path.is_dir():
                candidates.append(path)

    unique: list[Path] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    _BROWSE_CACHE.update(at=now, roots=list(unique))
    return unique


#: The longest a home name may be. Long enough for "The Kumar Family
#: Home", short enough that it cannot push the rest of the top bar off a phone.
HOME_NAME_MAX = 40


def clean_home_name(raw: Any) -> str:
    """Tidy a name somebody typed. Empty means "fall back to the default".

    Control characters are stripped and runs of whitespace collapsed, because
    this string is rendered into a heading and a browser tab title where a
    newline or a tab would either break the layout or silently disappear.
    """
    text = "" if raw is None else str(raw)
    # Whitespace becomes a space *before* unprintables are dropped. A tab is
    # not "printable", so filtering first would delete it and weld the words
    # either side together: "Kumar<tab>Home" -> "KumarHome".
    text = "".join(" " if ch.isspace() else (ch if ch.isprintable() else "")
                   for ch in text)
    return " ".join(text.split())[:HOME_NAME_MAX].strip()


def house_name(cfg: Any) -> str:
    """What this library is called. One place decides the fallback.

    Matches ``APP_NAME``; it is spelled out rather than imported because the
    package imports this module, not the other way round.
    """
    return clean_home_name(getattr(cfg, "house_name", "")) or "Ninaivu"


def home_name_for(user: Any, cfg: Any) -> str:
    """What *this person* calls it: their own name, or the household's.

    Every surface goes through here so the fallback chain cannot drift — the
    top bar, the tab title, the profile picker and the photo frame all give
    the same answer for the same viewer.
    """
    personal = clean_home_name(getattr(user, "home_label", "") or "")
    return personal or house_name(cfg)


#: Extensions that mean two entirely different things.
#:
#: ``.ts`` is an MPEG transport stream — and it is also TypeScript, which is
#: how eighty-two ``index.d.ts`` files ended up indexed as videos in a family
#: photo library. ``.mts`` is AVCHD, and also TypeScript's module flavour of
#: the same. For these, the extension is a question rather than an answer, and
#: the first bytes settle it.
AMBIGUOUS_EXTS = {".ts", ".mts"}

#: Every MPEG transport stream packet opens with this sync byte. Checking three
#: packets is enough to tell a real stream from a text file that happens to
#: open with one.
_TS_SYNC = 0x47


def looks_like_transport_stream(path: str | Path) -> bool:
    """True when the file really is MPEG-TS rather than a TypeScript source.

    Two packet layouts are real video. A downloaded or broadcast ``.ts`` has
    188-byte packets from the first byte. A camcorder's AVCHD ``.mts`` (and a
    Blu-ray's ``.m2ts``) puts a 4-byte timestamp before each packet, making
    them 192 bytes with the sync byte at offset 4 -- checking only the first
    layout dismissed every camcorder clip in a library as unknown.
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(400)
    except OSError:
        return False
    plain = len(head) >= 377 and all(head[i] == _TS_SYNC for i in (0, 188, 376))
    stamped = len(head) >= 389 and all(head[i] == _TS_SYNC for i in (4, 196, 388))
    return plain or stamped


def media_kind(path: str | Path) -> str:
    """Classify a path as ``picture`` / ``video`` / ``audio`` / ``unknown``.

    Extension first, because that is what it is for. The one exception is the
    handful of extensions two different worlds both claim: those are decided
    by looking, and a file that cannot be opened is not assumed to be media.
    """
    ext = Path(path).suffix.lower()
    if ext in IMAGE_EXTS or ext in RAW_EXTS:
        return "picture"
    if ext in AMBIGUOUS_EXTS:
        return "video" if looks_like_transport_stream(path) else "unknown"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    return "unknown"
