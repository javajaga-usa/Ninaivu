"""Indexing progress reaches the page while it happens, and cheaply.

The scanner announces itself between batches, which on a slow drive or a
CPU-only model can be minutes apart; the event stream fills the gaps by looking
at the counters itself. The gallery, which cannot use the stream, asks for the
counters alone instead of the whole library status.
"""

import json

import pytest

from ninaivu.api import api as api_module


@pytest.fixture()
def scanner(app):
    return app.config["MV_SCANNER"]


def events(response):
    """Each chunk of the stream, as the parsed payload or the keep-alive."""
    for chunk in response.response:
        text = chunk.decode() if isinstance(chunk, bytes) else chunk
        if text.startswith("data: "):
            yield json.loads(text[len("data: "):])
        else:
            yield text.strip()


def test_the_stream_sends_counters_that_moved_between_batches(
        as_admin, scanner, monkeypatch):
    monkeypatch.setattr(api_module, "PROGRESS_TICK", 0.01)
    scanner.progress.update(status="indexing", total=100, processed=1,
                            started_at=1.0, ended_at=0.0)
    response = as_admin.get("/api/events", buffered=False)
    stream = events(response)
    try:
        assert next(stream)["processed"] == 1
        # No notification: this is the scan thread between two batches.
        scanner.progress.update(processed=40, folder="2019/Cornwall")
        moved = next(stream)
        assert moved["processed"] == 40
        assert moved["folder"] == "2019/Cornwall"
    finally:
        response.close()


def test_the_stream_does_not_repeat_counters_that_did_not_move(
        as_admin, scanner, monkeypatch):
    """The clock ticking is not news; `elapsed` changes every call.

    What arrives second is a keep-alive *or* a phase notification: a scan
    thread that is still winding down announces itself through the same
    stream, and that is a real thing happening rather than a counter being
    repeated. The first version of this asserted the second chunk was the
    keep-alive exactly, which made it a test of "nothing else is going on" —
    true most of the time, and a failure in a full suite run where a previous
    test's scan is still finishing.

    So this reads a few chunks and asserts the thing it is named after: the
    same counters are never sent twice.
    """
    monkeypatch.setattr(api_module, "PROGRESS_TICK", 0.01)
    scanner.progress.update(status="indexing", total=100, processed=7,
                            started_at=1.0, ended_at=0.0)
    response = as_admin.get("/api/events", buffered=False)
    stream = events(response)
    try:
        first = next(stream)
        assert first["processed"] == 7

        seen_again = False
        quiet = False
        for _ in range(4):
            chunk = next(stream)
            if isinstance(chunk, str):
                quiet = True                      # the keep-alive
                continue
            if "phase" in chunk and "processed" not in chunk:
                continue                          # a scan announcing itself
            if _moved(chunk, first) is False:
                seen_again = True
        assert not seen_again, "the same counters were sent twice"
        assert quiet, "the stream never went quiet"
    finally:
        response.close()


def _moved(now: dict, before: dict) -> bool:
    """The server's own rule, so the test cannot drift away from it."""
    return api_module._moved(now, before)


def test_the_stream_says_when_a_scan_ends(as_admin, scanner, monkeypatch):
    monkeypatch.setattr(api_module, "PROGRESS_TICK", 0.01)
    scanner.progress.update(status="indexing", total=10, processed=9,
                            started_at=1.0, ended_at=0.0)
    response = as_admin.get("/api/events", buffered=False)
    stream = events(response)
    try:
        assert next(stream)["running"] is True
        scanner.progress.update(status="done", processed=10, ended_at=2.0)
        assert next(stream)["running"] is False
    finally:
        response.close()


def test_the_counters_alone_for_an_admin(as_admin, scanner):
    scanner.progress.update(status="tagging", tagged=3, tag_total=12,
                            started_at=1.0, ended_at=0.0)
    body = as_admin.get("/api/status/scan").get_json()
    assert body["status"] == "tagging"
    assert body["tagged"] == 3
    assert body["running"] is True
    assert "stats" not in body


@pytest.mark.parametrize("who", ["as_family", "as_guest", "anon"])
def test_nobody_else_learns_what_the_library_is_doing(who, request, scanner):
    """The same rule /api/status applies to its `scan` block."""
    scanner.progress.update(status="indexing", total=10, processed=4,
                            root="/secret/place", started_at=1.0, ended_at=0.0)
    response = request.getfixturevalue(who).get("/api/status/scan")
    if response.status_code != 200:
        return
    body = response.get_json()
    assert body == {"status": "idle", "running": False, "percent": 100}
