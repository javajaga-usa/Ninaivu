"""Extensions: the optional pieces that live outside the core's promise.

The core promises two things: a photograph never leaves the house unless the
administrator chose where it goes, and the server runs on a small machine
without a GPU. Anything that cannot keep both — a service that sends a
picture to somebody else's model, an editor that needs gigabytes of weights —
is an extension. It is a separate package, found through an entry point, off
until an administrator turns it on, and it says on its switch what it does.

An extension package exposes one object in the ``ninaivu.extensions`` entry
point group with these attributes:

``NAME``
    Its short name, used in the settings and the console: ``"gemini"``.
``TITLE``, ``SUMMARY``
    What the console shows on the switch.
``DATA_LEAVES_THE_MACHINE``
    ``True`` when turning it on can send anything off this computer. The
    console then says where, from ``DESTINATION``.
``DOWNLOADS``
    What it fetches when turned on, in words ("about 2 GB of model weights"),
    or an empty string.
``register(app, face)``
    Called for each Flask app the server builds — ``face`` is ``"home"`` or
    ``"admin"`` — while the extension is on. Blueprints go on here.
``image_provider`` (optional)
    An object Sudar can send edits to: ``capabilities()``, ``is_available()``,
    ``plan_adjustments(prompt, current, image_bytes)`` and
    ``generate_image_edit(prompt, image_bytes, options)``.
``studio`` (optional)
    The heavy end of Sudar — generative edits, object removal, upscaling —
    with large models: ``capabilities(cfg)``, ``edit(cfg, prompt, image,
    options, report)``, ``remove(cfg, image, mask, report)`` returning None
    when it has nothing for that, and ``job(cfg, kind, image, data)``
    returning a ``work(report)`` callable or None. One extension at a time.

Which extensions are on is ``Config.extensions``, a list of names. Turning one
on or off takes effect at the next start: blueprints are fixed once a Flask
app has answered its first request, and saying "restart" plainly beats a
switch that half works.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "ninaivu.extensions"


@dataclass
class Extension:
    name: str
    title: str
    summary: str
    data_leaves_the_machine: bool
    destination: str
    downloads: str
    module: Any
    problems: list[str] = field(default_factory=list)

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name, "title": self.title, "summary": self.summary,
            "data_leaves_the_machine": self.data_leaves_the_machine,
            "destination": self.destination, "downloads": self.downloads,
            "problems": list(self.problems),
        }


_discovered: dict[str, Extension] | None = None
_providers: dict[str, Any] = {}
_studio: Any = None


#: For a checkout that has not ``pip install -e``'d its extensions, and for the
#: tests: module names, comma-separated, to load as if each were an entry
#: point. Nothing a packaged install needs.
DEV_MODULES_VAR = "NINAIVU_EXTENSION_MODULES"


def _load(entry) -> Extension | None:
    try:
        module = entry.load()
    except Exception as exc:                        # noqa: BLE001 — one bad package must not stop the server
        log.warning("extension %s could not be loaded: %s", entry.name, exc)
        return None
    name = str(getattr(module, "NAME", entry.name) or entry.name)
    problems = [f"{attr} is missing" for attr in ("TITLE", "SUMMARY", "register")
                if not hasattr(module, attr)]
    return Extension(
        name=name,
        title=str(getattr(module, "TITLE", name)),
        summary=str(getattr(module, "SUMMARY", "")),
        data_leaves_the_machine=bool(getattr(module, "DATA_LEAVES_THE_MACHINE", True)),
        destination=str(getattr(module, "DESTINATION", "")),
        downloads=str(getattr(module, "DOWNLOADS", "")),
        module=module, problems=problems,
    )


def discover(refresh: bool = False) -> dict[str, Extension]:
    """Every extension package installed, by name — on or off."""
    global _discovered
    if _discovered is not None and not refresh:
        return _discovered
    from importlib.metadata import entry_points
    found: dict[str, Extension] = {}
    try:
        entries = entry_points(group=ENTRY_POINT_GROUP)
    except TypeError:                                # very old importlib.metadata
        entries = entry_points().get(ENTRY_POINT_GROUP, [])
    for entry in entries:
        ext = _load(entry)
        if ext is not None:
            found[ext.name] = ext
    import os
    for module_name in filter(None, (os.environ.get(DEV_MODULES_VAR) or "").split(",")):
        ext = _load(_DevEntry(module_name.strip()))
        if ext is not None:
            found[ext.name] = ext
    _discovered = found
    return found


class _DevEntry:
    """A module named in ``NINAIVU_EXTENSION_MODULES``, shaped like an entry point."""

    def __init__(self, module_name: str) -> None:
        self.name = module_name.rsplit(".", 1)[-1].removeprefix("ninaivu_")
        self.module_name = module_name

    def load(self):
        import importlib
        return importlib.import_module(self.module_name)


def enabled_names(cfg) -> list[str]:
    names = getattr(cfg, "extensions", None) or []
    return [str(n) for n in names if isinstance(n, str) and n]


def is_enabled(cfg, name: str) -> bool:
    return name in enabled_names(cfg)


def active(cfg) -> list[Extension]:
    """The extensions that are both installed and switched on."""
    found = discover()
    return [found[n] for n in enabled_names(cfg) if n in found]


def install(app, cfg, face: str) -> list[str]:
    """Give every active extension the app to register on. Returns their names."""
    global _studio
    names = []
    _providers.clear()
    _studio = None
    for ext in active(cfg):
        if ext.problems:
            log.warning("extension %s is on but not usable: %s", ext.name, "; ".join(ext.problems))
            continue
        try:
            ext.module.register(app, face)
        except Exception:                            # noqa: BLE001
            log.exception("extension %s failed to register on the %s app", ext.name, face)
            continue
        provider = getattr(ext.module, "image_provider", None)
        if provider is not None:
            _providers[ext.name] = provider
        if getattr(ext.module, "studio", None) is not None and _studio is None:
            _studio = ext.module.studio
        names.append(ext.name)
    return names


def studio():
    """The active extension that does Sudar's heavy edits, or None."""
    return _studio


def image_provider(name: str | None):
    """The active extension that Sudar may send an edit to, by name, or None.

    Only an extension that is installed *and* switched on is ever returned, so
    a request naming a provider that is off goes nowhere.
    """
    if not name:
        return None
    return _providers.get(str(name))


def image_providers() -> dict[str, Any]:
    return dict(_providers)


def listing(cfg) -> list[dict[str, Any]]:
    """What the console shows: each installed extension and whether it is on."""
    on = set(enabled_names(cfg))
    out = []
    for ext in discover().values():
        item = ext.describe()
        item["enabled"] = ext.name in on
        out.append(item)
    # Names switched on whose package is not installed, so the setting is not
    # silently wrong.
    for name in sorted(on - set(discover())):
        out.append({"name": name, "title": name, "summary": "", "enabled": True,
                    "data_leaves_the_machine": False, "destination": "", "downloads": "",
                    "problems": ["not installed on this machine"]})
    return sorted(out, key=lambda e: e["name"])


def set_enabled(cfg, name: str, on: bool) -> list[str]:
    """Update ``cfg.extensions`` (not saved here) and return the new list."""
    names = enabled_names(cfg)
    if on and name not in names:
        names.append(name)
    if not on and name in names:
        names.remove(name)
    cfg.extensions = names
    return names
