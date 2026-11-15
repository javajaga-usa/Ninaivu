"""Event albums: trips and festivals found from dates and places, offered to
an administrator, who makes each one an album or turns it down.

``media.py`` finds them, ``storage.py`` keeps each decision and
``routes.py`` answers the family app's administrator.
"""
from .. import ON_THE_FAMILY_API, Feature, Tables

MANIFEST = Feature(
    name="event_albums", title="Event albums",
    blueprints=ON_THE_FAMILY_API, routes="ninaivu.features.event_albums.routes",
    tables=Tables("ninaivu.features.event_albums.storage", "create"),
    settings=("event_albums",), switch="event_albums",
)
