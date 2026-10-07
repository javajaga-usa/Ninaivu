# Troubleshooting

## The phone cannot open Ninaivu

- The phone must be on the home Wi-Fi, not mobile data.
- Use the address the console's **Server** page lists under *for the
  household*. `localhost` only works on the server itself.
- **Network access** on the Server page must be on; off, Ninaivu answers on
  the server only. Turning it on needs a restart.
- Windows: if the firewall blocks it, the startup log prints the two `netsh`
  commands that open the ports; run them once in a terminal opened as
  administrator.

## I cannot open the console from my laptop

The console answers on the computer Ninaivu runs on only, until you tick
*Open this console from other devices at home too* on the Server page (at
that computer). Ninaivu restarts, and the console is then on the home
network too. Away from home, open it over Tailscale or WireGuard: it does
not answer the internet, so it will not open through a forwarded port or a
tunnel.

## A profile will not open away from home

Through a forwarded port, a tunnel or Tailscale Funnel, Ninaivu is on the
internet, and a profile without a PIN does not open there; plain HTTP is
refused altogether. Use Tailscale, or give the profile a PIN and reach
Ninaivu over HTTPS. [More on remote
access.](remote-access.md#home-and-the-internet)

## The browser warns about the certificate

Ninaivu serves HTTPS with its own certificate authority. Trust it once per
device: the tray's **Trust the HTTPS certificate**, or the Server page's
button, and the [tray page](../desktop-control.md) for doing it by hand on
Windows and macOS. Or put Tailscale or a reverse proxy in front, which bring
their own.

## Nothing appears after choosing the folder

The library is indexed in the background; a large one takes a while. The
Overview's **Library** numbers and the Server page's log show it going. If
the folder is on a drive that is not plugged in, the gallery says so rather
than showing nothing.

## The scan is slow

**System → Performance** says what this computer can do and what would help.
On a small machine, turn off the passes you do not need on **AI models**
(text in photographs and video moments are the expensive ones), or choose
*power-saving* on the Server page while the household is using the
computer. A scan can be stopped and picks up where it left off.

## An import says some files could not be copied

Open **Library → Import** and the **Errors** list: each file says why, in
plain words. The usual ones:

- *not allowed to read or write this file*: on a Mac the file may be locked
  (Get Info → Locked), or Ninaivu may need Full Disk Access (System Settings
  → Privacy & Security); then press **Retry failed**. Clips a camera or
  dashcam protected on its card are copied like any other, and the card is
  left alone.
- *the drive reported a read or write error*: the card, disk or cable may be
  failing; copy what you can from it soon.
- *the name or folder path is too long*, *may not accept this file name* or
  *too large for the archive drive*: the archive drive cannot hold that file
  (a FAT32 drive stops at 4 GB); a drive formatted APFS, NTFS or ext4 can.
- *the archive drive is full*: the import stopped and kept what it copied.
  Make room and press Start.

A file that failed for a passing reason was already tried a second time.
**Retry failed**, or the next Start, tries the rest again.

## Uploads say there is not enough free space

Uploads and phone backups are refused while the server's disk has less than 2 GB
free, so the index and the thumbnails always have room. Free some space, or
move the library to a larger drive.

## A model would not download

The models come from GitHub and Hugging Face. Check the machine has internet
at the moment of the download; the AI models page says which file failed.
Once a model is here nothing is asked of the internet again.

## Mugil says the copy did not restore

The weekly test brings a few files back and compares them. If it fails,
**Mugil → Test restores** says which file and why; the usual causes are a
changed Google password (sign in again on Mugil) and a full Drive.

## Mugil says nothing is uploading

Encryption is on by default, and nothing is uploaded until the encryption
key exists. Make it on the Mugil page and keep the recovery file.

## Ninaivu will not start

The log in the Control Panel (or the tray's **View the log**) has the
reason. The common ones: the port is taken by another program (Ninaivu then
moves to the next free one and says so), the state folder is on a drive that
is not mounted, or another Ninaivu is already running from the same state
folder.

Straight after an update: the first start of a new version copies the
settings and the index into `backups/before-update` in the state folder, and
does not start if that copy cannot be made, usually because the disk is
full. An older version started over an index a newer one changed does not
start either, and says which copy to restore. The
[technical administrator guide](../admin-guide.md#updating-to-a-new-version)
has the steps.

## Moving to another computer

**System → Move to another computer** lists the three things that make an
installation and, on the new machine, points the index at wherever the
library landed without re-indexing. [The details.](../moving-to-another-machine.md)

## Getting help

Ask whoever looks after your Ninaivu; the
[technical administrator guide](../admin-guide.md) says where to report a
problem. Say which version (**System → Home & extensions** shows it under *This
home*), what you did, and what the log said.
