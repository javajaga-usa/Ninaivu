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
- A file whose exact content the Secondary already keeps at another path is
  not sent again: the Secondary copies its own, checks the bytes as if they
  had been sent, and gives the copy its own modified time.

The Primary sends what is missing every `mugil_copy_every_hours` (24 by
default), and makes way while someone uses Ninaivu from outside the house,
as the cloud backup does.

### When the Secondary is down

Once the two are paired, the Primary waits for the Secondary instead of
giving up. If it is switched off, restarting, off the network, or its
originals disk is unplugged, sending pauses. The console says since when the
Secondary has not answered. The Primary asks again after 30 seconds, then
less often (at most every 10 minutes), and carries on from the file it had
reached when the Secondary is back. Stop ends the wait. A Secondary that
refuses, because it is no longer paired or its disk is full, still stops the
run and says why.

## The family app while the Primary is off

Tick **Let the family browse on the Secondary while this computer is off** on
the Primary. Every hour, if the library has changed, the Primary then sends
the family app's read-only copy to the Secondary: a slimmed snapshot of the
index, the thumbnails, faces, profile pictures, stories and films. It is the
same hand-over the Pi replica uses (see [the Pi replica](pi-replica.md)),
sent over the pairing instead of a share. Only what changed goes. Each file
is checked, and `replica.json` goes last, so a half-sent copy leaves the one
before whole. On the Secondary it lands in `mugil-handover/` in the state
folder.

To serve it, run the read-only family app on the Linux PC as the Pi does. The
originals there are under `<originals folder>/<library folder name>`, so name
each library folder with `--map`:

```bash
cd /opt/ninaivu
sudo -u ninaivu .venv/bin/python -m ninaivu replica unit \
    --from ~ninaivu/.ninaivu/mugil-handover \
    --map "/Volumes/JApple Extreme SSD/NinaivuMaster/Ninaivu Archive=/srv/n300/originals/Ninaivu Archive" \
    --primary http://<mac>.<tailnet>.ts.net \
    --output /tmp/ninaivu-replica.service
```

Then install and start the unit as the Pi guide says. While the Mac is
awake, pages go to it; while it is off, the Linux PC shows what the Mac last
sent, read-only.

## Optimize Mac storage: the plan

The Primary's page says how much of its library could live on the Secondary
only. An original qualifies only when all of these hold:

- it was not taken in the last year, and nobody made it a favourite;
- the Secondary has confirmed it, with the fingerprint it has now;
- the off-site copy (B2 or another) holds it as it is now;
- it has been on the Secondary for 30 days;
- it has a display copy, or needs none.

This only reads. Nothing is freed on the Primary until the household says so.

## The two computers, side by side

Once they are paired, the Mugil page lists both computers: name, part,
Ninaivu version and system, and free space. For the Secondary it also shows
when it was last seen, the free space on the originals disk, how many
originals it keeps, and when it last kept one. Where the system lets an
ordinary program ask (`smartctl -H` on Linux), it also shows the disk's own
health verdict. When the two run different versions, the page says so and
asks for the older one to be updated.

Between runs the Primary asks after the Secondary every hour. When the
Secondary has not answered for a day, or its originals disk fails its own
health check or has less than 5% free, it sends a notification wherever
Notifications are set to go (a webhook, ntfy, or email).

## Display copies

Once the Secondary has confirmed an original, the Primary makes a display
copy of it in its state folder (`display/`), one file at a time, at a low
priority. The copy is the original made smaller, and nothing else changes:

- A camera JPEG, a PNG or a RAW becomes an AVIF of at most 2560 pixels on its
  long side. The EXIF goes into it byte for byte, with the colour profile and
  the XMP. The orientation tag is kept as it is, and the pixels are not turned.
- A video becomes HEVC in MP4, at most 1080 lines. Its tags (when and where
  it was filmed, the camera) and its rotation go into the copy as they were.
- iPhone HEIC photos and iPhone videos are already small and get no copy.

Each copy has the original's modified time. It is checked before it is kept.
It must open, carry the same metadata, run as long as the original, and be
smaller. If it is not, the original is its own display copy. The copies are
a cache: deleting `display/` costs only the time to make them again. Nothing
in the library is changed, and no space is freed.

## An original that is not on the Primary

When the Primary is asked for an original that is not on its disk (the disk
is unplugged, for example), it sends for it from the Secondary. The file
arrives in `fetched/` in the state folder, and is kept only if its
fingerprint and size match the ones recorded when it was sent. It is given
its modified time and made read-only, and then it is shown, downloaded or
played as usual. The folder keeps at most 20 GB, and the least recently used
files go first. An edited copy cannot be saved beside a photograph whose
folder is not on the Primary; that is refused and explained.

## Setting it up

Install Ninaivu on both computers, then open the console on each one and go
to **Mugil → Two computers**.

1. **On the Secondary**, choose *Secondary: keeps the originals*, then give the
   folder on the big disk and press **Use this folder**. The folder must not be
   inside a library folder or the state folder. The Secondary now waits half an
   hour for its Primary. It also waits each time Ninaivu starts while it is not
   paired, and **Wait for the Primary again** starts a new wait.
2. **On the Primary**, choose *Primary: runs the library*. It looks for the
   Secondary by itself on the house network and on Tailscale, and lists it by
   name with a six-digit number.
3. **Check that the Secondary's screen shows the same number**, then tap
   **Pair** on the Primary. That is all: the two are paired.
4. The Primary sends the originals every day by itself, or when you press
   **Send the originals now**. The screen shows progress, the last run, and how
   many files the Secondary has confirmed.

Each computer's family app must be on the network (Server page → network
access), because that is the port the two use to talk. The console says so
when it is not.

**When the Primary does not find the Secondary.** This happens with a firewall
that blocks UDP port 47391, or when the two computers are on networks that do
not share broadcasts. Open *Pair by address instead* on both computers. Show a
code on the Secondary, then type the Secondary's address (for example
`http://linux-pc`) and the code on the Primary.

The same steps as console calls, for scripts: `POST /api/admin/mugil/role`,
`POST /api/admin/mugil/look`, `POST /api/admin/mugil/pair` with
`{"found": "<id>"}` (or `{"url": …, "code": …}`), `POST /api/admin/mugil/send`,
and `GET /api/admin/mugil` for the state.

## Security

- The two computers agree on a key when they pair. Each sends a one-off public
  key, so someone watching the network never learns the key.
- When they find each other, both screens show a six-digit number worked out
  from the two public keys. A device pretending to be either computer would
  give a different number, which is why you check it before tapping Pair. The
  wait on the Secondary closes after one pairing. With a code instead, each side
  proves it knows the code.
- After pairing, every request between them is signed with the key. A request
  that is unsigned, signed by another key, more than five minutes old or a
  replay is refused.
- The key is kept in `mugil-pair.json` in the state folder, readable only by
  the user Ninaivu runs as. The console never shows it.
- Unpair with `POST /api/admin/mugil/unpair`, on both computers. A computer's
  part can be changed only when it is not paired.
