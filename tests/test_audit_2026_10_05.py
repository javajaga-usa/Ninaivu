"""Findings from the project audit of 5 October 2026, each held to its fix.

H-01: a restore took a damaged file already on disk as restored.
H-02: a restore followed a linked folder out of the destination.
H-03: a video whose location could not be removed was sent as it was.
M-01: a safety check that could not run still read "Everything is safe."
M-02: a JSON body sent without a length was read whole, past its limit.
M-03: the Server page failed when the system would not say its memory.
M-04: a notice that failed to send was kept quiet for six hours anyway.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ninaivu.cloud import keyring, restore
from ninaivu.storage import db
from test_cloud_restore import run, upload
from test_cloud_restore import world as world                          # noqa: F401
from test_offsite import lib as lib                                    # noqa: F401
from test_offsite import library_files
from test_offsite import run as run_offsite


def _link(link: Path, to: Path) -> None:
    try:
        os.symlink(to, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this system will not make a symbolic link here")


# -- H-01: Google Drive -------------------------------------------------------

def test_an_encrypted_backup_does_not_call_a_damaged_file_restored(world):
    """An encrypted backup's checksum is of the ciphertext, so the file on
    disk was judged by its size alone, and a damaged photograph is the size
    it was."""
    db_path, _, client, library, files, _ = world
    key = upload(world, encrypted=True)
    damaged = bytearray(files["2021/garden.jpg"])
    damaged[:4] = b"BAD!"
    (library / "2021/garden.jpg").write_bytes(bytes(damaged))

    state = run(restore.RestoreJob(connect=lambda: client,
                                   items=restore.from_record(db.connect(db_path)),
                                   destination=None, key=key))

    assert state["beside"] == 1 and state["already_there"] == 2, state
    assert (library / "2021/garden (restored).jpg").read_bytes() == files["2021/garden.jpg"]
    assert (library / "2021/garden.jpg").read_bytes() == bytes(damaged), "overwrote a file"


def test_an_intact_file_is_still_already_there_with_an_encrypted_backup(world):
    db_path, _, client, library, files, _ = world
    key = upload(world, encrypted=True)

    state = run(restore.RestoreJob(connect=lambda: client,
                                   items=restore.from_record(db.connect(db_path)),
                                   destination=None, key=key))

    assert state["already_there"] == 3 and state["beside"] == 0, state
    assert not list(library.rglob("*(restored)*"))
    assert not (library / restore.STAGING).exists(), "staging was left behind"


# -- H-02: Google Drive ---------------------------------------------------------

def test_a_linked_folder_in_the_destination_is_not_followed_out(world):
    db_path, _, client, _, _, tmp_path = world
    upload(world)
    destination, outside = tmp_path / "dest", tmp_path / "outside"
    destination.mkdir()
    outside.mkdir()
    _link(destination / "2019", outside)

    state = run(restore.RestoreJob(connect=lambda: client,
                                   items=restore.from_record(db.connect(db_path)),
                                   destination=destination))

    assert state["failed"] == 2 and state["restored"] == 1, state
    assert "outside the destination" in state["problems"][0]["why"]
    assert not list(outside.rglob("*")), "wrote outside the destination"


def test_a_linked_staging_folder_is_not_followed_out(world):
    db_path, _, client, _, _, tmp_path = world
    upload(world)
    destination, outside = tmp_path / "dest", tmp_path / "outside"
    destination.mkdir()
    outside.mkdir()
    _link(destination / restore.STAGING, outside)

    state = run(restore.RestoreJob(connect=lambda: client,
                                   items=restore.from_record(db.connect(db_path)),
                                   destination=destination))

    assert state["failed"] == 3 and state["restored"] == 0, state
    assert not list(outside.rglob("*"))


# -- H-01 and H-02: the off-site copy -------------------------------------------

def _recovery(setup) -> Path:
    path = setup["tmp"] / "recovery.json"
    path.write_text(json.dumps(keyring.recovery_document(keyring.load(setup["cfg"].state_dir))))
    return path


def test_the_off_site_restore_brings_back_a_damaged_file_of_the_same_size(lib):
    run_offsite(lib["offsite"])
    out = lib["tmp"] / "restored"
    run_offsite(lib["offsite"], lambda: lib["offsite"].restore(str(out)))
    victim = next(p for p in out.rglob("*.jpg") if "(restored)" not in p.name)
    good = victim.read_bytes()
    victim.write_bytes(b"\0" * len(good))

    status = run_offsite(lib["offsite"], lambda: lib["offsite"].restore(str(out)))

    assert status["sent"] == 1 and status["failed"] == 0, status
    assert victim.with_name(f"{victim.stem} (restored){victim.suffix}").read_bytes() == good
    assert victim.read_bytes() == b"\0" * len(good), "overwrote a file"
    assert "already there" in status["message"]


def test_the_off_site_restore_checks_files_against_an_older_manifest(lib):
    """A manifest from before it carried each file's SHA-256: the copy is
    fetched and compared, so an intact file is still already there."""
    offsite = lib["offsite"]
    run_offsite(offsite)
    conn = offsite._db()
    conn.execute("UPDATE offsite_copies SET digest=''")
    conn.commit()
    offsite._manifest(conn, offsite.target(), *offsite._key())
    out = lib["tmp"] / "restored"
    run_offsite(lib["offsite"], lambda: lib["offsite"].restore(str(out)))
    before = {p: p.read_bytes() for p in out.rglob("*") if p.is_file()}

    status = run_offsite(lib["offsite"], lambda: lib["offsite"].restore(str(out)))

    assert status["sent"] == 0 and status["failed"] == 0, status
    assert {p: p.read_bytes() for p in out.rglob("*") if p.is_file()} == before


def test_the_off_site_tool_does_not_skip_a_wrong_file(lib, capsys):
    """It skipped anything already at the path, a wrong-sized file included,
    and said all was well."""
    from ninaivu.cli import offsite_restore as tool
    run_offsite(lib["offsite"])
    out = lib["tmp"] / "elsewhere"
    copy = str(Path(lib["cfg"].offsite_folder))
    first = sorted(library_files(lib["cfg"]))[0]
    (out / first).parent.mkdir(parents=True)
    (out / first).write_bytes(b"something much longer, and not the photograph at all")

    code = tool.main(["--recovery", str(_recovery(lib)), copy, str(out)])

    assert code == 0
    wanted = library_files(lib["cfg"])[first]
    beside = (out / first).with_name(f"{Path(first).stem} (restored){Path(first).suffix}")
    assert beside.read_bytes() == wanted
    capsys.readouterr()
    assert tool.main(["--recovery", str(_recovery(lib)), copy, str(out)]) == 0
    assert "already there" in capsys.readouterr().out


def test_the_off_site_tool_does_not_follow_a_linked_folder_out(lib):
    from ninaivu.cli import offsite_restore as tool
    run_offsite(lib["offsite"])
    out, outside = lib["tmp"] / "elsewhere", lib["tmp"] / "outside"
    out.mkdir()
    outside.mkdir()
    top = sorted(library_files(lib["cfg"]))[0].split("/")[0]
    _link(out / top, outside)

    code = tool.main(["--recovery", str(_recovery(lib)),
                      str(Path(lib["cfg"].offsite_folder)), str(out)])

    assert code == 1
    assert not list(outside.rglob("*")), "wrote outside the destination"


# -- H-03: a video's metadata, failing closed ---------------------------------

ORIGINAL = b"\x00\x00\x00\x18ftypmp42 the original, with GPS in it"


@pytest.fixture()
def clip(scanned):
    """A video in the library, as the index knows it."""
    cfg, conn, _ = scanned
    library = Path(cfg.active_root)
    (library / "misc" / "clip.mp4").write_bytes(ORIGINAL)
    asset_id = conn.execute(
        "INSERT INTO assets(root, rel_path, filename, folder, ext, kind, size, duration, "
        "visibility) VALUES(?,?,?,?,?,?,?,?,0)",
        (str(library), "misc/clip.mp4", "clip.mp4", "misc", "mp4", "video",
         len(ORIGINAL), 2.0)).lastrowid
    conn.commit()
    return asset_id


def _no_ffmpeg(monkeypatch):
    from ninaivu.media import stripped_video
    monkeypatch.setattr(stripped_video, "stripped_copy", lambda *a, **k: None)


def test_a_guest_is_not_sent_a_video_that_could_not_be_stripped(app, people, as_guest,
                                                                clip, monkeypatch):
    _no_ffmpeg(monkeypatch)
    answer = as_guest.get(f"/api/file/{clip}")
    assert answer.status_code == 409
    assert ORIGINAL not in answer.data


def test_a_guest_is_sent_the_stripped_copy_when_there_is_one(app, people, as_guest, clip,
                                                             monkeypatch, tmp_path):
    from ninaivu.media import stripped_video
    copy = tmp_path / "copy.mp4"
    copy.write_bytes(b"\x00\x00\x00\x18ftypmp42 no metadata")
    monkeypatch.setattr(stripped_video, "stripped_copy", lambda *a, **k: copy)
    answer = as_guest.get(f"/api/file/{clip}")
    assert answer.status_code == 200 and answer.data == copy.read_bytes()


def test_a_share_link_is_not_sent_a_video_that_could_not_be_stripped(app, people, as_family,
                                                                      clip, monkeypatch):
    _no_ffmpeg(monkeypatch)
    token = as_family.post("/api/shares", json={"scope": "asset", "target_id": clip}
                           ).get_json()["token"]
    answer = app.test_client().get(f"/api/share/{token}/file/{clip}")
    assert answer.status_code == 409
    assert ORIGINAL not in answer.data


def test_a_family_member_is_not_sent_a_video_filmed_at_home_that_could_not_be_stripped(
        app, people, as_family, as_admin, clip, scanned, monkeypatch):
    cfg, _, _ = scanned
    _no_ffmpeg(monkeypatch)
    monkeypatch.setattr(cfg, "strip_location", "all", raising=False)
    answer = as_family.get(f"/api/file/{clip}")
    assert answer.status_code == 409
    assert ORIGINAL not in answer.data
    # An administrator is always given the original.
    assert as_admin.get(f"/api/file/{clip}").data == ORIGINAL


def test_a_guest_is_not_sent_a_live_photos_clip_that_could_not_be_stripped(
        app, people, as_guest, scanned, monkeypatch):
    cfg, conn, _ = scanned
    _no_ffmpeg(monkeypatch)
    library = Path(cfg.active_root)
    photo = conn.execute("SELECT id, rel_path FROM assets WHERE kind='picture' "
                         "AND rel_path LIKE 'shared/%' LIMIT 1").fetchone()
    clip_rel = str(Path(photo["rel_path"]).with_suffix(".mov").as_posix())
    (library / clip_rel).write_bytes(ORIGINAL)
    conn.execute("UPDATE assets SET live_video_path=?, is_live=1, visibility=0 WHERE id=?",
                 (clip_rel, photo["id"]))
    conn.commit()
    answer = as_guest.get(f"/api/live-video/{photo['id']}")
    assert answer.status_code == 409, answer.status_code
    assert ORIGINAL not in answer.data


# -- M-01: a check that could not run is not a check that passed ----------------

def _safety_with(monkeypatch, *statuses):
    from ninaivu.server import safety

    def make(n, status):
        def one(services, now):
            if status == "raise":
                raise RuntimeError("database unavailable")
            return safety._check(f"c{n}", "A check", status, "Said.", "health")
        one.__name__ = f"c{n}"
        return one

    monkeypatch.setattr(safety, "CHECKS", [make(n, s) for n, s in enumerate(statuses)])
    return safety.Safety(services=None).report(fresh=True)


def test_a_check_that_failed_does_not_read_everything_is_safe(monkeypatch):
    report = _safety_with(monkeypatch, "ok", "off", "raise")
    assert report["verdict"] == "unknown"
    assert report["headline"] != "Everything is safe."
    assert "could not be checked" in report["headline"]
    assert any(c["status"] == "unknown" for c in report["checks"])


@pytest.mark.parametrize("statuses, verdict", [
    (("ok", "off"), "ok"),
    (("ok", "unknown", "unknown"), "unknown"),
    (("attention", "unknown"), "attention"),
    (("problem", "raise"), "problem"),
])
def test_the_verdict_is_the_worst_of_what_was_found(monkeypatch, statuses, verdict):
    assert _safety_with(monkeypatch, *statuses)["verdict"] == verdict


# -- M-02: a body's limit holds whether or not it says how long it is ------------

def _unsized(app, path, body: bytes, content_type="application/json", method="POST"):
    """A request with no Content-Length, its body read to the end of the stream,
    as a chunked request reaches the app."""
    import io

    from werkzeug.test import create_environ
    environ = create_environ(path, method=method, content_type=content_type)
    environ.pop("CONTENT_LENGTH", None)
    environ["wsgi.input"] = io.BytesIO(body)
    environ["wsgi.input_terminated"] = True
    return app.request_context(environ)


def test_a_json_body_without_a_length_is_still_held_to_its_limit(app):
    from werkzeug.exceptions import RequestEntityTooLarge

    from ninaivu.api import _body
    big = json.dumps("x" * (_body.JSON_MAX_BYTES + 1)).encode()
    with _unsized(app, "/api/anything", big):
        from flask import request
        assert request.content_length is None
        with pytest.raises(RequestEntityTooLarge):
            _body.json_object()


def test_a_small_json_body_without_a_length_is_read(app):
    from ninaivu.api import _body
    with _unsized(app, "/api/anything", b'{"a": 1}'):
        from flask import request
        assert _body.json_object() == {"a": 1}
        assert request.get_json() == {"a": 1}, "the body is still there afterwards"


def test_a_phone_backup_piece_without_a_length_is_held_to_its_limit(app, monkeypatch):
    from werkzeug.exceptions import RequestEntityTooLarge

    from ninaivu.api import _body
    with _unsized(app, "/x", b"y" * 1001, "application/octet-stream", "PUT"):
        with pytest.raises(RequestEntityTooLarge):
            _body.read_at_most(1000)
    with _unsized(app, "/x", b"y" * 1000, "application/octet-stream", "PUT"):
        assert _body.read_at_most(1000) == b"y" * 1000


# -- M-03: a reading the system will not give does not take the page down -------

def _refuse(*args, **kwargs):
    raise OSError("not permitted here")


def test_the_performance_report_comes_back_without_swap(as_admin, monkeypatch):
    psutil = pytest.importorskip("psutil")
    from ninaivu.server import capacity
    monkeypatch.setattr(psutil, "swap_memory", _refuse)
    assert capacity.load()["swap_used_bytes"] is None
    answer = as_admin.get("/api/admin/performance")
    assert answer.status_code == 200
    assert answer.get_json()["machine"]["logical_cores"] >= 1


def test_the_server_page_comes_back_when_children_cannot_be_listed(as_admin, monkeypatch):
    psutil = pytest.importorskip("psutil")

    def no_children(self, *args, **kwargs):
        raise PermissionError("not permitted here")

    monkeypatch.setattr(psutil.Process, "children", no_children)
    answer = as_admin.get("/api/admin/server")
    assert answer.status_code == 200
    assert answer.get_json()["metrics"]["process"]["processes"] >= 1


def test_the_server_page_comes_back_without_memory(as_admin, monkeypatch):
    psutil = pytest.importorskip("psutil")
    monkeypatch.setattr(psutil, "virtual_memory", _refuse)
    answer = as_admin.get("/api/admin/server")
    assert answer.status_code == 200
    assert answer.get_json()["metrics"]["memory"]["percent"] is None


# -- M-04: a notice that reached nobody is not kept quiet ------------------------

def _notifier(transport):
    from ninaivu.utils import notify
    scheduled = []
    notifier = notify.Notifier(webhook_url="https://example.invalid/hook",
                               transport=transport,
                               later=lambda delay, work: scheduled.append((delay, work)))
    return notifier, scheduled


def test_a_notice_that_failed_goes_as_soon_as_sending_works_again():
    def down(*args):
        raise OSError("mail server unreachable")

    sent = []
    notifier, _ = _notifier(down)
    assert notifier.send("integrity", "A file changed")["sent"] is False

    notifier.transport = lambda *args: sent.append(args)
    answer = notifier.send("integrity", "A file changed")
    assert answer["sent"] is True, answer
    assert len(sent) == 1
    assert notifier.send("integrity", "A file changed")["reason"] == "already reported recently"


def test_a_notice_that_failed_is_tried_again_without_another_report():
    from ninaivu.utils import notify
    outcomes = [OSError("down"), None]
    sent = []

    def flaky(*args):
        outcome = outcomes.pop(0)
        if outcome is not None:
            raise outcome
        sent.append(args)

    notifier, scheduled = _notifier(flaky)
    notifier.send("cloud_stalled", "The backup has stalled")
    assert [delay for delay, _ in scheduled] == [notify.RETRY_DELAYS[0]]
    scheduled.pop()[1]()                                   # the retry, when it comes due
    assert len(sent) == 1 and not scheduled


def test_retries_stop_after_a_few():
    from ninaivu.utils import notify

    def down(*args):
        raise OSError("down")

    notifier, scheduled = _notifier(down)
    notifier.send("cloud_stalled", "The backup has stalled")
    tries = 0
    while scheduled:
        scheduled.pop()[1]()
        tries += 1
    assert tries == len(notify.RETRY_DELAYS)


def test_two_reports_at_once_send_one_notice():
    import threading
    started, release = threading.Event(), threading.Event()
    sent = []

    def slow(*args):
        started.set()
        release.wait(5)
        sent.append(args)

    notifier, _ = _notifier(slow)
    first = threading.Thread(target=notifier.send, args=("integrity", "A file changed"))
    first.start()
    started.wait(5)
    assert notifier.send("integrity", "A file changed")["reason"] == "being sent now"
    release.set()
    first.join(5)
    assert len(sent) == 1


# -- M-07: stopping the watchers never takes the process down ---------------------

WATCH_CYCLES = r"""
import sys, threading
from pathlib import Path
from ninaivu.server.config import Config
from ninaivu.media.scanner import Scanner

base = Path(sys.argv[1])
cfg = Config()
cfg.state_dir = base / "state"
first, second = base / "one", base / "two"
for folder in (first, second):
    (folder / "2024").mkdir(parents=True)
cfg.roots = [str(first)]
cfg.active_root = str(first)
cfg.ai_enabled = False
cfg.ai_engine = "off"
cfg.ai_models_dir = str(base / "ai-models")
cfg.min_media_bytes = 0
cfg.ensure_dirs()
cfg.add_library(str(second))
cfg.watch = True
for n in range(6):
    scanner = Scanner(cfg)
    if n % 2:
        scanner.start(str(second))             # a scan, and the watchers with it
    else:
        racing = threading.Thread(target=scanner.watch)
        racing.start()
        scanner.watch()                         # two threads starting watchers at once
        racing.join()
    scanner.stop(join=True)                     # at once, while they are new
    assert not scanner._observers
print("stopped cleanly")
"""


def test_watchers_stop_cleanly_in_a_process_of_their_own(tmp_path):
    """In its own process, so a native crash (a segmentation fault in the
    macOS FSEvents watcher was seen on stopping) fails this test rather than
    ending the whole run."""
    import subprocess
    import sys
    done = subprocess.run([sys.executable, "-c", WATCH_CYCLES, str(tmp_path)],
                          capture_output=True, text=True, timeout=240,
                          cwd=Path(__file__).resolve().parents[1])
    assert done.returncode == 0, (done.returncode, done.stderr[-2000:])
    assert "stopped cleanly" in done.stdout


# -- Other observations ------------------------------------------------------------

INSTALLERS = Path(__file__).resolve().parents[1] / "installers"


def test_every_bundled_python_is_pinned_by_digest():
    import re
    sums = {line.split()[1]: line.split()[0] for line in
            (INSTALLERS / "python-build-standalone.sha256").read_text().splitlines()
            if line and not line.startswith("#")}
    for build in ("linux", "macos"):
        script = (INSTALLERS / build / "build.sh").read_text()
        assert "python-build-standalone.sha256" in script, f"{build} checks the download"
        python = re.search(r"pbs_python=\$\{PBS_PYTHON:-([^}]+)\}", script).group(1)
        release = re.search(r"pbs_release=\$\{PBS_RELEASE:-([^}]+)\}", script).group(1)
        for triple in re.findall(r"triple=([\w-]+)", script):
            name = f"cpython-{python}+{release}-{triple}-install_only.tar.gz"
            assert re.fullmatch(r"[0-9a-f]{64}", sums.get(name, "")), f"no digest for {name}"


def test_the_docker_image_can_strip_a_videos_location_and_says_no_stale_version():
    dockerfile = (INSTALLERS / "docker" / "Dockerfile").read_text()
    runtime = dockerfile[dockerfile.index("AS runtime"):]
    assert "ffmpeg" in runtime
    assert 'version="0.1.0"' not in dockerfile


def test_a_converted_video_leaves_the_originals_metadata_behind():
    """Found beside H-03: the playable copy of a video a browser cannot open
    carried the original's metadata, the place it was filmed among it."""
    from ninaivu.utils import proxies
    command = proxies.live_command("clip.mkv")
    reading = command.index("clip.mkv")
    assert command[reading + 1:reading + 1 + len(proxies.NO_METADATA)] == proxies.NO_METADATA
    assert proxies.PROXY_VERSION >= 2, "copies made before keep their metadata"
