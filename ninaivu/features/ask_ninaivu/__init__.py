"""Ask Ninaivu: a question in plain words, answered from the library.

``routes.py`` is what was ``features/ask_ninaivu/routes.py``; the old name is kept as
the very same module. Reading the question and wording the answer are in
``ninaivu/ask/``, which search shares. It has no tables.
"""
from .. import ON_THE_FAMILY_API, Feature

MANIFEST = Feature(
    name="ask_ninaivu", title="Ask Ninaivu",
    blueprints=ON_THE_FAMILY_API, routes="ninaivu.features.ask_ninaivu.routes",
    settings=("ask_ninaivu",), switch="ask_ninaivu", needs="writing",
)
