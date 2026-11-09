"""Time capsules: photographs and a few words sealed for somebody, opened on
a day the family chooses.

``routes.py`` is what was ``api/api_capsules.py`` and ``storage.py`` what was
``storage/capsules.py``; the old names are kept as the very same modules.
"""
from .. import ON_THE_FAMILY_API, Feature, Tables

MANIFEST = Feature(
    name="capsules", title="Time capsules",
    blueprints=ON_THE_FAMILY_API, routes="ninaivu.features.capsules.routes",
    tables=Tables("ninaivu.features.capsules.storage"),
    settings=("time_capsules",), switch="time_capsules",
)
