# Documentation

The docs site — Install, The first day, Family and roles, Backup, Remote
access, AI, Troubleshooting — is built from [`index.md`](index.md) and [`site/`](site/install.md) with
`mkdocs serve` (`pip install mkdocs-material`) and published at
<https://javajaga-usa.github.io/Ninaivu/>. Everything below is on it too,
under *More*.

- [A tour of the screens](screens.md) — every page, pictured and explained.
- [CHANGELOG](CHANGELOG.md) · [ROADMAP](ROADMAP.md) · [CONTRIBUTING](CONTRIBUTING.md) · [SECURITY](SECURITY.md) · [Third-party notices](THIRD_PARTY_NOTICES.md)

- [Operations and production](operations/production.md) — Docker, systemd, reverse proxies, large libraries, backups.
- [Backup and recovery](backup-recovery.md) — Mugil: what is backed up, how to restore.
- [Encryption and the recovery file](encryption-files.md)
- [Moving to another machine](moving-to-another-machine.md)
- [The tray, the Control Panel and HTTPS](desktop-control.md)
- [Local AI tools](local-ai-tools.md), [local AI editing](local-ai-editing.md), [the AI server](ai-server.md), [creative studio](creative-studio.md) — optional; see `extensions/README.md`.
- [Date access](date-access.md) — limiting what each role sees by date.
- [Development](development/) — repository structure, background work, review notes.
- [`Ninaivu-guide.pdf`](Ninaivu-guide.pdf) — the whole guide as one colour PDF to hand to the household (`python tools/build_guide_pdf.py` rebuilds it from the site's pages).
- [`Ninaivu-guide-ta.pdf`](Ninaivu-guide-ta.pdf) — the same guide in Tamil, from [`site/ta/`](site/ta/index.md); commands, keys and setting names stay in English (`python tools/build_guide_pdf.py --lang ta`).
- The guide: [the family app](site/guide-family.md) and [the console](site/guide-console.md), rebuilt for 0.1.0 from the current screens (they replace the Hearth-era `user-guide.html` and `handbook.html`).
