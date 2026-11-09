"""Speaking to search: a question spoken into the search box, written out on
this computer.

``routes.py`` is what was ``api/api_voice_search.py``; the old name is kept
as the very same module. The model stays in ``runtime/voice.py`` with the
other models. It has no tables.
"""
from .. import ON_THE_FAMILY_API, Feature

MANIFEST = Feature(
    name="voice_search", title="Speaking to search",
    blueprints=ON_THE_FAMILY_API, routes="ninaivu.features.voice_search.routes",
    settings=("voice_search",), switch="voice_search",
    needs="speech",
)
