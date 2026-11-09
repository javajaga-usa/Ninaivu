"""The family tree: who is whose parent, child and partner, among the people
the library has named.

``routes.py`` is what was ``api/api_family_tree.py`` and ``storage.py`` what
was ``storage/family_tree.py``; the old names are kept as the very same
modules. A life in photographs, then and now and young faces read it, and
the faces index carries it over when two people are merged. Always on.
"""
from .. import ON_THE_FAMILY_API, Feature, Tables

MANIFEST = Feature(
    name="family_tree", title="The family tree",
    blueprints=ON_THE_FAMILY_API, routes="ninaivu.features.family_tree.routes",
    tables=Tables("ninaivu.features.family_tree.storage"),
)
