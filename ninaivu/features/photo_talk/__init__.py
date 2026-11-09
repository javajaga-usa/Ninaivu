"""Talk about this photo: questions about a photograph, answered on this
computer by the model that can look.

``routes.py`` is what was ``api/api_photo_talk.py`` and ``media.py`` what was
``media/photo_talk.py``; the old names are kept as the very same modules. It
has no tables; it reads the descriptions of photographs.
"""
from .. import ON_THE_FAMILY_API, Feature

MANIFEST = Feature(
    name="photo_talk", title="Talk about this photo",
    blueprints=ON_THE_FAMILY_API, routes="ninaivu.features.photo_talk.routes",
    settings=("photo_talk",), switch="photo_talk",
    needs="looking",
)
