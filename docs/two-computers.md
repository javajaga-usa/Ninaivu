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
- The folder carries an id (`.ninaivu-originals-id`). When the disk is
  unplugged and only an empty folder is left at its place, the Secondary says
  its originals are away and the Primary waits, rather than sending
  everything again onto the computer's own disk.
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

## Phone uploads while the Primary sleeps

A sync app on a phone (PhotoSync and its kind) sends photographs to the phone
inbox, WebDAV at `/dav/` with a phone key. With two computers the Secondary
answers the same inbox, so phones can send while the Mac is asleep:

- Once paired, the Primary's phone-key page gives the Secondary's address. It
  is the always-on computer's family app, for example
  `http://linux-pc/dav/`. Keys made before pairing work there too, so there
  is nothing to make again. Change the address in the sync app.
- On every run, the Primary hands the Secondary its live phone keys. Only
  their hashes are sent, never a key. A key revoked on the Mac, or a person
  who can no longer back up, stops working on the Secondary at the next run.
- The Secondary keeps each file whole and fingerprinted in
  `.ninaivu-phone-inbox` on the originals disk, and tells the phone it is
  safely there. As on the Mac, a key can only add. It never opens, lists or
  deletes a photograph.
- When the Mac is back, the next run collects every file and checks it
  against its fingerprint. It is filed as the phone backup it is: to the
  review queue, or into the library when phone backups are trusted. Only then
  does the Secondary drop its copy. It keeps the record, so the phone still
  sees the file as home. The original returns to the Secondary with the next
  copy of the originals.

## Optimize Mac storage: the plan

The Primary's page says how much of its library could live on the Secondary
only. An original qualifies only when all of these hold:

- it was not taken in the last year, and nobody made it a favourite;
- the Secondary has confirmed it, with the fingerprint it has now;
- the off-site copy (B2 or another) holds it as it is now;
- it has been on the Secondary for 30 days;
- it has a display copy (an original that is its own display copy, such as
  an iPhone's HEIC, stays).

The plan only reads.

## Freeing space

Freeing is off. To use it, tick **Allow freeing space** on the Primary's
Mugil page (`mugil_free_space`), then press **Free space** and confirm. Even
then, every original the plan calls ready is checked again, one at a time,
before it goes:

- the file here is read end to end and still has the fingerprint, size and
  time the Secondary confirmed;
- the Secondary reads its own copy end to end and says it is the same;
- the off-site copy holds it at that size and time;
- its display copy is there;
- nothing changed here while the Secondary was checking.

If any check fails, the original stays. If the Secondary does not answer,
nothing more is let go. Each original that goes is written down in the index
(`mugil_let_go`) before it is removed. Its thumbnail, display copy, names,
albums and stories stay. A rescan does not count it as missing. Neither do
the integrity check, the repair or the second copy. When somebody opens the
original, it is sent for from the Secondary.

**Bring every original back** copies each let-go original back from the
Secondary to the same place, with its own modified time, and checks each
against its fingerprint. Turning the switch off stops a run, but nothing
already let go comes back by itself.

## The Mugil checklist

Below the two computers, the Mugil page lists what the setup needs, ticked
from what is true now rather than from what was done once. Each item is
done, still to do, off (not needed with the household's choices) or a
problem (it worked and has stopped: the Secondary silent for a day, a disk
reporting trouble, less than 5% free). The Overview's *Is everything safe?*
counts it as one more check.

## Waking the Mac at night

A Mac that sleeps does nothing at night unless something wakes it. With the
daily catch-up on, the Primary tells the Secondary, on every run, the
hardware addresses of its network connections and the time to wake it: five
minutes before the catch-up's stretch. The Secondary sends a Wake-on-LAN
packet then, once a night, to every network it is on. A wake missed by more
than half an hour (the Secondary was off) waits for the next night. Turning
the catch-up off stops it; unpairing forgets it on both.

The Mac answers only with **Wake for network access** on (System Settings,
Energy, or `sudo pmset -a womp 1`); the checklist says when it is off. Over
Wi-Fi a Mac may not wake: a cable is surer. **Wake the Primary now** on the
Secondary's Mugil page sends one at once.

## A new Mac, or a library disk that failed

The Secondary keeps every original the Primary sent, so the library can be
copied back from it. Pair the new Mac as the Primary, then on its Mugil page
use **Rebuild a library from the Secondary**:

1. Choose a library from the list. It shows what the Secondary keeps of each
   and, on the Mac that sent them, the folder it was at.
2. Give a folder: an empty one, or the library's own folder (a rebuild that
   was stopped carries on there).
3. Press **Rebuild** and confirm.

Each original arrives as it is kept. It is checked against the size and
fingerprint the Secondary recorded, gets its own modified time back, and is
written under a temporary name and then renamed. Nothing else about the file
changes, and nothing is removed or replaced on either computer: a file already
in the folder stays as it is. The folder is given the library's own id
(`.ninaivu-library`), so the Secondary knows it as the same library and the
next run sends nothing again. When every original is back, the folder is
added as a library and indexed. While the Secondary is away, the rebuild waits
for it and carries on.

To keep names, albums and stories as well, bring the old Mac's state folder
over first (`ninaivu --bring`, or a copy of the index), and rebuild into the
folder the library was at. The index then finds every photo where it was.

**Replacing the Secondary.** Unpair, set up the new one as the Secondary and
pair again; the Primary sends every original to it. Unpairing is refused while
originals freed from the Mac are on the Secondary only: press **Bring every
original back** first.

**Swapping the parts.** Unpair both, choose the new parts, and pair again.
The new Secondary is sent every original by the new Primary.

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

Install Ninaivu on both computers, and on each run `ninaivu mugil setup` once
(`./ninaivu mugil setup` in a portable folder; `--dry-run` says what it would
do). It makes Ninaivu start with the computer the way that system does it: a
LaunchAgent on a Mac, the sign-in start on Windows, a systemd user service on
Linux (restarted if it stops; `sudo loginctl enable-linger <you>` once for it
to start at boot before anybody signs in). The copies, checks, off-site copy
and the nightly wake run inside Ninaivu, so a running Ninaivu is all they need.
`ninaivu mugil status` prints the Mugil checklist.

Then open the console on each one and go to **Mugil → Two computers**.

1. **On the Secondary**, choose *Secondary: keeps the originals*, then give the
   folder on the big disk and press **Use this folder**. The folder must not be
   inside a library folder or the state folder. The Secondary now waits half an
   hour for its Primary. While it is not paired and keeps no originals yet, it
   also waits each time Ninaivu starts. **Wait for the Primary again** starts a
   new wait; one that already keeps originals (its Primary was replaced) waits
   only then, because whoever pairs with it can read them.
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
