"""A life in photographs: one person's life told decade by decade, from what
the library already knows about them.

``routes.py`` is what was ``api/api_life_story.py``, ``media.py`` what was
``media/life_story.py`` and ``storage.py`` what was ``storage/life_story.py``;
the old names are kept as the very same modules.
"""
from .. import ON_THE_FAMILY_API, Feature, Tables

MANIFEST = Feature(
    name="life_story", title="A life in photographs",
    blueprints=ON_THE_FAMILY_API, routes="ninaivu.features.life_story.routes",
    tables=Tables("ninaivu.features.life_story.storage", "create"),
    settings=("life_story",), switch="life_story",
)
