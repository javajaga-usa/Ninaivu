"""Derivative generation: hashing, blurhash, EXIF parsing, port selection."""


from PIL import Image

from ninaivu.media import media
from ninaivu.__main__ import pick_port, port_is_free


def solid(color, size=(64, 64)):
    return Image.new("RGB", size, color)


def test_perceptual_hash_is_stable_and_discriminating():
    a = solid((200, 30, 30))
    b = solid((200, 30, 30))
    c = Image.new("RGB", (64, 64))
    for x in range(64):
        for y in range(64):
            c.putpixel((x, y), (x * 4 % 256, y * 4 % 256, 0))

    ha, hb, hc = (media.perceptual_hash(i) for i in (a, b, c))
    assert ha == hb
    assert len(ha) == 16
    assert media.hamming(ha, hb) == 0
    assert media.hamming(ha, hc) > 6


def test_hamming_handles_garbage():
    assert media.hamming("zz", "zz") == 64
    assert media.hamming(None, "0") == 64


def test_blurhash_round_trip_shape():
    hash_ = media.blurhash_encode(solid((10, 120, 200)), cx=4, cy=3)
    assert hash_
    # 1 size flag + 1 max flag + 4 DC + 2 per AC component
    assert len(hash_) == 1 + 1 + 4 + 2 * (4 * 3 - 1)
    assert all(ch in media._B83 for ch in hash_)


def test_dominant_color_is_hex():
    color = media.dominant_color(solid((255, 0, 0)))
    assert color.startswith("#") and len(color) == 7


def test_exif_reader_extracts_fields(tmp_path):
    path = tmp_path / "x.jpg"
    img = solid((5, 5, 5))
    exif = img.getexif()
    exif[0x9003] = "2021:07:04 13:45:00"
    exif[0x010F] = "Acme"
    exif[0x0110] = "Cam9"
    img.save(path, "JPEG", exif=exif)

    with Image.open(path) as loaded:
        info = media.read_exif(loaded)
    assert info["camera"] == "Acme Cam9"
    assert info["captured_at"] > 0


def test_exif_reader_survives_no_exif(tmp_path):
    path = tmp_path / "plain.png"
    solid((1, 2, 3)).save(path)
    with Image.open(path) as loaded:
        assert media.read_exif(loaded) == {} or "orientation" in media.read_exif(loaded)


def test_thumb_names_are_sharded_and_stable():
    base = media.thumb_base("/root", "a/b/c.jpg")
    assert base == media.thumb_base("/root", "a/b/c.jpg")
    assert base != media.thumb_base("/other", "a/b/c.jpg")
    assert "/" in base  # sharded into a subdirectory
    assert media.thumb_file(base, 256, "WEBP").endswith("_256.webp")


def test_write_and_remove_thumbnails(tmp_path):
    base = "ab/abcdef"
    media.write_thumbnails(solid((9, 9, 9), (800, 600)), tmp_path, base, (128, 320))
    small = tmp_path / f"{base}_128.webp"
    assert small.is_file()
    with Image.open(small) as thumb:
        assert max(thumb.size) == 128
    media.remove_thumbnails(tmp_path, base, (128, 320))
    assert not small.exists()


def test_human_size():
    assert media.human_size(0) == "0 B"
    assert media.human_size(1536).endswith("KB")
    assert media.human_size(5 * 1024 ** 3).endswith("GB")


# -- port selection ----------------------------------------------------------

def test_pick_port_returns_preferred_when_free():
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
    assert pick_port("127.0.0.1", free) == free


def test_pick_port_steps_over_a_busy_port(monkeypatch):
    import socket

    import ninaivu.__main__ as server
    monkeypatch.setattr(server, "PREFERRED_WAIT", 0.5)

    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        busy = taken.getsockname()[1]
        assert not port_is_free("127.0.0.1", busy)
        chosen = pick_port("127.0.0.1", busy)
        assert chosen != busy
        assert port_is_free("127.0.0.1", chosen)


# --- decoding only as large as indexing needs ------------------------------
#
# A 24-megapixel camera JPEG decoded in full is 72 MB of pixels, and nothing
# indexing makes from it is larger than 640 pixels. Decoding at a quarter size
# measured 3.7x less processor time a photo on real Canon JPEGs.


def camera_jpeg(path, size=(4000, 3000), orientation=None):
    image = Image.new("RGB", size, (90, 140, 200))
    exif = image.getexif()
    if orientation:
        exif[0x0112] = orientation
    image.save(path, "JPEG", quality=85, exif=exif)
    return path


def test_a_big_jpeg_is_decoded_small_but_keeps_its_real_size(tmp_path):
    photo = camera_jpeg(tmp_path / "IMG_0001.jpg")
    image, size = media.open_for_index(photo, 640)
    assert size == (4000, 3000)
    assert max(image.size) < 4000, "it was decoded in full"
    assert min(image.size) >= 640, "decoded smaller than a thumbnail needs"


def test_a_sideways_camera_tag_swaps_the_real_size_too(tmp_path):
    """Orientation 6 is a portrait photograph stored lying down."""
    photo = camera_jpeg(tmp_path / "IMG_0002.jpg", orientation=6)
    image, size = media.open_for_index(photo, 640)
    assert size == (3000, 4000)
    assert image.size[1] > image.size[0], "the picture must be stood upright"


def test_a_small_jpeg_is_not_made_smaller_than_it_is(tmp_path):
    photo = camera_jpeg(tmp_path / "small.jpg", size=(500, 400))
    image, size = media.open_for_index(photo, 640)
    assert size == (500, 400) and image.size == (500, 400)


def test_a_format_that_cannot_be_decoded_small_is_decoded_as_before(tmp_path):
    photo = tmp_path / "scan.png"
    Image.new("RGB", (2000, 1000), (10, 20, 30)).save(photo)
    image, size = media.open_for_index(photo, 640)
    assert size == (2000, 1000) and image.size == (2000, 1000)


def test_a_greyscale_jpeg_stays_greyscale(tmp_path):
    """Asking for a smaller decode must not also change its colour mode."""
    photo = tmp_path / "grey.jpg"
    Image.new("L", (4000, 3000), 128).save(photo, "JPEG")
    image, size = media.open_for_index(photo, 640)
    assert image.mode == "L" and size == (4000, 3000)


def test_the_duplicate_fingerprint_survives_the_smaller_decode(tmp_path):
    """Or every existing duplicate group would be rewritten on the next scan."""
    photo = tmp_path / "gradient.jpg"
    seed = Image.new("RGB", (40, 30))
    seed.putdata([(x * 6 % 256, y * 8 % 256, (x * y) % 256)
                   for y in range(30) for x in range(40)])
    seed.resize((4000, 3000), Image.Resampling.BILINEAR).save(
        photo, "JPEG", quality=90)

    decoded, _ = media.open_for_index(photo, 640)
    with Image.open(photo) as full:
        full.load()
        assert media.hamming(media.perceptual_hash(full),
                             media.perceptual_hash(decoded)) <= 2
