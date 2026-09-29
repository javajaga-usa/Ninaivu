"""A file name that is safe to write, in whatever script it was written in.

werkzeug's ``secure_filename`` reduces a name to ASCII, which is right for a
web server that knows nothing about its users and wrong for a family library
in Tamil: ``நாள்.jpg`` came out as ``jpg`` — no stem, no extension — and the
upload was refused as "not a photograph", or a phone backup marked
"unsupported". What actually makes a name dangerous is narrower than "not
ASCII": path separators, control characters, a leading dot, the characters
and device names Windows will not have, and sheer length. Those go; letters
in any language stay.
"""

from __future__ import annotations

import re
import unicodedata

#: Characters Windows refuses in a name, plus both path separators. The drive
#: holding the library is often an exFAT or NTFS disk, whatever the server runs.
_FORBIDDEN = set('<>:"/\\|?*')

#: Names Windows reserves for devices, with or without an extension:
#: ``nul.jpg`` cannot be created on NTFS, and on some tools opens the device.
_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$", re.IGNORECASE)

#: Room left under the usual 255-byte limit for the suffix added when a name
#: is taken (``name (2).jpg``) and for multi-byte characters.
MAX_LENGTH = 200


def safe_filename(name: str) -> str:
    """*name* reduced to one safe path component, or "" if nothing is left.

    Keeps letters and marks in any script (NFC-normalised, so the same word
    typed on a Mac and a phone is one name), drops directories, control and
    format characters, what Windows forbids, leading dots (no hidden files) and
    trailing dots and spaces (which Windows silently strips), and shortens the
    stem so the whole stays within :data:`MAX_LENGTH`, keeping the extension.
    """
    if not name:
        return ""
    name = unicodedata.normalize("NFC", str(name))
    # Only the last component: a name that arrives as "../../x.jpg" or
    # "C:\\photos\\x.jpg" is x.jpg.
    name = re.split(r"[/\\]", name)[-1]
    kept = []
    for ch in name:
        if ch in _FORBIDDEN:
            continue
        category = unicodedata.category(ch)
        # Cc control, Cf format (bidi overrides that make "gpj.exe" read as
        # "exe.jpg"), Cs/Co/Cn surrogates, private use, unassigned.
        if category[0] == "C":
            continue
        # Zl/Zp line and paragraph separators, and any other odd space, become
        # an ordinary one.
        kept.append(" " if category[0] == "Z" else ch)
    name = re.sub(r" {2,}", " ", "".join(kept)).strip()
    name = name.lstrip(".").rstrip(". ").strip()
    if not name or _RESERVED.match(name):
        return ""

    if len(name) > MAX_LENGTH:
        stem, dot, ext = name.rpartition(".")
        if dot and stem and 0 < len(ext) <= 16:
            name = stem[:MAX_LENGTH - len(ext) - 1].rstrip(". ") + "." + ext
        else:
            name = name[:MAX_LENGTH].rstrip(". ")
    return name
