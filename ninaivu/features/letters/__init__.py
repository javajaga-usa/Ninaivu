"""Letters read aloud: an old letter's words, written out from the
photograph of it and read aloud in a Tamil or English voice, on this computer.

``routes.py`` is what was ``features/letters/routes.py``, ``media.py`` what was
``features/letters/media.py`` and ``storage.py`` what was ``features/letters/storage.py``; the
old names are kept as the very same modules.
"""
from .. import ON_THE_FAMILY_API, Feature, Tables

MANIFEST = Feature(
    name="letters", title="Letters read aloud",
    blueprints=ON_THE_FAMILY_API, routes="ninaivu.features.letters.routes",
    tables=Tables("ninaivu.features.letters.storage", "create"),
    settings=("letters_aloud",), switch="letters_aloud",
    hooks={"bin_swept": "ninaivu.features.letters.storage:swept",
           "readings_forgotten": "ninaivu.features.letters.storage:forget_read",
           "reading_aloud": "ninaivu.features.letters.media"},
)
