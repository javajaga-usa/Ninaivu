"""Our Year: the family's year in photographs, as cards.

``routes.py`` is what was ``api/api_our_year.py`` and ``media.py`` what was
``media/our_year.py``; the old names are kept as the very same modules. It
has no tables.
"""
from .. import ON_THE_FAMILY_API, Feature

MANIFEST = Feature(
    name="our_year", title="Our Year",
    blueprints=ON_THE_FAMILY_API, routes="ninaivu.features.our_year.routes",
    settings=("our_year",), switch="our_year",
)
