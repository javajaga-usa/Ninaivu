"""The Overview says first when nothing copies the library anywhere: no
second copy, no Secondary, no off-site copy, no Drive copy. On one computer
that is every photograph on one disk."""
from __future__ import annotations

from ninaivu import copies


def _item(client):
    answer = client.get("/api/admin/attention").get_json()
    return next(i for i in answer["items"] if i["key"] == "one_place"), answer


def test_a_library_with_no_copy_anywhere_says_so_first(as_admin, scanned):
    item, answer = _item(as_admin)
    assert item["count"] == 1 and item["page"] == "cloud"
    assert answer["items"][0]["key"] == "one_place", "before everything else that is waiting"
    assert "one place only" in item["title"]
    assert answer["total"] >= 1


def test_any_copy_switched_on_ends_it(as_admin, scanned, monkeypatch):
    class On:
        def status(self):
            return {"on": True}

    class Off:
        def status(self):
            return {"on": False}
    monkeypatch.setattr(copies, "destinations", lambda services: [Off(), On()])
    assert _item(as_admin)[0]["count"] == 0
    monkeypatch.setattr(copies, "destinations", lambda services: [Off(), Off()])
    assert _item(as_admin)[0]["count"] == 1


def test_an_empty_library_has_nothing_to_lose(as_admin, scanned):
    _cfg, conn, _ = scanned
    conn.execute("UPDATE assets SET trashed=1")
    conn.commit()
    assert _item(as_admin)[0]["count"] == 0


def test_it_never_breaks_the_overview(as_admin, scanned, monkeypatch):
    def broken(services):
        raise RuntimeError("an engine that will not say")
    monkeypatch.setattr(copies, "destinations", broken)
    item, answer = _item(as_admin)
    assert item["count"] == 0 and len(answer["items"]) > 3
