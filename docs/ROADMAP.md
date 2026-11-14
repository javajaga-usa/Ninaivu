# Roadmap

Ninaivu is built for one household's own library. This page says where it
stands, what is still open, and what has been decided against, so that
nobody spends time on the last list. The history of what was built, release
by release, is the [changelog](CHANGELOG.md).

## Where it stands

- One server with two faces: the family app and the administrator's console.
- Portable downloads for Windows (x64), Apple-silicon Macs and Linux (x64),
  each carrying its own Python, and a Docker image. One Python, 3.14.
- Two computers ([Primary and Secondary](two-computers.md)): the Secondary
  keeps a checked copy of every original and can serve the family when the
  Primary is asleep.
- Backups beside that: a second copy on another disk, an encrypted off-site
  copy to an S3-compatible bucket, and Mugil on Google Drive.
- On-device AI only, each model fetched when it is asked for on the AI
  models page.

## Open

- Signed downloads. The workflow signs when the certificates and secrets are
  set ([signing the downloads](development/signing-releases.md)); they have
  not been.
- WebDAV as a place for the off-site copy, and Mugil on more than Google
  Drive.
- Importing an Amazon Photos export.
- Inside the code: the largest modules (`storage/db.py`, `server/config.py`,
  `static/js/app.js`, `static/js/admin.js`) are still being split by subject,
  and background work started as a bare thread is still being moved under
  the supervisor ([background work](development/background-work.md)).

## Not planned

Each of these was built or considered and then decided against.

- Making videos or photographs smaller: Compress & Replace, the smaller-videos
  trial and the display copies of the two-computer set-up were removed in
  the release after 4.2. Ninaivu keeps originals as they are.
- A Raspberry Pi as the always-on copy, and the stand-alone read-only
  replica it ran. The Secondary is the always-on computer.
- Debian 12 and Python older than 3.14.
- Writing down what is said in videos.
- A front-end rewrite in a framework, and a rewrite of the server.
- Accounts beyond the household, hosted AI models, a paid layer or a hosted
  relay.
- Checking the internet for a newer version.
