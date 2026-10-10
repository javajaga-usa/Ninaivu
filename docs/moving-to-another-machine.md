# Moving a library to another machine

A first scan of a large library is most of a day: every file read, two
thumbnails written for each, then every picture described by a model. None of
that has to happen twice. The index, the thumbnails and everything the AI
worked out are all reusable on the next machine.

Whether they *are* reused comes down to one thing: the absolute path the
library ends up at.

## Why the path decides it

A thumbnail's filename is a hash of `"<absolute root>|<relative path>"`
(`media.thumb_base`). The relative path never changes when a library moves, but
the root does — and that is enough to change every thumbnail's name:

| Library root | Thumbnail for `2019/07/IMG_1.JPG` |
| --- | --- |
| `E:\Photo Archive` | `eb/eb6aea23210fd8d3867b9ca186921d032e1ae560` |
| `F:\Photo Archive` | `f7/f723d2051bf7dbb95864b33e19bef895faf82090` |
| `/srv/archive` | `b7/b7d76096cbbbd83120b82fd3819b4989a08c52db` |

So a library that comes back on a different drive letter has an index pointing
at a folder that is not there and a thumbnail tree that matches nothing. Ninaivu
recovers by rebuilding both, which is the expensive half of a first scan.

Nothing about the pictures has changed, though. `ninaivu reroot`
renames the thumbnails to their new names and rewrites the stored paths, which
takes minutes rather than hours.

## What to copy

Two things: the library itself, and Ninaivu's state directory
(`%USERPROFILE%\.ninaivu` on Windows, `~/.ninaivu` elsewhere, or wherever
`NINAIVU_STATE_DIR` points).

| In the state directory | Why |
| --- | --- |
| `index.db` | Everything Ninaivu knows. Copy `-wal` and `-shm` too, or stop Ninaivu first so there are none |
| `thumbs/` | The expensive part. Several GB on a large library |
| `archive.db` | The Archive's record of what it has copied |
| `models/` | Downloaded AI models, so they are not fetched again |
| `tls/` | Ninaivu's certificate authority. **Keep this.** A new one means re-trusting Ninaivu on every phone and laptop in the house |
| `faces/`, `renditions/`, `avatars/` | Face data, working copies, profile pictures |
| `config.json` | Library folders, settings, notification details |
| `google.json`, `cloud-encryption.json` | Cloud backup sign-in and encryption key, if used |
| `gemini.json` | The Gemini key set in the console, if the extension is on |

`ninaivu backup` makes a bundle of everything above *except*
`thumbs/`, which it leaves out deliberately because it can be rebuilt. For a
move you want the thumbnails as well — that is the whole point — so copy the
folder rather than relying on a backup bundle.

## Timestamps matter more than anything else here

Ninaivu decides a file is unchanged from its **size and modification time**. A
copy that resets modification times makes every file look new, and the whole
library is re-indexed and re-thumbnailed no matter what else you do.

```bat
robocopy E:\Photo Archive F:\Photo Archive /E /DCOPY:T /COPY:DAT
```

`/COPY:DAT` keeps data, attributes and timestamps; `/DCOPY:T` keeps them on the
folders too. On Linux or macOS, `rsync -a` does the same. Dragging a folder in
Explorer preserves file times, but verify a few afterwards rather than assume.

### The cloud backup is not sent again either way

The cloud backup's record of what has gone to Google Drive is in `index.db`,
so it moves with the state directory, and re-rooting rewrites it along with
everything else. The Google sign-in (`google.json`) and the encryption key
(`cloud-encryption.json`) move with it too, and uploads carry on into the same
Drive folder: whatever was already sent is not sent again, and a file that was
half-way up carries on from where it stopped if the new machine starts within
about a week, when Google expires the upload session. For encrypted backups,
copy `cloud-upload-cache/` as well to keep that half-sent file.

A copy that loses the timestamps no longer changes that. A file whose time
moved but whose size did not is checked by its contents — its size and both
ends of it, against what they were when it was uploaded — and is only sent
again if they differ. That costs a read of each file's first and last 64 KB,
once; it used to cost uploading the whole library a second time. (It still
costs the full re-index described above, so keep the timestamps anyway.)

If the Google permission ever has to be connected again on the new machine,
the new console address has to be added as a redirect URI in your Google
Cloud project first — or connect from `localhost` on that machine, which is
always accepted.

## The move

1. **Stop Ninaivu** on the old machine. Not just idle — stopped, so the index
   has no write-ahead log outstanding.
2. **Copy the library**, preserving timestamps as above.
3. **Copy the state directory** to the same place on the new machine.
4. **Check the drive letter.** If the library is at the same absolute path as
   before, skip to step 6. On Windows you can often avoid this step entirely by
   assigning the old letter in Disk Management.
5. **Re-root**, if the path changed:

   ```bat
   ninaivu reroot --from "E:\Photo Archive" --to "F:\Photo Archive" --dry-run
   ninaivu reroot --from "E:\Photo Archive" --to "F:\Photo Archive"
   ```

   The dry run changes nothing and prints what it would do. The tool refuses to
   run while Ninaivu is running, because it renames the files Ninaivu is serving.
6. **Start Ninaivu** and watch the first scan. It should report **0 files
   indexed** and rebuild nothing.

### When Ninaivu finds the library itself

Each library folder holds a small file, `.ninaivu-library`, with a random id
in it, and the state directory remembers which id each library folder had
(`library-ids.json`). When a library folder is not where it was at start,
Ninaivu looks for that id on the computer's other disks: the same folder on
every other drive letter on Windows, under every disk in `/Volumes` on a Mac
(which is how a disk mounted as `/Volumes/Photos 1` is found), and under
`/media` or `/mnt` on Linux. Found in exactly one place, the library is
re-rooted there, as in step 5, before anything else starts.

It does nothing when the id is on a network share, because that is where
backup copies live, or when two folders carry it, because one of them is a
copy and only you know which. Then step 5 is still the way. Keep the
`.ninaivu-library` file when you copy a library.

## What re-rooting does

- Renames every thumbnail to the name the new root produces.
- Rewrites `root` in each table that records which library folder a row belongs
  to: `assets`, `occasions`, `visibility_batches`, `recycled`, `scan_runs`,
  `bitrot_records`, `folder_rules`, `pending_uploads`, `cloud_uploads`.
- Rewrites `assets.thumb` to match the renamed files.
- Moves the folder a member is confined to, if one is set.
- Updates the library folders in `config.json`.

Drive letters are matched without regard to case — Windows does not care, and
somebody typing `f:` means the drive they have. From the first match onwards
the index's own spelling is used, because SQLite compares text exactly even
where the filesystem does not.

An interrupted run can be finished by running it again; a thumbnail already at
its new name is not looked for at the old one. A second run after a finished
one is refused, because there is nothing left under the old root.

### The AI models look after themselves

Downloaded models are found through `.ai-models/settings.json`, which records
*absolute* paths — so a Ninaivu that moved to another folder or machine would
point at where its models used to be, show them as installed, refuse to
download them again, and fail to use them. Ninaivu now repairs this on every
start: a setting that is missing, or that names a file that is not there, is
pointed back at the model where it actually is. Copy `.ai-models/` along with
everything else and there is nothing to do.

A key for Google Gemini set in the console is kept in the state directory
(`gemini.json`), not with the models, and moves with it. An earlier version
kept it in `.ai-models/settings.json`; a key still there is moved into the
state directory the first time it is read. A key set in the server's
environment does not move, and has to be set again on the new machine.

### What it does not touch

- **Renditions and face data** are keyed by asset id, not by path. They need
  nothing.
- **`archive.db`** records where the Archive copied files *from*. Those are
  source paths, not the library root, and only you know how they map onto the
  new machine. Check the Archive tab after a move if you rely on it.
- **The media files themselves.** This only ever renames thumbnails inside the
  state directory.

## Roughly how long

Measured against a 184,496-item library with 342,906 thumbnails, on a machine
managing about 1,700 renames a second:

| Step | Time |
| --- | --- |
| Working out what moves | under a second |
| Reading the thumbnail tree | ~2 seconds |
| Renaming 342,906 thumbnails | ~3 minutes |
| **Rebuilding them instead** | **~14 hours** |

Copying the library and the thumbnails takes as long as your disks take, and
will dominate everything above.

## If it did not work

**Ninaivu indexes the whole library again.** The modification times did not
survive the copy. Re-copy with `/COPY:DAT /DCOPY:T` or `rsync -a`; there is no
way to recover the old times once they are gone, and the rescan is the only
route from there.

**Thumbnails are blank and slowly come back.** The re-root was not run, or was
run with a `--from` that did not match. Check what the index actually records:

```bat
ninaivu reroot --from "anything" --to "anything" --dry-run
```

It refuses and prints the roots it knows about.

**"Nothing in the index is under …"** — the `--from` path is not the one
stored. Use the spelling the message prints.

**Every device warns about the certificate.** `tls/` was not copied, so a new
authority was generated. Restore `tls/` from the old machine, or reinstall
`ninaivu-ca.crt` on each device.
