# Background work: who runs, who waits, and why

Ninaivu runs on one household machine, usually one where the library lives on a
single spinning disk and the AI runs on a CPU with no GPU. Every long job wants
the same three things: **that disk**, **every core**, and **the SQLite write
lock**. This page is the map of every thread that competes for them and the
rules that decide who goes first.

## Start-up

```
python -m ninaivu
 └─ Services.start()                                     ninaivu/__init__.py
     ├─ power policy, archive guardian, backup scheduler (own threads)
     └─ ninaivu-boot thread
         ├─ load the AI engine (can take minutes on a first run)
         ├─ resume an interrupted archive job   → archive-resume thread
         ├─ first library scan                  → mv-scan thread (+ watcher)
         └─ resume interrupted jobs (storage/resume.py):
             cloud upload, storage check, model downloads
```

A job that was running when Ninaivu stopped wrote itself down when it started
(`storage/resume.py`) and is carried on here. Nothing else starts on its own.

## The jobs

| Thread | What it does | Disk | CPU | Writes |
|---|---|---|---|---|
| `mv-scan` / `mv-rescan` | Walk, index, then the passes below | heavy read | heavy (AI) | index rows |
| `archive-job` | Consolidate drives into the archive | heavy read + write | hashing | `archive.db`, files |
| `ninaivu-straighten-*` | Survey / apply orientation | full-size reads | heavy (model + faces) | proposals, thumbnails |
| `ninaivu-scrubber` | Storage check: hash every original | heavy read | hashing | check results |
| `ninaivu-cloud` | Upload to Google Drive | streaming read | light (encryption) | one row per file |
| `ninaivu-backup` | Snapshot state into a bundle | state dir only | gzip | none |
| proxies | Transcode a video for the browser | one file | ffmpeg | proxy file |
| downloads / installs | Models, ffmpeg, extras | none | none | files |

### The scan, in order

1. **Walk**: find new, changed and removed files (size and mtime).
2. **Index**: read and thumbnail changed files on `cfg.workers` threads, with
   at most four in flight per worker.
3. **Library passes** (seconds): quality, duplicates, live photos,
   occasions, screenshots.
4. **Analysis passes** (hours on a first run, nothing afterwards): tagging,
   place names, video keyframes, text reading, faces.

Every pass only looks at rows that still need it, so an idle scan should take
about as long as the walk. When a scan does real work while nothing on the
disk has changed, a pass is not marking its rows as done.

## Rule 1: the library scan yields to everything

The indexer is the one job that is always cheap to stop and restart: the walk
skips anything whose size and mtime already match. So any job that needs the
disk or the processors takes a **claim** on it:

```python
with scanner.held("checking the library's storage"):
    ...                      # the scan stands down, or queues if asked for
```

- `Scanner.defer(reason)` adds a claim. A running scan stops, and any scan
  asked for in the meantime is queued rather than lost.
- `Scanner.resume(reason)` releases **that** claim. The queued scan starts only
  once no claims are left.
- `Scanner.deferred` shows everything holding the indexer, for the console.

| Holder | Claim | Kind |
|---|---|---|
| Archive job | its job label, released while the archive sits idle | moves files |
| Approving a family upload | `approving a family upload` | moves files |
| Changing a creation date | `updating a file creation date` | moves files |
| Straighten survey / apply | `straightening photographs` | read-only |
| Storage check | `checking the library's storage` | read-only |

Operations that move files refuse to start while another file-moving claim is
held. They do not wait on read-only claims: an approval should not have to
wait hours for a storage check (`READ_ONLY_CLAIMS` in `media/scanner.py`).

There used to be a single slot here. The first job to finish released the
indexer even when another job still needed it.

## Rule 2: the watcher reacts to changes, not to reads

The watcher (`watchdog`) schedules a rescan after a quiet period when
something under a library folder changes. Windows reports a **last-access**
update as "modified" whenever last-access updates are enabled, and they are by
default. So every read of a photo counted as a change: the cloud upload, the
keyframe pass and the storage check all did it. The result was 1,071 full
scans in one day on a library where nothing had changed.

- `_worth_a_rescan` ignores "modified" events whose file mtime is older than
  `WATCH_FRESH_SECONDS`, plus the `opened` / `closed_no_write` events. Created,
  deleted and moved events always count.
- A change seen *during* a scan is not dropped. It is asked about again every
  `RESCAN_RETRY_SECONDS` until the running scan has finished.
- A change seen while the indexer is held is queued and runs on release.

## Rule 3: nobody holds the write lock for long

SQLite allows one writer. Other connections wait up to `busy_timeout` (30 s),
then fail with "database is locked".

- Library-wide passes write only what changed (`rebuild_occasions` writes a
  difference; the full-text trigger fires only when a searched column
  changes).
- Batch commits are bounded by time as well as count (straighten apply: 50
  rows or one second).
- The backup copies each database in one step, and holds `db._write_lock` only
  for the copy, not for gzipping.
- The cloud upload treats "database is locked" as a reason to wait, not to
  stop. The record of a finished upload is retried, so a file already on Drive
  is never sent twice.

## Rule 4: CPU budgets come from one place

`utils/resources.budget()` sets the worker and compute-thread counts for the
chosen mode (standard, performance, power-saving), and the environment
variables it exports cap OpenMP, MKL and OpenCV. Rule 1 means the CPU-heavy
jobs (scan analysis, straighten, storage check) no longer run on top of one
another, so each can use the full budget.

## Adding a new background job

1. Run it on one named daemon thread, and refuse a second start while it runs.
2. If it reads the whole library or keeps the CPU busy for minutes, run it
   inside `scanner.held(...)`. If it only reads, add its claim to
   `READ_ONLY_CLAIMS`.
3. Commit often: every second or so, never across slow work.
4. If it should survive a restart, write itself down with `resume.want` and
   cross itself off with `resume.done`.
5. Write one log line when it starts and one when it ends, with the counts.
   That is how a stalled job gets noticed.
