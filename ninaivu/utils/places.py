"""Turning coordinates into the name of a place, without asking anyone.

A camera writes down where it was, never what it was. Ninaivu has had the
coordinates all along — the map is built on them — but `assets.city` has
always been empty, so an occasion could only ever be called "12–14 May 2023"
when it was really "Ooty, 12–14 May 2023".

Every convenient way to fix that sends your coordinates to somebody's
geocoding API, which is exactly what a private library must not do. So the
answer is a gazetteer: GeoNames' `cities1000`, every settled place above a
thousand people, about 171,000 of them. It is fetched once, trimmed to the
five fields that matter, and from then on the lookup is arithmetic on a
table sitting in the state directory. Nothing about a photograph is sent
anywhere, then or ever.

The one judgement here is which name to use when several are close. Nearest
alone gives "City of Westminster" for a photograph taken in London, because
the borough's centre happens to be nearer than the city's. So distance is
weighed against how well known a place is: somewhere ten times larger is
allowed to be somewhat further away and still be the answer. It is not
perfect in dense cities — a photograph at the Eiffel Tower comes back as an
arrondissement, which is what GeoNames calls that spot — but it says London
for London, and it never invents a name for the middle of the ocean.
"""

from __future__ import annotations

import io
import logging
import math
import threading
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

try:
    import numpy as np
except Exception:  # pragma: no cover - optional
    np = None  # type: ignore

log = logging.getLogger("ninaivu.places")

#: Bumped when the naming changes in a way worth re-running a library for.
PLACE_VERSION = 1

#: GeoNames' free dump, CC BY 4.0. About 11 MB zipped, 30 MB of text inside.
SOURCE_URL = "https://download.geonames.org/export/dump/cities1000.zip"

#: What Ninaivu keeps afterwards: five columns instead of nineteen, ~5 MB.
DATA_NAME = "places.tsv"

#: Further than this and naming the photograph would be a guess dressed as a
#: fact. Mid-ocean, deep desert and most of Antarctica get no name at all.
MAX_KM = 50.0

#: How much a place's size counts against its distance. One is enough to say
#: "London" rather than "City of Westminster"; more changes nothing, because
#: by then the large places already win everywhere they should.
SIZE_WEIGHT = 1.0

#: A *section* of a settlement — Times Square, a named quarter — is not the
#: settlement. GeoNames marks those PPLX, and they read oddly as the place
#: somebody was.
SKIP_FEATURES = {"PPLX"}

_EARTH_KM = 6371.0
_cache: dict[str, "Gazetteer"] = {}
_cache_lock = threading.Lock()


def data_path(state_dir: str | Path) -> Path:
    return Path(state_dir) / DATA_NAME


def installed(state_dir: str | Path) -> bool:
    """Whether the trimmed gazetteer is already here."""
    path = data_path(state_dir)
    return path.is_file() and path.stat().st_size > 0


def install(state_dir: str | Path, *, url: str = SOURCE_URL,
            timeout: float = 120.0) -> int:
    """Fetch the gazetteer once and trim it. Returns how many places it kept.

    This is the only part that touches the network, it happens once, and it
    sends nothing but a request for a public file.
    """
    target = data_path(state_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    log.info("fetching the place-name list from %s", url)

    with urllib.request.urlopen(url, timeout=timeout) as answer:  # noqa: S310
        payload = answer.read()

    kept = 0
    temporary = target.with_suffix(".part")
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        name = next((n for n in archive.namelist() if n.endswith(".txt")), None)
        if name is None:
            raise ValueError("the place-name archive has no data file in it")
        with archive.open(name) as raw, \
                temporary.open("w", encoding="utf-8", newline="\n") as out:
            for line in io.TextIOWrapper(raw, encoding="utf-8"):
                fields = line.rstrip("\n").split("\t")
                # name, lat, lon, feature code, country, population
                if len(fields) < 15 or fields[7] in SKIP_FEATURES:
                    continue
                try:
                    lat, lon = float(fields[4]), float(fields[5])
                    population = int(fields[14] or 0)
                except ValueError:
                    continue
                place, country = fields[1].strip(), fields[8].strip()
                if not place:
                    continue
                out.write(f"{place}\t{country}\t{lat}\t{lon}\t{population}\n")
                kept += 1

    # Renamed into place at the end, so a download interrupted half way
    # leaves no half a gazetteer for the next start to read.
    temporary.replace(target)
    with _cache_lock:
        _cache.pop(str(Path(state_dir)), None)
    log.info("kept %d places", kept)
    return kept


class Gazetteer:
    """The trimmed list, in memory, ready to answer."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        names: list[str] = []
        countries: list[str] = []
        lats: list[float] = []
        lons: list[float] = []
        pops: list[int] = []
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 5:
                    continue
                try:
                    lats.append(float(parts[2]))
                    lons.append(float(parts[3]))
                    pops.append(int(parts[4] or 0))
                except ValueError:
                    continue
                names.append(parts[0])
                countries.append(parts[1])
        self.names = names
        self.countries = countries
        if np is not None:
            self.lat = np.asarray(lats, dtype="float64")
            self.lon = np.asarray(lons, dtype="float64")
            # Weighting on the log of the population, floored at a thousand:
            # the difference between a village and a town should count for
            # much less than the difference between a town and a capital.
            self.weight = 1.0 + SIZE_WEIGHT * np.log10(
                np.maximum(np.asarray(pops, dtype="float64"), 1000.0)) / 10.0
        else:  # pragma: no cover - numpy is a hard requirement in practice
            self.lat = self.lon = self.weight = None

    def __len__(self) -> int:
        return len(self.names)

    def _distances(self, lat: float, lon: float, index) -> Any:
        p1 = math.radians(lat)
        p2 = np.radians(self.lat[index])
        dp = p2 - p1
        dl = np.radians(self.lon[index] - lon)
        a = (np.sin(dp / 2) ** 2
             + math.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2)
        return 2 * _EARTH_KM * np.arcsin(np.minimum(1.0, np.sqrt(a)))

    def nearest(self, lat: float, lon: float,
                max_km: float = MAX_KM) -> dict[str, Any] | None:
        """The place a photograph taken here would be said to be in."""
        if np is None or self.lat is None or not len(self.names):
            return None
        # A box first, so the arithmetic runs over a few hundred candidates
        # rather than a hundred and seventy thousand.
        pad = max(0.2, max_km / 111.0)
        span = pad / max(0.05, math.cos(math.radians(lat)))
        mask = ((np.abs(self.lat - lat) < pad)
                & (np.abs(self.lon - lon) < span))
        index = np.where(mask)[0]
        if not len(index):
            return None

        distances = self._distances(lat, lon, index)
        # The 0.3 km floor stops a place you are standing exactly on from
        # scoring zero and beating every larger neighbour by default.
        score = (distances + 0.3) / self.weight[index]
        best = int(np.argmin(score))
        km = float(distances[best])
        if km > max_km:
            return None
        position = int(index[best])
        return {
            "city": self.names[position],
            "country": self.countries[position] or None,
            "km": round(km, 1),
        }


def gazetteer(state_dir: str | Path) -> Gazetteer | None:
    """The loaded gazetteer for this state directory, or None if not fetched.

    Held per state directory: loading parses five megabytes, and a scan asks
    it once per photograph.
    """
    key = str(Path(state_dir))
    with _cache_lock:
        found = _cache.get(key)
        if found is not None:
            return found
        if not installed(state_dir):
            return None
        try:
            loaded = Gazetteer(data_path(state_dir))
        except Exception as exc:  # noqa: BLE001 - a truncated or odd file
            log.warning("could not read the place-name list: %s", exc)
            return None
        _cache[key] = loaded
        return loaded
