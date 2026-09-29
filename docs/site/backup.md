# Backup

Three things can lose a library: the disk, the house, and a mistake. Ninaivu
keeps a copy against each.

## Mugil — a copy outside the house

**Mugil** (முகில், *cloud*) keeps an encrypted copy of the library on Google
Drive. It is one way: nothing in Drive can change, move or remove anything at
home, and deleting a photograph from Drive does not delete it here.

![Mugil](../screens/console-cloud.jpg)

On the **Mugil** page: the Google account (a few minutes, once), what is sent
and what is kept back, the speed and the hours it may use, the encryption key
and its **recovery file**, and the weekly test that brings a few files back
and checks they match.

!!! warning "The recovery file"
    The copy in Drive is useless without the key. Ninaivu writes a recovery
    file and asks you to keep it somewhere that is not this computer — a
    USB stick in a drawer, a password manager. Without it, a lost computer
    means a lost library, however good the copy in Drive is.
    [What is in it.](../encryption-files.md)

Other destinations — a second disk, a NAS path, S3, WebDAV — are on the
[roadmap](../ROADMAP.md).

## Restore

**Backup & health → Restore** brings photographs back from the cloud copy in
three steps: what to bring back, where to put it, and the key. Nothing here
ever overwrites a file that is already there.

## The index

The index — faces, names, albums, who may see what — is copied on this
computer every few hours (**Health** shows the copies) and into Drive with
the library, so a lost computer loses nothing but time.
[Moving to another machine.](../moving-to-another-machine.md)

## Phones

Photographs a phone takes back themselves up to the house over the home
Wi-Fi, into **Review → Uploads**, where the administrator files them.

## Is everything safe?

The console's Overview runs the household's safety checks every time it
opens — a copy outside the house, a backup that restores, copies of the
index, healthy drives — and says what to do about each.
