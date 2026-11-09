"""Then and now: the same place, the same person, a parent and their child at
the same age, years apart.

``routes.py`` is what was ``api/api_then_and_now.py``, ``media.py`` what was
``media/then_and_now.py`` and ``storage.py`` what was
``storage/then_and_now.py``; the old names are kept as the very same modules.
A pair is turned into a page of a photo book (``..books``) or a memory film
(``..films``), and the years of birth it keeps are read by a life in
photographs and by young faces.
"""
from .. import ON_THE_FAMILY_API, Feature, Tables

MANIFEST = Feature(
    name="then_and_now", title="Then and now",
    blueprints=ON_THE_FAMILY_API, routes="ninaivu.features.then_and_now.routes",
    tables=Tables("ninaivu.features.then_and_now.storage", "create"),
    settings=("then_and_now",), switch="then_and_now",
)
