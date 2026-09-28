"""Recognising a folder of course material, so it is not archived as memories.

A drive that has been in a family a while collects more than photographs. The
commonest stowaway is a downloaded course: a folder of numbered lesson videos,
some slides and source code, and the calling card of whichever site it came
from. Archived, each lecture becomes a "video" filed under the day it was
downloaded, and a library meant for birthdays fills with screen recordings of
somebody else's code editor.

No single clue is reliable, but together they are. A folder is judged as a
whole, from what the walk already has in hand when it arrives there -- its own
name, the names of the folders directly inside it, and the names of its files
-- so recognising a course costs no extra reads of the disk. Each clue adds
points; a folder at or above :data:`THRESHOLD` is course material.

What a course looks like, in points:

* **4** - the tag of a course-sharing site in the name: ``[FreeTutorials.Us]``,
  ``[FTUForum.com]``, ``Udemy``, ``Gooner``. Nobody names a holiday that.
* **2** - numbered lessons: most sub-folders (or most videos) numbered 1..N in
  order, each with a title, like ``01 Start Here`` ... ``17 Wrap Up``.
* **2** - course vocabulary in the name: course, tutorial, masterclass,
  training, bootcamp, "become a", "beginner to", "learn". Not "lessons" or
  "workshop": swimming lessons and a pottery workshop are exactly the family
  videos this must never hold back.
* **1 each, up to 2** - a technical subject in the name: java, spring, angular,
  photoshop, agile ...
* **2** - leftovers a download site ships with: ``.url`` shortcuts, ``.nfo``
  files, a text file named after the site.
* **1** - subtitles beside the videos.
* **2** - context, for a folder already on 2 or 3 points: most of the folders
  beside it are named like courses. A download collection is kept together,
  and a folder called ``Agile project management`` among thirty Udemy courses
  is one more course; the same name among holiday folders is not.

A trip folder of numbered days scores 2 and is left alone. The judgement is
deliberately a *suggestion*: nothing is excluded until somebody agrees, and a
folder holding photographs from a real camera is never held back on its own
say-so -- see :func:`camera_photographs`.
"""
from __future__ import annotations

import ntpath
import os
import re
from dataclasses import dataclass, field

#: Points at which a folder is treated as course material.
THRESHOLD = 4

VIDEO_EXTS = {"mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v", "ts", "mpg", "mpeg"}
SUBTITLE_EXTS = {"srt", "vtt", "ass", "ssa", "sub"}
PHOTO_EXTS = {"jpg", "jpeg", "heic", "heif", "tif", "tiff", "dng", "cr2", "cr3",
              "nef", "arw", "orf", "rw2", "raf", "pef", "srw"}

# Course-sharing and course-selling sites, as they appear in folder names.
_SITES = (
    "freetutorials", "ftuforum", "freecourseweb", "freecoursesonline", "tutsgalaxy",
    "courseclub", "desiredcourse", "coursedrive", "getfreecourses", "tutflix",
    "freeonlinemovies", "udemy", "coursera", "lynda", "pluralsight",
    "skillshare", "infiniteskills", "cbt nuggets", "cbtnuggets", "udacity",
    "frontendmasters", "frontend masters", "zerotomastery",
    "linkedin learning", "oreilly", "o'reilly", "domestika", "masterclass.com",
)
# A bracketed web address - the house style of every re-uploader.
# Short or everyday words that only count as a whole word, or in the form the
# uploader uses: "Gooner" is also every Arsenal supporter, so only the
# "(Pdf) Gooner" signature of the ebook uploader counts.
_SITE_WORDS = re.compile(r"(?<![a-z])(?:edx|packt|egghead)(?![a-z])|\((?:pdf|epub|mobi)\)\s*gooner",
                         re.IGNORECASE)
_SITE_TAG = re.compile(r"[\[(][^\])]*\.(?:us|com|co|net|org|to|cc|me|io|in|info|xyz|ws|club)[\])]",
                       re.IGNORECASE)
_VOCABULARY = re.compile(
    r"\b(?:course|courses|tutorial|tutorials|masterclass|master\s*class|training|bootcamp|"
    r"lectures?|crash[\s_-]*course|complete[\s_-]*guide|essentials|"
    r"beginner[\s_-]*to|from[\s_-]*beginner|zero[\s_-]*to|become[\s_-]*an?|learn|"
    r"certified|certification|nanodegree)\b",
    re.IGNORECASE)
_SUBJECTS = re.compile(
    r"(?<![a-z])(?:java|javascript|node\.?js|angular(?:js)?|react|vue|spring|hibernate|python|"
    r"django|flask|android|ios|swift|kotlin|aws|gcp|azure|devops|jenkins|docker|kubernetes|"
    r"sql|mongodb|html|css|api|rest|soap|algorithms?|data[\s_-]*structures|"
    r"design[\s_-]*patterns|photoshop|premiere|illustrator|excel|agile|scrum|pmp|"
    r"machine[\s_-]*learning|web[\s_-]*development|ethical[\s_-]*hacking|ionic|"
    r"microservices|typescript|c\+\+|c#|golang|linux)(?![a-z])",
    re.IGNORECASE)
# "01 Start Here", "1. Introduction", "002_Setup" - a number, then a title.
_NUMBERED = re.compile(r"^\s*(\d{1,3})\s*[.)_\-:]?\s+?[A-Za-z]")


@dataclass
class Assessment:
    """How much a folder looks like course material, and why."""

    score: int = 0
    reasons: list[str] = field(default_factory=list)

    @property
    def is_course(self) -> bool:
        return self.score >= THRESHOLD

    def add(self, points: int, reason: str) -> None:
        self.score += points
        self.reasons.append(reason)


def _ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _numbered_run(names: list[str]) -> bool:
    """Most names numbered, starting near one and mostly in sequence.

    Starting near one is what separates lessons from dates: a folder of
    ``2019-06 Paris`` style names starts in the thousands and is not a run.
    """
    if len(names) < 3:
        return False
    numbers = []
    for name in names:
        match = _NUMBERED.match(name)
        if match:
            numbers.append(int(match.group(1)))
    if len(numbers) < 3 or len(numbers) < 0.6 * len(names):
        return False
    numbers = sorted(set(numbers))
    if numbers[0] > 1:
        return False
    steps = sum(1 for a, b in zip(numbers, numbers[1:]) if b - a == 1)
    return steps >= 0.6 * (len(numbers) - 1)


def numbered_lessons(names: list[str]) -> bool:
    """Whether *names* read as a course's numbered lessons (see the module notes)."""
    return _numbered_run(names)


def named_like_a_course(name: str) -> bool:
    """Whether a name alone says course: a site tag, or course wording."""
    lowered = name.lower()
    return bool(any(s in lowered for s in _SITES) or _SITE_WORDS.search(name)
                or _SITE_TAG.search(name)
                or _VOCABULARY.search(name.replace("_", " ")))


def course_neighbourhood(sibling_names: list[str]) -> int:
    """How many of a parent's folders are named like courses, if most are.

    Zero unless at least three are and they make up at least half, so one
    tutorial folder among a family's albums lends nothing to its neighbours.
    """
    named = sum(1 for n in sibling_names if named_like_a_course(n))
    return named if named >= 3 and named >= 0.5 * len(sibling_names) else 0


def assess(name: str, subdirs: list[str], files: list[str],
           neighbours: int = 0) -> Assessment:
    """Judge one folder from its name and the names directly inside it.

    *neighbours* is :func:`course_neighbourhood` for the folder it sits in.
    """
    result = Assessment()
    lowered = name.lower()

    site = next((s for s in _SITES if s in lowered), None)
    if site is None and _SITE_WORDS.search(name):
        site = _SITE_WORDS.search(name).group(0)
    if site or _SITE_TAG.search(name):
        tag = _SITE_TAG.search(name)
        result.add(4, f"named for a course site ({tag.group(0) if tag else site})")

    videos = [f for f in files if _ext(f) in VIDEO_EXTS]
    if _numbered_run(subdirs):
        result.add(2, f"{len(subdirs)} numbered lesson folders")
    elif len(videos) >= 3 and _numbered_run(videos):
        result.add(2, f"{len(videos)} numbered lesson videos")

    vocabulary = _VOCABULARY.search(name.replace("_", " "))
    if vocabulary:
        result.add(2, f"course wording in the name (\"{vocabulary.group(0)}\")")

    subjects = []
    for match in _SUBJECTS.finditer(name.replace("_", " ")):
        word = match.group(0).lower()
        if word not in subjects:
            subjects.append(word)
    if subjects:
        points = min(2, len(subjects))
        result.add(points, "a technical subject in the name (" + ", ".join(subjects[:3]) + ")")

    leftovers = [f for f in files
                 if _ext(f) in {"url", "nfo"}
                 or (_ext(f) == "txt" and any(s in f.lower() for s in _SITES))
                 or (_ext(f) == "txt" and _SITE_TAG.search(f))]
    if leftovers:
        result.add(2, f"download-site leftovers ({', '.join(sorted(leftovers)[:3])})")

    if videos and any(_ext(f) in SUBTITLE_EXTS for f in files):
        result.add(1, "subtitles beside the videos")

    # Context can tip a borderline folder, never make one out of nothing.
    if neighbours and 2 <= result.score < THRESHOLD:
        result.add(2, f"kept among {neighbours} other folders named like courses")

    return result


def camera_photographs(folder: str, *, limit: int = 40, walk_limit: int = 20000,
                       reader=None, checkpoint=None) -> int | None:
    """How many photographs in *folder* came from a real camera, up to *limit*.

    The safety net under everything above. A course about photography can
    carry the teacher's sample shots, and a folder that merely *looks* like a
    course might be somebody's own. A photograph with a camera make, model or
    GPS position in its EXIF is the one thing a download site does not produce
    in bulk, so a folder containing one is never held back without a person
    deciding. Reads at most *limit* photos and lists at most *walk_limit*
    entries, in sorted order, so the answer is the same every run.
    """
    reader = reader or _has_camera_exif
    checked = found = listed = 0
    for root, dirs, files in os.walk(folder):
        if checkpoint:
            checkpoint()
        dirs.sort()
        for name in sorted(files):
            if checkpoint:
                checkpoint()
            listed += 1
            if listed > walk_limit or checked >= limit:
                return found or None
            if _ext(name) not in PHOTO_EXTS:
                continue
            checked += 1
            if reader(os.path.join(root, name)):
                found += 1
    return found


def _has_camera_exif(path: str) -> bool:
    try:
        import exifread
        with open(path, "rb") as handle:
            tags = exifread.process_file(handle, details=False, stop_tag="GPSLongitude")
        if any(key in tags for key in ("Image Make", "Image Model", "GPS GPSLatitude")):
            return True
    except Exception:                                  # noqa: BLE001 - try Pillow
        pass
    try:
        from PIL import Image
        with Image.open(path) as image:
            exif = image.getexif()
            return bool(exif.get(271) or exif.get(272) or exif.get(34853))
    except Exception:                                  # noqa: BLE001 - not readable
        return False


def decision_key(path: str, volume_identity) -> str:
    """Where a decision about *path* is remembered.

    Keyed by the drive's own identity and the path within it, so a choice
    made about ``E:\\Materials\\X`` still applies when the same drive comes
    back as ``F:``.
    """
    # A Windows path is read by Windows rules on any computer: on a Mac, os.path
    # took "E:\\Materials" for a file name in the current folder, and the drive
    # letter ended up inside the key it exists to leave out.
    rules = ntpath if re.match(r"^(?:[A-Za-z]:|\\\\)", path or "") else os.path
    plain = rules.normcase(rules.abspath(path))
    within = rules.splitdrive(plain)[1].replace("\\", "/").rstrip("/")
    if volume_identity:
        return f"{volume_identity[0]}:{volume_identity[1]}|{within}"
    return f"path|{plain}"


# ---------------------------------------------------------------------------
# A second opinion from the pictures, for folders the names cannot settle
# ---------------------------------------------------------------------------

#: What a frame from a course looks like, and what a family's pictures look
#: like, in the words the image model was trained against.
TEACHING_LOOKS = (
    "a screen recording of computer code",
    "presentation slides with text and bullet points",
    "a screenshot of a software tutorial",
    "a lecturer teaching in front of a whiteboard",
    "a technical diagram on a slide",
)
FAMILY_LOOKS = (
    "a family photo",
    "a home video of children playing",
    "people celebrating at a party",
    "a holiday landscape photograph",
    "a candid photo of friends",
)
IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "bmp"}

#: Points the pictures can add to a folder the names leave one point short.
#: They are only consulted there, so they can tip a near miss but never make a
#: course out of a folder its names say little about.
PICTURE_POINTS = 2


def sample_pictures(folder: str, *, videos: int = 3, images: int = 3,
                    walk_limit: int = 5000) -> list[tuple[str, int]]:
    """Which files a picture opinion would look at, as (path, size), in sorted order.

    Listing only -- nothing is opened -- so the same sample, and so the same
    stored opinion, can be found again without the image model loaded.
    """
    picked_videos: list[tuple[str, int]] = []
    picked_images: list[tuple[str, int]] = []
    listed = 0
    for root, dirs, files in os.walk(folder):
        dirs.sort()
        for name in sorted(files):
            listed += 1
            if listed > walk_limit:
                return picked_videos + picked_images
            ext = _ext(name)
            if ext in VIDEO_EXTS and len(picked_videos) < videos:
                wanted = picked_videos
            elif ext in IMAGE_EXTS and len(picked_images) < images:
                wanted = picked_images
            else:
                continue
            path = os.path.join(root, name)
            try:
                wanted.append((path, os.path.getsize(path)))
            except OSError:
                continue
        if len(picked_videos) >= videos and len(picked_images) >= images:
            break
    return picked_videos + picked_images


def sample_signature(sample: list[tuple[str, int]], folder: str) -> str:
    """Identifies an opinion by the files it judged: the same names at the same sizes."""
    import hashlib
    parts = [f"{os.path.relpath(path, folder).replace(os.sep, '/')}|{size}"
             for path, size in sample]
    return hashlib.sha256(";".join(parts).encode("utf-8")).hexdigest()


def _frame(path: str):
    """A still to judge: a video's middle frame, or the image itself."""
    from PIL import Image
    if _ext(path) in VIDEO_EXTS:
        import cv2
        capture = cv2.VideoCapture(path)
        try:
            count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            if count > 2:
                capture.set(cv2.CAP_PROP_POS_FRAMES, count // 2)
            ok, frame = capture.read()
        finally:
            capture.release()
        if not ok or frame is None:
            return None
        return Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    with Image.open(path) as image:
        return image.convert("RGB")


def picture_opinion(engine, sample: list[tuple[str, int]], frame=None) -> tuple[int, str]:
    """Points the sampled pictures add, and why, from the image model.

    Each still is compared with descriptions of teaching material and of family
    pictures, and counts toward a course when it is closer to the first. Two in
    three looking like screen recordings or slides, from at least two stills,
    adds :data:`PICTURE_POINTS`. Anything unreadable is simply not counted.
    """
    frame = frame or _frame
    stills = []
    for path, _size in sample:
        try:
            still = frame(path)
        except Exception:                               # noqa: BLE001 - skip it
            still = None
        if still is not None:
            stills.append(still)
    if len(stills) < 2:
        return 0, ""
    pictures = engine.embed_images(stills)
    words = engine.embed_texts(list(TEACHING_LOOKS) + list(FAMILY_LOOKS))
    similarity = pictures @ words.T
    teaching = similarity[:, :len(TEACHING_LOOKS)].max(axis=1)
    family = similarity[:, len(TEACHING_LOOKS):].max(axis=1)
    votes = int((teaching > family).sum())
    if votes * 3 >= len(stills) * 2:
        return PICTURE_POINTS, (f"{votes} of {len(stills)} sampled pictures look like "
                                f"screen recordings or slides")
    return 0, ""
