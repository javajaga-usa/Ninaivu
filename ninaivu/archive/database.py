"""
Durable state for the archive consolidator.

Design notes
------------
Every file the scanner *considers* gets a row before any work is attempted, so
nothing can be silently invisible. The row's status is the single source of
truth for resume:

    pending   -> seen, not yet processed        (resumable)
    copying   -> a copy was in flight           (resumable, .partial may exist)
    copied    -> bytes landed, not yet verified (resumable -> re-verified)
    verified  -> bytes landed and SHA-256 matched the source   TERMINAL
    duplicate -> byte-identical to an already *verified* file  TERMINAL
    skipped   -> zero-byte or otherwise deliberately not archived  TERMINAL
    error     -> failed; retried on the next run              (resumable)

Only VERIFIED files may act as a deduplication target. That is the invariant
that stops an unreadable file from making its healthy twin disappear.
"""

import json
import os
import sqlite3
import threading
import time

from .safety import is_within

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'archive.db')
SCHEMA_VERSION = 2

# Statuses that mean "this file is done, never touch it again".
TERMINAL_STATUSES = ('verified', 'duplicate', 'skipped')
# Only these may be used as a dedup target (invariant: never dedupe against
# a file that did not actually land and verify).
DEDUP_STATUSES = ('verified',)

#: A connection per thread, and every write committed as it is made.
#:
#: Both halves are deliberate, and both were arrived at the hard way.
#:
#: *Per thread, not shared.* A single connection shared by the copy workers
#: looks tempting — SQLite allows one writer anyway — but ``cur.rowcount``
#: comes from ``sqlite3_changes()``, which counts changes on the *connection*,
#: not on the statement. Two threads executing against one connection can
#: therefore read each other's row counts, and ``set_status`` uses exactly
#: that value to detect a file it was never told about. The symptom was a
#: photo recorded as an error with "unregistered file" while its bytes had in
#: fact been copied perfectly.
#:
#: *Committed immediately.* Coalescing commits leaves a write transaction open
#: across many statements, and every other worker then blocks on it for as
#: long as it is held. Short, frequent transactions let SQLite interleave the
#: writers properly, which is faster in a way that batching is not.
_local = threading.local()
_writer_lock = threading.Lock()

def flush():
    """Commit anything this thread still owes. Cheap, and safe to call often."""
    conn = getattr(_local, 'conn', None)
    if conn is None:
        return
    try:
        conn.commit()
    except sqlite3.Error:
        pass


def _use_wal(conn: sqlite3.Connection, tries: int = 30) -> None:
    """Put the file in WAL mode, waiting out anybody else who has it locked.

    The busy timeout does not cover this. Switching journal mode needs the file
    to itself, and SQLite answers "database is locked" straight away rather
    than waiting, because waiting there is how two connections deadlock. So a
    connection opened while another was part-way through a write failed on the
    spot — a brand-new archive.db being set up while something else already
    had it open was exactly that.

    Tried again for a few seconds, and then given up on without failing: WAL is
    a property of the file, not of the connection, so once any connection has
    set it every later one gets it anyway — and a connection left in the old
    journal mode is still a perfectly good connection, only less able to read
    while somebody else writes.
    """
    for _ in range(tries):
        try:
            conn.execute('PRAGMA journal_mode=WAL')
            return
        except sqlite3.OperationalError as exc:
            if 'locked' not in str(exc) and 'busy' not in str(exc):
                raise
            time.sleep(0.1)


def get_db():
    """One connection per thread, reused. WAL so the UI can read while we write."""
    conn = getattr(_local, 'conn', None)
    # A connection belongs to the file it was opened on. `DB_PATH` is set by
    # `archive.configure()` and can change, and changing it closes only the
    # calling thread's connection — every other thread went on writing to the
    # file the engine had just been told to stop using.
    if conn is not None and getattr(_local, 'path', None) != DB_PATH:
        close_db()
        conn = None
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=60.0)
        # Wait rather than fail when another worker is mid-write. Sixty seconds
        # is far longer than any single statement here takes, so hitting it
        # means something is genuinely wrong rather than merely busy.
        conn.execute('PRAGMA busy_timeout=60000')
        _use_wal(conn)
        conn.execute('PRAGMA synchronous=NORMAL')
        conn.row_factory = sqlite3.Row
        _local.conn = conn
        _local.path = DB_PATH
    return conn


def close_db():
    conn = getattr(_local, 'conn', None)
    if conn is not None:
        try:
            conn.commit()          # never drop owed work on the way out
        except sqlite3.Error:
            pass
        conn.close()
        _local.conn = None
        _local.path = None


FILE_COLUMNS = {
    'source_path':      'TEXT UNIQUE',
    'filename':         'TEXT',
    'size':             'INTEGER',
    'source_mtime':     'REAL',
    'file_hash':        'TEXT',
    'dest_hash':        'TEXT',
    'exif_date':        'TEXT',
    'date_source':      'TEXT',
    'status':           "TEXT NOT NULL DEFAULT 'pending'",
    'destination_path': 'TEXT',
    'duplicate_of':     'TEXT',
    'error':            'TEXT',
    'job_id':           'INTEGER',
    'updated_at':       'REAL',
}


def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute('''
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_path TEXT UNIQUE
        )
    ''')

    # Additive migration: an archive.db may already exist from the old schema.
    # Add whatever is missing rather than dropping the user's history.
    existing = {r['name'] for r in cur.execute('PRAGMA table_info(files)')}
    for col, decl in FILE_COLUMNS.items():
        if col not in existing:
            # UNIQUE cannot be added via ALTER; source_path already carries it.
            decl = decl.replace(' UNIQUE', '')
            cur.execute(f'ALTER TABLE files ADD COLUMN {col} {decl}')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sources TEXT,
            destination TEXT,
            layout TEXT,
            state TEXT,
            phase TEXT,
            total_files INTEGER DEFAULT 0,
            bytes_copied INTEGER DEFAULT 0,
            started_at REAL,
            ended_at REAL,
            message TEXT
        )
    ''')
    job_cols = {r['name'] for r in cur.execute('PRAGMA table_info(jobs)')}
    for col, decl in (('mode', "TEXT DEFAULT 'copy'"), ('total_bytes', 'INTEGER DEFAULT 0')):
        if col not in job_cols:
            cur.execute(f'ALTER TABLE jobs ADD COLUMN {col} {decl}')
    cur.execute('CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)')
    # Every archive folder a job wrote a temporary file into. A job that ends
    # uncleanly can leave one behind in any of them, and knowing which folders
    # those are is what lets the next run look in a handful of folders instead
    # of listing the whole archive — minutes on a large archive on a USB disk.
    cur.execute('''
        CREATE TABLE IF NOT EXISTS job_folders (
            job_id INTEGER NOT NULL,
            folder TEXT NOT NULL,
            PRIMARY KEY (job_id, folder)
        )''')
    # A person's answer about a folder the archive thought was course material.
    # Keyed by drive identity and path within the drive (course_material.decision_key),
    # so the answer survives the same drive coming back under another letter.
    # What the image model thought of a borderline folder's pictures. Kept so
    # the estimate, the dry run and the real run agree even when the model is
    # not loaded for all three: an opinion is reused while the sampled files are
    # unchanged. Keyed like folder_decisions.
    cur.execute('''
        CREATE TABLE IF NOT EXISTS folder_picture_opinions (
            key        TEXT PRIMARY KEY,
            signature  TEXT NOT NULL,
            points     INTEGER NOT NULL,
            detail     TEXT NOT NULL,
            model      TEXT NOT NULL DEFAULT '',
            decided_at REAL NOT NULL
        )''')
    # Whether an audio file about to be held back as music sounded like speech.
    # Listening means decoding a minute of sound, so the answer is kept for the
    # file at that size and time: the dry run and the real run reuse what the
    # estimate heard. Keyed by the file's own decision_key.
    cur.execute('''
        CREATE TABLE IF NOT EXISTS sound_opinions (
            key        TEXT PRIMARY KEY,
            signature  TEXT NOT NULL,
            verdict    TEXT NOT NULL,
            detail     TEXT NOT NULL,
            decided_at REAL NOT NULL
        )''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS folder_decisions (
            key        TEXT PRIMARY KEY,
            path       TEXT NOT NULL,
            decision   TEXT NOT NULL CHECK (decision IN ('exclude', 'include')),
            decided_at REAL NOT NULL
        )''')

    cur.execute('CREATE INDEX IF NOT EXISTS idx_status ON files(status)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_job ON files(job_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_hash_status ON files(file_hash, status)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_size_status ON files(size, status)')
    # Every question about an archive path - "has a dry run already claimed
    # it", "what did we record for the file sitting there", "does another
    # source own it" - filters on destination_path. Without this index each
    # one walked the whole table, so a dry run over a large archive was
    # quadratic: the more it had planned, the slower each next file got.
    cur.execute('CREATE INDEX IF NOT EXISTS idx_dest_status '
                'ON files(destination_path, status)')
    # The single-column hash and size indexes were prefixes of the composite
    # ones above, so SQLite never needed them; they only cost space and a
    # write per row. Dropped on databases that still carry them.
    cur.execute('DROP INDEX IF EXISTS idx_file_hash')
    cur.execute('DROP INDEX IF EXISTS idx_size')

    conn.commit()

    # Legacy rows carry status 'copied' from the old scanner, which never
    # verified anything. 'copied' is deliberately NOT terminal, so the next
    # run re-hashes and verifies them instead of trusting them blindly.
    if get_config('schema_version') != str(SCHEMA_VERSION):
        _requeue_untrustworthy_legacy_rows(conn)
    set_config('schema_version', str(SCHEMA_VERSION))


def _requeue_untrustworthy_legacy_rows(conn):
    """
    One-time repair on upgrade.

    The old scanner deduplicated against ANY earlier row with a matching hash,
    including rows for files that errored out and never reached the archive.
    A file written off that way is not in the archive and never will be, and it
    is indistinguishable after the fact from a correctly-identified duplicate.

    Every row this version writes records duplicate_of. So a 'duplicate' row
    with no duplicate_of came from the old logic and cannot be trusted: send it
    back to 'pending' to be re-evaluated. Re-evaluating a genuine duplicate is
    cheap - it gets re-identified and skipped. Trusting a bogus one loses a
    photo permanently.
    """
    cur = conn.execute(
        "UPDATE files SET status='pending' "
        "WHERE status='duplicate' AND (duplicate_of IS NULL OR duplicate_of='')")
    conn.commit()
    if cur.rowcount:
        print(f'[migration] {cur.rowcount} legacy "duplicate" rows had no recorded '
              f'original and were requeued for re-evaluation.')


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def set_config(key, value):
    conn = get_db()
    conn.execute('INSERT INTO config(key, value) VALUES(?, ?) '
                 'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))
    conn.commit()


def get_config(key, default=None):
    row = get_db().execute('SELECT value FROM config WHERE key=?', (key,)).fetchone()
    return row['value'] if row else default


def folder_decision(key):
    """'exclude', 'include', or None when nobody has decided."""
    row = get_db().execute('SELECT decision FROM folder_decisions WHERE key=?',
                           (key,)).fetchone()
    return row['decision'] if row else None


def folder_decisions_version():
    """Changes whenever any folder decision is made, changed or forgotten.

    Every write stamps ``decided_at`` afresh, so the count and the sum of those
    stamps between them move on any insert, update or delete.
    """
    row = get_db().execute('SELECT COUNT(*) n, COALESCE(SUM(decided_at), 0) t '
                           'FROM folder_decisions').fetchone()
    return f"{row['n']}:{row['t']!r}"


def set_folder_decision(key, path, decision):
    """Remember a decision; None forgets it, so the folder is asked about again."""
    conn = get_db()
    if decision is None:
        conn.execute('DELETE FROM folder_decisions WHERE key=?', (key,))
    else:
        if decision not in ('exclude', 'include'):
            raise ValueError(f'unknown folder decision: {decision!r}')
        conn.execute('INSERT INTO folder_decisions(key, path, decision, decided_at) '
                     'VALUES(?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET '
                     'path=excluded.path, decision=excluded.decision, '
                     'decided_at=excluded.decided_at',
                     (key, path, decision, time.time()))
    conn.commit()


def picture_opinion(key, signature):
    """(points, detail) stored for exactly this sample of files, or None."""
    row = get_db().execute('SELECT points, detail FROM folder_picture_opinions '
                           'WHERE key=? AND signature=?', (key, signature)).fetchone()
    return (row['points'], row['detail']) if row else None


def set_picture_opinion(key, signature, points, detail, model=''):
    conn = get_db()
    conn.execute('INSERT INTO folder_picture_opinions(key, signature, points, detail, model, '
                 'decided_at) VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET '
                 'signature=excluded.signature, points=excluded.points, '
                 'detail=excluded.detail, model=excluded.model, decided_at=excluded.decided_at',
                 (key, signature, int(points), detail, model, time.time()))
    conn.commit()


def sound_opinion(key, signature):
    """(verdict, detail) heard for exactly this file, or None."""
    row = get_db().execute('SELECT verdict, detail FROM sound_opinions '
                           'WHERE key=? AND signature=?', (key, signature)).fetchone()
    return (row['verdict'], row['detail']) if row else None


def set_sound_opinion(key, signature, verdict, detail):
    conn = get_db()
    conn.execute('INSERT INTO sound_opinions(key, signature, verdict, detail, decided_at) '
                 'VALUES(?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET '
                 'signature=excluded.signature, verdict=excluded.verdict, '
                 'detail=excluded.detail, decided_at=excluded.decided_at',
                 (key, signature, verdict, detail, time.time()))
    conn.commit()


ALL_MEDIA_KINDS = ['audio', 'image', 'video']


def save_settings(sources, destination, media_types=None, deep_scan=None):
    """
    Remember the job so the next launch does not start from an empty form.

    Sources are stored in the per-folder form [{'path':..., 'types':[...]}] so
    each folder's media selection survives a restart along with its path.
    """
    entries = []
    for s in sources or []:
        if isinstance(s, dict):
            path = s.get('path') or ''
            types = sorted(set(s.get('types') or ALL_MEDIA_KINDS) & set(ALL_MEDIA_KINDS))
        else:
            path = str(s)
            types = sorted(set(media_types or ALL_MEDIA_KINDS) & set(ALL_MEDIA_KINDS))
        if path:
            entries.append({'path': path, 'types': types})

    set_config('last_sources', json.dumps(entries))
    set_config('last_destination', destination or '')
    set_config('last_media_types',
               json.dumps(sorted(set(media_types or ALL_MEDIA_KINDS))))
    if deep_scan is not None:
        set_config('last_deep_scan', '1' if deep_scan else '0')


def load_settings():
    def _json(key, fallback):
        try:
            value = json.loads(get_config(key) or '')
        except (ValueError, TypeError):
            return fallback
        return value if value is not None else fallback

    raw = _json('last_sources', [])
    entries = []
    for s in raw if isinstance(raw, list) else []:
        # Tolerate the flat list written by earlier versions: a folder saved
        # before per-folder selection existed means "everything", which is also
        # the default for a newly added folder.
        if isinstance(s, dict):
            path = s.get('path') or ''
            types = sorted(set(s.get('types') or ALL_MEDIA_KINDS) & set(ALL_MEDIA_KINDS))
        else:
            path, types = str(s), list(ALL_MEDIA_KINDS)
        if path:
            entries.append({'path': path, 'types': types})

    return {
        'source_dirs': entries,
        'destination_dir': get_config('last_destination', '') or '',
        'media_types': _json('last_media_types', list(ALL_MEDIA_KINDS)),
        'deep_scan': (get_config('last_deep_scan', '1') or '1') != '0',
    }


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------

def create_job(sources, destination, layout, mode='copy'):
    conn = get_db()
    cur = conn.execute(
        'INSERT INTO jobs(sources, destination, layout, mode, state, phase, started_at) '
        'VALUES(?,?,?,?,?,?,?)',
        (json.dumps(sources), destination, layout, mode, 'running', 'enumerating',
         time.time()))
    conn.commit()
    return cur.lastrowid


def close_orphaned_jobs(before_id):
    """End every job older than *before_id* that still says it is running.

    Called as a job starts. Only one runs at a time, so any earlier row still
    ``running`` with no end time belongs to a Ninaivu that stopped underneath
    it. Picking it up after a restart closes it, but Start pressed a moment
    before that pick-up got to it started a new job instead — and the old
    row said "running" from then on, however many runs came after.
    """
    conn = get_db()
    cur = conn.execute(
        "UPDATE jobs SET state='interrupted', phase='interrupted', ended_at=?, "
        "message=? WHERE id < ? AND state='running' AND ended_at IS NULL",
        (time.time(), 'Ninaivu stopped during this run; a later run carried it on.',
         before_id))
    conn.commit()
    return cur.rowcount


def update_job(job_id, **fields):
    if not job_id or not fields:
        return
    allowed = {'state', 'phase', 'total_files', 'total_bytes', 'bytes_copied',
               'ended_at', 'message'}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    conn = get_db()
    sets = ', '.join(f'{k}=?' for k in fields)
    conn.execute(f'UPDATE jobs SET {sets} WHERE id=?', (*fields.values(), job_id))
    conn.commit()


def get_job(job_id):
    row = get_db().execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    return dict(row) if row else None


def previous_destinations():
    """
    Every archive folder this database has written to, newest first. Used to
    recognise that a chosen destination is really a subfolder of an archive the
    user already built.
    """
    rows = get_db().execute(
        'SELECT destination, MAX(id) last_id FROM jobs '
        "WHERE destination IS NOT NULL AND destination <> '' AND mode <> 'dry-run' "
        'GROUP BY destination ORDER BY last_id DESC').fetchall()
    return [r['destination'] for r in rows]


def jobs_before(job_id):
    """id, destination, mode and state of every job older than *job_id*."""
    rows = get_db().execute('SELECT id, destination, mode, state FROM jobs WHERE id < ? '
                            'ORDER BY id', (job_id,)).fetchall()
    return [dict(row) for row in rows]


def note_job_folder(job_id, folder):
    """Record that *job_id* is about to write a temporary file in *folder*.

    Committed before the file exists, so a run killed a moment later has
    already said where to look.
    """
    if not job_id:
        return
    conn = get_db()
    conn.execute('INSERT OR IGNORE INTO job_folders(job_id, folder) VALUES(?, ?)',
                 (int(job_id), str(folder)))
    conn.commit()


def job_folders(job_ids):
    """Every folder any of these jobs wrote a temporary file into."""
    ids = [int(i) for i in job_ids]
    if not ids:
        return []
    rows = get_db().execute(
        f'SELECT DISTINCT folder FROM job_folders WHERE job_id IN '
        f'({",".join("?" * len(ids))}) ORDER BY folder', ids).fetchall()
    return [row['folder'] for row in rows]


def interrupted_job():
    """The latest job, if Ninaivu stopped while it was still running.

    A job that ends in any way the engine knows about — finished, stopped,
    failed, or handing over to its own restart after a drive came back —
    writes an end time. One that is still ``running`` with none, while no job
    is alive in this process, is one the process ended underneath.
    """
    job = latest_job()
    if job and job.get('state') == 'running' and not job.get('ended_at'):
        return job
    return None


def latest_job():
    row = get_db().execute('SELECT * FROM jobs ORDER BY id DESC LIMIT 1').fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------
# files
# --------------------------------------------------------------------------

def claim_file(source_path, filename, size, mtime, job_id, destination_root=None):
    """
    Register a file before doing any work with it, and report what we already
    know about it.

    Returns the row's status *prior* to this call:
      - a terminal status means the caller should skip the file
      - anything else means it is ours to (re)process, and the row has been
        moved to 'pending' for this job.

    This closes two defects at once. Files that failed early used to leave no
    row at all, because the old code ran an UPDATE against a row it had never
    INSERTed and silently matched zero rows. And files interrupted mid-flight
    were skipped forever on the next run, because the INSERT collided on the
    UNIQUE constraint and the loop simply moved on.
    """
    with _writer_lock:
        conn = get_db()
        row = conn.execute('SELECT status, destination_path FROM files WHERE source_path=?',
                           (source_path,)).fetchone()
        prior = row['status'] if row else None

        if prior in TERMINAL_STATUSES:
            # 'Done' is only meaningful relative to the archive it was done into.
            # Point the tool at a different destination and the previous results say
            # nothing about the new one, so a terminal row whose recorded path lies
            # outside the current destination goes back in the queue. Without this,
            # changing the destination produced a run that did nothing at all and
            # reported success.
            if prior == 'skipped' or destination_root is None:
                return prior
            recorded = row['destination_path']
            if recorded and is_within(recorded, destination_root):
                return prior
            # Requeued for a different archive. The caller decides what to do from
            # the RETURNED status, so it has to be told this file is outstanding -
            # returning the stale terminal status would make it skip the file it
            # just asked us to requeue.
            prior = None

        now = time.time()
        if row is None:
            conn.execute(
                'INSERT INTO files(source_path, filename, size, source_mtime, status, '
                'job_id, updated_at) VALUES(?,?,?,?,?,?,?)',
                (source_path, filename, size, mtime, 'pending', job_id, now))
        else:
            conn.execute(
                'UPDATE files SET filename=?, size=?, source_mtime=?, status=?, job_id=?, '
                'updated_at=?, error=NULL WHERE source_path=?',
                (filename, size, mtime, 'pending', job_id, now, source_path))
        conn.commit()
        return prior


def set_status(source_path, status, **fields):
    """Move a file to a new status. An unknown source_path is an internal error."""
    allowed = {'file_hash', 'dest_hash', 'exif_date', 'date_source',
               'destination_path', 'duplicate_of', 'error'}
    fields = {k: v for k, v in fields.items() if k in allowed}
    conn = get_db()
    sets = ''.join(f', {k}=?' for k in fields)
    cur = conn.execute(
        f'UPDATE files SET status=?, updated_at=?{sets} WHERE source_path=?',
        (status, time.time(), *fields.values(), source_path))
    # A terminal status is the one worth having on disk promptly — it is what
    # stops the next run re-doing the work. The in-between states are pure
    # progress reporting.
    conn.commit()
    if cur.rowcount == 0:
        # Loud rather than silent - this used to be the bug.
        raise KeyError(f'set_status on unregistered file: {source_path}')


def find_verified_duplicate(file_hash, exclude_path, statuses=DEDUP_STATUSES,
                            destination_root=None):
    """
    Find an already-VERIFIED file with this hash *in this archive*.

    Restricting to 'verified' is what prevents the data-loss path: an
    unreadable file left an 'error' row, and its healthy byte-identical twin
    on another drive matched that row and was written off as a duplicate, so
    neither copy ever reached the archive.

    `destination_root` scopes the question to the archive being written now.
    "Duplicate" has to mean "already present HERE", not "seen at some point in
    this database". Without the scope, migrating archive A into a new archive B
    matched A's own files against the rows from the run that built A - every
    file was written off as a duplicate of a copy sitting in A, and B was left
    completely empty while the run reported success. Wiping A on the strength
    of that report would have destroyed the lot.

    A dry run passes ('verified', 'planned') instead, because during a dry run
    nothing ever becomes verified - so without this it would predict zero
    duplicates and badly misrepresent what the real run is going to do.
    """
    placeholders = ','.join('?' * len(statuses))
    rows = get_db().execute(
        f'SELECT source_path, destination_path FROM files '
        f'WHERE file_hash=? AND source_path<>? AND status IN ({placeholders})',
        (file_hash, exclude_path, *statuses)).fetchall()

    for row in rows:
        if destination_root is not None:
            dest = row['destination_path']
            # A copy living in a different archive says nothing about whether
            # this archive already holds these bytes.
            if not dest or not is_within(dest, destination_root):
                continue
        return dict(row)
    return None


def path_is_planned(destination_path):
    """True when a dry-run prediction has already claimed this archive path."""
    row = get_db().execute(
        "SELECT 1 FROM files WHERE destination_path=? AND status='planned' LIMIT 1",
        (destination_path,)).fetchone()
    return row is not None


def path_is_recorded_for(destination_path, source_path):
    """True when *source_path* was archived, or a dry run predicted it, to
    exactly *destination_path*."""
    # By source_path, which is UNIQUE and so indexed, and which this caller
    # already has in hand: one lookup answers both halves of the question.
    row = get_db().execute(
        "SELECT destination_path FROM files WHERE source_path=? "
        "AND status IN ('verified', 'planned')", (source_path,)).fetchone()
    return row is not None and row['destination_path'] == destination_path


def archived_record(destination_path):
    """
    The recorded size and hash of a file already sitting in the archive.

    Resolving a name collision means asking "are these the same bytes?", and the
    answer is already on record for anything this tool archived. Without this,
    every collision re-reads every file it collides with - which is quadratic,
    and a folder of 500 same-named photos ends up reading hundreds of gigabytes
    to answer a question the database could answer instantly.
    """
    row = get_db().execute(
        "SELECT size, dest_hash, file_hash FROM files "
        "WHERE destination_path=? AND status='verified' LIMIT 1",
        (destination_path,)).fetchone()
    if not row:
        return None
    digest = row['dest_hash'] or row['file_hash']
    if not digest:
        return None
    return {'size': row['size'], 'hash': digest}


def path_is_claimed(destination_path, exclude_source):
    """
    True when some other source file is already recorded as the verified owner
    of this archive path. Used to tell 'these bytes are already in the archive
    because I put them there' apart from 'another file got here first'.
    """
    row = get_db().execute(
        "SELECT 1 FROM files WHERE destination_path=? AND source_path<>? "
        "AND status='verified' LIMIT 1", (destination_path, exclude_source)).fetchone()
    return row is not None


def size_is_known(size, destination_root=None):
    """
    Cheap pre-filter. A byte-identical duplicate must have an identical size,
    so when no verified file of this size exists we can skip the standalone
    source-hashing pass and hash during the copy instead - one read of the
    file rather than two.

    `destination_root` scopes the question the way find_verified_duplicate
    scopes its own: a verified copy in some other archive cannot be a
    duplicate here, so it must not cost this file a second read. Without the
    scope, migrating archive A into a new archive B pre-hashed every single
    file - each one's size was "known", from the run that built A.
    """
    rows = get_db().execute(
        "SELECT destination_path FROM files WHERE size=? AND status='verified'",
        (size,))
    for row in rows:
        if destination_root is None:
            return True
        dest = row['destination_path']
        if dest and is_within(dest, destination_root):
            return True
    return False


_VERIFIED_ROWS_SQL = (
    "SELECT source_path, destination_path, file_hash, dest_hash, status FROM files "
    "WHERE status IN ('verified','error') AND destination_path IS NOT NULL "
    "AND file_hash IS NOT NULL")


def verified_rows():
    """Every file that is supposed to be sitting in the archive right now."""
    return [dict(r) for r in get_db().execute(_VERIFIED_ROWS_SQL + " ORDER BY id")]


def has_verified():
    """Whether verified_rows() would return anything, without loading it.

    The audit asks this before it starts, to refuse a run with nothing to
    check. Loading every archived row to answer yes or no read the whole
    table twice on a large archive.
    """
    return get_db().execute(_VERIFIED_ROWS_SQL + " LIMIT 1").fetchone() is not None


def guardian_candidates(after_id=0, limit=64):
    """A rotating slice of verified archive files for a lightweight scrub."""
    conn = get_db()
    query = ("SELECT id, source_path, destination_path, file_hash, size FROM files "
             "WHERE status='verified' AND destination_path IS NOT NULL "
             "AND file_hash IS NOT NULL AND id>? ORDER BY id LIMIT ?")
    rows = [dict(r) for r in conn.execute(query, (int(after_id), int(limit)))]
    if len(rows) < limit:
        remaining = limit - len(rows)
        rows.extend(dict(r) for r in conn.execute(
            query.replace('AND id>?', 'AND id<=?'),
            (int(after_id), remaining)))
    # A small archive can be returned on both sides of the cursor after wrap.
    unique = {}
    for row in rows:
        unique[row['id']] = row
    return list(unique.values())


def load_guardian_state():
    try:
        return json.loads(get_config('guardian_state', '{}') or '{}')
    except (TypeError, ValueError):
        return {}


def save_guardian_state(state):
    set_config('guardian_state', json.dumps(state, separators=(',', ':')))


def finished_count():
    """How many files this archive has already decided, once and for all.

    "Decided" is the terminal set - copied and verified, recognised as a
    duplicate, or deliberately skipped - and not the transient states, and not
    a dry run's predictions. This is the number that makes a resumed run
    legible: without it, pressing Start after a power cut looks identical to
    starting from nothing, and the only clue is a counter moving impossibly
    fast for the first few minutes.
    """
    marks = ','.join('?' * len(TERMINAL_STATUSES))
    row = get_db().execute(
        f'SELECT COUNT(*) n FROM files WHERE status IN ({marks})',
        TERMINAL_STATUSES).fetchone()
    return row['n'] or 0


def finished_among(source_paths):
    """How many of these source paths are already finished.

    `finished_count` is the whole archive - every source ever archived into
    it, and files since deleted or no longer selected. A run's estimate needs
    the files *it* will meet, and only the walk knows those, so the walk asks
    in batches as it counts.
    """
    paths = list(source_paths)
    if not paths:
        return 0
    marks = ','.join('?' * len(paths))
    states = ','.join('?' * len(TERMINAL_STATUSES))
    row = get_db().execute(
        f'SELECT COUNT(*) n FROM files WHERE source_path IN ({marks}) '
        f'AND status IN ({states})', (*paths, *TERMINAL_STATUSES)).fetchone()
    return row['n'] or 0


def finished_size(source_path):
    """The size recorded for *source_path* if it is finished with, else None."""
    states = ','.join('?' * len(TERMINAL_STATUSES))
    row = get_db().execute(
        f'SELECT size FROM files WHERE source_path=? AND status IN ({states})',
        (source_path, *TERMINAL_STATUSES)).fetchone()
    return None if row is None else int(row['size'] or 0)


def bytes_already_archived():
    row = get_db().execute(
        "SELECT COALESCE(SUM(size),0) n FROM files WHERE status='verified'").fetchone()
    return row['n'] or 0


def clear_plan_rows():
    """
    Drop the predictions a dry run left behind. They describe what *would*
    happen, so treating them as history would make a real run skip everything.
    """
    conn = get_db()
    cur = conn.execute("UPDATE files SET status='pending', duplicate_of=NULL, "
                       "destination_path=NULL WHERE status LIKE 'plan%'")
    conn.commit()
    return cur.rowcount


def get_stats():
    conn = get_db()
    counts = {r['status']: r['n'] for r in
              conn.execute('SELECT status, COUNT(*) n FROM files GROUP BY status')}
    total = sum(counts.values())
    job = latest_job() or {}
    return {
        'total_scanned': total,
        'verified':      counts.get('verified', 0),
        # 'copied' is the transient pre-verification state; surface it together
        # with verified so the number never appears to go backwards.
        'total_copied':  counts.get('verified', 0) + counts.get('copied', 0),
        'duplicates':    counts.get('duplicate', 0),
        'skipped':       counts.get('skipped', 0),
        'errors':        counts.get('error', 0),
        'pending':       counts.get('pending', 0) + counts.get('copying', 0),
        'planned':       counts.get('planned', 0),
        'plan_duplicates': counts.get('plan-duplicate', 0),
        'plan_skipped':  counts.get('plan-skip', 0),
        'total_files':   job.get('total_files') or 0,
        'total_bytes':   job.get('total_bytes') or 0,
        'bytes_copied':  job.get('bytes_copied') or 0,
        'phase':         job.get('phase') or 'idle',
        'job_mode':      job.get('mode') or 'copy',
        'job_message':   job.get('message') or '',
    }


def year_breakdown(limit=40):
    """How the archive is distributed across years - a quick sanity check that
    dates were read correctly rather than defaulted to this month."""
    rows = get_db().execute(
        "SELECT substr(exif_date,1,4) y, COUNT(*) n FROM files "
        "WHERE status='verified' AND exif_date IS NOT NULL "
        "GROUP BY y ORDER BY y").fetchall()
    return [{'year': r['y'], 'count': r['n']} for r in rows][:limit]


def add_bytes(job_id, n):
    if not job_id:
        return
    conn = get_db()
    conn.execute('UPDATE jobs SET bytes_copied = COALESCE(bytes_copied,0) + ? WHERE id=?',
                 (n, job_id))
    conn.commit()


def get_recent_files(limit=50, status=None):
    conn = get_db()
    cols = ('SELECT filename, status, destination_path, source_path, duplicate_of, '
            'error, size, file_hash, dest_hash, date_source FROM files ')
    if status and status != 'all':
        rows = conn.execute(cols + 'WHERE status=? ORDER BY updated_at DESC, id DESC '
                            'LIMIT ?', (status, limit)).fetchall()
    else:
        rows = conn.execute(cols + 'ORDER BY updated_at DESC, id DESC LIMIT ?',
                            (limit,)).fetchall()
    return [dict(r) for r in rows]


def iter_all_files():
    """Streaming cursor for manifest export - never materialises the whole set."""
    conn = get_db()
    return conn.execute(
        'SELECT source_path, filename, size, file_hash, dest_hash, exif_date, '
        'date_source, status, destination_path, duplicate_of, error '
        'FROM files ORDER BY id')


def iter_recovery_files(_destination_root):
    """Verified physical copies belonging to one portable archive root."""
    # Duplicates point at these rows, so exporting verified rows produces one
    # entry per real file rather than asking a verifier to hash the same bytes
    # repeatedly.
    return get_db().execute(
        "SELECT destination_path, size, file_hash, exif_date FROM files "
        "WHERE status='verified' AND destination_path IS NOT NULL "
        "AND file_hash IS NOT NULL ORDER BY destination_path")


def reset_errors():
    """Requeue failed files without discarding the successful history."""
    conn = get_db()
    cur = conn.execute("UPDATE files SET status='pending', error=NULL "
                       "WHERE status IN ('error','copying')")
    conn.commit()
    return cur.rowcount


def clear_db():
    conn = get_db()
    conn.execute('DELETE FROM files')
    conn.execute('DELETE FROM jobs')
    conn.commit()
    conn.execute('VACUUM')


if __name__ == '__main__':
    init_db()
    print(f'Database initialised at {DB_PATH} (schema v{SCHEMA_VERSION}).')
