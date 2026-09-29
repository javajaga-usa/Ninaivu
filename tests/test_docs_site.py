"""The docs site's pages exist and their links resolve, so `mkdocs build
--strict` on the Pages workflow does not find out first."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
SITE = DOCS / "site"
LINK = re.compile(r"\[[^\]]*\]\(([^)\s#]+)(?:#[^)]*)?\)")


def _nav_paths(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _nav_paths(value)
    elif isinstance(node, list):
        for item in node:
            yield from _nav_paths(item)


def test_every_page_in_the_nav_exists():
    yaml = pytest.importorskip("yaml")
    text = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    text = re.sub(r"!!python/[^\s]+", "", text)
    config = yaml.safe_load(text)
    pages = list(_nav_paths(config["nav"]))
    assert len(pages) >= 18
    missing = [p for p in pages if not (DOCS / p).is_file()]
    assert missing == []
    assert [p for p in pages if p.startswith("site/")][:8] == [
        "site/index.md", "site/install.md", "site/first-day.md", "site/family-and-roles.md",
        "site/backup.md", "site/remote-access.md", "site/ai.md", "site/troubleshooting.md"]


@pytest.mark.parametrize("page", sorted(p.name for p in SITE.glob("*.md")))
def test_the_links_on_each_page_resolve(page):
    text = (SITE / page).read_text(encoding="utf-8")
    broken = []
    for target in LINK.findall(text):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        if not (SITE / target).resolve().exists():
            broken.append(target)
    assert broken == [], f"{page}: {broken}"


def test_the_pages_workflow_builds_strictly():
    text = (ROOT / ".github" / "workflows" / "docs.yml").read_text(encoding="utf-8")
    assert "mkdocs build --strict" in text
    assert "deploy-pages" in text
