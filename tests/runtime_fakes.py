"""Stand-in capabilities for the model host's tests (tests/test_ai_runtime.py).

Imported by name inside a spawned host process, so they live in a module of
their own rather than in the test file: the host imports only this.
"""
from __future__ import annotations

import os
import time

from ninaivu.runtime.host import Capability, Unavailable


def _kept(setup):
    return {"loaded_at": time.time(), "models": setup.get("models")}


def _echo(kept, **kwargs):
    return {"pid": os.getpid(), "kwargs": kwargs, "models": kept["models"],
            "loaded_at": kept["loaded_at"]}


def _crash(kept, **kwargs):
    os._exit(3)


def _slow(kept, seconds=30.0):
    time.sleep(seconds)
    return "late"


def _boom(kept, **kwargs):
    raise ValueError("this file is not a sound")


def _missing(setup):
    raise Unavailable("no model for this")


def _bad_model(setup):
    raise RuntimeError("protobuf parsing failed")


def _dies_loading(setup):
    os._exit(11)


def _slow_load(setup):
    time.sleep(3.0)
    return _kept(setup)


ECHO = Capability("echo", 1, _kept, _echo)
BAD_MODEL = Capability("bad_model", 1, _bad_model, _echo)
DIES_LOADING = Capability("dies_loading", 1, _dies_loading, _echo)
CRASH = Capability("crash", 1, _kept, _crash)
SLOW = Capability("slow", 1, _kept, _slow)
BOOM = Capability("boom", 1, _kept, _boom)
MISSING = Capability("missing", 1, _missing, _echo)
SLOW_LOAD = Capability("slow_load", 1, _slow_load, _echo)

ALL = {name: f"runtime_fakes:{name.upper()}" for name in ("echo", "crash", "slow", "boom", "missing",
                                                           "bad_model", "dies_loading",
                                                           "slow_load")}


# The on-device engine's image models, on stand-in ONNX files
# (tests/onnx_fakes.py) in the folder the host is given as its models folder.

def _siglip(setup):
    from pathlib import Path
    from ninaivu.runtime import siglip
    return siglip.Siglip(Path(setup["models"]))


def _florence(setup):
    from pathlib import Path
    from ninaivu.runtime import florence
    return florence.Florence(Path(setup["models"]))


def _search(kept, **kwargs):
    from ninaivu.runtime import siglip
    return siglip.search(kept, **kwargs)


def _describe(kept, **kwargs):
    from ninaivu.runtime import florence
    return florence.describe(kept, **kwargs)


SIGLIP = Capability("search", 1, _siglip, _search)
FLORENCE = Capability("describe", 1, _florence, _describe)
