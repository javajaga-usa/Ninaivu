"""Sent for screening: a family member flags a photograph, and an
administrator decides whether it stays.

``routes.py`` is what was ``api/api_screening.py`` and ``storage.py`` what
was ``storage/screening.py``; the old names are kept as the very same
modules. Its routes are on the family API and, on the console, on the
administrator's beside it. Always on.
"""
from .. import WITH_THE_CONSOLE, Feature, Tables

MANIFEST = Feature(
    name="screening", title="Sent for screening",
    blueprints=WITH_THE_CONSOLE, routes="ninaivu.features.screening.routes",
    tables=Tables("ninaivu.features.screening.storage"),
)
