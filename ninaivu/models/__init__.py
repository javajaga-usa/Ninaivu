"""Model plugins: each model feature in a package of its own (P16).

A package here holds what one model feature needs and nothing the core does:
its model's file names, the arithmetic around it, and the capability the
model host loads (``ninaivu/runtime/host.py``). The core never imports one at
its top level (docs/development/model-plugins.md), and with a package deleted
or its model missing the feature turns itself off.

Moved so far: ``colour``. Faces and picture search follow, one at a time.
"""
