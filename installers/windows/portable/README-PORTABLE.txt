Ninaivu, portable
=================

Ninaivu.exe, the app folder and the logs folder are all there is. Nothing is
installed and deleting the folder removes everything it made, except for what
you ask for yourself that reaches outside it:

  - "Start when I sign in" adds one value to your sign-in list in the
    registry. Turn it off in the Control Panel before you delete the folder.
  - Trusting Ninaivu's HTTPS certificate puts it among this Windows account's
    Trusted Root Certification Authorities. Remove it there (certmgr.msc,
    look for Ninaivu) if you stop using Ninaivu.
  - A tool installed from the console's Extras page that is a program of its
    own, such as ffmpeg, is installed for Windows by winget, not into this
    folder. Uninstall it from Settings > Apps. The Python packages Extras adds
    go into app\ and leave with the folder.
  - If this folder cannot be written to (a read-only drive), the Control
    Panel keeps its own settings in %LOCALAPPDATA%\Ninaivu\control instead
    of app\.ninaivu-control.

To start
  1. Extract the whole zip somewhere you can write to (your Documents, or a
     USB drive). Right-click the zip and choose Extract All.
  2. Open Ninaivu.exe in the folder it makes. The Ninaivu Control Panel opens;
     press Start.

Where things are
  Ninaivu.exe     opens the Control Panel.
  app\            the program, its private Python, and what Ninaivu keeps:
                  app\data (the index of your library, settings, people, thumbnails)
                  app\ai-models (the AI models, once you download them).
                  Do not edit what is in here.
  logs\           ninaivu.log and server.log: what Ninaivu did, for when
                  somebody asks what went wrong. Look one over before you
                  send it: it names the addresses Ninaivu answered on and
                  the setup code shown at first run.

Your photographs are not copied into this folder. You choose which folders
Ninaivu reads in the Control Panel or the console, and they stay where they are.

On a USB stick
  app\data holds the index, the settings, the accounts and the keys that sign
  sessions and share links. On a drive formatted FAT32 or exFAT, as most USB
  sticks are, Windows cannot restrict who reads a file: whoever has the stick
  has all of it. On an NTFS drive Ninaivu makes app\ and logs\ readable by
  your Windows account only, as it does app\data. If the library is private, keep the folder on an NTFS drive
  (your Documents) or on an encrypted stick (BitLocker To Go).

Moving it
  Stop Ninaivu first (press Stop in the Control Panel and wait until it says
  Stopped), then move or copy the whole folder. The index, settings and models
  go with it. A library on another disk must be reachable at the same path, or
  chosen again.

Updating
  Stop Ninaivu, extract the newer zip into a new folder, and move your
  app\data and app\ai-models folders from the old one into the new app folder,
  and the file app\.ninaivu-control\settings.json too (the Control Panel's own
  settings, such as how Ninaivu is started). Or run the Windows
  installer, which keeps its own data.

Running both
  This and an installed Ninaivu use the same ports. Run one at a time.

Windows SmartScreen
  If the release is not signed, Windows says "Windows protected your PC": choose
  More info, then Run anyway.
