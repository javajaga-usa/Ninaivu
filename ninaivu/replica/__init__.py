"""The family's read-only view: serving a hand-over of the library.

The Primary is not always on. With two computers (ninaivu/mugil/) and
``mugil_family_copy`` on, the Primary sends the Secondary a *hand-over*
(mugil/handover.py): a consistent snapshot of the index taken through
SQLite's backup API and slimmed of what must not leave it, the thumbnails,
face crops, profile pictures, stories and films, the settings that decide
what the family sees, and last of all ``replica.json``, which names the
snapshot and its checksum. A reader that finds the folder half-written sees
the previous ``replica.json`` and the previous snapshot, both whole.

This package serves it, on the Secondary's own family app, with each
library re-rooted onto its originals folder (mugil/family_view.py):
:mod:`.receive` takes up the newest snapshot that checks out, and a newer one
while it runs, and :mod:`.front` keeps the app read-only, refusing every
change but signing in and out. While the Primary is awake, pages are sent to
it; while it is off, the copy is served and says so in a line at the top.

Until 4.2 a second way was kept: a stand-alone read-only Ninaivu started with
``--replica-of`` (and ``ninaivu replica unit`` to write its service), and
before 4.0 a Raspberry Pi served a hand-over the Primary wrote into a shared
folder (``replica_dir``). Both are gone; serving is what they used, and stays.
"""

from __future__ import annotations

#: The hand-over's own description, written last. A folder without it is not
#: a hand-over yet.
MANIFEST = "replica.json"

#: What a hand-over says it is, so a Pi can refuse one it does not understand.
FORMAT = 1

#: The folders beside the index the family app reads from, copied as they are.
#: Films too: listed on the Pi as done, they gave 404 when played, the photo
#: frame's "Play our films" included (audit C-06). Each is copied once.
FOLDERS = ("thumbs", "faces", "avatars", "stories", "films")

__all__ = ["MANIFEST", "FORMAT", "FOLDERS"]
