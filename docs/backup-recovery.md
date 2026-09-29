# Backup and recovery

Create backups with `ninaivu backup --out <backup-folder>`.
Backups contain Ninaivu's index and supported configuration, certificate, avatar,
and archive-log files. They do not contain the original media library.

A missing optional entry, such as a certificate on an HTTP-only installation,
is allowed. A missing library index or a failure to copy an existing supported
entry aborts the backup. No completed bundle is published on that failure.

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
