"""Young faces, older names: who an unnamed child in an old photograph may be.

``routes.py`` is what was ``features/young_faces/routes.py``; the old name is kept as
the very same module. It has no tables: it reads the family tree and the years
of birth then and now keeps.
"""
from .. import ON_THE_FAMILY_API, Feature

MANIFEST = Feature(
    name="young_faces", title="Young faces, older names",
    blueprints=ON_THE_FAMILY_API, routes="ninaivu.features.young_faces.routes",
    settings=("young_faces",), switch="young_faces",
)
