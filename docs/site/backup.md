# Backup

Three things can lose a library: the disk, the house, and a mistake. Ninaivu
keeps a copy against each.

## Mugil — a copy outside the house

**Mugil** (முகில், *cloud*) keeps an encrypted copy of the library on Google
Drive. It is one way: nothing in Drive can change, move or remove anything at
home, and deleting a photograph from Drive does not delete it here.

Encryption is on from the start, and **nothing is uploaded until you have made
the encryption key** on the Mugil page — so no photograph, and no copy of the
index with the names given to faces, ever goes up unencrypted. An
administrator can switch encryption off; the page then says plainly that
photographs and names go up as they are.

![Mugil](../screens/console-cloud.jpg)

On the **Mugil** page: the Google account (a few minutes, once), the
encryption key and its **recovery file**, what is sent and what is kept back,
the speed and the hours it may use, and **Test restores** — the weekly test
that brings a few files back and checks they match, with its history.

!!! warning "The recovery file"
    The copy in Drive is useless without the key. Ninaivu writes a recovery
    file and asks you to keep it somewhere that is not this computer — a
    USB stick in a drawer, a password manager. Without it, a lost computer
    means a lost library, however good the copy in Drive is.
    [What is in it.](../encryption-files.md)

Google Drive is Mugil's destination today; the second disk and S3 are the
separate copies below, and WebDAV is on the [roadmap](../ROADMAP.md).

**What is sent** can be narrowed in **Settings → Backup**: everything, all but
videos, or pictures only; nothing over a size; and folders or words in a path
to leave out. A file over **cloud approval** (1 GB by default) waits
until an administrator says yes to it, and the safety check says when files
are waiting.

## A second copy, and one off-site

Two more copies, set up beside Mugil:

* **A second copy** on another disk or a NAS share: every photograph copied
  across on a schedule and checked against its own record every few weeks.
  Nothing removed at home is removed there.
* **An off-site copy** to any S3-compatible bucket (Backblaze B2, Wasabi,
  MinIO, Amazon S3) or a folder (a friend's disk, a mounted share), encrypted
  with the Mugil key before it leaves. It can be put back on any computer,
  with or without Ninaivu running, by
  `ninaivu offsite-restore --recovery <recovery file> <copy> <output>`.

The safety check **Every photograph in more than one place** counts the
photographs that have no copy anywhere but this computer.

## Repairing a damaged file

The scheduled check (every 30 days by default) reads every photograph and
compares it with what was recorded when it was indexed. With **scrub repair**
on, a file that has changed on disk without being edited is put back from the
second copy or from Mugil, and the damaged one moved out of the library into
the state folder (`repair/damaged/`), never thrown away.

## Restore

**Backup & health → Restore** brings photographs back from the cloud copy in
three steps: what to bring back, where to put it, and the key. Nothing here
ever overwrites a file that is already there.

## The index

The index — faces and the names given to them, albums, who may see what — is
copied on this computer once a day, seven copies kept (**Health** shows them),
and into Drive with the library, encrypted with the same key, so a lost
computer loses nothing but time. To put a copy back: stop Ninaivu and run
`ninaivu restore <file>`. [Moving to another machine.](../moving-to-another-machine.md)

## Phones

On a phone, the family app's backup screen sends the photographs and videos
you choose to the house over the home Wi-Fi. A web page cannot read the
phone's library by itself or keep going in the background, so you pick what
to send and keep the page open while it sends; it skips what is already here
and carries on where it stopped. What arrives waits under **Review →
Uploads** for the administrator, unless the administrator switched on
**trust phone backups**; an administrator's own phone skips the review.

## Is everything safe?

The console's Overview runs the household's safety checks every time it
opens — a copy outside the house, a backup that restores, copies of the
index, healthy drives — and says what to do about each.
