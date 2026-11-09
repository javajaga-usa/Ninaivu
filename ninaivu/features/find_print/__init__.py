"""Find this print: the library photograph a framed print is a copy of.

``routes.py`` is what was ``api/api_find_print.py`` and ``media.py`` what was
``media/find_print.py``; the old names are kept as the very same modules. It
has no tables, and shares the print scanner (``media/print_scan.py``) with
scanning old prints.
"""
from .. import ON_THE_FAMILY_API, Feature

MANIFEST = Feature(
    name="find_print", title="Find this print",
    blueprints=ON_THE_FAMILY_API, routes="ninaivu.features.find_print.routes",
    settings=("find_print",), switch="find_print",
)
