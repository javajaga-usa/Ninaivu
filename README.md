# Ninaivu

**நினைவு** · *memory*

Your family's photographs, at home.

Ninaivu is a private, self-hosted photo and video library for a household. It runs on a computer you own, indexes what is on your own disks, and gives your family the things people usually leave for a cloud service to get: faces and names, search by description, places on a map, text in pictures, "on this day", a link to share with grandparents, and phones that send their photographs home. Nothing leaves your house unless you choose where it goes, and the backup goes encrypted.

It has three parts, and each has a name:

| Name | What it is |
| --- | --- |
| **Ninaivu** | The library: the gallery, faces, search, places, albums, sharing, and the three roles |
| **Mugil** (முகில், *cloud*) | Backup: an encrypted copy of the library on Google Drive, and your phone's photographs sent to the house |
| **Sudar** (சுடர், *glow*) | The photo studio: adjustments, straightening, crops and suggestions, done in the browser without changing the original |

> **Status: 1.0.4.** Ninaivu is the general edition of a server that has run one household's library for a year. It has over 4,000 tests, and the installers for Windows, macOS, Linux and Raspberry Pi each carry their own Python. The installers are not yet signed; see [ROADMAP.md](docs/ROADMAP.md) for what comes next.

[Download installers](https://github.com/javajaga-usa/Ninaivu/releases) · [Read the guide](https://javajaga-usa.github.io/Ninaivu/) · [What's changed](docs/CHANGELOG.md) · [Report a problem](https://github.com/javajaga-usa/Ninaivu/issues)

## A look

![The gallery](docs/screens/family-gallery.jpg)

![The console's overview](docs/screens/console-overview.jpg)

Every screen, with what it is for: [a tour of the screens](docs/screens.md).
(The pictures use a generated sample library, not anybody's photographs.)

**The documentation** — Install, The first day, Family and roles, Backup,
Remote access, AI, Troubleshooting — is at
<https://javajaga-usa.github.io/Ninaivu/> (built from `docs/site/`).

## Three roles

One **administrator** runs it. **Family members** see everything marked for the family. **Guests** see only what was chosen for them. Every photograph has a visibility, every folder can be assigned to a person, and every link shares exactly what its maker could already see. That model is the product; everything else is built on it.

## Try it

Installers for Windows, macOS, Linux and Raspberry Pi are on the
[releases page](https://github.com/javajaga-usa/Ninaivu/releases); each carries
its own Python, and the Windows and Mac ones put Ninaivu in the system tray or
menu bar. From a checkout:

```bash
git clone https://github.com/javajaga-usa/Ninaivu.git
cd Ninaivu
```

On Windows, double-click `start.cmd`, or run this in Command Prompt:

```bat
start.cmd "%USERPROFILE%\Pictures"
```

On macOS or Linux:

```bash
sh launcher/start.sh ~/Pictures
```

The launcher makes a virtual environment, installs what is missing, finds free
ports and opens your browser. The first screen makes the administrator; after
that, add the household under **People**.

On a Mac, double-clicking **Setup Ninaivu.command** once does all of that
without a terminal (installing Python 3.12 first if the Mac has none) and
puts **Ninaivu** and the **Ninaivu Control Panel** in Applications. On
Windows, **Start - Ninaivu Control Panel.vbs** opens the Control Panel, a
window that starts and stops Ninaivu and shows its readings and log, beside
the tray. [More on both.](docs/desktop-control.md)

Docker:

```bash
cp installers/docker/.env.example installers/docker/.env   # set MEDIA_DIR to your photo folder
docker compose -f installers/docker/docker-compose.yml up -d
```

Python 3.12 or newer. Everything AI-related is optional: the gallery, roles, albums and sharing work with the core dependencies alone, and the models download only when you turn a feature on — search by description, the largest at about 2 GB, is added from the first-day walk-through if you want it. Ninaivu is tested on two kinds of computer — a small box with no graphics processor, and a machine with a GPU, Apple silicon or plenty of memory — and works out at start which yours is.

## What it does

- **Library** — photographs, videos and RAW files from any number of folders, watched for changes, laid out by date and by folder. Duplicates found by content.
- **Import** — sweep old drives, cards and backup folders into one archive laid out as `YYYY/MM/DD`, hash-verified on the way in and checked for bit rot afterwards. Sources are only ever read. Import Google Photos Takeout, iCloud and WhatsApp exports, preserving the metadata available in each export and skipping files already in the library.
- **People** — faces found and grouped; name one and the rest follow. Faces are found and matched on this computer; the names go nowhere except inside Mugil's encrypted copy of the index.
- **Search** — by description ("the beach at sunset"), by text in the picture, by place, by date, by person, by camera.
- **Places** — every located photograph on a map, grouped; trips by year; "photos taken nearby".
- **Albums and sharing** — ordinary albums and smart albums that fill from saved searches; a link for one photograph or an album, with an optional password and expiry, scoped to what its maker may see.
- **Mugil** — encrypted backup to Google Drive, test-restored automatically; phone backup over the home network; state backups with verified bundles.
- **More copies and recovery** — a scheduled, verified second copy on another disk or NAS; an encrypted off-site copy to an S3-compatible bucket or folder; repair of damaged files from a verified backup.
- **Sudar** — exposure, colour, sharpness, noise, straightening and crops in the browser; "make it warmer" in plain words; the original is never changed.
- **Health** — disk health, bit-rot scrubbing, and notifications by email or webhook when something needs a person.
- **Languages** — English and Tamil today; the strings are in `ninaivu/static/i18n/` and adding a language is one file.

The core AI features run on your computer. Optional [extensions](extensions/README.md) add heavier studio tools or Gemini editing; Gemini sends a copy of the selected photograph and your instruction to Google when you use it. Extensions are off by default and enabled by the administrator.

## Where things are

The root holds the double-click starters (`start.cmd` for Windows, `Ninaivu.command` and
`Setup Ninaivu.command` for macOS, `Start - Ninaivu Control Panel.vbs` and
`ninaivu_control.pyw` for the Windows control panel) and the files Git, GitHub, pip and
mkdocs look for by name (`README.md`, `LICENSE`, `pyproject.toml`, `mkdocs.yml`, the
dotfiles and `.github/`).
Everything else is in a folder for what it is:

```
launcher/       start.py and start.sh — set up a virtual environment and run the server
ninaivu/        the server package: api/ storage/ media/ cloud/ archive/ server/ utils/
                templates/ static/ (the browser app) and static/i18n/ (languages)
requirements/   what pip installs: the core, the developer tools, and each optional AI extra
tests/          the Python, JavaScript and browser suites
installers/     the Windows and macOS installers, Docker, systemd, Caddy and nginx examples
extensions/     optional pieces that are not part of the core (see extensions/README.md)
tools/          setup, diagnostics and maintenance commands
docs/           operator and user documentation, CHANGELOG, ROADMAP, CONTRIBUTING, SECURITY
```

## Contributing

See [CONTRIBUTING.md](docs/CONTRIBUTING.md). The short version: Python 3.12+, `ruff check`, `pytest`, one focused change per pull request, and a regression test for every fix that changes behaviour.

## Licence

MIT. © 2026 Jagadeesh Rajendran. See [LICENSE](LICENSE).

Ninaivu began as [Hearth](https://github.com/javajaga-usa/Hearth), one household's server, and carries that code forward under a new name.
