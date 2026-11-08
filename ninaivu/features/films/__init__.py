"""Memory films: a few minutes of video made from the family's own
photographs, clips and voice stories, on this computer.

``routes.py`` is what was ``api/api_films.py``, ``media.py`` what was
``media/films.py``, ``storage.py`` what was ``storage/films.py``, ``music.py``
what was ``media/film_music.py`` and ``storyboard.py`` what was
``media/storyboard.py``; the old names are kept as the very same modules.

Its table has no place in the start: it is made the first time a film is
(``storage.init``), as it was in 3.7, so a fresh index is the same file.
Films are made from books' picks (``..books``) and narrate letters read
aloud (``..letters``) where nobody recorded a story.
"""
from .. import ON_THE_FAMILY_API, Feature

MANIFEST = Feature(
    name="films", title="Memory films",
    blueprints=ON_THE_FAMILY_API, routes="ninaivu.features.films.routes",
    tables=None, settings=("memory_films",), switch="memory_films",
)
