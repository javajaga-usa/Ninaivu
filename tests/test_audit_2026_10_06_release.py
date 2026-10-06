"""Audit 2026-10-06: installers, launcher, tooling and the release workflows.

The Linux installer is run in tests/test_linux_installer_runs.py and the Mac
launchers in tests/test_mac_launcher.py; these cover the rest.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ninaivu.media import model_catalog

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def leave_the_catalogue_as_it_was():
    before = model_catalog._root
    yield
    model_catalog.configure(before)


# -- A-23: an upgrade deleted every downloaded AI model ----------------------------------

@pytest.mark.parametrize("folder", [
    "/opt/ninaivu/python/lib/python3.12/site-packages",
    "/usr/lib/python3/dist-packages",
    r"C:\Users\amma\AppData\Local\Programs\Ninaivu\pkgs",
    "/Applications/Ninaivu.app/Contents/Resources/python/lib/python3.12/site-packages",
])
def test_an_installed_copy_is_told_apart_from_a_checkout(folder):
    path = Path(folder.replace("\\", "/"))
    assert model_catalog.is_installed_copy(path)


def test_a_checkout_is_not_an_installed_copy():
    assert not model_catalog.is_installed_copy(ROOT)
    assert not model_catalog.is_installed_copy(Path("/home/amma/src/ninaivu"))


def test_a_checkout_keeps_its_models_beside_it(monkeypatch):
    monkeypatch.delenv("NINAIVU_AI_MODELS_DIR", raising=False)
    model_catalog.configure(None)
    assert model_catalog.models_root() == ROOT / ".ai-models"


def test_an_installed_copy_keeps_its_models_in_a_folder_of_the_persons_own(tmp_path, monkeypatch):
    """Two levels up from the package is site-packages, pkgs or the inside of
    the Mac app in an installed copy, and every upgrade replaces that whole."""
    monkeypatch.delenv("NINAIVU_AI_MODELS_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(model_catalog, "is_installed_copy", lambda folder: True)
    model_catalog.configure(None)
    root = model_catalog.models_root()
    if sys.platform.startswith("linux"):
        assert root == tmp_path / "state" / "ninaivu" / "ai-models"
    assert "site-packages" not in root.parts and ".ai-models" not in root.parts


def test_the_setting_and_the_environment_still_win_over_the_default(tmp_path, monkeypatch):
    monkeypatch.setattr(model_catalog, "is_installed_copy", lambda folder: True)
    monkeypatch.setenv("NINAIVU_AI_MODELS_DIR", str(tmp_path / "env"))
    model_catalog.configure(None)
    assert model_catalog.models_root() == tmp_path / "env"
    model_catalog.configure(tmp_path / "chosen")
    assert model_catalog.models_root() == tmp_path / "chosen"


def test_the_per_user_folder_on_each_platform(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert model_catalog.user_models_dir("win32") == tmp_path / "Local" / "Ninaivu" / "ai-models"
    assert model_catalog.user_models_dir("darwin") == (
        Path.home() / "Library" / "Application Support" / "Ninaivu" / "ai-models")
    assert model_catalog.user_models_dir("linux") == tmp_path / "xdg" / "ninaivu" / "ai-models"


def test_the_windows_installer_moves_the_models_before_removing_pkgs():
    nsi = (ROOT / "installers" / "windows" / "ninaivu.nsi").read_text(encoding="ascii")
    moved = nsi.index(r'Rename "$INSTDIR\pkgs\.ai-models"')
    assert moved < nsi.index(r'RMDir /r "$INSTDIR\pkgs"')
    assert r"\Ninaivu\ai-models" in nsi and "ReadEnvStr $1 LOCALAPPDATA" in nsi
    assert "Abort" in nsi[moved:nsi.index(r'RMDir /r "$INSTDIR\pkgs"')], \
        "models that cannot be moved stop the upgrade before anything is removed"


# -- A-54: macOS hardening ------------------------------------------------------------------

def test_the_mac_app_does_not_allow_dyld_injection():
    import plistlib
    entitlements = plistlib.loads((ROOT / "installers" / "macos" / "entitlements.plist").read_bytes())
    assert "com.apple.security.cs.allow-dyld-environment-variables" not in entitlements
    assert entitlements["com.apple.security.cs.disable-library-validation"] is True


# -- A-29: the launcher picked a random port on every start --------------------------------

def _launcher():
    import importlib.util
    spec = importlib.util.spec_from_file_location("ninaivu_start_a29", ROOT / "launcher" / "start.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _linux_sockets(busy=()):
    """socket.socket as Linux answers an ordinary user: no port below 1024."""
    import errno

    class Socket:
        def __init__(self, *args, **kwargs):
            self.port = None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def setsockopt(self, *args):
            pass

        def bind(self, address):
            port = address[1]
            if port == 0:
                self.port = 50000 + len(busy)
                return
            if port < 1024:
                raise PermissionError(errno.EACCES, "Permission denied")
            if port in busy:
                raise OSError(errno.EADDRINUSE, "Address already in use")
            self.port = port

        def getsockname(self):
            return ("127.0.0.1", self.port)
    return Socket


@pytest.mark.parametrize("preferred, expected", [(443, 8443), (80, 8080)])
def test_a_port_this_user_may_not_use_falls_back_to_a_fixed_one(monkeypatch, preferred, expected):
    module = _launcher()
    monkeypatch.setattr(module.socket, "socket", _linux_sockets())
    assert module.find_port("0.0.0.0", preferred) == expected
    # The same on the next start: the address on the phones keeps working.
    assert module.find_port("0.0.0.0", preferred) == expected


def test_the_fallback_steps_past_a_busy_one(monkeypatch):
    module = _launcher()
    monkeypatch.setattr(module.socket, "socket", _linux_sockets(busy={8443}))
    assert module.find_port("0.0.0.0", 443) == 8080


# -- A-27: a release only from a tested commit, never over one already out ------------------

def _workflow(name: str) -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))


def _gate_script() -> str:
    step = next(s for s in _workflow("release.yml")["jobs"]["gate"]["steps"] if s.get("id") == "check")
    assert "${{" not in step["run"], "event values reach the script through env, not the text"
    return step["run"]


CURL = """#!/bin/sh
# A stand-in for the GitHub API: the URL's last part chooses a canned answer.
out=""; url=""
while [ $# -gt 0 ]; do
    case "$1" in -o) out=$2; shift 2 ;; -w|-H) shift 2 ;; -*) shift ;; *) url=$1; shift ;; esac
done
echo "$url" >> "$FAKE/asked.log"
case "$url" in
    */git/ref/tags/*) code=$(cat "$FAKE/tag" 2>/dev/null || echo 404) ;;
    */releases/tags/*) code=$(cat "$FAKE/release" 2>/dev/null || echo 404) ;;
    */actions/workflows/tests.yml/runs*)
        code=200
        n=$(cat "$FAKE/polls" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$FAKE/polls"
        if [ -f "$FAKE/runs.$n" ]; then cp "$FAKE/runs.$n" "$out"; else cp "$FAKE/runs" "$out"; fi ;;
    *) code=500 ;;
esac
printf '%s' "$code"
"""


def _runs(*runs):
    import json
    return json.dumps({"workflow_runs": [{"status": s, "conclusion": c} for s, c in runs]})


@pytest.fixture()
def gate(tmp_path):
    import shutil
    import subprocess
    if not (shutil.which("jq") and shutil.which("bash")):
        pytest.skip("needs bash and jq, as the runner has")
    # Wherever jq is (Homebrew's /opt/homebrew/bin on a Mac runner), it stays
    # reachable; everything else comes from the system folders.
    jq_dir = str(Path(shutil.which("jq")).parent)
    fake = tmp_path / "fake"
    fake.mkdir()
    (fake / "curl").write_text(CURL)
    (fake / "curl").chmod(0o755)
    (fake / "sleep").write_text("#!/bin/sh\nexit 0\n")
    (fake / "sleep").chmod(0o755)
    work = tmp_path / "work"
    (work / "ninaivu").mkdir(parents=True)
    (work / "ninaivu" / "__init__.py").write_text('__version__ = "2.3.4"\n')
    script = _gate_script()

    def run(event="push", ref="refs/tags/v2.3.4", ref_name="v2.3.4", runs=None):
        (fake / "runs").write_text(runs or _runs(("completed", "success")))
        output = tmp_path / "output.txt"
        output.write_text("")
        env = {"PATH": f"{fake}:/usr/bin:/bin:{jq_dir}", "FAKE": str(fake), "GH_TOKEN": "t",
               "EVENT": event, "REF": ref, "REF_TYPE": "tag" if ref.startswith("refs/tags/") else "branch",
               "REF_NAME": ref_name, "SHA": "abc123", "GITHUB_REPOSITORY": "o/r",
               "RUNNER_TEMP": str(tmp_path), "GITHUB_OUTPUT": str(output)}
        done = subprocess.run(["bash", "-c", script], cwd=work, env=env,
                              capture_output=True, text=True, timeout=60)
        return done, output.read_text()
    run.fake = fake
    return run


def test_a_tagged_commit_whose_tests_passed_is_released(gate):
    done, output = gate()
    assert done.returncode == 0, done.stdout + done.stderr
    assert output.strip() == "version=2.3.4"


def test_a_tag_that_is_not_the_version_is_refused(gate):
    done, _ = gate(ref="refs/tags/v2.3.5", ref_name="v2.3.5")
    assert done.returncode == 1 and "not v + __version__" in done.stdout


def test_run_workflow_from_another_branch_is_refused(gate):
    done, _ = gate(event="workflow_dispatch", ref="refs/heads/feature", ref_name="feature")
    assert done.returncode == 1 and "only from main" in done.stdout


def test_run_workflow_when_the_tag_exists_is_refused(gate):
    (gate.fake / "tag").write_text("200")
    done, _ = gate(event="workflow_dispatch", ref="refs/heads/main", ref_name="main")
    assert done.returncode == 1 and "exists already" in done.stdout


def test_a_release_that_exists_is_never_replaced(gate):
    (gate.fake / "release").write_text("200")
    done, _ = gate()
    assert done.returncode == 1 and "release for v2.3.4 exists" in done.stdout


def test_failed_tests_stop_the_release(gate):
    done, _ = gate(runs=_runs(("completed", "failure")))
    assert done.returncode == 1 and "has not passed" in done.stdout


def test_tests_still_running_are_waited_for(gate):
    (gate.fake / "runs.1").write_text(_runs(("in_progress", None)))
    (gate.fake / "runs.2").write_text(_runs(("queued", None), ("completed", "failure")))
    done, output = gate(runs=_runs(("completed", "success")))
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.count("still running") == 2 and output.strip() == "version=2.3.4"


def test_only_the_publishing_jobs_may_write():
    """A-30: write access was granted to every job, the pull-request builds too."""
    release = _workflow("release.yml")
    assert release["permissions"] == {"contents": "read"}
    assert release["jobs"]["release"]["permissions"] == {"contents": "write"}
    assert release["jobs"]["gate"]["permissions"] == {"contents": "read", "actions": "read"}
    for job in ("windows", "macos", "linux"):
        assert "permissions" not in release["jobs"][job]
        assert release["jobs"][job]["needs"] == "gate"
    docker = _workflow("docker.yml")
    assert docker["permissions"] == {"contents": "read"}
    assert docker["jobs"]["build"]["permissions"] == {"contents": "read"}
    assert docker["jobs"]["publish"]["if"] == "inputs.version != ''"
    docs = _workflow("docs.yml")
    assert docs["permissions"] == {"contents": "read"}
    assert docs["jobs"]["deploy"]["permissions"] == {"pages": "write", "id-token": "write"}
    assert _workflow("tests.yml")["permissions"] == {"contents": "read"}


def test_every_action_is_pinned_to_a_commit():
    import re
    for path in (ROOT / ".github" / "workflows").glob("*.yml"):
        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.search(r"uses:\s*(\S+)", line)
            if not match or match.group(1).startswith("./"):
                continue
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", match.group(1)), (path.name, line)
            assert re.search(r"# v\d+\.\d+\.\d+$", line), ("the version, as a comment", path.name, line)


def test_no_secret_is_written_into_a_script():
    import re
    for path in (ROOT / ".github" / "workflows").glob("*.yml"):
        flow = _workflow(path.name)
        for name, job in flow["jobs"].items():
            for step in job.get("steps", []):
                assert not re.search(r"\$\{\{\s*secrets\.", step.get("run", "")), (path.name, name)


def test_the_docker_image_is_pushed_from_the_release():
    """A-49: pushed only on a tag push, the image stayed at 0.1.2."""
    release = _workflow("release.yml")
    job = release["jobs"]["docker"]
    assert job["uses"] == "./.github/workflows/docker.yml"
    assert job["needs"] == ["gate", "release"]
    assert job["permissions"]["packages"] == "write"
    docker = _workflow("docker.yml")
    on = docker[True] if True in docker else docker["on"]
    assert "tags" not in on["push"] and "workflow_call" in on
    compose = (ROOT / "installers" / "docker" / "docker-compose.yml").read_text(encoding="utf-8")
    assert "ninaivu:1.0" not in compose and "image: ninaivu:local" in compose
    dockerfile = (ROOT / "installers" / "docker" / "Dockerfile").read_text(encoding="utf-8")
    assert "localhost:${NINAIVU_PORT:-5000}/healthz" in dockerfile
    assert "constraints-tested.txt" in dockerfile


# -- A-28 / A-47: what ships is pinned and recorded -----------------------------------------

def test_every_build_uses_the_tested_versions_and_lists_what_it_bundled():
    builds = {
        "linux": (ROOT / "installers" / "linux" / "build.sh").read_text(encoding="utf-8"),
        "macos": (ROOT / "installers" / "macos" / "build.sh").read_text(encoding="utf-8"),
        "windows": (ROOT / "installers" / "windows" / "build.ps1").read_text(encoding="utf-8"),
    }
    for name, text in builds.items():
        assert "constraints-tested.txt" in text, name
        assert "-packages.txt" in text, name
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    for artifact in ("out/windows/nsis/*-packages.txt", "out/linux-arm64/*-packages.txt",
                     "out/macos-x86_64/*-packages.txt"):
        assert artifact in release


def test_the_build_tools_are_pinned():
    import re
    lines = [line.split("#")[0].strip()
             for line in (ROOT / "requirements" / "build-tools.txt").read_text().splitlines()]
    pins = [line for line in lines if line]
    assert pins and all(re.fullmatch(r"[A-Za-z0-9_.-]+==[\w.]+", line) for line in pins), pins
    names = {line.split("==")[0].lower() for line in pins}
    assert {"pip", "wheel", "pynsist", "markdown", "playwright"} <= names


def test_the_shipped_set_has_no_version_with_a_known_advisory():
    """The versions pip-audit reported on 6 Oct 2026, raised."""
    pins = {}
    for line in (ROOT / "requirements" / "constraints-tested.txt").read_text().splitlines():
        if "==" in line and not line.startswith("#"):
            name, version = line.split("==")
            pins[name.strip().lower()] = tuple(int(part) for part in version.strip().split(".")[:3])
    for name, fixed in {"pillow": (12, 3, 0), "werkzeug": (3, 1, 9), "urllib3": (2, 8, 0),
                        "anyio": (4, 14, 2), "idna": (3, 15), "pygments": (2, 20, 0),
                        "fsspec": (2026, 6, 0)}.items():
        assert pins[name] >= fixed, name


# -- A-53: stale or broken tooling -----------------------------------------------------------

def _setup_ai_models():
    import importlib.util
    before = list(sys.path)
    try:
        spec = importlib.util.spec_from_file_location("setup_ai_models_a53", ROOT / "tools" / "setup_ai_models.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = before                 # the tool puts tools/ on the path for itself
    return module


def test_setup_ai_models_fetches_only_pinned_and_hashed_files(tmp_path, monkeypatch, capsys):
    import json
    module = _setup_ai_models()
    source = (ROOT / "tools" / "setup_ai_models.py").read_text(encoding="utf-8")
    assert "refs/pr" not in source and "hf_hub_download" not in source
    for model_id in module.IMAGE_MODELS.values():
        for entry in model_catalog.MODELS[model_id]["files"]:
            assert len(entry["revision"]) == 40 and len(entry["sha256"]) == 64
    model_catalog.configure(tmp_path / "models")
    fetched = []
    monkeypatch.setattr(module, "fetch", lambda model_id: fetched.append(model_id) or True)

    assert module.main([]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[0])["models"] == ["generative", "segmentation"]
    assert fetched == [], "nothing is fetched without --download"

    assert module.main(["--download"]) == 0
    assert fetched == ["generative", "segmentation"]
    settings = json.loads((tmp_path / "models" / "settings.json").read_text())
    assert settings["image_model"] == str(tmp_path / "models" / "magicbrush")
    assert settings["segmentation_model"].endswith("model.onnx")
    assert settings["budget_bytes"] == 20_000_000_000


def test_setup_ai_models_refuses_a_checkpoint_it_cannot_check():
    module = _setup_ai_models()
    with pytest.raises(SystemExit, match="not fetched by this tool"):
        module.main(["--image-model", "instructpix2pix"])


def test_the_source_archive_reads_the_version_where_it_is_written(tmp_path):
    """pyproject.toml takes the version from ninaivu/__init__.py, so reading it
    from pyproject.toml alone failed on every real commit."""
    import subprocess
    import zipfile
    from tools.build_source_archive import build_archive

    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    git("init")
    (tmp_path / "pyproject.toml").write_text('[project]\ndynamic = ["version"]\n')
    (tmp_path / "ninaivu").mkdir()
    (tmp_path / "ninaivu" / "__init__.py").write_text('__version__ = "3.1.4"\n')
    git("add", ".")
    git("-c", "user.name=Ninaivu Test", "-c", "user.email=test@example.invalid", "commit", "-m", "x")
    output = build_archive(tmp_path)
    assert output.name.startswith("Ninaivu-3.1.4-")
    with zipfile.ZipFile(output) as archive:
        assert "Ninaivu-3.1.4/ninaivu/__init__.py" in archive.namelist()


def test_the_linux_build_compiles_with_the_python_it_bundles():
    build = (ROOT / "installers" / "linux" / "build.sh").read_text(encoding="utf-8")
    assert '"$host_python" != "${pbs_python%.*}"' in build


def test_the_service_files_point_at_this_project():
    unit = (ROOT / "installers" / "systemd" / "ninaivu.service").read_text(encoding="utf-8")
    assert "Documentation=https://github.com/javajaga-usa/Ninaivu" in unit
    from ninaivu.desktop import autostart
    with pytest.raises(RuntimeError) as raised:
        autostart.enable(root=ROOT, platform="linux")
    assert "deploy/systemd" not in str(raised.value)
    assert "installers/systemd/ninaivu.service" in str(raised.value)


# -- A-50: the Windows service's firewall rules ------------------------------------------------

def test_the_windows_service_opens_only_the_family_app_to_the_home_network():
    script = (ROOT / "installers" / "windows" / "install-service.ps1").read_text(encoding="utf-8")
    added = [line for line in script.splitlines() if "firewall add rule" in line]
    assert len(added) == 1 and "localport=5000" in added[0] and "remoteip=localsubnet" in added[0]
    uninstall = script[script.index('"Uninstall" {'):script.index('"Start" {')]
    assert 'delete rule name="Ninaivu Family App (5000)"' in uninstall
    assert 'delete rule name="Ninaivu Admin Console (3000)"' in uninstall
