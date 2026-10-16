Ninaivu, portable, for the Mac
==============================

Ninaivu.app and this folder are all there is. Nothing is installed: the app
carries its own Python and every package, so the Mac needs none, and
deleting the folder removes everything it made. For a Mac with Apple
silicon (M1 or later) on macOS 12 or newer.

To start
  1. Double-click the zip. Move the Ninaivu folder it makes somewhere you
     can write to: your Documents, or a drive.
  2. Open Ninaivu.app in that folder. The Ninaivu Control Panel opens; press
     Start.

  If the release is not signed, macOS refuses it at first: open it once,
  then go to System Settings > Privacy & Security and choose Open Anyway.

  macOS first runs a downloaded app from a hidden copy, where Ninaivu cannot
  see this folder; Ninaivu notices, tells macOS the folder is wanted, and
  opens itself again from here. If it says instead that macOS opened it from
  a temporary copy: open Terminal, type
      xattr -dr com.apple.quarantine 
  (with the space at the end), drag the Ninaivu folder onto the Terminal
  window, press Return, and open Ninaivu.app again. macOS runs a downloaded
  app from a hidden copy until it is told the folder is wanted, and from
  there Ninaivu cannot see the folder its data lives in.

Where things are
  Ninaivu.app     opens the Control Panel.
  data/           what Ninaivu keeps, made the first time it opens: the
                  index of your library, settings, people, thumbnails;
                  data/ai-models (the AI models, once you download them);
                  data/control (the Control Panel's own settings).
                  Do not edit what is in here.
  logs/           ninaivu.log and server.log: what Ninaivu did, for when
                  somebody asks what went wrong. Look one over before you
                  send it: it names the addresses Ninaivu answered on and
                  the setup code shown at first run.
  .ninaivu-portable   a hidden file that tells Ninaivu.app to keep its data
                  in this folder. Leave it where it is.

Your photographs are not copied into this folder. You choose which folders
Ninaivu reads in the Control Panel or the console, and they stay where they are.

Ninaivu.app on its own
  Dragged out of this folder on its own (into Applications), Ninaivu.app works
  as an ordinary app: it keeps its data in ~/.ninaivu and
  ~/Library/Application Support/Ninaivu instead, and does not see this folder's.

On a drive
  data/ holds the index, the settings, the accounts and the keys that sign
  sessions and share links. On a drive formatted FAT32 or exFAT nothing can
  restrict who reads a file: whoever has the drive has all of it. Keep the
  folder on an APFS disk (your Documents) or an encrypted one if the library
  is private.

Moving it
  Stop Ninaivu first (press Stop in the Control Panel and wait until it says
  Stopped), then move or copy the whole folder. The index, settings and models
  go with it. A library on another disk must be reachable at the same path, or
  chosen again.

Updating
  Stop Ninaivu, unzip the newer version, and move your data folder from the
  old Ninaivu folder into the new one (replacing the empty one, if there is
  one). Then open the new Ninaivu.app.

Running two
  Two copies use the same ports. Run one at a time.
