"""Photo books: a PDF made from the family's own photographs, of an album, an
occasion, a smart album, a person or a year.

``routes.py`` is what was ``api/api_books.py``, ``media.py`` what was
``media/books.py`` and ``storage.py`` what was ``storage/books.py``; the old
names are kept as the very same modules. Always on: a book costs nothing
until somebody asks for one, and it is made on this computer.
"""
from .. import ON_THE_FAMILY_API, Feature, Tables

MANIFEST = Feature(
    name="books", title="Photo books",
    blueprints=ON_THE_FAMILY_API, routes="ninaivu.features.books.routes",
    tables=Tables("ninaivu.features.books.storage"),
    hooks={"carry_on": "ninaivu.features.books.media:carry_on",
           "stand_down": "ninaivu.features.books.media:stand_down",
           "profile_deleted": "ninaivu.features.books.storage:forget_owner"},
    noun="photo book",
)
