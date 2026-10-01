"""The retouching tools' idea of a face: what is skin, what is hair, what is a bindi.

The pictures here are drawn from a recipe (see ``synthetic_faces``), so every
pixel has a known answer, in skin from fair to very dark. What is being tested is
what a family photograph asks of these tools: every face in the frame, each
judged against its own skin; the eyes, brows and lips left alone; the marks worn
on purpose — a bindi, sindoor, sacred ash — never touched; hair found when it can
be told from what is behind it, and declined when it cannot; and suggestions that
follow from what is measured and never from how dark somebody's skin is.
"""

import io

import numpy as np
import pytest
from PIL import Image

from ninaivu.media import portrait
from synthetic_faces import draw

pytestmark = pytest.mark.skipif(not portrait.available(), reason="needs OpenCV")


def analysed(faces, **kwargs):
    picture = draw(faces, **kwargs)
    return picture, portrait.analyse(picture.rgb, picture.located)


def share(plane, truth, at=0.5):
    """How much of the true region the map picked up."""
    return float((plane[truth] > at).mean())


THREE = [dict(x=130, y=170, d=80, skin="fair"), dict(x=330, y=170, d=80, skin="brown"),
         dict(x=540, y=170, d=80, skin="deep")]


# ---------------------------------------------------------------------------
# Every face, only skin
# ---------------------------------------------------------------------------

def test_every_face_is_found_and_judged_against_its_own_skin():
    """A fair face and a very dark one in the same frame need different treatment."""
    picture, found = analysed(THREE, size=(690, 420))
    assert [face["id"] for face in found.faces] == [1, 2, 3]
    lightness = [face["skin"]["lightness"] for face in found.faces]
    assert lightness[0] > lightness[1] > lightness[2] + 10, lightness
    for truth in picture.truth:
        assert share(found.planes["skin"], truth["skin"]) > 0.75


def test_eyes_lips_and_brows_are_not_skin():
    """Smoothing the eyes is the mistake the old selection made."""
    picture, found = analysed(THREE, size=(690, 420))
    for truth in picture.truth:
        for part in ("eyes", "mouth"):
            assert found.planes["skin"][truth[part]].mean() < 0.1, part
        assert found.planes["skin"][truth["brows"]].mean() < 0.3


def test_pale_grey_brows_are_not_skin_either():
    """An elder's brows are lighter than the skin, not darker, and must be left alone too."""
    picture, found = analysed([dict(x=200, y=170, d=80, skin="brown", hair="grey")],
                              size=(400, 420), background=(40, 44, 48))
    assert found.planes["skin"][picture.truth[0]["brows"]].mean() < 0.4


def test_the_background_is_never_selected_as_skin():
    picture, found = analysed(THREE, size=(690, 420), background=(200, 160, 130))   # a skin-coloured wall
    outside = np.ones(found.planes["skin"].shape, bool)
    for truth in picture.truth:
        outside &= ~(truth["skin"] | truth["eyes"] | truth["brows"] | truth["mouth"] | truth["hair"])
    outside[230:] = False                                       # below the heads, the necks are skin too
    assert found.planes["skin"][outside].mean() < 0.02


def test_dark_circles_are_skin_so_they_can_be_lifted():
    picture, found = analysed([dict(x=200, y=170, d=80, skin="wheatish", circles=0.3)], size=(400, 420))
    assert found.faces[0]["suggest"].get("underEye", 0) > 20
    assert found.planes["under"].max() > 0.8


# ---------------------------------------------------------------------------
# Where light may reach
# ---------------------------------------------------------------------------

def test_face_light_reaches_the_whole_face_the_features_and_the_neck():
    """Or there would be a seam where the lift stops: at the eyes, at the lips, under the chin."""
    picture, found = analysed([dict(x=200, y=170, d=80, skin="brown", hair="black")], size=(400, 420),
                              background=(170, 175, 180))
    truth = picture.truth[0]
    zone = found.planes["zone"]
    whole_face = truth["skin"] | truth["eyes"] | truth["mouth"] | truth["brows"]
    assert share(zone, whole_face, 0.8) > 0.9
    neck = np.zeros(zone.shape, bool)
    neck[170 + int(1.75 * 80): 170 + int(2.3 * 80), 200 - 25: 200 + 25] = True      # just under the chin
    assert zone[neck].mean() > 0.5


def test_face_light_stops_where_the_person_does():
    """An ellipse round the face would lift the wall beside the jaw, and leave a halo."""
    picture, found = analysed([dict(x=200, y=170, d=80, skin="brown", hair="black")], size=(400, 420),
                              background=(170, 175, 180))
    zone = found.planes["zone"]
    yy, xx = np.mgrid[0:420, 0:400]
    u, v = (xx - 200) / 80.0, (yy - 170) / 80.0
    # A ring of wall a little beyond the side of the jaw and the chin, clear of hair and neck.
    beside = (np.abs(u) > 1.05) & (np.abs(u) < 1.6) & (v > 1.3) & (v < 2.4)
    beside &= ~picture.truth[0]["hair"]
    assert beside.sum() > 500
    assert zone[beside].mean() < 0.08
    # and hair, which is lit by nobody's face light, only a little
    assert zone[picture.truth[0]["hair"]].mean() < 0.3


def test_colour_goes_on_the_neck_and_ears_as_well_as_the_face():
    """A cast turned back on the face alone would leave a line at the jaw where the neck is another colour."""
    picture, found = analysed([dict(x=200, y=170, d=80, skin="brown", hair="black")], size=(400, 420),
                              background=(170, 175, 180))
    truth = picture.truth[0]
    body = found.planes["body"]
    neck = np.zeros(body.shape, bool)
    neck[170 + int(1.75 * 80): 170 + int(2.4 * 80), 200 - 25: 200 + 25] = True
    assert body[neck].mean() > 0.6, "the neck is part of the person's skin"
    assert share(body, truth["skin"]) > 0.75
    assert body[truth["eyes"]].mean() < 0.15 and body[truth["mouth"]].mean() < 0.15, "and the eyes and lips are not"
    yy, xx = np.mgrid[0:420, 0:400]
    u, v = (xx - 200) / 80.0, (yy - 170) / 80.0
    wall = (np.abs(u) > 1.05) & (np.abs(u) < 1.6) & (v > 1.3) & (v < 2.4) & ~truth["hair"]
    assert body[wall].mean() < 0.08, "the wall is not"


def test_a_beard_is_lit_with_the_face_it_is_on():
    picture, found = analysed([dict(x=200, y=170, d=80, skin="fair", beard=True)], size=(400, 420))
    assert share(found.planes["zone"], picture.truth[0]["beard"], 0.8) > 0.9


# ---------------------------------------------------------------------------
# What is worn on purpose
# ---------------------------------------------------------------------------

MARKS = [
    ("red bindi", dict(skin="fair", bindi="red")),
    ("black bindi", dict(skin="wheatish", bindi="black")),
    ("sindoor", dict(skin="brown", sindoor=True)),
    ("vibhuti", dict(skin="dark", vibhuti=True)),
    ("bindi and sindoor", dict(skin="brown", bindi="red", sindoor=True)),
]


@pytest.mark.parametrize("name, recipe", MARKS, ids=[m[0] for m in MARKS])
def test_a_mark_worn_on_purpose_is_protected(name, recipe):
    picture, found = analysed([dict(x=200, y=170, d=80, **recipe)], size=(400, 420))
    truth = picture.truth[0]
    assert found.faces[0]["marks"] is True
    assert share(found.planes["protect"], truth["marks"]) > 0.6, name
    # What a smoothing tool sees of that spot is skin weight times what is left
    # after protection, and it has to be next to nothing.
    left = found.planes["skin"] * (1.0 - found.planes["protect"])
    assert left[truth["marks"]].mean() < 0.3


@pytest.mark.parametrize("recipe", [
    dict(skin="fair", cast=(1.1, 0.95, 0.93)),                # flushed, ruddy skin
    dict(skin="brown", shine=0.9),                            # a shiny forehead
    dict(skin="deep"),
    dict(skin="wheatish", hair="brown"),
], ids=["ruddy", "shiny", "very dark", "brown hair"])
def test_a_plain_forehead_is_not_mistaken_for_a_mark(recipe):
    _, found = analysed([dict(x=200, y=170, d=80, **recipe)], size=(400, 420))
    assert found.faces[0]["marks"] is False
    assert found.planes["protect"].max() < 0.5


def test_protection_does_not_spread_across_the_face():
    picture, found = analysed([dict(x=200, y=170, d=80, skin="fair", bindi="red")], size=(400, 420))
    truth = picture.truth[0]
    assert found.planes["protect"][truth["skin"] & (np.abs(np.arange(420)[:, None] - 138) > 16)].mean() < 0.05


# ---------------------------------------------------------------------------
# Hair and beard
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("hair, wall", [("black", (170, 175, 180)), ("brown", (170, 175, 180)),
                                        ("grey", (40, 44, 48)), ("white", (40, 44, 48))])
def test_hair_is_found_when_it_can_be_told_from_the_wall(hair, wall):
    picture, found = analysed([dict(x=200, y=170, d=80, skin="wheatish", hair=hair)],
                              size=(400, 420), background=wall)
    assert found.faces[0]["hair"]["found"] is True
    assert share(found.planes["hair"], picture.truth[0]["hair"]) > 0.85
    wall_only = ~(picture.truth[0]["hair"] | picture.truth[0]["skin"] | picture.truth[0]["eyes"]
                  | picture.truth[0]["brows"] | picture.truth[0]["mouth"])
    wall_only[250:] = False
    assert found.planes["hair"][wall_only].mean() < 0.03


def test_hair_the_colour_of_the_wall_is_declined_not_guessed():
    """Silence, not a guess: a head of black hair against a black wall has no edge to find."""
    _, found = analysed([dict(x=200, y=170, d=80, skin="brown", hair="black")], size=(400, 420),
                        background=(28, 24, 22))
    assert found.faces[0]["hair"]["found"] is False
    assert found.planes["hair"].max() == 0.0


def test_hair_the_colour_of_the_wall_is_found_when_a_foreground_says_where_the_wall_is():
    """The one thing colour cannot do here, a segmentation model's foreground can:
    say where the wall is. The colour still has to agree, so the wall is excluded
    and the hair is what is left."""
    import cv2
    picture = draw([dict(x=200, y=170, d=80, skin="brown", hair="black")], size=(400, 420),
                   background=(28, 24, 22))
    truth = picture.truth[0]
    person = (truth["hair"] | truth["skin"] | truth["eyes"] | truth["brows"] | truth["mouth"]).astype(np.uint8)
    person = cv2.dilate(person, np.ones((5, 5), np.uint8)).astype(np.float32)
    asked = []

    def foreground():
        asked.append(True)
        return person

    found = portrait.analyse(picture.rgb, picture.located, foreground=foreground)
    assert asked == [True], "the foreground was asked for more than once, or not at all"
    assert found.faces[0]["hair"]["found"] is True
    assert share(found.planes["hair"], truth["hair"]) > 0.8
    wall_only = ~(truth["hair"] | truth["skin"] | truth["eyes"] | truth["brows"] | truth["mouth"])
    wall_only[250:] = False
    assert found.planes["hair"][wall_only].mean() < 0.03

    # Hair that colour alone can find never asks for it.
    asked.clear()
    found = portrait.analyse(*(lambda p: (p.rgb, p.located))(draw(
        [dict(x=200, y=170, d=80, skin="wheatish", hair="black")], size=(400, 420), background=(170, 175, 180))),
        foreground=foreground)
    assert found.faces[0]["hair"]["found"] is True and asked == []

    # A foreground that fails is a help that was not there: declined, as before.
    def broken():
        raise RuntimeError("no model")
    found = portrait.analyse(picture.rgb, picture.located, foreground=broken)
    assert found.faces[0]["hair"]["found"] is False


def test_a_bald_head_has_no_hair_to_find():
    _, found = analysed([dict(x=200, y=170, d=80, skin="brown", bald=True)], size=(400, 420))
    assert found.faces[0]["hair"]["found"] is False
    assert found.planes["hair"].max() == 0.0


def test_each_persons_hair_belongs_to_that_person():
    faces = [dict(x=200, y=170, d=80, skin="brown", hair="black"),
             dict(x=450, y=170, d=80, skin="wheatish", hair="brown")]
    picture, found = analysed(faces, size=(650, 420), background=(170, 175, 180))
    for index, truth in enumerate(picture.truth, start=1):
        owners = found.hair_id[truth["hair"] & (found.planes["hair"] > 0.5)]
        assert (owners == index).mean() > 0.97
        skin_owners = found.skin_id[truth["skin"] & (found.planes["skin"] > 0.5)]
        assert (skin_owners == index).mean() > 0.97


@pytest.mark.parametrize("skin", ["fair", "deep"])
def test_a_beard_is_found_on_fair_skin_and_on_very_dark_skin(skin):
    picture, found = analysed([dict(x=200, y=170, d=80, skin=skin, beard=True)], size=(400, 420))
    assert found.faces[0]["beard"] > 0.2
    assert share(found.planes["beard"], picture.truth[0]["beard"], 0.3) > 0.8
    assert found.planes["skin"][picture.truth[0]["beard"]].mean() < 0.15     # and it is not skin


def test_long_hair_beside_the_jaw_is_not_a_beard():
    _, found = analysed([dict(x=200, y=170, d=80, skin="brown", hair="black")], size=(400, 420),
                        background=(170, 175, 180))
    assert found.faces[0]["beard"] == 0.0


# ---------------------------------------------------------------------------
# What each face needs
# ---------------------------------------------------------------------------

def suggestions(recipe, **kwargs):
    kwargs.setdefault("size", (400, 420))
    _, found = analysed([dict(x=200, y=170, d=80, **recipe)], **kwargs)
    return found.faces[0]["suggest"]


def test_a_face_in_shadow_is_offered_light_and_a_dark_face_in_good_light_is_not():
    """The difference between a correction and a fairness filter.

    Both faces are darker than the bright wall behind them. Only the one whose own
    eye whites are dim is under-exposed.
    """
    wall = (205, 205, 210)
    shadowed = suggestions(dict(skin="wheatish", dark=0.38), background=wall)
    assert shadowed.get("faceLight", 0) > 40
    for complexion in ("brown", "dark", "deep"):
        assert "faceLight" not in suggestions(dict(skin=complexion), background=wall), complexion


def test_no_suggestion_ever_asks_for_more_than_a_modest_lift():
    tops = []
    for dark in (0.2, 0.3, 0.4, 0.5):
        tops.append(suggestions(dict(skin="brown", dark=dark), background=(205, 205, 210)).get("faceLight", 0))
    assert max(tops) <= 90


def test_side_light_is_offered_balance():
    assert suggestions(dict(skin="wheatish", side=0.30)).get("balance", 0) > 10
    assert "balance" not in suggestions(dict(skin="wheatish"))


def test_a_shiny_forehead_is_offered_shine_control_and_a_matt_one_is_not():
    assert suggestions(dict(skin="brown", shine=1.5)).get("shine", 0) > 10
    assert "shine" not in suggestions(dict(skin="brown"))


def test_a_colour_cast_is_offered_a_tone_correction_in_the_right_direction():
    magenta = suggestions(dict(skin="wheatish", cast=(1.05, 0.80, 1.0)))
    assert magenta.get("tone", 0) > 20
    green = suggestions(dict(skin="wheatish", cast=(0.85, 1.05, 0.75)))
    assert green.get("tone", 0) < -20 or "tone" not in green         # never the wrong way
    assert "tone" not in suggestions(dict(skin="wheatish"))


@pytest.mark.parametrize("hair", ["grey", "white"])
def test_grey_hair_is_offered_grey_cover_and_black_hair_is_not(hair):
    grey = suggestions(dict(skin="brown", hair=hair), background=(40, 44, 48))
    assert grey.get("greyCover", 0) > 40
    assert "greyCover" not in suggestions(dict(skin="brown", hair="black"), background=(170, 175, 180))


def test_nothing_is_offered_that_lightens_skin():
    """Whatever the face, the only suggestions are the ones the tools list, and none
    of them is a lighter complexion."""
    allowed = {"faceLight", "balance", "shine", "even", "underEye", "tone", "richness",
               "hairDetail", "greyCover"}
    for complexion in ("fair", "wheatish", "brown", "dark", "deep"):
        assert set(suggestions(dict(skin=complexion, shine=0.9, circles=0.2, side=0.3))) <= allowed


def test_measurements_are_reported_with_the_suggestions():
    _, found = analysed([dict(x=200, y=170, d=80, skin="brown")], size=(400, 420))
    face = found.faces[0]
    assert set(face["measure"]) >= {"lightness", "hueOffset", "asymmetry", "shine", "blotch", "underEye"}
    assert {"x", "y", "d", "cos", "sin"} <= set(face["frame"])


# ---------------------------------------------------------------------------
# The maps that go to the browser
# ---------------------------------------------------------------------------

def test_the_maps_are_packed_as_four_opaque_pictures_that_survive_a_round_trip():
    _, found = analysed(THREE, size=(690, 420))
    packed = portrait.pack_planes(found)
    assert set(packed) == {"a", "b", "c", "ids"}
    for name, array in packed.items():
        assert array.dtype == np.uint8 and array.shape == (420, 690, 3), name
        buffer = io.BytesIO()
        Image.fromarray(array, mode="RGB").save(buffer, format="PNG")
        assert (np.asarray(Image.open(io.BytesIO(buffer.getvalue()))) == array).all(), name
    assert packed["a"][..., 0].max() > 200                         # skin
    assert set(np.unique(packed["ids"][..., 0])) <= {0, 1, 2, 3}


def test_ids_say_which_face_owns_each_pixel():
    picture, found = analysed(THREE, size=(690, 420))
    for index, spec in enumerate(THREE, start=1):
        cheek = found.skin_id[int(spec["y"] + 0.6 * spec["d"]), int(spec["x"] - 0.45 * spec["d"])]
        assert cheek == index


# ---------------------------------------------------------------------------
# Awkward pictures
# ---------------------------------------------------------------------------

def test_the_same_picture_gives_the_same_maps_every_time():
    """Hair is refined by a method that starts from random clusters; looking twice must not give two answers."""
    recipe = [dict(x=200, y=170, d=80, skin="brown", hair="black")]
    _, first = analysed(recipe, size=(400, 420), background=(170, 175, 180))
    _, second = analysed(recipe, size=(400, 420), background=(170, 175, 180))
    for name in first.planes:
        assert np.array_equal(first.planes[name], second.planes[name]), name
    assert first.faces == second.faces


def test_no_faces_is_an_empty_answer():
    picture = draw([], size=(300, 200))
    found = portrait.analyse(picture.rgb, [])
    assert found.faces == [] and found.planes["skin"].max() == 0.0


def test_a_face_running_off_the_edge_of_the_picture_does_not_break_the_rest():
    faces = [dict(x=40, y=60, d=70, skin="brown"), dict(x=300, y=170, d=80, skin="wheatish")]
    picture, found = analysed(faces, size=(400, 420))
    assert found.faces[1].get("usable") is True


def test_a_head_tipped_right_over_is_left_alone_rather_than_guessed_at():
    picture = draw([dict(x=200, y=200, d=80, skin="brown", tilt=1.3)], size=(400, 420))
    found = portrait.analyse(picture.rgb, picture.located)
    assert found.faces == [] or found.faces[0].get("usable") is not True


def test_one_face_that_cannot_be_worked_out_does_not_take_the_others_with_it(monkeypatch):
    real = portrait._light_zone
    calls = []

    def fails_for_the_first(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("something about this face")
        return real(*args, **kwargs)

    monkeypatch.setattr(portrait, "_light_zone", fails_for_the_first)
    picture, found = analysed([dict(x=130, y=170, d=80, skin="fair"), dict(x=330, y=170, d=80, skin="brown")],
                              size=(480, 420))
    assert found.faces[0]["usable"] is False and found.faces[1]["usable"] is True
    assert share(found.planes["skin"], picture.truth[1]["skin"]) > 0.75
    assert found.planes["skin"][picture.truth[0]["skin"]].max() == 0.0


def test_a_modest_tilt_is_followed():
    picture, found = analysed([dict(x=200, y=200, d=80, skin="brown", tilt=0.35)], size=(400, 420))
    assert share(found.planes["skin"], picture.truth[0]["skin"]) > 0.7
    assert found.planes["skin"][picture.truth[0]["eyes"]].mean() < 0.1


def test_more_faces_than_the_limit_keeps_the_largest():
    faces = [dict(x=60 + 70 * i, y=80, d=12 + 4 * i, skin="brown") for i in range(6)]
    picture = draw(faces, size=(500, 160))
    found = portrait.analyse(picture.rgb, picture.located, limit=3)
    assert len(found.faces) == 3
    sizes = [face["frame"]["d"] for face in found.faces]
    assert min(sizes) > 0.028                                     # the three largest, not the three first


def test_a_close_up_is_looked_at_smaller_and_the_maps_say_how_big_they_are():
    """A face four hundred pixels across the eyes has nothing to tell that a hundred and sixty does not."""
    picture = draw([dict(x=600, y=500, d=400, skin="brown", hair="black", bindi="red")], size=(1200, 1000),
                   background=(170, 175, 180))
    found = portrait.analyse(picture.rgb, picture.located)
    width, height = found.size
    assert width < 1200 and height < 1000 and abs(width / height - 1.2) < 0.01
    assert found.planes["skin"].shape == (height, width) and found.skin_id.shape == (height, width)
    # Resampled back up, the map still lies on the face.
    import cv2

    skin = cv2.resize(found.planes["skin"], (1200, 1000), interpolation=cv2.INTER_LINEAR)
    assert share(skin, picture.truth[0]["skin"]) > 0.75
    assert found.faces[0]["marks"] is True
    frame = found.faces[0]["frame"]
    assert abs(frame["x"] - 0.5) < 0.01 and abs(frame["d"] - 400 / 1200) < 0.01, "frames are fractions of the picture, so they do not care"


def test_a_group_keeps_its_smallest_face_findable_while_the_big_one_is_reduced():
    faces = [dict(x=200, y=300, d=300, skin="fair"), dict(x=900, y=180, d=40, skin="brown")]
    picture = draw(faces, size=(1200, 700), background=(170, 175, 180))
    found = portrait.analyse(picture.rgb, picture.located)
    assert found.size[0] >= 1200 * portrait.MIN_FACE_D / 40 - 2 or found.size[0] == 1200
    assert all(face.get("usable") for face in found.faces)


def test_a_large_group_photograph_is_analysed_in_reasonable_time():
    import time

    faces = [dict(x=160 + 280 * (i % 8), y=240 + 380 * (i // 8), d=70, skin=["fair", "wheatish", "brown", "dark"][i % 4],
                  hair="black") for i in range(16)]
    picture = draw(faces, size=(2400, 1000))
    started = time.time()
    found = portrait.analyse(picture.rgb, picture.located)
    assert time.time() - started < 30
    assert sum(1 for face in found.faces if face.get("usable")) == 16


# ---------------------------------------------------------------------------
# With a face-parsing network, when there is one
# ---------------------------------------------------------------------------

class Oracle:
    """A stand-in for a face-parsing network that knows the answer, from the drawing's own truth."""

    def __init__(self, picture, skin=True, hair=True, features=True):
        self.picture, self.use = picture, (skin, hair, features)
        self.calls = 0

    def probabilities(self, rgb, origin=None):
        self.calls += 1
        (y0, x0), (h, w) = origin, rgb.shape[:2]
        out = np.zeros((19, h, w), np.float32)
        out[0] = 1.0

        def mark(label, plane):
            region = plane[y0:y0 + h, x0:x0 + w]
            out[label][region] = 1.0
            out[0][region] = 0.0

        for truth in self.picture.truth:
            if self.use[0]:
                mark(1, truth["skin"])
            if self.use[2]:
                mark(2, truth["brows"]); mark(4, truth["eyes"]); mark(11, truth["mouth"])
            if self.use[1]:
                mark(17, truth["hair"])
        return out


def with_parser(faces, parser_of=Oracle, **kwargs):
    picture = draw(faces, **kwargs)
    parser = parser_of(picture)
    return picture, portrait.analyse(picture.rgb, picture.located, parser=parser), parser


def test_a_parser_finds_the_hair_the_built_in_method_declines():
    """Black hair against a black wall has no edge for colour to find; a network has been shown what a head is."""
    recipe = [dict(x=200, y=170, d=80, skin="brown", hair="black")]
    black_wall = dict(size=(400, 420), background=(28, 24, 22))
    _, plain = analysed(recipe, **black_wall)
    assert plain.faces[0]["hair"]["found"] is False
    picture, found, parser = with_parser(recipe, **black_wall)
    assert parser.calls == 1
    assert found.faces[0]["engine"] == "parser" and portrait.engine_of(found) == "parser"
    assert found.faces[0]["hair"]["found"] is True
    assert share(found.planes["hair"], picture.truth[0]["hair"]) > 0.85
    wall = ~(picture.truth[0]["hair"] | picture.truth[0]["skin"] | picture.truth[0]["eyes"] | picture.truth[0]["brows"] | picture.truth[0]["mouth"])
    wall[250:] = False
    assert found.planes["hair"][wall].mean() < 0.05


def test_with_a_parser_skin_is_still_only_skin_and_marks_are_still_protected():
    picture, found, _ = with_parser([dict(x=200, y=170, d=80, skin="dark", bindi="red")], size=(400, 420),
                                    background=(170, 175, 180))
    truth = picture.truth[0]
    assert found.faces[0]["engine"] == "parser"
    assert share(found.planes["skin"], truth["skin"]) > 0.85
    assert found.planes["skin"][truth["eyes"]].mean() < 0.1 and found.planes["skin"][truth["mouth"]].mean() < 0.1
    assert found.faces[0]["marks"] is True
    assert share(found.planes["protect"], truth["marks"]) > 0.6       # the network calls a bindi skin; the built-in method knows it is not


def test_a_parser_that_is_wrong_about_a_face_is_not_believed():
    class Nothing(Oracle):
        def probabilities(self, rgb, origin=None):
            out = np.zeros((19, *rgb.shape[:2]), np.float32)
            out[0] = 1.0
            return out

    class Everything(Oracle):
        def probabilities(self, rgb, origin=None):
            out = np.zeros((19, *rgb.shape[:2]), np.float32)
            out[1] = 1.0
            return out

    recipe = [dict(x=200, y=170, d=80, skin="brown", hair="black")]
    _, plain = analysed(recipe, size=(400, 420), background=(170, 175, 180))
    for wrong in (Nothing, Everything):
        _, found, _ = with_parser(recipe, parser_of=wrong, size=(400, 420), background=(170, 175, 180))
        assert found.faces[0]["engine"] == "builtin", wrong.__name__
        assert np.allclose(found.planes["skin"], plain.planes["skin"])


def test_a_parser_that_fails_or_answers_in_the_wrong_shape_is_ignored():
    class Raises:
        def probabilities(self, rgb, origin=None):
            raise RuntimeError("the network fell over")

    class WrongShape:
        def probabilities(self, rgb, origin=None):
            return np.zeros((19, 4, 4), np.float32)

    class Silent:
        def probabilities(self, rgb, origin=None):
            return None

    recipe = [dict(x=200, y=170, d=80, skin="brown", hair="black")]
    _, plain = analysed(recipe, size=(400, 420), background=(170, 175, 180))
    for broken in (Raises, WrongShape, Silent):
        picture = draw(recipe, size=(400, 420), background=(170, 175, 180))
        found = portrait.analyse(picture.rgb, picture.located, parser=broken())
        assert found.faces[0]["engine"] == "builtin", broken.__name__
        assert np.allclose(found.planes["hair"], plain.planes["hair"])


def test_without_a_parser_nothing_about_a_face_says_it_used_one():
    _, found = analysed([dict(x=200, y=170, d=80, skin="brown")], size=(400, 420))
    assert found.faces[0]["engine"] == "builtin" and portrait.engine_of(found) == "builtin"


def test_the_parser_is_only_there_when_its_model_is(tmp_path):
    from ninaivu.media import face_parser, model_catalog

    face_parser.forget()
    assert face_parser.get() is None                        # the models folder is empty for every test
    target = model_catalog.models_root() / "faceparse" / "face_parsing.onnx"
    assert face_parser.model_path() == target
    target.parent.mkdir(parents=True)
    target.write_bytes(b"this is not a network")
    face_parser.forget()
    assert face_parser.get() is None, "a file that does not load is not a parser"
    face_parser.forget()


def test_a_network_is_shown_a_normalised_picture_and_its_answer_is_read_as_probabilities(monkeypatch):
    from ninaivu.media import face_parser

    seen = {}

    class Net:
        def setInput(self, blob):
            seen["blob"] = blob

        def forward(self):
            logits = np.zeros((1, 19, 512, 512), np.float32)
            logits[0, 1] = 6.0                               # skin everywhere...
            logits[0, 17, :200] = 9.0                        # ...but hair across the top
            return logits

    monkeypatch.setattr(face_parser.cv2.dnn, "readNetFromONNX", lambda path: Net())
    parser = face_parser.FaceParser("somewhere.onnx")
    answer = parser.probabilities(np.full((300, 240, 3), 128, np.uint8))
    assert answer.shape == (19, 300, 240) and answer.dtype == np.float32
    assert np.allclose(answer.sum(axis=0), 1.0, atol=1e-4)
    assert answer[17, 10].mean() > 0.9 and answer[1, 290].mean() > 0.9
    assert seen["blob"].shape == (1, 3, 512, 512)
    assert abs(float(seen["blob"].mean())) < 0.3            # grey, taken off the mean the network was trained with
    assert parser.probabilities(np.zeros((8, 8, 3), np.uint8)) is None

    class Odd(Net):
        def forward(self):
            return np.zeros((1, 7, 512, 512), np.float32)

    monkeypatch.setattr(face_parser.cv2.dnn, "readNetFromONNX", lambda path: Odd())
    assert face_parser.FaceParser("somewhere.onnx").probabilities(np.zeros((64, 64, 3), np.uint8)) is None


# ---------------------------------------------------------------------------
# A real photograph, with the real face detector, when it is on this machine
# ---------------------------------------------------------------------------

def needs_the_face_model(test):
    """The one place drawn faces are not enough: a detector has to find a real one. Both marks, as in test_local_ai."""
    from ninaivu.media import model_catalog

    return pytest.mark.real_models(pytest.mark.skipif(
        not model_catalog.installed("faces"), reason="the face model has not been downloaded")(test))


@needs_the_face_model
def test_a_real_portrait_is_found_and_selected_sensibly(monkeypatch):
    """Neil Armstrong's NASA portrait (public domain, tests/data/README.txt)."""
    import cv2
    from pathlib import Path

    from ninaivu.media import model_catalog
    from ninaivu.media.faces import FaceEngine

    # An earlier test's app may have pointed every model at its own empty folder; this one wants the real ones.
    monkeypatch.setattr(model_catalog, "_root", None)
    monkeypatch.delenv("NINAIVU_AI_MODELS_DIR", raising=False)

    with Image.open(Path(__file__).parent / "data" / "portrait.jpg") as opened:
        rgb = np.asarray(opened.convert("RGB"))
    located = FaceEngine(Path(".")).locate(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
    assert len(located) == 1
    found = portrait.analyse(rgb, located)
    face = found.faces[0]
    assert face["usable"] is True
    frame = face["frame"]
    cx, cy, d = frame["x"] * rgb.shape[1], frame["y"] * rgb.shape[0], frame["d"] * rgb.shape[1]
    # A cheek is skin; an eye, the lips and the background are not.
    skin = found.planes["skin"]
    assert skin[int(cy + 0.6 * d), int(cx - 0.45 * d)] > 0.5 and skin[int(cy + 0.6 * d), int(cx + 0.45 * d)] > 0.5
    for u, v in ((-0.5, 0.0), (0.5, 0.0), (0.0, 1.0)):
        assert skin[int(cy + v * d), int(cx + u * d)] < 0.2, (u, v)
    assert skin[40:200, 40:200].max() == 0.0
    # Light reaches the ears and the neck as well, so there is no seam.
    zone = found.planes["zone"]
    assert zone[int(cy + 0.9 * d), int(cx)] > 0.6 and zone[int(cy + 2.0 * d), int(cx)] > 0.4
    # A well-exposed face is not offered light, and the measurements are those of a fair face in good light.
    assert "faceLight" not in face["suggest"]
    assert 55 < face["measure"]["lightness"] < 85
    assert face["marks"] is False
    # Hair is the colour of the moon behind it: declined, not guessed.
    assert face["hair"]["found"] is False
