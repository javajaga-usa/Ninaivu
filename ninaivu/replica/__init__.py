"""The Pi as the always-on front: a read-only replica of the library.

The Mac mini runs Ninaivu and is not always on. When it sleeps the family app
goes with it, although every photograph is sitting on the Pi in the mirror it
keeps (``/srv/ninaivu/current``). This package lets the Pi answer for it:

**On the Mac** (:mod:`.publish`), a hand-over is written to a folder the Pi
can read — its Samba share, or a folder the nightly rsync carries. It holds a
consistent snapshot of the index taken through SQLite's backup API, the
thumbnails, face crops, profile pictures and voice stories, the settings that
decide what the family sees, and last of all ``replica.json``, which names the
snapshot and its checksum. A Pi that reads the folder half-written sees the
previous ``replica.json`` and the previous snapshot, both whole.

**On the Pi** (:mod:`.receive`), ``ninaivu --replica-of <that folder>`` serves
the family app from the newest snapshot that checks out, and takes up a newer
one while it runs. It never writes to the library: the Pi's systemd unit
mounts the mirror read-only at the Mac's own library path (so every path and
every thumbnail name in the index is right as it is), and the app itself
refuses every change but signing in and out (:mod:`.front`). No scan, no
analysis, no backup, no console: the Mac does all of that, and the Pi shows
what the Mac last knew.

**One address for the family** (:mod:`.front`). The family bookmarks the Pi.
While the mini is awake, the Pi sends each page they open to the mini, so
they use the real thing with everything it can do; while it sleeps, the Pi
serves the copy and says so in a line at the top. Changing something there
can ask the mini to wake up (Wake-on-LAN), when the household gave its
network address.

What this stage leaves for later: the index streamed continuously instead of
handed over in snapshots, search by description on the Pi (it needs the
smaller model the AI runtime brings), and phone backups landing on the Pi for
the mini to take in when it wakes.
"""

from __future__ import annotations

#: The hand-over's own description, written last. A folder without it is not
#: a hand-over yet.
MANIFEST = "replica.json"

#: What a hand-over says it is, so a Pi can refuse one it does not understand.
FORMAT = 1

#: The folders beside the index the family app reads from, copied as they are.
FOLDERS = ("thumbs", "faces", "avatars", "stories")

__all__ = ["MANIFEST", "FORMAT", "FOLDERS"]
