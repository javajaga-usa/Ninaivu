"""Voice stories: somebody's own voice telling what a photograph was, written
out on this computer so search finds its words.

``routes.py`` is what was ``api/api_stories.py`` and ``storage.py`` what was
``storage/stories.py``; the old names are kept as the very same modules. The
core reaches the stories by their old name: search reads their words
(``storage/story_words.py``), the bin keeps them with their photograph and
the transcriber (``runtime/transcriber.py``) writes them out. Always on.
"""
from .. import ON_THE_FAMILY_API, Feature, Tables

MANIFEST = Feature(
    name="stories", title="Voice stories",
    blueprints=ON_THE_FAMILY_API, routes="ninaivu.features.stories.routes",
    tables=Tables("ninaivu.features.stories.storage"),
)
