"""Letters read aloud: an old letter's words, written out from the
photograph of it and read aloud in a Tamil or English voice, on this computer.

``routes.py`` is what was ``api/api_letters.py``, ``media.py`` what was
``media/letters.py`` and ``storage.py`` what was ``storage/letters.py``; the
old names are kept as the very same modules.
"""
from .. import ON_THE_FAMILY_API, Feature, Tables

MANIFEST = Feature(
    name="letters", title="Letters read aloud",
    blueprints=ON_THE_FAMILY_API, routes="ninaivu.features.letters.routes",
    tables=Tables("ninaivu.features.letters.storage", "create"),
    settings=("letters_aloud",), switch="letters_aloud",
)
