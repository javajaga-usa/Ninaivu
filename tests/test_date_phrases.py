"""Reading a date range out of what somebody typed.

Two failure modes matter more than coverage of the grammar. A phrase that is
*not* a date must survive untouched, because a wrong range returns nothing
and reads as a broken library. And a range that *is* applied has to be
visible, because a silent narrowing looks the same as a loss.
"""

from datetime import date

import pytest

from ninaivu.utils import query

# A Wednesday, so weekday arithmetic is worth checking against it.
TODAY = date(2026, 9, 9)


def window(text, **kw):
    return query.parse(text, today=TODAY, **kw)


# ---------------------------------------------------------------------------
# What it recognises
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("phrase, start, end", [
    ("today", "2026-09-09", "2026-09-09"),
    ("yesterday", "2026-09-08", "2026-09-08"),
    ("this week", "2026-09-07", "2026-09-09"),
    ("last week", "2026-08-31", "2026-09-06"),
    ("last month", "2026-08-01", "2026-08-31"),
    ("this year", "2026-01-01", "2026-09-09"),
    ("last year", "2025-01-01", "2025-12-31"),
    ("last 30 days", "2026-08-10", "2026-09-09"),
    ("may 2019", "2019-05-01", "2019-05-31"),
    ("2019-05", "2019-05-01", "2019-05-31"),
    ("2019", "2019-01-01", "2019-12-31"),
])
def test_ranges(phrase, start, end):
    _, found = window(phrase)
    assert (found.date_from, found.date_to) == (start, end)


def test_the_rest_of_the_sentence_is_handed_on():
    """The words that describe the picture must reach the search engine."""
    text, found = window("beach photos last summer")
    assert text == "beach photos"
    assert (found.date_from, found.date_to) == ("2026-06-01", "2026-08-31")


def test_a_season_already_past_means_this_year():
    _, found = window("summer")
    assert found.date_from == "2026-06-01"


def test_a_season_still_ahead_means_last_year():
    """In March, "summer" is the one that ended, not the one coming."""
    _, found = query.parse("summer", today=date(2026, 3, 1))
    assert found.date_from == "2025-06-01"


def test_winter_runs_into_the_following_year():
    _, found = window("winter 2023")
    assert (found.date_from, found.date_to) == ("2023-12-01", "2024-02-29")


def test_seasons_can_belong_to_the_other_hemisphere():
    """June is summer in Atlanta and winter in Adelaide."""
    _, north = window("last summer")
    _, south = window("last summer", southern=True)
    assert north.date_from == "2026-06-01"
    assert south.date_from == "2025-12-01"


def test_a_bare_month_means_the_most_recent_one():
    _, found = window("in june")
    assert found.date_from == "2026-06-01"
    _, later = window("in november")
    assert later.date_from == "2025-11-01", "November has not happened yet"


# ---------------------------------------------------------------------------
# What it leaves alone
# ---------------------------------------------------------------------------

def test_an_ordinary_search_is_untouched():
    assert window("grandma at the lake") == ("grandma at the lake", None)


def test_may_the_verb_is_not_may_the_month():
    """"photos that may be blurry" is a real thing to type."""
    text, found = window("photos that may be blurry")
    assert found is None and text == "photos that may be blurry"


def test_an_empty_query_is_not_a_date():
    assert window("") == ("", None)
    assert window("   ")[1] is None


def test_only_the_first_date_phrase_is_used():
    """Two ranges in one query is a caption, not an intersection."""
    text, found = window("last week 2019")
    assert found is not None
    assert "2019" in text


# ---------------------------------------------------------------------------
# Through the API
# ---------------------------------------------------------------------------

def test_a_date_phrase_narrows_the_search(as_family, scanned):
    _, conn, _ = scanned
    year = conn.execute(
        "SELECT substr(date_key,1,4) y FROM assets WHERE date_key != '' "
        "GROUP BY y ORDER BY COUNT(*) DESC LIMIT 1").fetchone()["y"]

    everything = as_family.get("/api/assets?limit=500").get_json()
    narrowed = as_family.get(f"/api/assets?q={year}&limit=500").get_json()

    assert narrowed["dates"]["from"] == f"{year}-01-01"
    assert narrowed["dates"]["to"] == f"{year}-12-31"
    assert 0 < narrowed["total"] < everything["total"]


def test_the_applied_range_is_reported_back(as_family):
    body = as_family.get("/api/assets?q=last%20week&limit=5").get_json()
    assert body["dates"]["matched"] == "last week"


def test_an_ordinary_query_reports_no_range(as_family):
    body = as_family.get("/api/assets?q=grandma&limit=5").get_json()
    assert body["dates"] is None


def test_an_explicit_range_wins_over_the_sentence(as_family):
    """A date picker is a decision; a sentence is a hint."""
    body = as_family.get(
        "/api/assets?q=last%20week&from=2019-01-01&to=2019-12-31&limit=5"
    ).get_json()
    assert body["dates"] is None


def test_the_feature_can_be_turned_off(app, as_family):
    app.config["MV_CONFIG"].date_phrase_search = False
    try:
        body = as_family.get("/api/assets?q=last%20week&limit=5").get_json()
        assert body["dates"] is None
    finally:
        app.config["MV_CONFIG"].date_phrase_search = True
