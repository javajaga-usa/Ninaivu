"""The gallery's opening queries are read before anybody asks for them.

Every query the family app opens with is fast once the pages it touches are
in the operating system's cache and slow before that — and the cost is the
*size* of the column, not the number of rows. Two were found this way:

* the sidebar counts joined `embeddings` by rowid and read 358 MB of vectors
  to count 183,157 of them, which was twenty-three seconds cold;
* the tag cloud still reads 12 MB of tag JSON, four and a half seconds cold
  against half a second warm.

The first was fixed with an index. The second is the shape of the problem
rather than one instance of it, so start-up reads them once, off the path
somebody is waiting on. Deliberately *not* a cache: a cache has to be
invalidated, and a wrong tag cloud is a worse failure than a slow one.
"""

from __future__ import annotations



from ninaivu import Services
from ninaivu.storage import db


def test_it_reads_the_two_queries_the_first_page_needs(scanned, monkeypatch):
    cfg, _, _ = scanned
    asked: list[str] = []
    real_stats, real_facets = db.library_stats, db.facets
    monkeypatch.setattr(db, "library_stats",
                        lambda *a, **k: (asked.append("stats"),
                                         real_stats(*a, **k))[1])
    monkeypatch.setattr(db, "facets",
                        lambda *a, **k: (asked.append("facets"),
                                         real_facets(*a, **k))[1])

    blank = Services.__new__(Services)
    blank.cfg = cfg
    blank._warm_the_pages_the_gallery_asks_for()

    assert asked == ["stats", "facets"]


def test_a_library_that_is_not_set_up_is_nothing_to_warm(cfg):
    cfg.roots = []
    cfg.active_root = None
    blank = Services.__new__(Services)
    blank.cfg = cfg
    blank._warm_the_pages_the_gallery_asks_for()      # must not raise


def test_a_database_that_will_not_open_is_not_fatal(cfg, monkeypatch):
    """A courtesy that crashed start-up would be worse than a slow page."""
    def broken(*_args, **_kwargs):
        raise RuntimeError("the index is busy")

    monkeypatch.setattr(db, "connect", broken)
    blank = Services.__new__(Services)
    blank.cfg = cfg
    blank._warm_the_pages_the_gallery_asks_for()      # must not raise


def test_it_changes_no_answer(scanned):
    """Warming is reading. Whatever the gallery asks must be what it was."""
    cfg, conn, _ = scanned
    roots = [cfg.active_root]
    before = (db.library_stats(conn, roots), db.facets(conn, roots))

    blank = Services.__new__(Services)
    blank.cfg = cfg
    blank._warm_the_pages_the_gallery_asks_for()

    assert (db.library_stats(conn, roots), db.facets(conn, roots)) == before


def test_it_runs_last_so_it_delays_nothing_that_matters():
    """A courtesy ahead of the scan would be a courtesy nobody asked for."""
    import inspect

    source = inspect.getsource(Services.start)
    warm = source.index("_warm_the_pages_the_gallery_asks_for")
    for earlier in ("_resume_archive", "_scan_the_libraries", "_resume_jobs"):
        assert source.index(earlier) < warm, earlier


def test_the_search_vectors_are_loaded_too_when_search_is_on(scanned, monkeypatch):
    """The first search after a start read 197,015 vectors from the index,
    4.5 s for a search that takes 10 ms once they are held."""
    cfg, _, _ = scanned
    loaded = []
    monkeypatch.setattr(db, "embedding_store",
                        lambda conn: loaded.append(conn) or ([1, 2], b"", 512, {}))
    blank = Services.__new__(Services)
    blank.cfg = cfg

    cfg.ai_enabled = False
    blank._warm_the_pages_the_gallery_asks_for()
    assert loaded == [], "no search by description, nothing to load"

    cfg.ai_enabled = True
    blank._warm_the_pages_the_gallery_asks_for()
    assert len(loaded) == 1


def test_vectors_that_will_not_load_are_not_fatal(scanned, monkeypatch):
    cfg, _, _ = scanned
    cfg.ai_enabled = True

    def broken(conn):
        raise RuntimeError("the index is busy")

    monkeypatch.setattr(db, "embedding_store", broken)
    blank = Services.__new__(Services)
    blank.cfg = cfg
    blank._warm_the_pages_the_gallery_asks_for()      # must not raise
