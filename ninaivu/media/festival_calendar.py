"""The Tamil calendar's festivals, worked out from the sky.

Deepavali, Pongal, Karthigai Deepam and the rest do not fall on the same
Gregorian day each year: Pongal is the day the sun enters Capricorn as the
Tamil calendar measures it (against the stars, not the seasons), Deepavali
the fourteenth day of the waning moon in the lunar month that ends in
Aippasi, Karthigai Deepam the day the moon stands in the Krittika stars
during the solar month of Karthigai. So the dates are worked out here from
where the sun and the moon were, for any year from the first photograph to
now, the way a panchangam does: no table to keep up to date, nothing looked
up anywhere.

The positions are Jean Meeus's (Astronomical Algorithms, 2nd ed., chapters
25 and 47): the sun to about a hundredth of a degree and the moon to a few
hundredths, which puts the start of a lunar day (tithi) within a few minutes
of a printed panchangam. The stars are counted with the Lahiri ayanamsa, the
one Indian calendars are made with. Days are Indian Standard Time; sunrise
is taken as 06:00 and sunset as 18:00, which is near enough in Tamil Nadu.

A festival on the very edge of a rule (a tithi that starts at sunrise, a
sankranti at sunset) can come out a day from a family's own calendar. That
is why an album takes a few days around each date (media/festivals.py), and
why the picture search, not the date, decides which photographs belong.

Pure functions of a year; nothing is stored.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache

#: Indian Standard Time, which the festival days are counted in.
IST = timezone(timedelta(hours=5, minutes=30))

_J2000 = 2451545.0
_UNIX_JD = 2440587.5


def _jd(moment: datetime) -> float:
    return moment.timestamp() / 86400.0 + _UNIX_JD


def _norm(deg: float) -> float:
    return deg % 360.0


def _sin(deg: float) -> float:
    return math.sin(math.radians(deg))


def sun_longitude(jd: float) -> float:
    """The sun's apparent longitude, in degrees (Meeus ch. 25, low accuracy)."""
    t = (jd - _J2000) / 36525.0
    l0 = 280.46646 + 36000.76983 * t + 0.0003032 * t * t
    m = 357.52911 + 35999.05029 * t - 0.0001537 * t * t
    c = ((1.914602 - 0.004817 * t - 0.000014 * t * t) * _sin(m)
         + (0.019993 - 0.000101 * t) * _sin(2 * m) + 0.000289 * _sin(3 * m))
    omega = 125.04 - 1934.136 * t
    return _norm(l0 + c - 0.00569 - 0.00478 * _sin(omega))


# The periodic terms of the moon's longitude (Meeus table 47.A): multiples of
# D, M, M', F and the coefficient in millionths of a degree.
_MOON_TERMS = (
    (0, 0, 1, 0, 6288774), (2, 0, -1, 0, 1274027), (2, 0, 0, 0, 658314),
    (0, 0, 2, 0, 213618), (0, 1, 0, 0, -185116), (0, 0, 0, 2, -114332),
    (2, 0, -2, 0, 58793), (2, -1, -1, 0, 57066), (2, 0, 1, 0, 53322),
    (2, -1, 0, 0, 45758), (0, 1, -1, 0, -40923), (1, 0, 0, 0, -34720),
    (0, 1, 1, 0, -30383), (2, 0, 0, -2, 15327), (0, 0, 1, 2, -12528),
    (0, 0, 1, -2, 10980), (4, 0, -1, 0, 10675), (0, 0, 3, 0, 10034),
    (4, 0, -2, 0, 8548), (2, 1, -1, 0, -7888), (2, 1, 0, 0, -6766),
    (1, 0, -1, 0, -5163), (1, 1, 0, 0, 4987), (2, -1, 1, 0, 4036),
    (2, 0, 2, 0, 3994), (4, 0, 0, 0, 3861), (2, 0, -3, 0, 3665),
    (0, 1, -2, 0, -2689), (2, 0, -1, 2, -2602), (2, -1, -2, 0, 2390),
    (1, 0, 1, 0, -2348), (2, -2, 0, 0, 2236), (0, 1, 2, 0, -2120),
    (0, 2, 0, 0, -2069), (2, -2, -1, 0, 2048), (2, 0, 1, -2, -1773),
    (2, 0, 0, 2, -1595), (4, -1, -1, 0, 1215), (0, 0, 2, 2, -1110),
    (3, 0, -1, 0, -892), (2, 1, 1, 0, -810), (4, -1, -2, 0, 759),
    (0, 2, -1, 0, -713), (2, 2, -1, 0, -700), (2, 1, -2, 0, 691),
    (2, -1, 0, -2, 596), (4, 0, 1, 0, 549), (0, 0, 4, 0, 537),
    (4, -1, 0, 0, 520), (1, 0, -2, 0, -487),
)


def moon_longitude(jd: float) -> float:
    """The moon's longitude, in degrees (Meeus ch. 47), with the same
    nutation the sun's apparent longitude carries."""
    t = (jd - _J2000) / 36525.0
    lp = 218.3164477 + 481267.88123421 * t - 0.0015786 * t * t + t ** 3 / 538841.0
    d = 297.8501921 + 445267.1114034 * t - 0.0018819 * t * t + t ** 3 / 545868.0
    m = 357.5291092 + 35999.0502909 * t - 0.0001536 * t * t
    mp = 134.9633964 + 477198.8675055 * t + 0.0087414 * t * t + t ** 3 / 69699.0
    f = 93.2720950 + 483202.0175233 * t - 0.0036539 * t * t
    a1 = 119.75 + 131.849 * t
    a2 = 53.09 + 479264.290 * t
    e = 1.0 - 0.002516 * t - 0.0000074 * t * t
    total = 0.0
    for kd, km, kmp, kf, coeff in _MOON_TERMS:
        term = coeff * _sin(kd * d + km * m + kmp * mp + kf * f)
        if km:
            term *= e ** abs(km)
        total += term
    total += 3958 * _sin(a1) + 1962 * _sin(lp - f) + 318 * _sin(a2)
    omega = 125.04452 - 1934.136261 * t
    nutation = -0.00478 * _sin(omega)
    return _norm(lp + total / 1e6 + nutation)


def ayanamsa(jd: float) -> float:
    """How far the Tamil zodiac stands from the seasons' (Lahiri), degrees."""
    return 23.85675 + 50.2877 / 3600.0 * (jd - _J2000) / 365.25


def sidereal_sun(jd: float) -> float:
    return _norm(sun_longitude(jd) - ayanamsa(jd))


def sidereal_moon(jd: float) -> float:
    return _norm(moon_longitude(jd) - ayanamsa(jd))


def elongation(jd: float) -> float:
    """How far the moon has run ahead of the sun: 0 at new moon, 180 full."""
    return _norm(moon_longitude(jd) - sun_longitude(jd))


def tithi(jd: float) -> int:
    """The lunar day, 1 to 30: 1-15 the waxing half (15 the full moon),
    16-30 the waning (30 the new moon, amavasai)."""
    return int(elongation(jd) // 12.0) + 1


def _at(day: date, hour: float) -> float:
    """The Julian day of *hour* o'clock IST on *day*."""
    whole = datetime(day.year, day.month, day.day, tzinfo=IST)
    return _jd(whole) + hour / 24.0


def _when(jd: float) -> datetime:
    return datetime.fromtimestamp((jd - _UNIX_JD) * 86400.0, tz=IST)


def _solve(fn, target: float, lo: float, hi: float) -> float:
    """When the angle *fn* passes *target* between *lo* and *hi* (Julian days),
    to about a minute; *fn* must rise through it once in that span."""
    def off(jd: float) -> float:
        return (fn(jd) - target + 180.0) % 360.0 - 180.0
    for _ in range(40):
        mid = (lo + hi) / 2.0
        if off(mid) < 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def sankranti(year: int, sign_deg: float) -> float:
    """When the sun enters the sidereal sign starting at *sign_deg* in *year*
    (Julian day). The sun moves about a degree a day; sidereal Aries begins
    around 14 April."""
    guess = _at(date(year, 4, 14), 0) + (sign_deg % 360.0) / 360.0 * 365.2564
    if guess >= _at(date(year + 1, 1, 1), 0):
        guess -= 365.2564
    return _solve(sidereal_sun, sign_deg % 360.0, guess - 12, guess + 12)


def _tamil_day(jd: float) -> date:
    """The day a solar month starts by the Tamil rule: the day of the
    sankranti if before sunset, otherwise the next."""
    moment = _when(jd)
    day = moment.date()
    return day if moment.hour < 18 else day + timedelta(days=1)


def new_moon_after(jd: float) -> float:
    """The first new moon after *jd*."""
    step = jd
    prev = elongation(step)
    for _ in range(40):
        nxt = elongation(step + 1.0)
        if nxt < prev:                      # wrapped through 360: a new moon
            return _solve(elongation, 0.0, step, step + 1.0)
        prev = nxt
        step += 1.0
    raise RuntimeError(jd)                  # cannot happen: a lunation is 29.5 days


def _lunation(year: int, sign_deg: float) -> tuple[float, float]:
    """The new moons around the lunar month in which the sun enters the
    sidereal sign at *sign_deg* (amanta: from new moon to new moon). With
    Tula (180) that is Ashvina, the month of Navaratri that ends on
    Deepavali's new moon."""
    entry = sankranti(year, sign_deg)
    end = new_moon_after(entry)
    start = new_moon_after(end - 32.0)
    if start >= end - 1:                    # the search above found the end itself
        start = new_moon_after(end - 34.0)
    return start, end


def _days(first: float, last: float):
    day = _when(first).date()
    while _at(day, 0) <= last + 1:
        yield day
        day += timedelta(days=1)


def _first_day(start: float, end: float, wanted: int, hour: float) -> date | None:
    """The first day of the lunation whose *hour* o'clock falls in tithi
    *wanted*, or failing that (a tithi that spans no such hour) the day the
    next one holds then."""
    for target in (wanted, wanted + 1):
        for day in _days(start, end):
            jd = _at(day, hour)
            if start <= jd <= end and tithi(jd) == target:
                return day
    return None


def pongal(year: int) -> date:
    """Thai Pongal: the first day of Thai, the sun into sidereal Capricorn."""
    return _tamil_day(sankranti(year, 270.0))


def puthandu(year: int) -> date:
    """Tamil New Year: the first day of Chithirai, the sun into sidereal Aries."""
    return _tamil_day(sankranti(year, 0.0))


def deepavali(year: int) -> date:
    """Naraka Chaturdashi, the day Tamil Nadu keeps Deepavali: the waning
    fourteenth (tithi 29) at dawn, in the month whose new moon follows the
    sun's entry into Tula."""
    start, end = _lunation(year, 180.0)
    found = _first_day(start, end, 29, 6.0)
    return found - timedelta(days=1) if found and tithi(_at(found, 6.0)) == 30 else found


def navaratri(year: int) -> tuple[date, date]:
    """Navaratri, from the first waxing day of Ashvina to Vijayadashami (the
    tenth, as held in the afternoon)."""
    start, end = _lunation(year, 180.0)
    return _first_day(start, end, 1, 6.0), _first_day(start, end, 10, 15.5)


def vinayagar_chathurthi(year: int) -> date:
    """The waxing fourth of Bhadrapada (the lunar month in which the sun
    enters Kanya), as held at noon."""
    start, end = _lunation(year, 150.0)
    return _first_day(start, end, 4, 12.0)


def krishna_jayanthi(year: int) -> date:
    """Gokulashtami: the waning eighth of the month in which the sun enters
    Simha (amanta Shravana), as it stands at night."""
    start, end = _lunation(year, 120.0)
    return _first_day(start, end, 23, 23.5)


#: Krittika, the Pleiades: the third of the 27 nakshatras.
_KRITTIKA = (26.0 + 40.0 / 60.0, 40.0)


def karthigai_deepam(year: int) -> date:
    """The day the moon stands in Krittika at lamp-lighting (18:00) in the
    solar month of Karthigai. When it does so twice, the later: in 2024 it
    passed Krittika on the month's first evening, at the full moon that
    closed Aippasi, and the lamps were lit at the next, on 13 December."""
    first = _tamil_day(sankranti(year, 210.0))
    last = _tamil_day(sankranti(year, 240.0)) - timedelta(days=1)
    found: list[date] = []
    best: tuple[float, date] | None = None
    day = first
    while day <= last:
        moon = sidereal_moon(_at(day, 18.0))
        if _KRITTIKA[0] <= moon < _KRITTIKA[1]:
            found.append(day)
        gap = abs((moon - 33.333 + 180.0) % 360.0 - 180.0)
        if best is None or gap < best[0]:
            best = (gap, day)
        day += timedelta(days=1)
    return found[-1] if found else best[1]      # else Krittika fell between two evenings


@lru_cache(maxsize=4096)
def days(key: str, year: int) -> tuple[date, date, date]:
    """(the festival's own day, the album's first day, its last day) for the
    festival *key* in *year*; see ``FESTIVALS`` for which keys there are."""
    one = timedelta(days=1)
    if key == "pongal":
        d = pongal(year)
        return d, d - one, d + 2 * one                 # Bhogi to Kaanum Pongal
    if key == "puthandu":
        d = puthandu(year)
        return d, d, d
    if key == "vinayagar":
        d = vinayagar_chathurthi(year)
        return d, d - one, d + one
    if key == "navaratri":
        first, tenth = navaratri(year)
        return tenth - one, first, tenth            # Saraswati Puja, golu to Vijayadashami
    if key == "deepavali":
        d = deepavali(year)
        return d, d - one, d + one
    if key == "karthigai":
        d = karthigai_deepam(year)
        return d, d - one, d + one
    if key == "krishna":
        d = krishna_jayanthi(year)
        return d, d, d + one
    if key == "christmas":
        d = date(year, 12, 25)
        return d, d - one, d
    raise KeyError(key)


#: The festivals, in the order of the year.
FESTIVALS = ("pongal", "puthandu", "krishna", "vinayagar", "navaratri", "deepavali",
             "karthigai", "christmas")
