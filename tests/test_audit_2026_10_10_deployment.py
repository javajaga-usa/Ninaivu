"""The deployment files, from the audit of 10 October 2026: the two proxy
configurations (M1, L-1), the Linux service (L-16), the container image
(L-17), what git must never take (L-18), the Mac setup's quarantine clearing
(L-19), the portable README (I-2) and the release's provenance (I-1).

Each test reads the shipped file and asserts what the finding asked for, so
that a later edit cannot quietly put the hole back.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLERS = ROOT / "installers"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _yaml(path: Path) -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(_read(path))


# --- M1: the Caddyfile serves only the names it lists, and forwards the browser's ------

def _caddy_sites(text: str) -> list[tuple[list[str], list[str]]]:
    """Each site block as (labels, body lines without comments)."""
    sites, labels, body = [], None, []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if labels is None:
            if line and not line.startswith((" ", "\t", "{")) and line.endswith("{"):
                labels = [label.strip() for label in line[:-1].split(",")]
                body = []
        elif line == "}":
            sites.append((labels, body))
            labels = None
        elif line.strip():
            body.append(line.strip())
    return sites


def test_the_caddyfile_has_no_bare_port_beside_a_name():
    sites = _caddy_sites(_read(INSTALLERS / "caddy" / "Caddyfile"))
    assert len(sites) >= 3
    for labels, body in sites:
        bare = [label for label in labels if re.fullmatch(r":\d+", label)]
        if not bare:
            continue
        # A catch-all is allowed only on its own, and only to refuse.
        assert labels == [":443"], labels
        assert body == ["abort"], body
    assert any(labels == [":443"] for labels, _ in sites), "unknown names are refused, not served"


def test_the_caddyfile_forwards_the_browsers_host_and_every_x_header_to_both_apps():
    text = _read(INSTALLERS / "caddy" / "Caddyfile")
    directives = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    assert "header_up Host" not in directives, "Caddy's default forwards the browser's Host"
    assert "{upstream_hostport}" not in directives
    proxied = [body for labels, body in _caddy_sites(text)
               if any(line.startswith("reverse_proxy") for line in body)]
    assert len(proxied) == 2
    for body in proxied:
        for header in ("X-Real-IP", "X-Forwarded-For", "X-Forwarded-Proto"):
            assert any(line.startswith(f"header_up {header} ") for line in body), header
        for header in ("Strict-Transport-Security", "X-Content-Type-Options",
                       "X-Frame-Options", "Referrer-Policy"):
            assert any(line.startswith(header) for line in body), header


@pytest.mark.parametrize("path", ["caddy/Caddyfile", "nginx/ninaivu.conf"])
def test_each_proxy_configuration_says_trusted_proxies_must_be_1(path):
    head = _read(INSTALLERS / path).split("upstream", 1)[0][:2000]
    assert "trusted_proxies" in head and " 1 " in head.replace("to 1 (", " 1 ")
    assert "Secure" in head, "why: the cookie, the local-only routes, the Host check"


def test_the_production_guide_no_longer_says_to_keep_trusted_proxies_at_0():
    guide = _read(ROOT / "docs" / "operations" / "production.md")
    assert "Keep `trusted_proxies` at 0" not in guide
    assert "set `trusted_proxies` to 1" in guide
    # The Caddyfile is pointed at, not copied: the copy had drifted.
    assert "{upstream_hostport}" not in guide and "```caddyfile" not in guide
    assert "installers/caddy/Caddyfile" in guide


# --- L-1: the nginx console block has the headers, and every location the X-* ---------

def _nginx_blocks(text: str) -> tuple[str, str]:
    family = text[text.index("# --- Family App Server Block ---"):text.index("# --- Admin Console Server Block")]
    admin = text[text.index("# --- Admin Console Server Block"):]
    return family, admin


def test_the_nginx_console_block_sets_the_same_headers_as_the_family_block():
    family, admin = _nginx_blocks(_read(INSTALLERS / "nginx" / "ninaivu.conf"))
    headers = re.findall(r"add_header (\S+) .* always;", family)
    assert set(headers) >= {"X-Content-Type-Options", "X-Frame-Options", "Referrer-Policy",
                            "Strict-Transport-Security"}
    assert set(re.findall(r"add_header (\S+) .* always;", admin)) == set(headers)


def test_every_nginx_location_that_proxies_forwards_who_called():
    text = _read(INSTALLERS / "nginx" / "ninaivu.conf")
    locations = re.findall(r"location [^{]*\{(.*?)\n    \}", text, re.S)
    assert len(locations) == 5
    for body in locations:
        assert "proxy_pass" in body
        for header in ("X-Real-IP $remote_addr", "X-Forwarded-For $proxy_add_x_forwarded_for",
                       "X-Forwarded-Proto $scheme"):
            assert f"proxy_set_header {header};" in body, body


# --- L-16: the systemd unit is hardened ------------------------------------------------
# (The Linux installer that wrote its own unit is gone: Ninaivu ships for Linux
# as a portable folder, and the unit in installers/systemd is the one to use.)

def test_the_static_unit_is_hardened():
    static = _read(INSTALLERS / "systemd" / "ninaivu.service")
    for line in ("NoNewPrivileges=true", "ProtectSystem=strict", "ProtectHome=read-only",
                 "PrivateTmp=true", "PrivateDevices=true", "ProtectKernelTunables=true",
                 "ProtectKernelModules=true", "ProtectControlGroups=true", "RestrictRealtime=true",
                 "RestrictSUIDSGID=true", "LockPersonality=true", "ReadWritePaths=/var/lib/ninaivu"):
        assert f"\n{line}\n" in static, line
    assert "install.sh" not in static, "no installer of its own to point at"


# --- L-17: the image is pinned by digest and publishes only the family app ----------------

def test_the_docker_base_image_is_pinned_by_digest_in_both_stages():
    dockerfile = _read(INSTALLERS / "docker" / "Dockerfile")
    froms = re.findall(r"^FROM (\S+)", dockerfile, re.M)
    assert len(froms) == 2
    digests = {re.fullmatch(r"python:3\.\d+-slim@sha256:([0-9a-f]{64})", f).group(1) for f in froms}
    assert len(digests) == 1, "both stages name the same image"


def test_the_docker_image_exposes_only_the_family_app():
    dockerfile = _read(INSTALLERS / "docker" / "Dockerfile")
    exposed = re.findall(r"^EXPOSE (.*)$", dockerfile, re.M)
    assert exposed == ["5000"], exposed
    compose = _read(INSTALLERS / "docker" / "docker-compose.yml")
    assert '"127.0.0.1:3000:3000"' in compose, "the console reaches the host's loopback only"


def test_dependabot_moves_the_docker_digest():
    config = _yaml(ROOT / ".github" / "dependabot.yml")
    docker = [u for u in config["updates"] if u["package-ecosystem"] == "docker"]
    assert [u["directory"] for u in docker] == ["/installers/docker"]


# --- L-18: keys stay out of git, and a secret out of a commit ------------------------------

def test_git_ignores_keys_certificates_and_a_state_folder_in_the_checkout():
    ignored = _read(ROOT / ".gitignore").splitlines()
    for pattern in ("*.pem", "*.key", "*.pfx", "*.p12", "*.crt", "tls/", ".ninaivu/"):
        assert pattern in ignored, pattern
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
    if tracked.returncode == 0:
        assert not [f for f in tracked.stdout.split()
                    if re.search(r"\.(pem|key|pfx|p12|crt)$|(^|/)tls/", f)], "a tracked key"


def test_the_pre_commit_hooks_scan_for_secrets_pinned_by_commit():
    config = _yaml(ROOT / ".pre-commit-config.yaml")
    gitleaks = [r for r in config["repos"] if r.get("repo", "").endswith("/gitleaks/gitleaks")]
    assert len(gitleaks) == 1
    assert re.fullmatch(r"[0-9a-f]{40}", gitleaks[0]["rev"])
    assert [h["id"] for h in gitleaks[0]["hooks"]] == ["gitleaks"]
    text = _read(ROOT / ".pre-commit-config.yaml")
    assert re.search(r"rev: [0-9a-f]{40}\s+# v\d+\.\d+\.\d+", text), "the version beside the commit"


# --- L-19: the Mac setup clears the quarantine from what runs, not the whole folder -------

def test_the_mac_setup_clears_the_quarantine_only_from_the_files_it_runs():
    setup = _read(ROOT / "Setup Ninaivu.command")
    assert "xattr -dr" not in setup
    assert 'xattr -d com.apple.quarantine "$file"' in setup
    cleared = setup[setup.index("for file in"):setup.index("; do")]
    for name in ('"$HERE/Ninaivu.command"', '"$HERE/Setup Ninaivu.command"',
                 '"$HERE/ninaivu_control.pyw"', '"$HERE"/launcher/*'):
        assert name in cleared, name
    assert '[ -f "$file" ] || continue' in setup


# --- I-2: the portable README says what a USB stick cannot protect ------------------------

def test_the_portable_readme_warns_about_data_on_a_fat_formatted_stick():
    readme = _read(INSTALLERS / "windows" / "portable" / "README-PORTABLE.txt")
    assert "app\\data" in readme and "exFAT" in readme and "FAT32" in readme
    assert "NTFS" in readme and "encrypted" in readme


# --- I-1: the release attests where its files came from -----------------------------------

def test_the_release_attests_the_provenance_of_what_it_attaches():
    text = _read(ROOT / ".github" / "workflows" / "release.yml")
    flow = _yaml(ROOT / ".github" / "workflows" / "release.yml")
    job = flow["jobs"]["release"]
    assert job["permissions"]["id-token"] == "write"
    assert job["permissions"]["attestations"] == "write"
    # The builders never attest; the docker job does, through the workflow it calls.
    for name in ("gate", "windows", "macos", "linux"):
        assert "attestations" not in flow["jobs"][name].get("permissions", {}), name
    step = next(s for s in job["steps"] if "attest-build-provenance" in s.get("uses", ""))
    assert re.fullmatch(r"actions/attest-build-provenance@[0-9a-f]{40}", step["uses"])
    assert re.search(r"attest-build-provenance@[0-9a-f]{40} # v\d+\.\d+\.\d+", text)
    subjects = step["with"]["subject-path"].split()
    assert "out/SHA256SUMS.txt" in subjects
    for pattern in ("out/windows/*.zip", "out/macos/*.zip", "out/linux/*.tar.gz"):
        assert pattern in subjects, pattern
    names = [s.get("name") or s.get("uses") for s in job["steps"]]
    assert names.index("Checksums") < names.index("Build provenance") < names.index("Publish the release")


def test_the_docker_image_is_attested_by_the_digest_it_was_pushed_as():
    text = _read(ROOT / ".github" / "workflows" / "docker.yml")
    flow = _yaml(ROOT / ".github" / "workflows" / "docker.yml")
    job = flow["jobs"]["publish"]
    assert job["permissions"]["id-token"] == "write"
    assert job["permissions"]["attestations"] == "write"
    # The pull-request build only builds; it has nothing to attest.
    assert "attestations" not in flow["jobs"]["build"]["permissions"]
    push = next(s for s in job["steps"] if "build-push-action" in s.get("uses", ""))
    assert push["id"] == "push"
    step = next(s for s in job["steps"] if "attest-build-provenance" in s.get("uses", ""))
    assert re.fullmatch(r"actions/attest-build-provenance@[0-9a-f]{40}", step["uses"])
    assert re.search(r"attest-build-provenance@[0-9a-f]{40} # v\d+\.\d+\.\d+", text)
    assert step["with"]["subject-digest"] == "${{ steps.push.outputs.digest }}"
    assert step["with"]["push-to-registry"] is True
    names = [s.get("name") or s.get("uses") for s in job["steps"]]
    assert names.index("Build and push the image") < names.index("Build provenance")
    # A called workflow holds no more than its caller grants.
    release = _yaml(ROOT / ".github" / "workflows" / "release.yml")
    caller = release["jobs"]["docker"]["permissions"]
    assert caller["id-token"] == "write" and caller["attestations"] == "write"


# --- the Mac build is a zip of the portable folder, notarised when it can be ---------------

def test_the_mac_build_zips_the_portable_folder_and_staples_the_app_inside():
    """The .dmg (and hdiutil's "Resource busy", 10 October 2026) is gone: the
    Mac download is the portable folder, zipped with ditto so the bundle's
    links and signature survive, notarised as the zip and stapled to the app."""
    text = _read(ROOT / "installers" / "macos" / "build.sh")
    assert "hdiutil" not in text and ".dmg" not in text
    assert "ditto -c -k --sequesterRsrc --keepParent" in text
    notarise = text[text.index("notarytool submit"):]
    assert 'stapler staple "$stage/Ninaivu.app"' in notarise
    assert notarise.index("stapler staple") < notarise.rindex("ditto -c -k"), "zipped again after stapling"
