# Linux app and relocation follow-up — 3 November 2026

Starting revision: `946a718`. This implements the audit follow-ups: relocation
browser coverage, portable upgrade/Go back compatibility, guided recovery-copy
cleanup, broader test execution, and a dependency advisory scan. It also adds
the requested Linux app behavior, with `~/.ninaivu` in the user's home folder
as the default index location.

## Linux app

- `Setup Ninaivu.sh` prepares a checkout and installs application-menu and
  Desktop entries. `--apps-only` rebuilds entries without reinstalling Python
  dependencies. A native **Open Ninaivu** launcher is built when `cc` is
  available; it handles both checkout and portable layouts.
- Both Linux menu entries open the Control Panel without a terminal. The Tk
  window class matches the desktop entry for application grouping and icons.
- Checkout and portable launchers default to `~/.ninaivu`. Existing app-local
  libraries and forwarding notes remain usable, and explicit state-directory
  overrides are preserved. Setup was run locally with `--apps-only`.
- Linux sign-in startup uses a user XDG autostart entry and carries the selected
  index/environment. A different program copy cannot disable the selected
  copy's entry. It is available as a checkbox, not enabled automatically.
- Switching resource mode while running now saves the requested mode **after**
  Stop captures the old process settings. Previously this reverted to the old
  mode before restart. A real isolated Linux server test covers Start, Stop,
  Restart, mode selection, URLs, logs, and a home-folder index.
- Startup failures such as an unavailable relocated drive are shown in a
  dialog. The standard-library stop helper follows forwarding notes without
  requiring Flask and clears the live stale record, not a recovery copy.

## Relocation, portable updates and Go back

Portable discovery resolves the live index instead of choosing the original
recovery copy. Shared home-folder or relocated data stays in place during an
upgrade. Ownership metadata distinguishes the active program from inactive
copies and prevents a rolled-back newer program from upgrading the same index
again until the user deliberately brings the library across.

Models and Control Panel settings inside the state folder follow relocation;
explicit external model directories remain unchanged. Relative program paths
are saved as absolute paths so rollback works from another working directory.
Ownership lookup also follows forwarding notes after recovery cleanup.

Upgrade and rollback hold both source and destination startup gates through
the handover, checking that no server still owns the data. They do not hold
an open lock file inside a directory being renamed, preserving Windows rename
compatibility. Regression tests assert a competing start cannot take either
gate during the move.

Tests cover relocation before and after upgrade, preservation of newer work
under `backups/before-go-back`, old-index restoration, reactivation, unavailable
drives, and ownership after cleanup. Linux, macOS and Windows on-disk layouts
are exercised; the latter two are layout simulations on Linux, not native GUI
or operating-system certification.

## Guided recovery cleanup

The Migration page offers **Review recovery-copy cleanup** after a move that
created a verified recovery manifest. The administrator reviews the source,
maximum file count and size, then explicitly confirms. The background worker
checks that the original folder still forwards to the active index and removes
only recorded files whose size and SHA-256 still match. Changed files, added
files, links, and forwarding notes are preserved. Empty directories are removed
only where verified files lived. Cleanup can be repeated after interruption.

Endpoint tests enforce administrator access and confirmation. Filesystem tests
cover invalid paths, changed forwarding, symlinks, repeated cleanup, and active
index preservation. Older moves without manifests retain manual cleanup.

The new `index_relocation_ui.mjs` browser test covers empty/invalid input,
cancellation, duplicate submissions, automatic/manual restart messages,
verified success, failure recovery, and cleanup confirmation/results. Invalid
paths use the real API; restart and post-restart result scenarios use HTTP
fixtures to avoid interrupting the test server. The existing export/import
browser regression also passes in headless Chrome.

## Broader fixes discovered by testing

- HEVC compression failed on this 20-core machine because ffmpeg requested
  19 frame threads, exceeding the encoder's supported range. Frame parallelism
  is bounded and its worker pool follows the Control Panel compute budget.
  All 40 video compression tests passed. See the
  [x265 threading documentation](https://x265.readthedocs.io/en/master/cli.html#cmdoption-frame-threads).
- New browser/API messages have Tamil translations, and the server message
  catalogue has been regenerated. Language and catalogue guards passed.
- The package-restart test now models a missing package explicitly instead of
  failing when every optional package happens to be installed.
- Filesystem mirror tests disable unrelated real-machine workload gating,
  join the scanner, and stop their services during teardown. They no longer
  assume a live CPU-load-dependent background job finishes within 30 seconds.
- The old Linux autostart audit test now writes to a temporary profile. Its
  accidental real-profile entry from the initial run was removed; sign-in
  startup remains off.

## Dependencies

The local setuptools was upgraded to the existing release-build pin, 84.1.0,
and all three package build requirements now require at least that version.
The incompatible torchvision wheel was replaced with its official CPU build
for the installed CPU-only PyTorch; the Tamil VITS model test passed afterward.
The CPU installation example now selects both torch and torchvision from the
same official index.

The advisory gate has no unaccepted findings. The existing, unextended
DiskCache exception remains valid through 31 January 2027. See the separate
[dependency scan report](dependency-audit-2026-11-03.md).

## Verification scope

Ruff, Python compilation, shell syntax, JavaScript syntax, message-catalogue
checks, and dependency compatibility checks passed. The Python suite is
verified across batches and focused reruns, retaining completed results while
fixing failures and updating the requested default location. Platform/model
skips remain as declared by the suite. Native macOS/Windows GUI operation and
manual visual inspection of the Linux desktop are outside this pass.

Final collection accounting covers all **7,494 collected Python cases**, with
no unverified cases remaining after the batch runs and focused fixes. Expected
platform/model skips remain; this count is coverage, not a claim that every
case ran without skipping. The repaired admin template passed all password
prompt and language checks (40 passed, 1 skipped).

Both browser suites passed again against the final template using Playwright
Chromium 156.0.8078.4 (build 1248). System Chrome had crashed before opening
a page, including on a minimal blank-page diagnostic; the matched browser
completed relocation and export/import checks without browser errors.
