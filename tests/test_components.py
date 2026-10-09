"""The Extras tab: installing what Ninaivu can run without, from the console.

The safety of this is the whole design, so most of these are about what a
request can and cannot cause: an id from a fixed list chooses a pinned command
written in Ninaivu's own source, and nothing a request carries reaches a shell.
"""

import sys
import time

import pytest

from ninaivu.media import components


@pytest.fixture(autouse=True)
def forget_installs(monkeypatch):
    monkeypatch.setattr(components, "installs", components.Installs())


# -- what a request can ask for ----------------------------------------------------

def test_only_what_is_in_the_catalogue(as_admin):
    assert as_admin.post("/api/admin/components/rm%20-rf/install").status_code == 404
    assert as_admin.post("/api/admin/components/torch/install").status_code == 404


def test_an_id_chooses_the_command_and_nothing_else():
    """The pinned requirement comes from the source, not from the request."""
    command = components.install_command("heif")
    assert command[0] == sys.executable
    assert command[1:4] == ["-m", "pip", "install"]
    assert command[4] == components.CATALOGUE["heif"]["requirement"]
    assert len(command) == 5


def test_the_family_port_does_not_route_it(scanned):
    from ninaivu import build_services, create_home_app

    cfg, _, _ = scanned
    cfg.watch = False
    services = build_services(cfg)
    services.scanner.stop()
    routes = {str(rule) for rule in create_home_app(services).url_map.iter_rules()}
    assert not [r for r in routes if r.startswith("/api/admin/components")]


def test_only_an_admin(as_family, as_guest):
    for client in (as_family, as_guest):
        assert client.get("/api/admin/components").status_code in (401, 403, 404)
        assert client.post("/api/admin/components/heif/install").status_code in (401, 403, 404)


# -- how each kind is fetched ------------------------------------------------------

def test_a_tool_comes_from_the_machines_package_manager(monkeypatch):
    monkeypatch.setattr(components.shutil, "which",
                        lambda name, path=None: "C:/winget.exe" if name == "winget" else None)
    monkeypatch.setattr(components.sys, "platform", "win32")
    assert components.available_manager() == "winget"
    command = components.install_command("ffmpeg")
    assert command[:4] == ["winget", "install", "--id", "Gyan.FFmpeg"]


def test_with_no_package_manager_it_says_so_rather_than_trying(monkeypatch):
    monkeypatch.setattr(components.shutil, "which", lambda name, path=None: None)
    monkeypatch.setattr(components.sys, "platform", "win32")
    with pytest.raises(ValueError, match="no package manager"):
        components.install_command("ffmpeg")
    described = components.describe("ffmpeg")
    assert described["cannot"] and described["command"] == ""
    assert described["manual"].startswith("https://")


def test_a_manager_that_needs_root_hands_the_command_over(monkeypatch):
    monkeypatch.setattr(components.sys, "platform", "linux")
    monkeypatch.setattr(components.shutil, "which",
                        lambda name, path=None: "/usr/bin/apt-get" if name == "apt-get" else None)
    with pytest.raises(ValueError, match="administrator rights"):
        components.install_command("ffmpeg")


class FakeInstaller:
    """Stands in for the installer process: lines out, then an exit code."""

    def __init__(self, lines, code=0, linger=0.0):
        self.lines = list(lines)
        self.code = code
        self.linger = linger

    def __call__(self, command, **kwargs):
        self.command = command
        return self

    def readline(self):
        return self.lines.pop(0) if self.lines else ""

    @property
    def stdout(self):
        return self

    def wait(self, timeout=None):
        if self.linger:
            time.sleep(self.linger)
        return self.code


def settled(component_id, seconds=5.0):
    """Wait for the install thread to finish, as the console's poll does."""
    for _ in range(int(seconds / 0.02)):
        if components.installs.state(component_id).get("status") != "installing":
            return components.installs.state(component_id)
        time.sleep(0.02)
    raise AssertionError(f"{component_id} never finished installing")


def test_installing_reports_what_the_installer_said(monkeypatch):
    monkeypatch.setattr(components, "refresh_tools", lambda: None)
    runner = FakeInstaller(["Downloading…\n", "Successfully installed\n"])
    assert components.installs.start("heif", ["pip", "install", "x"],
                                     runner=runner) is True
    state = settled("heif")
    assert state["status"] == "installed"
    assert "Successfully installed" in "\n".join(state["log"])


def test_an_installer_that_fails_says_why(monkeypatch):
    monkeypatch.setattr(components, "refresh_tools", lambda: None)
    runner = FakeInstaller(["No matching distribution found\n"], code=1)
    components.installs.start("ocr", ["pip", "install", "x"], runner=runner)
    state = settled("ocr")
    assert state["status"] == "failed"
    assert "code 1" in state["error"]
    assert "No matching distribution" in "\n".join(state["log"])


def test_two_installs_of_the_same_thing_do_not_overlap(monkeypatch):
    monkeypatch.setattr(components, "refresh_tools", lambda: None)
    runner = FakeInstaller(["working\n"], linger=1.0)
    assert components.installs.start("ocr", ["pip"], runner=runner) is True
    assert components.installs.start("ocr", ["pip"], runner=runner) is False


def test_something_already_installed_is_not_installed_again(monkeypatch):
    monkeypatch.setitem(components.CATALOGUE["heif"], "present", lambda: True)
    started, refusal = components.install("heif")
    assert started is False and "already installed" in refusal


# -- a tool installed a minute ago counts ------------------------------------------

def test_a_newly_installed_tool_is_found_without_a_restart(monkeypatch):
    """A package manager adds its folder to the environment for *new*
    processes; this one keeps the PATH it started with."""
    from ninaivu.media import media

    monkeypatch.setattr(components, "_stored_path", lambda: "C:/ffmpeg/bin")
    monkeypatch.setattr(components.shutil, "which",
                        lambda name, path=None: f"{path}/{name}.exe" if path else None)
    monkeypatch.setattr(media, "FFMPEG", None)
    components.refresh_tools()
    assert media.FFMPEG == "C:/ffmpeg/bin/ffmpeg.exe"

    from ninaivu.archive import entertainment
    assert entertainment.FFMPEG == media.FFMPEG, "the archive kept its own stale copy"


def test_a_mac_app_finds_homebrew_where_the_shell_would(monkeypatch):
    """Ninaivu opened from Finder or started at sign-in inherits only
    /usr/bin:/bin:/usr/sbin:/sbin. Homebrew lives in /opt/homebrew/bin, so the
    Extras page said there was no package manager and ffmpeg could not be
    installed, and an ffmpeg Homebrew had already installed was not seen."""
    from ninaivu.media import media

    monkeypatch.setattr(components.sys, "platform", "darwin")
    monkeypatch.setenv("PATH", components.os.pathsep.join(["/usr/bin", "/bin", "/usr/sbin", "/sbin"]))

    def which(name, path=None):
        if path and "/opt/homebrew/bin" in path.split(components.os.pathsep):
            return f"/opt/homebrew/bin/{name}"
        return None

    monkeypatch.setattr(components.shutil, "which", which)
    assert "/opt/homebrew/bin" in components._stored_path().split(components.os.pathsep)
    assert components.available_manager() == "brew"
    assert components.install_command("ffmpeg") == ["brew", "install", "ffmpeg"]
    folder = str(components.Path("/opt/homebrew/bin"))     # as find_tool writes it
    assert folder in components.os.environ["PATH"].split(components.os.pathsep), \
        "the install runs brew by name, so its folder has to be on the PATH"

    monkeypatch.setattr(media, "FFMPEG", None)
    components.refresh_tools()
    assert media.FFMPEG == "/opt/homebrew/bin/ffmpeg"


def test_windows_path_entries_written_with_variables_are_expanded(monkeypatch):
    """The user's PATH in the registry is REG_EXPAND_SZ: winget's folder for
    the ffmpeg it installs can be stored as "%LOCALAPPDATA%\\...", which
    shutil.which cannot look in until it is expanded."""
    import sys
    import types

    class Key:
        def __init__(self, value):
            self.value = value

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    fake = types.SimpleNamespace(
        HKEY_CURRENT_USER="user", HKEY_LOCAL_MACHINE="machine",
        OpenKey=lambda root, key: Key(r"%LOCALAPPDATA%\Microsoft\WinGet\Links"
                                      if root == "user" else r"C:\Windows"),
        QueryValueEx=lambda handle, name: (handle.value, 2),
    )
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(components.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\amma\AppData\Local")
    stored = components._stored_path()
    assert r"C:\Users\amma\AppData\Local\Microsoft\WinGet\Links" in stored
    assert "%" not in stored


# -- the console's own listing -----------------------------------------------------

def test_the_listing_says_what_is_missing_and_how(as_admin):
    body = as_admin.get("/api/admin/components").get_json()
    ids = {item["id"] for item in body["components"]}
    assert {"ffmpeg", "heif", "opencv", "exifread", "ocr"} <= ids
    for item in body["components"]:
        assert item["used_for"], item["id"]
        assert item["installed"] or item["command"] or item["cannot"], item["id"]


# ---------------------------------------------------------------------------
# Saying why an install failed
# ---------------------------------------------------------------------------
#
# "the installer stopped with code 1" above forty lines of pip output is true
# and useless. The one worth naming is the failure that trying again cannot
# fix: Windows will not replace a file a running program has open, and the
# program holding it is Ninaivu itself. Installing the text reader pulls a
# second build of OpenCV over the `cv2` the face and orientation models
# already have loaded, and pip dies on a permission error that has nothing to
# do with permissions.

DENIED = [
    r"C:\Github\Ninaivu\.venv\Scripts\python.exe -m pip install rapidocr>=3,<4",
    "Installing collected packages: opencv_python, rapidocr",
    "ERROR: Could not install packages due to an OSError: [WinError 5] Access "
    r"is denied: 'C:\Github\Ninaivu\.venv\Lib\site-packages\cv2\cv2.pyd'",
    "Check the permissions.",
]


def test_a_file_ninaivu_is_using_is_named_as_the_reason():
    reason = components._why_it_failed({"log": DENIED})
    assert "open" in reason and "Stop Ninaivu" in reason, reason
    # This page is part of Ninaivu and is gone once Ninaivu stops, so "install
    # it again" from here is not something anybody can do.
    assert "command shown above" in reason, reason


def test_the_reason_does_not_blame_the_household_s_permissions():
    """It reads as a permissions problem and is not one; saying "check the
    permissions" sends somebody to the wrong place for an afternoon."""
    reason = components._why_it_failed({"log": DENIED})
    assert "permission" not in reason.lower(), reason


def test_a_permission_error_outside_site_packages_is_not_claimed():
    """Only the case actually understood. A guess that reads as certain is
    worse than the exit code."""
    assert components._why_it_failed(
        {"log": [r"[WinError 5] Access is denied: 'D:\somewhere\else.txt'"]}) == ""


def test_no_build_for_this_python_is_named_as_that():
    assert "catch up" in components._why_it_failed(
        {"log": ["ERROR: No matching distribution found for rapidocr"]})


def test_a_timeout_is_worth_trying_again():
    assert "again" in components._why_it_failed(
        {"log": ["ERROR: Read timed out."]})


def test_anything_else_falls_back_to_the_exit_code():
    assert components._why_it_failed({"log": ["something nobody predicted"]}) == ""


def test_an_install_that_never_ran_says_nothing():
    assert components._why_it_failed({}) == ""


# ---------------------------------------------------------------------------
# The runtime the models on the AI page need
# ---------------------------------------------------------------------------

def test_the_onnx_runtime_can_be_installed_from_the_console():
    """Background removal downloaded a 176 MB model and then could not run it,
    because the one package it needs was not on the list the console installs
    from and nothing said so."""
    assert "onnxruntime" in components.CATALOGUE


def test_the_onnx_models_can_be_made_to_run_from_the_console():
    """Not every model's requirements belong here — the editing and search
    models want torch, transformers and diffusers, which are a requirements
    file and a deliberate decision, not a button. The ONNX ones are different:
    they are small, they are offered on the AI models page as though a click
    were enough, and one package stands between downloading them and using
    them."""
    from ninaivu.media import model_catalog

    onnx_models = [m for m in model_catalog.MODELS.values()
                   if "onnxruntime" in m["requires"]]
    assert onnx_models, "no model needs the ONNX runtime any more"
    for model in onnx_models:
        assert set(model["requires"]) <= set(components.CATALOGUE), model["label"]


# ---------------------------------------------------------------------------
# The text reader, installed without replacing the OpenCV Ninaivu has open
# ---------------------------------------------------------------------------
#
# rapidocr names `opencv_python`. Ninaivu already has `opencv-python-headless`,
# the same `cv2` built without a window system, so the requirement is met in
# every way that matters — but pip only knows distribution names and installs
# the second build over the first. On Windows that failed every time, because
# `cv2.pyd` is loaded by the face and orientation models and Windows will not
# replace an open file.

def test_the_text_reader_never_asks_for_a_second_opencv():
    joined = " ".join(" ".join(step) for step in components.install_steps("ocr"))
    assert "opencv" not in joined.lower(), joined


def test_its_dependencies_come_first_and_it_comes_last():
    """Stopping early must leave nothing half in place: a rapidocr whose
    dependencies failed would show as installed and not import."""
    steps = components.install_steps("ocr")
    assert len(steps) == 2
    assert "rapidocr>=3,<4" not in steps[0]
    assert steps[1][-1] == "rapidocr>=3,<4" and "--no-deps" in steps[1]


def test_the_runtime_it_reads_with_is_part_of_it():
    """rapidocr runs its models on the ONNX runtime without declaring it, so a
    plain install imports and then reads nothing."""
    first = " ".join(components.install_steps("ocr")[0])
    assert "onnxruntime" in first


def test_an_ordinary_package_is_still_one_step():
    assert len(components.install_steps("heif")) == 1


def test_the_page_shows_every_step():
    """Somebody who has to run it by hand needs all of it."""
    shown = components.describe("ocr")["command"]
    assert "--no-deps" in shown and "pyclipper" in shown, shown


def test_the_list_matches_what_rapidocr_itself_asks_for():
    """The list is copied from rapidocr's metadata, so a new rapidocr that adds
    a dependency would install without it. Checked against whichever rapidocr
    is actually installed here."""
    from importlib import metadata

    try:
        declared = metadata.requires("rapidocr") or []
    except metadata.PackageNotFoundError:
        pytest.skip("rapidocr is not installed here to compare against")

    def name(requirement: str) -> str:
        return requirement.split(";")[0].split("[")[0].strip().split(" ")[0] \
            .split("<")[0].split(">")[0].split("=")[0].split("!")[0].lower() \
            .replace("_", "-")

    wanted = {name(r) for r in declared if "extra ==" not in r}
    wanted.discard("opencv-python")
    listed = {name(r) for r in components.CATALOGUE["ocr"]["also"]}
    assert wanted <= listed, f"rapidocr now also wants {wanted - listed}"


def test_a_failed_step_stops_the_ones_after_it(monkeypatch):
    monkeypatch.setattr(components, "refresh_tools", lambda: None)
    ran: list[list[str]] = []

    def runner(command, **_):
        ran.append(command)
        return FakeInstaller(["no\n"], code=1)(command)

    components.installs.start("ocr", [["pip", "one"], ["pip", "two"]],
                              runner=runner)
    state = settled("ocr")
    assert state["status"] == "failed"
    assert ran == [["pip", "one"]], "the second step ran after the first failed"


def test_every_step_is_run_when_they_succeed(monkeypatch):
    monkeypatch.setattr(components, "refresh_tools", lambda: None)
    ran: list[list[str]] = []

    def runner(command, **_):
        ran.append(command)
        return FakeInstaller(["ok\n"])(command)

    components.installs.start("ocr", [["pip", "one"], ["pip", "two"]],
                              runner=runner)
    assert settled("ocr")["status"] == "installed"
    assert ran == [["pip", "one"], ["pip", "two"]]


# -- ffmpeg put there by something else counts, and stops being offered --------------

@pytest.fixture
def ffmpeg_missing(monkeypatch):
    """No ffmpeg anywhere, and a clock that has not been consulted yet."""
    from ninaivu.archive import entertainment
    from ninaivu.media import media

    monkeypatch.setattr(media, "FFMPEG", None)
    monkeypatch.setattr(entertainment, "FFMPEG", None)
    monkeypatch.setattr(components, "find_tool", lambda name: None)
    monkeypatch.setitem(components._tool_checked, "at", 0.0)
    return media


def test_ffmpeg_installed_outside_ninaivu_is_noticed(ffmpeg_missing, monkeypatch):
    """`brew install ffmpeg` in a Terminal, or winget in a prompt, while Ninaivu
    runs: the Extras page went on offering the install until a restart, because
    the path was read once, at start."""
    from ninaivu.archive import entertainment

    before = components.describe("ffmpeg")
    assert before["installed"] is False and before["status"] != "installed"

    monkeypatch.setattr(components, "find_tool",
                        lambda name: "/opt/homebrew/bin/ffmpeg" if name == "ffmpeg" else None)
    monkeypatch.setitem(components._tool_checked, "at", 0.0)      # the next look is allowed
    after = components.describe("ffmpeg")
    assert after["installed"] is True and after["status"] == "installed"
    assert after["needs_restart"] is False
    assert ffmpeg_missing.FFMPEG == "/opt/homebrew/bin/ffmpeg"
    assert entertainment.FFMPEG == "/opt/homebrew/bin/ffmpeg", "the archive kept its stale copy"
    # And it is not offered again.
    started, refusal = components.install("ffmpeg")
    assert started is False and "already installed" in refusal


def test_a_missing_ffmpeg_is_not_searched_for_on_every_poll(ffmpeg_missing, monkeypatch):
    looked = []
    monkeypatch.setattr(components, "find_tool", lambda name: looked.append(name))
    for _ in range(20):
        assert components.ffmpeg_available() is False
    assert looked.count("ffmpeg") == 1


def test_an_ffmpeg_that_was_removed_is_offered_again(ffmpeg_missing, monkeypatch, tmp_path):
    gone = str(tmp_path / "bin" / "ffmpeg")            # absolute, and not there
    monkeypatch.setattr(ffmpeg_missing, "FFMPEG", gone)
    assert components.ffmpeg_available() is False
    assert ffmpeg_missing.FFMPEG is None


def test_an_ffmpeg_still_there_is_not_looked_for_again(monkeypatch, tmp_path):
    from ninaivu.media import media

    present = tmp_path / "ffmpeg"
    present.write_bytes(b"")
    monkeypatch.setattr(media, "FFMPEG", str(present))
    monkeypatch.setattr(components, "find_tool",
                        lambda name: pytest.fail("looked again for what is there"))
    assert components.ffmpeg_available() is True


def test_windows_package_manager_folders_are_looked_in(monkeypatch, tmp_path):
    """winget's links folder, Gyan.FFmpeg's own, Scoop, Chocolatey and a plain
    C:\ffmpeg are on no PATH a running Ninaivu has."""
    packages = tmp_path / "Microsoft" / "WinGet" / "Packages" / "Gyan.FFmpeg_x" / "ffmpeg-7.1-full_build" / "bin"
    packages.mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "me"))
    monkeypatch.setenv("ChocolateyInstall", str(tmp_path / "choco"))
    folders = [components.os.path.normcase(f) for f in components._windows_tool_folders()]
    expected = [str(tmp_path / "Microsoft" / "WinGet" / "Links"), str(packages),
                str(tmp_path / "me" / "scoop" / "shims"), str(tmp_path / "choco" / "bin")]
    for folder in expected:
        assert components.os.path.normcase(folder) in folders, folder
