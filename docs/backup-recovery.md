# Backup and recovery

Create backups with `ninaivu backup --out <backup-folder>`.
Backups contain Ninaivu's index and supported configuration, certificate, avatar,
and archive-log files. They do not contain the original media library.

A missing optional entry, such as a certificate on an HTTP-only installation,
is allowed. A missing library index or a failure to copy an existing supported
entry aborts the backup. No completed bundle is published on that failure.

## Where the scheduled backups go, and what they leave out

Ninaivu also writes a bundle on its own schedule (`backup_every_hours`), into
`backups` inside the state folder unless `backup_dir` names another folder.
A bundle holds the settings (including the mail password), the certificate
authority's private key and every name in the index, so:

- `backup_dir` cannot be inside a library folder or the second copy's folder.
  The setting is refused when it is saved, and a backup is refused when a
  library folder added later makes it so.
- When `backup_dir` is outside the state folder, the scheduled bundle leaves
  out the cloud encryption key (`cloud-encryption.json`), the Google
  sign-in (`google.json`), the Gemini key set from the console
  (`gemini.json`) and the HTTPS certificates with their private keys (`tls/`
  and the older `ninaivu-ca.*` and `ninaivu.*` files). It also empties the
  mail password and the notification webhook in the bundle's copy of the
  settings. The bundle's manifest lists what was left out (`left_out`) and
  which settings were emptied (`secrets_blanked`), so nothing is missing
  without saying so.
  - A restore onto the **same computer** keeps the keys, certificates,
    password and webhook it has.
  - A restore onto a **new computer** asks for them again: bring the cloud
    key back from the recovery file (or the passphrase) on the Mugil page,
    connect Google again, paste the Gemini key, and type the mail password
    and webhook on the Settings page. Ninaivu makes a new certificate
    authority, so each phone and computer is told to trust Ninaivu once more.
  The off-site copy's secret is never in a bundle.
- `ninaivu backup --out <folder>` writes the **full** bundle, keys and
  passwords included: it is the one to keep for a complete recovery, somewhere
  only you can read (an encrypted disk, say). When the folder is outside the
  state folder it says so. `ninaivu backup --out <folder> --without-secrets`
  writes the same bundle a scheduled backup to another disk writes.
- The folder is made readable by Ninaivu's own account only. On Windows, a
  state or backup folder outside the user's profile (for example on `D:` or a
  USB disk) has its inherited permissions removed and only that account and
  SYSTEM given access. A FAT or exFAT disk keeps no permissions at all: keep
  backups there only on a disk nobody else uses.

## A full export, for another computer

A backup leaves out what Ninaivu can make again: the thumbnails, face crops,
memory films, letters, photo books, coloured photographs and working copies.
On a large library making them again is days, so a move to another computer
takes a **full export** instead: `ninaivu backup --full --out <folder>`, or
**Export** under **Export and import the whole index** on the console's
Migration page. It is the same bundle, named `ninaivu_export_…` so the
scheduled backups never count or prune it, with those folders in it too.
`ninaivu restore <file>`, or **Import and restart** on the same page, puts it
back, replacing the thumbnails and the rest along with the index; a backup
restored the same way keeps the ones the computer has. See
[Moving a library to another machine](moving-to-another-machine.md).

Every bundle carries the thumbnail keys (`thumb-keys.json`), which the
thumbnails are named by.

The copy of the index Mugil sends to Google Drive is only sent with cloud
encryption on, and never carries passwords, tokens, the sign-in record, share
links or the sign-in counters.

The sign-in record (the audit log) keeps a year, and the archive keeps the logs
of its last 50 runs; older ones are removed once a day.

## How much a power cut can take

The index (`index.db`) and the archive database run SQLite in WAL mode with
`synchronous=NORMAL`. A crash of Ninaivu, or of the computer, never leaves them
damaged. A **power cut** can lose the commits made in the moment before it,
which were in the write-ahead log and not yet synced: usually the last few
seconds of indexing, which the next scan redoes, or an edit made just then.
Photographs themselves are never in the index and are not affected.

- To trade some indexing speed for those seconds, start Ninaivu with
  `NINAIVU_DB_SYNCHRONOUS=full`. Each commit then waits for the disk.
  Anything other than `normal` or `full` is ignored, with a warning in the log.
- A small UPS, or a laptop's battery, covers the same risk without the cost.
- Keep the state folder (`NINAIVU_STATE_DIR`) on a **local** disk. SQLite's
  WAL mode needs shared memory on the same machine and is not for a network
  share. The library and the backup folder can be on a NAS; the state folder
  should not be.

Thumbnails and other caches can be rebuilt. The household's own work (names on
faces, albums, visibility, recorded stories, accounts) is in the index and in
the state folder, which is why the scheduled backups above exist.

## Checking and repairing the index

Ninaivu checks its databases (`index.db`, `archive.db`, `mugil-sent.db`) at
start, before anything opens them, when there is reason to:

- the index has just moved to another folder, or Ninaivu was pointed at the
  index in another folder (Advanced settings);
- an error such as `database disk image is malformed` was logged while
  Ninaivu ran (it notices this in its own log);
- an administrator pressed **Check now and restart** on the console's
  Migration page.

The check is SQLite's `quick_check`: every page read once. It reads a
database in the order of its tables rather than of the file, which on a hard
disk (an index on a USB drive such as an N300) is a great many small reads
far apart, so each database is first read straight through, at the disk's
full speed, while there is memory free to hold it; the check then reads from
memory. The Control Panel shows how far it has got. After a move, which
already compared every file it copied with the original, each database is
only opened and its tables listed. A sound index is left untouched. A damaged database is handled
the way SQLite recommends:

1. It is copied aside unchanged, as `<name>.damaged-<date>/` in the state
   folder, and the copy is compared with the original before anything else
   happens. Only the newest such copy is kept.
2. `REINDEX` rebuilds damaged indexes from their tables.
3. If that is not enough, the database is rebuilt into a fresh file: the
   schema, then every row that can still be read, with its id, table by
   table, stepping over unreadable stretches; then indexes and triggers, and
   the photo search is rebuilt from the photographs.
4. A repaired database is used only if it passes the full `integrity_check`.
   Rows whose parent row was lost are counted (`foreign_key_check`) and
   reported.

Photographs still in the library that lost their row come back with the next
scan; names on faces, captions or albums in an unreadable stretch do not.
When no repair passes, the original is put back exactly as it was, and the
console says so. **Restore** a backup then (above). Ninaivu never restores a
backup by itself, as that undoes everything since the backup.

Repairing needs room for two more copies of the database beside it; without
that room nothing is changed and the console says why. The Control Panel
shows each step while it runs (`checking the index`, `repairing the index`).
Delete the `.damaged-` folder once Ninaivu works as it should.

## Verify my backup: the backup drill

Admin → Health → **Backup drill** → **Verify my backup** proves that every
copy would bring the photographs back, in a few minutes. From each copy that
is set up it restores five photographs chosen at random into a temporary
folder in the state folder (`backup-drill/`):

- **Second disk**: read off the disk.
- **Off-site copy**: fetched and decrypted.
- **Google Drive**: downloaded through the same checks a real restore makes.
- **The Secondary**: read back there over the pairing.
- **Index backup**: the newest one unpacked and opened.

Each photograph is checked against the library's content fingerprint, not
only against what the copy recorded when it was made. A copy whose own
record agrees with its bytes but not with the photograph is caught. A file
that has changed in the library since it was copied is checked against the
copy's record instead, and the result says so.

Nothing in the library or in a copy is changed. The temporary folder is
removed afterwards, and the last ten results are kept in
`backup-drill.json` in the state folder.

## Restore procedure

1. Stop Ninaivu and disable automatic restarts for the duration of the restore.
   The tool refuses a running process recorded in `ninaivu.run`, but this check
   is not a lock against an external service manager starting a new process.
2. Confirm `NINAIVU_STATE_DIR` points to the intended state directory. Allow
   space beside it for a replacement copy of state, including local caches and
   stored backups, plus temporary space for the extracted bundle.
3. Run `ninaivu restore <bundle.tar.gz>`.
4. Check for the successful completion message, start Ninaivu, and verify profiles,
   albums, visibility, and library paths before removing any recovery directory.

The restore verifies an exact match between the manifest and archive files,
their sizes and SHA-256 checksums, and SQLite `quick_check` results. It rejects
unsafe paths, duplicate entries, symlinks and hardlinks in the archive. A checksum
establishes consistency with the manifest; it does not authenticate who made a
bundle. Use backups from a trusted source.

The destination must be a regular directory that can be renamed from its parent.
A filesystem root, a symlink, or a junction cannot be the destination. A mounted
volume root may also refuse the rename. For containers, restore from the host
or into a regular subdirectory of the mounted volume, and point Ninaivu at that
restored directory. Permission or mount errors leave the original in place.

## What changes during restore

The tool prepares a complete replacement beside the destination before moving
the original. Backed-up files and directories replace their existing counterparts,
including removing optional state that was absent from the backup. Unrelated
local files, such as credentials and caches, are preserved in the replacement.
Old SQLite WAL, SHM and rollback-journal files are excluded so they cannot be
applied to the restored database. The stale run file is excluded too.

The original directory is renamed to a unique sibling named
`.ninaivu_pre_restore_<state-name>_<unique-id>`. Its location is printed before
the rename. The prepared directory then takes the original location. The old
directory remains available after success and is not pruned automatically.

## Interrupted restore

The two directory renames are not a single atomic transaction. If publication
fails or Ctrl+C interrupts it, the tool attempts to rename the original directory
back. If rollback fails, the error reports the retained original directory.

After a hard process termination or power loss, keep Ninaivu stopped. Inspect the
destination and the printed recovery directory. If the destination is absent,
rename the retained original back to the configured state-directory name before
restarting. If a destination exists, preserve both directories until you have
verified which state to use. Do not merge old WAL files into the restored state.

These failure paths are covered by isolated Windows regression tests. Actual
power-loss testing, Linux restore drills, and container-mounted-volume restores
still require validation in their deployment environments.

## The off-site copy

The off-site copy (Admin, Cloud: a folder elsewhere or an S3-compatible bucket)
is encrypted with the backup key and restored with `ninaivu offsite-restore`.
Without Ninaivu installed, `python ninaivu/cli/offsite_restore.py` from a copy
of the source needs only Python and the `cryptography` package.

```bash
ninaivu offsite-restore --recovery ninaivu-recovery-XXXX.json <copy> <output>
ninaivu offsite-restore <copy> <output>        # asks for the passphrase
```

The passphrase route uses the `ninaivu-encryption.json` kept beside the
manifest. Each file is checked against the SHA-256 recorded when it was sent.
A file that changed is kept in the copy in every version sent; the newest is
restored, and `--all-versions` brings the older ones back too, beside it.

The destination carries an identity file, `ninaivu-offsite-id.json`. When the
destination's settings or the backup key change, or the identity file is not
the one the record was made against (another disk at the same path, a bucket
that was emptied), what the record says was sent is not believed there and is
sent again. A folder whose `ninaivu-offsite` folder has gone while files were
sent there is refused, as a disk that is not mounted, instead of a new copy being
started on the system disk; if the copy there was removed on purpose, start it
over (`POST /api/offsite/start` with `{"start_over": true}`).

A manifest already at the destination is read before each run and merged with,
never replaced; the one before each run is kept as `manifest.prev.ninaivu`. A
manifest made with a different key stops the run with a message: import that
key's recovery file, or choose another destination.

## The family archive on a drive

Every copy above needs Ninaivu, or somebody who knows it, to be useful. The
family archive does not. Backup & health → Handover → **Family photo archive
on a drive** copies the photographs and videos to a USB drive (or any folder
outside the library and Ninaivu's own folder) as a folder called
`Family photo archive`:

- `Open me.html` opens in any web browser, with no internet and no server:
  the photographs by month, with the people named in them, the albums, the
  places, and a search box. Click one to see it full size; the arrow keys go on.
- `Read me first.html` says what the drive is and carries a letter from the
  household, written on the same page (who to ask, where the rest is kept).
  Never put a password or a key in it.
- `photos/` holds the files themselves, in their own folders and under their
  own names; `thumbs/` and `data.js` are what the page reads.

By default only what the family sees goes in; **Include hidden photographs**
adds the hidden ones. Flagged items, sound recordings and the bin never go.
**Smaller copies of the photographs** writes them at 2048 pixels as JPEG for
a small drive (videos are copied as they are). The originals keep their
location; a small copy has none.

Made again on the same drive, it copies only what is new or changed and leaves
everything already on the drive in place, so a photograph deleted from the
library stays on the archive. A stopped run carries on where it stopped the
next time, and keeps the page of the last whole run until then. Once made, the
archive is listed on the handover sheet with when it was last made.

The console's routes are `GET /api/admin/keepsake` (progress, the letter, the
last folder and when it was last made) and `POST /api/admin/keepsake` with
`{"folder": "...", "everything": false, "small": false, "letter": "..."}` to
make it, `{"stop": true}` to stop, or `{"letter": "..."}` alone to save the
letter.
