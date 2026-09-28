"""Turning what somebody typed into a date filter.

People do not search a photo library by date range. They type "beach photos
last summer", or "diwali 2019", or just "last week", and expect the words to
do two jobs at once: narrow the dates, and describe the picture. This reads
the first job out of the sentence and hands the rest on untouched, so the
existing full-text and CLIP search still see "beach photos" — the part they
are good at.

Three rules keep it from being annoying:

* It only removes a phrase it is sure about. An unrecognised word is left in
  the query rather than guessed at, because a wrong date filter returns
  nothing and looks like a broken library.
* It never overrides a range the caller set explicitly. A date picker is a
  decision; a sentence is a hint.
* It says what it matched, so the interface can show the range it applied.
  A search that silently narrows to three days is indistinguishable from a
  library that lost everything else.

No model, no dependency, no network — this is a regular expression and a
calendar.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta

MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

#: Meteorological seasons, northern hemisphere: the first month of each and
#: how many months it runs. Winter starts in December and ends in the
#: February of the following year, which is what "winter 2023" means when
#: somebody says it out loud.
_SEASONS = {
    "spring": 3, "summer": 6, "autumn": 9, "fall": 9, "winter": 12,
}
_SEASON_LENGTH = 3


@dataclass(frozen=True)
class Window:
    """A closed date range and the words it came from."""

    start: date
    end: date
    label: str

    @property
    def date_from(self) -> str:
        return self.start.isoformat()

    @property
    def date_to(self) -> str:
        return self.end.isoformat()


def _month_window(year: int, month: int) -> tuple[date, date]:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def _span(year: int, month: int, months: int) -> tuple[date, date]:
    """A run of whole months starting at (year, month), inclusive."""
    start, _ = _month_window(year, month)
    end_year, end_month = year, month + months - 1
    while end_month > 12:
        end_month -= 12
        end_year += 1
    return start, _month_window(end_year, end_month)[1]


def _season_window(name: str, year: int, southern: bool) -> tuple[date, date]:
    month = _SEASONS[name]
    if southern:
        month = (month + 5) % 12 + 1
    return _span(year, month, _SEASON_LENGTH)


def _most_recent_season(name: str, today: date, southern: bool) -> tuple[date, date]:
    """The season of that name that has already begun.

    "summer" in November means the one that just ended, not the one nine
    months away — nobody searches forward through their own photographs.
    """
    start, end = _season_window(name, today.year, southern)
    if start > today:
        start, end = _season_window(name, today.year - 1, southern)
    return start, end


def _most_recent_month(month: int, today: date) -> tuple[date, date]:
    year = today.year if month <= today.month else today.year - 1
    return _month_window(year, month)


def _rule_patterns(southern: bool):
    """Each rule: a pattern, and what the match means as a date range.

    Ordered most specific first — "last summer 2019" must not be read as
    "last summer" with a stray number left behind.
    """
    season = "|".join(_SEASONS)

    def today_only(m, today):
        return today, today

    def yesterday(m, today):
        d = today - timedelta(days=1)
        return d, d

    def this_week(m, today):
        start = today - timedelta(days=today.weekday())
        return start, today

    def last_week(m, today):
        start = today - timedelta(days=today.weekday() + 7)
        return start, start + timedelta(days=6)

    def this_month(m, today):
        return _month_window(today.year, today.month)[0], today

    def last_month(m, today):
        year, month = (today.year, today.month - 1) if today.month > 1 \
            else (today.year - 1, 12)
        return _month_window(year, month)

    def this_year(m, today):
        return date(today.year, 1, 1), today

    def last_year(m, today):
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)

    def last_n(m, today):
        count = int(m.group("count"))
        unit = m.group("unit").rstrip("s")
        days = {"day": 1, "week": 7, "month": 30, "year": 365}[unit] * count
        return today - timedelta(days=days), today

    def season_of_year(m, today):
        return _season_window(m.group("season").lower(),
                              int(m.group("year")), southern)

    def bare_season(m, today):
        return _most_recent_season(m.group("season").lower(), today, southern)

    def month_of_year(m, today):
        return _month_window(int(m.group("year")), MONTHS[m.group("month").lower()])

    def iso_month(m, today):
        return _month_window(int(m.group("year")), int(m.group("month")))

    def bare_year(m, today):
        year = int(m.group("year"))
        return date(year, 1, 1), date(year, 12, 31)

    def bare_month(m, today):
        return _most_recent_month(MONTHS[m.group("month").lower()], today)

    months = "|".join(sorted(MONTHS, key=len, reverse=True))
    # "may" is a month and also an ordinary English verb, and "photos that
    # may be blurry" is a real thing to type. On its own it is left alone;
    # written as "in May", or with a year, it is unambiguous enough to use.
    unambiguous = "|".join(sorted((m for m in MONTHS if m != "may"),
                                  key=len, reverse=True))
    return [
        (rf"\b(?:last|this)\s+(?P<season>{season})\s+(?P<year>(?:19|20)\d{{2}})\b",
         season_of_year),
        (rf"\b(?P<season>{season})\s+(?:of\s+)?(?P<year>(?:19|20)\d{{2}})\b",
         season_of_year),
        (rf"\b(?:last|this)\s+(?P<season>{season})\b", bare_season),
        (rf"\b(?P<month>{months})\s+(?P<year>(?:19|20)\d{{2}})\b", month_of_year),
        (r"\b(?P<year>(?:19|20)\d{2})-(?P<month>0[1-9]|1[0-2])\b", iso_month),
        (r"\blast\s+(?P<count>\d+)\s+(?P<unit>days?|weeks?|months?|years?)\b",
         last_n),
        (r"\byesterday\b", yesterday),
        (r"\btoday\b", today_only),
        (r"\bthis\s+week\b", this_week),
        (r"\blast\s+week\b", last_week),
        (r"\bthis\s+month\b", this_month),
        (r"\blast\s+month\b", last_month),
        (r"\bthis\s+year\b", this_year),
        (r"\blast\s+year\b", last_year),
        (rf"\b(?P<season>{season})\b", bare_season),
        (rf"\bin\s+(?P<month>{months})\b", bare_month),
        (rf"\b(?P<month>{unambiguous})\b", bare_month),
        (r"\b(?:in\s+)?(?P<year>(?:19|20)\d{2})\b", bare_year),
    ]


def parse(text: str, *, today: date | None = None,
          southern: bool = False) -> tuple[str, Window | None]:
    """Pull a date range out of a search phrase.

    Returns the phrase with the date words removed, and the range they meant
    — or the phrase unchanged and ``None`` when nothing was recognised.

    Only the first match wins. Two date phrases in one query almost always
    means somebody typed a caption, not a range, and intersecting them would
    produce an empty result from a search that looked reasonable.
    """
    if not text or not text.strip():
        return text, None

    today = today or date.today()
    for pattern, resolve in _rule_patterns(southern):
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        start, end = resolve(match, today)
        remaining = (text[:match.start()] + " " + text[match.end():])
        return re.sub(r"\s+", " ", remaining).strip(), Window(
            start=start, end=end, label=match.group(0).strip())

    return text, None
