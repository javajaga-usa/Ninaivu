"""Every model in the catalogue is a complete manifest.

A model is a plugin: it says what it is for, who made it, under what licence,
what runs it, which packages it needs, and exactly which files to fetch and
what they must hash to. The AI models page, the downloader and the delete
button read nothing else, so an entry that leaves one out would be a model
that cannot be fetched, verified or removed. See docs/development/model-plugins.md.
"""
import re

import pytest

from ninaivu.media import model_catalog

SHA256 = re.compile(r"^[0-9a-f]{64}$")


@pytest.mark.parametrize("model_id", sorted(model_catalog.MODELS))
def test_a_model_declares_everything_the_page_and_the_downloader_read(model_id):
    entry = model_catalog.MODELS[model_id]
    for key in ("label", "used_for", "licence", "source", "runs_on"):
        assert isinstance(entry.get(key), str) and entry[key].strip(), f"{model_id}: {key}"
    assert isinstance(entry.get("requires"), list), f"{model_id}: requires"
    assert all(isinstance(p, str) and p for p in entry["requires"])
    assert entry.get("files"), f"{model_id}: nothing to fetch"


@pytest.mark.parametrize("model_id", sorted(model_catalog.MODELS))
def test_every_file_is_pinned_to_exact_bytes(model_id):
    names = set()
    for item in model_catalog.MODELS[model_id]["files"]:
        # Where it comes from: a full address, or a Hugging Face repository
        # at one pinned revision (the catalogue's HF template builds the address).
        if "url" in item:
            assert item["url"].startswith("https://"), (model_id, item["name"])
        else:
            assert item["repo"] and re.match(r"^[0-9a-f]{40}$", item["revision"]), (model_id, item["name"])
        assert item["name"] and not item["name"].startswith("/") and ".." not in item["name"]
        assert isinstance(item["bytes"], int) and item["bytes"] > 0, (model_id, item["name"])
        # A SHA-256 of the bytes; or, for the small text files of a repository
        # at a pinned revision, the git blob id that revision fixes them by.
        if "sha256" in item:
            assert SHA256.match(item["sha256"]), (model_id, item["name"])
        else:
            assert re.match(r"^[0-9a-f]{40}$", item.get("git_sha1", "")), (model_id, item["name"])
        key = (item.get("folder", ""), item["name"])
        assert key not in names, f"{model_id}: {key} listed twice"
        names.add(key)
        for mirror in item.get("mirrors", []):
            assert mirror.startswith("https://")


def test_what_a_model_replaces_is_a_list_of_file_names():
    for model_id, entry in model_catalog.MODELS.items():
        assert all(isinstance(name, str) and name for name in entry.get("replaces", [])), model_id
