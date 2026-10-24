# Two computers: the Primary and the Secondary

One computer runs everything, as Ninaivu always has. Nothing on this page
applies to it: its part is **single**, and none of this runs.

With two computers each one gets a part:

- **The Primary** (the fast one, for example a Mac mini) keeps the index and
  thumbnails and serves the library the family browses. It also runs the
  models and does the editing.
- **The Secondary** (the one that is always on with the big disk, for example
  a Linux PC with a 12 TB drive) keeps **every original exactly as it was
  sent**. No original there is changed, overwritten or removed.

Both run the same Ninaivu, on any operating system it runs on, and they talk
to each other over the network: Tailscale, or the house network. No shared
folder, SSH or rsync is needed, so a Windows computer can take either part.

## What is sent, and how it is checked

- Every original in the Primary's library folders that are plugged in. Hidden
  and explicit files are included, because they are the household's own.
- A file is sent as the bytes on the disk. EXIF, dates and every other
  property in it stay as they are, and the Secondary gives it the Primary's
  modified time.
- The Primary names each file's fingerprint (BLAKE3). The Secondary works the
  fingerprint out again from the bytes that arrived, and keeps the file only
  if the two match, and the size too. A file that changed while it was being
  sent is not kept; it goes again on the next run.
- A file that changed on the Primary later is kept **beside** the older one.
  The older one moves to `.ninaivu-set-aside/<date>/` in the originals folder.
  A file deleted on the Primary stays on the Secondary.
- The originals folder is plain: one folder per library, with the same folders
  and names as on the Primary. Anything can read it.
- Nothing is taken off the Primary in this stage.

The Primary sends what is missing every `mugil_copy_every_hours` (24 by
default), and makes way while someone uses Ninaivu from outside the house,
as the cloud backup does.

## Setting it up

There is no console screen for this yet. Until there is, use these console
calls as an administrator, or the settings named below.

1. **On the Secondary**, make it the Secondary and choose the folder on the
   big disk. The folder must not be inside a library folder or the state
   folder.
   `POST /api/admin/mugil/role` with `{"role": "secondary", "originals_dir": "/srv/n300/originals"}`
2. **On the Secondary**, ask for a pairing code. It is good for ten minutes
   and can be used once.
   `POST /api/admin/mugil/code` answers `{"code": "K7QM-3XRP", ...}`
3. **On the Primary**, make it the Primary, then pair with the Secondary's
   console address and the code:
   `POST /api/admin/mugil/role` with `{"role": "primary"}`, then
   `POST /api/admin/mugil/pair` with `{"url": "http://linux-pc:3000", "code": "K7QM-3XRP"}`
4. **On the Primary**, send now, or wait for the daily run:
   `POST /api/admin/mugil/send`. `GET /api/admin/mugil` shows progress, the
   last run, and how many files the Secondary has confirmed for each library.

The Secondary's console must be reachable from the Primary. Turn on "Console
on the network" on the Server page, or reach it over Tailscale.

## Security

- The two computers agree on a key when they pair. Each sends a one-off public
  key with proof that it knows the code, so someone watching the network
  never learns the key.
- After pairing, every request between them is signed with the key. A request
  that is unsigned, signed by another key, more than five minutes old or a
  replay is refused.
- The key is kept in `mugil-pair.json` in the state folder, readable only by
  the user Ninaivu runs as. The console never shows it.
- Unpair with `POST /api/admin/mugil/unpair`, on both computers. A computer's
  part can be changed only when it is not paired.
