"""Smart albums: an album that is a saved search, filled by its rules.

``routes.py`` is what was ``api/api_smart.py`` and ``storage.py`` what was
``storage/smart.py``; the old names are kept as the very same modules. The
gallery's listings apply a smart album's rules (``routes.apply_rules``) and
photo books are made from them. Always on.
"""
from .. import ON_THE_FAMILY_API, Feature, Tables

MANIFEST = Feature(
    name="smart", title="Smart albums",
    blueprints=ON_THE_FAMILY_API, routes="ninaivu.features.smart.routes",
    tables=Tables("ninaivu.features.smart.storage"),
)
