"""The handover plan: what the family needs to keep the photographs if the
person who looks after this computer cannot any more.

``routes.py`` is what was ``features/handover/routes.py`` and ``storage.py`` what was
``features/handover/storage.py``; the old names are kept as the very same modules. Its
routes are on the family API and, on the console, on the administrator's
beside it. (Mugil's handover to a second computer, ``mugil/handover.py``, is
a different thing and stays with Mugil.) Always on.
"""
from .. import WITH_THE_CONSOLE, Feature, Tables

MANIFEST = Feature(
    name="handover", title="The handover plan",
    blueprints=WITH_THE_CONSOLE, routes="ninaivu.features.handover.routes",
    tables=Tables("ninaivu.features.handover.storage"),
)
