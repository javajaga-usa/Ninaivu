# Ninaivu

**நினைவு** · *memory*

Your family's photographs, at home.

Ninaivu is a private, self-hosted photo and video library for a household. It runs on a computer you own, indexes what is on your own disks, and gives your family the things people usually leave for a cloud service to get: faces and names, search by description, places on a map, text in pictures, "on this day", a link to share with grandparents, and a phone that backs itself up at home. Nothing leaves your house unless you choose where it goes, and then it goes encrypted.

It has three parts, and each has a name:

| Name | What it is |
| --- | --- |
| **Ninaivu** | The library: the gallery, faces, search, places, albums, sharing, and the three roles |
| **Mugil** (முகில், *cloud*) | Backup: an encrypted copy of the library on Google Drive or another disk, and your phone's photographs backed up to the house |
| **Sudar** (சுடர், *glow*) | The photo studio: adjustments, straightening, crops and suggestions, done in the browser without changing the original |

> **Status: 0.1.0, pre-release.** Ninaivu is the general edition of a server that has run one household's library for a year. It works, it has 3,500 tests, and it is not yet packaged for somebody who has never seen a terminal. See [ROADMAP.md](ROADMAP.md) for what 1.0 needs.

## Three roles

One **administrator** runs it. **Family members** see everything marked for the family. **Guests** see only what was chosen for them. Every photograph has a visibility, every folder can be assigned to a person, and every link shares exactly what its maker could already see. That model is the product; everything else is built on it.

## Try it

```bash
git clone https://github.com/javajaga-usa/Ninaivu.git
cd Ninaivu
python start.py ~/Pictures
```

`start.py` makes a virtual environment, installs what is missing, finds free ports and opens your browser. The first screen makes the administrator; after that, add the household under **People**.

Docker:

```bash
cp .env.example .env            # set MEDIA_DIR to your photo folder
docker compose -f installers/docker/docker-compose.yml up -d
```

Python 3.12 or newer. Everything AI-related is optional: the gallery, roles, albums and sharing work with the core dependencies alone, and the models download only when you turn a feature on.

## What it does

- **Library** — photographs, videos and RAW files from any number of folders, watched for changes, laid out by date and by folder. Duplicates found by content.
- **Import** — sweep old drives, cards and backup folders into one archive laid out as `YYYY/MM/DD`, hash-verified on the way in and checked for bit rot afterwards. Sources are only ever read.
- **People** — faces found and grouped; name one and the rest follow. Names are the household's and never leave the machine.
- **Search** — by description ("the beach at sunset"), by text in the picture, by place, by date, by person, by camera.
- **Places** — every located photograph on a map, grouped; trips by year; "photos taken nearby".
- **Sharing** — a link for one photograph or an album, with an optional password, scoped to what its maker may see.
- **Mugil** — encrypted backup to Google Drive, test-restored automatically; phone backup over the home network; state backups with verified bundles.
- **Sudar** — exposure, colour, sharpness, noise, straightening and crops in the browser; "make it warmer" in plain words; the original is never changed.
- **Health** — disk health, bit-rot scrubbing, and notifications by email or webhook when something needs a person.
- **Languages** — English and Tamil today; the strings are in `ninaivu/static/i18n/` and adding a language is one file.

## Where things are

```
ninaivu/        the server package: api/ storage/ media/ cloud/ archive/ server/ utils/
                templates/ static/ (the browser app) and static/i18n/ (languages)
tests/          the Python, JavaScript and browser suites
installers/     Docker, systemd, Caddy and nginx examples, a Windows service installer
extensions/     optional pieces that are not part of the core (see extensions/README.md)
tools/          setup, diagnostics and maintenance commands
docs/           operator and user documentation
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The short version: Python 3.12+, `ruff check`, `pytest`, one focused change per pull request, and a regression test for every fix that changes behaviour.

## Licence

MIT. See [LICENSE](LICENSE).

Ninaivu began as [Hearth](https://github.com/javajaga-usa/Hearth), one household's server, and carries that code forward under a new name.
