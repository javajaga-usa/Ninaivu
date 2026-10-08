"""The tightening pass over the server, its settings and start-up/shutdown.

One test (or a few) per fix; each says what used to go wrong.
"""
from __future__ import annotations

import gc
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from ninaivu.server.config import Config

GB = 1024 ** 3
LAPTOP = {"cores": 4, "memory_bytes": 8 * GB, "board": None, "gpu": None,
          "apple_silicon": False, "library_spinning": None, "tier": "basic",
          "ai_enabled": True, "mode": None}


@pytest.fixture()
def state(tmp_path, monkeypatch):
    folder = tmp_path / "state"
    folder.mkdir()
    monkeypatch.setenv("NINAIVU_STATE_DIR", str(folder))
    for name in ("NINAIVU_RESOURCE_MODE", "NINAIVU_SERVER_THREADS", "NINAIVU_COMPUTE_THREADS",
                 "NINAIVU_WORKERS", "NINAIVU_HARDWARE_TIER", "NINAIVU_PORT",
                 "NINAIVU_ADMIN_PORT", "NINAIVU_MAX_UPLOAD_MB", "NINAIVU_TRUSTED_PROXIES"):
        monkeypatch.delenv(name, raising=False)
    return folder


# -- SRV-01: a service manager's stop is an orderly stop ---------------------

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM is a POSIX signal")
def test_sigterm_stops_the_server_in_order(tmp_path):
    """systemd and docker stop send SIGTERM. It ended the process on the spot
    (and, as process 1 in a container, was ignored until Docker killed it)."""
    state_dir = tmp_path / "state"
    library = tmp_path / "library"
    library.mkdir()
    env = {**os.environ, "NINAIVU_STATE_DIR": str(state_dir), "NINAIVU_AI": "0",
           "NINAIVU_WATCH": "0", "PYTHONUNBUFFERED": "1"}
    port = _free_port()
    process = subprocess.Popen(
        [sys.executable, "-m", "ninaivu", str(library), "--host", "127.0.0.1",
         "--port", str(port), "--no-admin", "--no-mdns", "--allow-sleep", "--strict-port"],
        cwd=Path(__file__).resolve().parents[1], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        run_file = state_dir / "ninaivu.run"
        until = time.monotonic() + 60
        while not run_file.exists() and time.monotonic() < until:
            assert process.poll() is None, process.stdout.read()
            time.sleep(0.2)
        assert run_file.exists(), "the server did not start"
        time.sleep(0.5)
        process.send_signal(signal.SIGTERM)
        output, _ = process.communicate(timeout=60)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    assert process.returncode == 0, output
    assert "Stopping" in output and "Stopped." in output
    assert not run_file.exists(), "the run file named a process that had gone"


# -- SRV-02: a stop does not wait for idle keep-alive connections -----------

def test_a_stop_closes_idle_connections_and_finishes_a_response_in_flight(caplog):
    from waitress.server import create_server

    from ninaivu.__main__ import close_servers

    started = threading.Event()

    def app(environ, start_response):
        if environ["PATH_INFO"] == "/slow":
            start_response("200 OK", [("Content-Length", "6")])

            def body():
                started.set()
                yield b"abc"
                time.sleep(1.0)
                yield b"def"
            return body()
        start_response("200 OK", [("Content-Length", "2")])
        return [b"ok"]

    server = create_server(app, host="127.0.0.1", port=0, threads=2, channel_timeout=60)
    port = server.effective_port
    loop = threading.Thread(target=server.run, daemon=True)
    loop.start()

    idle = socket.create_connection(("127.0.0.1", port))
    idle.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
    assert b"200 OK" in idle.recv(200)

    got = {}

    def slow_client():
        with socket.create_connection(("127.0.0.1", port)) as sock:
            sock.sendall(b"GET /slow HTTP/1.1\r\nHost: x\r\n\r\n")
            data = b""
            while not data.endswith(b"abcdef"):
                piece = sock.recv(200)
                if not piece:
                    break
                data += piece
            got["data"] = data

    reader = threading.Thread(target=slow_client)
    reader.start()
    assert started.wait(5)
    began = time.monotonic()
    close_servers([server], patience=10)
    loop.join(10)
    reader.join(10)
    idle.close()
    assert not loop.is_alive(), "the serving loop waited on an idle connection"
    assert time.monotonic() - began < 8
    assert got["data"].endswith(b"abcdef"), "a response in flight was cut off"
    assert "Bad file descriptor" not in caplog.text, "the trigger was closed under a response"


# -- SRV-03: the stop keeps to its time and names what did not stop ----------

def test_services_stop_keeps_to_its_budget_and_names_what_is_still_going():
    from ninaivu import Services

    hold = threading.Event()
    quiet = types.SimpleNamespace(stop=lambda *a, **k: None, pause=lambda: None,
                                  restore_stop=lambda: None)
    services = object.__new__(Services)
    services.scanner = types.SimpleNamespace(stop=lambda join=False: hold.wait(5),
                                             running=False)
    for name in ("straightener", "cloud", "guardian", "backups", "updates", "digest",
                 "disks", "restore_tests", "index_copy", "mirror", "offsite", "repairer",
                 "xmp", "importer", "power"):
        setattr(services, name, quiet)
    services._pause_archive = lambda: None
    # Earlier tests can leave Tk variables as garbage. Freed on a stop-part
    # thread, each one makes tkinter wait about a second for a main loop that
    # is not there (Windows CI took 5 s that way), which is not the stop's
    # time; so the garbage goes now, and none is collected while timed.
    gc.collect()
    gc.disable()
    try:
        began = time.monotonic()
        problems = services.stop(timeout=0.5)
        elapsed = time.monotonic() - began
    finally:
        gc.enable()
        hold.set()
    assert elapsed < 2
    assert any(p.startswith("the library scan") for p in problems), problems


# -- SRV-04 and SRV-05: numbers held to their bounds -------------------------

def test_tuned_numbers_from_the_settings_file_are_held_to_their_bounds(state):
    from ninaivu.server import tuning

    (state / "config.json").write_text(json.dumps(
        {"cloud_parallel": 500, "workers": 500, "clip_batch_size": 100000}))
    cfg = Config.load()
    tuning.apply(cfg, machine=LAPTOP)
    assert cfg.cloud_parallel == 6 and cfg.workers == 64 and cfg.clip_batch_size == 256


def test_no_web_server_without_threads(state, monkeypatch):
    from ninaivu.server import tuning

    monkeypatch.setenv("NINAIVU_SERVER_THREADS", "0")
    cfg = Config.load()
    assert cfg.server_threads == 2
    cfg.server_threads = 0          # as an older start would have had it
    tuning.apply(cfg, machine=LAPTOP)
    assert cfg.server_threads >= 2


def test_runtime_numbers_from_the_environment_are_held_and_said(state, monkeypatch, caplog):
    monkeypatch.setenv("NINAIVU_MAX_UPLOAD_MB", "0")
    monkeypatch.setenv("NINAIVU_PORT", "70000")
    monkeypatch.setenv("NINAIVU_ADMIN_PORT", "three thousand")
    with caplog.at_level("WARNING"):
        cfg = Config.load()
    assert cfg.max_upload_mb == 1 and cfg.port == 65535 and cfg.admin_port == 3000
    text = caplog.text
    assert "NINAIVU_MAX_UPLOAD_MB" in text and "NINAIVU_PORT" in text
    assert "NINAIVU_ADMIN_PORT" in text


# -- SRV-06: the Advanced page takes every default it shows ------------------

def test_blur_threshold_can_be_set_and_ocr_score_is_bounded():
    from ninaivu.server import settings_groups

    assert settings_groups.coerce("blur_threshold", 60) == 60
    with pytest.raises(settings_groups.BadValue):
        settings_groups.coerce("ocr_min_score", 5)


def test_every_default_passes_its_own_check():
    from dataclasses import fields

    from ninaivu.server import settings_groups

    shipped = Config()
    for field in fields(Config):
        if (settings_groups.group_of(field.name) is None
                or field.name in settings_groups.RUNTIME_ONLY
                or field.name in settings_groups.MANAGED
                or field.name in ("allowed_hosts", "notify_events")):
            continue
        settings_groups.coerce(field.name, settings_groups._plain(getattr(shipped, field.name)))


# -- SRV-07: the panel's --workers is not saved as the household's ------------

def test_workers_from_the_command_line_are_not_written_down(state):
    from ninaivu.__main__ import take_workers

    cfg = Config.load()
    take_workers(cfg, 1 if cfg.workers != 1 else 2)
    cfg.save()
    assert "workers" not in json.loads((state / "config.json").read_text())
    assert "workers" not in Config.load()._chosen


# -- SRV-10: each setting's meaning is its own -------------------------------

def test_the_advanced_page_describes_each_setting_by_its_own_comment():
    from ninaivu.server import settings_groups

    docs = settings_groups._docs()
    assert docs["tailnet_https"].startswith("Answer this computer's Tailscale name")
    assert docs["server_threads"].startswith("Size of the request thread pool")
    assert docs["trusted_proxies"].startswith("How many reverse proxies sit in front")
    assert docs["detect_orientation"].startswith("Work out which way up a photograph goes")


# -- SRV-11: a setting of the wrong shape is refused, not taken --------------

def test_a_hand_edited_setting_of_the_wrong_shape_keeps_its_default(state, caplog):
    (state / "config.json").write_text(json.dumps(
        {"thumb_sizes": 512, "ignore_dirs": "node_modules", "open_browsing": "no",
         "house_name": "Ours"}))
    with caplog.at_level("ERROR"):
        cfg = Config.load()
    shipped = Config()
    assert cfg.thumb_sizes == shipped.thumb_sizes
    assert "_deleted" in cfg.ignore_dirs and "n" not in cfg.ignore_dirs
    assert cfg.open_browsing is shipped.open_browsing
    assert cfg.house_name == "Ours"
    for key in ("thumb_sizes", "ignore_dirs", "open_browsing"):
        assert key in caplog.text
    assert "thumb_sizes" not in cfg._chosen


# -- SRV-12: switching the update check on takes effect ----------------------

def test_the_update_check_starts_when_switched_on_after_start_up():
    from ninaivu.server import updates

    cfg = Config()
    cfg.update_check = False
    checker = updates.UpdateChecker(cfg, "0.1.0", fetch=lambda: {"tag_name": "v9.0.0"})
    checker.start()
    assert checker._thread is None
    cfg.update_check = True
    assert checker.describe()["enabled"] is True
    try:
        assert checker._thread is not None and checker._thread.is_alive()
    finally:
        checker.stop()


# -- SRV-13: a thread that dies says so in the log ---------------------------

def test_a_thread_that_dies_is_logged(caplog):
    from ninaivu.utils import logs

    try:
        raise RuntimeError("the boot went wrong")
    except RuntimeError as exc:
        args = types.SimpleNamespace(exc_type=RuntimeError, exc_value=exc,
                                     exc_traceback=exc.__traceback__,
                                     thread=types.SimpleNamespace(name="ninaivu-boot"))
    with caplog.at_level("ERROR"):
        logs._log_thread_death(args)
    assert "ninaivu-boot" in caplog.text and "the boot went wrong" in caplog.text


def test_the_thread_hook_is_installed(tmp_path, monkeypatch):
    from ninaivu.utils import logs

    monkeypatch.setattr(logs, "_configured", False)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    import logging
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        logs.configure(tmp_path)
        assert threading.excepthook is logs._log_thread_death
    finally:
        for handler in root.handlers[:]:
            if handler not in before:
                root.removeHandler(handler)
                handler.close()


# -- SRV-14: the keep-awake helper ends with this process ----------------------

def test_the_linux_inhibitor_is_tied_to_this_process(monkeypatch):
    from ninaivu.utils.awake import KeepAwake

    seen = {}

    def fake_popen(command, **kwargs):
        seen["command"] = command
        return types.SimpleNamespace(terminate=lambda: None, kill=lambda: None,
                                     wait=lambda timeout=None: 0)

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("ninaivu.utils.awake._which", lambda program: True)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    assert KeepAwake().start() is True
    assert "infinity" not in seen["command"]
    assert str(os.getpid()) in " ".join(seen["command"])


# -- SRV-16: a quiet day is looked at once -------------------------------------

def test_a_day_with_nothing_to_send_is_not_looked_at_again(monkeypatch):
    from ninaivu.utils import digest

    cfg = Config()
    cfg.digest_enabled, cfg.digest_to = True, "us@example.org"
    cfg.notify_smtp_host = "mail.example.org"
    keeper = digest.DigestKeeper(cfg, connect=lambda: None, roots=lambda: [])
    monkeypatch.setattr(keeper, "_last_sent", lambda: "")
    built = []
    monkeypatch.setattr(digest, "build", lambda *a, **k: built.append(1) or
                        {"ready": False, "reason": "nothing from this day"})
    sunday = time.strptime("2026-10-11 10:00", "%Y-%m-%d %H:%M")
    later = time.strptime("2026-10-11 11:00", "%Y-%m-%d %H:%M")
    cfg.digest_weekday, cfg.digest_hour = sunday.tm_wday, 9
    assert keeper.due(sunday)
    keeper.run(when=sunday)
    assert not keeper.due(later)
    assert built == [1]


# -- SRV-17: a rule for 8080 does not cover 80 ----------------------------------

def test_the_firewall_check_compares_whole_port_numbers(monkeypatch):
    from ninaivu import __main__ as entry

    assert not entry._rule_covers("Rule Name: Ninaivu\nLocalPort: 8080,3000", 80)
    assert entry._rule_covers("Rule Name: Ninaivu\nLocalPort: 8080,80", 80)
    assert entry._rule_covers("Nom de la règle : Ninaivu\nPort local : 5000-5010", 5005)

    class Result:
        def __init__(self, stdout):
            self.stdout = stdout

    def run(args, *_, **__):
        if "name=Ninaivu mDNS" in args:
            return Result("LocalPort: 5353")
        return Result("Rule Name: Ninaivu\nLocalPort: 8080")

    monkeypatch.setattr(entry.sys, "platform", "win32")
    monkeypatch.setattr(entry.subprocess, "run", run)
    assert entry._firewall_warning(80) != []
