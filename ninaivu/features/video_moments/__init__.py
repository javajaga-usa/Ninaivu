"""Moments inside videos: search finds the second inside a long clip, from
pictures of it looked at on this computer.

``routes.py`` is what was ``features/video_moments/routes.py``, ``media.py`` what was
``features/video_moments/media.py`` and ``storage.py`` what was
``features/video_moments/storage.py``; the old names are kept as the very same
modules. It needs nothing beyond the picture search the gallery already has.
"""
from .. import ON_THE_FAMILY_API, Feature, Tables

MANIFEST = Feature(
    name="video_moments", title="Moments inside videos",
    blueprints=ON_THE_FAMILY_API, routes="ninaivu.features.video_moments.routes",
    tables=Tables("ninaivu.features.video_moments.storage", "create"),
    settings=("video_moments",), switch="video_moments", needs="",
    hooks={"readings_forgotten": "ninaivu.features.video_moments.storage:forget"},
)
