"""One-way outbound sync: the two promises, and everything that could break them.

The promises are in ``ninaivu/cloud/__init__.py`` and they are short:

* a file goes up **once**, and a record of that is permanent
* the cloud **never** reaches back in — nothing up there can change anything here

Most of what follows is about the second promise, because it is the one whose
failure is unrecoverable. If a deletion in Drive could propagate back, then
somebody tidying up their Google storage on a Sunday afternoon would empty the
library their household keeps its photographs in, and there would be nothing to
put back. So the tests do the thing that would break it — delete from Drive,
disconnect the account, reconnect a different one, rescan, restart — and check
that the library and the record are exactly as they were.

Google is not reachable from a test run and should not need to be. These drive
the real client through ``tests/fake_drive.py``, which implements the parts of
the protocol Ninaivu relies on, awkward corners included.
"""

import sys
from pathlib import Path

import pytest

from fake_drive import FakeDrive
from ninaivu.storage import db
from ninaivu.cloud import drive as drive_mod
from ninaivu.cloud import engine as engine_mod
from ninaivu.cloud import store


# --- fixtures --------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "index.db"
    store.init_schema(db.init_db(path))
    return path


@pytest.fixture()
def conn(db_path):
    """This thread's connection, the same way the server gets one."""
    return db.connect(db_path)


@pytest.fixture(autouse=True)
def quick_backoff():
    """Several tests shorten the backoff; put it back so order cannot matter."""
    original = engine_mod.BACKOFF_START
    yield
    engine_mod.BACKOFF_START = original


@pytest.fixture()
def fake():
    return FakeDrive()


@pytest.fixture()
def client(fake):
    creds = drive_mod.Credentials(
        client_id="cid", client_secret="secret", refresh_token="r",
        access_token="a", expires_at=9e9, folder_name="Ninaivu")
    return drive_mod.DriveClient(creds=creds, transport=fake)


@pytest.fixture()
def library(tmp_path):
    """A few real files of a size worth chunking."""
    root = tmp_path / "lib"
    (root / "2019" / "07").mkdir(parents=True)
    (root / "2021").mkdir(parents=True)
    (root / "2019" / "07" / "beach.jpg").write_bytes(b"B" * (3 * 1024 * 1024))
    (root / "2019" / "07" / "pier.jpg").write_bytes(b"P" * 2048)
    (root / "2021" / "garden.jpg").write_bytes(b"G" * 4096)
    return root


def queue_everything(conn, root: Path):
    for path in sorted(root.rglob("*.jpg")):
        # The record's keys are POSIX-formed on every platform — the scanner
        # stores rel_path with forward slashes (see walk_media), and a
        # backslashed key here would be a different file as far as the store
        # is concerned.
        rel = path.relative_to(root).as_posix()
        store.remember(conn, str(root), rel, size=path.stat().st_size,
                       filename=path.name)


def build(db_path, client, root, **kwargs):
    return engine_mod.SyncEngine(
        connect=lambda: client, open_db=lambda: db.connect(db_path), **kwargs)


def run_to_completion(engine, timeout=30):
    engine.start()
    engine._thread.join(timeout)
    assert not engine.running, "the engine did not finish"


# --- the record ------------------------------------------------------------

def test_a_new_file_is_queued(conn):
    assert store.remember(conn, "/lib", "a.jpg", size=10) == store.PENDING
    assert store.state_of(conn, "/lib", "a.jpg") == store.PENDING


def test_a_finished_file_is_not_queued_again(conn):
    store.remember(conn, "/lib", "a.jpg", size=10)
    store.record_done(conn, "/lib", "a.jpg", remote_id="x")
    assert store.remember(conn, "/lib", "a.jpg", size=10) == store.DONE
    assert store.state_of(conn, "/lib", "a.jpg") == store.DONE


def test_the_record_survives_being_asked_a_hundred_times(conn):
    store.remember(conn, "/lib", "a.jpg", size=10)
    store.record_done(conn, "/lib", "a.jpg", remote_id="x")
    for _ in range(100):
        store.remember(conn, "/lib", "a.jpg", size=10)
    assert store.summary(conn)["done"] == 1
    assert store.summary(conn)["pending"] == 0


def test_a_file_that_failed_is_queued_again(conn):
    store.remember(conn, "/lib", "a.jpg", size=10)
    store.record_failure(conn, "/lib", "a.jpg", "network")
    assert store.state_of(conn, "/lib", "a.jpg") == store.PENDING


def test_a_file_that_keeps_failing_stops_being_retried(conn):
    store.remember(conn, "/lib", "a.jpg", size=10)
    for _ in range(store.MAX_ATTEMPTS):
        store.record_failure(conn, "/lib", "a.jpg", "network")
    assert store.state_of(conn, "/lib", "a.jpg") == store.FAILED
    assert not store.pending_batch(conn)


def test_giving_up_is_reversible_on_purpose(conn):
    store.remember(conn, "/lib", "a.jpg", size=10)
    for _ in range(store.MAX_ATTEMPTS):
        store.record_failure(conn, "/lib", "a.jpg", "network")
    assert store.reset_failures(conn) == 1
    assert store.state_of(conn, "/lib", "a.jpg") == store.PENDING


def test_a_different_file_at_the_same_path_goes_up(conn):
    """"Uploaded once" is about a file, not about a path.

    Somebody re-edits a photograph, or an old scan is replaced by a better one
    under the same name. What is sitting there now has never been backed up,
    and treating the path as settled would lose it.
    """
    store.remember(conn, "/lib", "a.jpg", size=1000)
    store.record_done(conn, "/lib", "a.jpg", remote_id="x")
    assert store.remember(conn, "/lib", "a.jpg", size=2000) == store.PENDING


def test_the_same_file_offered_with_no_size_is_left_alone(conn):
    """Not knowing the size is not evidence that it changed."""
    store.remember(conn, "/lib", "a.jpg", size=1000)
    store.record_done(conn, "/lib", "a.jpg", remote_id="x")
    assert store.remember(conn, "/lib", "a.jpg") == store.DONE


def test_a_queue_pass_does_not_quietly_retry_what_gave_up(conn):
    """Otherwise the reason it failed is buried and Retry means nothing."""
    store.remember(conn, "/lib", "a.jpg", size=10)
    for _ in range(store.MAX_ATTEMPTS):
        store.record_failure(conn, "/lib", "a.jpg", "network")
    assert store.state_of(conn, "/lib", "a.jpg") == store.FAILED

    store.queue_missing(conn, [{"root": "/lib", "rel_path": "a.jpg", "size": 10}])
    assert store.state_of(conn, "/lib", "a.jpg") == store.FAILED
    assert store.reset_failures(conn) == 1


def test_something_set_aside_is_offered_again_later(conn):
    """A photograph kept back for being hidden is not a permanent decision —
    un-hide it and the next pass picks it up."""
    store.remember(conn, "/lib", "a.jpg", size=10)
    store.record_skipped(conn, "/lib", "a.jpg", "kept back: this is hidden")
    assert store.pending_batch(conn) == []

    added = store.queue_missing(
        conn, [{"root": "/lib", "rel_path": "a.jpg", "size": 10}])
    assert added == 1
    assert store.state_of(conn, "/lib", "a.jpg") == store.PENDING


def test_two_libraries_may_hold_the_same_path(conn):
    """Keyed on root *and* path, so two drives with a 2019/ folder do not
    silently mark each other's photographs as already sent."""
    store.remember(conn, "/lib-a", "2019/x.jpg", size=1)
    store.record_done(conn, "/lib-a", "2019/x.jpg", remote_id="1")
    assert store.remember(conn, "/lib-b", "2019/x.jpg", size=1) == store.PENDING


# --- uploading, for real, through the real client --------------------------

def test_everything_goes_up(conn, db_path, client, fake, library):
    queue_everything(conn, library)
    run_to_completion(build(db_path, client, library))

    assert sorted(fake.uploaded_names()) == ["beach.jpg", "garden.jpg", "pier.jpg"]
    assert store.summary(conn)["done"] == 3
    assert store.summary(conn)["pending"] == 0


def test_the_record_holds_a_hash_of_what_was_sent(conn, db_path, client, fake,
                                                  library):
    """Taken from the bytes on their way past, not by reading the file again —
    a second full read of every file would double the disk work of a backup
    for a field nobody is waiting on."""
    import hashlib

    queue_everything(conn, library)
    run_to_completion(build(db_path, client, library))

    row = next(r for r in store.recent(conn) if r["filename"] == "beach.jpg")
    expected = hashlib.sha256(
        (library / "2019/07/beach.jpg").read_bytes()).hexdigest()
    assert row["digest"] == expected


def test_the_hash_is_right_even_when_drive_keeps_less_than_was_sent(
        conn, db_path, client, fake, library):
    """A short write makes the next chunk re-read from an earlier offset. The
    overlap must not be hashed twice."""
    import hashlib

    fake.short_write = 1024
    queue_everything(conn, library)
    run_to_completion(build(db_path, client, library))

    row = next(r for r in store.recent(conn) if r["filename"] == "beach.jpg")
    expected = hashlib.sha256(
        (library / "2019/07/beach.jpg").read_bytes()).hexdigest()
    assert row["digest"] == expected


def test_the_bytes_arrive_intact(conn, db_path, client, fake, library):
    """A three-megabyte file crosses several chunks; it has to arrive whole."""
    queue_everything(conn, library)
    run_to_completion(build(db_path, client, library))
    assert fake.content("beach.jpg") == (library / "2019/07/beach.jpg").read_bytes()


def test_drive_keeping_less_than_was_sent_is_handled(conn, db_path, client, fake, library):
    """Drive reports what it kept, which is not always what was sent."""
    fake.short_write = 1024
    queue_everything(conn, library)
    run_to_completion(build(db_path, client, library))
    assert fake.content("beach.jpg") == (library / "2019/07/beach.jpg").read_bytes()


def test_the_folder_shape_is_kept(conn, db_path, client, fake, library):
    queue_everything(conn, library)
    run_to_completion(build(db_path, client, library))
    names = {f["name"] for f in fake.folders.values()}
    assert {"Ninaivu", "2019", "07", "2021"} <= names


def test_running_it_again_uploads_nothing(conn, db_path, client, fake, library):
    """The promise, exercised the way it will actually be met: a nightly run
    over a library where everything already went."""
    queue_everything(conn, library)
    run_to_completion(build(db_path, client, library))
    before = len(fake.uploads)

    queue_everything(conn, library)          # a rescan re-offers everything
    run_to_completion(build(db_path, client, library))
    assert len(fake.uploads) == before, "files were sent a second time"


# --- the cloud cannot reach back in ---------------------------------------

def test_a_file_deleted_from_drive_is_not_sent_again(conn, db_path, client, fake, library):
    """The case the whole design is arranged around.

    Somebody clears space in Drive. Ninaivu must not notice, must not re-upload,
    and above all must not treat the absence as instruction.
    """
    queue_everything(conn, library)
    run_to_completion(build(db_path, client, library))

    fake.files.clear()                        # deleted, up there
    fake.uploads.clear()

    queue_everything(conn, library)
    run_to_completion(build(db_path, client, library))
    assert fake.uploads == [], "a deletion in Drive pulled the file back up"
    assert store.summary(conn)["done"] == 3


def test_a_file_deleted_from_drive_stays_in_the_library(conn, db_path, client,
                                                        fake, library):
    queue_everything(conn, library)
    run_to_completion(build(db_path, client, library))
    fake.files.clear()
    queue_everything(conn, library)
    run_to_completion(build(db_path, client, library))

    still_here = sorted(p.name for p in library.rglob("*.jpg"))
    assert still_here == ["beach.jpg", "garden.jpg", "pier.jpg"]


def test_nothing_in_the_engine_can_delete_a_local_file(library):
    """Belt and braces: the words are not in the module.

    A test that greps is a blunt instrument, but the guarantee here is a
    negative one — that a whole category of operation is absent — and a
    negative is exactly what an ordinary test cannot show.
    """
    import ninaivu.cloud.engine as target
    source = Path(target.__file__).read_text(encoding="utf-8")
    for forbidden in ("os.remove", "os.unlink", "shutil.rmtree", ".unlink(",
                      "DELETE FROM assets"):
        assert forbidden not in source, f"the sync engine contains {forbidden}"


def test_sending_it_again_has_to_be_asked_for(conn, db_path, client, fake, library):
    queue_everything(conn, library)
    run_to_completion(build(db_path, client, library))
    fake.uploads.clear()

    root = str(library)
    assert store.forget(conn, root, "2021/garden.jpg") is True
    queue_everything(conn, library)
    run_to_completion(build(db_path, client, library))
    assert fake.uploaded_names() == ["garden.jpg"]


# --- one folder, and nothing outside it -----------------------------------

def test_everything_lands_inside_the_ninaivu_folder(conn, db_path, client, fake,
                                                   library):
    """The containment rule, stated as the only thing that matters: after a
    full run, nothing Ninaivu made sits anywhere but under its own folder."""
    queue_everything(conn, library)
    run_to_completion(build(db_path, client, library))

    root = fake.top_level_folder("Ninaivu")
    assert root, "no Ninaivu folder was made"
    assert fake.outside(root) == [], "something was put outside the Ninaivu folder"


def test_an_unrelated_drive_folder_of_the_same_name_is_left_alone(
        conn, db_path, client, fake, library):
    """Somebody's own ``2019`` folder, sitting at the top of their Drive, is
    not where the holiday photographs go. The search is parent-scoped, so it
    is not even a candidate."""
    fake.folders["theirs"] = {"name": "2019", "parents": ["root"]}
    queue_everything(conn, library)
    run_to_completion(build(db_path, client, library))

    root = fake.top_level_folder("Ninaivu")
    assert all(f["parents"] != ["theirs"] for f in fake.files.values())
    assert fake.outside(root) == []
    # And it was not written into, moved, or renamed.
    assert fake.folders["theirs"] == {"name": "2019", "parents": ["root"]}


def test_a_remembered_folder_that_is_no_longer_ours_is_not_used(
        conn, db_path, client, fake, library):
    """A stale saved id fails safe.

    If the folder was deleted, or the id was edited to point at something
    else, the right answer is to find or make the Ninaivu folder again — not to
    upload a family's photographs into whatever that id happens to be now.
    """
    client.creds.folder_id = "no-such-folder"
    queue_everything(conn, library)
    run_to_completion(build(db_path, client, library))

    root = fake.top_level_folder("Ninaivu")
    assert root and client.creds.folder_id == root
    assert fake.outside(root) == []


def test_renaming_the_folder_in_drive_does_not_make_a_second_one(
        conn, db_path, client, fake, library):
    """Somebody tidying up their Drive is not asking for a divided backup."""
    queue_everything(conn, library)
    run_to_completion(build(db_path, client, library))
    root = fake.top_level_folder("Ninaivu")

    fake.folders[root]["name"] = "Family photos"     # renamed in Drive
    store.forget(conn, str(library), "2021/garden.jpg")
    queue_everything(conn, library)
    run_to_completion(build(db_path, client, library))

    assert client.creds.folder_id == root, "it went somewhere else"
    assert client.creds.folder_name == "Family photos", "the console would lie"
    assert fake.outside(root) == []


def test_a_folder_that_is_gone_is_made_again(conn, db_path, client, fake,
                                             library):
    queue_everything(conn, library)
    run_to_completion(build(db_path, client, library))
    first = fake.top_level_folder("Ninaivu")

    del fake.folders[first]                          # deleted in Drive
    fake.uploads.clear()
    store.forget(conn, str(library), "2021/garden.jpg")
    queue_everything(conn, library)
    run_to_completion(build(db_path, client, library))

    second = fake.top_level_folder("Ninaivu")
    assert second and second != first, "it kept using a folder that is gone"
    # What went up this time is inside the new folder. The first run's files
    # are still under the folder that was deleted, which is Drive's business
    # and not something Ninaivu reaches back in to tidy.
    sent = fake.uploaded_names()
    assert sent == ["garden.jpg"]
    # The last one, not the first: the copy from the earlier run is still
    # recorded, under the folder that was deleted.
    landed = [f for f in fake.files.values() if f["name"] == "garden.jpg"][-1]
    assert second in fake._ancestors(landed["parents"])


def test_a_file_is_never_uploaded_without_a_parent(client):
    with pytest.raises(drive_mod.DriveError, match="outside the Ninaivu folder"):
        client.begin_upload("x.jpg", "", 10)


def test_a_folder_is_never_made_without_a_parent(client):
    with pytest.raises(drive_mod.DriveError, match="outside the Ninaivu folder"):
        client.ensure_folder("2019", "")
    with pytest.raises(drive_mod.DriveError, match="outside the Ninaivu folder"):
        client.folder_path(["2019"], "")


def test_a_path_cannot_climb_out_of_the_ninaivu_folder(client, fake):
    """``..`` is not a folder name, and is not treated as one."""
    root = client.ninaivu_root()
    assert client.folder_path(["..", "", "  ", "."], root) == root


# --- hidden photographs stay here -----------------------------------------

def hidden_everything(root, rel):
    return 2


def test_hidden_photographs_are_not_uploaded(conn, db_path, client, fake, library):
    queue_everything(conn, library)
    run_to_completion(build(db_path, client, library,
                            visibility_of=hidden_everything))
    assert fake.uploads == [], "an admin-only photograph was sent to Google"


def test_nothing_sends_a_hidden_photograph(conn, db_path, client, fake, library):
    """There is no setting for it: hiding something from your own household is
    not consent to put it on Google."""
    queue_everything(conn, library)
    run_to_completion(build(db_path, client, library,
                            visibility_of=hidden_everything))
    assert fake.uploads == []
    assert all(r["state"] == "skipped" for r in store.recent(conn))


def test_a_picture_not_yet_checked_for_screenshots_waits(conn, db_path, client,
                                                         fake, library):
    """It is hidden if it turns out to be one, so it goes after the check,
    not before it."""
    queue_everything(conn, library)
    unchecked = {"2019/07/pier.jpg"}
    run_to_completion(build(db_path, client, library,
                            awaiting_check=lambda root, rel: rel in unchecked))
    assert len(fake.uploads) == 2
    waiting = [r for r in store.recent(conn) if r["state"] == "skipped"]
    assert len(waiting) == 1 and "not yet checked" in waiting[0]["error"]


def test_files_indexed_while_it_ran_go_in_the_same_run(conn, db_path, client,
                                                       fake, library):
    """The queue is offered the library once more before a run ends."""
    queue_everything(conn, library)
    late = library / "2021" / "late.jpg"

    def requeue():
        if late.exists():
            return 0
        late.write_bytes(b"L" * 2048)
        store.remember(conn, str(library), "2021/late.jpg", size=2048,
                       filename="late.jpg")
        return 1

    run_to_completion(build(db_path, client, library, requeue=requeue))
    assert len(fake.uploads) == 4, "the file indexed mid-run waited for the next run"


def test_a_kept_back_file_says_why(conn, db_path, client, fake, library):
    queue_everything(conn, library)
    run_to_completion(build(db_path, client, library,
                            visibility_of=hidden_everything))
    rows = store.recent(conn)
    assert all("hidden" in r["error"] for r in rows), [r["error"] for r in rows]


# --- interruptions ---------------------------------------------------------

def test_a_rate_limit_is_waited_out_not_failed(conn, db_path, client, fake, library):
    queue_everything(conn, library)
    fake.fail_next = [429, 503]
    engine = build(db_path, client, library)
    engine_mod.BACKOFF_START = 0.05           # the wait itself is not the test
    run_to_completion(engine, timeout=60)
    assert store.summary(conn)["done"] == 3


def test_an_expired_upload_session_is_started_again(conn, db_path, client, fake, library):
    queue_everything(conn, library)
    fake.expire_next_session = True
    engine = build(db_path, client, library)
    engine_mod.BACKOFF_START = 0.05
    run_to_completion(engine, timeout=60)
    assert store.summary(conn)["done"] == 3


def test_a_missing_file_is_not_retried_for_ever(conn, db_path, client, fake, library):
    store.remember(conn, str(library), "gone.jpg", size=10)
    run_to_completion(build(db_path, client, library))
    row = store.recent(conn)[0]
    assert "no longer there" in row["error"]


def test_one_unreadable_file_does_not_end_the_run(conn, db_path, client, fake, library,
                                                 monkeypatch):
    """A file the OS refuses to read is set aside; the rest still goes up.

    The read happened outside the ``try`` that guards the upload, so the error
    escaped ``_one`` and ended the run — and, since the row stayed pending and
    oldest, ended every later run at the same file. Nothing was backed up
    until somebody un-indexed it.
    """
    from ninaivu.utils import source_version as sv

    blocked = library / "2019" / "07" / "beach.jpg"
    real = sv.source_version

    def refuse(path):
        if Path(path).name == blocked.name:
            raise PermissionError(13, "held open by another program", str(path))
        return real(path)

    monkeypatch.setattr(sv, "source_version", refuse)
    engine_mod.BACKOFF_START = 0.05           # the retries' wait is not the test
    queue_everything(conn, library)
    run_to_completion(build(db_path, client, library))

    assert sorted(fake.uploaded_names()) == ["garden.jpg", "pier.jpg"], (
        "the two readable files should have gone up despite the third")
    rows = {r["rel_path"]: r for r in store.recent(conn)}
    assert "could not read" in (rows["2019/07/beach.jpg"]["error"] or "")
    assert rows["2019/07/beach.jpg"]["attempts"] >= store.MAX_ATTEMPTS, (
        "the failure was not counted, so it would be retried for ever")


def test_a_revoked_permission_stops_and_says_so(conn, db_path, client, fake, library):
    fake.invalid_grant = True
    client.creds.expires_at = 0               # force a refresh
    queue_everything(conn, library)
    engine = build(db_path, client, library)
    run_to_completion(engine)
    assert fake.uploads == []
    # "Says so": the console's Connect again prompt reads these two.
    snap = engine.state.snapshot()
    assert snap["needs_reconnect"] is True, snap
    assert "Connect the account again" in (snap["last_error"] or ""), snap


def test_pausing_stops_between_files(conn, db_path, client, fake, library):
    queue_everything(conn, library)
    engine = build(db_path, client, library)
    engine.start()
    engine.stop(join=True)
    assert not engine.running
    # Whatever it managed is recorded honestly: nothing is left claiming to be
    # in flight, so a restart picks up cleanly.
    assert all(r["state"] != store.UPLOADING for r in store.recent(conn))


# --- tokens ----------------------------------------------------------------

def test_a_sign_in_without_a_refresh_token_is_refused(fake):
    """It would work for an hour and then quietly stop, which is worse than
    failing now."""
    creds = drive_mod.Credentials(client_id="c", client_secret="s")
    client = drive_mod.DriveClient(creds=creds, transport=fake)
    fake.give_refresh_token = False
    with pytest.raises(drive_mod.DriveError, match="refresh token"):
        client.exchange_code("code", "http://127.0.0.1:3000/cb")


def test_a_revoked_permission_is_reported_as_needing_a_person(fake):
    creds = drive_mod.Credentials(client_id="c", client_secret="s",
                                  refresh_token="r")
    client = drive_mod.DriveClient(creds=creds, transport=fake)
    fake.invalid_grant = True
    with pytest.raises(drive_mod.NeedsReconnect):
        client.refresh()


def test_credentials_never_hand_out_the_token(tmp_path):
    creds = drive_mod.Credentials(client_id="c" * 30, client_secret="s",
                                  refresh_token="SECRET", access_token="ALSO")
    shown = creds.public()
    assert "SECRET" not in str(shown)
    assert "ALSO" not in str(shown)
    assert shown["connected"] is True


def test_the_token_file_is_owner_only(tmp_path):
    import stat
    if sys.platform == "win32":
        # os.chmod has no mode bits to set on Windows; the file's protection
        # there is the user profile's ACL on the state directory.
        pytest.skip("POSIX permissions do not apply")
    path = tmp_path / "google.json"
    drive_mod.Credentials(client_id="c", refresh_token="r").save(path)
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, oct(mode)


def test_the_scope_is_the_narrow_one():
    """`drive.file` is only what this app created — not the user's Drive."""
    assert drive_mod.SCOPE.endswith("/auth/drive.file")


def test_the_consent_url_asks_for_offline_access():
    url = drive_mod.consent_url("cid", "http://127.0.0.1:3000/cb", "state123")
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "state123" in url
    assert "drive.file" in url


# --- a busy library index --------------------------------------------------
#
# A scan pass or a backup holding the write lock past the busy timeout raised
# "database is locked" out of the engine, which ended the run: uploads stopped
# part-way through a library and stayed stopped until Ninaivu was restarted.

def _locked_for(times, real):
    left = {"n": times}

    def flaky(*args, **kwargs):
        if left["n"] > 0:
            left["n"] -= 1
            raise engine_mod.sqlite3.OperationalError("database is locked")
        return real(*args, **kwargs)
    return flaky


def test_a_busy_index_is_waited_out_not_the_end_of_the_run(
        conn, db_path, client, fake, library, monkeypatch):
    queue_everything(conn, library)
    monkeypatch.setattr(store, "mark_uploading",
                        _locked_for(2, store.mark_uploading))
    engine = build(db_path, client, library)
    engine_mod.BACKOFF_START = 0.05
    run_to_completion(engine, timeout=60)
    assert store.summary(conn)["done"] == 3
    assert engine.state.last_error == ""


def test_an_upload_is_recorded_even_when_the_index_is_busy(
        conn, db_path, client, fake, library, monkeypatch):
    """By then the file is on Drive; losing the record would send it twice."""
    queue_everything(conn, library)
    monkeypatch.setattr(store, "record_done", _locked_for(2, store.record_done))
    monkeypatch.setattr(engine_mod.time, "sleep", lambda s: None)
    engine = build(db_path, client, library)
    run_to_completion(engine, timeout=60)
    assert store.summary(conn)["done"] == 3
    assert len(fake.files) == 3


def test_a_broken_database_still_stops_the_run(
        conn, db_path, client, fake, library, monkeypatch):
    """Only busy is waited out; anything else is reported, not looped on."""
    queue_everything(conn, library)

    def broken(*args, **kwargs):
        raise engine_mod.sqlite3.OperationalError("no such table: cloud_uploads")
    monkeypatch.setattr(store, "mark_uploading", broken)
    engine = build(db_path, client, library)
    run_to_completion(engine, timeout=30)
    assert "no such table" in engine.state.last_error


def test_queueing_the_library_leaves_an_upload_in_flight_alone(conn):
    """The queue pass snapshots the table, then walks the library for minutes.
    An upload that started meanwhile and saved its resume URL was rewritten
    from the snapshot — state back to pending, resume URL empty — so the next
    run sent the whole film again."""
    store.remember(conn, "/lib", "film.mp4", size=4000)
    store.mark_uploading(conn, "/lib", "film.mp4")
    store.save_resume(conn, "/lib", "film.mp4", "https://resume/abc")

    store.queue_missing(conn, [{"root": "/lib", "rel_path": "film.mp4", "size": 4000}])

    row = conn.execute("SELECT state, resume_url FROM cloud_uploads "
                       "WHERE rel_path='film.mp4'").fetchone()
    assert row["state"] == store.UPLOADING
    assert row["resume_url"] == "https://resume/abc", "the resume URL was wiped"


def test_a_changed_file_in_flight_is_still_queued_afresh(conn):
    store.remember(conn, "/lib", "film.mp4", size=4000)
    store.mark_uploading(conn, "/lib", "film.mp4")
    store.save_resume(conn, "/lib", "film.mp4", "https://resume/abc")

    store.queue_missing(conn, [{"root": "/lib", "rel_path": "film.mp4", "size": 5000}])

    row = conn.execute("SELECT state, resume_url FROM cloud_uploads "
                       "WHERE rel_path='film.mp4'").fetchone()
    assert (row["state"], row["resume_url"]) == (store.PENDING, "")


def test_drive_folders_keep_a_full_date():
    """YYYY/MM/DD reaches Drive whole; anything deeper is cut at three levels."""
    from ninaivu.cloud.engine import _date_folders
    assert _date_folders({"rel_path": "2024/03/15/a.jpg"}) == ["2024", "03", "15"]
    assert _date_folders({"rel_path": "2024\\03\\15\\a.jpg"}) == ["2024", "03", "15"]
    assert _date_folders({"rel_path": "2024/03/15/extra/a.jpg"}) == ["2024", "03", "15"]
    assert _date_folders({"rel_path": "2019/07/beach.jpg"}) == ["2019", "07"]
    assert _date_folders({"rel_path": "loose.jpg"}) == []
