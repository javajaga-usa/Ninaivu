# Changelog

## 0.1.0 — unreleased

The first cut of Ninaivu as a product, carried forward from Hearth 2.0.0 (with
the fixes of the 28 September 2026 code review) under a new name.

- The package, the settings, the environment variables (`NINAIVU_*`), the state
  directory (`~/.ninaivu`) and the mDNS name (`ninaivu.local`) all carry the new
  name. There is no upgrade path from a Hearth state directory in this release.
- The Cloud tab is **Mugil** and the photo editor is **Sudar**.
- The Windows, macOS and shell launchers are gone; `start.py`, Docker and the
  service examples under `installers/` are the ways to run it until the
  installers in the roadmap exist.
- The household-specific documents, audits and the illustrated PDF guide were
  not carried over.
- Python 3.12 is the floor.
