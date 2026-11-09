"""Festival albums: every Deepavali, every Pongal, found from the calendar.

``routes.py`` is what was ``api/api_festivals.py`` and ``media.py`` what was
``media/festivals.py``; the old names are kept as the very same modules. The
calendar itself (``media/festival_calendar.py``) stays with the core, which
reads it on start. It has no tables; photo books are made from its albums.
"""
from .. import ON_THE_FAMILY_API, Feature

MANIFEST = Feature(
    name="festivals", title="Festival albums",
    blueprints=ON_THE_FAMILY_API, routes="ninaivu.features.festivals.routes",
    settings=("festival_albums",), switch="festival_albums",
)
