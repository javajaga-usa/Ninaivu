"""The Primary updates the Secondary (mugil/update.py, api/mugil_update_api.py).

The Linux portable download is sent over the pairing, signed; the Secondary
keeps it only as signed, looks inside before unpacking anything, unpacks it
beside the folder in use, waits for a quiet moment, and a helper outside the
server brings the library across and starts the newer version. When that
version ends twice before it ever served, the library goes back to the older
folder by itself and the older Ninaivu is started again. These run on
folders laid out as the Linux portable build lays them out.
"""

from __future__ import annotations

import io
import json
import sqlite3
import sys
import tarfile
import time
from contextlib import closing
from pathlib import Path

import pytest

from ninaivu import __version__
from ninaivu.desktop import portable
from ninaivu.mugil import update
from ninaivu.storage import upgrade
from test_mugil_two_computers import two_apps as two_apps  # noqa: F401 - a fixture

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"),
                                reason="the Secondary that is updated runs Linux")


def _tree(top: Path, version: str) -> Path:
    (top / "app" / "python" / "bin").mkdir(parents=True)
    (top / "app" / "python" / "bin" / "python3").write_text("#!/bin/sh\n")
    package = top / "app" / "python" / "lib" / "python3.12" / "site-packages" / "ninaivu"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(f'"""Ninaivu."""\n\n__version__ = "{version}"\n')
    (top / "ninaivu").write_text("#!/bin/sh\n")
    (top / "ninaivu").chmod(0o755)
    (top / "logs").mkdir()
    return top


def _download(where: Path, version: str = "9.0.0", processor: str = "x64", *,
              inside: str | None = None, extra=None) -> Path:
    """A Linux portable download, as the release builds it."""
    name = f"Ninaivu-{version}-linux-{processor}-portable"
    build = where / f"build-{version}-{processor}"
    _tree(build / name, inside or version)
    out = where / f"{name}.tar.gz"
    with tarfile.open(out, "w:gz") as tar:
        tar.add(build / name, arcname=name)
        if extra:
            extra(tar, name)
    return out


def _index(path: Path, words: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS assets (id INTEGER PRIMARY KEY, words TEXT)")
        conn.execute("INSERT INTO assets(words) VALUES (?)", (words,))
        conn.commit()


def _words(path: Path) -> str:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute("SELECT words FROM assets").fetchone()[0]


@pytest.fixture
def older(tmp_path):
    """The Secondary's portable folder in use, with its library."""
    home = tmp_path / "home"
    home.mkdir()
    path = _tree(home / "Ninaivu-2.2.0-linux-x64-portable", "2.2.0")
    state = path / "data"
    _index(state / "index.db", "Paati's 80th")
    (state / "thumbs").mkdir()
    (state / "thumbs" / "a.webp").write_bytes(b"thumb")
    (state / "config.json").write_text(json.dumps({"roots": ["/photos"]}))
    (state / "control").mkdir()
    (state / "ai-models").mkdir()
    (state / upgrade.VERSION_FILE).write_text(json.dumps({"version": "2.2.0"}))
    return portable._layout(path)


# -- looking inside -------------------------------------------------------------

def test_a_portable_download_is_read_for_what_it_is(tmp_path):
    info = update.look_inside(_download(tmp_path, "3.0.0", "arm64"))
    assert info == {"top": "Ninaivu-3.0.0-linux-arm64-portable", "version": "3.0.0",
                    "arch": "arm64"}


def _add_file(name, data=b"x"):
    def add(tar, top):
        member = tarfile.TarInfo(name.format(top=top))
        member.size = len(data)
        tar.addfile(member, io.BytesIO(data))
    return add


def _add_link(name, target):
    def add(tar, top):
        member = tarfile.TarInfo(name.format(top=top))
        member.type, member.linkname = tarfile.SYMTYPE, target
        tar.addfile(member)
    return add


@pytest.mark.parametrize("extra", [
    _add_file("{top}/../escaped.txt"),
    _add_file("/etc/cron.d/ninaivu"),
    _add_file("another-folder/a.txt"),
    _add_link("{top}/app/out", "/etc/passwd"),
    _add_link("{top}/app/out", "../../../elsewhere"),
])
def test_a_download_with_anything_outside_its_folder_is_refused(tmp_path, extra):
    with pytest.raises(update.UpdateRefused):
        update.look_inside(_download(tmp_path, extra=extra))


def test_a_link_inside_the_folder_is_fine(tmp_path):
    package = _download(tmp_path, extra=_add_link("{top}/app/python/bin/python", "python3"))
    assert update.look_inside(package)["version"] == "9.0.0"


def test_a_download_whose_name_and_contents_disagree_is_refused(tmp_path):
    with pytest.raises(update.UpdateRefused, match="holds Ninaivu 1.0.0"):
        update.look_inside(_download(tmp_path, "9.0.0", inside="1.0.0"))


def test_not_a_download_at_all_is_refused(tmp_path):
    junk = tmp_path / "photo.tar.gz"
    junk.write_bytes(b"\xff\xd8 not a tarball")
    with pytest.raises(update.UpdateRefused):
        update.look_inside(junk)


def test_only_a_newer_one_for_this_processor_is_taken(tmp_path):
    package = _download(tmp_path, "3.0.0", "arm64")
    with pytest.raises(update.UpdateRefused, match="arm64 processors"):
        update.check(package, mine="2.2.0", machine="x86_64")
    assert update.check(package, mine="2.2.0", machine="aarch64")["version"] == "3.0.0"
    with pytest.raises(update.UpdateRefused, match="already runs"):
        update.check(package, mine="3.0.0", machine="aarch64")


def test_it_unpacks_beside_under_a_name_of_its_own(tmp_path):
    package = _download(tmp_path, "3.0.0")
    info = update.look_inside(package)
    beside = tmp_path / "home"
    beside.mkdir()
    (beside / info["top"]).mkdir()                       # an earlier try, left there
    made = update.unpack(package, beside, info)
    assert made.name == f"{info['top']} (2)"
    assert portable._layout(made) is not None
    assert not [p for p in beside.iterdir() if p.name.startswith(".ninaivu-update-")]


# -- the Secondary taking it -------------------------------------------------------

def _updater(older, **kwargs):
    spawned = []
    kwargs.setdefault("quiet", lambda: True)
    made = update.Updater(older.state, folder=older, systemd=False, server_args=["--port", "80"],
                          spawn=lambda command, **how: spawned.append((command, how)),
                          sleep=lambda _s: None, version="2.2.0", machine="x86_64", **kwargs)
    return made, spawned


def _send(updater, package: Path, digest: str | None = None):
    data = package.read_bytes()
    return updater.receive(io.BytesIO(data), len(data), digest or update.digest_of(package))


def test_the_secondary_unpacks_it_waits_for_quiet_and_hands_over(tmp_path, older):
    asked = iter([False, False, True])
    updater, spawned = _updater(older, quiet=lambda: next(asked))
    package = _download(tmp_path, "3.0.0")
    assert _send(updater, package)["stage"] == "unpacking"
    updater.join(10)
    newer = older.path.parent / "Ninaivu-3.0.0-linux-x64-portable"
    assert portable._layout(newer) is not None
    assert not (older.path.parent / ".ninaivu-update.tar.gz").exists()
    assert len(spawned) == 1
    command, how = spawned[0]
    assert command[1:4] == ["-m", "ninaivu.mugil.update", "finish"]
    assert command[command.index("--old") + 1] == str(older.path)
    assert command[command.index("--new") + 1] == str(newer)
    assert json.loads(command[command.index("--args") + 1]) == ["--port", "80"]
    # Run by the older folder's Python, the one known to start here.
    assert command[0] == sys.executable and how["systemd"] is False
    said = update.record(older.state)
    assert said["stage"] == "switching" and said["to"] == "3.0.0" and said["from"] == "2.2.0"


def test_a_download_changed_on_the_way_is_not_kept(tmp_path, older):
    updater, spawned = _updater(older)
    with pytest.raises(update.UpdateRefused, match="changed on the way"):
        _send(updater, _download(tmp_path, "3.0.0"), digest="0" * 64)
    assert not spawned and update.record(older.state)["stage"] == "failed"
    assert [p.name for p in older.path.parent.iterdir()] == [older.path.name]


def test_an_older_download_is_refused_and_nothing_is_unpacked(tmp_path, older):
    updater, spawned = _updater(older)
    with pytest.raises(update.UpdateRefused, match="already runs"):
        _send(updater, _download(tmp_path, "2.1.0"))
    assert [p.name for p in older.path.parent.iterdir()] == [older.path.name]
    assert update.record(older.state)["stage"] == "refused"


def test_a_secondary_not_run_from_a_portable_folder_says_so(tmp_path):
    updater = update.Updater(tmp_path, folder=None, quiet=lambda: True, systemd=False)
    assert "portable" in updater.refusal()
    with pytest.raises(update.UpdateRefused, match="portable"):
        updater.receive(io.BytesIO(b"x"), 1, "0")


def test_the_helper_is_started_outside_the_service_under_systemd(tmp_path):
    ran = []

    class Done:
        returncode, stdout, stderr = 0, "", ""

    update.start_helper(["python", "-m", "ninaivu.mugil.update"], systemd=True, logs=tmp_path,
                        runner=lambda command, **_: ran.append(command) or Done())
    assert ran[0][:4] == ["systemd-run", "--user", "--collect", "--quiet"]
    assert ran[0][-3:] == ["python", "-m", "ninaivu.mugil.update"]


# -- the helper ---------------------------------------------------------------------

class FakeService:
    """The service, started and stopped; *serves* says whether a start of the
    newer folder gets as far as serving."""

    def __init__(self, serves: bool, version: str = "3.0.0", prepare: bool = False):
        self.serves, self.version, self.prepare = serves, version, prepare
        self.calls: list[tuple[str, str]] = []
        self.started = None
        self.ended = 0

    def stop(self, folder):
        self.calls.append(("stop", folder.path.name))

    def start(self, folder):
        self.calls.append(("start", folder.path.name))
        self.started, self.ended = folder, 0
        if folder.path.name.startswith("Ninaivu-3.0.0") and self.prepare:
            upgrade.prepare(folder.state, version=self.version)
        if self.serves and folder.path.name.startswith("Ninaivu-3.0.0"):
            upgrade.prepare(folder.state, version=self.version)
            upgrade.note_good_start(folder.state)

    def failures(self):
        if not self.serves:
            self.ended += 1
        return self.ended


def _newer(tmp_path, older):
    package = _download(tmp_path, "3.0.0")
    return update.unpack(package, older.path.parent, update.look_inside(package))


def test_the_helper_brings_the_library_across_and_the_newer_one_serves(tmp_path, older):
    newer = _newer(tmp_path, older)
    service = FakeService(serves=True)
    assert update.finish(older.path, newer, "3.0.0", service=service, sleep=lambda _s: None) == "done"
    assert service.calls == [("stop", older.path.name), ("start", newer.name)]
    assert _words(newer / "data" / "index.db") == "Paati's 80th"
    assert not (older.path / "data" / "index.db").exists()
    assert update.record(newer / "data")["stage"] == "done"


@pytest.mark.parametrize("prepare", [False, True], ids=["ended-at-once", "after-the-update-copy"])
def test_a_version_that_cannot_start_twice_gives_the_library_back(tmp_path, older, prepare):
    newer = _newer(tmp_path, older)
    service = FakeService(serves=False, prepare=prepare)
    ended = update.finish(older.path, newer, "3.0.0", service=service, sleep=lambda _s: None)
    assert ended == "went_back"
    assert service.calls == [("stop", older.path.name), ("start", newer.name),
                             ("stop", newer.name), ("start", older.path.name)]
    assert _words(older.path / "data" / "index.db") == "Paati's 80th"
    # As the older version left it: no record of the newer one is left behind.
    assert (upgrade.recorded(older.path / "data") or {}).get("version", "2.2.0") == "2.2.0"
    assert not (newer / "data" / "index.db").exists()
    said = update.record(older.path / "data")
    assert said["stage"] == "went_back" and "could not start" in said["message"]


def test_a_slow_start_is_left_running(tmp_path, older):
    newer = _newer(tmp_path, older)

    class Slow(FakeService):
        def failures(self):
            return 0

    now = [0.0]

    def tick(seconds):
        now[0] += seconds

    service = Slow(serves=False)
    ended = update.finish(older.path, newer, "3.0.0", service=service, sleep=tick,
                          clock=lambda: now[0], patience=30)
    assert ended == "slow" and service.calls[-1] == ("start", newer.name)
    assert _words(newer / "data" / "index.db") == "Paati's 80th"


def test_the_service_is_pointed_at_the_newer_folder_and_back(tmp_path, older):
    from ninaivu.cli import mugil as mugil_cli
    env = update.folder_environment(older)
    text = mugil_cli.unit_text(mugil_cli.start_command(env), env)
    newer = portable._layout(_newer(tmp_path, older))
    moved = update.unit_for(text, older, newer)
    assert str(older.path) not in moved
    assert f"ExecStart={newer.path}/ninaivu --server" in moved
    assert f'Environment="NINAIVU_PORTABLE_ROOT={newer.path}"' in moved
    # A unit somebody wrote by hand that names neither is written afresh.
    assert "ExecStart=" in update.unit_for("[Service]\nExecStart=/opt/x\n", older, newer)


def test_systemd_counts_each_time_it_ended(tmp_path):
    states = iter(["ActiveState=activating\nSubState=start\n",
                   "ActiveState=activating\nSubState=auto-restart\n",
                   "ActiveState=activating\nSubState=auto-restart\n",
                   "ActiveState=active\nSubState=running\n",
                   "ActiveState=failed\nSubState=failed\n"])

    class Done:
        returncode, stderr = 0, ""

        def __init__(self, out=""):
            self.stdout = out

    def runner(command, **_):
        return Done(next(states) if "show" in command else "")

    service = update.Service(runner=runner, home=tmp_path)
    assert [service.failures() for _ in range(5)] == [0, 1, 1, 1, 2]


def test_without_systemd_the_helper_starts_it_again_once(tmp_path, older):
    started = []

    class Child:
        def poll(self):
            return 1

    def popen(command, **_):
        started.append(command)
        return Child()

    process = update.Process(["--port", "80"], popen=popen)
    process.start(older)
    assert started[0] == [str(older.path / "ninaivu"), "--server", "--port", "80"]
    assert process.failures() == 1 and len(started) == 2
    assert process.failures() == 2 and len(started) == 2


# -- the Primary sending it ------------------------------------------------------------

def test_the_primary_takes_only_the_download_of_its_own_version(tmp_path):
    sender = update.Sender(tmp_path, lambda: None, version="3.0.0")
    data = _download(tmp_path, "2.9.0").read_bytes()
    with pytest.raises(update.UpdateRefused, match="Ninaivu 3.0.0, the version"):
        sender.take(io.BytesIO(data), len(data))
    assert not list((tmp_path / "mugil-update").iterdir())


def test_from_the_primary_page_to_the_secondary_end_to_end(two_apps, tmp_path, older, monkeypatch):
    from conftest import ADMIN, login
    from test_mugil_two_computers import partner_through  # noqa: PLC0415
    from ninaivu.api import mugil_update_api
    primary_app, secondary_app, other = two_apps
    second = login(secondary_app.test_client(), *ADMIN)
    first = login(primary_app.test_client(), *ADMIN)
    folder = tmp_path / "n300"
    folder.mkdir()
    second.post("/api/admin/mugil/role", json={"role": "secondary", "originals_dir": str(folder)})
    first.post("/api/admin/mugil/role", json={"role": "primary"})
    first.post("/api/admin/mugil/look")
    assert first.post("/api/admin/mugil/pair", json={"found": "linux-pc-id"}).status_code == 200
    monkeypatch.setattr(mugil_update_api, "Partner", partner_through(secondary_app.test_client()))

    spawned = []
    secondary_app.config["MV_SERVICES"].mugil_updater = update.Updater(
        other.state_dir, folder=older, quiet=lambda: True, systemd=False, server_args=[],
        spawn=lambda command, **_: spawned.append(command), version="1.0.0", machine="x86_64")

    data = _download(tmp_path, __version__).read_bytes()
    sent = first.post("/api/admin/mugil/update", data=data,
                      headers={"Content-Type": "application/octet-stream",
                               "X-Requested-With": "fetch"})
    assert sent.status_code == 200, sent.get_json()
    sender = primary_app.config["MV_SERVICES"].mugil_update_sender
    sender._thread.join(30)
    assert sender.status()["stage"] == "sent", sender.status()
    assert sender.status()["sent"] == len(data)
    secondary_app.config["MV_SERVICES"].mugil_updater.join(30)
    assert spawned, "the Secondary handed the update over"
    shown = first.get("/api/admin/mugil/update").get_json()
    assert shown["online"] is True and shown["secondary"]["stage"] == "switching"
    assert shown["secondary"]["to"] == __version__


def test_an_unsigned_download_is_refused_by_the_secondary(two_apps, tmp_path):
    from conftest import ADMIN, login
    primary_app, secondary_app, other = two_apps
    second = login(secondary_app.test_client(), *ADMIN)
    folder = tmp_path / "n300"
    folder.mkdir()
    assert second.post("/api/admin/mugil/role", json={
        "role": "secondary", "originals_dir": str(folder)}).status_code == 200
    answer = secondary_app.test_client().put("/api/mugil/update", data=b"x" * 100,
                                             headers={"X-Requested-With": "x"})
    assert answer.status_code == 401
    assert not (Path(other.state_dir) / update.RECORD).exists()


def test_a_single_computer_has_none_of_it(two_apps):
    from conftest import ADMIN, login
    primary_app, _secondary_app, _other = two_apps
    first = login(primary_app.test_client(), *ADMIN)
    assert first.get("/api/admin/mugil/update").status_code == 404
    assert first.post("/api/admin/mugil/update", data=b"x",
                      headers={"X-Requested-With": "fetch"}).status_code == 409


def test_the_record_travels_with_the_library(tmp_path):
    update.note(tmp_path, "receiving", to="3.0.0")
    update.note(tmp_path, "done")
    said = update.record(tmp_path)
    assert said["to"] == "3.0.0" and said["stage"] == "done" and said["finished_at"] >= time.time() - 5
    assert not update.in_progress(tmp_path)


def test_a_newer_launcher_that_will_not_start_is_gone_back_from_for_real(tmp_path, older):
    """No systemd: the helper starts the newer folder's own launcher, which
    ends at once, twice; the library goes back and the older one is started."""
    newer = _newer(tmp_path, older)
    (newer / "ninaivu").write_text("#!/bin/sh\necho 'a package that does not load here' >&2\nexit 1\n")
    (older.path / "ninaivu").write_text(f"#!/bin/sh\necho started >> '{tmp_path}/older-started'\n")
    (older.path / "ninaivu").chmod(0o755)
    ended = update.finish(older.path, newer, "3.0.0", service=update.Process([]),
                          sleep=lambda _s: time.sleep(0.2))
    assert ended == "went_back"
    assert _words(older.path / "data" / "index.db") == "Paati's 80th"
    assert "does not load here" in (newer / "logs" / "server.log").read_text()
    for _ in range(50):
        if (tmp_path / "older-started").exists():
            break
        time.sleep(0.1)
    assert (tmp_path / "older-started").read_text().strip() == "started"
