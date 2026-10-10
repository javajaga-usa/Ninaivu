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
  sign-in (`google.json`) and the Gemini key set from the console
  (`gemini.json`). After a restore onto a new computer, bring the key
  back from the recovery file (or the passphrase) on the Mugil page, connect
  Google again and paste the Gemini key again. A restore onto the same
  computer keeps the ones it has. The off-site copy's secret is never in a
  bundle. `ninaivu backup --out` still writes everything: it is a copy
  somebody asked for, to a place they chose.
- The folder is made readable by Ninaivu's own account only. On Windows, a
  state or backup folder outside the user's profile (for example on `D:` or a
  USB disk) has its inherited permissions removed and only that account and
  SYSTEM given access. A FAT or exFAT disk keeps no permissions at all: keep
  backups there only on a disk nobody else uses.

The copy of the index Mugil sends to Google Drive is only sent with cloud
encryption on, and never carries passwords, tokens, the sign-in record, share
links or the sign-in counters.

The sign-in record (the audit log) keeps a year, and the archive keeps the logs
of its last 50 runs; older ones are removed once a day.

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
