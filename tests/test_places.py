"""Naming the place a photograph was taken, from a list held on the machine.

Nothing here touches the network. The fetch is tested against a zip built in
the test, which is the part that could go wrong quietly — a change in the
column order would otherwise show up as a library full of wrong towns.
"""

import io
import zipfile
from pathlib import Path

import pytest

from ninaivu.utils import places

# name, ascii, alt, lat, lon, feature class, feature code, country, cc2,
# admin1..4, population, elevation, dem, timezone, modified — GeoNames' order.
ROWS = [
    ("London", 51.5074, -0.1278, "GB", "PPLC", 8961989),
    ("City of Westminster", 51.4973, -0.1372, "GB", "PPLA3", 247614),
    ("Alpharetta", 34.0754, -84.2941, "US", "PPL", 63693),
    ("Chester", 32.3918, -83.1524, "US", "PPL", 1568),
    ("Times Square", 40.7570, -73.9860, "US", "PPLX", 17749),
]


def geonames_zip() -> bytes:
    lines = []
    for name, lat, lon, country, feature, population in ROWS:
        fields = [""] * 19
        fields[1] = name
        fields[2] = name
        fields[4] = str(lat)
        fields[5] = str(lon)
        fields[6] = "P"
        fields[7] = feature
        fields[8] = country
        fields[14] = str(population)
        lines.append("\t".join(fields))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("cities1000.txt", "\n".join(lines) + "\n")
    return buffer.getvalue()


@pytest.fixture()
def gazetteer(tmp_path, monkeypatch):
    """A gazetteer installed from a fabricated download."""
    class FakeAnswer:
        def __init__(self, payload):
            self.payload = payload

        def read(self):
            return self.payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(places.urllib.request, "urlopen",
                        lambda *a, **k: FakeAnswer(geonames_zip()))
    places.install(tmp_path)
    return places.gazetteer(tmp_path)


# ---------------------------------------------------------------------------
# Fetching and trimming
# ---------------------------------------------------------------------------

def test_nothing_is_installed_to_begin_with(tmp_path):
    assert not places.installed(tmp_path)
    assert places.gazetteer(tmp_path) is None


def test_the_download_is_trimmed_to_five_columns(gazetteer, tmp_path):
    assert places.installed(tmp_path)
    first = places.data_path(tmp_path).read_text(encoding="utf-8").splitlines()[0]
    assert len(first.split("\t")) == 5


def test_a_section_of_a_town_is_not_a_town(gazetteer):
    """Times Square is a place in New York, not the place you were."""
    assert "Times Square" not in gazetteer.names


def test_an_interrupted_download_leaves_nothing_half_written(tmp_path, monkeypatch):
    def explodes(*a, **k):
        raise OSError("the connection dropped")

    monkeypatch.setattr(places.urllib.request, "urlopen", explodes)
    with pytest.raises(OSError):
        places.install(tmp_path)
    assert not places.installed(tmp_path)


# ---------------------------------------------------------------------------
# Choosing the name
# ---------------------------------------------------------------------------

def test_a_photograph_in_a_town_is_named_for_it(gazetteer):
    found = gazetteer.nearest(34.0754, -84.2941)
    assert found["city"] == "Alpharetta"
    assert found["country"] == "US"


def test_the_better_known_place_wins_when_both_are_close(gazetteer):
    """Nearest alone says "City of Westminster" for a photograph in London."""
    found = gazetteer.nearest(51.5007, -0.1246)
    assert found["city"] == "London"


def test_a_small_town_is_still_the_answer_when_it_is_the_only_one(gazetteer):
    found = gazetteer.nearest(32.40, -83.16)
    assert found["city"] == "Chester"


def test_the_middle_of_the_ocean_is_not_named(gazetteer):
    assert gazetteer.nearest(35.0, -40.0) is None


def test_a_place_beyond_the_limit_is_not_claimed(gazetteer):
    """Naming a photograph after a town eighty kilometres away is a guess."""
    assert gazetteer.nearest(33.2, -84.2941, max_km=10) is None


def test_the_distance_that_decided_it_is_reported(gazetteer):
    found = gazetteer.nearest(34.0760, -84.2950)
    assert 0 <= found["km"] < 1


# ---------------------------------------------------------------------------
# Through the scan
# ---------------------------------------------------------------------------

def test_the_pass_is_skipped_when_it_is_switched_off(scanned):
    from ninaivu.media.scanner import Scanner

    cfg, conn, _ = scanned
    cfg.place_names = False
    conn.execute("UPDATE assets SET gps_lat=34.0754, gps_lon=-84.2941, "
                 "place_version=0 WHERE kind='picture'")
    conn.commit()
    Scanner(cfg)._name_places(conn, cfg.active_root)
    assert conn.execute(
        "SELECT COUNT(*) n FROM assets WHERE city IS NOT NULL"
    ).fetchone()["n"] == 0


def test_the_scan_fills_in_the_town(scanned, tmp_path, monkeypatch):
    from ninaivu.media.scanner import Scanner

    cfg, conn, _ = scanned
    cfg.place_names = True
    monkeypatch.setattr(places, "installed", lambda _s: True)
    monkeypatch.setattr(places, "gazetteer", lambda _s: _Book())
    conn.execute("UPDATE assets SET gps_lat=34.0754, gps_lon=-84.2941, "
                 "place_version=0 WHERE kind='picture'")
    conn.commit()

    Scanner(cfg)._name_places(conn, cfg.active_root)
    named = conn.execute(
        "SELECT COUNT(*) n FROM assets WHERE city='Alpharetta'").fetchone()["n"]
    assert named > 0


def test_a_photograph_with_no_coordinates_is_left_alone(scanned, monkeypatch):
    from ninaivu.media.scanner import Scanner

    cfg, conn, _ = scanned
    cfg.place_names = True
    monkeypatch.setattr(places, "installed", lambda _s: True)
    monkeypatch.setattr(places, "gazetteer", lambda _s: _Book())
    conn.execute("UPDATE assets SET gps_lat=NULL, gps_lon=NULL, place_version=0")
    conn.commit()

    Scanner(cfg)._name_places(conn, cfg.active_root)
    assert conn.execute(
        "SELECT COUNT(*) n FROM assets WHERE place_version > 0"
    ).fetchone()["n"] == 0


def test_a_photograph_nowhere_near_a_town_is_marked_done(scanned, monkeypatch):
    """Otherwise every scan looks up the same empty patch of sea forever."""
    from ninaivu.media.scanner import Scanner

    cfg, conn, _ = scanned
    cfg.place_names = True
    monkeypatch.setattr(places, "installed", lambda _s: True)
    monkeypatch.setattr(places, "gazetteer", lambda _s: _Book(answer=None))
    conn.execute("UPDATE assets SET gps_lat=35.0, gps_lon=-40.0, place_version=0 "
                 "WHERE kind='picture'")
    conn.commit()

    Scanner(cfg)._name_places(conn, cfg.active_root)
    rows = conn.execute(
        "SELECT city, place_version FROM assets WHERE kind='picture'").fetchall()
    assert rows and all(r["place_version"] > 0 and r["city"] is None for r in rows)


def test_a_failed_fetch_does_not_fail_the_scan(scanned, monkeypatch):
    from ninaivu.media.scanner import Scanner

    cfg, conn, _ = scanned
    cfg.place_names = True
    monkeypatch.setattr(places, "installed", lambda _s: False)

    def explodes(*a, **k):
        raise OSError("no network")

    monkeypatch.setattr(places, "install", explodes)
    Scanner(cfg)._name_places(conn, cfg.active_root)   # must not raise


def test_the_switch_is_on_the_command_line():
    root = Path(__file__).resolve().parents[1]
    main = (root / "ninaivu" / "__main__.py").read_text(encoding="utf-8")
    assert '"--places"' in main and "cfg.place_names = True" in main


def test_the_naming_pass_says_how_far_it_has_got(scanned, monkeypatch):
    """Otherwise the progress bar sits where indexing left it."""
    from ninaivu.media.scanner import Scanner

    cfg, conn, _ = scanned
    cfg.place_names = True
    monkeypatch.setattr(places, "installed", lambda _s: True)
    monkeypatch.setattr(places, "gazetteer", lambda _s: _Book())
    conn.execute("UPDATE assets SET gps_lat=34.0754, gps_lon=-84.2941, "
                 "place_version=0")
    conn.commit()

    scanner = Scanner(cfg)
    scanner._name_places(conn, cfg.active_root)
    assert scanner.progress.tag_total > 0
    assert scanner.progress.tagged == scanner.progress.tag_total


def test_a_stopped_naming_pass_keeps_the_names_it_had_already_written(
        scanned, monkeypatch):
    """Names are written a batch at a time, so a stop must not undo the batch.

    One commit per photograph was most of the cost of this pass — the lookup
    itself is a dictionary hit — but batching is only safe if a scan that is
    stopped half way through keeps what it had got to, so the next one carries
    on instead of starting again.
    """
    from ninaivu.media.scanner import Scanner

    cfg, conn, _ = scanned
    cfg.place_names = True
    monkeypatch.setattr(places, "installed", lambda _s: True)
    scanner = Scanner(cfg)
    scanner.PLACE_BATCH = 2

    class _StopsPartWayThrough(_Book):
        def __init__(self):
            super().__init__()
            self.asked = 0

        def nearest(self, lat, lon, max_km=50.0):
            self.asked += 1
            if self.asked >= 4:
                scanner._stop.set()
            return super().nearest(lat, lon, max_km)

    monkeypatch.setattr(places, "gazetteer", lambda _s: _StopsPartWayThrough())
    conn.execute("UPDATE assets SET gps_lat=34.0754, gps_lon=-84.2941, "
                 "place_version=0")
    conn.commit()

    scanner._name_places(conn, cfg.active_root)

    named = conn.execute(
        "SELECT COUNT(*) n FROM assets WHERE city='Alpharetta'").fetchone()["n"]
    total = conn.execute("SELECT COUNT(*) n FROM assets").fetchone()["n"]
    assert 0 < named < total


class _Book:
    """A gazetteer of one answer."""

    def __init__(self, answer="Alpharetta"):
        self.answer = answer

    def nearest(self, lat, lon, max_km=50.0):
        if self.answer is None:
            return None
        return {"city": self.answer, "country": "US", "km": 0.1}
