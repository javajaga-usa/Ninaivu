""""Who is this?" links moved to ninaivu/features/ask_family/routes.py in 4.0 (core and
feature modules, proposal 1).

This name is kept, and it is the very same module object, not a copy of its
names: everything that imports it from here, and every test that patches
something on it, reaches the one module that does the work.
"""
import sys

from ..features.ask_family import routes as _moved

sys.modules[__name__] = _moved
