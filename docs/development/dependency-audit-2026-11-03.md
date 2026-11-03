# Dependency advisory scan — 3 November 2026

Scanned this project's installed Python environment with pip-audit 2.10.1.
This covers installed packages, including optional packages present locally;
it does not establish coverage for every optional dependency or platform.

The initial scan found setuptools 78.1.0 in the local virtual environment.
It was upgraded to **84.1.0**, matching the existing release-build pin in
`requirements/build-tools.txt`. The minimum build dependency in the main
package and both extensions is now also 84.1.0. This incorporates the fixes
for the package-index path traversal, manifest Unicode exclusion, and legacy
Windows wheel path handling advisories reported by the scan.

Primary references:

- [Package-index path traversal](https://github.com/pypa/setuptools/issues/4946)
- [Manifest Unicode exclusion](https://github.com/pypa/setuptools/security/advisories/GHSA-h35f-9h28-mq5c)
- [Legacy wheel path handling](https://github.com/pypa/setuptools/security/advisories/GHSA-xmj3-9gf7-h52w)

The final scan has **no unaccepted advisories** under the project's blocking
audit gate. DiskCache 5.6.3 still has the unsafe-pickle advisory
[GHSA-w8v5-vhqr-4h9v / CVE-2025-69872](https://github.com/advisories/GHSA-w8v5-vhqr-4h9v).
No patched version was reported. The existing exception in
`requirements/audit-exceptions.toml` applies: Ninaivu does not enable the
llama-cpp prompt disk cache. It is owned by `@javajaga-usa` and expires on
31 January 2027. This pass did not add or extend any exception.

To reproduce with the development dependencies installed:

```sh
python -m pip_audit --format json --output /tmp/ninaivu-audit.json
python tools/audit_gate.py /tmp/ninaivu-audit.json requirements/audit-exceptions.toml --blocking
python -m pip check
```

The raw scanner exits nonzero while an accepted advisory is present; the gate
applies the repository's documented exceptions and expiry dates. Dependency
compatibility checks also passed after the upgrade.


A separate binary compatibility check found torchvision 0.29.2 could not
register `torchvision::nms` with the installed CPU-only torch 2.15.0. The local
wheel was replaced with **torchvision 0.29.2+cpu** from the official
[PyTorch CPU wheel index](https://download.pytorch.org/whl/cpu). This preserves
the package's base version and fixes the VITS speech-model import. Dependency
metadata alone (`pip check`) did not detect the incompatible binary variant.

PyPI could not directly audit the local version labels `torch==2.15.0+cpu`
and `torchvision==0.29.2+cpu`. Their upstream base releases (`2.15.0` and
`0.29.2`) were separately queried with pinned, dependency-free pip-audit input;
no known advisories were reported for those releases. This supplements the
installed-environment scan, rather than claiming to audit the exact CPU-wheel
binary contents.
