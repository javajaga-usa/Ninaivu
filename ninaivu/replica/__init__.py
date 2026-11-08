"""The family's read-only view: serving a hand-over of the library.

The Primary is not always on. With two computers (ninaivu/mugil/) and
``mugil_family_copy`` on, the Primary sends the Secondary a *hand-over*
(mugil/handover.py): a consistent snapshot of the index taken through
SQLite's backup API and slimmed of what must not leave it, the thumbnails,
face crops, profile pictures, stories and films, the settings that decide
what the family sees, and last of all ``replica.json``, which names the
snapshot and its checksum. A reader that finds the folder half-written sees
the previous ``replica.json`` and the previous snapshot, both whole.

This package serves it. Since 4.0 the Secondary's own Ninaivu does, on its
own family app, with each library re-rooted onto its originals folder
(mugil/family_view.py). The old way, still kept: ``ninaivu --replica-of <that folder>``
(:mod:`.receive`) serves the family app from the newest snapshot that checks
out, and takes up a newer one while it runs. It never writes to the library:
the service mounts the originals read-only at the Primary's own library path
(``ninaivu replica unit``), so every path and thumbnail name in the index is
right as it is, and the app itself refuses every change but signing in and
out (:mod:`.front`). While the Primary is awake, pages are sent to it; while
it is off, the copy is served and says so in a line at the top.

Until 4.0 the Primary could also write the hand-over into a shared folder
for a Raspberry Pi to serve. That way (``replica_dir``, ``replica
publish``) is retired; serving is what both used, and stays.
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
