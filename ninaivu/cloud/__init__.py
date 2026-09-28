"""One-way outbound sync: this library, out to the cloud, and never back.

The direction is the whole design, and it is worth being blunt about what it
rules out. Nothing in here reads the cloud in order to change the library. Not
to reconcile, not to tidy up, not to notice a deletion. Somebody who clears out
their Drive to free space must not, by doing so, empty the photograph library
their household lives in — and a household that trusts Ninaivu with the only
copy of anything deserves that guarantee stated once, plainly, rather than
implied by the absence of code.

That gives the two rules the rest of this package exists to keep:

**A file is uploaded once.** Completion is recorded against the file's own
identity — where it sits and what is in it — and a recorded completion is
permanent. Rescans do not clear it. Reinstalling the cloud connection does not
clear it. A file deleted from Drive afterwards is *still* recorded as uploaded,
so it is not sent again; if somebody wants it back up there, they say so.

**The cloud never reaches in.** The only Drive state this package reads is what
it needs to put a file *up*: whether a folder exists, and whether an interrupted
upload can be resumed. Neither of those can change a row in the library.

The pieces:

``store``
    The durable record of what has gone up. Everything above rests on it.

``drive``
    Google Drive: OAuth against the admin's own client credentials, token
    refresh, and resumable uploads. Speaks HTTP through an injected transport
    so it can be exercised without the internet.

``engine``
    The worker. Picks up what is pending, uploads it one at a time, backs off
    when Google says to, and stops cleanly.

``limits``
    How fast it may go and when it may go at all: a token bucket for the rate
    ceiling and a local-time window for the hours. Kept apart from the engine
    because both are arithmetic, and arithmetic is worth testing without a
    thread.
"""

from __future__ import annotations

from . import drive, engine, limits, store

__all__ = ["store", "drive", "engine", "limits"]
