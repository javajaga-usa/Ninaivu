"""Two computers, one library: the Primary does the work, the Secondary keeps the originals.

A household with one computer runs everything on it, as Ninaivu always has:
the library, the albums, the models, the backups. Nothing here changes that;
``mugil_role`` is ``single`` and none of this package runs.

A household with two can give each one a part (``mugil_role``). The
**Primary** is the fast one, the Mac mini: it keeps the index, the thumbnails
and the library the family browses, runs the models and does the editing.
The **Secondary** is the one that is always on with the big disk, the Linux
PC with the N300: it keeps every original exactly as it was sent, and changes
none of them. Either can be any operating system Ninaivu runs on, and they
speak to each other over the network as two Ninaivus (HTTP over Tailscale or
the house network), not through a share or SSH, so a Windows computer can be
either one.

They are introduced once (:mod:`.pairing`): the Secondary shows a short code,
the Primary is given it with the Secondary's address, and the two agree on a
key that nobody watching the network learns. Every request between them after
that is signed with it.

Then the Primary sends its originals (:mod:`.courier`) and the Secondary keeps
them (:mod:`.hold`), under the rules of the second copy (storage/mirror.py):

**The bytes are the bytes.** A file is sent as it is on the disk, and kept as
it was sent: EXIF, dates and every other property in it are untouched. The
modified time is set to the Primary's.

**Checked at both ends.** The Primary names each file's fingerprint (BLAKE3,
storage/fingerprints.py) before sending it; the Secondary works it out again
from what arrived, and keeps the file only when the two agree, the size too.
A file that changed while it was being sent is not kept, and is sent again
next time.

**Nothing on the Secondary is overwritten or removed.** A file that has
changed on the Primary is kept beside the older one, which is set aside under
a folder that says when. A file deleted on the Primary stays on the Secondary.

This stage only adds copies. Nothing is ever taken off the Primary here:
lighter display copies and fetching an original back when it is wanted come
later, and only for files the Secondary has confirmed it holds.
"""

from __future__ import annotations

#: What ``mugil_role`` may be.
SINGLE, PRIMARY, SECONDARY = "single", "primary", "secondary"
ROLES = (SINGLE, PRIMARY, SECONDARY)


def role(cfg) -> str:
    """This computer's part: ``single`` unless it was given another."""
    value = str(getattr(cfg, "mugil_role", SINGLE) or SINGLE).lower()
    return value if value in ROLES else SINGLE


def display_name(cfg) -> str:
    """What the other computer calls this one: the house name, or the computer's."""
    import socket                                                  # noqa: PLC0415
    from ..server.config import house_name                         # noqa: PLC0415
    named = house_name(cfg)
    return named if named != "Ninaivu" else (socket.gethostname() or named)


__all__ = ["SINGLE", "PRIMARY", "SECONDARY", "ROLES", "role", "display_name"]
