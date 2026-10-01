"""The ComfyUI workflows an administrator has given Ninaivu, and how they are filled in.

A workflow is ComfyUI's own *API format* export (Workflow → Export (API)): a map
of node id to ``{"class_type": ..., "inputs": {...}}``. Ninaivu does not try to
understand the graph. The administrator marks the inputs it should fill by
replacing their values with placeholders:

``{{image}}``            the photograph, uploaded to the server (a LoadImage ``image`` input)
``{{mask}}``             the painted area as a white-on-black PNG (a LoadImageMask ``image`` input)
``{{prompt}}``           the request typed in the Playground (may sit inside longer text)
``{{negative_prompt}}``  the "avoid" text (may sit inside longer text)
``{{seed}}``             the seed, as a number

Each job needs some of them — an edit is nothing without the photograph and the
request — and the console says which are missing before a workflow is assigned.
"""
from __future__ import annotations

import copy
import json
import re
import time
from pathlib import Path
from typing import Any

from ninaivu.words import said

PLACEHOLDERS = ("image", "mask", "prompt", "negative_prompt", "seed")
#: Placeholders that must be an input's whole value, because they become a
#: filename or a number rather than text.
WHOLE_VALUE = ("image", "mask", "seed")

PURPOSES = {
    "edit": {"label": said("Generative edit"), "required": ("image", "prompt")},
    "remove": {"label": said("Object removal"), "required": ("image", "mask")},
    # The painted area redrawn to a request: hair where there is none, a sky,
    # a shirt. Sudar's Add hair (generative) is this with its own words.
    "inpaint": {"label": said("Paint and ask (inpaint)"), "required": ("image", "mask", "prompt")},
    # Photo-in, photo-out jobs. Their result keeps the size the server made it
    # at — an upscale is meant to come back larger.
    "upscale": {"label": said("Upscale"), "required": ("image",)},
    "restore": {"label": said("Restore faces"), "required": ("image",)},
    "colorize": {"label": said("Colourise"), "required": ("image",)},
}

MAX_WORKFLOW_BYTES = 2 * 1024 * 1024
MAX_NODES = 1000
_TOKEN = re.compile(r"\{\{\s*([A-Za-z_]+)\s*\}\}")
_ID = re.compile(r"[^a-z0-9]+")


def folder(cfg) -> Path:
    return Path(cfg.state_dir) / "ai-server" / "workflows"


def make_id(name: str) -> str:
    slug = _ID.sub("-", name.lower()).strip("-")[:48]
    if not slug:
        raise ValueError("Give the workflow a name with some letters or numbers in it.")
    return slug


def parse(workflow: Any) -> dict[str, Any]:
    """Check a workflow is an API-format export; return it as a dict."""
    if isinstance(workflow, str):
        if len(workflow.encode("utf-8")) > MAX_WORKFLOW_BYTES:
            raise ValueError("That workflow is larger than Ninaivu accepts (2 MB).")
        try:
            workflow = json.loads(workflow)
        except ValueError as error:
            raise ValueError(f"That is not valid JSON: {error}") from error
    if not isinstance(workflow, dict) or not workflow:
        raise ValueError("Paste a workflow exported from ComfyUI with Export (API).")
    if "nodes" in workflow and "links" in workflow:
        raise ValueError("That is ComfyUI's editor format. In ComfyUI use Workflow → "
                         "Export (API) and paste that file instead.")
    if len(workflow) > MAX_NODES:
        raise ValueError(f"That workflow has more than {MAX_NODES} nodes.")
    for node_id, node in workflow.items():
        if (not isinstance(node, dict) or not isinstance(node.get("class_type"), str)
                or not isinstance(node.get("inputs", {}), dict)):
            raise ValueError(f"Node {node_id!r} is not in ComfyUI's API format "
                             "(each node needs class_type and inputs).")
    if len(json.dumps(workflow).encode("utf-8")) > MAX_WORKFLOW_BYTES:
        raise ValueError("That workflow is larger than Ninaivu accepts (2 MB).")
    return workflow


def placeholders(workflow: dict[str, Any]) -> set[str]:
    """Every placeholder the workflow uses. ValueError for unknown or misplaced ones."""
    found: set[str] = set()
    for node_id, node in workflow.items():
        for key, value in (node.get("inputs") or {}).items():
            if not isinstance(value, str):
                continue
            for match in _TOKEN.finditer(value):
                name = match.group(1)
                if name not in PLACEHOLDERS:
                    raise ValueError(f"Node {node_id} input {key!r} uses {{{{{name}}}}}, which "
                                     f"Ninaivu does not fill. Use one of: "
                                     + ", ".join(f"{{{{{p}}}}}" for p in PLACEHOLDERS) + ".")
                if name in WHOLE_VALUE and value.strip() != match.group(0):
                    raise ValueError(f"Node {node_id} input {key!r}: {{{{{name}}}}} must be the "
                                     "whole value, not part of longer text.")
                found.add(name)
    return found


def fill(workflow: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """A copy of *workflow* with its placeholders replaced by *values*."""
    filled = copy.deepcopy(workflow)
    for node in filled.values():
        inputs = node.get("inputs") or {}
        for key, value in list(inputs.items()):
            if not isinstance(value, str) or "{{" not in value:
                continue
            whole = _TOKEN.fullmatch(value.strip())
            if whole and whole.group(1) in WHOLE_VALUE:
                inputs[key] = values[whole.group(1)]
                continue
            inputs[key] = _TOKEN.sub(lambda m: str(values.get(m.group(1), "")), value)
    return filled


def describe(entry: dict[str, Any], node_types: set[str] | None = None) -> dict[str, Any]:
    """What the console shows for one saved workflow."""
    workflow = entry["workflow"]
    try:
        used = placeholders(workflow)
        problem = ""
    except ValueError as error:
        used, problem = set(), str(error)
    purpose = PURPOSES.get(entry.get("purpose", ""), {"label": "?", "required": ()})
    classes = sorted({node["class_type"] for node in workflow.values()})
    return {
        "id": entry["id"],
        "name": entry["name"],
        "purpose": entry.get("purpose", ""),
        "purpose_label": purpose["label"],
        "nodes": len(workflow),
        "placeholders": sorted(used),
        "missing_placeholders": [p for p in purpose["required"] if p not in used],
        "problem": problem,
        "missing_nodes": (sorted(c for c in classes if c not in node_types)
                          if node_types is not None else None),
        "saved_at": entry.get("saved_at", 0),
    }


def save(cfg, name: Any, purpose: Any, workflow: Any) -> dict[str, Any]:
    if not isinstance(name, str) or not name.strip() or len(name) > 80:
        raise ValueError("Give the workflow a name of up to 80 characters.")
    if purpose not in PURPOSES:
        raise ValueError("Choose what the workflow is for.")
    parsed = parse(workflow)
    used = placeholders(parsed)
    missing = [p for p in PURPOSES[purpose]["required"] if p not in used]
    if missing:
        raise ValueError(f"A {PURPOSES[purpose]['label'].lower()} workflow needs "
                         + " and ".join(f"{{{{{p}}}}}" for p in missing)
                         + " in place of the inputs Ninaivu should fill.")
    entry = {"id": make_id(name), "name": name.strip(), "purpose": purpose,
             "workflow": parsed, "saved_at": time.time()}
    target = folder(cfg)
    target.mkdir(parents=True, exist_ok=True)
    temporary = target / f".{entry['id']}.json.tmp"
    temporary.write_text(json.dumps(entry, indent=1), encoding="utf-8")
    temporary.replace(target / f"{entry['id']}.json")
    return entry


def load(cfg, workflow_id: str) -> dict[str, Any] | None:
    try:
        if not isinstance(workflow_id, str) or make_id(workflow_id) != workflow_id:
            return None
    except ValueError:
        return None
    path = folder(cfg) / f"{workflow_id}.json"
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
        entry["workflow"] = parse(entry.get("workflow"))
        return entry
    except (OSError, ValueError, KeyError):
        return None


def all_entries(cfg) -> list[dict[str, Any]]:
    entries = []
    for path in sorted(folder(cfg).glob("*.json")) if folder(cfg).is_dir() else []:
        entry = load(cfg, path.stem)
        if entry:
            entries.append(entry)
    return entries


def delete(cfg, workflow_id: str) -> bool:
    if load(cfg, workflow_id) is None:
        return False
    (folder(cfg) / f"{workflow_id}.json").unlink()
    return True
