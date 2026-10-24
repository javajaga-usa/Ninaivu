"""The Mac signing path in the release workflow, and the update check each
build is put through (docs/development/signing-releases.md).

Neither can run here: they need Apple's tools and the repository's secrets.
What is held is what a release depends on: the secret names the guide tells
the owner to create are the ones the workflow reads, the certificate's name
and team are read from it when not given, and the Linux and Mac builds are
both updated to from the latest release before anything is published.
"""

from __future__ import annotations

import fnmatch
import plistlib
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
GUIDE = ROOT / "docs" / "development" / "signing-releases.md"
BUILD = ROOT / "installers" / "macos" / "build.sh"

REQUIRED = ("MAC_SIGN_P12_BASE64", "MAC_SIGN_P12_PASSWORD", "MAC_NOTARY_APPLE_ID", "MAC_NOTARY_PASSWORD")
OPTIONAL = ("MAC_SIGN_IDENTITY", "MAC_NOTARY_TEAM_ID")


def _jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _step(job: str, name: str) -> dict:
    return next(step for step in _jobs()[job]["steps"] if step.get("name", "").startswith(name))


def test_the_guide_names_exactly_the_secrets_the_workflow_reads():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    read = set(re.findall(r"secrets\.(MAC_\w+)", workflow))
    assert read == set(REQUIRED) | set(OPTIONAL)
    guide = GUIDE.read_text(encoding="utf-8")
    for name in (*REQUIRED, *OPTIONAL):
        assert f"`{name}`" in guide, name


def test_the_certificate_says_who_signs_when_nobody_else_does():
    run = _step("macos", "Import the signing certificate")["run"]
    assert 'if [ -z "${SIGN_IDENTITY:-}" ]' in run
    assert "security find-identity -v -p codesigning" in run
    assert "DeveloperIDG2CA.cer" in run, "the intermediate codesign needs to trust the certificate"
    # Notarised only with an Apple ID and its app-specific password; the
    # team comes from the certificate's name otherwise.
    assert 'if [ -z "${TEAM_ID:-}" ]' in run
    assert "notarytool store-credentials" in run and "::warning::" in run


def test_the_release_notes_say_what_the_mac_app_is():
    release = _jobs()["release"]
    assert "MAC_NOTARY_APPLE_ID" in release["env"]["HAS_NOTARY"]
    notes = next(step["run"] for step in release["steps"] if "signed but not notarised" in step.get("run", ""))
    assert "not signed or notarised" in notes


def test_a_notarised_build_must_pass_gatekeeper():
    build = BUILD.read_text(encoding="utf-8")
    assert "xcrun stapler validate" in build
    assert "spctl --assess --type execute" in build
    assert "source=Notarized Developer ID" in build


def test_the_app_says_why_it_asks_for_each_folder():
    build = BUILD.read_text(encoding="utf-8")
    start = build.index("<?xml", build.index("Info.plist"))
    end = build.index("</plist>", start) + len("</plist>")
    plist = plistlib.loads(build[start:end].replace("$version", "1.0.0").encode())
    assert plist["CFBundleIdentifier"] == "org.ninaivu.app", "macOS keeps its answers by this name"
    for key in ("NSDocumentsFolderUsageDescription", "NSDesktopFolderUsageDescription",
                "NSDownloadsFolderUsageDescription", "NSRemovableVolumesUsageDescription",
                "NSNetworkVolumesUsageDescription"):
        assert plist[key].startswith("Ninaivu "), key


def test_linux_and_mac_builds_are_updated_to_from_the_latest_release():
    for job, asset in (("linux", "linux-x64-portable.tar.gz"), ("macos", "macos-arm64-portable.zip")):
        steps = [step.get("name", "") for step in _jobs()[job]["steps"]]
        check = _step(job, "Bring a library from the latest release")
        assert steps.index(check["name"]) > steps.index("Build"), job
        assert "releases/latest" in check["run"] and asset in check["run"]
        assert "SHA256SUMS.txt" in check["run"], "the older download is checked first"
        assert "tools/check_bring_across.py" in check["run"]
    paths = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))[True]["pull_request"]["paths"]
    for path in ("tools/check_bring_across.py", "ninaivu/desktop/portable.py", "ninaivu/storage/upgrade.py"):
        assert any(fnmatch.fnmatch(path, glob) for glob in paths), path
