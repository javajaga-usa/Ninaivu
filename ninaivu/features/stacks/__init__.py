"""Photo stacks: a burst, or a photograph and its copies, shown as one tile
with the best of them on top.

``routes.py`` is what was ``api/api_stacks.py`` and ``storage.py`` what was
``storage/stacks.py``; the old names are kept as the very same modules. The
timeline asks this feature whether to fold its stacks (``api/api.py``
``_stacks_apply``).
"""
from .. import ON_THE_FAMILY_API, Feature, Tables

MANIFEST = Feature(
    name="stacks", title="Photo stacks",
    blueprints=ON_THE_FAMILY_API, routes="ninaivu.features.stacks.routes",
    tables=Tables("ninaivu.features.stacks.storage", "create"),
    settings=("photo_stacks",), switch="photo_stacks",
)
