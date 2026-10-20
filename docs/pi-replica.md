# The Pi as the always-on copy

The Mac mini runs Ninaivu and is not always on. The Raspberry Pi is, and it
already holds every photograph in its mirror of the library. With this set
up, the family keeps one bookmark, the Pi's: while the mini is awake each page
they open goes on to the mini, and while it sleeps the Pi shows its own copy
of the library, with a line at the top saying so.

On the Pi the family can look at everything they may see on the mini: the
gallery, albums, people, places, dates, voice stories, and the photographs and
videos themselves. Changes wait for the mini: a caption, an album, a deletion
or an upload is refused with a sentence saying why, and can wake the mini up.
Searching by description needs the image model, which does not run on the Pi,
so the Pi searches by words, dates, people and places only.

Nothing on the Pi ever writes to the library. Its service mounts the mirror
read-only, so even a mistake in Ninaivu could not change a photograph, and the
app refuses every change except signing in and out.

## How it works

1. **The Mac hands the library over.** Every 30 minutes, when anything has
   changed, Ninaivu on the Mac writes a hand-over into a folder on the Pi's
   share: a snapshot of the index (who is in each photograph, albums, dates,
   accounts), the thumbnails, face pictures, profile pictures, stories and finished films.
   Only what changed is copied. Sign-in sessions, share links, the sign-in
   record and the image-search vectors stay on the Mac.
2. **The Pi serves it.** `ninaivu --replica-of /srv/ninaivu/replica` serves
   the family app from the newest hand-over that checks out (its size and
   checksum are in `replica.json`) and takes up a newer one within a minute,
   without signing anybody out. The photographs come from the mirror the Pi
   already keeps (`/srv/ninaivu/current`).
3. **The Pi sends people to the mini when it can.** It asks the mini every 20
   seconds whether it is awake. Pages opened on the Pi go to the mini while it
   is. A browser can stay on the Pi with `?pi=1` (and go back with `?pi=0`).

## Before you start

* The Mac and the Pi run the **same version** of Ninaivu. A Pi older than the
  Mac refuses a hand-over whose index it cannot read, and says so.
* The Pi's mirror of the library is up to date. A photograph the Mac has
  indexed but the copy job has not yet carried shows its thumbnail on the Pi
  but not its original.
* A 64-bit Raspberry Pi OS or Ubuntu on the Pi, with Python 3.11 or newer.

## On the Mac

1. Connect the Pi's `Ninaivu` share in Finder (Go → Connect to Server,
   `smb://<pi>/Ninaivu`) and make a folder `replica` in it. It must be
   outside the library and outside Ninaivu's own folder, and **not** inside
   `current`, so the nightly snapshots and the Backblaze copy stay the
   library alone.
2. In the console, **Advanced → Backup**, set `replica_dir` to that folder
   (for example `/Volumes/Ninaivu/replica`) and keep `replica_every_minutes`
   at 30. The first hand-over starts within five minutes; later ones follow
   whenever the library has changed. To write one now, from the Ninaivu
   folder:

   ```bash
   python -m ninaivu replica publish --to /Volumes/Ninaivu/replica
   ```

   The console also has it as `POST /api/replica/handover`, and its state as
   `GET /api/replica/handover`.
3. For waking: **System Settings → Energy → Wake for network access** on, and
   note the mini's Ethernet address (`networksetup -getmacaddress en0`).
   Wake-on-LAN works reliably over Ethernet, not Wi-Fi.

The first hand-over copies every thumbnail (about 8 GB for 208,000 items) and
takes a while over the network; after that only changes go.

## On the Pi

Install Ninaivu from source, at the version the Mac runs:

```bash
sudo apt install -y git python3-venv
sudo useradd --system --home-dir /opt/ninaivu --shell /usr/sbin/nologin ninaivu
sudo git clone https://github.com/javajaga-usa/Ninaivu /opt/ninaivu
sudo git -C /opt/ninaivu checkout v2.0.0           # the Mac's version
sudo python3 -m venv /opt/ninaivu/.venv
sudo /opt/ninaivu/.venv/bin/pip install -r /opt/ninaivu/requirements/requirements.txt
```

The image-model packages are not needed. The service runs as the `ninaivu`
account, which must be able to read the hand-over and the mirror:

```bash
sudo -u ninaivu ls /srv/ninaivu/replica /srv/ninaivu/current
```

If either is refused, let it read them (`sudo chmod -R o+rX /srv/ninaivu/replica
/srv/ninaivu/current`, or add `ninaivu` to the group that owns them).

Then let Ninaivu write the service for you. It finds each of the Mac's library
folders in the mirror by the id in its `.ninaivu-library` file, and mounts it
read-only at the Mac's own path, so every path in the index fits as it is:

```bash
cd /opt/ninaivu
sudo -u ninaivu .venv/bin/python -m ninaivu replica unit \
    --from /srv/ninaivu/replica --mirror /srv/ninaivu/current \
    --primary http://<mini>.<tailnet>.ts.net \
    --wake 3c:22:fb:12:34:56 \
    --output /tmp/ninaivu-replica.service
```

It prints the few commands to run once as root before the first start (the
empty folder the library is mounted on, and the Pi's own state folder,
`/srv/ninaivu/replica-state`, on the 12 TB drive so the SD card is spared the
index copies). Run them, then:

```bash
sudo cp /tmp/ninaivu-replica.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ninaivu-replica
journalctl -u ninaivu-replica -f
```

If a library folder is not found by its marker (a mirror made before the
marker existed), name it: `--map "/Volumes/JApple Extreme SSD/NinaivuMaster/Ninaivu Archive=/srv/ninaivu/current/ninaivu/Ninaivu Archive"`.

`--primary` is the mini's family app address as the family's phones reach it
(its Tailscale name works from home and away). Leave it out and the Pi always
serves its own copy. Leave out `--wake` and nothing is woken.

## The family's bookmark

Each phone, tablet and computer bookmarks the Pi: `http://ninaivu-pi.local`
at home, or the Pi's Tailscale name from anywhere. They sign in on the mini
and on the Pi separately, once each, with the same names and passwords.

## Checking it

* `python -m ninaivu replica status --from /srv/ninaivu/replica` says when
  the hand-over was made and by which version.
* `GET /api/replica` on the Pi says which copy it serves, how old it is and
  whether the mini answered.
* On the Mac, `GET /api/replica/handover` on the console says when the last
  hand-over went and any error.

## Turning it off

On the Pi, `sudo systemctl disable --now ninaivu-replica`. On the Mac, empty
`replica_dir`. The hand-over folder can then be deleted; it holds nothing the
Mac does not have.

## What it keeps from the Mac, and what it does not

The hand-over carries the accounts with their scrambled passwords and PINs, so
the family signs in on the Pi as on the mini, and who is in every photograph.
Keep the share it is written to private to the household, as the library on
the same disk already is. It never carries sign-in sessions, share links,
"who is this?" links, the handover code, the sign-in record, the mail or
webhook settings, or any key.

It does carry time capsule letters and the words of voice stories, so the Pi
can show them. Only the Mac should be able to write to the hand-over folder:
its checksums are kept in the same folder, so whoever can write there decides
what the Pi serves. Give the Samba user the Mac signs in with write access to
that folder, and every other account read access at most.

## Coming later

This is the first stage of proposal F. Later stages stream the index to the
Pi continuously instead of in snapshots, let the Pi search by description
once the smaller on-device model arrives, and let phone backups land on the
Pi for the mini to take in when it wakes.
