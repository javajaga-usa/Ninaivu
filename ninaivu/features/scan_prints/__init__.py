"""Scan old prints: a phone's picture of an album page, cut into its prints
and filed in the library.

``routes.py`` is what was ``api/api_scan_prints.py``; the old name is kept as
the very same module. It has no tables and no switch, and shares the print
scanner (``media/print_scan.py``) with Find this print.
"""
from .. import ON_THE_FAMILY_API, Feature

MANIFEST = Feature(
    name="scan_prints", title="Scan old prints",
    blueprints=ON_THE_FAMILY_API, routes="ninaivu.features.scan_prints.routes",
)
