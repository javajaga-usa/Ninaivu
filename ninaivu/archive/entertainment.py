"""Recognising films, TV and music, so they are not archived as memories.

The drive that collects downloaded courses collects entertainment too: a
``Movies`` folder of ``Kaithi (2019) Tamil HDRip x264 - TamilRockers.mkv``, a
season of a TV show, and songs saved from sites like ``MassTamilan.com`` --
often loose, in the same ``Downloads`` folder as the family's voice notes and
phone videos. Archived, each song becomes "audio" and each film a two-hour
"video" filed under the day it was downloaded.

Unlike a course, entertainment is judged **file by file**. Holding back the
``Downloads`` folder would hold back the voice note beside the song. The folder
still counts, as context: numbered tracks and cover art around a song, a poster
and an ``.nfo`` around a film, and whether the files beside it are songs too.

Two rules keep this safe.

**Only names can accuse.** What a file is called, the folder it is in and the
tags a distributor wrote into it are what put a file under suspicion: a file is
only held back when at least :data:`NAMED_MINIMUM` of its points say so about
the file itself. What can be measured -- a cinema-shaped frame, several
soundtracks, feature length -- can tip a near miss, never start one. A
professionally filmed wedding is 2.39:1 and two hours long, and nothing more.

**Anything that looks personal is never held back on a guess.** A phone's or
camera's make inside a video, a camera's naming (``IMG_0768.MOV``,
``VID_20190607_…``), a family occasion in the name, audio recorded in mono or at
voice-note quality, a recorder's naming (``PTT-20190607-WA0003.opus``,
``11-02-21-20-36-23.wav``), an audiobook -- and, before any audio file is held,
listening to it: a recording with the pauses of speech is copied whatever it is
called (see :func:`listen`). Such a file is copied, and listed as worth a look.

What a film or TV episode looks like, in points:

* **4** - a release source (``HDRip``, ``BluRay``, ``WEB-DL``), a release site
  or group (``TamilRockers``, ``YTS``, a web address), an episode number
  (``S01E03``). Nobody names a birthday that.
* **2** - encoding tags (``x264``, ``HEVC``, ``ESub``, ``Dual Audio``), film
  wording (``full movie``, ``trailer``, ``video song``) or a studio or channel.
  A media server's naming: the folder and file both ``Title (Year)``.
* **1** - a resolution (``1080p``) or a release year: ``Title (2019)``.
* **context** - 2 for a film folder's leftovers (``.nfo``, ``poster.jpg``, a
  ``Sample``), 1 for subtitles, 1 for a folder called ``Movies``, and 2 when
  most of the videos beside it are films on their own.
* **measured, to tip only** - 2 for a cinema-shaped frame (2:1 or wider), 2 for
  several soundtracks, 2 for subtitles inside the file, 1 for feature length, 1
  for chapters. A web address in the file's own tags is a name, and counts 4.

What music looks like, in points:

* **4** - a music site in the name or the tags (``MassTamilan.com``,
  ``StarMusiQ``).
* **2** - song wording (``lyrics``, ``official audio``, ``320kbps``) or a
  record label (``T-Series``, ``Think Music``); album *and* artist tags.
* **1** - a track number tag; cover art inside the file.
* **context** - 2 for numbered tracks, 2 for an album's leftovers (``cover.jpg``,
  ``.cue``, ``.m3u``), 1 for a folder called ``Songs`` or ``Music``, and 2 when
  most of the audio beside it is music on its own.

A file at or above :data:`THRESHOLD` with enough named points is held back, and
the cover art or poster beside held files goes with them. Nothing is excluded
until somebody agrees.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections import Counter
from dataclasses import dataclass, field

#: Points at which a file is treated as a film or music.
THRESHOLD = 4
#: Points that must come from names and tags rather than measurements.
NAMED_MINIMUM = 2
#: A video this large is probed for tags even when its name says nothing.
LARGE_VIDEO_BYTES = 700 * 1024 * 1024

FILM = "film"
MUSIC = "music"
CATEGORIES = (FILM, MUSIC)

FFPROBE = shutil.which("ffprobe")
FFMPEG = shutil.which("ffmpeg")


# ---------------------------------------------------------------------------
# What the names say
# ---------------------------------------------------------------------------

def _words(text: str) -> str:
    """Separators as spaces, so ``Kaithi.2019.HDRip`` reads like a sentence."""
    return re.sub(r"[._+\[\](){}|]+", " ", text).lower()


def _stem(name: str) -> str:
    return os.path.splitext(name)[0]


def _ext(name: str) -> str:
    return os.path.splitext(name)[1].lstrip(".").lower()


# A web address. Only top-level domains that are not also ordinary words count
# anywhere in a name; ".in", ".to" or ".me" need a "www." or brackets, because
# "Wedding.in.Goa" and "Trip.to.Ooty" are family videos.
_WEB = re.compile(
    r"(?:www\.[a-z0-9-]+\.[a-z]{2,6}"
    r"|[\[(]\s*[a-z0-9-]+\.(?:[a-z]{2,6})\s*[\])]"
    r"|(?<![a-z0-9])[a-z0-9][a-z0-9-]{2,40}\.(?:com|net|org|info|cc|ws|pk|mx|xyz|vip|lol|biz|club|site|live)"
    r"(?![a-z0-9]))",
    re.IGNORECASE)

_FILM_SITES = re.compile(
    r"(?<![a-z0-9])(?:tamilrockers|tamil ?blasters|1tamilmv|tamilmv|isaidub|moviesda|tamilyogi|"
    r"tamilgun|filmyzilla|filmywap|vegamovies|katmoviehd|hdhub4u|mkvcinemas|moviesflix|"
    r"bolly4u|khatrimaza|worldfree4u|movierulz|jalshamoviez|kuttymovies|madrasrockers|"
    r"yify|yts|rarbg|eztv|ettv|tgx|torrentgalaxy|psa|pahe|galaxyrg|qxr|tigole|"
    r"evo|sparks|ganool|mkvcage|shaanig)(?![a-z0-9])",
    re.IGNORECASE)
_MUSIC_SITES = re.compile(
    r"(?<![a-z0-9])(?:mass ?tamilan|starmusiq|5starmusiq|friendstamilmp3|tamilwire|isaimini|"
    r"tamiltunes|tamilbeat|isaiaruvi|sensongs|kuttyweb|tamilanda|naasongs|songspk|"
    r"pagalworld|mr-?jatt|djmaza|downloadming|wapking|djpunjab|raagtune|ytmp3|y2mate|"
    r"savefrom|mp3juices|spotdl)(?![a-z0-9])",
    re.IGNORECASE)

_RELEASE_SOURCE = re.compile(
    r"(?<![a-z0-9])(?:blu-?ray|bdrip|brrip|bdremux|web-?dl|webrip|hdrip|dvdrip|dvdscr|hdtv|"
    r"hdcam|camrip|predvd|hdts|hq ?hdrip|untouched)(?![a-z0-9])", re.IGNORECASE)
_EPISODE = re.compile(r"(?<![a-z0-9])s\d{1,2} ?e\d{1,3}(?![0-9])", re.IGNORECASE)
_ENCODING = re.compile(
    r"(?<![a-z0-9])(?:x264|x265|h ?264|h ?265|hevc|xvid|divx|10bit|aac ?[25] ?[01]|"
    r"ddp? ?5 ?1|dts(?:-hd)?|atmos|truehd|e-?subs?|m-?subs?|dual ?audio|multi ?audio|"
    r"dubbed|hq ?line ?audio)(?![a-z0-9])", re.IGNORECASE)
_FILM_WORDS = re.compile(
    r"(?<![a-z0-9])(?:full ?movie|movie|official trailer|trailer|teaser|full video songs?|"
    r"video songs?|web ?series|full episode|episode \d{1,3}|season \d{1,2})(?![a-z0-9])",
    re.IGNORECASE)
_STUDIOS = re.compile(
    r"(?<![a-z0-9])(?:sun nxt|sun tv|star vijay|vijay tv|zee5|zee tamil|sonyliv|hotstar|"
    r"netflix|amazon prime|aha video|ayngaran|rajshri|goldmines|shemaroo|eros now|"
    r"pyramid saimira|lyca productions|t-series|sony music|saregama|think music|lahari|"
    r"aditya music|zee music|divo|u1 records|muzik ?247|speed records|sun music|"
    r"tips official|yrf|mango music|junglee music)(?![a-z0-9])", re.IGNORECASE)
_RESOLUTION = re.compile(r"(?<![a-z0-9])(?:480p|576p|720p|1080p|2160p|4k ?uhd|uhd)(?![a-z0-9])",
                         re.IGNORECASE)
_TITLE_YEAR = re.compile(r"[a-z].*?[(\[. ](19[2-9]\d|20[0-4]\d)(?:[)\]. ]|$)", re.IGNORECASE)
_SONG_WORDS = re.compile(
    r"(?<![a-z0-9])(?:lyrics?|lyrical|official (?:audio|video|song)|full songs?|audio songs?|"
    r"video songs?|\d{2,3} ?kbps|kbps|remix|bgm|ringtone|instrumental|ost|theme music|"
    r"title (?:track|song)|jukebox|hd audio|hq audio|8d audio|slowed|reverb|lofi)(?![a-z0-9])",
    re.IGNORECASE)

# Folder names that suggest a collection. Weak on their own: "Songs" is also
# where a family keeps the grandchildren singing.
_FILM_FOLDER = re.compile(r"(?<![a-z0-9])(?:movies|films|cinema|tv shows|series|season \d+)(?![a-z0-9])",
                          re.IGNORECASE)
_MUSIC_FOLDER = re.compile(
    r"(?<![a-z0-9])(?:music|songs|album|albums|discography|soundtrack|hits|mp3|mp3s|flac|"
    r"playlist|jukebox|best of|greatest hits)(?![a-z0-9])", re.IGNORECASE)

_FILM_LEFTOVERS = re.compile(
    r"^(?:.*\.nfo|poster\..*|fanart\..*|banner\..*|clearlogo\..*|clearart\..*|landscape\..*|"
    r"movie\.xml|.*[ ._-]sample\.\w+|sample\.\w+)$", re.IGNORECASE)
_MUSIC_LEFTOVERS = re.compile(
    r"^(?:.*\.cue|.*\.m3u8?|.*\.pls|.*\.accurip|cover\.(?:jpe?g|png)|folder\.(?:jpe?g|png)|"
    r"front\.(?:jpe?g|png)|albumart.*\.jpe?g)$", re.IGNORECASE)
_ARTWORK = {
    FILM: re.compile(r"^(?:poster|fanart|banner|clearlogo|clearart|landscape|folder|cover)"
                     r"(?:[ ._-].*)?\.(?:jpe?g|png|webp)$", re.IGNORECASE),
    MUSIC: re.compile(r"^(?:cover|folder|front|back|albumart.*)\.(?:jpe?g|png)$", re.IGNORECASE),
}
_SUBTITLES = {"srt", "sub", "idx", "ass", "ssa", "vtt"}

# --- what personal files look like -----------------------------------------

_CAMERA_NAME = re.compile(
    r"^(?:img|vid|mvi|mov|dsc[nf]?|pxl|gopr|g[hxl]\d\d|dji|mah|maq|sam|wp|trim|pano|"
    r"hnvc|clip|mvi)[_\- ]?\d"
    r"|^\d{8}[_ -]\d{6}"
    r"|^(?:vid|img)-\d{8}-wa\d+"
    r"|^whatsapp video"
    r"|^screen ?recording|^rpreplay|^screenrecord"
    r"|^(?:signal|telegram|snapchat)-",
    re.IGNORECASE)
_CAMERA_EXTS = {"mts", "tod", "mod", "insv", "lrv", "3gp", "3g2", "dv"}

_OCCASIONS = re.compile(
    r"(?<![a-z])(?:wedding|weds|marriage|engagement|nichayathartham|nischayathartham|reception|"
    r"birthday|bday|b'day|anniversary|baby shower|seemantham|valaikappu|valaikaappu|"
    r"naming ceremony|namakaranam|christening|baptism|first communion|graduation|convocation|"
    r"annual day|sports day|recital|school play|housewarming|grihapravesam|gruhapravesam|"
    r"puberty|manjal neerattu|upanayanam|sangeet|mehndi|mehendi|haldi)(?![a-z])",
    re.IGNORECASE)

_RECORDER_NAME = re.compile(
    r"^(?:ptt|aud)-\d{8}-wa\d+"
    r"|^(?:voice|recording|new recording|rec|record|memo|call|audio|vn|sound)[\s_\-]*\d"
    r"|voice ?(?:memo|note|message|recording)|call ?record|call@|^whatsapp audio"
    r"|^(?:ds|dw|dm|ws|ls|icd|zoom|tascam|ste|r09|dr)[_\-]?[a-z]?\d{3,}",
    re.IGNORECASE)
_RECORDER_EXTS = {"amr", "3ga", "awb", "dss", "ds2", "msv", "dvf", "gsm", "qcp", "sln", "voc"}
_AUDIOBOOK_EXTS = {"m4b", "aa", "aax"}
_SPOKEN_GENRES = re.compile(
    r"(?<![a-z])(?:speech|spoken|audiobook|audio book|podcast|talk|sermon|discourse|"
    r"pravachan|pravachanam|upanyasam|satsang|interview|lecture)(?![a-z])", re.IGNORECASE)
_RECORDER_TOOLS = re.compile(r"voice ?memo|voicememos|recorder|call record", re.IGNORECASE)


def _timestamp_only(stem: str) -> bool:
    """``11-02-21-20-36-23`` or ``20190607_123456``: a recorder, not a title."""
    return bool(re.fullmatch(r"[\d\s_\-.()]+", stem)) and sum(c.isdigit() for c in stem) >= 8


def _personal_by_name(file: str) -> str | None:
    """Why an audio file is personal from its name or format alone, or None."""
    stem, ext = _stem(file), _ext(file)
    if _RECORDER_NAME.search(stem) or _timestamp_only(stem):
        return f"named the way a recorder names a recording ({file})"
    if ext in _RECORDER_EXTS:
        return f"saved in a voice recorder's format (.{ext})"
    if ext in _AUDIOBOOK_EXTS:
        return f"an audiobook, which is speech (.{ext})"
    return None


def reads_tags(file: str) -> bool:
    """Whether judging this audio file reads its tags, so a walk can read them early."""
    return _personal_by_name(file) is None


# ---------------------------------------------------------------------------
# The judgement
# ---------------------------------------------------------------------------

@dataclass
class FileVerdict:
    """What one file looks like, and why."""

    name: str
    category: str
    size: int = 0
    score: int = 0
    named: int = 0                 # points from names and tags
    reasons: list[str] = field(default_factory=list)
    personal: str | None = None    # why it is never held back on a guess
    decisive: bool = False         # a clue nobody gives a home video
    length: float | None = None    # seconds, when the tags said

    def add(self, points: int, reason: str, *, named: bool = True) -> None:
        self.score += points
        if named:
            self.named += points
        self.reasons.append(reason)

    @property
    def suspected(self) -> bool:
        return self.score >= THRESHOLD and self.named >= NAMED_MINIMUM

    @property
    def held(self) -> bool:
        return self.suspected and not self.personal


@dataclass
class FolderJudgement:
    files: list[FileVerdict] = field(default_factory=list)
    #: Cover art and posters that go with held files, by category.
    artwork: dict[str, list[str]] = field(default_factory=dict)


#: A web address the way re-uploaders stamp one: ``www.`` or in brackets. A
#: bare ``.com`` in a video's name is as often a lesson about a website
#: (``115 WhatsTheTime.com.mp4``) or a meeting as a release.
_WEB_STAMP = re.compile(r"www\.[a-z0-9-]+\.[a-z]{2,6}|[\[(]\s*(?:www\.)?[a-z0-9-]+\.[a-z]{2,6}\s*[\])]",
                        re.IGNORECASE)


#: Where a family's own videos and recordings come back from. A clip saved from
#: YouTube, Facebook or Drive carries the address, and is still the family's.
_OWN_PLACES = re.compile(r"youtube|youtu\.be|facebook|fb\.com|instagram|whatsapp|google|icloud|"
                         r"dropbox|onedrive|vimeo|tiktok|telegram|zoom\.us|teams", re.IGNORECASE)


def _site(text: str, sites: re.Pattern, web: re.Pattern = _WEB) -> str | None:
    match = sites.search(text)
    if not match:
        match = next((m for m in web.finditer(text) if not _OWN_PLACES.search(m.group(0))), None)
    return match.group(0).strip("[]() ") if match else None


def _film_names(verdict: FileVerdict, text: str, folder: str, stem: str) -> None:
    words = _words(text)
    site = _site(text, _FILM_SITES, _WEB_STAMP)
    if site:
        verdict.add(4, f"a release site in the name ({site})")
        verdict.decisive = True
    source = _RELEASE_SOURCE.search(words)
    if source:
        verdict.add(4, f"a release tag in the name ({source.group(0)})")
        verdict.decisive = True
    episode = _EPISODE.search(words)
    if episode:
        verdict.add(4, f"an episode number ({episode.group(0).upper()})")
        verdict.decisive = True
    encoding = _ENCODING.search(words)
    if encoding:
        verdict.add(2, f"encoding tags in the name ({encoding.group(0)})")
    wording = _FILM_WORDS.search(words) or _STUDIOS.search(words)
    if wording:
        verdict.add(2, f"film wording in the name (\"{wording.group(0)}\")")
    year = _TITLE_YEAR.search(stem) or _TITLE_YEAR.search(folder)
    if year and _words(_stem(stem)).strip() == _words(folder).strip():
        verdict.add(2, "named like a media server's film library (\"Title (Year)\")")
    elif year:
        verdict.add(1, f"a release year in the name ({year.group(1)})")
    resolution = _RESOLUTION.search(words)
    if resolution:
        verdict.add(1, f"a resolution in the name ({resolution.group(0)})")


def _music_names(verdict: FileVerdict, text: str) -> None:
    words = _words(text)
    site = _site(text, _MUSIC_SITES)
    if site:
        verdict.add(4, f"a music site in the name ({site})")
        verdict.decisive = True
    wording = _SONG_WORDS.search(words) or _STUDIOS.search(words)
    if wording:
        verdict.add(2, f"song wording in the name (\"{wording.group(0)}\")")


def _numbered(names: list[str]) -> bool:
    from .course_material import numbered_lessons
    return numbered_lessons([_stem(n) for n in names])


def judge_folder(folder: str, subdirs: list[str], files: list[str], *, kind_of,
                 allowed=("image", "video", "audio"), size_of=None, min_bytes: int = 0,
                 tags=None, probe=None, listen_to=None, checkpoint=None) -> FolderJudgement:
    """Judge the videos and audio directly inside *folder*.

    *kind_of* maps a file name to ``'image' | 'video' | 'audio' | None``;
    *size_of*, *tags*, *probe* and *listen_to* read the disk and are passed in
    so a caller can cache them and a test can stand in for them. Only kinds in
    *allowed* are judged, and only files of at least *min_bytes*.
    """
    tags = tags or read_tags
    probe = probe or probe_video
    listen_to = listen_to or (lambda path, length: listen(path, length))
    size_of = size_of or (lambda path: os.path.getsize(path))
    name = os.path.basename(folder.rstrip("\\/"))
    judged = FolderJudgement()

    def tick():
        if checkpoint:
            checkpoint()

    def sized(names):
        out = {}
        for n in names:
            try:
                size = size_of(os.path.join(folder, n))
            except OSError:
                continue
            if size >= min_bytes:
                out[n] = size
        return out

    videos = sized([f for f in files if kind_of(f) == "video"]) if "video" in allowed else {}
    audio = sized([f for f in files if kind_of(f) == "audio"]) if "audio" in allowed else {}
    lowered = [f.lower() for f in files] + [d.lower() for d in subdirs]

    # --- films -----------------------------------------------------------
    film_context = []
    if videos:
        leftovers = [f for f in files if _FILM_LEFTOVERS.match(f)]
        leftovers += [d for d in subdirs if d.lower() in ("sample", "samples", "subs", "subtitles")]
        if leftovers:
            film_context.append((2, f"a film folder's leftovers ({', '.join(sorted(leftovers)[:3])})"))
        if any(_ext(f) in _SUBTITLES for f in lowered):
            film_context.append((1, "subtitles beside the video"))
        if _FILM_FOLDER.search(_words(name)):
            film_context.append((1, f"kept in a folder called \"{name}\""))

    film = []
    for file, size in videos.items():
        tick()
        stem = _stem(file)
        verdict = FileVerdict(file, FILM, size)
        _film_names(verdict, f"{name} / {stem}", name, stem)
        if _CAMERA_NAME.match(stem) or (_ext(file) in _CAMERA_EXTS):
            verdict.personal = f"named or saved the way a camera or phone saves video ({file})"
        elif _OCCASIONS.search(_words(stem)) and not verdict.decisive:
            verdict.personal = f"a family occasion in the name ({_OCCASIONS.search(_words(stem)).group(0)})"
        film.append(verdict)

    for verdict in film:
        for points, reason in film_context:
            verdict.add(points, reason)
    _neighbours(film, "films")

    for verdict in film:
        if verdict.personal and _CAMERA_NAME.match(_stem(verdict.name)):
            continue
        if verdict.named < NAMED_MINIMUM and verdict.size < LARGE_VIDEO_BYTES:
            continue           # the names say too little for a measurement to matter
        tick()
        data = probe(os.path.join(folder, verdict.name))
        if data:
            _probe_clues(verdict, data)

    # --- music -----------------------------------------------------------
    music_context = []
    if len(audio) >= 3:
        if _numbered(list(audio)):
            music_context.append((2, f"{len(audio)} numbered tracks"))
    if audio:
        leftovers = [f for f in files if _MUSIC_LEFTOVERS.match(f)]
        if leftovers:
            music_context.append((2, f"an album's leftovers ({', '.join(sorted(leftovers)[:3])})"))
        if _MUSIC_FOLDER.search(_words(name)):
            music_context.append((1, f"kept in a folder called \"{name}\""))

    music = []
    for file, size in audio.items():
        tick()
        stem = _stem(file)
        verdict = FileVerdict(file, MUSIC, size)
        _music_names(verdict, f"{name} / {stem}")
        length = None
        personal = _personal_by_name(file)
        if personal:
            verdict.personal = personal
        else:
            info = tags(os.path.join(folder, file)) or {}
            length = info.get("length")
            _tag_clues(verdict, info)
        verdict.length = length
        music.append(verdict)

    for verdict in music:
        for points, reason in music_context:
            verdict.add(points, reason)
    _neighbours(music, "music")

    # Listen before holding anything: a recording with the pauses of speech is
    # somebody talking, whatever it is called or tagged.
    for verdict in music:
        if verdict.held:
            tick()
            heard = listen_to(os.path.join(folder, verdict.name), verdict.length)
            if heard is None:
                verdict.reasons.append("not listened to: the sound could not be decoded here")
            elif heard[0] == "speech":
                verdict.personal = heard[1]

    judged.files = [v for v in film + music if v.suspected]
    if "image" in allowed:
        for category in CATEGORIES:
            if any(v.held and v.category == category for v in judged.files):
                art = sized([f for f in files if kind_of(f) == "image" and _ARTWORK[category].match(f)])
                if art:
                    judged.artwork[category] = sorted(art)
    return judged


def _neighbours(verdicts: list[FileVerdict], label: str) -> None:
    """Most of the files beside it are films (or songs) on their own: tip a near miss."""
    alone = sum(1 for v in verdicts if v.held)
    if alone >= 3 and alone * 2 >= len(verdicts):
        for verdict in verdicts:
            if not verdict.held and NAMED_MINIMUM <= verdict.score < THRESHOLD:
                verdict.add(2, f"kept among {alone} other files that are {label}")


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

_TAG_KEYS = {
    "album": ("TALB", "\xa9alb", "album"),
    "artist": ("TPE1", "TPE2", "\xa9ART", "aART", "artist", "albumartist"),
    "track": ("TRCK", "trkn", "tracknumber"),
    "genre": ("TCON", "\xa9gen", "genre"),
    "tool": ("TSSE", "TENC", "\xa9too", "encoder", "encoded-by", "encodedby"),
}


def read_tags(path: str) -> dict | None:
    """The tags and stream facts that matter here, from mutagen; None if unreadable."""
    try:
        import mutagen
        audio = mutagen.File(path)
    except Exception:                                   # noqa: BLE001 - unreadable
        return None
    if audio is None:
        return None
    info = getattr(audio, "info", None)
    out = {"channels": getattr(info, "channels", None),
           "sample_rate": getattr(info, "sample_rate", None),
           "length": getattr(info, "length", None),
           "picture": bool(getattr(audio, "pictures", None)),
           "text": []}
    found: dict[str, str] = {}
    try:
        items = list((audio.tags or {}).items())
    except Exception:                                   # noqa: BLE001
        items = []
    for key, value in items:
        base = str(key).split(":")[0] if str(key)[:1].isupper() else str(key)
        if base in ("APIC", "covr", "metadata_block_picture"):
            out["picture"] = True
            continue
        raw = getattr(value, "text", value)
        if isinstance(raw, (list, tuple)):
            text = " ".join(str(v) for v in raw)
        else:
            text = str(raw)
        if base in ("WXXX", "WOAR", "WCOM", "WPUB"):
            text = str(getattr(value, "url", text))
        text = text.strip()
        if not text or len(text) > 500:
            continue
        out["text"].append(text)
        for field_name, keys in _TAG_KEYS.items():
            if base in keys or base.lower() in keys:
                found.setdefault(field_name, text)
    out.update(found)
    return out


def _tag_clues(verdict: FileVerdict, info: dict) -> None:
    text = " | ".join(info.get("text") or [])
    if text:
        site = _site(text, _MUSIC_SITES)
        if site and not any("music site" in r for r in verdict.reasons):
            verdict.add(4, f"a music site in the tags ({site})")
            verdict.decisive = True
    if info.get("album") and info.get("artist"):
        verdict.add(2, "album and artist tags")
    if info.get("track"):
        verdict.add(1, "a track number tag")
    if info.get("picture"):
        verdict.add(1, "cover art inside the file")

    channels, rate = info.get("channels"), info.get("sample_rate")
    genre, tool = info.get("genre") or "", info.get("tool") or ""
    if channels == 1:
        verdict.personal = "recorded in mono, like a voice note"
    elif rate and rate < 32000:
        verdict.personal = f"recorded at voice-note quality ({rate // 1000} kHz)"
    elif _RECORDER_TOOLS.search(tool) or _RECORDER_TOOLS.search(text):
        verdict.personal = f"made by a recording app ({(tool or text)[:40]})"
    elif _SPOKEN_GENRES.search(genre):
        verdict.personal = f"tagged as spoken word ({genre[:30]})"


# ---------------------------------------------------------------------------
# Probing a video
# ---------------------------------------------------------------------------

def probe_video(path: str) -> dict | None:
    """ffprobe's description of a file, or None without ffprobe or on failure."""
    if not FFPROBE:
        return None
    try:
        proc = subprocess.run(
            [FFPROBE, "-v", "quiet", "-print_format", "json", "-show_format",
             "-show_streams", "-show_chapters", path],
            capture_output=True, timeout=30, check=False)
        if proc.returncode != 0:
            return None
        return json.loads(proc.stdout or b"{}")
    except (subprocess.SubprocessError, ValueError, OSError):
        return None


_CAMERA_TAGS = ("com.apple.quicktime.make", "com.apple.quicktime.model",
                "com.apple.quicktime.location.iso6709", "make", "model", "location",
                "location-eng", "com.android.version", "com.android.capture.fps", "firmware")
_CAMERA_MAKERS = re.compile(r"gopro|dji|insta360|ambarella|canon|nikon|sony dsc|panasonic|"
                            r"samsung|apple|xiaomi|oneplus|huawei|motorola|google pixel", re.IGNORECASE)


def _probe_clues(verdict: FileVerdict, data: dict) -> None:
    fmt = data.get("format") or {}
    tags = {str(k).lower(): str(v) for k, v in (fmt.get("tags") or {}).items()}
    streams = data.get("streams") or []

    for key in _CAMERA_TAGS:
        if tags.get(key):
            make = tags.get("com.apple.quicktime.make") or tags.get("make") or ""
            model = tags.get("com.apple.quicktime.model") or tags.get("model") or ""
            detail = " ".join(x for x in (make, model) if x) or key
            verdict.personal = f"recorded on a camera or phone ({detail[:40]})"
            break
    else:
        handlers = " ".join(str((s.get("tags") or {}).get("handler_name", "")) for s in streams)
        maker = _CAMERA_MAKERS.search(tags.get("encoder", "") + " " + handlers)
        if maker and "lavf" not in tags.get("encoder", "").lower():
            verdict.personal = f"recorded on a camera ({maker.group(0)})"

    texts = [v for k, v in tags.items() if k in ("title", "comment", "description", "encoded_by",
                                                    "copyright", "artist", "album", "synopsis")]
    texts += [str((s.get("tags") or {}).get("title", "")) for s in streams]
    site = _site(" | ".join(texts), _FILM_SITES, _WEB_STAMP)
    if site and not any("release site" in r for r in verdict.reasons):
        verdict.add(4, f"a release site in its tags ({site})")
        verdict.decisive = True

    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    if video and video.get("width") and video.get("height"):
        ratio = 1.0
        sar = str(video.get("sample_aspect_ratio") or "1:1")
        try:
            a, b = (int(x) for x in sar.split(":"))
            ratio = a / b if a and b else 1.0
        except ValueError:
            pass
        aspect = video["width"] * ratio / video["height"]
        if aspect >= 2.0 and video["height"] >= 200:
            verdict.add(2, f"a cinema-shaped frame ({aspect:.2f}:1)", named=False)
    soundtracks = [s for s in streams if s.get("codec_type") == "audio"]
    if len(soundtracks) >= 2:
        languages = sorted({str((s.get("tags") or {}).get("language", "")) for s in soundtracks} - {"", "und"})
        verdict.add(2, "several soundtracks" + (f" ({', '.join(languages[:4])})" if languages else ""),
                    named=False)
    if any(s.get("codec_type") == "subtitle" for s in streams):
        verdict.add(2, "subtitles inside the file", named=False)
    try:
        minutes = float(fmt.get("duration") or 0) / 60
    except ValueError:
        minutes = 0
    if minutes >= 75:
        verdict.add(1, f"feature length ({minutes:.0f} minutes)", named=False)
    if len(data.get("chapters") or []) >= 2:
        verdict.add(1, "chapters, like a disc", named=False)


# ---------------------------------------------------------------------------
# Listening
# ---------------------------------------------------------------------------

#: Excerpts scoring at least this are speech-like. Measured on real recordings:
#: seven downloaded film songs scored 0.12-0.27 away from their fade-in and
#: fade-out, meeting recordings and a voice note 0.41-0.90.
SPEECH_SCORE = 0.33
_RATE = 16000


def listen(path: str, length: float | None = None, *, decoder=None) -> tuple[str, str] | None:
    """Whether a recording sounds like ``'speech'`` or ``'music'``, and why.

    Speech stops: between words, between sentences, while somebody thinks.
    Music, including songs, keeps sounding. Three 20-second excerpts from inside
    the file (a quarter, half and three quarters through, clear of intros and
    fades) are each scored by how much of them is near silence plus half their
    share of quiet moments; the middle score decides. Anything silent or
    undecidable counts as speech, so a mistake keeps a file rather than holding
    it back. None when the sound cannot be decoded at all.
    """
    decoder = decoder or _decode
    if length and length > 60:
        excerpts = [(length * f - 10, 20.0) for f in (0.25, 0.5, 0.75)]
    else:
        excerpts = [(0.0, 60.0)]
    scores = []
    for start, seconds in excerpts:
        samples = decoder(path, max(0.0, start), seconds)
        if samples is None:
            return None
        if len(samples) < _RATE:           # under a second: nothing to judge
            continue
        scores.append(_speech_score(samples))
    if not scores:
        return "speech", "too short to tell, so treated as a recording"
    scores.sort()
    middle = scores[len(scores) // 2]
    if middle >= SPEECH_SCORE:
        return "speech", "sounds like speech: it pauses the way people do between words"
    return "music", "sounds like continuous music"


def _speech_score(samples) -> float:
    import numpy as np
    x = np.asarray(samples, dtype=np.float32)
    frame, hop = int(0.025 * _RATE), int(0.010 * _RATE)
    count = 1 + (len(x) - frame) // hop
    if count < 100:
        return 1.0
    energy = np.empty(count, dtype=np.float64)
    window = np.hanning(frame).astype(np.float32)
    for i in range(count):
        chunk = x[i * hop:i * hop + frame] * window
        energy[i] = float(np.mean(chunk * chunk))
    energy += 1e-10
    loudest = np.percentile(energy, 95)
    if loudest < 1e-7:                     # about -70 dBFS: silence, not music
        return 1.0
    decibels = 10 * np.log10(energy)
    pauses = float(np.mean(decibels < 10 * np.log10(loudest) - 30))
    quiet = []
    for start in range(0, count - 99, 100):   # one-second windows
        second = energy[start:start + 100]
        quiet.append(float(np.mean(second < 0.5 * second.mean())))
    return pauses + 0.5 * (float(np.mean(quiet)) if quiet else 0.0)


def _decode(path: str, start: float, seconds: float):
    """Mono 16 kHz samples in [-1, 1], through ffmpeg, or a plain WAV without it."""
    import numpy as np
    if FFMPEG:
        try:
            proc = subprocess.run(
                [FFMPEG, "-v", "quiet", "-ss", f"{start:.2f}", "-i", path, "-t", f"{seconds:.2f}",
                 "-vn", "-ac", "1", "-ar", str(_RATE), "-f", "s16le", "-"],
                capture_output=True, timeout=60, check=False)
        except (subprocess.SubprocessError, OSError):
            return None
        if proc.returncode != 0:
            return None
        return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    if _ext(path) not in ("wav", "wave"):
        return None
    return _decode_wav(path, start, seconds)


def _decode_wav(path: str, start: float, seconds: float):
    import wave
    import numpy as np
    try:
        with wave.open(path, "rb") as handle:
            rate, channels, width = handle.getframerate(), handle.getnchannels(), handle.getsampwidth()
            if width != 2 or not rate:
                return None
            handle.setpos(min(handle.getnframes(), int(start * rate)))
            raw = handle.readframes(int(seconds * rate))
    except (OSError, EOFError, wave.Error):
        return None
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples[: len(samples) // channels * channels].reshape(-1, channels).mean(axis=1)
    if rate != _RATE:
        positions = np.arange(0, len(samples), rate / _RATE)
        samples = np.interp(positions, np.arange(len(samples)), samples).astype(np.float32)
    return samples


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------

def common_reasons(verdicts: list[FileVerdict], limit: int = 4, personal: bool = False) -> list[str]:
    """The reasons most files share, most common first."""
    counts: Counter[str] = Counter()
    for verdict in verdicts:
        if personal:
            if verdict.personal:
                counts[verdict.personal] += 1
        else:
            counts.update(dict.fromkeys(verdict.reasons, 1))
    return [f"{reason} ({n} files)" if n > 1 else reason
            for reason, n in counts.most_common(limit)]
