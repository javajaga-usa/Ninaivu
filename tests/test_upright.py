"""Working out which way up a photograph goes when nothing says.

A camera writes an orientation tag and Ninaivu honours it — that half has always
worked. This is the other half: scanned prints, photographs stripped of their
metadata by a messaging app, anything re-saved by an editor that dropped the
tag. There is nothing to read, so the picture has to be looked at.

Three rules run through all of this, and every test here exists to hold one of
them:

**EXIF wins.** A tag written by the camera that took the photograph beats
anything inferred from the pixels, always. Guessing over a known answer is how
you turn a library that was right into one that is wrong in a new way.

**A guess is not made unless it is a good one.** Rotating a photograph that was
already upright is worse than leaving a sideways one alone: the first breaks
something that worked, the second leaves a problem the owner already knows
about. So an unconfident detector returns nothing.

**Every decision says where it came from.** ``exif``, ``faces``, ``ai``,
``manual`` or ``none`` — so an admin looking at a photograph that came out
sideways can tell a bad guess from a bad tag, and correct the right thing.
"""

from pathlib import Path

import pytest
from PIL import Image

from ninaivu.media import upright


# --- the vocabulary --------------------------------------------------------

def test_the_rotations_are_the_only_four_that_exist():
    assert upright.ROTATIONS == (0, 90, 180, 270)


@pytest.mark.parametrize("tag,expected", [
    (1, 0), (2, 0), (3, 180), (4, 180), (5, 90), (6, 90), (7, 270), (8, 270),
])
def test_every_exif_tag_maps_to_a_quarter_turn(tag, expected):
    """Mirrored tags carry the same turn as their unmirrored twin — the flip is
    handled when the image is opened, not here."""
    assert upright.rotation_for_exif(tag) == expected


def test_a_missing_or_nonsense_tag_is_no_rotation():
    for value in (None, 0, 9, -1, "", "six"):
        assert upright.rotation_for_exif(value) == 0


# --- the guard rails -------------------------------------------------------

def test_a_photograph_with_a_real_exif_tag_is_never_guessed_at(monkeypatch):
    """The detector must not even run. A camera that said which way up beats
    any amount of cleverness about pixels."""
    called = []
    monkeypatch.setattr(upright, "detect_rotation",
                        lambda *a, **k: called.append(1) or (90, 0.99))

    verdict = upright.decide(Image.new("RGB", (100, 80)), exif_orientation=6)
    assert verdict.rotation == 90
    assert verdict.source == "exif"
    assert not called, "the detector ran despite a usable EXIF tag"


def test_an_unconfident_detector_changes_nothing(monkeypatch):
    monkeypatch.setattr(upright, "detect_rotation", lambda *a, **k: (90, 0.10))
    verdict = upright.decide(Image.new("RGB", (100, 80)), exif_orientation=None)
    assert verdict.rotation == 0
    assert verdict.source == "none"


def test_a_confident_detector_is_used_and_says_so(monkeypatch):
    monkeypatch.setattr(upright, "detect_rotation", lambda *a, **k: (270, 0.93))
    verdict = upright.decide(Image.new("RGB", (100, 80)), exif_orientation=None)
    assert verdict.rotation == 270
    assert verdict.source in {"faces", "ai"}


def test_detection_is_off_unless_asked_for(monkeypatch):
    monkeypatch.setattr(upright, "detect_rotation", lambda *a, **k: (90, 0.99))
    verdict = upright.decide(Image.new("RGB", (100, 80)), exif_orientation=None,
                             enabled=False)
    assert verdict.rotation == 0
    assert verdict.source == "none"


def test_a_detector_that_throws_leaves_the_photograph_alone(monkeypatch):
    """One unreadable file must never stop a scan, and must never rotate
    anything on the strength of an exception."""
    def boom(*a, **k):
        raise RuntimeError("no model")
    monkeypatch.setattr(upright, "detect_rotation", boom)
    verdict = upright.decide(Image.new("RGB", (100, 80)), exif_orientation=None)
    assert verdict.rotation == 0
    assert verdict.source == "none"


# --- faces -----------------------------------------------------------------

cv2 = pytest.importorskip("cv2")


@pytest.fixture(scope="module")
def portrait():
    """A real photograph of a person, framed the way photographs are.

    Drawn in code it would not work: a Haar cascade keys off photographic
    texture, so a cartoon face is simply not detected and a test built on one
    would pass or fail for reasons with nothing to do with Ninaivu. See
    ``tests/data/README.txt`` for why this is the framed version rather than
    the head crop.
    """
    return Image.open(Path(__file__).parent / "data" / "portrait.jpg")


@pytest.fixture(scope="module")
def head_crop():
    """A tight, centred head — the hardest case, and the one Ninaivu refuses."""
    return Image.open(Path(__file__).parent / "data" / "face.jpg")


def test_the_fixture_face_is_findable_upright(portrait):
    """Guards the two tests below: if this fails they prove nothing."""
    assert upright.count_faces(portrait) > 0, (
        "the drawn face is not detectable, so the rotation tests are vacuous")


@pytest.mark.parametrize("turn", [90, 180, 270])
def test_a_rotated_portrait_is_turned_back(portrait, turn):
    """Pillow's rotate is anticlockwise; every number in `upright` is a
    clockwise turn, so a photograph lying N degrees anticlockwise is put right
    by a clockwise turn of N."""
    sideways = portrait.rotate(turn, expand=True)
    rotation, confidence = upright.detect_by_faces(sideways)
    assert rotation == turn, f"{rotation} at confidence {confidence}"
    assert confidence > 0.5


def test_the_whole_round_trip_puts_the_face_back(portrait):
    """Detect and apply, and the picture is upright — the only claim that
    actually matters to somebody looking at their photographs."""
    for turn in (90, 180, 270):
        sideways = portrait.rotate(turn, expand=True)
        rotation, _ = upright.detect_by_faces(sideways)
        restored = upright.apply(sideways, rotation)
        assert restored.size == portrait.size, turn
        # And asked again, it has no further correction to offer.
        assert upright.detect_by_faces(restored)[0] == 0, turn


def test_a_tight_centred_head_is_refused_rather_than_guessed_at(head_crop):
    """The honest limit, pinned down.

    A head crop with nothing else in the frame is the one case a Haar cascade
    genuinely cannot resolve: it finds the same head upside down at almost the
    same size, and there is no framing to break the tie. Refusing is right, and
    somebody reading this code later should know it is deliberate rather than
    an oversight.
    """
    for turn in (0, 90, 180, 270):
        rotation, confidence = upright.detect_by_faces(
            head_crop.rotate(turn, expand=True))
        assert (rotation, confidence) == (0, 0.0), turn


def test_the_axis_is_evidence_and_the_flip_is_not(portrait, monkeypatch):
    """Faces at 0 and 90 is a classifier being agreeable, not a verdict."""
    monkeypatch.setattr(upright, "find_faces",
                        lambda img, **k: [(10, 10, 50, 50)])
    rotation, confidence = upright.detect_by_faces(portrait)
    assert confidence == 0.0 and rotation == 0


def test_an_upright_portrait_is_left_alone(portrait):
    rotation, _ = upright.detect_by_faces(portrait)
    assert rotation == 0


def test_a_picture_with_no_faces_yields_no_opinion():
    """A landscape is not evidence of anything, and must not be rotated."""
    from PIL import ImageDraw
    img = Image.new("RGB", (600, 400), (150, 180, 220))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 240, 600, 400], fill=(90, 130, 70))
    rotation, confidence = upright.detect_by_faces(img)
    assert confidence == 0.0
    assert rotation == 0


def test_a_detection_that_is_not_skin_coloured_is_not_a_face(monkeypatch):
    """The check that makes this safe to run unattended.

    A Haar cascade fires on colour charts, brickwork and car grilles at a size
    and certainty indistinguishable from a real face further away. Measured for
    skin they are not close, and this holds that gate shut.
    """
    monkeypatch.setattr(upright, "find_faces",
                        lambda img, **k: [(10, 10, 60, 60)])
    blue = Image.new("RGB", (200, 200), (40, 90, 200))
    assert upright.face_boxes(blue) == []
    assert upright.detect_by_faces(blue) == (0, 0.0)


def test_a_centred_face_either_way_up_is_not_a_verdict(monkeypatch):
    """The 180 tie is broken on how high the faces sit. A face dead centre
    sits at the same height upside down, so there is nothing to choose — and
    picking one anyway would turn somebody's photograph over on a coin toss."""
    monkeypatch.setattr(upright, "find_faces",
                        lambda img, **k: [(20, 40, 20, 20)])   # centred in 100px

    # Only the 0/180 axis finds anything, and both at the same height.
    monkeypatch.setattr(upright, "find_faces",
                        lambda img, **k: ([(20, 40, 20, 20)]
                                          if img.size[1] >= img.size[0] else []))
    rotation, confidence = upright.detect_by_faces(Image.new("RGB", (100, 100)))
    assert confidence == 0.0
    assert rotation == 0


# --- applying it -----------------------------------------------------------

def test_applying_a_quarter_turn_swaps_the_sides():
    img = Image.new("RGB", (120, 80))
    assert upright.apply(img, 90).size == (80, 120)
    assert upright.apply(img, 270).size == (80, 120)
    assert upright.apply(img, 180).size == (120, 80)
    assert upright.apply(img, 0).size == (120, 80)


def test_applying_zero_returns_the_same_image():
    img = Image.new("RGB", (120, 80))
    assert upright.apply(img, 0) is img


def test_face_detection_on_a_worker_thread_does_not_stop_the_process_exiting():
    """OpenCV on Windows (Concurrency Runtime) left python.exe unable to exit when
    the first detectMultiScale ran off the main thread — the scanner's pool —
    so the test suite never returned after its summary. Priming on the main
    thread first is the fix; this runs the scanner's pattern in a child process
    and requires it to end."""
    import subprocess
    import sys
    pytest.importorskip("cv2")
    script = (
        "import threading, numpy as np\n"
        "from ninaivu.media import upright\n"
        "upright.prime_on_main_thread()\n"
        "from PIL import Image\n"
        "t = threading.Thread(target=lambda: upright.find_faces(Image.new('RGB', (320, 240))))\n"
        "t.start(); t.join()\n"
        "print('finished')\n")
    import tempfile
    from pathlib import Path
    root = str(Path(__file__).resolve().parents[1])
    with tempfile.TemporaryFile() as out:        # a file, not a pipe: the hang needed one
        try:
            result = subprocess.run([sys.executable, "-c", script], cwd=root, stdout=out,
                                    stderr=subprocess.STDOUT, timeout=60)
        except subprocess.TimeoutExpired:
            pytest.fail("the process did not exit after OpenCV ran on a worker thread")
        out.seek(0)
        assert result.returncode == 0 and b"finished" in out.read()
