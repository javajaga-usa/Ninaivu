# Changelog

## Unreleased

### Family photo archive on a drive

- **The photographs on a USB drive, readable with nothing but a web browser.** Backup & health → Handover can now make a family photo archive on a drive: the photographs and videos under their own names, an `Open me.html` page that shows them by month with the people, albums and places and a search box, and a `Read me first.html` with a letter from the household. Nothing on the drive needs Ninaivu, the internet or a server, so a family member who never ran Ninaivu can still open the photographs ([the details](backup-recovery.md#the-family-archive-on-a-drive)).
- **Only what the family sees, unless asked.** Hidden photographs go in only when chosen; flagged items, sound recordings and the bin never do. Smaller copies (2048 pixels) fit a small drive. Made again on the same drive, only what is new or changed is copied, and a stopped run keeps the last whole page until it finishes.
- **The handover sheet names it.** Once made, the archive is listed with the other backups on the handover page and sheet, with when it was last made, and the sheet's restore steps say how to open it.

### TV album

- **One album can be shown on the televisions at home.** An administrator chooses the album, and Ninaivu answers as a DLNA media server, so a smart TV's own media player, VLC or Kodi finds it on the home network and the remote walks through its photographs and videos, newest first. It is off until an album is chosen, and the console's controls for it are still to come; until then it is set through the console's API ([the details](admin-guide.md#the-tv-album)).
- **The TV sees that album and nothing else, and only at home.** Nothing hidden, flagged, in the bin or a sound recording is offered even when the album holds it, a device outside the home network (a public address, Tailscale or WireGuard) is refused, and every address carries a secret that a new one from the console makes worthless.

### Phones back up by themselves

- **A phone can back itself up, with a sync app and a phone key.** The backup screen only sends while somebody keeps it open, because a web page cannot read a phone's library or keep going in the background. Apps made for that (PhotoSync and its kind) can, and they speak WebDAV, so Ninaivu now has a phone inbox at `/dav/` for them. A family member makes a key for each phone (`/api/phone-keys`; the screens for it are to come), types it into the app with their username, and the app sends new photographs on its own, for example whenever the phone gets home. Each file goes through the backup screen's own checks: it is recognised if the library already has it, waits in the review queue unless phone backups are trusted (and is then filed a minute after the phone stops sending, one pause of the indexer for the lot), and is filed by the date it was taken.
- **A phone key can only add photographs.** It cannot list, open or delete anything in the library, it is kept as a hash and shown only once, it stops working when its profile is switched off, made a guest or deleted, and its owner or the console can revoke it (`/api/admin/phone-keys`). The inbox does not answer the internet: only home, Tailscale and WireGuard.
- **A file sent whole can be up to 8 GB** (`phone_upload_max_gb`), about eighteen minutes of a phone's 4K video. The web server holds such a file until it has all arrived, so its body limit is raised to match; uploads from the gallery keep their own limit.

### Audit: security

A review of the whole of Ninaivu — sign-in and sessions, who may see and
change what, file paths and commands, the pages in the browser, what leaves
the machine, and how it is installed — found nothing that lets one role past
another, nothing that sends a photograph or a name away unasked, and no
known-vulnerable dependency. What it did find is below.

- **Behind Caddy or nginx, Ninaivu is told where the visitor really is.** The shipped Caddyfile answered any name on port 443 and 8443 (so a page from elsewhere could reach Ninaivu under a borrowed name), and both proxies hid the browser's own address and name; the production guide said to leave `trusted_proxies` at 0. Together that meant the session cookie went out without its `Secure` mark, the rate limits and the home-or-internet judgement were made about the proxy rather than the visitor, and the console refused honest writes from older Safari. The Caddyfile now answers only the names you give it and closes everything else, both proxies forward the browser's name and address on every route (the console's included), both say `trusted_proxies` must be 1, and the guide agrees. The session cookie, and the one a password-protected share sets, are marked `Secure` for the household's public name and for Tailscale names whatever the connection says.
- **In a Docker container, a visitor is the internet until a proxy says otherwise.** Docker Desktop and Docker's own port forwarding hand every connection to Ninaivu from the container's gateway, a private address, so a router port forwarded to the image made the whole internet look like home: tap-to-enter profiles opened, and browsing without signing in was allowed. Inside a container a private address is now the internet unless a proxy named in `trusted_proxies` says who is really asking; the console reached from the host itself is left as it was. The same question for an IPv6 address on this computer's own network is now asked only of networks that are also home networks, not a shared block at a hosting company.
- **The AI server address, the notification webhook and the off-site store agree on what is never a target.** The AI server accepted link-local and `.internal` names over plain HTTP (a cloud machine's metadata service among them); the webhook looked a name up once to check it and again to connect, so a name that changes its mind could point elsewhere; the off-site store accepted `http://` to anywhere and showed whatever text the far end sent. One rule now serves all three: loopback, home and tunnel addresses are home, globally routable addresses are the internet, and link-local, unspecified, multicast and reserved addresses are refused. The webhook connects to the very address it checked; the store needs `https://` unless it is on the home network, and shows at most a line of what it was told.
- **Turning photographs on disk asks for the administrator's password**, as deleting them and emptying the bin already did. A family member who found the administrator's device open on the family app could have turned the whole library.
- **Recently deleted never moves a file outside the library.** Putting a file back, and moving one to the bin, now check that where it is going is inside the library, as every other place Ninaivu writes already did; a linked folder inside the library pointing elsewhere could otherwise carry a file out.
- **An import cannot fill the disk from a damaged or hostile export.** Each file inside a Takeout or WhatsApp export is stopped at the size it declared, refused above the size any upload may be, and skipped when the disk is near full — the same margin every other arrival keeps.
- **Only safetensors model weights are ever loaded.** A cache holding an older pickled copy of a model was loaded as it was; such files can run code as they open. They are now ignored and the model fetched in the safe form.
- **A link from another site cannot start a video conversion.** The two addresses that start one on request now refuse a cross-site call.
- **A form posted from another page by an older browser is refused.** Ninaivu tells a write from its own pages apart by what the browser says about where the request came from; a few older engines and embedded browsers say nothing for a form post from another site while still sending the cookie. An upload, an avatar, a voice story, an edited copy or a phone-backup piece arriving with no such word and no body-marker of Ninaivu's own fetch is now refused; Ninaivu's pages send that marker. Pages are also marked never to be framed in the older header as well as the newer one.
- **Starting from a checkout installs the tested set of packages.** `start.cmd` and `Ninaivu.command` installed whatever PyPI had that day within the version ranges; they now hold to the same `constraints-tested.txt` the installers, Docker image and tests use, and refresh when a pin changes.
- **Guessing from the internet learns nothing about which names exist.** Twenty wrong passwords for a made-up name paused it for half an hour, and for a real name for longer each time; the difference told a guesser which was which. Both are now paused alike, pauses are forgotten after they have long ended, so made-up names no longer stay in the index for ever, and misses on names that are nobody's are written to the activity log at most twenty times an hour.
- **Taking over as administrator needs something of the successor's own.** A successor whose profile opened with a tap could claim with the printed code and any new password typed on the spot. A profile with a PIN now gives the PIN, and chooses its administrator password in the same step; a tap-to-enter profile is told to get a PIN or a password first. The plan page says which successors need one.
- **First-run setup makes exactly one administrator**, even when two browsers send the setup form in the same instant.
- **Private keys and the AI settings file are created private, not made private afterwards.** The local certificate authority's key and the AI models' settings file were written with the folder's default permissions and tightened a moment later; they now start at owner-only.
- **The Gemini key lives with the other credentials.** It was kept in the AI models' settings file, which sits beside the models — on whatever disk the administrator chose for them, outside the folder Ninaivu keeps private and outside the settings backup. It now lives in `gemini.json` in the state folder, written owner-only, carried by the settings backup (and, like the Google sign-in, left out of a backup written to another disk) and never in the copy of the index kept in Drive. A key already saved is moved there on the first read; if the models folder cannot be written the old copy stays and the log says which line to remove by hand.
- **Stopping Ninaivu from the control panel or `tools/stop.py` checks the server's certificate** against Ninaivu's own authority, instead of trusting any certificate on the port.
- **The Windows Python installer check matches the Python Software Foundation's name exactly**, not any certificate whose name contains it.
- **The Linux service is confined in both scopes.** The unit the installer writes now gets the device, kernel and privilege restrictions the shipped template had, in the system and `--user` scopes alike, and the system service may write only its state, its models and the library.
- **The Docker image pins its base by digest and publishes only the family app's port**; the console is reached through the compose file's loopback mapping, so `docker run -P` cannot put it on the network.
- **Each release, and the Docker image, carries a signed statement of where it was built.** `gh attestation verify` checks an installer or image against this repository.
- **The Mac setup clears the download warning only from the files it runs**, not from everything in the folder.
- **The repository ignores keys and certificates, and a pre-commit hook scans for secrets.**
- **The Mac disk image is made again when the build machine says "Resource busy"**, which GitHub's runners do now and then; a release no longer fails on the first such answer.
- The security notes now describe the Docker case and what the settings backup holds (the certificate authority's key, the cloud and Google credentials, the mail password — session tokens are stored hashed, so not those).

### Audit: what was left open

The audit of 10 October and the smoother-flow pass listed a few things they
did not get to. They are done here.

- **Stop no longer waits for the people to be grouped.** On a large library the grouping at the end of the face pass takes minutes, and Stop (or quitting Ninaivu) waited for all of it. It now stops part way, writes nothing half done, and the next scan groups them.
- **A scan that found nothing new no longer rebuilds every occasion.** Each scan ended by grouping the whole library into occasions again, most of a second on 200,000 photographs. It is now skipped when no photograph's date, place or town has changed, and when it does run, only the occasions that changed are written.
- **Faces, text and place names that were waiting for their turn carry on after a restart.** A pass that waits for the night, or for a video being watched, ends without stopping the scan, so the scan called itself done; start-up then skipped the library and the work waited for somebody to press Rescan. Start-up now carries such work on without walking the library again.
- **Compressions queued on the Large files page carry on after a restart.** The queue was kept in memory only, so a restart forgot every video still waiting. Ninaivu now writes it down and queues them again when it starts, each asked the same questions as the button asks (still in the library, not damaged, no copy made meanwhile). A Replace is carried on only within a day of being asked for.
- **The pages open without waiting for the name to be announced.** Announcing `ninaivu.local` on the network checks each name first, a second or more, and start-up waited for it. It now happens beside start-up.
- **Straightening never turns an edited photograph by an old verdict.** A suggestion made before a photograph was edited, or turned by hand, was still applied to it. Such suggestions are now dropped, and the survey looks at the photograph again.
- **Stop on the Straighten page stays stopped.** A survey waiting for the scan to finish could start anyway if Stop came at the wrong moment, and a survey stopped just as it began could come back at the next start. A clock that steps back while the computer starts (a Raspberry Pi setting its time from the network) no longer leaves the survey's automatic turning with nothing to turn.
- **The portable Windows copy keeps its logs and program files to your account.** On a USB drive or a second disk formatted NTFS, `logs\` (which holds the first-run setup code) and `app\` are now readable by your Windows account only, as `app\data` already was.
- **"Start when I sign in" on the portable copy starts that copy.** It now registers the very `Ninaivu.exe` that was opened, even renamed, and refuses with a message rather than registering a bare start that would open a second, empty Ninaivu. With two portable copies, each shows its own setting truly, and turning one off does not turn the other off.
- **One family member cannot fill the disk with uploads nobody has approved.** Each person may have up to 50 GB waiting for approval (uploads, phone backups, scanned prints and saved edits); more is refused with a plain message until some is approved or turned away. Administrators are not limited. The amount is `upload_quota_gb` under People on the console's Advanced settings page; 0 turns it off.
- **The Mac installer's package list names each package by its SHA-256**, as the Windows and Linux lists do and the release notes say. The packages are fetched first and installed from those very files.
- **"Don't ask about this drive again" is that drive only**, not every stick of the same make, size and name. Drives are told apart by their volume's serial number or UUID. A drive set aside before is asked about once more.
- **An album's date is the earliest photograph you can see in it.** It was the earliest of all of them, so a family member could learn the date of a photograph only an administrator may see. Who may open an album is decided as before.
- The portable copy's notes now say what reaches outside its folder (a trusted certificate, a tool installed from Extras, the Control Panel's settings when the folder cannot be written) and that an update should carry `app\.ninaivu-control\settings.json` across.

## 1.10.4 — 10 October 2026

1.10.3 was tagged but never released: a test that did not expect Windows' long-path form of a file name failed on that commit. Everything below, listed for 1.10.3, ships in 1.10.4.

### Faster pages

- **The faces review queue opens at once.** Suggesting faces for one person read every named person's confirmed faces, one query each; with 150 people and 150,000 faces that was 0.4 s for each person reviewed. It now reads only that person's, in one query.
- **The admin pages' whole-library figures are remembered until they change.** The storage report, the folder view, the copies pages and the face status read the whole library on every visit (a quarter to half a second each on 200,000 items). They are now kept until a photograph, a face or a copy changes, and the folder view is refreshed at least every five minutes.
- **The map is quicker to pan and opens on a remembered world view.** A pan over a country or a city is answered from the map's index rather than a row read for each photograph (where the computer's SQLite is 3.48 or newer), and the whole-world view is kept until the places change.
- **The family view's count stays on its index with newer SQLite.** On SQLite 3.53 the count of what a family member may see could read a row for every photograph; the date index now holds what that count filters on. Existing libraries rebuild these two indexes once, on the first start after updating.

### Smoother flow

- **A new photograph is in the library in seconds, not after a walk of the whole library.** When a photograph arrived — copied in from the Finder, filed from a phone backup, approved from an upload, imported, or moved to another day by changing its date — Ninaivu read every folder of the library again to find it: on a library of 200,000 files that is minutes, and it stopped whatever describing or face-finding was under way to do it. Now only the folders that changed are read. A folder dragged into the library is noticed too (on Linux and often on a Mac it was not, because its photographs arrived with no news of their own), and moving a photograph to the bin no longer sets off a scan at all. When reading a few folders cannot settle what has gone — a folder emptied all at once — the whole library folder is read, as before.
- **Describing, faces and text carry on through an approval, a phone backup and an import.** Each of these used to stop the analysis, which can have hours to run, and start it again afterwards behind a fresh walk of the library. They only add photographs, so the analysis now carries on while they do; on a machine without a graphics processor or with the library on a spinning disk, an import still pauses it, as before.
- **Photographs copied in while a scan was still reading are read straight after it, not after the whole analysis.** A scan that had already passed their folder used to describe the entire library (or wait for the night, in overnight mode) before it looked again.
- **A restart during the analysis carries on with the analysis.** If the last scan had read the library through moments before Ninaivu stopped, start-up no longer walks every folder again before describing the next photograph. Switching on place names, text reading or faces starts that work at once, rather than at the next scan.
- **Start-up no longer walks the library for photographs that cannot be described.** A few photographs whose thumbnails were missing stayed "owed" for ever, so every restart read the whole library to try them again. What the last full pass could not describe is now remembered until something new is owed.
- **A phone backup finished during an import or a consolidation is filed as soon as that ends.** It was left waiting, and nothing came back for it until another photograph was backed up. An import no longer holds phone backups up at all, and two family members finishing a backup at the same moment no longer get in each other's way. A phone backup with nothing to file no longer stops the scan.
- **The console says indexing is paused, and for what, while a consolidation or storage check has the disk.** The pause note was replaced by "Cancelled" a moment after it appeared. And a pass waiting for its turn (overnight mode, or a video being watched) no longer counts that wait in its time left, which put the finish days away.
- **Stopping Ninaivu no longer starts a scan on the way out.** Each job stopped after the scan let go of the disk, and each let-go started the scan it had queued.
- **An import stopped part way has what it copied indexed.** The files it had already copied waited for the watcher, which with watching off never came. A damaged part of an export that cannot be closed no longer leaves indexing switched off until a restart.
- **A video on a drive that dropped out, or slow to read, while it was being described is tried again.** A clip whose drive went away part way through, or one ffmpeg ran out of time on, was marked as described with nothing described, and so never turned up in search.
- **Photographs the straightening turned stay turned after a full rescan.** A Rescan with "re-read everything" put every photograph the survey had turned back the way the camera wrote it.
- **A storage check interrupted by a restart now repairs what it found, and its alert counts the whole check.** A check carried on after a restart (or started from the Storage page) ran to the end and then did nothing about the damaged files it found: only the nightly schedule handed them to automatic repair, so they waited up to a week. And the counts started again from nought when the check carried on, so damage found before the restart was left out of the "files changed on disk" message, or the message was not sent at all. Every check now hands over to repair when it ends, and the counts carry across the restart.
- **A storage check waiting for its turn no longer holds up new photographs, and Stop works while it waits.** With Ninaivu set to do heavy work overnight, a check started in the afternoon (or carried on after a restart) waited until night before reading its first file, and kept the indexer stood down the whole time, so photographs copied in that day did not appear. Stop did nothing until it was the check's turn. While it waits, new photographs are now indexed as usual, and Stop ends it at once.
- **New photographs found as the cloud backup was finishing are sent.** When a scan found new photographs in the moment a backup run was ending, the backup took note and then stopped anyway, and those photographs stayed on the computer until the next scan or restart. The backup now goes again for them.
- **A video made smaller from the Large files page goes to the cloud backup.** Compress and Replace update the library themselves, so the next scan saw nothing new and a backup that had caught up never sent the smaller video or its copy. They now tell the backup when they finish.
- **A restart in the middle of compressing a video no longer leaves gigabytes behind.** The half-made copy is a hidden file beside the video, and only a later Compress or Replace in the same folder removed it, so a restart part way through often left most of a film's size there for good. Ninaivu now clears such leftovers beside the library's videos when it starts.
- **The off-site copy tries again an hour after it could not run, not a day later.** A disk unplugged for the evening, or a service that would not answer, put the next try off by a whole day; any other failure was tried again every ten minutes. It now tries again after about an hour. And what it had already sent before the trouble is written into its list of files, so a restore from that copy knows about it.
- **An import or consolidation with several copies at once no longer copies the same photograph twice.** One copy could let go of another's hold on a photograph it was still copying, so a third identical photograph went ahead and was copied again.
- **One leftover temporary file that cannot be removed no longer makes every archive run look through the whole archive.** The clean-up of the hidden temporary files older versions left behind was meant to look through the whole archive once. But if even one of them could not be removed (still locked, or no permission), every later run looked through the whole archive again before copying anything, which on a large USB drive is minutes each time. Such a file is now remembered by name and tried again at the start of each run, and the whole archive is looked through only once.
- **A consolidation started straight after another keeps the library scan out of its way.** The first one's watcher, noticing a moment later that its job had ended, let the scan back onto the drive while the second one was copying, so both ran at once and each slowed the other. Each watcher now looks after its own job only.
- **A backup of Ninaivu's settings that fails its check is no longer remade every quarter of an hour.** A bundle that kept failing was rebuilt and checked ninety-six times a day, each time to fail the same way. After a failure Ninaivu now waits an hour, then longer each time up to a day, and goes back to the usual schedule once one succeeds.
- **The second copy starts straight away when its check makes way for it.** A check of the second copy steps aside when the nightly copy is due, and the copy then waited up to ten minutes before starting, with nothing happening. It now starts at once.
- **Ninaivu opens its pages a little sooner when it starts.** It worked out this computer's network addresses three or four times before the pages could be opened, which on a Mac can take seconds each time; it now does so once. And holding large files back for approval before they go to the cloud now happens with the rest of start-up's work, not before the pages open.

### Audit: Large files

- **Replace never puts another video's smaller copy in place of the original.** Two videos with the same name in one folder (`clip.mov` and `clip.mp4`) shared one "compressed copy", so Replace on one could put the other's picture in its place, with the real original kept only where no screen showed it. Every compressed copy now carries a note of the file it came from (copies made before are matched by their record or caption), Replace and Compress accept only a copy of that very file, and a video whose length was never recorded is measured before the copy is checked.
- **Replace gives the space back.** The original Replace keeps is now an ordinary entry in Recently deleted: listed, put back beside the smaller file if wanted, erased by hand or when the bin's time is up. Before, it stayed in `_deleted/_originals` for good, so a Replace made the library larger.
- **A video deleted while it is being replaced stays deleted.** Deleting a video with a Compress or Replace queued or running is refused with a plain message, and Replace checks again just before it swaps the files: a video deleted, changed or moved meanwhile, or Stop pressed during *Checking…*, leaves everything as it was and takes back the safety copy. A deleted admins-only video could come back as a new photograph the family could see.
- **Replace cannot fill the disk.** Its safety copy of the original is a hard link where the disk allows (no time, no room); where a real copy is needed there must be room for the whole original first, and a copy that fails part way leaves nothing behind.
- **Stop works on an ffmpeg that has gone quiet**, on a sleeping USB disk or a stalled share: it is ended, by force after a few seconds, and an encode with no progress for 10 minutes is ended with a message instead of holding up the queue. Half-written temporary files from a job that never finished (Ninaivu closed, a power cut) are removed when the next job starts in that folder.
- **Tidy copies only touches copies Ninaivu made**, per library. A file of your own named like `holiday-compressed.mp4` is never moved to the bin.
- **On Windows, Replace on a video another program has open** stops with a plain message, with no extra copy left and no second copy of the original in the library.

### Audit: drives

- **The plug-in notice no longer asks about the disk holding the second copy, the backups or a local off-site folder.**
- **"Don't ask about this drive again" on a phone is that phone only**, not every phone of the same model. Phones set aside before are asked about once more.
- **On a Mac, a disk whose check failed is asked about again a minute later**, instead of being taken for a plugged-in drive until it is remounted.

### Audit: privacy and sign-in

- **Guesses from the internet cannot lock the family out at home.** Twenty wrong PINs or passwords from twenty addresses on the internet paused that profile, or the administrator, for everybody at home as well, for up to 32 hours. Guesses from the internet now have an allowance of their own (still stopped when the home one is spent), and a tap from the internet on a profile with no PIN, which is refused, no longer counts at all.
- **Deleting a profile takes its place in the handover plan with it.** Profile numbers are used again, so the next profile made after a named successor was deleted was told it was the successor, could take over with the printed code, and a claim still waiting would have made it an administrator. Deleting a profile now takes it off the successors, ends its waiting claim, deletes its photo books and forgets who recorded its stories; a claim older than the profile never completes.
- **Making a book names no album or occasion you cannot open.** The book picker answered with the name of an album holding only admin-only photographs, or the place of an occasion outside a member's folders.
- **Accepting a "Who is this?" name never joins somebody only administrators can see.** For a family member the name matches only people on their own People page; otherwise a new person is made.
- **Straightening never looks at admins-only photographs.** The survey read them with the orientation model and the face detector, and could turn them.
- **A photo book is not downloaded again once one of its photographs is hidden or gone**; make the book again.
- **The copy of the index in Drive carries no "Who is this?" links or answers, and no handover code.**
- **The microphone is never left on.** Moving to another photograph while the browser was asking for the microphone started a recording with no Stop button.
- **The notification webhook cannot be used to look around the house's network.** It follows no redirects, the test button says only whether it answered and with which status, and link-local addresses are refused.

### Audit: family features

- **Voice stories and scanned prints leave 2 GB free on the disk that holds the index**, as uploads and phone backups already did.
- **Somebody not named in the handover plan cannot use up the household's handover attempts** before the real successor needs them.
- **Saving a batch of scanned prints twice at once saves it once.**
- **The family tree refuses a loop through a marriage** (a parent of somebody's spouse who is also their child), which made the tree's rows wrong.

### Audit: photographs and passes

- **Applying straightening no longer stalls the rest of Ninaivu.** Each photograph turned left a write open while the next was decoded, and a save from the web or the scan then waited 30 seconds and failed.
- **Undo waits until straightening has finished.** Pressed while photographs were still being turned, it put back the first ones and left the rest turned but marked as not applied, with nothing left to undo them.
- **A straightening run that fails says so** and carries on after a restart, instead of showing *surveying* for ever.
- **A library drive unplugged during the face pass loses nothing.** Every photograph after it was marked as looked at, with no faces, and never looked at again; the pass now stops and the next scan carries on.
- **ffprobe installed after ffmpeg is noticed without a restart.**

### Audit: release and tests

- **Releases run one at a time**, count only the test run on a push of that very commit, and refuse a tag on a commit that is not on main. `pip-audit` is pinned.
- **"Review & clean up" is in Tamil**: its translation key was stored with the `&` escaped, so it was never found.

### Tuning

- **Performance mode uses 95 % of every core, on any size of machine.** On an 18-core Mac it now plans 17 cores of work (15 indexing workers and 2 for the orientation survey running beside the scan, and 17 analysis threads), where it was seen using 12. Balanced on a large machine had a fixed maximum of twelve workers; it is now 80 % of the cores with at least two kept back (14 of 18). The Control Panel's Performance card said 16 at most; it now shows the same 95 % the server uses. Every kind of core an Apple chip has (Super, Performance, Efficiency) is counted, because macOS runs this background work on all of them.
- **The passes that read ahead use more of a large machine.** Reading originals for the face pass, the moments of videos, pictures for audio files and photographs for the image model each stopped at four threads whatever the machine; they now get as many as the scan workers up to four, and half of them beyond that, up to eight. The orientation survey judges up to eight at once (six before).
- **Balanced on a four-core computer with a graphics processor stays within its 80 %.** Such a machine with 16 GB measures as a powerful computer, and that profile gave at least four workers, so it planned all four cores. It now gives two (three on five cores), like any other machine of that profile.
- **The orientation survey beside the scan is part of the plan.** It ran beside the scan with its own readers on top of the scan's workers, so the two together could plan past the profile's ceiling. Its readers now come out of the same share: Peak keeps two cores for it, and a profile with no room left holds the scan while the survey looks, as it did before the two could run together.

## 1.10.2 — 9 October 2026

### New and changed

- **A portable Windows zip, for a computer where nothing is installed.** The release now carries `Ninaivu-<version>-windows-x64-portable.zip` beside the installer. Extract it anywhere that can be written to, a USB drive included, and open `Ninaivu.exe`: the zip holds `Ninaivu.exe`, `app` and `logs` and nothing else at its top. The program, its private Python, the library's index and settings and the AI models are all inside `app`, so the folder moves as one; the log is in `logs`, where it can be found and sent. Nothing is installed, and deleting the folder removes everything it made (turn *Start when I sign in* off first if you turned it on: it adds one registry value, named apart from the installed Ninaivu's). It is made in the same release job as the installer, from the same Python and packages.
- **A drive, memory card or phone plugged in is a notice at the top of the console, not a pop-up.** It used to open a centred dialog in the console and, on a Mac, a second window of the operating system's own, over whatever was in front and outside the browser. Now the console shows a notice at the top of the window: click it and the Import page opens with the drive as its source (nothing is copied until you press Start). *Export media to this drive*, *Don't ask about this drive again* and × (not now) are the buttons under it; a phone a Mac cannot read says so and offers Image Capture. The window on the Mac, and the setting that turned it off, are gone.
- **The family app says *Log off*, not *Switch profile*.** The button and the line in the profile sheet did sign the person out; the old name hid that.

### Fixed

- **ffmpeg installed outside Ninaivu is noticed, and the Install button goes.** Ninaivu looked for ffmpeg once, when it started, so an ffmpeg put on the computer with `brew install ffmpeg` in a Terminal (or winget, Chocolatey or Scoop in a prompt) while Ninaivu was running went on being offered for install on Settings → Extras, on the Overview and on the Performance page until Ninaivu was restarted. It now looks again when the page asks (at most every few seconds), the Extras page checks slowly while something is missing, and an ffmpeg that has been removed is offered again. On Windows it also looks in winget's, Chocolatey's and Scoop's folders and `Program Files\ffmpeg\bin`, which are on no PATH a running Ninaivu has.
- **The top bar is sharp on an iPhone on every page, not only the console and the family app.** 1.10.0 made those two bars solid so Safari on iOS 26 would stop drawing its own soft blur over the top of the page, but four other pages still had a see-through bar at the top and kept the smear: the page a shared album opens on, *Ask the family*, the photo editor (Sudar) and AI Studio. Their top bars are now solid too. Nothing scrolls behind them, so nothing else about how they look changes. A test now checks every top bar, so a new one cannot be made see-through again unnoticed.

## 1.10.1 — 9 October 2026

### Fixed

- **Release checks pass with the version-validation gate.** The gate tests now create matching changelog and extension metadata, so Linux, macOS and Basic/Full-tier checks can reach the existing release safeguards. Regression tests also check that a missing changelog section or a mismatched extension version prevents publishing.

## 1.10.0 — 8 October 2026

### New and changed

- **The Large files list has one row of buttons, for the files you tick.** Instead of Compress, Replace, Keep and Delete on every row, each file has a tick box (or tick it by clicking the row), *Choose all* ticks the whole list, and the four buttons sit once above the list, saying how many files are chosen. Compress and Replace act on the ticked videos that are not already being compressed and queue them one after another, each row still showing its own progress and Stop; Replace and Delete ask for your password once for all of them; Keep and Delete each go to the server in one request. With the sidebar the buttons stay in sight while a long list scrolls; on a phone they sit two by two.
- **Ninaivu no longer looks on the internet for a newer version.** The daily question to GitHub, its switch on the console's **Server** page and the tray's *Check for an update* are gone, so nothing about updates ever leaves the house. The Server page shows the version this is, and a household that wants a newer one downloads the installer and runs it when it chooses. A settings file that had the old switch on still loads; the setting is simply ignored.
- **Stop Ninaivu before updating, and every place says so.** The Server page, the Control Panel and the tray's new *How to update* all say to press **Stop** and wait until Ninaivu has stopped, then run the newer installer. The Windows installer, when it finds an earlier version, says that only the program is replaced and the photographs, settings, index, people and AI models are kept, and asks for Ninaivu to be stopped first (OK goes on, Cancel changes nothing); the Linux installer prints the same; the Mac disk image has a *Before updating* note beside the app. The guides' update sections say it too.
- **Large videos can be compressed or replaced, not only kept or deleted.** On the console's Library → Large files list, every video now has **Compress** and **Replace** beside Keep and Delete. **Compress**, marked as recommended, saves a smaller copy beside the original as an MP4 (H.264 video, AAC sound, no larger than 1080p), with the original's date, place and who-can-see-it, and leaves the original as it is. **Replace** makes the same smaller MP4, checks that it opens, gives a picture, runs as long as the original and is actually smaller, and only then puts it in the original's place; the original is first copied into `_deleted/_originals` at the top of the library, and if that copy cannot be made nothing is replaced. Replace asks for your password, as Delete does. The video keeps its albums, favourites and faces; a video in another format (AVI, MOV, WMV and so on) becomes an `.mp4` of the same name. One video is compressed at a time, with progress and a Stop button on its row, and a video that would not come out smaller is left alone. Without ffmpeg, or with an ffmpeg that has no H.264 encoder, the two buttons are greyed out and the page says why. In English and Tamil, and the buttons sit two by two on a phone.
- **Replace uses the smaller copy Compress already made.** On the Large files list, pressing Replace on a video that already has a copy from Compress now puts that copy in the original's place straight away, after the same checks, instead of compressing the whole video again; the copy is then no longer kept twice, and its albums and favourites move to the video. With no copy, or one that has changed or no longer plays, Replace compresses as before.
- **The Large files buttons run from mildest to strongest, each in its own colour.** Keep (green), Compress (yellow), Replace (orange), Delete (red).
- **One compressed copy per video, and a way to tidy the extras.** Pressing Compress on a video that already has a smaller copy now says so and points to Replace, instead of making `name-compressed-2.mp4` beside the first; a compressed copy itself is not compressed again. **Tidy copies** on the Large files list moves the copies that are not needed to the bin, after showing how many and how large and asking for your password: every copy but the newest of the same video, and every copy of a video that Replace has already made smaller. The only copy of a video is never touched, and anything moved can be put back from Recently deleted.

### Fixed

- **A video damaged at its source is marked on the Large files list and says so plainly.** A clip cut short where it came from (a camera that lost power, a copy that stopped part way) has no index, the part of an MP4 or MOV that says where each frame is, so nothing can play or compress it. Compress used to try anyway, on the Mac's video engine and then again on the processor, and showed ffmpeg's own words ("moov atom not found … Invalid data found when processing input"). Such a video is now found before anything starts: its row carries a *damaged* mark and one sentence saying it was cut short and can only be kept or deleted, Compress and Replace leave it out when it is ticked, and nothing is queued to fail. Only an older copy from elsewhere can bring it back.

- **Temporary files left by copies of locked photos are cleared, freeing the space they held.** Before 1.0.5, a photo or clip marked Locked in the Finder failed to copy, and its temporary copy (a hidden `.pam-partial-….tmp` file in the archive) was locked too, so neither the copy nor the next run's clean-up could remove it. Because those runs still ended as completed, no later run looked for them again: one archive kept 5,132 of them, 672 GB, and its next copy stopped for want of 0.3 GB. The next copy now looks through the whole archive once, unlocks and removes them, and then goes back to looking only where a run was cut short. The photos themselves were never lost: they are still waiting to be copied and go across on that run.
- **"No route to host" from the name announcer is no longer a warning with a traceback.** A Mac that has just signed in or woken sometimes refuses the first announcement of `ninaivu.local` on its own Wi-Fi address; the next one goes through and the name answers, so the warning was noise in the log and the console's problems.
- **On a Mac, the start-up message no longer calls the computer's own `.local` name a "built-in Windows hostname".** It now says "this computer's own name".
- **The selection circle no longer covers the video symbol.** In the photo grid the circle for choosing a photo and the mark showing that a tile is a video (or a sound recording) sat in the same top-left corner, so while selecting, or with the pointer over a tile, the circle hid the video mark. The mark now moves to the right of the circle whenever the circle shows.
- **Dragging a slider in Sudar with a mouse no longer highlights the panel beside it.** In Safari on a Mac, moving the Exposure slider or any other, or the Before/After slider, also swept a text selection across the editor as the mouse moved, which turned the Edited side of the picture a washed-out blue. Dragging now only moves the slider, and clears any highlight left over.
- **"Checking which way up" carries on from where it got after a restart, and says so.** The survey already kept what it had looked at, but after a restart the console counted from zero against only what was left, so it looked as if it had started again from the beginning. It now counts against the whole library, starting from the photographs already looked at, and the server log says how many that was. What it has looked at is now written down every ten seconds rather than every 500 photographs, so a restart that is not an orderly one (a power cut, a forced stop) loses at most a few seconds of work. And the automatic survey after a scan, carried on after a restart, now also turns what it found before the restart, instead of leaving those photographs waiting for review.
- **Hide logs in the Control Panel gives the window its size back.** Opening the logs made the window taller to fit them, but hiding them left it that tall with empty space where the logs had been. Hiding them now returns the window to the height it had before; if you resized it while the logs were open, it loses only the logs' height, never becoming too short to show the buttons at the bottom. The width and its place on the screen stay as they are.
- **The top bar is sharp on an iPhone.** In Safari on iOS 26, and in Ninaivu added to the Home Screen, the logo, buttons and avatar at the top of the console looked smeared, because Safari draws its own soft blur over the top of a page whose bar is see-through. The console's top bar, and the family app's, are now solid, so Safari carries the bar's colour up under the status bar instead.

### Faster

- **Compressing a large video on a Mac is several times faster.** Compress and Replace on the Large files list now use the video engine built into the Mac's chip, for reading the original as well as writing the smaller copy, instead of the processor, which stays free for the family meanwhile. The copy is still an MP4 (H.264, up to 1080p); because that engine has no quality setting like the processor's, it is given about what 1080p needs to look like the original, less for smaller pictures, and never more than half of what the original spends, so the copy comes out smaller. If the engine cannot do a particular video, that video is compressed again on the processor, as before. Windows, Linux and the Pi are unchanged.
- **"Checking which way is up" looks at several photographs at once on a machine with room for it.** It used one processor core however many the machine had; now it judges up to half the scan workers the Tuning page chose at the same time (six at most), and finds exactly what it found before, in the same order. On the Mac mini in Performance mode that is six at once. A Raspberry Pi, or any machine in Power saving, still looks at one at a time.
- **On a machine with room, "Checking which way is up" no longer pauses the scan.** It used to stop the scan's indexing and analysis while it looked, because both read every original and, on a processor alone, both want every core. Where the image model runs on a graphics processor, the library is on a solid-state disk and the Tuning page gives at least four workers, the two now carry on together; turning the photographs it found still pauses the scan for that moment. A spinning disk, a disk Ninaivu cannot identify, a machine without a graphics processor, a Pi and Power saving keep the old one-at-a-time behaviour.
- **Describing photographs opens the next batch while the graphics processor works on this one.** Only when the image model runs on a graphics processor (a Mac's, or a graphics card); on the processor alone it already has every thread the Tuning page gave it, so nothing changes there.
- **Finding faces follows the Tuning page.** It reads up to four originals ahead of the face detector as before, but no more than the scan workers chosen, so Power saving and a Pi read one or two.

## 1.0.7 — 8 October 2026

Family members can now record voice stories on photos and find them by what was said, send the elders a link to name unknown faces and build a family tree, photograph old prints so each one is cropped, straightened and filed by year, make a print-ready photo book for an occasion, and leave a handover plan so the library outlives its administrator. Plugging in a drive, card or phone now asks whether to import it, and describing videos is several times quicker.

### New and changed

- **The Control Panel has a dark look, and a calmer, more finished one in the light.** A switch at the top right picks **System**, **Light** or **Dark**. System is the default and follows the computer's own setting, changing with it while the panel is open; a choice of Light or Dark is remembered. The colours are the family app's and the console's, so the three look like one product, and on a Mac and on Windows the window's title bar follows too. The panel itself is quieter: headings in small capitals with a hairline, one small mark of colour per reading instead of a stripe, the readings and resource modes in evenly sized cards, the CPU chart and the status pill redrawn for both looks, and the log pane in the same look as the rest. Every button, reading and setting is where it was and works as before.
- **"Who is this?" questions for the elders, and a family tree.** From People in the family app (or Faces on the console) a family member chooses up to 20 unnamed faces and gets a link to send by WhatsApp; whoever opens it, with no account, sees only those faces — the whole photograph only if asked for, never an administrator-only one — and types a name and a note in English or Tamil. Answers wait for review, and accepting one confirms the face as the People screen does, so Ninaivu then finds that person across the library. Links last 30 days, can be withdrawn, take a limited number of answers, and drop any photograph hidden after they were made. Beside it, a family tree of parents and spouses, a generation to a row, holding only the people the viewer can see.
- **Photo books for occasions.** From an album, a trip, a smart album or a person, **Make a book** builds a print-ready PDF — Plain, Wedding, Pongal, Deepavali, Birthday or Holiday; A4 portrait or landscape, or a 20 or 30 cm square, at 300 dpi with optional 3 mm bleed — from the best photographs (blurred ones, screenshots and repeats left out, spread across the occasion), which you can untick before it is built on this computer. Finished books are listed under **Books** to download again or delete.
- **Scan old prints.** In the family app, **Scan old prints** takes a photo of one print or several laid on a table (or a flatbed scan) and turns each print into a photo of its own: found, flattened, turned the right way up and straightened on this machine, with faded colours fixed and, where their AI models are installed, faces restored and the print made bigger. The picture-search model, where installed, suggests the decade; the year or date you give is saved as the photo's date, in `Scanned prints/<year>`, with the phone's original kept in `Scanned prints/originals` unless unticked. Nothing is overwritten, a family member's prints wait for approval like any upload, and nothing is sent to an outside service.
- **A handover plan, so the library outlives its administrator.** Backup & health → Handover writes down who takes over (successors, in order, from the family's profiles), a message to them, where the backups are, and where the backup key's recovery papers and the computer's password are *kept* — never the passwords themselves; text that looks like the key is refused. Ninaivu fills in the computer, its version and folders, every backup with when it last worked, the backup key's fingerprint and how to restore, and prints it all as an A4 sheet in English or Tamil. A one-time handover code, kept only as a hash, lets a named successor take over from their own profile with their own password; every administrator sees the claim at the top of every page and can stop it during the wait (7 days unless set from 0 to 30), and nobody is demoted. The Overview nudges when there is no plan, when it is six months old, or when the backups changed since it was reviewed.
- **Voice stories on photos.** In the photo viewer a family member can record what they remember about a photograph or video, in Tamil or English (up to five minutes, or upload a recording up to 25 MB), with the words typed beside it if they like and who is speaking; the gallery's search finds the photograph by those words. A story is seen only by whoever may open its photograph, guests can listen but not record, and a share link carries its photograph's stories to listen to. Recordings are kept in the `stories` folder beside the index, are in the daily backup of the index, and are never sent to any outside service (the copy of the index in Drive leaves them out).

### Fixed

- **Plugging in a memory card, a drive or a phone asks whether to import it.** Ninaivu could already tell when a pendrive, a card or an external drive was plugged in (it came over from Ninaivu Lite), but the console never asked anything, so nothing happened. Now the console shows a pop-up while it is open: **Import media from this drive** opens the Import page with the drive already added as a source (nothing is copied until Start), **Export media to this drive** copies the library onto it, and **Not now** asks again next time it is plugged in. On a Mac the same question also comes up in a small window on the Mac itself, so it is asked even with no console open; its Import… button opens the console on the Import page. A disk that stays plugged in, such as a backup SSD, can be set aside with **Don't ask about this drive again**, and the Import page lists such disks with **Ask again**. On a Mac, disk images (an installer you opened), the Mac's own disks and Time Machine disks are never asked about. A phone or camera on a Mac's cable is now noticed too; a Mac does not let other programs read a phone's photos over the cable, so for one Ninaivu explains that and offers to open Image Capture, which copies the photos into a folder for the Import page.

### Faster

- **Describing videos is several times quicker.** To describe a clip Ninaivu reads five moments from it. Each was decoded at full size, a 4K frame written out as a lossless picture, read back, saved again and only then shrunk to the few hundred pixels the image model looks at, and one clip was read at a time while the rest of the processor sat idle. The moments are now shrunk by the decoder as they are read, and the next few clips are read while the model describes this one. Measured on twelve HD and 4K clips on a four-core computer, the pass went from 29.5 seconds to 5.5. Tags, search and the content check are worked out the same way, from a copy of each moment 768 pixels across, which is still more than the image model looks at.

## 1.0.6 — 8 October 2026

Sudar, Photo Studio and a photo's Details can be closed on an iPhone and the family app fills the screen, one stalled connection can no longer freeze Ninaivu over HTTPS or stop the Control Panel's Stop from working, and the guide PDFs are redesigned with a book's finish on every page.

### Fixed

- **Sudar and Photo Studio can be closed on an iPhone, and the family app fills the screen.** On a phone Sudar opened as a window 97% of the screen tall, so its header, close button included, sat under the clock and battery where a tap does not reach the page, and there was no way out. Sudar, its Portrait, Remove object, Recolor and Creative Studio windows, and Photo Studio now fill the screen on a phone and keep their header and buttons clear of the notch and the home bar. On iOS 26 a Home Screen app is told the screen is shorter than it is, so the app, the photo viewer and every sheet stopped about 60 points above the bottom over an empty band; they now reach the bottom edge. And the timeline's month marks no longer stay printed over the right-hand column of photographs after a tap: on a phone the date handle shows while the photographs scroll, and the month marks only while it is dragged. A photo's Details on a phone open as a page of their own, instead of under the viewer's bar, which covered their heading and close button.
- **One stalled connection can no longer freeze Ninaivu over HTTPS.** A device that opened a connection and then said nothing (a phone leaving Wi-Fi or Tailscale halfway through connecting, a browser connecting ahead of time) held up the whole port: pages stopped loading, and the Control Panel's Stop was answered with "Ninaivu did not accept the shutdown request". Each connection now introduces itself on its own and is given ten seconds to do it.

### Guides

- **The guides, as PDFs, are redesigned.** The household guide in English and Tamil and the technical administrator guide now have a full-bleed cover; a contents page with a page number for every chapter and section, each one a link; a coloured opener for every chapter; the Noto serif and sans faces, with their Tamil counterparts so the Tamil text is set properly; callouts for notes, tips and warnings with their own icons; screenshots in a window frame with a caption; command blocks with the comments and prompts picked out; tables that repeat their heading across pages; links between chapters that say which page they point to (a printed page cannot be clicked); the chapter name and the page number at the foot of every page; and a back cover with where to find the guide online. In the PDF reader there are bookmarks for every chapter and heading, document properties, and a tagged structure for screen readers. The pages are built from the same Markdown as before, and the version on the cover is read from the program rather than typed in. `python tools/build_guide_pdf.py` makes them; the look is in `tools/guide_pdf.css`, and the build now also needs `pypdf`.
- **Every page of the guide PDFs is finished like a book.** A tab in the chapter's colour sits on the page's edge, a step lower for each chapter, so the closed book shows where each one starts; a chapter opens with a drop cap and its first paragraph set in the serif; section headings sit under a short bar of the chapter's colour; tables have a small-capitals heading and hairlines instead of tinted bars, and a table without headings no longer prints an empty one; and the foot of each page carries the chapter in spaced capitals and the folio in the serif. The contents no longer shows a sentence from the middle of Troubleshooting as its summary.

## 1.0.5 — 7 October 2026

1.0.4 was prepared but not released, so its changes are here too. Imports say what they are doing after a restart, and protected camera clips now import on a Mac with every failure explained in plain words. Updates can no longer lose the index, the console stays closed to the internet unless you open it, the console and the family app are much quicker on a large library, and the server stays steady when scans, imports, backups and browsing all run at once.

### New and changed

- **Two console pages have names that say what is on them.** System → Settings, which holds this home's name, extensions, Extras and what this computer can do, is now **Home & extensions**. System → All settings, the full list of every setting with its meaning and default, is now **Advanced settings**. Nothing on either page moved or changed, and the Tamil names change with them.
- **An import says what it is doing from the moment Ninaivu starts again.** After a restart of Ninaivu or the computer in the middle of an import, the console and the activity strip said nothing for up to a quarter of an hour: start-up loaded the image model before picking the import up, and the import then counted every file on the sources before copying, showing 0 of 0 until the count ended. The import now carries on before the model has loaded, the Import page and the activity strip say "resuming after restart" with how many files were already done, the count shows how many files it has found so far, and the server log says when the import is picked up, how the count is going every 30 seconds, and how far the copying has got every five minutes. Stop works while it waits to carry on.
- **Refreshing the console stays on the page that was open.** A refresh always went back to the Overview, so somebody watching an import lost its numbers until they opened Import again. The open page is now kept in the address.
- **The home page of the guide site now describes Ninaivu Lite**, in English and Tamil.
- **filelock is updated to 4.0.10.**

### Fixed

- **Protected videos from a dashcam or camera card now import on a Mac.** A clip the camera protects is marked read-only on its card, which macOS shows as Locked. The import copied that lock onto its unfinished copy, and macOS will not rename a locked file, so every protected clip (dashcam event clips ending in `_E` among them) failed with "Operation not permitted" and left a hidden `.pam-partial` file behind that could not be removed. The archive copy is now never locked; the source is left exactly as it was. Leftover locked `.pam-partial` files are cleared at the start of the next import, the failed clips are copied when the import is run again, and if a rename is still refused the Import page says in plain words that the file may be locked, the drive read-only or the file held by another program. Copy to drive had the same fault and is fixed with it.
- **Every import failure now says why in plain words.** The Errors list showed the raw system error with both full paths, such as `PermissionError: [Errno 1] Operation not permitted: '…/.pam-partial-….tmp' -> '….MP4'`. Each failed file now gives the reason first (the file was locked or Ninaivu needs Full Disk Access, the file was gone by the time it was copied, the drive reported a read error, the name is too long or has characters the archive drive cannot store, the file is larger than a FAT32 drive allows, and so on) with the technical name after it; the run log still has the full detail. The finished import's message says how many files could not be copied and points at Retry failed.
- **A file that fails for a passing reason is tried once more in the same import.** A file still being written by a sync app, a file another program had open, a drive that was slow to answer, or a finished copy that something held for a moment is tried again 30 seconds after the rest of the import, instead of waiting for the next Start. Failures that will not pass by themselves, like a permission problem, are not repeated.
- **A full archive drive stops the import instead of failing every file after it.** The import stops with "the archive drive is full", keeps everything already copied and verified, and leaves the file it was copying queued rather than failed, so Start carries on once there is space.

### Updating safely

- **An update never touches the photographs, and now cannot lose the index either.** Every installer already replaced only the program and kept the library, the settings, the index and the AI models. The one thing an update did change was the index, when the new version first started and added what it needs; that could not be undone by installing the older version again. Now the first start of a new version copies the settings and the index into the state folder's `backups/before-update` before anything changes (the last three are kept), and does not start if the copy cannot be made.
- **An older version no longer opens an index a newer one changed.** It used to run its own setup over it and write its older schema number back. It now stops, says which copy to restore, and leaves the index as it was.
- **The guides say how updating works:** what each installer replaces and keeps, in English and Tamil, and how to go back to an older version in the technical administrator guide.

### Protection from the internet

- **The internet is recognised however it reaches Ninaivu.** Profiles that open on a tap, and browsing without signing in, were closed to the internet only when Remote access was set to "tunnel" or "proxy". A router forwarding a port, a Cloudflare tunnel set up without telling Ninaivu, or Tailscale Funnel counted as home, so anyone who found the address could open a profile with no PIN. Now any public address, any proxy Ninaivu was not told about, and Funnel count as the internet; the house's Wi-Fi (IPv6 included), Tailscale and WireGuard still count as home.
- **Ninaivu does not answer the internet over plain HTTP.** A connection straight from a public address without HTTPS (a forwarded port) is refused, because passwords, PINs, cookies and share links would cross the internet readable. Reach Ninaivu with Tailscale, or put HTTPS in front of it.
- **The console does not open from the internet.** It is used at home, over Tailscale or over WireGuard. The new setting `console_from_internet` (Advanced settings → Remote access) opens it to a tunnel or proxy on purpose.
- **Guessing the administrator's password is paused for longer each time.** After 20 wrong passwords in half an hour, sign-in by username is paused for 30 minutes, then an hour, then two, as the administrator's tile already was. Before, about a thousand guesses a day were possible indefinitely. The computer Ninaivu runs on can still sign in.
- **An upload that is really a playlist is refused.** A text file naming other files, saved as a video, could make an older ffmpeg build the preview from another video on the computer.
- **Uploads stop when the server's disk is nearly full,** keeping 2 GB free, as phone backups already did.
- **Signing out empties the browser's cache of thumbnails** over HTTPS, so the next person on a shared tablet cannot read them from it.
- **Error messages no longer name folders on the server** when a share-link upload or a location-free download fails; the reason goes to the log.

### Quicker console

- **The Cloud page and the activity strip no longer stall while a scan saves.** Each ask set up the cloud queue's tables again, and part of that waits for the database's write lock, so on a busy library the strip and the page froze for as long as the scan's save took. The tables are now set up once per start.
- **The Cloud page asks the database far less while uploading.** Its counts and its lists of recent, failed and set-aside files took about a third of a second at 200,000 files, every two seconds; they are now kept for five seconds, counted again at once after Retry, a rule change or an approval, and the three lists read twelve rows each instead of sorting the whole queue.
- **The Storage check page is lighter while a check runs.** The totals are worked out about four times faster, and while a check runs they are taken again every ten seconds, or at once when it finds a problem, rather than every two.
- **The Overview's "Groups of faces without a name" is counted once a minute,** or straight away after faces are named or merged. It read every face in the library every ten seconds.
- **The Server page no longer runs network commands every two seconds.** This computer's addresses and the remote-access lookup are kept for a minute; a changed remote-access setting is looked up again at once. The page's data now also carries the saved remote-access setting, network ranges and public name.
- **An open Archive tab no longer counts the whole archive every second when no job is running.** It counts every ten seconds then, and at once when a job starts or ends; during a job it still updates every second.
- **The Overview's folder totals are kept until the library changes,** and the count of analysed items until more are analysed, instead of being counted on every visit.
- **The scheduled storage check starts in the household's own night.** It always waited for one to six in the morning, whatever night was set under Workload; it now uses that night, and one to six only when none is set.

### Quicker browsing

Found by timing what the family app asks for on a 300,000-item library.

- **The gallery's layout loads about six times faster on a large library.** The database's sampled statistics took a library folder of 300,000 photographs for one of a thousand, so every 25,000-tile piece of the layout sorted the whole library (1.3 s instead of 0.2 s on a computer). The photographs' table is now measured in full.
- **The sidebar's counts, the timeline and the filters stay quick while the AI pass tags the library.** Every tag it wrote, even one that had not changed, made Ninaivu work out all of them again on the next request — over three seconds on every page load for the days a first tagging pass takes. Only a real change now counts, and a new tag only refreshes the tag list.
- **The People page, the occasions and the map's list of places open at once.** They were worked out afresh on every visit (up to a second and a half for the places); they are now remembered until faces, names, occasions or locations change.
- **Showing one person's photographs, or one tag's, is quicker.** The person filter read every photograph in the library to find theirs; the tag filter read every photograph's tags. Both now start from what the index already knows, and give exactly the same photographs in the same order.
- **A large gallery loads its later pieces sooner,** because the total is counted once rather than again for each piece. Coming back to a gallery that has not changed can be answered without sending it again.
- **Search no longer pauses for a second once a minute,** when the names and places a phrase is read against were looked up again; they are now kept until they change. Searching the same words again, or paging through them, no longer runs the AI's text model each time, or works out again which photographs the viewer may be shown.
- **Phones stop downloading photographs taken at home again every time they are opened.** The copy without the location was sent with a tag that changed each time it was used, so it never matched what the phone had kept. One person's photograph being copied also no longer holds up everybody else's.
- **A shared album's thumbnails come quicker** when no date limit is set, because the album's date is no longer worked out again for each one.
- **A library drive that is slow to answer holds up one request, not all of them.** Every request that found the "is the drive there" answer out of date asked the drive itself, at once; now one asks and the rest use the last answer.

### Steadier under load

Found by running a first scan, the second copy, repairs and a dozen family members browsing at once, on the Peak profile.

- **Asking for a scan while one runs answers at once, and no longer throws the running scan away.** Rescan, a finished import, a restored file or a photo arriving during a scan stopped the running one and waited ten seconds for it, every time; a full scan asked for again and again (by imports finishing one after another) could never finish. A scan still reading files now finishes and the one asked for follows; one that is analysing stands down at once, and the analysis carries on in the next scan.
- **Files copied in during the day are indexed straight away in overnight mode.** While a scan's analysis waited for the night, the watcher kept asking again and nothing new was indexed until the analysis had finished.
- **An item hidden while the AI passes are running is left alone by them.** A pass reads its list once and runs for hours; something hidden in the middle could still have tags, text, a search vector or faces written onto it a moment later.
- **Hiding a folder during a big first scan now covers the files the scan writes there afterwards.** They used to be indexed at the family level, and stayed there.
- **Photos hidden in iCloud or Google Photos are hidden as soon as an import has been indexed,** not after the whole scan's analysis, and the import's details are applied once per scan instead of several times at once.
- **One failed write no longer leaves a web thread showing an old library and failing every save.** The database connection is put right at the end of every request.
- **The cloud backup keeps sending during a big import.** Bringing its queue up to date failed with "database is locked" whenever the indexer saved, so it spent its time retrying; it also could start twice from a double click, and could stay off after a restore from Drive.
- **The off-site copy stops when its disk goes away,** instead of filling the system disk with a new copy, and Stop stops it in the middle of a large file.
- **Stopping a repair that is fetching a file from Drive stops at once.**
- **A playing or paused video no longer holds one of the web threads.** On a Raspberry Pi six of them left nobody's thumbnails loading.
- **The console's live progress streams leave at least half the web threads free;** a page that cannot have one polls instead. Location-free video copies are made two at a time at most.
- **AI search no longer re-reads every search vector on every search while a first scan is tagging,** and replaced vectors are no longer kept in memory when nobody searches.
- **A scan or a consolidation whose last write fails no longer stays "running" until a restart.**
- **New photos are indexed while the storage check runs.** The check reads the whole library, which takes hours on a Pi, and held the indexer for all of it. When a scan is asked for, the check now lets it index the new files and then carries on. The check can also be stopped (`POST /api/admin/scrubber/stop`; the console button comes with the next console update): it ends after the file it is reading, is not restarted by a restart, and Start carries on from where it stopped.
- **Moving the library (re-root) stops the second copy, the off-site copy, a repair, an import and the storage check first,** and waits for them and the cloud upload to let go. Before, only the indexer, the straightening pass and the upload were stopped, and the others could keep writing under the old folder.
- **With `--https`, the web server serves a bounded number of connections and closes ones left idle for 30 seconds.** It started a new thread for every connection, however many there were.

### Backups and copies do less needless work

- **The off-site copy writes its list of files every quarter of an hour, not every 200 files, and not at all when nothing changed.** The list is encrypted and sent whole each time, and for half a million files it is well over a hundred megabytes: over a home connection that was more time than the photographs themselves. A nightly run that finds nothing new now sends nothing, and the list from before is kept on the destination only when a new one replaces it.
- **The off-site copy no longer asks S3 or B2 about each file before sending it to a new, empty destination.** There is nothing there to find, and the question cost a request per file; every file is still checked after it goes up.
- **A restore from Drive over files that are still there no longer downloads the encrypted ones to compare.** A file with the size and date it had when it went up, and the same bytes at both ends as then, is taken as already there.
- **Cloud backup keeps every upload going while one large video goes up.** It used to wait for the whole group of files to finish before starting the next, so one film left the other uploads idle; now the next file starts as each one ends, and none is ever sent twice at once.
- **Looking for new files to back up takes far less time and memory on a large library.** The comparison with what has already gone is done in the database, and only new or changed files are looked at, rather than the whole record and the whole index every time the library changes.
- **The second copy no longer rewrites a file that is already on its disk.** A file whose date changed but whose bytes did not is now read and compared on both sides, and nothing is written; before, the whole file was written out again first, which on a slow USB disk was most of the run. Each folder on the copy is also listed once a run instead of once per file.
- **A check of the second copy carries on where it stopped, and steps aside for the nightly copy.** Reading a large disk back takes many hours; stopping it, or a restart, used to start it again from the first file, and while it ran the nightly copy waited.
- **A second copy that could not finish is tried again within the hour, not the next day.** A run cut short by the disk being unplugged, a full disk or Stop counted as the day's run.
- **An import no longer forces duplicates to disk, and reads a .tgz Takeout export without searching it for every file.** A file found to be already in the library is dropped without being synced first, and opening each file of a large .tgz no longer means looking through the whole export's list of files.
- **The archive's time left and copying speed leave out the count before copying and any pause,** so they are right from the start of copying and after a pause.
- **A nearly full archive disk is checked for space every five minutes without reading files back each time,** and copying to the archive on battery is slowed once for each file's bytes instead of again for the check before and the read-back after.

### Smoother hand-offs

- **The cloud backup sends new photographs after it has caught up.** Once everything was up, the backup stopped, and nothing started it again: photographs taken afterwards stayed on the computer until somebody pressed Start or restarted Ninaivu. A backup that was started stays on, and a scan that finds new photographs sends them. Pause still stops it until Start.
- **An import no longer fights the library scan for the disk.** While files were copied in, every pause in the copy set off a walk of the whole library and stopped the analysis. Indexing now waits for the import and then indexes the new files once. Imports also no longer wait for the night in overnight mode, or pause when somebody uses Ninaivu from outside the house: they use no internet.
- **WhatsApp stickers and voice notes no longer leave an import "indexing" for ever.** Files smaller than the library takes in are skipped and counted as skipped, so the albums and favourites are applied.
- **A scan queued for one drive scans that drive,** not every library folder, and a scan handed over to another one carries on with the folders it had not reached.
- **A restore from Drive is indexed in one walk instead of two,** and items put back from the bin are indexed in their own library folder even while a scan runs.
- **Ninaivu notices new photographs, and carries on an interrupted backup or storage check, as soon as it starts,** not after the image model has loaded, which on a Pi can take a minute. The scan says when it is waiting for the model.
- **A scan that found nothing new no longer re-matches the whole library's duplicates and live photos.**
- **People found by a face pass that was stopped near its end are grouped by the next scan.** Before, they waited for a pass that ran to the end, and one with nothing left to look at never grouped them.
- **Place names and sound-file pictures come before photo analysis,** which in overnight mode waits for the night. When the place-name list cannot be downloaded, it is tried again once a day instead of holding up every scan for up to two minutes.

### Steadier server, settings and shutdown

- **Stopping Ninaivu with `systemctl stop`, `docker stop` or the stop tool finishes what is in hand first.** The server was cut off on the spot, and in Docker it was killed after ten seconds. The service file and docker-compose now allow 45 seconds for the stop.
- **Stop and Restart from the console take effect in seconds.** An open browser tab held the server up for a minute or more; a video or download being sent still finishes.
- **Stopping Ninaivu keeps to its time limit and says what did not stop.** It could take minutes, and a scan still running was reported as stopped.
- **Numbers typed into config.json by hand are held within their limits.** "500 uploads at once" was used as given, and zero web threads gave a server that never answered.
- **Server settings from the environment that are out of range are corrected and named in the log.** `NINAIVU_MAX_UPLOAD_MB=0` refused every request, signing in included, without saying why.
- **The blur threshold can be changed on Advanced settings.** Every sensible value was refused, and the minimum score for reading text had no limit.
- **The desktop panel's choice of indexing workers is no longer saved as a setting.** It outlived the mode it came from when Ninaivu was started some other way.
- **Advanced settings explains Tailscale HTTPS and web threads correctly, and lists the right webhook formats.** Two settings showed another setting's explanation.
- **A setting of the wrong kind in config.json is set aside with a message, and Ninaivu starts anyway.** One wrong value stopped Ninaivu starting; another made the scan go through the recycle bin again.
- **Switching on "check for updates" works without a restart.**
- **A background task that fails with an error is written to the log.** It disappeared without a trace.
- **On Linux the computer can sleep again once Ninaivu has gone, even if it was killed.**
- **On a day with nothing to send, the weekly photograph looks once, not every 20 minutes, and a failed send is logged.**
- **The Windows firewall hint no longer mistakes a rule for port 8080 for one covering port 80.**

### Backups, Drive and the archive

- **Uploads no longer blame the disk when Google's answer is cut short.** A dropped connection while Google was answering was recorded as "could not read the file", counted against the photo, threw away its resume point, or stopped the whole backup.
- **Renaming the Drive folder takes effect for the upload already running.** The rest of that run kept filling the old folder, so one backup ended up split across two folders.
- **Stopping the off-site copy part-way through a file is reported as stopped.** It counted as a finished run, and the next scheduled copy waited a whole day.
- **An off-site copy that cannot reach its storage service stops straight away.** It read and checked every photo and video in the library first, only to fail each one.
- **A restore from Google Drive pauses if Google stops answering.** It marked every remaining file as "could not be restored" and sent a notification saying so.
- **An archive drive plugged back in is found again within minutes,** and a check that could not finish is tried again soon; an unplugged drive used to show as missing for up to a day.
- **Reading the copy of the index back from Drive retries a slow answer.** One slow moment from Google failed the whole restore-wizard step.
- **The weekly test restore checks downloads against the checksum saved when the file was uploaded,** not against whatever Drive reports at the time.
- **Disconnecting the Google account stays disconnected, and the log says if Google refuses to withdraw access.** An upload still finishing could quietly save the old permission back.
- **The backup no longer waits twice as long as intended after Google asks it to slow down** in the one-file-at-a-time mode.
- **Temporary files are cleaned up after crashes and failed writes.** Encrypted off-site copies, cut-off test restores, half-written archive status files and old unpacked index copies could stay on disk for good.
- **A damaged recovery file gets a clear message in the restore wizard** instead of a server error.
- **The Google connection is written to disk safely, and a damaged connection file is reported.** After a power cut the account could look signed out with nothing in the log.
- **A failed save in the archive database is logged.** Lost progress was redone on the next run with no explanation.

### Library, imports and visibility

- **Photos hidden in iCloud or Google Photos stay hidden from the moment they are imported.** They showed in the family gallery while the import was being indexed, and for longer if that scan was stopped. (Closes DS-15 from the data security audit.)
- **Undoing a folder visibility change covers photos that arrived in that folder since.** A folder opened to the family by mistake and then put back stayed open for anything added in between, and what the AI had read from those photos stayed too.
- **Moving the library to a new path no longer re-sends the whole off-site copy or brings back erased photos.** The off-site copy uploaded everything again, a restore could put back photos erased from the bin, and large uploads already approved or declined were held for approval again.
- **One damaged part of a Google Takeout or iCloud export no longer stops the import.** The rest comes in and the damaged file is listed; before, the import stopped at the same file every time it was started.
- **Checking a damaged backup says it did not verify.** The check failed with an error, and the console went on showing the last good result.
- **The storage check's history no longer grows without limit.** Every check added a row per photo for ever, making the index and every backup bigger each month.
- **A restore from the second copy that is stopped, or runs out of room, leaves nothing half-written in the library.**
- **Backups interrupted while they were being written are cleaned up.** Each one left a full-size temporary file in the backup folder for good.
- **A repair waiting on a very slow Google Drive download stops it after an hour,** instead of leaving it running in the background.
- **A drive check that cannot run on this computer is logged once, not every ten minutes.**
- **Restoring Ninaivu's state also checks the server's own lock to see whether Ninaivu is running.** A server whose run file was missing was not noticed.
- **The backup, restore and re-root commands describe themselves with the commands you actually type.** Their help still pointed at old `tools/` scripts.
- **A repeated off-site restore says the files were already there.** It began "Restored 0 of …", which read like a failure.

### Photos, videos and faces

- **Face models downloaded while Ninaivu is running are used straight away.** Faces, the scan's face pass and Straighten went on saying the models were missing until Ninaivu was restarted, even though the AI models page said they were ready.
- **A sound file with an oversized cover picture can no longer bring the server down.** A small music file whose cover claimed to be enormous could take over a gigabyte to open, enough to stop Ninaivu on a Raspberry Pi.
- **A thumbnail that fails to save no longer leaves a broken file behind.** On a nearly full disk, every failed thumbnail left a half-written file in the thumbnails folder that nothing ever removed.
- **Very large photographs are refused politely instead of running out of memory.** A huge photograph could be indexed and then crash Ninaivu when faces were looked for, when it was turned, or when it was opened in the browser.
- **Changing a photo's date on an external drive is quick again.** On drives formatted exFAT or FAT32, a date change copied the whole file while every other change waited, which took minutes for a long video.
- **When Ninaivu cannot watch a library folder for new files, the log says so.** New photographs then only appeared after a scan, and nothing in the log said why.
- **A face-parsing model copied into the models folder is found without a restart.**
- **Finishing a scan no longer loads the orientation model just to check it is there.** Every scan spent a second and a hundred megabytes on this.
- **A slow local AI model is reported as slow.** When it took more than 90 seconds, Ninaivu said to start Ollama even though Ollama was already running.
- **Clearing the photo viewing-copy cache now empties the folder.** A small leftover file stayed behind for every photograph ever opened.
- **Playground results nobody came back for are freed from memory.** A large result from a closed tab stayed in memory until somebody started another edit.
- **A video ffmpeg gives up on can no longer stall a scan.** It was handed to a second reader that has no time limit, which could hang a scan worker for good.
- **Ninaivu no longer keeps a little memory for every photo and video ever viewed.**
- **A video or photo being cleared from the cache while someone opens it no longer shows an error.** It is simply made again.

### Console and web requests

- **A settings change that is refused now changes nothing.** A mistyped host name showed an error while switches sent with it, such as open browsing, had already been turned on.
- **Clearing the home location together with a wrong option no longer removes it.** The page showed an error, but the home zone was already gone and downloads stopped leaving the home location out.
- **The storage-check, off-site copy and privacy settings check every field before saving any.** A very large number caused a server error, and some fields could change even though the save was refused.
- **"Send a test" now tests the notification channel itself.** With the "file changed on disk" alert unticked, it said "Nothing was sent: event not enabled".
- **Alerts in Slack and Discord show the detail on its own line** instead of a stray "\n" between the title and the detail.
- **The "form" webhook format now sends a form.** It sent JSON, which form-only services turned down.
- **Removing or changing a webhook stops pending retries to the old address.** A failed alert kept being retried with the old address and password for up to an hour and a half.
- **Upload failures on the server are recorded in Ninaivu's log.** Family members saw raw error text that included the server's folders, and the administrator was never told.
- **Renaming an album to a name already in use says so** instead of giving a server error.
- **Restoring with a damaged recovery file explains what is wrong.** It said just "key", or gave a server error.
- **A person can be assigned to a library folder whose drive is unplugged.** This was refused with "add it on the Library tab first", although the folder was already there.
- **Very large numbers or wrongly shaped requests on the copies and import pages are refused politely** instead of giving a server error.
- **The note about sound recordings appears only when sound recordings were actually left alone.**
- **Straightening refuses a confidence outside 0 to 1.** A value like 80 said "started" and then changed nothing.
- **Face lists and face scans keep to sensible sizes.** A negative number listed everything, or ran the whole library's face detection while the page waited.
- **An odd sign-in error from Google can no longer make the Cloud page think it is connected.**

### Tests and builds

- **Every installer ships the tray icon's components at recorded, tested versions.** Those few were picked fresh on the day of each build.
- **Tests that had stopped running, or could never fail, run and check properly again.** The Gemini extension's safety tests, the gallery's button and keyboard checks, and the tests for pausing and stopping an import, a revoked Google permission and guests searching "near this photo" now really test what they say.
- **The automatic checks are steadier and quicker.** The style check uses the same version as developers, the small-server run uses the versions the installers ship, slow tests no longer rebuild or sleep, the browser tests use a supported Node.js, and two old test scripts that stopped any running Ninaivu on the computer are gone.

## 1.0.3 — 7 October 2026

New features and fixes, the fixes from a data security audit, and the fixes for the findings of a second full project audit, made on 6 October 2026.

### New and changed

- **The administrator has a tile on the family app's sign-in screen, with their picture.** Until now "Who's watching?" left administrators out, so they had to choose *Sign in with a username instead* every time. Their tile is locked: tapping it asks for the password, never a PIN, and wrong guesses there count against the same allowance as the username form. The console still signs in with the username and password.
- **Hidden (admins-only) items are no longer read by AI.** Scanned documents, screenshots and anything else set to admins only are not tagged, have no text read from them, are not searched for faces and cannot be sent to Gemini. Tags, captions, read text, search vectors and unconfirmed faces made before an item was hidden are removed when it is hidden, and from items hidden in earlier releases on the next scan. An admin's own tags and captions, and faces they confirmed, are kept. A picture recognised as a document by how it looks loses its search vector as soon as it has been recognised.
- **"Open Extras" opens the Extras section again.** The button on the Performance page's "ffmpeg is not installed" card, and a running install on the Activity page, pointed at a page that had moved into Settings, so pressing them did nothing. They now open Settings at Extras.
- **On a Mac, Extras can install ffmpeg with Homebrew, and Ninaivu sees an ffmpeg Homebrew already installed.** An app opened from Finder or started at sign-in does not have Homebrew's folder on its search path, so the page said there was no package manager and videos went without poster frames even when ffmpeg was there.
- **On Windows, ffmpeg installed from Extras is found without signing out, including after a restart from the tray.** Ninaivu now expands folders the user's PATH stores as `%LOCALAPPDATA%\...`, and looks ffmpeg up the same way when it starts, not only straight after an install.
- **The admin console now shows "Ninaivu is Offline" when the server cannot be reached, as the family app does.** Opened while Ninaivu was stopped or out of reach, the console showed the browser's own error page. It now has a small offline page of its own; it stores nothing, so the console still never works offline.
- **Ninaivu sizes its work to the computer it runs on, and the console has a Tuning page.** At start it measures the cores, memory, graphics processor and drives, recognises a Raspberry Pi, and picks a Small box, Everyday computer or Powerful computer profile, which decides the indexing workers, analysis threads, web threads, image-model batch, backup uploads at once and the index cache. A Peak performance profile uses up to 95% of the processor and memory. System → Tuning shows what was measured and what the numbers are expected to use, and lets an administrator change the profile, set any number, or go back to automatic.

### Data security

- **The copy of the index is sent to Google Drive only when cloud encryption is on**, and no longer carries the sign-in record, the sign-in counters (which could hold a whole share link) or the saved details of what is in the bin. With encryption off, Mugil says to switch it on instead of sending the copy unencrypted.
- **Scheduled backups cannot be written inside a library folder or the second copy's folder**, and when `backup_dir` is outside the state folder they leave out the cloud encryption key and the Google sign-in. Restore the key from the recovery file or passphrase and connect Google again after restoring such a backup on a new computer.
- **The cloud key's passphrase cannot be one letter or one word repeated**, or use fewer than six different letters.
- **An AI server named by a host name must use https.** An IP address on the home network, or a `.local`/`.lan` name, can still use http. The Advanced settings page now checks the address the same way.
- **A mail password is never sent over a connection without TLS**, except to a mail relay on the same computer. Port 465 now uses TLS from the start.
- **The webhook address is no longer shown in the console.** Only its start (for example `https://hooks.slack.com/…`) is shown; leaving the field as it is keeps the saved address.
- **The desktop panel's logs are readable by your account only**, and a log over 5 MB is moved aside to `.1` when the server next starts.
- **The sign-in record is kept for a year, and the archive keeps the logs of its last 50 runs.**
- **Hugging Face usage reports are always off**, not only once the AI model has been downloaded.
- **On Windows, a state or backup folder outside your user folder is made private to your account**, as it already is inside your user folder.
- **Hidden photographs are never sent to Gemini or to an AI server from the photo editor.** The editor now says which photograph it is editing, and the server refuses one that is admin-only. Edits on this computer still work.
- **Putting the library back from the second copy or from Google Drive leaves out what was deleted**, whether it is still in the recycle bin or was erased from it.
- **Erasing a photograph from the bin also erases the original kept from before it was rotated, its face crops, and its XMP sidecar**, and deleted rows are overwritten inside the index rather than left in free space. The bin's "erase after" setting is now applied every day, not only when Ninaivu starts.
- **Copying the library to a drive leaves out the recycle bin.**
- **XMP sidecars are not written for hidden photographs**, and one written before a photograph was hidden is removed.
- **A share link to one photograph stops working when the photograph is hidden or flagged.** An administrator can still share a hidden photograph on purpose by making the link afterwards.
- **Share links last 30 days unless another time is chosen**, and only an administrator can make one that never ends; anyone else's lasts at most a year. Links made before keep their expiry.
- **Hiding, flagging or deleting a live photo takes its motion clip with it.**
- **Creating an album with the name of somebody else's album is refused** instead of adding to theirs.
- **Smart albums no longer show the names of people the viewer cannot see**, occasions are no longer shaped by hidden photographs, and guests are not told where an occasion took place.
- **Putting a PIN on a profile signs that profile out everywhere**, and every session ends 180 days after sign-in however often it is used.
- **Through a public tunnel or reverse proxy, profiles without a PIN do not open and the library cannot be browsed without signing in.** At home and over Tailscale or WireGuard nothing changes. Give a PIN to any profile that is used away from home through a tunnel.
- **A tunnel that names its visitor with `CF-Connecting-IP` or Tailscale's headers is no longer taken for this computer**, and HTTPS is pinned (HSTS) for the public tunnel name and `.ts.net` names.
- **The archive status page left on a drive lists only that drive's failed files**, and the off-site storage key is written owner-only and synced.

### Privacy and sign-in

- **Every kind of video now reaches guests and share links without where it was filmed.** Phone 3GP clips, camcorder MTS/M2TS files and several other types were sent as they were, because the server misjudged what kind of file they were. They now get the same metadata-free copy as an MP4, or are refused when no copy can be made.
- **Videos are now indexed with the place they were filmed.** Until a video's place is known, a family member is given a copy without its location whenever a home zone is set.
- **A downloaded photograph taken at home no longer carries its location in hidden extras.** The second frame of a 3D or multi-frame photo, a Motion Photo's video and the IPTC city fields are now left out of the copy, as the GPS already was.
- **Guests are no longer shown the town and country of a photograph, and cannot search by place or camera.**
- **With `trusted_proxies` set, only a proxy on this computer is believed about who is calling.** Another device on the network could claim to be the Ninaivu computer and skip the setup code or get fresh password guesses. A proxy on another machine or in another container goes in `NINAIVU_TRUSTED_PROXY_ADDRESSES`.
- **Sign-in sessions are stored scrambled, and the state folder is for Ninaivu's own account only.** Nobody is signed out by the change.
- **The settings file is never readable by other accounts, not even while it is being saved.**
- **Paused PINs and used-up sign-in allowances survive a restart.**
- **Very large pictures no longer take the server down.** Pictures sent to Ninaivu are limited to 250 megapixels, and a picture too large for the memory free is reported as unreadable instead of crashing the scan.
- **A warning that failed to send during a long outage is retried again the next time it happens.**
- **The desktop panel keeps showing its other readings when one (battery, disk, memory) cannot be read.**
- **ffmpeg only ever reads library files from the disk.** It never follows an internet address hidden inside a file.

### Photo library safety

- **A new photo at a path archived before is no longer skipped.** After a card is formatted, cameras start again at `IMG_0001.JPG`. A file at a path the archive had finished is now archived again unless its size and time still match. The same bytes are still recognised as a duplicate.
- **An unplugged drive whose folder stays behind is treated as away.** A library folder now counts as there only if it holds its own `.ninaivu-library` id. The storage check counts its files as unavailable rather than missing. Repair, the second copy, Google Drive uploads, phone backups and imports no longer write into the empty folder, and repair leaves alone a library where most files have gone missing.
- **The second copy no longer starts over on the system disk.** An empty folder where its disk used to be is refused instead of being wiped and filled again.
- **A library found elsewhere by its id is no longer adopted on its own.** A backup copy carries the same id, so Ninaivu records what it found, prints it at start, and moves the library only after an administrator confirms it (`ninaivu reroot`, or `POST /api/admin/library/relocation`; a console button is to come).
- **Importing the same export after losing the library brings the photos back.** Earlier imports count only while their files are still in the library.
- **Re-rooting a library now moves the recycle bin, second-copy and import records too.**
- **File names that Windows, exFAT or network drives cannot hold are made safe** when importing, filing uploads and phone backups, and copying to a drive.
- **Copy to a drive now syncs and checks each file**, and no longer skips a file because it has the same name and size.
- **A phone backup marked "already here" is sent again if the library's copy is deleted**, and only the sender's own folders count when deciding.
- **Two names that differ only in case on the second-copy disk now get their own files**, instead of setting each other aside on every run.
- **Deleting is safe across a power cut.** The bin entry is written before the file moves, and a delete that was cut short is finished or undone.
- **Deleting a photo from a library that is also the archive no longer brings it back** on the next archive run.
- **Temporary files are never written through a link** during repair, the second copy and sidecar writing.
- **The index backup no longer copies pending uploads into /tmp while it blocks the library.**
- **Phone and camera staging on Windows no longer accepts a file that stopped short.**

### Backups and recovery

- **The off-site copy notices when its destination changes.** A new folder, bucket or prefix, a different disk at the same path, or a bucket that was emptied used to be reported as "kept N of N" while nothing was there. The destination now carries an identity file, and anything not there is sent again. A folder whose disk is not mounted is refused, so the copy is no longer written onto the system disk.
- **A changed or damaged photograph no longer replaces its good off-site copy.** Each version is kept under its own name. A restore brings back the newest, and `ninaivu offsite-restore --all-versions` brings back the older ones beside it.
- **A new key or a fresh index no longer makes the off-site copy unrestorable.** The list of files already there is read first and added to, and the one before each run is kept. A copy made with another key is left alone with a message saying how to bring that key back. The console can now import a recovery file (`POST /api/cloud/encryption/import`; the button is to come).
- **The passphrase alone now restores the off-site copy.** The key settings are kept beside it, and a key made later gets its own settings file in Drive.
- **`tools/cloud_decrypt.py` and `python ninaivu/cli/offsite_restore.py` need only Python and the `cryptography` package.**
- **A new-computer restore from Google Drive now includes photographs sent after the last copy of the index**, checked by Drive's checksum, and says how many there were.
- **Off-site restores check every file against its SHA-256**, so a swapped or rolled-back file is caught. Each upload to the off-site copy is confirmed at the destination.
- **A restore can no longer be tricked into writing through a link at its temporary file names.**
- **New backup keys use stronger scrypt settings.** The key file is written safely. A damaged key file no longer has to be deleted by hand, and encrypted uploads that were abandoned no longer stay in the upload cache.

### Installers and releases

- **Upgrading on Linux keeps your library.** Running the Linux installer again used to point Ninaivu at `~/Pictures` and switch the library there. It now keeps the photographs folder the service already had, or the library the server already knows.
- **Upgrades no longer delete the AI models you downloaded.** On every platform they used to live inside the program, which each upgrade replaces, so faces, straightening and search by description had to be downloaded again. They now live in a folder of your own (`%LOCALAPPDATA%\Ninaivu\ai-models`, `~/Library/Application Support/Ninaivu/ai-models`, `~/.local/state/ninaivu/ai-models`). The Windows and Linux installers move existing ones there. In Docker they are kept on the state volume.
- **Uninstalling on Linux removes only Ninaivu.** `uninstall` used to delete the whole `--prefix` folder, whatever else was in it. It now removes only the files the installer put there. `uninstall --purge` now really removes the index, the settings and the AI models; it used to leave the index, accounts and keys behind in `~/.ninaivu`.
- **A Linux install made with `sudo` no longer runs Ninaivu as root.** The service runs as its own user, `ninaivu`, can still use port 80, and gets systemd's hardening. An install whose data is in root's home keeps running as before, and the technical administrator guide says how to move it.
- **A Linux install into a folder with `$` in its name now starts.**
- **Started with `start.sh` on Linux, Ninaivu keeps the same address.** When it may not use port 443 or 80, it now takes 8443 or 8080 every time, instead of a different random port on each start that broke the address saved on phones.
- **Releases are made only from tested commits, and never replace one already out.** Each installer is built from the exact package versions the tests ran with. A list of what each installer carries, with checksums, is published beside it, and `SHA256SUMS.txt` now checks with `sha256sum -c`.
- **Updated packages with known security problems:** Pillow 12.3, Werkzeug 3.1.9, urllib3 2.8 and others.
- **The Docker image is published with every release** (`ghcr.io/javajaga-usa/ninaivu:<version>`), and its health check follows the port you give it.
- **The Windows service script opens only the family app's port, and only to the home network.** It removes its firewall rules when uninstalled.
- **macOS: the app no longer accepts library injection through `DYLD_` variables.** Python from python.org is installed only when signed by the Python Software Foundation's own certificate. Setup works from a folder whose name has quotes or `$` in it.
- **`tools/setup_ai_models.py` downloads only pinned, checksummed files,** the same ones as Admin → AI models.

## 1.0.2 — 6 October 2026

Fixes for the findings of the project audit of 5 October 2026.

### Recovery

- **A damaged photograph already on disk is no longer counted as restored.**
  An encrypted Google Drive backup's checksum is of the encrypted bytes, so a
  file already at the target was taken as restored on its size alone, and a
  damaged photograph is the size it was. The backup is now downloaded,
  decrypted and compared byte for byte; a different file is left alone and the
  restored one goes beside it as `name (restored).ext`.
- **The off-site restore checks files already there, in the console and in
  `ninaivu offsite-restore`.** Both skipped an existing file (the tool did so
  whatever its size, and still said all was well). The manifest now carries
  each original's SHA-256, so an intact file is recognised without fetching
  its copy; with an older manifest the copy is fetched and compared. Files
  already there and files put beside are reported.
- **A restore no longer follows a linked folder out of the folder it was
  given.** A symbolic link inside the destination (or as its staging folder)
  carried restored files somewhere else; paths are now checked with links
  followed, before and after the folders are made.

### Privacy

- **A video whose location cannot be removed is no longer sent as it is.**
  Without ffmpeg, or for a video ffmpeg cannot copy, guests, share links and
  family members whose copies leave out the home location were sent the
  original, with the place it was filmed in it. They are now told the video
  cannot be shown, and the log says ffmpeg is needed. The same goes for a live
  photo's clip. The playable copy made of a video a browser cannot open no
  longer carries the original's metadata either (copies made before are made
  again when next played). The Docker image now includes ffmpeg.

### Trustworthy answers

- **"Everything is safe." is said only when every check could answer.** A
  check that failed to run now makes the Overview say how many things could
  not be checked.
- **A notice that reached nobody is tried again.** The six-hour quiet window
  started before sending, so a mail server or webhook that was down for a
  minute kept a failing-drive or stalled-backup warning quiet for six hours.
  It now starts after a delivery; a failed notice is tried again after 1, 5,
  15 and 60 minutes, and two reports at once send one notice.
- **The Server and Performance pages come back when the system will not give
  a reading** (swap, memory, or the list of Ninaivu's own processes, in a
  sandbox or a container); that reading is left empty.
- **Request bodies sent without a length are held to their limits.** JSON
  bodies (4 MB) and phone backup pieces (32 MB) are read no further than their
  limit, rather than up to the whole upload ceiling.

### Installers

- **Linux: folders with a space in their name work.** The desktop entry, the
  service and the commands now quote the program, state and photographs
  folders each by their own rules; a name with a line break is refused.
- **Linux: an upgrade stops the running Ninaivu, and starts the new one.** It
  replaced the program under a running server and left the old version
  running. The installer now stops the service (and closes programs still
  running from the old version) before replacing it, keeps the old one until
  the new one is in place, and restarts the service. It also says the right
  port: 80 for the system service, 8080 for a user's.
- **macOS: stopping the folder watchers is serialised and waits for each
  one.** A watcher stopped while it was still starting could crash the server
  on a Mac.
- The Python bundled in the Mac and Linux installers is checked against a
  SHA-256 pinned in the repository. The Docker image's version label is no
  longer a stale `0.1.0`.

## 1.0.1 — 6 October 2026

Fixes that came in after 1.0.0 was published. Upgrading from 1.0.0 needs
nothing; the library and the index are unchanged.

- **A proxy on the Ninaivu computer no longer skips the setup code.** With
  Caddy or Tailscale Serve on the same computer in front of Ninaivu over
  plain HTTP, the first-run page let a visitor through the proxy create the
  first administrator without the code: the web server deleted the
  proxy's `X-Forwarded-For` and `Forwarded` headers before Ninaivu saw
  them, so the visit looked like a browser on the computer itself. Ninaivu
  now sees those headers and asks for the code. A browser on the computer,
  the Control Panel and the `ninaivu.local` link still need none. The same
  change makes `trusted_proxies` work again when Ninaivu runs without TLS:
  ProxyFix never received the headers it was set up to read.
- **The guide, in English and Tamil, now installs Ninaivu only with the
  installers.** The Install chapter names the file for each computer on the
  download page and says how to upgrade; it no longer shows the source
  repository, `git clone`, Docker or running from a checkout, and the
  console guide, AI and Troubleshooting chapters lost their command-line
  tables and GitHub links. All of that moved to a new **technical
  administrator guide** (`docs/admin-guide.md`, English only), which the
  release also publishes as `Ninaivu-admin-guide.pdf`.
- **A storage check that meets a photograph a scan has just removed** no
  longer stops early with a database error and loses its alerts; it skips
  that photograph and carries on.
- **The off-site upload speed on Windows no longer reads low.** Bytes sent
  within one tick of the Windows clock (about 15 ms before Python 3.13) were
  left out of the speed the console shows; they now count in the next
  reading.

## 1.0.0 — 5 October 2026

The first release for any household, not only the one Ninaivu grew up in. It
brings in what [Ninaivu Lite](https://github.com/javajaga-usa/Ninaivu-lite)
and [Hearth](https://github.com/javajaga-usa/Hearth) learned after the fork,
fixes what a full review of the code found, makes the library harder to lose
track of, and makes scanning and the database faster with the same answers.

Upgrading from 0.1.x needs nothing: the index is brought up to date at the
first start, and the library's `YYYY/MM/DD` folder layout is unchanged, so no
photograph moves. The installers are not signed yet (see
[the roadmap](ROADMAP.md)): Windows SmartScreen and macOS Gatekeeper will ask
once before the first start. Several of the new features below are switched
on in **Settings** or through the API until their console pages arrive.

### From Ninaivu Lite

What Lite 1.5 learned that Ninaivu had not.

- **A pendrive, an external hard drive or a phone plugged in can be asked
  about.** `GET /api/admin/drives` lists what was carried in (USB sticks,
  memory cards, USB and SD hard drives, and phones: on Windows through the
  shell, as Import already reads them, on Linux as the desktop opened them),
  never the computer's own disks or the one the library lives on, and says
  which nobody has answered yet; a drive is asked about once each time it is
  plugged in. `POST /api/admin/drives/export` copies the library's photos and
  videos into a `Ninaivu` folder on the drive, adding only what is new and
  never overwriting a file there. Import from the drive is the Import page
  with the drive as its source. The console's question box is still to come.

- **Moving up from Ninaivu Lite.** `ninaivu import-lite lite-export.json`
  reads the file Lite's `--export` writes and brings across people (with
  their passwords and PINs, which Lite hashes the way Ninaivu checks them),
  folder rules, each photograph's own visibility, favourites, albums and share
  links, whose tokens are kept so a link already sent keeps working. Run it
  with Ninaivu stopped, after its first scan of the same folders. Nothing in
  Ninaivu is overwritten, and photographs it cannot find are listed.
- **Importing old drives, cards and phone backups, as in Lite.** Before the
  first import the archive's folder is suggested as "Ninaivu Archive" inside
  the first library folder, so what comes in is shown to the family once it
  is indexed (it is stepped around when the library is itself a source).
  An archive inside a library counts as in the library, so it is not offered
  or added a second time. A source or destination typed without its full
  path is refused rather than read from wherever Ninaivu started. Every
  refusal and notice on the Import page now reaches the page as a sentence
  it can translate, and Tamil has them all.
- **A way back in for a forgotten administrator password.**
  `ninaivu reset-password NAME` on the computer Ninaivu runs on sets a new
  password, turns the profile back on and signs it out everywhere.
- **Share links show a turned photograph upright.** A photograph turned by
  hand, or put right during the scan, opened on its side for a visitor: the
  share page does not turn pictures, so the turn is now baked into the
  visitor's copy.
- **A video carries no location for a guest or a share-link visitor.** A
  phone writes where a video was shot into the file, as it does for a photo.
  With ffmpeg, guests and visitors get a copy with every metadata atom left
  behind and the streams copied as they are (kept in the state folder);
  without it, the original as before. An iPhone's live clip too.
- **A turn by hand survives a rescan in the thumbnails.** The index kept the
  turn, but a full rescan wrote the thumbnails and the shape from what the
  scan decided.
- **The server, tightened.** Waitress refuses an upload over the limit before
  reading it, a connection idle for a minute is closed (it was five), a JSON
  body over 4 MB is refused unread, a 413 answers as JSON, and a
  Permissions-Policy header denies the camera, microphone, location and
  payment.
- **Lighter first visit.** The pages, scripts, styles and Tamil strings are
  sent gzipped (once per version, kept in memory), not only the API's JSON.
- **Profile pictures.** A replaced picture gets a new address (it kept the
  old one for a day in every browser), is written whole before it is used,
  is turned the way the phone held it, and an administrator can take
  someone's picture down (`DELETE /api/people/<id>/avatar`).
- **Two thumbnail writers no longer share a temporary file.**
- **Windows system folders are refused on whichever drive Windows is on,**
  not only on C:.
- **Import:** an archive is never written into Ninaivu's own data folder,
  and a source that is already in the library says that adding the archive
  as well would show those photographs twice.
- **The update check waits to be asked.** It is off on a new installation
  until it is turned on on the Server page; the tray's *Check for an update*
  asks once.
- **Windows installer.** It asks a running Ninaivu to stop before writing
  anything and, while files are still in use, asks for the Control Panel to
  be closed, with Retry, instead of failing half-way; an upgrade removes the
  previous program files first (the data and the photographs are never
  touched); the uninstaller stops Ninaivu and its start at sign-in; the
  file's properties name the product, its version and
  © 2026 Jagadeesh Rajendran. The icon is plain bitmaps, which NSIS can read
  (it showed blank). `python -m ninaivu.desktop.control --stop | --status`
  is what it uses.
- **Launchers.** `start.cmd` waits on an error instead of closing with the
  reason, and the launcher brings an existing `.venv` up to date when the
  packages it asks for change (it only checked that each one imported).

### From Hearth

What Hearth, the project Ninaivu grew from, added after the fork. None of it
changes the library's folder layout. The console pages for these are still
to come; until then they are switched on in **Settings** or through the API.

- **Backup rules and large-file approval.** Mugil can leave out videos (or
  everything but pictures), files over a size, and folders or words in a
  path. A file over 1 GB (`cloud_approval_mb`) waits for an administrator's
  yes, and the household is told, at most twice a day, when files wait.
- **A second copy on another disk or NAS**, made on a schedule and checked
  against its own record, never removing what was removed at home; and a
  restore from it.
- **An encrypted off-site copy** to an S3-compatible bucket or a folder,
  encrypted with the Mugil key, with `ninaivu offsite-restore` to bring it
  back on any computer without Ninaivu running.
- **Repairing damaged files.** The storage check runs on a schedule
  (`scrub_every_days`, 30 by default) and, with `scrub_repair`, puts a file
  whose bytes changed without an edit back from the second copy or Mugil,
  but only from a copy that hashes to what the file was. The damaged file is
  kept in the state folder.
- **"Every photograph in more than one place"**, a new safety check, and a
  copies report saying which photographs exist only on this computer.
- **Search in plain words.** In "Maya and Arjun at Ooty", names the library
  knows become people and a town it has photographs from becomes a place
  (`phrase_search`); the rest goes to the ordinary search, and the results
  say what was understood.
- **Smart albums**: saved searches that fill themselves.
- **Storage report**: what takes the space, by folder, kind and year.
- **No home location leaves the house.** For family members, downloads, zips
  and the viewer drop the location from photographs and videos taken near
  home (`strip_location`, `home_lat`, `home_lon`, `home_radius_m`);
  administrators get the original, and guests never get a location.
- **XMP sidecars, opt-in** (`xmp_sidecars`, off by default): ratings,
  favourites, people and hand-written captions written beside each
  photograph as `IMG_1234.jpg.xmp` for other programs to read.
- **Anything plays.** Any video or sound the browser cannot open is
  converted, and `/api/stream` plays the conversion as it is made instead of
  waiting for the finished copy.
- **Importing Google Photos, iCloud and WhatsApp exports.** The zips as
  downloaded are read in place; new photographs are filed into the library's
  date folders, anything already here (by hash) is not copied again, and
  dates, places, descriptions, favourites, hidden and albums come across.
  Something deleted in iCloud stays deleted. Console only.

Fixed while porting, so not carried over from Hearth: a stopped backup run
no longer ends the schedule of the second copy, off-site copy, repair or
sidecars; restoring one folder from the second copy works when its name
has `_` in it (it brought back nothing); the large-file notice is actually
sent; a generated caption is no longer written into a sidecar as if
somebody had typed it.

### A library that is harder to lose track of

The library's folder layout was reviewed and stays as it is; only its edges
change, and no existing file moves.

- **Live photos stay paired when a name is taken.** When a still or its clip
  lands where a file of that name already exists, both halves now get the
  same new name, on upload approval and in the Archive. Before, each half
  got its own suffix and the two never paired again; two iPhones in one
  house with the same photo number on the same day were enough. RAW and
  JPEG pairs benefit the same way. Live photos already split by the old
  behaviour are not re-paired.
- **A library that moved is found again.** Each library folder holds a
  `.ninaivu-library` file with a random id, and the state folder remembers
  it. At start, a library that is missing is looked for by that id on the
  computer's own drives (every drive letter on Windows, `/Volumes` on a Mac,
  `/media` and `/mnt` on Linux), and if it is found in exactly one place it
  is rerooted there. A match on a network share, or in two places, is left
  to `ninaivu reroot`. On a Mac this is the SSD that mounted as
  `/Volumes/Photos 1` because another disk had the same name.
- **Undated imports are no longer one flat folder.** They go into
  `Unknown-Date/<source folder>/`; a source folder whose name reads as a
  date, or cannot be used as a folder name, still goes into the flat folder.

### Fixed after a review of the whole program

- **The first administrator's setup code can always be found.** It is now
  repeated in the last lines of the start-up banner, next to "First run",
  and kept in `setup-code.txt` in the state folder (owner-only) until the
  administrator exists, so a server started by the Control Panel, the tray
  or the sign-in task, which has no window, still shows it somewhere, and a
  restart keeps the same code. The computer Ninaivu runs on is no longer
  asked for it when the page is opened as `ninaivu.local` or by the
  computer's own address, which is what the Control Panel and the tray open.
- **Emptying the recycle bin** no longer erases the thumbnails of a new
  photograph saved at the same path, and a photograph restored under a new
  name gets its own thumbnails.
- **`ninaivu reroot`** no longer refuses on every machine where the server
  ever started (it checks whether the lock is held, not whether the lock
  file exists), and puts the thumbnails back if the index rewrite fails.
- **`tools/db_maintenance.py --prune-thumbs`** removes thumbnails again: it
  looked for names the thumbnails do not have. It keeps the ones the bin
  and the upload queue use, and anything less than an hour old.
- **Archive:** two workers can no longer reserve `IMG_1.JPG` and
  `img_1.jpg` at once, which on NTFS, exFAT or APFS is one file.
- **Cloud restore:** files with `:` in their names (common on a Mac) are
  restored (the colon is replaced only on Windows), and a name like
  `IMG [1].jpg` no longer gets a new `(restored N)` copy on every run.
- **Storage-check alerts** about damaged or missing files are sent; they
  never were.
- **Turning backups off** while Ninaivu runs no longer spins a processor
  core at 100% until a restart; switching them back on starts them.
- **`cloud_folder_name`** (`NINAIVU_CLOUD_FOLDER`) is read, and a rename
  reaches Mugil. The Advanced page also starts or stops the weekly digest,
  caps uploads at 6 at once, and says when a restart is needed.
- **Guests and open-browsing visitors** no longer see the household's
  camera models in search suggestions and filters.
- **Dates:** occasion titles and *on this day* use the time the photograph
  was taken where it was taken, not UTC, so a photograph from 2 a.m. in
  India is no longer titled the day before or remembered on two days, and
  undated files are no longer remembered by their modification time. A
  date set by hand is no longer the evening before west of Greenwich.
- **Smaller:** long Tamil file names fit (200 bytes, not 200 characters);
  malformed requests to the faces, straightening and notification
  endpoints get a 400, not a 500; the notifications page checks a webhook
  as the Advanced page does; removing a library typed with a trailing slash
  clears its index; location writes take the write lock; `tools/netcheck.py`
  no longer names a script that is not shipped.
- **Docs:** the production guide's paths, clone address, nginx route and
  table of contents; the Tamil guides' anchors; the descriptions of the
  repository's folders.

### Faster, with the same answers

An optimisation pass over the whole program, measured on a library of 400
photographs and checked against the index it produced before: the same
duplicate fingerprints, focus scores, dates and EXIF.

- **Scanning.** A full scan of that library takes 25.7 s instead of 30 s.
  Each photograph is opened once (it was opened a second time for its EXIF,
  and a RAW had its preview extracted twice), the thumbnails are made
  without copying the picture at each size, and the blur placeholder and
  the grid colour come from the smallest thumbnail, with the placeholder's
  arithmetic in numpy. A scan no longer replays the schema and the healing
  checks under the write lock for every library folder.
- **The database.** Indexes for what deleting, the storage check, the
  person filter, albums and the console overview read, and a larger page
  cache. Deleting or restoring a batch asks once per relation instead of
  once per photograph; the album list looks its covers and first dates up
  in one query; the unnamed-face groups and the console's count of them no
  longer run a query per group; a thumbnail request reads the few columns
  it needs and no longer counts the library; the storage check writes its
  records in batches.
- **The archive (Mugil's consolidation).** An index on the destination path,
  so a dry run over a large archive is no longer slower for every file it
  has already planned; a video or sound file goes straight to its container
  for a date instead of being searched for EXIF; a PNG screenshot is no
  longer decoded in full to look for a date it does not carry; the
  same-size check that decides whether a file is read twice is scoped to
  this archive; an audit writes only the rows whose answer changed; the
  status stream counts the archive once a tick and reads the battery every
  five seconds instead of every second.
- **Importing an export.** A Takeout sidecar is reduced to the few facts
  the import reads as soon as it is seen, instead of the whole JSON being
  held until its photograph turns up in a later zip; the duplicate check is
  limited to the library's own folders, which lets it use the index.
- **Cloud, background jobs and start-up.** Mugil keeps its backup rules and
  key record instead of remaking them for every file it asks about; the
  sidecar pass commits every 200 photographs and before it pauses, so other
  writers no longer meet "database is locked"; the settings file is written
  at start only when something changed; and the all-in-one app gained the
  migration page the console app already had.

## 0.1.2 — 1 October 2026

- **No black windows flashing up on Windows.** Opened from the Control Panel,
  the tray or at sign-in, or restarted from the console, Ninaivu runs without
  a console of its own, and Windows gave every tool it ran — ffprobe,
  PowerShell, netsh, OpenSSL and the rest — a console window that opened and
  closed with it. Those tools now run hidden. Started from `start.cmd`,
  nothing changes: their output still shows in that window.
- **The Control Panel opens at a size that fits.** It is sized to what is in
  it, at the display's scaling, rather than to fixed pixels. A running
  server's addresses wrap beside the buttons instead of stretching the window
  almost to the width of the screen, and the window grows once its first
  readings are in, and when the log opens, instead of cutting off the bottom
  row.

## 0.1.1 — 1 October 2026

- **The Control Panel is what the installers open.** The window that starts
  and stops Ninaivu and shows its readings and log is the Start menu entry
  and a Desktop shortcut on Windows (the installer now carries Tk for it), the
  Ninaivu app on a Mac (in the Dock while it is open), and *Ninaivu Control
  Panel* in the applications menu and on the Desktop on Linux and a Raspberry
  Pi. The tray is beside it for those who want it: *Ninaivu tray* in the
  Start menu folder, `open -a Ninaivu --args --tray`, `ninaivu-tray`. A
  Python without Tk opens the tray instead of nothing.
- **Installers for Linux and Raspberry Pi, and no source in any installer.**
  `Ninaivu-<version>-linux-amd64.sh` for a PC and `-arm64.sh` for a Raspberry
  Pi 4 or 5 with a 64-bit OS: one file, run with `sh`, no root needed, that
  carries its own relocatable Python (the machine needs none), every package
  for that architecture, and Ninaivu as bytecode. It makes a `ninaivu`
  command, a desktop entry and a systemd service pointed at the photographs
  folder, starts it, and upgrades in place. The Windows and macOS installers
  already carried their own Python; all three now ship Ninaivu and its
  extensions with the Python compiled away (`installers/strip_sources.py`) —
  the web files a browser needs stay as they are. The release workflow builds
  all of them on a tag and attaches them to the GitHub release. Published by
  Jagadeesh Rajendran.

- **Look younger, in one tap, with nothing redrawn.** A button with a strength
  slider at the top of the Skin panel, in the Photo Studio and in Sudar's
  retouch studio, for one person or everyone: smoother and more even skin,
  calmer shine, brighter under-eyes, a little of the skin's own colour back;
  grey covered, strands brought out, thin hair filled. It sets the existing
  sliders, so every one of them can still be changed afterwards, and a tool
  already set higher by hand is left alone. It has no face light and no tone
  in it, so it cannot lighten skin or turn its colour, and the engine's test
  holds it to that on a deep complexion. There is no age model in Ninaivu: the
  ones that exist redraw the face and lean towards lighter, Western features,
  which is the opposite of the promise these tools make.
- **Hair: added, not only thickened — three ways, checked on a real photograph.**
  On a portrait with dark hair in front of a dark background the hair finder
  declined, by design, and every Hair slider then did nothing; and nothing in
  Ninaivu could put hair where there was none. Now:
  - *Add hair, on this machine.* A third brush beside Add and Remove in the
    Hair panel of the Photo Studio and of Sudar's retouch studio: paint where
    there should be hair — a receding hairline, a thin crown — and it is drawn
    from the strands beside it. Each painted pixel takes the colour of its
    mirror image across the nearest edge of real hair, so the new hair runs on
    from the old with the same strands, colour and shine; an *Added hair* slider
    says how much shows, and the grown hair is hair for every other tool. No
    model is needed. Held by the engine's tests.
  - *Add hair, generative.* A new AI server purpose, *Paint and ask (inpaint)*
    — the photograph, a painted area and a request — and a Magic tool in Sudar
    that uses it: paint where the hair should be and the AI server draws this
    person's own hair there. Shown only when a workflow is assigned.
  - *A brush in Sudar's retouch studio* at all: it had none, only a note
    sending people to the Photo Studio. Add, Remove and Add hair, a brush size,
    a colour-following option and "Show what is selected"; a stroke that strays
    onto the face is kept off the skin by the skin and body maps.
  - *Hair the colour of its background is found with the background-removal
    model's help.* When colour cannot tell black hair from a dark wall and the
    household has downloaded Background removal (RMBG-1.4), its foreground
    says where the wall is and the colour decides the rest; the model is asked
    only for a face that needs it.
  - *Fuller hair, rebuilt.* A thin place is judged by its neighbourhood as well
    as the pixel — lighter than the dark strands, hair all round, strands
    crossing it — and gaps are brought most of the way down to the strands'
    tone with their texture kept. A smooth patch of shadowed skin, a stray dab
    or a lone pixel the maps missed no longer becomes a dark spot.
- **The Creative Studio, brought up to the rest of Sudar.** Photographs come
  from the library as well as from files (*Add from library*, with paging);
  a postcard, collage, selective-colour picture, versions sheet, recipe or
  calendar can be saved into the library like an edit, an administrator's at
  once and a family member's into the approval queue; the picture creations
  come in three shapes — landscape, square, and a 9:16 story; a collage has
  three layouts — grid, one large with the rest beside it, and Polaroid prints
  with their captions, each a little askew; and the postcard is a postcard,
  the photograph filling the card with the words on a band that darkens
  towards the foot. The browser test covers all of it.
- **Sudar's Enhance tools are always on the page, and Colourise works on this
  machine.** The *Enhance tools* card (Upscale, Restore faces, Colourise) was
  hidden whenever no model had been downloaded and no AI server workflow was
  assigned, so a household that had not set anything up never learned the
  tools existed; and Colourise had no local path at all, only a ComfyUI
  workflow. The card now shows every tool, greys out the ones that are not set
  up, and says under them which model each needs and who can add it. The
  capabilities route carries this as `enhance_tools`.
  - *Colourise on this machine* (`ninaivu/media/onnx_tools.py`): a DDColor
    ONNX model is shown the photograph's lightness as a grey square and
    answers with colour, which is laid under the photograph's own lightness at
    full size, so every edge and every grain is the original's. The catalogue
    cannot pin that model's bytes yet, so it is the one model placed by hand:
    `ddcolor.onnx` in the models folder's `onnx` subfolder, or wherever
    `NINAIVU_COLORIZE_MODEL` or the `colorize_model` setting points. Nothing
    leaves the machine.
  - *Revive old photo*, one tap when Restore faces and Colourise are both set
    up: the faces first, then colour for the whole print.
- **Every result Sudar makes can be saved into the library.** A generated
  preview, a cut-out, a blurred background, a colour pop, an upscaled or
  restored or colourised picture: each had only *Download*. Each now has
  *Save to library* beside it, on the same route as the slider edit, so an
  administrator's copy is published beside its source and a family member's
  waits in the administrator's queue of uploads awaiting approval. Object
  removal and Clothing colour gain *Apply to photo*, which puts their result
  on the canvas where the sliders, Save and Download already are.
- **Hair: Fuller hair, and hairstyles.** The skin-and-hair engine gains
  *Fuller hair*, which closes the light gaps where scalp shows between strands
  towards the hair's own tone, in proportion, leaving the glints and reaching
  nothing outside the hair map — in Sudar's retouch studio and the Photo
  Studio's Hair panel alike, with the engine's tests holding it to that. A new
  hairstyle (a bob, a fringe, curls, a braid, a bun, a beard, thicker hair) is
  drawn rather than adjusted, so Sudar offers ten of them as generative ideas
  when the Creative Studio extension or Gemini is on, and says so plainly when
  neither is; a request for one no longer lands in the retouch studio, which
  can only work on the hair that is there.
- **Clothing colour is back.** The brush-and-recolour studio was in the
  code with its tests and its Tamil, and nothing opened it. It is a Magic
  tool again, "make her sari red" opens it, and it knows a sari, a kurta, a
  veshti and a dupatta as well as a shirt.
- **The sliders people reach for first.** Sudar gains *Vibrance* (richer
  colour that leaves skin alone), *Clarity* and *Dehaze*, all from the shared
  engine; a *Story · 9:16* crop for stories and reels; an *HDR* look; a
  *Colour pop* background tool (the subject in colour, the rest in black and
  white); and a *Clear the haze* suggestion for a bright, flat scene. The
  built-in planner, the local language model and the Gemini planner speak the
  new words too ("dehaze, more vibrant and crop to 9:16"). *Improve colors*
  and *Auto enhance* now use vibrance rather than saturation. The slider that
  was labelled *Color tint* is *Saturation*, which is what it always did.
- **The guides, in both languages, brought up to date and rebuilt as PDFs.**
  The family guide said *Edit photo* opened Sudar; it opens the Photo Studio,
  which the guide never described. It now has its own section (light with the
  tone curve, colour with the mixer and Natural skin tone, detail, the ten
  looks, crop and export, and where a copy goes), and the Tamil pages carry
  everything the English ones gained since 0.1.0: the straightening that
  turns photographs by itself, Skin and hair, the Python install on Windows
  and Mac, and today's Sudar changes. Both PDFs are rebuilt from the pages.
- Reopening Sudar in the moment after closing it did nothing: the closed
  dialog was still in the page until its close event fired, and counted as
  open. Only an open one does now.

- **Real editing in the Photo Studio, on one engine shared with Sudar.** The
  Photo Studio did its sums on the numbers in the file rather than on light,
  and Sudar had a second, cruder engine of its own in which "saturation" meant
  something else. Both editors now hand one recipe to one engine
  (`ninaivu/static/js/studio/develop.mjs`, with the recipe and the looks in
  `recipe.mjs`), so a slider means the same thing wherever it is moved.
  - *Light that behaves like light.* Exposure and white balance are gains on
    linear light (40 on the exposure slider is one stop), and white balance
    changes the colour of a grey without changing its brightness. Positive tint
    is now magenta, as the slider's label always said.
  - *Shadows and highlights without halos.* Each part of the photograph is
    judged by its neighbourhood, through an edge-aware guided filter worked out
    once at low resolution, so a face in front of a bright window can be opened
    up without a pale ring round it, a grey sky above it, or its texture
    flattened; the lift is applied as a ratio, so the face keeps its colour.
    Whites, blacks, an S-curve contrast that holds black and white, and a new
    **Dehaze** (dark-channel) join them.
  - *A tone curve* over a live histogram — RGB and per channel, monotone, with
    points added by clicking and removed by dragging them off the square — and
    a histogram at the top of the Light panel.
  - *Colour in OKLab.* **Vibrance** and **Saturation** are separate sliders
    again; vibrance knows where brown skin of every complexion sits on the
    colour wheel and leaves it nearly alone. An eight-band **colour mixer**
    (hue, saturation, luminance), **black and white** whose tones come from the
    mixer's luminance sliders, and **split toning**.
  - *Detail sized to the real photograph.* Clarity (now also negative, to
    soften), sharpening on lightness only with radius and masking, luminance and
    colour noise reduction, film grain, and a vignette with midpoint and feather.
    A little dither before the last rounding keeps skies from banding.
  - *Looks for family photographs*, ten of them, shown as small pictures of the
    photograph being edited and applied with an amount slider on top of the
    person's own sliders: Natural, Festival, Golden hour, Soft portrait,
    Backlit rescue, Revive old print, Film, Monsoon, Classic and Warm black and
    white. The engine's tests hold every one of them to the promise about skin:
    none raises its lightness or drains its colour.
  - *Crop* gains flip across and flip upside down; *export* gains a size (full,
    3840, 2048 or 1080 pixels on the long edge), reduced in halving steps so
    fine patterns do not shimmer.
  - *24-megapixel photographs in strips.* The engine works a hundred rows at a
    time with only the neighbourhood each step needs, and the soften brush blurs
    only the painted rectangle: the old worker allocated about 400 MB of floating
    point for every blur at that size. Softening now also keeps every other
    adjustment, where it used to blend back towards the unedited photograph.
  - *Sudar* renders through the same engine, keeping its own names for
    adjustments (they are what Gemini and its suggestions speak) mapped onto the
    engine's. **Balance this photograph** (`ninaivu/media/enhance.py`) answers
    in the new vocabulary: exposure in stops, `vibrance` rather than
    `saturation`, and magenta for a green cast.
- **Edited copies keep what the camera wrote.** Saving an edit used to write a
  file with only its date, and a database row with only its folder and date, so
  the copy lost its camera, lens, exposure settings and place. The copy's EXIF
  now carries the source's make, model, lens, exposure, ISO, focal length,
  flash, white balance and the whole GPS block; orientation is set to 1 because
  the turn is already in the pixels, Software names the Photo Studio, and the
  maker note, thumbnails, serial numbers and owner name are left behind. The
  row copies camera, lens, ISO, aperture, shutter, focal length, coordinates,
  country and city. Both the administrator's direct save and a family member's
  save that waits for approval.
- **No more 404 for the Tamil font on Mac and Linux builds.** Every page named
  `fonts/NotoSansTamil.ttf`, which only the Windows build carries, so every
  other build asked for it and logged a 404 on every page. The stylesheet now
  names only the device's own Tamil font, and the faces that use the bundled
  file live in `css/tamil-font.css`, which the pages link only when the file
  is there; the share page does the same.
- The editing engines' Node tests now run with `pytest`
  (`tests/test_develop_engine.py`), so CI holds them too.

- **Skin and Hair, rebuilt for the people in a family photograph.** The two
  tools in Sudar's Photo Studio, and the AI studio's portrait dialog that
  duplicated them badly, are now one engine and one panel. The old ones
  selected only the largest face, with an ellipse that included the eyes and
  the lips, found no hair at all, and in the AI studio took everything the
  colour of skin for skin and everything else that was not too bright for
  hair — the wall behind a head as much as the head. What replaced them:
  - *Every face, each against its own skin.* The household's face detector
    finds every face; each is judged against a model of *its own* skin, so a
    fair face and a very dark one in one frame are not pushed towards the same
    colour. A strip of faces across the top of the panel chooses **Everyone**
    or one person; a setting chosen for one person is theirs alone.
  - *Only skin.* Eyes, brows, lips and teeth are left out, by where they are
    and how far they are from that person's skin. Grey and white brows and
    moustaches are found by position, not by being dark.
  - *Marks worn on purpose are never touched.* A bindi (red or black), kumkum,
    sindoor in a parting, and sacred ash or sandal paste across the forehead
    are found and protected from every smoothing, recolouring and blurring
    operation — byte for byte — and are kept out of every average, so their red
    cannot leak into the skin beside them.
  - *No seams.* Light is brought to the face, the ears and the neck as one
    piece, and follows the real edge of the person rather than an ellipse round
    them (which left a pale halo on the wall beside the jaw); a colour is turned
    on the neck and ears as well as the face, so there is no line at the jaw;
    smoothing stops at the face. Skin in shade — the far cheek of a face seen a
    little from the side — is skin.
  - *Nothing lightens skin.* There is no fairness, whitening or "porcelain"
    setting anywhere, and the old presets of those names are gone. **Face
    light** is an exposure change for a face that is in shadow: it is offered
    only when the whites of that person's own eyes say the face is dim — never
    from how dark the skin is, so a dark face in front of a bright wall is not
    offered a lift — and it lifts skin of every colour by the same number of
    stops, so a dark face stays as dark as it is.
  - *Tools from what a family photograph needs, measured on the household's own
    faces:* face light, **skin tone** (turn a colour cast back towards skin, by
    hue only), even out the light (one side brighter), calm the shine, even out
    the tone, brighten under-eyes, soften (blemishes, not pores; folds, lids and
    moles kept), richness; for hair and beard, strands and shine, cover grey,
    and colour — black, dark brown, brown, chestnut, henna, burgundy or any
    other. Hair that cannot be told from what is behind it is *declined*, not
    guessed, and painted in by hand.
  - **Improve faces** gives each person what stood out about them and only
    that, in one step of Undo.
  - **Natural skin tone** in the Colour panel turns the cast of a whole
    photograph back by judging the skin in it, only as far as the edge of what
    skin of any complexion looks like.
  - Painting by hand where the tools missed or erred (Add, Remove), and all
    painted areas now belong to the photograph, not the window: they go with
    the picture when it is cropped, turned or straightened, which they did not
    before.
  - The retouch works a face at a time, in CIE Lab, on a window of the picture,
    so its cost follows the faces and not the photograph's size. The faces are
    found by `POST /api/portrait/analyse` (which replaces
    `/api/asset/<id>/portrait-masks`): the picture the studio is showing goes
    to the household's own computer and four small maps and some numbers come
    back. Nothing is stored and nothing leaves the house.
  - *An optional face-parsing network* is used for where skin and hair are — at
    the places colour cannot tell, like black hair against a black wall — when
    its ONNX model is installed as `faceparse/face_parsing.onnx` in the AI
    models folder. It runs through OpenCV, so nothing new is needed to run it;
    every answer is checked against the face it is for and a face it gets wrong
    is done the built-in way. Nothing downloads it yet, and nothing needs it.
  - In Tamil throughout.
- **Straightening no longer waits for approval.** What the automatic survey
  finds after a scan is now turned straight away, as one batch that **Undo
  last straighten** puts back exactly. Only what that survey has just found is
  turned, so a batch somebody undid is not turned again by the next scan, and
  a survey started with the button still waits for review. A second switch on
  Review → Straighten (`straighten_auto_apply`, on by default) keeps the old
  behaviour.
- **The code review of 29 September**, fixed:
  - *Library.* A rescan or a re-tag no longer clears an explicit-content flag,
    tags or a caption set by hand. Face regrouping no longer overwrites a
    confirmed or rejected face. An interrupted search rebuild finishes at the
    next start. A delete that fails to record puts the files back.
  - *Sign-in and sharing.* First-time setup through a tunnel or reverse proxy
    asks for the setup code. Share-link passwords, screen unlock, password
    changes and the password asked before deleting are all rate-limited
    without races. Signing out of the console leaves the gallery signed in.
    A deleted profile's albums and phone backups no longer pass to the next
    profile made. File names in Tamil are kept.
  - *Mugil.* A new computer no longer replaces the index copy in Drive, and a
    restore from Drive no longer writes outside the libraries. Resumed uploads
    are checked against Drive's checksum. Google's rate limits pause the run
    instead of failing files.
  - *Server.* Restarting no longer replays `--admin` or `--rescan`. The
    Windows service runs as the installing user, not SYSTEM. A restart under a
    service, systemd or Docker is left to the supervisor. Network access can
    no longer be switched off inside a container. Uploads are checked for
    decompression bombs before they are read.
- `start.cmd` installs Python 3.12 for the current user when none is found.
- The Control Panel window is back beside the tray: **Start - Ninaivu Control
  Panel.vbs** on Windows, and **Ninaivu Control Panel** on a Mac. On a Mac,
  **Setup Ninaivu.command** installs Python 3.12 if needed and builds both
  apps; **Ninaivu.command** starts Ninaivu.

## 0.1.0 — 29 September 2026

The first cut of Ninaivu as a product, carried forward from Hearth 2.0.0 (with
the fixes of the 28 September 2026 code review) under a new name.

- The package, the settings, the environment variables (`NINAIVU_*`), the state
  directory (`~/.ninaivu`) and the mDNS name (`ninaivu.local`) all carry the new
  name. There is no upgrade path from a Hearth state directory in this release.
- The Cloud tab is **Mugil** and the photo editor is **Sudar**.
- Hearth's Windows, macOS and shell launchers are gone. Ninaivu installs
  from the Windows installer or the macOS disk image, or runs from
  `launcher/start.py`, Docker or the service examples under `installers/`.
- The household-specific documents and audits were not carried over; the
  guide is new, in English and Tamil, on the docs site and as two PDFs.
- Python 3.12 is the floor.
- **The product audit of 29 September**, fixed:
  - *First run.* Making the first administrator from any device but the
    server itself asks for the setup code on both ports — the console used
    to count as local by itself, so anyone at home could claim a new install
    through port 3000. The code is ten characters and guesses are limited.
    The console now answers on this computer only until *Open this console
    from other devices at home too* is ticked on the Server page. A new
    install no longer greets you with "1 thing went wrong".
  - *Privacy.* Mugil encrypts by default and uploads nothing until the key is
    made, so neither photographs nor the index copy with everybody's names
    go up in the clear; switching it off says so. The launcher no longer
    installs PyTorch and fetches a model at the first start — search by
    description is offered on the first day and in Extras. Only an
    administrator can send a photograph to an extension that goes outside
    the house, unless family members are allowed on Settings.
  - *Install.* Stop and Restart work in installed copies (the stop helper is
    in the package). `ninaivu backup`, `restore`, `list-backups` and
    `reroot` replace the `tools/` scripts an installed copy did not have.
    Environment values no longer undo choices made in the console at every
    restart, and the Docker `.env` holds only folders and ports. Linux
    without root gets port 8080, not a random one. The optional
    requirements no longer put a second OpenCV or ONNX Runtime over the
    first. The launcher checks for Python 3.12. The Windows firewall hint
    prints the commands instead of a script that was never shipped.
  - *Screens.* The keyboard help is in Tamil too; English dates follow the
    browser's English; a tap-to-enter profile says who can open it; PIN
    lockouts grow each time; the Import page's engine stamp moved to a
    tooltip; Tamil day headings stay on one line on a phone; the version is
    on Settings; folder hints no longer name the Hearth household's drives.
  - *Docs.* Every claim the audit found wrong is corrected, a list of
    everything Ninaivu sends out on its own is on Remote access, and
    third-party notices are in `docs/THIRD_PARTY_NOTICES.md`.
- **The language follows the person.** Choosing English or Tamil is saved
  on the profile and applied at sign-in on every device; the browser's own
  language only decides what a device shows before anyone has signed in.
  The lock screen now has the language switch the sign-in card has, so a
  locked screen in a language you cannot read is no longer a locked door
  with no handle.
- **Two kinds of computer.** Ninaivu works out at start whether this is a
  Basic machine (2 GB, no graphics processor) or a Full one (a GPU or Apple
  silicon), and `--ai auto` means the light engine on Basic and the image
  model on Full; the expensive passes take smaller defaults on Basic. The
  Performance page says which and why; `hardware_tier` overrides it. CI
  runs the suite as both: in a 2 GB memory cgroup, and on an Apple-silicon
  runner with the model stack installed.
- **The docs site.** `docs/site/` — Install, The first day, Family and
  roles, Backup, Remote access, AI, Troubleshooting, and the guide (the
  family app, the console), rewritten from the current screens — built
  with MkDocs Material and published to GitHub Pages on every push to
  `main` (`.github/workflows/docs.yml`). The older documents are under
  *More*; the Hearth-era `user-guide.html` and `handbook.html` are gone.
- **No request once the model is here.** `HF_HUB_OFFLINE` and
  `HF_HUB_DISABLE_TELEMETRY` are set for the process as soon as the image
  model's weights are known to be on disk, so "nothing leaves the machine"
  is literal for the model libraries too.
- **Installers.** `installers/windows/build.ps1` makes a Windows installer
  with pynsist (a private Python, Ninaivu, the wheels, both extensions; the
  tray in the Start menu and at sign-in) and the winget manifests;
  `installers/macos/build.sh` makes `Ninaivu.app` in a `.dmg` for Apple
  Silicon and Intel and the Homebrew cask. `release.yml` builds all of
  them on a version tag, signs and notarises when the secrets exist, and
  attaches them to the GitHub release with checksums.
- **A tray instead of a window.** `python -m ninaivu.desktop.tray` puts
  Ninaivu in the system tray or menu bar with one menu: running or not,
  open, start, stop, restart, check for an update, the log, the HTTPS
  certificate, start at sign-in. The Tk control panel with its graphs and
  log pane is gone; the console's Server page has both. Needs `pystray`
  (`requirements/requirements-desktop.txt`).
- **Creative Studio is an extension.** The diffusion-model editor and the
  ComfyUI AI server — `generative_editing.py`, `ai_server/`, the AI server
  page — moved out of the core into `extensions/creative-studio`, found
  through the same entry point as Gemini and off until switched on. The core
  asks it for the heavy end of Sudar through one object (`studio`); without
  it, Sudar still edits light, colour, crops and looks, and removes objects
  and upscales with the small models it carries. Background image jobs
  (`media/jobs.py`) stay in the core, since the local upscaler uses them.
- **All settings, in one place.** System → All settings shows every setting
  in six groups (Library, People, Backup, Remote access, AI, Advanced) with
  its meaning and its default, the ten a household changes first; each is
  editable there, checked before anything is written. `server/config.py`
  stays one flat dataclass — the grouping lives in
  `server/settings_groups.py`, and a test keeps every field in exactly one
  group with a `#:` comment above it.
- **Straightening looks by itself.** After every scan, the survey runs over
  what the scan indexed and puts what it finds on Review → Straighten for
  approval; a switch on that page (on by default) turns it off, leaving the
  button. It remembers how far it has looked, so a second pass — or the one
  after every scan — no longer puts the whole library through the model
  again, and it decodes each photograph only as large as it looks at it
  (about 40 ms instead of 280 ms on a 24 MP JPEG, before the model runs).
- **Import is the first Library page**, ahead of Library settings: a library
  usually begins with the drives.
- **Google Photos comes across whole.** Importing a Takeout export already
  took the dates from its JSON sidecars; now the location (often the only
  place it survives, since the export strips it from many files) and the
  description typed under a photograph go into the index too, and the
  albums come back: once the archive is in the library, the Import page
  offers to make every album folder it found as an album, from the copies
  the import kept, without reading the export a second time.
- **The first day.** Right after the administrator is made, the console
  walks through the five things a new library needs — the folder, the
  household, what the scan works out by itself, a copy outside the house,
  the address for the phones — each step skippable, each using the page's
  own routes. It opens once; an established library never sees it.
- **Remote access is a choice, not an assumption.** Server → Away from home
  picks Tailscale, WireGuard (with its subnets), a Cloudflare Tunnel, the
  household's own reverse proxy, or nothing; each says what it still needs.
  Which addresses count as away from home, which are listed for the
  household, and whose certificate is served all follow the choice. The
  default works out Tailscale by itself, as before.
- **An update check.** Once a day Ninaivu asks GitHub's releases page whether
  a newer version is out — one plain request with nothing about this computer
  in it — and the Server page says so with a link. A switch there turns it off.
- **The console says only what is true on this machine.** Each AI switch
  shows the model it needs and whether it is installed, with a link to it;
  the search box promises "last summer", which a calendar reads, and not a
  place name until place names are on; every Overview card opens the page
  behind its number; Live Photos and On This Day appear in the family sidebar
  only once there is something behind them.
- **Ninaivu's own mark**: a roof over a photograph, drawn in code
  (`tools/generate_icons.py`), in every size and splash screen.
- **A tour of the screens** (`docs/screens.md`): every page pictured from a
  generated sample library, with what it is for. The Overview puts "Needs
  you" — the queues, counted — above the numbers, and the safety checks
  point at the Health page they are about.
- **A tidy root.** `start.cmd` is the one file at the root besides what Git,
  GitHub and pip need there. The launcher is `launcher/start.py` (with
  `start.sh` beside it), the pins are under `requirements/`, and the
  changelog, roadmap, contributing and security notes are under `docs/`.
- **The console, arranged by what each page is for.** Every switch sits on
  the page of the thing it governs: the rules for everyone (who may browse
  without signing in, what is screened, how far back each role sees) open the
  Visibility page; indexing is on Library settings; the AI passes (places,
  text, faces, video moments) are on AI models beside the models they use;
  System → Settings keeps this home's name, the extensions and what is
  installed. Restoring from the cloud copy has its own page under Backup &
  health, apart from the everyday Mugil page. Library comes straight after
  Home in the sidebar, with Import as its first page.
- **Extensions.** `ninaivu/extensions.py` finds extension packages through the
  `ninaivu.extensions` entry point; the console lists them under AI models →
  Extensions with what each sends off the machine, and every one is off until
  an administrator turns it on. **Gemini is the first**, moved out of the core
  into `extensions/gemini/`. Sudar's generate route no longer falls back to
  Gemini when no local model is installed: a request that names no provider
  stays on this machine.
