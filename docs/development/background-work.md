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
     ├─ power policy (no thread of its own)
     ├─ the periodic keepers, as jobs on the supervisor (see below)
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
- `Scanner.deferred` shows everything holding the indexer, for the console,
  each reason once.
- A claim is a reason **and a holder**. Callers that give no holder share a
  claim with anybody else giving the same reason alone, as they always did.
  A supervisor job holds in its own name (`defer(reason, holder=job.name)`),
  so a job and the Storage page's check, or two jobs, giving the same reason
  never let go of each other's claim. `Scanner.holding()` lists them.

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

## The supervisor

`ninaivu/server/supervisor.py` runs the periodic keepers. Each is a `Job`:
what one round does, when rounds happen, and what it needs of the machine.
The supervisor runs each job on its own named thread (the name the keeper's
thread always had), logs and retries a round that raises (so a loop never
dies on one error), honours the job's claims with the scan's rules above,
stops everything within a budget, and lists every job's state at
`GET /api/admin/jobs/background`.

### Declaring a job

```python
from ..server.supervisor import NETWORK, Job, every, supervisor_of

class Keeper:
    JOB = "ninaivu-something"           # its thread name, as before

    def start(self) -> None:
        supervisor_of(self).add(Job(
            self.JOB, self._look, every(600, first=60),
            claims={NETWORK}, log=log, failure="could not do the thing",
            retry_most=0, progress=lambda: {"busy": self.busy}))

    def stop(self, timeout: float = 5.0) -> None:
        supervisor_of(self).stop_job(self.JOB, timeout)
```

- **When**: `at_start()` (once, until a round succeeds), `every(N or
  callable, first=)` (N seconds after each round ends; a callable is asked
  afresh each round; `first` defaults to one interval, which is what
  `while not stop.wait(N)` did), `on_request()` (a round per
  `supervisor.request(name)`; asks during a round come to one more round) or
  `after(job)`. `request()` also wakes an `every` job early.
- **Claims**: from `library_disk`, `archive_disk`, `cpu`, `index_write`,
  `network`. The first three make the scan stand down and need a `reason`;
  a job that only reads uses a reason in `READ_ONLY_CLAIMS`, and one that
  moves files sets `moves_files=True` and waits for any other mover.
  `index_write` and `network` are shown, not arbitrated. Declaring a claim
  the scan stands down for where the keeper held none is a change of
  behaviour, not a refactor.
- **Failures**: the round raises; the supervisor logs `failure` (on `log`)
  with the traceback and tries again after `retry_first`, doubling up to
  `retry_most`, never sooner than the schedule. `retry_most=0` keeps a
  scheduled job to exactly its schedule, which is what the old loops did.
- **Stopping**: give `interrupt` when a round can be cut short (usually the
  flag the work already watches); otherwise a stop waits for the round.
- Keep the keeper's old `start`/`keep`/`stop` methods as thin wrappers, and
  a `supervisor = None` attribute: Ninaivu hands each keeper the shared
  supervisor in `Services.__init__`, and one used by itself (as much of the
  test suite does) gets its own from `supervisor_of`.

### The jobs on it

| Thread | Keeper | When | Claims |
|---|---|---|---|
| `bin-sweep` | the recycle bin, `ninaivu/__init__.py` | every day | index_write |
| `ninaivu-digest` | weekly photograph, `utils/digest.py` | every 20 min | network |
| `ninaivu-library-drives` | `storage/library_drives.py` | every 15 s | |
| `ninaivu-index-copy` | `cloud/index_copy.py` | every 30 min | network |
| `ninaivu-restore-test` | `cloud/restore_test.py` | every hour | network |
| `ninaivu-backup` | `storage/backup.py` | every 15 min (none while off) | |
| `ninaivu-disks` | `storage/disk_health.py` | 1 min in, then every 10 min | |
| `archive-guardian` | `archive/guardian.py` | 5 s in, then every 5 min | |
| `ninaivu-second-copy-keeper` | `storage/mirror.py` | every 10 min, or `look_now()` | |
| `ninaivu-offsite-schedule` | `cloud/offsite.py` | every 10 min | network |
| `ninaivu-xmp-schedule` | `storage/xmp.py` | every hour | |
| `ninaivu-xmp-read-timer` | `storage/xmp_read.py` | every 30 min | |
| `ninaivu-daily-catchup` | `server/catchup.py` | every 30 s | |
| `mugil-keeper` | `mugil/courier.py` | every 10 min, sooner for the first copy | network |
| `mugil-wake` | `mugil/wake.py` | every 30 s | network |
| `mugil-display` | `mugil/display.py` | at once, then every 10 min | |
| `ninaivu-descriptions` | `runtime/describer.py` | 2 min in, every 30 min, or `wake()` | |
| `ninaivu-story-words` | `runtime/transcriber.py` | 1 min in, every 30 min, or `wake()` | |
| `ninaivu-video-moments` | `media/video_moments.py` | 3 min in, every hour, or `wake()` | |

Not on it yet, each with its reason:

- The scan (`mv-scan`, `mv-rescan`), the archive job (`archive-job`,
  `archive-resume`) and the cloud upload (`ninaivu-cloud`): not periodic,
  and each has its own stop and resume rules; they move over one at a time.
- The job engine (`ninaivu/engine`): a run is worked through on the caller's
  own thread (the scan's), with the caller's database connection, and hands
  its report back to it. Running it on a supervisor thread means handing the
  run and its connection across threads and waiting on the answer.
- The model host (`runtime/host.py`): it has no thread to keep. It is started
  by the call that needs it, under the call's lock, leaves when idle, and a
  crash is the caller's to retry; models that fail to load are already held
  off with a growing wait.
- Runs started on request or carried on at start-up (the copies' runs,
  sending the originals, freeing space, checking a copy made by hand,
  rebuilding, an update, the smaller-videos trial), the beacon's listening
  socket, and the hand-over keeper (`mugil/handover.py`).
- The repair's check schedule (`ninaivu-check-schedule`), the replica's
  keepers (`ninaivu-replica-front`, `ninaivu-replica-receive`) and the
  tailnet certificate (`ninaivu-tailnet-cert`): periodic, not yet moved.

## Adding a new background job

1. If it runs on a schedule or on request, declare it as a `Job` on the
   supervisor (above), with the thread name it is to be known by. Otherwise
   run it on one named daemon thread, and refuse a second start while it
   runs.
2. If it reads the whole library or keeps the CPU busy for minutes, it
   claims the disk or the processors (on the supervisor) or runs inside
   `scanner.held(...)`. If it only reads, add its reason to
   `READ_ONLY_CLAIMS`.
3. Commit often: every second or so, never across slow work.
4. If it should survive a restart, write itself down with `resume.want` and
   cross itself off with `resume.done`.
5. Write one log line when it starts and one when it ends, with the counts.
   That is how a stalled job gets noticed.
