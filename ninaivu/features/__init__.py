"""Core and feature modules (4.0, proposal 1).

Ninaivu's core is what every household gets and what the promises are about:
the library, roles, faces, search, places, albums, sharing, import and the
copies. Everything else — photo books, memory films, letters read aloud, the
family tree, time capsules and the rest of the storytelling — is a *feature*.
Until 4.0 a feature was spread over four places that knew nothing of each
other: its routes in ``api/``, its tables in ``storage/db.py`` ``init_db``,
its switch in ``Config``, and whether it was on in ``server/features.py``.
Nothing said "this is photo books", so nothing could leave photo books out.

A feature now says what it is in one :class:`Feature`, its manifest:

``name``, ``title``
    Its short name, and what a person calls it.
``blueprints``
    Per face (``home``, ``admin``), the blueprints its routes are on. Every
    feature so far puts its routes on the family API's own blueprint
    (``api``), and a few administrator-only ones on ``api_admin``; a feature
    with a blueprint of its own names it here.
``routes``
    The module whose import puts those routes on them.
``tables``
    What makes its tables, run on every start whether the feature is on or
    off: a household that switches a feature off must not lose what it made,
    and Go back to 3.7 reads these tables.
``settings``
    The settings it owns (they are still declared in ``server/config.py``,
    so ``docs/settings.md`` and the settings pages do not move).
``switch``
    The one of those that turns it on, or None for a feature that is always on.
``needs``
    The model it needs before it is offered (``server/features.py`` NEEDS).
``tier``
    The smallest computer it runs on (``server/tiers.py``). Every feature
    here runs on Basic: the core's second promise is that the small machine
    keeps everything.

:data:`ORDER` is the loader's order, and it is the order ``init_db`` made
the tables in before 4.0, so a fresh index is the same file it was.

In this pass every feature's routes are still registered at every start, in
the very place they were before (the foot of ``api/api.py``), and a feature
that is switched off still answers 404 from inside its routes. Leaving a
switched-off feature's routes out altogether is the next step; the route
manifest (``tests/data/route_manifest.txt``) would then show it.

Three features have moved whole into a package of their own here
(``books``, ``films``, ``letters``), each with ``routes.py``, ``media.py``
and ``storage.py`` for what was ``api/api_<name>.py``, ``media/<name>.py``
and ``storage/<name>.py``. Their old module names are kept as the very same
modules (see ``api/api_books.py``), so nothing that imports them changed.
The others are described here and have not moved yet.

An extension (``ninaivu/extensions.py``) can be described by the same shape
(:func:`from_extension`), so the console can one day list features and
extensions alike. Extensions themselves are unchanged.
"""
from __future__ import annotations

import importlib
import inspect
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from ..server.tiers import TIERS

#: The two apps a route can be on (``ninaivu/__init__.py`` FACE_HOME and
#: FACE_ADMIN).
FACES = ("home", "admin")

#: The family API's blueprint, on both apps.
ON_THE_FAMILY_API: Mapping[str, tuple[str, ...]] = {"home": ("api",), "admin": ("api",)}
#: The family API's, and on the console the administrator's beside it.
WITH_THE_CONSOLE: Mapping[str, tuple[str, ...]] = {"home": ("api",), "admin": ("api", "api_admin")}


@dataclass(frozen=True)
class Tables:
    """What makes a feature's tables: its module's ``SCHEMA`` script, or a
    function in it that is given the connection (``create``).

    Named rather than imported, so describing a feature costs nothing until
    an index is opened.
    """

    module: str
    function: str | None = None

    def __call__(self, conn: sqlite3.Connection) -> None:
        found = importlib.import_module(self.module)
        if self.function is None:
            conn.executescript(found.SCHEMA)
        else:
            getattr(found, self.function)(conn)

    def shape(self) -> list[str]:
        """What these tables are made from, for ``storage/upgrade.py``
        :func:`index_shape`: every ``…SCHEMA`` script the module has and the
        function's source. A change to either is a change to the index."""
        found = importlib.import_module(self.module)
        parts = [f"{self.module}:{self.function or 'SCHEMA'}"]
        parts += [value for name, value in sorted(vars(found).items())
                  if name.endswith("SCHEMA") and isinstance(value, str)]
        if self.function is not None:
            code = getattr(found, self.function)
            try:
                parts.append(inspect.getsource(code).replace("\r\n", "\n"))
            except (OSError, TypeError):
                parts.append(code.__qualname__)          # shipped without its source
        return parts


@dataclass(frozen=True)
class Feature:
    """A feature's manifest: everything the core needs to know about it."""

    name: str
    title: str
    blueprints: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    routes: str | None = None
    tables: Callable[[sqlite3.Connection], None] | None = None
    settings: tuple[str, ...] = ()
    switch: str | None = None
    needs: str | None = None
    tier: str = "basic"
    #: ``feature``, or ``extension`` for one described by :func:`from_extension`.
    kind: str = "feature"
    #: True only for an extension that can send something off this computer.
    #: No feature may: that is the core's first promise.
    data_leaves_the_machine: bool = False

    def on(self, cfg: Any) -> bool:
        """Whether it is on for *cfg*: its switch, and the model it needs
        (``server/features.py``). An extension is on when it is in
        ``Config.extensions``."""
        if self.kind == "extension":
            from .. import extensions                             # noqa: PLC0415
            return self.name in extensions.enabled_names(cfg)
        if self.switch is None:
            return True
        from ..server import features as switches                 # noqa: PLC0415
        return switches.on(cfg, self.switch)

    def problems(self) -> list[str]:
        """What is wrong with this manifest, in words; empty when nothing is."""
        found = []
        if not self.name or not self.name.isidentifier():
            found.append(f"name {self.name!r} is not a short name")
        if not self.title:
            found.append("it has no title")
        for face in self.blueprints:
            if face not in FACES:
                found.append(f"face {face!r} is not one of {FACES}")
        if bool(self.routes) != bool(self.blueprints) and self.kind == "feature":
            found.append("routes and blueprints must be named together")
        if self.switch is not None and self.switch not in self.settings:
            found.append(f"its switch {self.switch!r} is not one of its settings")
        if self.tier not in TIERS:
            found.append(f"tier {self.tier!r} is not one of {TIERS}")
        if self.kind not in ("feature", "extension"):
            found.append(f"kind {self.kind!r} is not feature or extension")
        if self.kind == "feature" and self.data_leaves_the_machine:
            found.append("a feature may not send anything off this computer; that is an extension")
        return found

    def describe(self) -> dict[str, Any]:
        """The manifest as plain data, for a page or a log."""
        return {
            "name": self.name, "title": self.title, "kind": self.kind,
            "blueprints": {face: list(names) for face, names in self.blueprints.items()},
            "routes": self.routes, "tables": self.tables is not None,
            "settings": list(self.settings), "switch": self.switch, "needs": self.needs,
            "tier": self.tier, "data_leaves_the_machine": self.data_leaves_the_machine,
        }


def _here(name: str, title: str, *, routes: str | None, tables: Tables | None,
          blueprints: Mapping[str, tuple[str, ...]] = ON_THE_FAMILY_API,
          switch: str | None = None, needs: str | None = None) -> Feature:
    """A feature that has not moved into this package yet."""
    return Feature(name=name, title=title, blueprints=blueprints if routes else {},
                   routes=routes, tables=tables, settings=(switch,) if switch else (),
                   switch=switch, needs=needs)


def _manifests() -> tuple[Feature, ...]:
    from . import books, capsules, colour_copies, films, frame, letters  # noqa: PLC0415
    from . import life_story, stacks, then_and_now, video_moments  # noqa: PLC0415
    return (
        _here("smart", "Smart albums", routes="ninaivu.api.api_smart",
              tables=Tables("ninaivu.storage.smart")),
        _here("ask_family", "\"Who is this?\" links", routes="ninaivu.api.api_ask_family",
              tables=Tables("ninaivu.storage.ask_family")),
        _here("family_tree", "The family tree", routes="ninaivu.api.api_family_tree",
              tables=Tables("ninaivu.storage.family_tree")),
        books.MANIFEST,
        films.MANIFEST,
        _here("handover", "The handover plan", routes="ninaivu.api.api_handover",
              tables=Tables("ninaivu.storage.handover"), blueprints=WITH_THE_CONSOLE),
        _here("stories", "Voice stories", routes="ninaivu.api.api_stories",
              tables=Tables("ninaivu.storage.stories")),
        capsules.MANIFEST,
        frame.MANIFEST,
        _here("screening", "Sent for screening", routes="ninaivu.api.api_screening",
              tables=Tables("ninaivu.storage.screening"), blueprints=WITH_THE_CONSOLE),
        _here("descriptions", "Descriptions of photographs", routes=None,
              tables=Tables("ninaivu.storage.descriptions", "create"), switch="describe_photos"),
        colour_copies.MANIFEST,
        video_moments.MANIFEST,
        then_and_now.MANIFEST,
        life_story.MANIFEST,
        letters.MANIFEST,
        stacks.MANIFEST,
        _here("journal", "The change journal", routes=None,
              tables=Tables("ninaivu.storage.journal", "create"), switch="change_journal"),
    )


_registry: tuple[Feature, ...] | None = None
#: The features whose routes are on their blueprints, in the order they were put there.
_routed: list[str] = []


def registry() -> tuple[Feature, ...]:
    """Every feature, in :data:`ORDER`."""
    global _registry
    if _registry is None:
        found = {feature.name: feature for feature in _manifests()}
        if set(found) != set(ORDER):
            raise RuntimeError(f"features and ORDER differ: {sorted(set(found) ^ set(ORDER))}")
        _registry = tuple(found[name] for name in ORDER)
    return _registry


def names() -> tuple[str, ...]:
    return tuple(feature.name for feature in registry())


def get(name: str) -> Feature:
    """The feature called *name*; KeyError for one there is not."""
    for feature in registry():
        if feature.name == name:
            return feature
    raise KeyError(name)


def create_tables(conn: sqlite3.Connection) -> list[str]:
    """Make every feature's tables, in order, on or off (``storage/db.py``
    ``init_db``). Returns the names of the features that have tables."""
    made = []
    for feature in registry():
        if feature.tables is not None:
            feature.tables(conn)
            made.append(feature.name)
    return made


def register_routes(name: str) -> Feature:
    """Put feature *name*'s routes on its blueprints, by importing its routes
    module. Called from the foot of ``api/api.py``, in the place each was
    imported before 4.0, so the URL map is the same one, rule for rule.

    The blueprints are registered on each app afterwards, as before; a
    feature's routes are on every app its blueprints are on.
    """
    feature = get(name)
    if feature.routes:
        importlib.import_module(feature.routes)
    if name not in _routed:
        _routed.append(name)
    return feature


def routed() -> list[str]:
    """The features whose routes are registered, in the order they were."""
    return list(_routed)


def endpoints(app: Any, feature: Feature) -> list[str]:
    """The endpoints of *app* that *feature*'s routes module made."""
    if not feature.routes:
        return []
    return sorted(endpoint for endpoint, view in app.view_functions.items()
                  if getattr(view, "__module__", None) == feature.routes)


def shape() -> list[Any]:
    """What the features' tables are made from, in order, for
    ``storage/upgrade.py`` :func:`index_shape`."""
    return [(feature.name, feature.tables.shape() if isinstance(feature.tables, Tables)
             else repr(feature.tables)) for feature in registry() if feature.tables is not None]


def listing(cfg: Any) -> list[dict[str, Any]]:
    """Every feature, and whether it is on for *cfg*."""
    return [{**feature.describe(), "on": feature.on(cfg)} for feature in registry()]


def from_extension(ext: Any) -> Feature:
    """An extension (``ninaivu/extensions.py`` :class:`Extension`) in a
    feature's shape. An extension registers its own blueprints when it is
    on (``register(app, face)``) and says nothing of them beforehand unless
    its package has ``BLUEPRINTS``; its tables are its own business unless it
    has ``create_tables``. It is off until named in ``Config.extensions``, and
    one that does not say which computer it needs is taken to need the Full
    one, since it lives outside the core's promise."""
    module = getattr(ext, "module", None)
    blueprints = getattr(module, "BLUEPRINTS", None) or {}
    return Feature(
        name=str(ext.name), title=str(ext.title or ext.name),
        blueprints={face: tuple(names) for face, names in dict(blueprints).items()},
        routes=None, tables=getattr(module, "create_tables", None),
        settings=("extensions", *tuple(getattr(module, "SETTINGS", ()) or ())),
        switch=None, needs=getattr(module, "NEEDS", None),
        tier=str(getattr(module, "TIER", "full") or "full"), kind="extension",
        data_leaves_the_machine=bool(ext.data_leaves_the_machine),
    )


#: The loader's order, by name. The same order init_db made the tables in.
ORDER: tuple[str, ...] = (
    "smart", "ask_family", "family_tree", "books", "films", "handover", "stories", "capsules",
    "frame", "screening", "descriptions", "colour_copies", "video_moments", "then_and_now",
    "life_story", "letters", "stacks", "journal",
)

__all__ = ["FACES", "Feature", "ORDER", "TIERS", "Tables", "create_tables", "endpoints",
           "from_extension", "get", "listing", "names", "register_routes", "registry",
           "routed", "shape"]
