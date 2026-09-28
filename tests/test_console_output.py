"""The launcher must not die of its own startup banner.

Ninaivu prints its addresses with arrows — ``family → http://…`` — and its help
with em dashes and curly quotes. On Windows, Python only handles those by
itself while stdout is an interactive console. Redirect it to a file or a pipe
and the encoding falls back to the locale's, which is cp1252 on most machines,
and ``→`` has no cp1252 code point at all. The result was a
``UnicodeEncodeError`` raised *after* the ports were bound and before the
address was printed: the server died of its own success message.

The situations that hit it are the ones nobody is watching — ``start.bat >
log.txt``, a service supervisor, Task Scheduler, any wrapper that captures
output — so it was invisible on the machine it was developed on and fatal on
somebody else's.

These reproduce it on every platform rather than only on Windows, by asking the
child for a narrow encoding explicitly. That is the same condition Windows
arrives at on its own, without needing a Windows machine to notice a
regression.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import start                                                    # noqa: E402
from ninaivu import __main__ as ninaivu_main                       # noqa: E402

#: The one that actually broke it. Present in the startup banner, absent from
#: cp1252.
ARROW = "\u2192"
#: In the ``--help`` description, and encodable in cp1252 but not in ASCII —
#: which is why the subprocess tests ask for ASCII rather than cp1252.
EM_DASH = "\u2014"


@pytest.fixture(params=[ninaivu_main.use_utf8_output, start.use_utf8_output],
                ids=["ninaivu", "start.py"])
def retune(request):
    """Both entry points carry their own copy, and both have to work.

    ``start.py`` cannot import from the package — it is what installs the
    package — so the duplication is deliberate and a test that only covered
    one of them would let the other rot.
    """
    return request.param


def cp1252_stream() -> tuple[io.BytesIO, io.TextIOWrapper]:
    raw = io.BytesIO()
    return raw, io.TextIOWrapper(raw, encoding="cp1252")


# --- the character, on the stream that could not take it -------------------

def test_an_arrow_cannot_be_written_to_a_cp1252_stream():
    """The premise, stated so the rest of the file is not testing a guess."""
    _, stream = cp1252_stream()
    with pytest.raises(UnicodeEncodeError):
        stream.write(f"family {ARROW} http://127.0.0.1:5000")
        stream.flush()


def test_retuning_lets_the_banner_through(retune, monkeypatch):
    raw, stream = cp1252_stream()
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)

    retune()

    stream.write(f"family {ARROW} http://127.0.0.1:5000")
    stream.flush()
    assert ARROW in raw.getvalue().decode("utf-8")


def test_the_stream_ends_up_as_utf8(retune, monkeypatch):
    _, stream = cp1252_stream()
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)

    retune()

    assert stream.encoding.lower().replace("-", "").replace("_", "") == "utf8"


def test_a_stream_that_cannot_be_retuned_is_survived(retune, monkeypatch):
    """A captured or wrapped stdout must not turn a fix into a new crash."""

    class Awkward:
        encoding = "cp1252"

        def write(self, text):
            return len(text)

        def flush(self):
            pass

    monkeypatch.setattr(sys, "stdout", Awkward())
    monkeypatch.setattr(sys, "stderr", Awkward())
    retune()                            # no reconfigure attribute at all


# --- end to end, through a real interpreter --------------------------------

def run(args: list[str], encoding: str) -> subprocess.CompletedProcess:
    """The launcher, with its output on a pipe and a narrow encoding asked for.

    ``PYTHONIOENCODING`` is how a test reaches the state Windows reaches by
    itself: it sets the encoding of the child's stdout regardless of platform.
    """
    env = {**os.environ, "PYTHONIOENCODING": encoding,
           "PYTHONPATH": str(ROOT)}
    return subprocess.run([sys.executable, *args], cwd=str(ROOT), env=env,
                          capture_output=True, timeout=180, check=False)


def test_the_help_survives_a_console_that_cannot_take_a_dash():
    """``python -m ninaivu --help`` describes itself with an em dash, so on an
    ASCII stream this is the crash in miniature — and it needs no server, no
    port and no library to reproduce."""
    result = run(["-m", "ninaivu", "--help"], "ascii")

    assert b"UnicodeEncodeError" not in result.stderr, result.stderr.decode(
        "utf-8", "replace")
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert EM_DASH in result.stdout.decode("utf-8", "replace")


def test_the_launcher_help_survives_it_too():
    result = run(["start.py", "--help"], "ascii")
    assert b"UnicodeEncodeError" not in result.stderr, result.stderr.decode(
        "utf-8", "replace")
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")


def test_the_version_still_prints_plainly():
    """Nothing above should have turned the simple path into a surprise."""
    result = run(["-m", "ninaivu", "--version"], "ascii")
    assert result.returncode == 0
    assert b"Ninaivu" in result.stdout


def test_the_child_is_told_which_encoding_to_use():
    """`start.py` hands its own stdout to `python -m ninaivu`, so the child
    inherits the pipe. The interpreter's own errors are written before any of
    Ninaivu's code runs and cannot be fixed from inside it."""
    source = (ROOT / "start.py").read_text(encoding="utf-8")
    assert "PYTHONIOENCODING" in source


def test_an_explicit_encoding_choice_is_left_alone():
    """Somebody who set PYTHONIOENCODING on purpose is not overridden."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(ROOT)}
    result = subprocess.run(
        [sys.executable, "-c",
         "import os, start; "
         "print(os.environ.get('PYTHONIOENCODING'))"],
        cwd=str(ROOT), env=env, capture_output=True, timeout=120, check=False)
    assert result.stdout.strip() == b"utf-8"
