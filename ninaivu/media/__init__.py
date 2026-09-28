"""Media processing and indexing.

Exports:
    - Scanner: Detect and index media files
    - Straightener: Image alignment
    - And other media processing modules

Import specific modules as needed:
    from ninaivu.media import scanner
    from ninaivu.media.straighten import Straightener
"""

import os

# OpenCV bundles its own FFmpeg, and that library writes straight to stderr —
# past Python, so no amount of `capture_output` or `except` catches it. A
# damaged or half-copied video makes it print things like
#
#     [mov,mp4,m4a,3gp,3g2,mj2 @ ...] moov atom not found
#
# into whatever window Ninaivu was started from, with no filename attached and
# no way to act on it. Quietening the backend and logging the failure
# ourselves — with the path — turns unattributable noise into something a
# person can actually look up. It has to be set before cv2 first loads, which
# is why it lives here: every module in this package that imports cv2 is
# imported through it.
os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "-8")   # AV_LOG_QUIET

__all__ = [
    "scanner",
    "media",
    "faces",
    "portrait",
    "straighten",
    "upright",
    "stills",
    "faceindex",
    "facematch",
    "orientnet",
    "gemini_media",
]
