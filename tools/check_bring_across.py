"""Bring a library made by the previous release into this build, and back.

What a household does when it updates a portable Ninaivu, run end to end on
two real downloads, with nobody at the screen::

    python tools/check_bring_across.py OLDER NEWER [--work DIR]

OLDER and NEWER are portable downloads (a Linux .tar.gz or a Mac .zip) or
folders they were unpacked to: the previous release and the build being
checked. Both are unpacked side by side, as README-PORTABLE.txt says to, and
then:

1. the older Ninaivu indexes a small library of photographs, and a family
   caption is written to its index, as somebody in the family would;
2. the newer one is told to bring the library across (``--bring``, the same
   step its Control Panel offers), and its first start must upgrade the
   index after copying it, find every photograph and keep the caption;
3. a caption is changed in the newer version, then ``--go-back`` takes the
   library back, and the older Ninaivu must start on it as it left it: its
   own caption, not the newer one, and no "written by a newer Ninaivu".

The photographs are hashed before and after: none may change, and on the
Mac nothing inside the newer Ninaivu.app may either (a signed app that
changes itself is no longer the app Apple notarised). Exits 0 when
every step held, and says which did not otherwise. Uses only the standard
library, and the Pythons inside the two downloads.

The release workflow runs it on the Linux and Mac builds against the latest
release, so a change that breaks updating shows on the pull request.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from contextlib import closing
from pathlib import Path

PHOTOS = 6
#: Written into the older index as a family member's own words.
CAPTION = "Paati's 80th, the whole family"
CHANGED = "changed in the newer version"
START_PATIENCE = 180.0


class Failed(RuntimeError):
    pass


def step(text: str) -> None:
    print(f"\n== {text}", flush=True)


# ---------------------------------------------------------------------------
# The two folders


def unpack(archive: Path, into: Path) -> Path:
    """The portable folder *archive* unpacks to, unpacked into *into*.

    Under another name when one of that name is there already (a pull
    request built before the version was raised has the release's name).
    """
    staging = Path(tempfile.mkdtemp(prefix="unpack-", dir=into.parent))
    if archive.is_dir():
        shutil.copytree(archive, staging / archive.name, symlinks=True)
    elif archive.name.endswith(".tar.gz"):
        with tarfile.open(archive) as tar:
            tar.extractall(staging, filter="tar")
    elif archive.suffix == ".zip" and sys.platform == "darwin":
        # ditto keeps what a Mac app needs and zipfile loses: the bundle's
        # symbolic links and its permissions.
        subprocess.run(["ditto", "-x", "-k", str(archive), str(staging)], check=True)
    else:
        raise Failed(f"not a portable download this machine can unpack: {archive}")
    made = [path for path in staging.iterdir() if path.is_dir() and path.name != "__MACOSX"]
    if len(made) != 1:
        raise Failed(f"{archive.name} unpacked to {len(made)} folders, not one")
    target = into / made[0].name
    if target.exists():
        target = into / f"{made[0].name} (newer)"
    made[0].rename(target)
    shutil.rmtree(staging)
    return target


def launcher(folder: Path) -> Path:
    for candidate in (folder / "ninaivu", folder / "Ninaivu.app" / "Contents" / "MacOS" / "ninaivu"):
        if candidate.is_file():
            return candidate
    raise Failed(f"no Ninaivu launcher in {folder}")


def base_env(home: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("NINAIVU_", "PYTHON"))}
    env.update(HOME=str(home), NINAIVU_NO_DIALOG="1")
    return env


#: Where every program here is started (set by :func:`main`): not a
#: checkout, whose own ninaivu/ is what ``python -m ninaivu`` would find first.
WORK = Path(tempfile.gettempdir())


def cwd() -> str:
    return str(WORK)


def where(folder: Path, home: Path) -> dict[str, str]:
    """What the folder's own launcher tells its Python, with the two it
    does not print (that it is portable, and where)."""
    done = subprocess.run([str(launcher(folder)), "--where"], env=base_env(home), cwd=cwd(),
                          capture_output=True, text=True, timeout=60)
    if done.returncode:
        raise Failed(f"{folder.name}: --where failed: {done.stderr.strip()}")
    told = dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)
    env = base_env(home)
    env.update(told)
    env.update(NINAIVU_PORTABLE="1", NINAIVU_PORTABLE_ROOT=str(folder))
    if (folder / "Ninaivu.app").is_dir():
        # As the Mac launcher sets it for everything it starts, from 1.12.
        env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


# ---------------------------------------------------------------------------
# The library


def make_photos(folder: Path, python: str) -> None:
    """Photographs big enough to count as photographs (not icons)."""
    script = (
        "import os, sys\n"
        "from PIL import Image\n"
        "root = sys.argv[1]\n"
        f"for i in range({PHOTOS}):\n"
        "    sub = os.path.join(root, '2019' if i % 2 else '2021', '0%d' % (i % 3 + 1))\n"
        "    os.makedirs(sub, exist_ok=True)\n"
        "    Image.frombytes('RGB', (480, 360), os.urandom(480 * 360 * 3))"
        ".save(os.path.join(sub, 'photo-%d.jpg' % i), quality=92)\n")
    subprocess.run([python, "-I", "-c", script, str(folder)], check=True, timeout=120, cwd=cwd())


def fingerprint(folder: Path) -> dict[str, str]:
    """Every photograph's hash. Not the ``.ninaivu-library`` marker beside
    them: Ninaivu writes that on purpose, to know the library again."""
    return {str(path.relative_to(folder)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(folder.rglob("*.jpg"))}


def program_files(folder: Path) -> dict[str, tuple[int, int]]:
    """The Mac app's files, each with its size and time: a signed app must
    not change itself as it runs, or it is no longer what was notarised."""
    app = folder / "Ninaivu.app"
    return {str(path.relative_to(app)): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in sorted(app.rglob("*")) if path.is_file() and not path.is_symlink()}


def query(state: Path, sql: str, *args: object) -> list[tuple]:
    with closing(sqlite3.connect(f"file:{state / 'index.db'}?mode=ro", uri=True)) as conn:
        return conn.execute(sql, args).fetchall()


def indexed(state: Path) -> int:
    try:
        return int(query(state, "SELECT COUNT(*) FROM assets")[0][0])
    except sqlite3.Error:
        return 0


def set_caption(state: Path, text: str) -> None:
    with closing(sqlite3.connect(state / "index.db")) as conn:
        conn.execute("UPDATE assets SET caption = ? WHERE id = (SELECT MIN(id) FROM assets)", (text,))
        conn.commit()


def caption(state: Path) -> str | None:
    rows = query(state, "SELECT caption FROM assets WHERE id = (SELECT MIN(id) FROM assets)")
    return rows[0][0] if rows else None


def built_version(env: dict[str, str]) -> str:
    done = subprocess.run([env["NINAIVU_PYTHON"], "-I", "-c", "import ninaivu; print(ninaivu.__version__)"],
                          env=env, cwd=cwd(), capture_output=True, text=True, timeout=60, check=True)
    return done.stdout.strip()


def last_version(state: Path) -> str | None:
    try:
        return json.loads((state / "version.json").read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Running a server


def free_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def serve(env: dict[str, str], photos: Path, log: Path, *, until) -> None:
    """Start the server on *photos*, wait for ``/healthz`` and *until*, stop it."""
    port, admin = free_port(), free_port()
    command = [env["NINAIVU_PYTHON"], "-m", "ninaivu", str(photos), "--http", "--local-only",
               "--no-mdns", "--strict-port", "--port", str(port), "--admin-port", str(admin),
               "--ai", "off", "--no-watch", "--allow-sleep"]
    with open(log, "ab") as out:
        process = subprocess.Popen(command, env=env, cwd=cwd(), stdout=out, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + START_PATIENCE
        healthy = False
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise Failed(f"the server ended (code {process.returncode}) before it was ready; "
                             f"see {log}")
            if not healthy:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as answer:
                        healthy = answer.status == 200
                except OSError:
                    pass
            if healthy and until():
                return
            time.sleep(1.0)
        raise Failed(f"not ready after {START_PATIENCE:.0f} s (healthy: {healthy}); see {log}")
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=90)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise Failed(f"the server did not stop when asked; see {log}") from None


def run_launcher(folder: Path, home: Path, *args: str) -> str:
    done = subprocess.run([str(launcher(folder)), *args], env=base_env(home), cwd=cwd(),
                          capture_output=True, text=True, timeout=300)
    print(done.stdout.strip() or "", done.stderr.strip() or "", sep="\n", flush=True)
    if done.returncode:
        raise Failed(f"{folder.name} {' '.join(args)} failed (code {done.returncode})")
    return done.stdout


def expect(condition: bool, what: str) -> None:
    if not condition:
        raise Failed(what)
    print(f"   ok: {what}", flush=True)


# ---------------------------------------------------------------------------


def check(older_archive: Path, newer_archive: Path, work: Path) -> None:
    home = work / "home"
    downloads = work / "Downloads"           # both unpacked here, side by side
    photos = work / "Photos"
    logs = work / "logs"
    for folder in (home, downloads, photos, logs):
        folder.mkdir(parents=True, exist_ok=True)

    step("Unpacking both downloads side by side")
    older = unpack(older_archive, downloads)
    newer = unpack(newer_archive, downloads)
    print(f"   older: {older.name}\n   newer: {newer.name}")

    old_env = where(older, home)
    old_state = Path(old_env["NINAIVU_STATE_DIR"])
    make_photos(photos, old_env["NINAIVU_PYTHON"])
    before = fingerprint(photos)
    expect(len(before) == PHOTOS, f"{PHOTOS} photographs made")

    step("The older Ninaivu indexes the library")
    serve(old_env, photos, logs / "older-first.log", until=lambda: indexed(old_state) >= PHOTOS)
    old_version = last_version(old_state)
    expect(indexed(old_state) == PHOTOS, f"older version {old_version} indexed all {PHOTOS}")
    set_caption(old_state, CAPTION)

    step("The newer Ninaivu brings the library across")
    new_env = where(newer, home)
    new_state = Path(new_env["NINAIVU_STATE_DIR"])
    app_before = program_files(newer)
    run_launcher(newer, home, "--bring", str(older))
    expect((new_state / "index.db").is_file(), "the index is in the newer folder")
    expect(not (old_state / "index.db").exists(), "and no longer in the older one")
    expect((older / "LIBRARY-MOVED.txt").is_file(), "the older folder says where it went")

    step("The newer Ninaivu starts on it")
    new_version = built_version(new_env)
    serve(new_env, photos, logs / "newer.log", until=lambda: last_version(new_state) == new_version)
    expect(last_version(new_state) == new_version, f"newer version {new_version} took the index over")
    expect(indexed(new_state) == PHOTOS, f"it has all {PHOTOS} photographs")
    expect(caption(new_state) == CAPTION, "the family's caption came across")
    if new_version != old_version:
        copies = list((new_state / "backups" / "before-update").glob("*"))
        expect(bool(copies), "the index was copied before it was upgraded")
    set_caption(new_state, CHANGED)

    step("Go back to the older version")
    run_launcher(newer, home, "--go-back")
    expect((old_state / "index.db").is_file(), "the index is back in the older folder")
    expect(not (new_state / "index.db").exists(), "and gone from the newer one")
    expect(not (older / "LIBRARY-MOVED.txt").exists(), "the moved note is gone")
    # Put back from the copy only when the newer version changed the index's
    # shape; the same version (a pull request before the version is raised)
    # hands it back with what was done since.
    kept = CAPTION if new_version != old_version else CHANGED
    expect(caption(old_state) == kept, "the index is as the older version can read it")

    step("The older Ninaivu starts on it again")
    serve(old_env, photos, logs / "older-again.log", until=lambda: True)
    expect(last_version(old_state) == old_version, f"version {old_version} opened it")
    expect(indexed(old_state) == PHOTOS, f"with all {PHOTOS} photographs")
    expect(caption(old_state) == kept, "and the family's caption")

    expect(fingerprint(photos) == before, "no photograph was changed")
    if app_before:
        changed = sorted(set(program_files(newer).items()) ^ set(app_before.items()))
        expect(not changed, "Ninaivu.app did not change itself as it ran"
               + (f" (it did: {', '.join(name for name, _ in changed[:5])})" if changed else ""))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("older", type=Path, help="the previous release's portable download")
    parser.add_argument("newer", type=Path, help="the build being checked")
    parser.add_argument("--work", type=Path, help="where to unpack (default: a new temporary folder)")
    args = parser.parse_args(argv)
    work = args.work or Path(tempfile.mkdtemp(prefix="ninaivu-bring-across-"))
    work.mkdir(parents=True, exist_ok=True)
    global WORK
    WORK = work.resolve()
    try:
        check(args.older.resolve(), args.newer.resolve(), work.resolve())
    except (Failed, subprocess.SubprocessError, OSError) as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        for log in sorted((work / "logs").glob("*.log")):
            print(f"\n--- {log.name} (last lines)", file=sys.stderr)
            print("\n".join(log.read_text(errors="replace").splitlines()[-30:]), file=sys.stderr)
        return 1
    print("\nThe library came across, and went back, whole.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
