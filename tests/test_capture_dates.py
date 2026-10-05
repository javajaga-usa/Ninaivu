"""A file with no EXIF date is still filed under the day it was taken.

The archive used to go from EXIF and Takeout straight to the file's own
timestamp, so a WhatsApp photo, a phone video or a scanned album restored from
a backup landed under the day of the restore — and the library, trusting the
``YYYY/MM/DD`` folder it had been copied into, showed it there too. These
check each piece of evidence ``ninaivu.archive.dates`` now reads, the traps it
must not fall into, and that the archive and the gallery agree about the day.
"""

import json
import os
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PIL import Image

from ninaivu import archive
from ninaivu.archive import database as adb
from ninaivu.archive import dates, scanner as archive_scanner
from ninaivu.archive.scanner import ArchiveJob
from ninaivu.media import media
from ninaivu.media.scanner import build_record

RESTORED = datetime(2024, 3, 1, 10, 0).timestamp()      # the day a backup was restored


def _stamp(path, when=RESTORED):
    os.utime(path, (when, when))
    return path


def _photo(path, exif_date=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    # A colour per name: identical bytes would be archived once, as duplicates.
    shade = sum(path.name.encode()) % 256
    image = Image.new("RGB", (64, 48), (shade, 90, 255 - shade))
    if exif_date:
        exif = image.getexif()
        exif.get_ifd(0x8769)[36867] = exif_date
        image.save(path, "JPEG", exif=exif)
    else:
        image.save(path, "JPEG")
    return _stamp(path)


# --- building video containers ----------------------------------------------

def _box(kind, payload):
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def _qt_seconds(when):
    return int((when - datetime(1904, 1, 1, tzinfo=timezone.utc)).total_seconds())


def _mvhd(seconds, version=0):
    if version == 1:
        body = bytes([1, 0, 0, 0]) + struct.pack(">QQIQ", seconds, seconds, 1000, 5000)
    else:
        body = bytes(4) + struct.pack(">IIII", seconds, seconds, 1000, 5000)
    return _box(b"mvhd", body + bytes(80))


def _apple_meta(value):
    key = dates._APPLE_CREATION_KEY
    hdlr = _box(b"hdlr", bytes(8) + b"mdta" + bytes(13))
    keys = _box(b"keys", bytes(4) + struct.pack(">I", 1)
                + struct.pack(">I4s", 8 + len(key), b"mdta") + key)
    data = _box(b"data", struct.pack(">II", 1, 0) + value.encode())
    item = struct.pack(">I", 8 + len(data)) + struct.pack(">I", 1) + data
    return _box(b"meta", hdlr + keys + _box(b"ilst", item))


def _mp4(path, *, recorded=None, version=0, apple=None, brand=b"isom"):
    """ftyp, then the media, then the movie header — the order phones write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    moov = _mvhd(_qt_seconds(recorded) if recorded else 0, version)
    if apple:
        moov += _apple_meta(apple)
    ftyp = _box(b"ftyp", brand + struct.pack(">I", 512) + b"isommp41")
    path.write_bytes(ftyp + _box(b"mdat", bytes(4096)) + _box(b"moov", moov))
    return _stamp(path)


def _avi(path, chunk=b"IDIT", value=b"SAT DEC 31 12:34:56 2005\n\x00"):
    def piece(cid, data):
        return struct.pack("<4sI", cid, len(data)) + data + (b"\x00" if len(data) % 2 else b"")

    def listed(kind, content):
        return struct.pack("<4sI", b"LIST", 4 + len(content)) + kind + content

    header = piece(b"avih", bytes(56))
    if chunk == b"IDIT":
        body = listed(b"hdrl", header + piece(b"IDIT", value)) + listed(b"movi", bytes(64))
    else:
        body = listed(b"hdrl", header) + listed(b"INFO", piece(chunk, value)) + listed(b"movi", bytes(64))
    riff = b"AVI " + body
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<4sI", b"RIFF", len(riff)) + riff)
    return _stamp(path)


# ---------------------------------------------------------------------------
# 1. inside the file
# ---------------------------------------------------------------------------

def test_a_phone_video_is_dated_by_its_movie_header(tmp_path):
    noon = datetime(2018, 7, 4, 12, 0, tzinfo=timezone.utc)
    clip = _mp4(tmp_path / "clip.mp4", recorded=noon)
    assert dates.container_date(clip).date() == datetime(2018, 7, 4).date()
    assert archive_scanner.capture_date(str(clip))[1] == "container"


def test_a_64_bit_movie_header_is_read_too(tmp_path):
    clip = _mp4(tmp_path / "long.mov", recorded=datetime(2021, 1, 9, 12, tzinfo=timezone.utc),
                version=1)
    assert dates.container_date(clip).date() == datetime(2021, 1, 9).date()


def test_apples_local_recording_time_beats_the_utc_header(tmp_path):
    """Recorded at 00:30 in India is 4 July there, whatever this machine's zone."""
    clip = _mp4(tmp_path / "IMG_0042.MOV",
                recorded=datetime(2018, 7, 3, 19, 0, tzinfo=timezone.utc),
                apple="2018-07-04T00:30:00+0530")
    assert dates.container_date(clip) == datetime(2018, 7, 4, 0, 30)


def test_an_unset_movie_header_is_not_1904(tmp_path):
    clip = _mp4(tmp_path / "blank.mp4")
    assert dates.container_date(clip) is None
    assert archive_scanner.capture_date(str(clip))[1] == "filesystem"


def test_a_heic_is_not_mistaken_for_a_video(tmp_path):
    still = _mp4(tmp_path / "IMG_0001.HEIC", brand=b"heic",
                 recorded=datetime(2011, 1, 1, 12, tzinfo=timezone.utc))
    assert dates.container_date(still) is None


@pytest.mark.parametrize("chunk,value,expected", [
    (b"IDIT", b"SAT DEC 31 12:34:56 2005\n\x00", datetime(2005, 12, 31, 12, 34, 56)),
    (b"IDIT", b"2006:08:15 09:10:11\x00", datetime(2006, 8, 15, 9, 10, 11)),
    (b"ICRD", b"2004-02-29\x00", datetime(2004, 2, 29)),
])
def test_an_old_camera_avi_is_dated_by_its_riff_chunks(tmp_path, chunk, value, expected):
    movie = _avi(tmp_path / "MVI_0001.AVI", chunk, value)
    assert dates.container_date(movie) == expected


def test_a_jpeg_has_no_container_date(tmp_path):
    assert dates.container_date(_photo(tmp_path / "a.jpg")) is None


# ---------------------------------------------------------------------------
# 2. Google Takeout
# ---------------------------------------------------------------------------

def _sidecar(path, when):
    ts = int(when.replace(tzinfo=timezone.utc).timestamp())
    path.write_text(json.dumps({"photoTakenTime": {"timestamp": str(ts)}}))


def test_takeouts_name_for_a_duplicate_is_found(tmp_path):
    photo = _photo(tmp_path / "IMG_1234(1).jpg")
    _sidecar(tmp_path / "IMG_1234.jpg(1).json", datetime(2016, 6, 2, 12))
    assert dates.takeout_date(photo).date() == datetime(2016, 6, 2).date()


def test_the_original_sidecar_is_not_used_for_its_duplicate(tmp_path):
    photo = _photo(tmp_path / "IMG_1234(1).jpg")
    _sidecar(tmp_path / "IMG_1234.jpg.json", datetime(2010, 1, 1, 12))
    _sidecar(tmp_path / "IMG_1234.jpg(1).json", datetime(2016, 6, 2, 12))
    assert dates.takeout_date(photo).year == 2016


def test_takeouts_truncated_sidecar_name_is_found(tmp_path):
    photo = _photo(tmp_path / "Screenshot_Messenger_family_chat.jpg")
    _sidecar(tmp_path / "Screenshot_Messenger_family_chat.jpg.supplemen.json",
             datetime(2020, 8, 8, 12))
    assert dates.takeout_date(photo).date() == datetime(2020, 8, 8).date()


def test_an_edited_copy_uses_its_originals_sidecar(tmp_path):
    photo = _photo(tmp_path / "IMG_77-edited.jpg")
    _sidecar(tmp_path / "IMG_77.jpg.supplemental-metadata.json", datetime(2015, 3, 3, 12))
    assert dates.takeout_date(photo).year == 2015


def test_a_takeout_scan_from_before_1970_keeps_its_date(tmp_path):
    photo = _photo(tmp_path / "grandparents.jpg")
    _sidecar(tmp_path / "grandparents.jpg.json", datetime(1955, 6, 1, 12))
    assert dates.takeout_date(photo).date() == datetime(1955, 6, 1).date()


# ---------------------------------------------------------------------------
# 3. the filename
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("IMG-20190512-WA0001.jpg", datetime(2019, 5, 12)),
    ("VID-20190512-WA0003.mp4", datetime(2019, 5, 12)),
    ("IMG_20190512_123456.jpg", datetime(2019, 5, 12, 12, 34, 56)),
    ("PXL_20230101_101112345.jpg", datetime(2023, 1, 1)),
    ("20190512_123456.mp4", datetime(2019, 5, 12, 12, 34, 56)),
    ("Screenshot_2021-11-03-14-22-10.png", datetime(2021, 11, 3, 14, 22, 10)),
    ("signal-2019-05-12-123456.jpg", datetime(2019, 5, 12, 12, 34, 56)),
    ("DJI_20230501123456_0001_D.JPG", datetime(2023, 5, 1, 12, 34, 56)),
    ("1987-08-14 wedding scan.tif", datetime(1987, 8, 14)),
])
def test_dates_written_in_filenames(name, expected):
    assert dates.filename_date(name) == expected


@pytest.mark.parametrize("name", [
    "DSC_2012.JPG",              # a counter, not a year
    "P1000123.JPG",              # Panasonic's counter
    "1920x1080 wallpaper.jpg",
    "IMG_20190231_000000.jpg",   # 31 February
    "IMG_2019-0512.jpg",         # separators disagree
    "VID_20990101_000000.mp4",   # the future is a dead clock
    "IMG_0042.JPG",
])
def test_numbers_that_are_not_dates(name):
    assert dates.filename_date(name) is None


# ---------------------------------------------------------------------------
# 4. dated folders, weighed against the file's timestamp
# ---------------------------------------------------------------------------

def _period(path):
    period = dates.folder_period(path)
    return (period[0].date(), period[1].date()) if period else None


def test_dated_folder_layouts():
    d = datetime
    assert _period("D:/Scans/2017/07/beach.jpg") == (d(2017, 7, 1).date(), d(2017, 7, 31).date())
    assert _period("D:/Scans/2017/07/15/beach.jpg") == (d(2017, 7, 15).date(),) * 2
    assert _period("D:/2017-07-15 Kerala trip/beach.jpg") == (d(2017, 7, 15).date(),) * 2
    assert _period("D:/20170715/beach.jpg") == (d(2017, 7, 15).date(),) * 2
    assert _period("D:/Photos 2017/beach.jpg") is None
    assert _period("D:/2017/Kerala/beach.jpg") == (d(2017, 1, 1).date(), d(2017, 12, 31).date())
    # A number below an undated folder is not a month.
    assert _period("D:/2017/Kerala/07/beach.jpg") == (d(2017, 1, 1).date(), d(2017, 12, 31).date())
    # The deepest dated folder wins over the backup that holds it.
    assert _period(r"E:\2024-03-01 phone\DCIM\2016\02\x.jpg")[0] == d(2016, 2, 1).date()
    assert _period("D:/100CANON/IMG_0001.JPG") is None


def test_a_timestamp_later_than_the_folder_is_a_copy():
    july = dates.folder_period("S/2017/07/a.jpg")
    assert dates.weigh_folder(july, datetime(2024, 3, 1)) == (datetime(2017, 7, 1), "folder")


def test_a_timestamp_inside_the_folder_is_more_precise():
    july = dates.folder_period("S/2017/07/a.jpg")
    assert dates.weigh_folder(july, datetime(2017, 7, 19, 8)) == (datetime(2017, 7, 19, 8), "filesystem")


def test_a_timestamp_earlier_than_the_folder_means_the_folder_is_a_label():
    backup = dates.folder_period("S/2024-03-01 backup/a.jpg")
    assert dates.weigh_folder(backup, datetime(2012, 5, 5)) == (datetime(2012, 5, 5), "filesystem")


# ---------------------------------------------------------------------------
# 5. the file's own clock
# ---------------------------------------------------------------------------

class _Stat:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_a_dead_modification_clock_is_not_rescued_by_the_copy_date():
    st = _Stat(st_mtime=datetime(1980, 1, 1, 12).timestamp(), st_ctime=RESTORED)
    assert dates.file_timestamp(st) is None


def test_a_future_modification_clock_is_rejected():
    future = (datetime.now() + timedelta(days=30)).timestamp()
    assert dates.file_timestamp(_Stat(st_mtime=future, st_ctime=future)) is None


def test_the_earlier_believable_timestamp_wins():
    old = datetime(2012, 5, 5).timestamp()
    assert dates.file_timestamp(_Stat(st_mtime=RESTORED, st_ctime=old)) == old
    assert dates.file_timestamp(_Stat(st_mtime=old, st_ctime=RESTORED)) == old
    assert dates.file_timestamp(_Stat(st_mtime=old, st_ctime=0.0)) == old


def test_timestamps_before_1970_round_trip():
    """Windows' C library refuses them; a 1955 scan is still a photograph."""
    when = datetime(1955, 6, 1, 10, 30)
    assert dates.from_timestamp(dates.to_timestamp(when)) == when


# ---------------------------------------------------------------------------
# EXIF plausibility, the same on both sides
# ---------------------------------------------------------------------------

def test_a_future_exif_date_is_a_dead_clock_on_both_sides():
    future = (datetime.now() + timedelta(days=400)).strftime("%Y:%m:%d 10:00:00")
    assert archive_scanner._parse_exif_datetime(future) is None
    assert media._parse_exif_datetime(future) is None


def test_an_exif_date_from_1955_survives_in_the_gallery(tmp_path, cfg):
    root = tmp_path / "old-lib"
    photo = _photo(root / "scan.jpg", "1955:06:01 10:30:00")
    record = build_record(root, "scan.jpg", photo.stat(), cfg)
    assert (record["date_key"], record["date_source"]) == ("1955-06-01", "exif")
    assert archive_scanner.capture_date(str(photo))[0].date() == datetime(1955, 6, 1).date()


# ---------------------------------------------------------------------------
# End to end: archive, then index the archive, and compare
# ---------------------------------------------------------------------------

@pytest.fixture()
def engine(tmp_path):
    archive.configure(tmp_path / "state")
    adb.close_db()
    adb.init_db()
    try:
        yield tmp_path
    finally:
        adb.close_db()


def _archived(dest):
    found = {}
    for base, _dirs, files in os.walk(dest):
        for name in files:
            if name.startswith(".") or name in archive_scanner.status_kit.KIT_FILES:
                continue
            found[name] = os.path.relpath(base, dest).replace(os.sep, "/")
    return found


def test_the_archive_and_the_gallery_file_by_the_same_day(engine, cfg):
    src, dest = engine / "restored-backup", engine / "MasterArchive"
    _photo(src / "WhatsApp Images" / "IMG-20190512-WA0001.jpg")
    _mp4(src / "Camera" / "clip.mp4", recorded=datetime(2018, 7, 4, 12, tzinfo=timezone.utc))
    _photo(src / "Scans" / "2017" / "07" / "beach.jpg")
    _stamp(_photo(src / "2024-03-01 phone" / "kept.jpg"), datetime(2012, 5, 5, 9).timestamp())
    _stamp(_photo(src / "deadclock.jpg"), datetime(1980, 1, 1, 12).timestamp())
    future = (datetime.now() + timedelta(days=400)).strftime("%Y:%m:%d 10:00:00")
    _stamp(_photo(src / "wrongclock.jpg", future), datetime(2015, 8, 20, 9).timestamp())
    _photo(src / "camera.jpg", "2014:11:23 16:45:00")

    ArchiveJob([str(src)], str(dest)).run()
    filed = _archived(dest)
    assert filed == {
        "IMG-20190512-WA0001.jpg": "2019/05/12",
        "clip.mp4": "2018/07/04",
        "beach.jpg": "2017/07/01",
        "kept.jpg": "2012/05/05",
        "deadclock.jpg": f"{archive_scanner.UNDATED_FOLDER}/restored-backup",
        "wrongclock.jpg": "2015/08/20",
        "camera.jpg": "2014/11/23",
    }

    for name, folder in filed.items():
        rel = f"{folder}/{name}"
        record = build_record(dest, rel, (dest / rel).stat(), cfg)
        undated = folder.split("/")[0] == archive_scanner.UNDATED_FOLDER
        expected = "" if undated else folder.replace("/", "-")
        assert record["date_key"] == expected, (
            f"{name}: archived under {folder}, gallery says {record['date_key']!r} "
            f"from {record['date_source']}")


def test_an_undated_file_is_indexed_as_undated(tmp_path, cfg):
    from ninaivu.server import auth
    from ninaivu.storage import db
    from ninaivu.media.scanner import Scanner

    library = Path(cfg.active_root)
    _stamp(_photo(library / "misc" / "deadclock.jpg"), 0)
    conn = db.init_db(cfg.db_path)
    auth.init_auth_schema(conn)
    Scanner(cfg)._run(library, full=True)

    row = conn.execute("SELECT * FROM assets WHERE filename='deadclock.jpg'").fetchone()
    assert row is not None, "an undated file must still be indexed"
    assert (row["captured_at"], row["date_key"], row["date_source"]) == (None, "", "none")


# ---------------------------------------------------------------------------
# 5. the readers that cannot answer are not asked
# ---------------------------------------------------------------------------

def test_a_video_is_not_offered_to_the_exif_readers(tmp_path, monkeypatch):
    """Neither EXIF reader understands a movie, but both opened every one and
    searched it for a header that is not there."""
    opened = []

    class NoPictures:
        @staticmethod
        def open(*args, **kwargs):
            opened.append(args)
            raise AssertionError("Pillow was asked to open a video")

    class NoExif:
        @staticmethod
        def process_file(*args, **kwargs):
            opened.append(args)
            raise AssertionError("ExifRead was asked to read a video")

    monkeypatch.setattr(archive_scanner, "_PILImage", NoPictures)
    monkeypatch.setattr(archive_scanner, "exifread", NoExif)
    noon = datetime(2018, 7, 4, 12, 0, tzinfo=timezone.utc)
    clip = _mp4(tmp_path / "clip.mp4", recorded=noon)
    when, source = archive_scanner.capture_date(str(clip))
    assert (when.date(), source) == (noon.date(), "container")
    song = tmp_path / "song.mp3"
    song.write_bytes(b"ID3" + bytes(64))
    _stamp(song)
    assert archive_scanner.capture_date(str(song))[1] == "filesystem"
    assert opened == []


def test_the_callers_stat_is_used_rather_than_taken_again(tmp_path, monkeypatch):
    clip = _mp4(tmp_path / "blank.mp4")
    st = os.stat(clip)
    real_stat, again = os.stat, []

    def watching(path, *args, **kwargs):
        if os.fspath(path) == str(clip):
            again.append(path)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", watching)
    when, source = archive_scanner.capture_date(str(clip), st=st)
    assert source == "filesystem" and when.timestamp() == RESTORED
    assert again == [], "the file was stat'ed again"


def _png(path, exif_date=None):
    image = Image.new("RGB", (64, 48), (20, 90, 200))
    if exif_date:
        exif = image.getexif()
        # Pillow writes a PNG's EXIF only when the top-level IFD has something
        # in it; the sub-IFD alone is dropped on the way out.
        exif[306] = exif_date
        exif.get_ifd(0x8769)[36867] = exif_date
        image.save(path, "PNG", exif=exif)
    else:
        image.save(path, "PNG")
    return _stamp(path)


def _count_decodes(monkeypatch):
    from PIL import ImageFile
    decodes = []
    real = ImageFile.ImageFile.load

    def counting(self):
        decodes.append(self.filename)
        return real(self)

    monkeypatch.setattr(ImageFile.ImageFile, "load", counting)
    return decodes


def test_a_png_is_dated_without_decoding_its_pixels(tmp_path, monkeypatch):
    """Pillow puts a PNG's EXIF chunk after the pixel data and its getexif()
    decodes the whole picture to reach it; a screenshot has none and paid for
    the decode anyway. Walking the chunk table finds the same answer."""
    decodes = _count_decodes(monkeypatch)
    plain = _png(tmp_path / "screenshot.png")
    dated = _png(tmp_path / "exported.png", "2016:05:04 10:00:00")
    assert archive_scanner.capture_date(str(plain))[1] == "filesystem"
    when, source = archive_scanner.capture_date(str(dated))
    assert (when.date(), source) == (datetime(2016, 5, 4).date(), "exif")
    assert decodes == [], "a PNG was decoded to look for its date"


def test_a_png_with_its_exif_after_the_pixels_is_still_dated(tmp_path, monkeypatch):
    """The PNG spec lets a writer put the EXIF chunk after the image data, and
    then neither ExifRead nor Pillow sees it on the way in: only a full decode
    did. The chunk walk reaches it with a seek."""
    decodes = _count_decodes(monkeypatch)
    dated = _png(tmp_path / "late.png", "2016:05:04 10:00:00")
    raw = dated.read_bytes()
    chunks, pos = [], 8
    while pos < len(raw):
        length = struct.unpack(">I", raw[pos:pos + 4])[0]
        chunks.append(raw[pos:pos + 12 + length])
        pos += 12 + length
    kinds = [c[4:8] for c in chunks]
    assert kinds == [b"IHDR", b"eXIf", b"IDAT", b"IEND"], kinds
    exif = chunks.pop(1)
    chunks.insert(2, exif)
    dated.write_bytes(raw[:8] + b"".join(chunks))
    _stamp(dated)
    with Image.open(dated) as img:
        assert "exif" not in img.info, "the fixture's EXIF must follow the pixels"
    when, source = archive_scanner.capture_date(str(dated))
    assert (when.date(), source) == (datetime(2016, 5, 4).date(), "exif")
    assert decodes == [], "a PNG was decoded to look for its date"


def test_a_jpeg_still_reads_its_date_with_pillow_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(archive_scanner, "exifread", None)
    photo = _photo(tmp_path / "camera.jpg", "2017:08:09 10:00:00")
    when, source = archive_scanner.capture_date(str(photo))
    assert (when.date(), source) == (datetime(2017, 8, 9).date(), "exif")
