"""The living photo frame: a tablet or a television's browser, signed in,
drifting through the library full screen.

``routes.py`` is what was ``api/api_frame.py`` and ``media.py`` what was
``media/frame.py``; the old names are kept as the very same modules. It has
no tables. Our Year and a life in photographs borrow its excerpt of a story
(``media._excerpt``).
"""
from .. import ON_THE_FAMILY_API, Feature

MANIFEST = Feature(
    name="frame", title="The living photo frame",
    blueprints=ON_THE_FAMILY_API, routes="ninaivu.features.frame.routes",
    settings=("photo_frame",), switch="photo_frame",
)
