"""Ninaivu keeps an account of itself.

Sixty places in the codebase call a logger and, until this existed, none of
them reached anywhere: the default configuration drops everything below a
warning and sends the rest to a closed console window. A scan that died
overnight left nothing to read in the morning.
"""

import logging

import pytest

from ninaivu.utils import logs


@pytest.fixture(autouse=True)
def clean_root():
    """Logging is global; put it back exactly as it was found."""
    root = logging.getLogger()
    before = list(root.handlers), root.level
    logs._configured = False
    logs.forget()
    yield
    root.handlers, root.level = before
    logs._configured = False
    logs.forget()


def test_the_file_is_written(tmp_path):
    path = logs.configure(tmp_path, debug=False)
    assert path and path.exists(), "no log file was opened"

    logging.getLogger("ninaivu.test").info("the scan finished")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert "the scan finished" in path.read_text(encoding="utf-8")


def test_warnings_reach_the_console_panel(tmp_path):
    logs.configure(tmp_path)
    logging.getLogger("ninaivu.media.scanner").warning("could not read %s", "a.jpg")

    recent = logs.recent()
    assert recent and recent[0]["what"] == "could not read a.jpg"
    assert recent[0]["level"] == "warning"
    assert recent[0]["where"] == "ninaivu.media.scanner"


def test_ordinary_work_stays_out_of_the_panel(tmp_path):
    """The file takes everything; the screen takes what needs attention."""
    logs.configure(tmp_path)
    logging.getLogger("ninaivu.test").info("indexed 400 files")
    assert logs.recent() == []


def test_other_libraries_chatter_stays_out(tmp_path):
    """A request line per thumbnail, and ExifRead's shrug at every video,
    buried a day's real account and pushed it out of the rotation."""
    path = logs.configure(tmp_path)
    logging.getLogger("werkzeug").info('"GET /api/thumb/1 HTTP/1.1" 200 -')
    logging.getLogger("exifread").warning("File format not recognized.")
    for handler in logging.getLogger().handlers:
        handler.flush()

    assert "GET /api/thumb" not in path.read_text(encoding="utf-8")
    assert logs.recent() == []

    logging.getLogger("werkzeug").error("the server could not bind")
    assert logs.recent()[0]["what"] == "the server could not bind"


def test_newest_first_and_capped(tmp_path):
    logs.configure(tmp_path)
    for n in range(logs._KEEP + 10):
        logging.getLogger("ninaivu.test").warning("problem %d", n)

    recent = logs.recent(limit=5)
    assert len(recent) == 5
    assert recent[0]["what"] == f"problem {logs._KEEP + 9}"
    assert len(logs.recent(limit=999)) == logs._KEEP, "the ring grew without bound"


def test_an_exception_keeps_its_traceback(tmp_path):
    logs.configure(tmp_path)
    try:
        raise ValueError("no such folder")
    except ValueError:
        logging.getLogger("ninaivu.test").exception("the scan stopped")

    item = logs.recent()[0]
    assert item["level"] == "error"
    assert "ValueError" in item.get("detail", "")


def test_configuring_twice_does_not_double_up(tmp_path):
    logs.configure(tmp_path)
    count = len(logging.getLogger().handlers)
    logs.configure(tmp_path)
    assert len(logging.getLogger().handlers) == count


def test_a_log_that_cannot_be_opened_is_not_fatal(tmp_path):
    """A server that will not run because it cannot write its log is worse
    than one with no log."""
    wall = tmp_path / "file"
    wall.write_text("not a directory", encoding="utf-8")
    assert logs.configure(wall / "inside") is None
    logging.getLogger("ninaivu.test").warning("still works")
    assert logs.recent()[0]["what"] == "still works"


def test_a_connection_that_cannot_carry_the_name_is_not_a_problem(tmp_path):
    """zeroconf announces on every connection, and a VPN or a link-local
    address refuses. That filled the console's problems with tracebacks."""
    import errno
    path = logs.configure(tmp_path)
    refused = OSError(errno.EADDRNOTAVAIL, "Can't assign requested address")
    logging.getLogger("zeroconf").warning(
        "Error with socket 20 (('100.115.249.50', 5353))): %s", refused, exc_info=refused)
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert logs.recent() == []
    assert "100.115.249.50" not in path.read_text(encoding="utf-8")

    # Any other trouble with a socket still is.
    broken = OSError(errno.ENETDOWN, "Network is down")
    logging.getLogger("zeroconf").warning("Error with socket 21: %s", broken, exc_info=broken)
    assert "Network is down" in logs.recent()[0]["what"]
