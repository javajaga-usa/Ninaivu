Ninaivu, portable
=================

Ninaivu.exe, the app folder and the logs folder are all there is. Nothing is
installed and deleting the folder removes everything it made. The one thing
that goes outside it is "Start when I sign in", if you turn it on: it adds one
value to your sign-in list in the registry. Turn it off in the Control Panel
before you delete the folder.

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

Moving it
  Stop Ninaivu first (press Stop in the Control Panel and wait until it says
  Stopped), then move or copy the whole folder. The index, settings and models
  go with it. A library on another disk must be reachable at the same path, or
  chosen again.

Updating
  Stop Ninaivu, extract the newer zip into a new folder, and move your
  app\data and app\ai-models folders from the old one into the new app folder.
  Or run the Windows installer, which keeps its own data.

Running both
  This and an installed Ninaivu use the same ports. Run one at a time.

Windows SmartScreen
  If the release is not signed, Windows says "Windows protected your PC": choose
  More info, then Run anyway.
