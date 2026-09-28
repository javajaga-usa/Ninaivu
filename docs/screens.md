# A tour of the screens

Every page of Ninaivu, with what it is for. The pictures are from a sample
library of generated scenes (`tools/make_sample.py`), not anybody's
photographs; the layout and the words are exactly what a household sees.

Ninaivu has two faces on two ports: the **family app**, where everyone looks
at the library, and the **console**, where the administrator runs it.

## The family app

### Choosing who you are

![The profile picker](screens/family-picker.jpg)

The family app opens on the household's profiles. A family member taps their
tile and enters a PIN; a guest signs in with a password; with open browsing
on, a visitor can look at what is public without signing in at all. The name
at the top is the household's own — here "The Kumar Home".

![Entering a PIN](screens/family-pin.jpg)

### The gallery

![The gallery](screens/family-gallery.jpg)

Everything this person may see, newest first, grouped by day, with the
timeline down the right edge for jumping across years. The left column is the
library's shape: photos, videos, favourites, duplicates the index found,
albums people made, trips the dates suggest, then years and folders. Search
takes plain words ("Paris last summer"); the map button shows every located
photograph grouped by place.

### A photograph

![The viewer](screens/family-viewer.jpg)

One photograph, with its date, place and people, and the actions this role
may take: favourite, rotate, download, share, and open it in Sudar. What a
guest sees here is narrower than what a family member sees, by design.

### Sudar, the photo studio

![Sudar](screens/family-sudar.jpg)

**Sudar** (சுடர், *glow*) edits a photograph in the browser: looks,
suggestions measured from the picture, light and colour sliders, straightening
and crops, with the original on the left and the edit on the right. The
original file is never changed; a copy can be saved beside it, and a family
member's copy waits for the administrator's approval like an upload.

## The console

### Signing in

![Signing in to the console](screens/console-sign-in.jpg)

The console is for administrators only and always takes a password, never a
PIN. It answers on its own port, bound to this computer by default.

### Home — Overview

![Overview](screens/console-overview.jpg)

The first screen answers two questions before anything else. **Is everything
safe?** runs the household's safety checks — a copy outside the house, a
backup that restores, copies of the index, healthy drives — and says what to
do about each. **Needs you** counts what is waiting for a person: uploads to
approve, faces without a name, photographs that may be on their side, things
that went wrong. Then the numbers, and what each role would actually see.

### Library — Library settings

![Library settings](screens/console-library.jpg)

Which folders make up the library, and how they are indexed. The first page
after Overview on purpose: it is where a new library begins.

### Library — Folders

![Folders](screens/console-folders.jpg)

The library as folders, with what is in each, and the recycle bin. Deleting
in Ninaivu moves a file into a `_deleted` folder beside the library, where it
can be put back with its faces and albums intact.

### Library — Import

![Import](screens/console-archive.jpg)

Sweep old drives, memory cards and backup folders into one archive laid out
as `YYYY/MM/DD`. Every file is hashed on the way in and read back to verify
the copy; duplicates are recognised by content; sources are only ever read. A
dry run shows what would happen first.

### Library — Large files

![Large files](screens/console-large-files.jpg)

The files taking the most space, to look over once and mark as reviewed.

### Review — Uploads

![Uploads](screens/console-uploads.jpg)

Photographs the family sent from their phones, waiting to be filed or
refused, and the phones backing themselves up to the house.

### Review — Straighten

![Straighten](screens/console-straighten.jpg)

Photographs the model thinks are on their side, strongest guess first, to
approve or dismiss one at a time. It only ever suggests a quarter turn.

### People & access — People

![People](screens/console-people.jpg)

The household's profiles: one administrator, family members, guests. Each
has a role, a way in (PIN, password, or a tap), and optionally a folder of
their own.

### People & access — Visibility

![Visibility](screens/console-visibility.jpg)

Who sees what. The rules for everyone come first — whether visitors may
browse without signing in, whether explicit content is screened, how far back
in time each role may look — and then each folder's own setting: everyone,
family, or admins only. Every change can be undone in one step.

### People & access — Faces

![Faces](screens/console-faces.jpg)

The people the face matcher found, grouped. Name one face and the rest of its
group follow; groups waiting for a name are listed for a quick pass. Names
never leave the machine.

### Backup & health — Mugil

![Mugil](screens/console-cloud.jpg)

**Mugil** (முகில், *cloud*): the encrypted copy of the library on Google
Drive — the account, what is sent and what is kept back, speed and hours, the
encryption key and its recovery file, and the weekly test that the copy
restores.

### Backup & health — Health

![Health](screens/console-health.jpg)

The drives and their SMART state, the storage check that reads every file
back against its fingerprint, anything that went wrong since the last start,
the copies of the index kept on this computer, and how the household is
told when something needs them.

### Backup & health — Restore

![Restore](screens/console-restore.jpg)

Bringing photographs back from the cloud copy, in three steps: what to bring
back, where to put it, and the encryption key. Nothing here ever overwrites a
file that is already there.

### AI — AI models

![AI models](screens/console-ai-models.jpg)

What the scan does with AI — naming places, reading text, finding faces,
describing videos by several moments — each switch saying which model it
needs and whether that model is here, then the models themselves with their
downloads. Everything on this page runs on this machine.

### AI — AI server

![AI server](screens/console-ai-server.jpg)

For a household with a second, stronger computer: hand the heavy jobs
(generative edits, object removal, upscaling) to it.

### System — Settings

![Settings](screens/console-settings.jpg)

This home's name, the extensions installed (each saying what leaves the
computer when it is on), the optional packages, and what is installed.

### System — Server

![Server](screens/console-server.jpg)

Whether Ninaivu is running and where it answers, whether a newer version is
out, the addresses to give the household, **Away from home** — how the
household reaches Ninaivu from outside (Tailscale, WireGuard, a tunnel, a
reverse proxy, or nothing) — the resource mode, the log, and the HTTPS
certificate.

### System — Activity

![Activity](screens/console-activity.jpg)

What is running now and who is using the computer, so the backup and the
scans can make way for the household.

### System — Performance

![Performance](screens/console-performance.jpg)

What this computer can do for Ninaivu, and what would help it do more.

### System — Move to another computer

![Move to another computer](screens/console-migration.jpg)

Moving Ninaivu, or telling it the library has moved, without re-indexing
everything.
