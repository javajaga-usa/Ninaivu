"""\"Who is this?\" links: a face nobody has named, sent to a relative who may
know, answered without an account.

``routes.py`` is what was ``features/ask_family/routes.py`` and ``storage.py`` what
was ``features/ask_family/storage.py``; the old names are kept as the very same
modules. The faces index holds a face back while it is asked about
(``storage/db_faces.py``). Always on.
"""
from .. import ON_THE_FAMILY_API, Feature, Tables

MANIFEST = Feature(
    name="ask_family", title="\"Who is this?\" links",
    blueprints=ON_THE_FAMILY_API, routes="ninaivu.features.ask_family.routes",
    tables=Tables("ninaivu.features.ask_family.storage"),
)
