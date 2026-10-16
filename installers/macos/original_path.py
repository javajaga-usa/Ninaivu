"""Where a translocated app really is: Ninaivu.app/Contents/Resources/original_path.py.

macOS runs a downloaded app that is still quarantined from a hidden, read-only
copy (App Translocation) until it is told the folder is wanted, and from there
the portable app cannot see the folder its data lives in. The Security
framework knows where the copy came from; this asks it, with ctypes and the
app's own Python, and prints the original path — or nothing, when it cannot be
told (the launcher then says how to clear the quarantine by hand).

    python3 original_path.py /private/var/folders/…/AppTranslocation/…/d/Ninaivu.app
"""
from __future__ import annotations

import ctypes
import sys

SECURITY = "/System/Library/Frameworks/Security.framework/Security"
CORE_FOUNDATION = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"


def original_path(path: str) -> str | None:
    try:
        security = ctypes.CDLL(SECURITY)
        cf = ctypes.CDLL(CORE_FOUNDATION)
        create_url = cf.CFURLCreateFromFileSystemRepresentation
        create_url.restype = ctypes.c_void_p
        create_url.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_bool]
        to_path = cf.CFURLGetFileSystemRepresentation
        to_path.restype = ctypes.c_bool
        to_path.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_char_p, ctypes.c_long]
        release = cf.CFRelease
        release.argtypes = [ctypes.c_void_p]
        original = security.SecTranslocateCreateOriginalPathForURL
        original.restype = ctypes.c_void_p
        original.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    except (OSError, AttributeError):
        return None
    raw = path.encode("utf-8")
    url = create_url(None, raw, len(raw), True)
    if not url:
        return None
    try:
        found = original(url, None)
        if not found:
            return None
        try:
            buffer = ctypes.create_string_buffer(4096)
            if not to_path(found, True, buffer, len(buffer)):
                return None
            return buffer.value.decode("utf-8")
        finally:
            release(found)
    finally:
        release(url)


if __name__ == "__main__":
    answer = original_path(sys.argv[1]) if len(sys.argv) > 1 else None
    if answer:
        print(answer)
