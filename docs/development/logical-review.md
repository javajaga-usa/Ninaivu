# Application logic review

This review combines the Python regression suite with focused code review of
gallery query bounds, edited-copy publication, certificate selection, desktop
log handling, and browser editing state. It does not claim that every possible
device, model or network configuration has been manually exercised.

## Corrections

| Area | Problem | Result |
| --- | --- | --- |
| Gallery and occasions | Negative limits became SQLite's unbounded `LIMIT -1`. | Requests use positive, capped limits. |
| Library copies | Saving a PNG converted every image to RGB. | Alpha transparency is retained while source files and visibility rules remain unchanged. |
| Certificate setup | A previous local CA could still be offered while running with a custom certificate. | The server and control panel distinguish custom certificates from the Ninaivu CA. |
| Desktop logs | Reading the pathname's metadata could race log rotation; long lines without newlines disappeared. | Read the opened file's metadata and retain bounded long-line content. |
| Selective color | Changing or reordering the active photo dropped its brush mask. | Masks belong to individual photos and survive selection changes. |
| Restoration stories | The edited half referred to whichever photo happened to be first. | The original/edited pair is retained independently of album order and removal. |
| Then-and-now | The second photo was implicitly chosen; a single photo could be exported as a pair. | Explicit comparison-photo selection; a pair is required to export. |
| Exports | The comparison slider reset to 50%; an asynchronous PNG download could get the next mode's filename. | Exports retain the chosen comparison and creation type. |
| Audio | Preview used a data URL blocked by Ninaivu's media policy; delayed reads could restore removed audio. | Local blob playback works under the existing policy, and stale loads are ignored. |
| Print | Interactive scripts ran in the inherited app policy; slides were hidden in print. | Print output is static, excludes audio, and includes every slide. |
| AI results | Closing or saving a normal adjustment copy could discard an undownloaded generated result. | Close confirms unsaved results; saving adjustments keeps separate results available. |
| Async editing | Late blur or object-removal results could touch a closed or discarded workspace. | Ignore stale completions, release image resources, and scope keyboard undo to the active editor. |
| Initialization and versions | Failed initialization leaked images; rapid captures could exceed the version limit or capture changed labels. | Cleanup on failure and serialized captures with fixed labels/look settings. |

## Verification

Recorded on 2026-09-11: the complete suite finished with **1,836 passed and
29 skipped**. Additional focused runs after the relevant changes passed:
API/photo/log checks (47 tests), HTTPS/log checks (39 passed, 1 skipped), and
desktop control/log checks (19 tests). Both desktop/mobile browser suites
passed. Ruff passed, 29 JavaScript modules passed syntax checks, and the built
wheel passed verification of 47 required application files.

- `python -m pytest -q`: complete Python suite, including roles, library scope,
  uploads, deletion/recovery, archive/cloud operations, database/scanning,
  launchers, HTTPS, shutdown and desktop controls. Platform-specific tests can
  be skipped on Windows; optional service coverage depends on installed tools.
- Focused API, PNG-alpha, certificate and log tests exercise the changes above.
- `node tests/ai_playground_ui.mjs`: desktop/mobile editing, undo/redo, downloads,
  saving copies, unsaved-result protection and late object-removal completion.
- `node tests/creative_studio_ui.mjs`: all ten export modes, real media CSP,
  audio load/removal, per-photo masks, stable restoration pairs and HTML export
  behavior. Test images and audio are synthetic.
- Repository-wide Ruff and JavaScript syntax checks.
- Build the wheel and run `tools/check_distribution.py` to verify web assets.

## Product boundaries retained

Creative Studio is an in-memory workspace; downloads preserve finished output,
not an editable project. Local AI quality and speed depend on the selected
model and hardware. Certificate trust and firewall changes remain explicit
device setup actions. None of these fixes changes those product boundaries.
