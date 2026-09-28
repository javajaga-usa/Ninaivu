"""Which photographs count as the same shot, and what a scan writes about it.

The engine groups near-identical pictures by the distance between their
perceptual hashes. Two things decide whether that grouping is any good: which
pairs it bothers to compare at all, and whether a chain of pairs ends up as one
group or several. Both used to be wrong in ways a library only shows at size,
which is why they are pinned down here with hashes rather than photographs.
"""

from ninaivu.media.scanner import dup_bands, group_duplicates


def hashes(*values: int) -> list[str]:
    return [f"{value:016x}" for value in values]


# ---------------------------------------------------------------------------
# Which pairs get compared
# ---------------------------------------------------------------------------

def test_a_pair_that_differs_at_the_front_of_the_hash_is_still_found():
    """Bucketing by a hash prefix never compared these two.

    Three bits apart is the same photograph by any measure, but both of the
    bits that differ are in the leading hex characters — so a bucket keyed on
    those characters put the pair in two different buckets and no comparison
    ever happened. At the default distance of six bits that was most pairs.
    """
    a, b = hashes(0x0000_0000_0000_0000, 0xE000_0000_0000_0000)
    groups = group_duplicates([(1, a), (2, b)], threshold=6)
    assert groups[1] == groups[2]


def test_pictures_further_apart_than_the_threshold_are_left_alone():
    a, b = hashes(0x0000_0000_0000_0000, 0xFFFF_0000_0000_0000)
    assert group_duplicates([(1, a), (2, b)], threshold=6) == {}


def test_identical_hashes_group_with_no_distance_allowed_at_all():
    """The same file twice needs no threshold, and a library is full of them."""
    same, = hashes(0x1234_5678_9ABC_DEF0)
    groups = group_duplicates([(1, same), (2, same)], threshold=0)
    assert groups[1] == groups[2]


def test_a_picture_with_no_twin_is_in_no_group():
    a, b = hashes(0x0000_0000_0000_0000, 0xFFFF_FFFF_FFFF_FFFF)
    assert group_duplicates([(1, a), (2, b)], threshold=6) == {}


def test_a_hash_this_scanner_did_not_write_stands_alone():
    """Whatever it is, it must not throw and must not join anything."""
    a, = hashes(0x0000_0000_0000_0000)
    assert group_duplicates([(1, a), (2, "not a hash")], threshold=6) == {}


# ---------------------------------------------------------------------------
# Whether a chain ends up as one group
# ---------------------------------------------------------------------------

def test_a_chain_of_near_duplicates_is_one_group():
    """The ends are further apart than the threshold, and still belong together.

    A burst of shots links up a frame at a time. Grouping them pair by pair
    without folding the findings together left the run split in two.
    """
    a, b, c = hashes(0x0, 0x3F, 0xFFF)          # 6 bits, then 6 more
    groups = group_duplicates([(1, a), (2, b), (3, c)], threshold=6)
    assert groups[1] == groups[2] == groups[3]


def test_two_pairs_found_apart_and_then_joined_end_up_in_one_group():
    """The shape that used to split: both halves already had a group.

    ``a-b`` and ``c-d`` are found as separate pairs; ``b-d`` then says the two
    halves are the same photograph. Keeping only the first of the two group
    names left whichever half was named first pointing somewhere else.
    """
    a, b, c, d = hashes(0x0000, 0x000F, 0x00F0, 0x00FF)
    groups = group_duplicates(
        [(1, a), (2, b), (3, c), (4, d)], threshold=6)
    assert len({groups[1], groups[2], groups[3], groups[4]}) == 1


def test_the_group_key_does_not_depend_on_the_order_files_were_read():
    """Otherwise every scan rewrites every group under a new name."""
    a, b, c = hashes(0x0, 0x3F, 0xFFF)
    forwards = group_duplicates([(1, a), (2, b), (3, c)], threshold=6)
    backwards = group_duplicates([(3, c), (2, b), (1, a)], threshold=6)
    assert forwards == backwards


# ---------------------------------------------------------------------------
# The bands the candidate search is cut into
# ---------------------------------------------------------------------------

def test_a_small_library_gets_enough_bands_to_miss_nothing():
    bands = dup_bands(threshold=6, count=100)
    assert len(bands) == 7                       # threshold + 1, by pigeonhole
    shifts = [shift for shift, _ in bands]
    assert shifts == sorted(shifts) and len(set(shifts)) == 7


def test_the_bands_never_overlap():
    for count in (10, 10_000, 5_000_000):
        covered: set[int] = set()
        for shift, mask in dup_bands(threshold=6, count=count):
            bits = {shift + n for n in range(mask.bit_length())}
            assert not bits & covered
            covered |= bits
            assert max(bits) < 64


def test_a_large_library_widens_the_bands_rather_than_comparing_everything():
    """Narrow bands mean few keys, and few keys mean enormous buckets.

    The guarantee is worth nothing if honouring it turns one pass over a
    million pictures into an afternoon, so the bands widen — fewer of them,
    each holding far fewer candidates.
    """
    small = dup_bands(threshold=6, count=100)
    large = dup_bands(threshold=6, count=2_000_000)
    assert len(large) < len(small)
    assert large[0][1] > small[0][1]             # a wider mask, so more keys


# ---------------------------------------------------------------------------
# What reaches the database
# ---------------------------------------------------------------------------

def updates_during(conn, work) -> int:
    """How many statements *work* ran against ``assets``."""
    seen = []
    conn.set_trace_callback(lambda sql: seen.append(sql))
    try:
        work()
    finally:
        conn.set_trace_callback(None)
    return len([sql for sql in seen if "UPDATE assets" in sql])


def test_a_rescan_that_finds_the_same_duplicates_writes_nothing(scanned):
    """It used to clear and rewrite the group of every picture in the library.

    Each of those writes also rebuilt that row's entry in the search index,
    through the trigger on ``assets`` — a full rewrite of the index on every
    scan of a library where nothing had changed.
    """
    cfg, conn, scanner = scanned
    root = cfg.active_root
    scanner._link_duplicates(conn, root)        # settle the groups

    before = conn.execute(
        "SELECT id, dup_group FROM assets ORDER BY id").fetchall()
    written = updates_during(conn, lambda: scanner._link_duplicates(conn, root))
    after = conn.execute(
        "SELECT id, dup_group FROM assets ORDER BY id").fetchall()

    assert written == 0
    assert [tuple(r) for r in before] == [tuple(r) for r in after]


def test_a_picture_whose_only_twin_went_away_is_in_no_group():
    a, b = hashes(0x0, 0x3)
    assert group_duplicates([(1, a), (2, b)], threshold=6)[1]
    assert group_duplicates([(1, a)], threshold=6) == {}


def test_the_survivor_of_a_deleted_pair_is_written_back_as_no_duplicate(scanned):
    """The group has to be cleared, not merely left out of the new grouping."""
    cfg, conn, scanner = scanned
    root = cfg.active_root
    pair = [int(row["id"]) for row in
            conn.execute("SELECT id FROM assets ORDER BY id LIMIT 2")]
    # Hashes this test controls, so it is about the grouping rather than about
    # how alike the fixture's flat-colour images happen to be.
    conn.execute("UPDATE assets SET phash=NULL")
    conn.executemany("UPDATE assets SET phash='d629292829c6c629' WHERE id=?",
                     [(asset_id,) for asset_id in pair])
    conn.commit()

    scanner._link_duplicates(conn, root)
    grouped = conn.execute(
        "SELECT id, dup_group FROM assets WHERE id IN (?, ?)", pair).fetchall()
    assert all(row["dup_group"] for row in grouped)

    conn.execute("DELETE FROM assets WHERE id=?", (pair[0],))
    conn.commit()
    scanner._link_duplicates(conn, root)

    left = conn.execute("SELECT dup_group FROM assets WHERE id=?",
                        (pair[1],)).fetchone()
    assert left["dup_group"] is None
