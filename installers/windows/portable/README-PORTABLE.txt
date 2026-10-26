Ninaivu, portable
=================

Ninaivu.exe, the app folder, the data folder and the logs folder are all there is. Nothing is
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
    of data\control.

To start
  1. Extract the whole zip somewhere you can write to (your Documents, or a
     USB drive). Right-click the zip and choose Extract All.
  2. Open Ninaivu.exe in the folder it makes. The Ninaivu Control Panel opens;
     press Start.

Where things are
  Ninaivu.exe     opens the Control Panel.
  app\            the program and its private Python.
  data\           what Ninaivu keeps, made the first time it opens: the
                  index of your library, settings, people, thumbnails;
                  data\ai-models (the AI models, once you download them);
                  data\control (the Control Panel's own settings).
                  Do not edit what is in here.
  logs\           ninaivu.log and server.log: what Ninaivu did, for when
                  somebody asks what went wrong. Look one over before you
                  send it: it names the addresses Ninaivu answered on and
                  the setup code shown at first run.

Your photographs are not copied into this folder. You choose which folders
Ninaivu reads in the Control Panel or the console, and they stay where they are.

On a USB stick
  data\ holds the index, the settings, the accounts and the keys that sign
  sessions and share links. On a drive formatted FAT32 or exFAT, as most USB
  sticks are, Windows cannot restrict who reads a file: whoever has the stick
  has all of it. On an NTFS drive Ninaivu makes app\ and logs\ readable by
  your Windows account only, as it does data\. If the library is private, keep the folder on an NTFS drive
  (your Documents) or on an encrypted stick (BitLocker To Go).

Moving it
  Stop Ninaivu first (press Stop in the Control Panel and wait until it says
  Stopped), then move or copy the whole folder. The index, settings and models
  go with it. A library on another disk must be reachable at the same path, or
  chosen again.

Updating
  Extract the newer zip beside this folder, on the same drive (for example
  Documents\Ninaivu-3.3.0 next to Documents\Ninaivu-3.2.0), and open its
  Ninaivu.exe. Its Control Panel finds this folder and offers to bring the
  library across: it stops this Ninaivu if it is running and moves data\
  (with the models and the Control Panel's settings) into the new folder, in
  a moment, since nothing is copied. This folder keeps its program and a note,
  LIBRARY-MOVED.txt, saying where the library went.

  The first start of the newer version copies the index before it changes
  anything. Until you delete the old folder, "Go back to ..." in the newer
  Control Panel moves the library back to it as the old version left it.

  A folder from Ninaivu 1.11 or earlier kept the library in app\data; the
  newer Ninaivu finds it there too.

Running two
  Two copies, or this and a Ninaivu installed by an earlier version, use the
  same ports. Run one at a time.

Windows SmartScreen
  If the release is not signed, Windows says "Windows protected your PC": choose
  More info, then Run anyway.
