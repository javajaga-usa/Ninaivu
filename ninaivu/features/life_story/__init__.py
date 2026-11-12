"""A life in photographs: one person's life told decade by decade, from what
the library already knows about them.

``routes.py`` is what was ``features/life_story/routes.py``, ``media.py`` what was
``features/life_story/media.py`` and ``storage.py`` what was ``features/life_story/storage.py``;
the old names are kept as the very same modules.
"""
from .. import ON_THE_FAMILY_API, Feature, Tables

MANIFEST = Feature(
    name="life_story", title="A life in photographs",
    blueprints=ON_THE_FAMILY_API, routes="ninaivu.features.life_story.routes",
    tables=Tables("ninaivu.features.life_story.storage", "create"),
    settings=("life_story",), switch="life_story",
)
