"""Colour for black-and-white photographs: a coloured copy made beside the
original on this computer, which is never changed.

``routes.py`` is what was ``api/api_colour.py`` and ``storage.py`` what was
``storage/colour_copies.py``; the old names are kept as the very same
modules. The model itself stays in ``runtime/colour.py`` with the other
models, and the gallery's own listings read the copies' table
(``api/api.py``).
"""
from .. import ON_THE_FAMILY_API, Feature, Tables

MANIFEST = Feature(
    name="colour_copies", title="Colour for black-and-white photographs",
    blueprints=ON_THE_FAMILY_API, routes="ninaivu.features.colour_copies.routes",
    tables=Tables("ninaivu.features.colour_copies.storage", "create"),
    settings=("colour_photos",), switch="colour_photos", needs="colour",
)
