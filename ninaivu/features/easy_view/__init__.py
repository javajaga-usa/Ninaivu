"""Easy view: a calmer, larger family app for a grandparent, per profile,
with each photograph told aloud.

``routes.py`` holds its routes, ``media.py`` the sentences it says about a
photograph and ``storage.py`` which profiles have it.
"""
from .. import ON_THE_FAMILY_API, Feature, Tables

MANIFEST = Feature(
    name="easy_view", title="Easy view",
    blueprints=ON_THE_FAMILY_API, routes="ninaivu.features.easy_view.routes",
    tables=Tables("ninaivu.features.easy_view.storage", "create"),
    settings=("easy_view",), switch="easy_view",
)
