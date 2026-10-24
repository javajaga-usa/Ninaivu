Ninaivu, portable, for Linux
============================

Open Ninaivu, the ninaivu script, the app folder, the data folder and the
logs folder are all there is. Nothing is installed: the folder carries its own Python and
every package, so the computer needs none, and deleting the folder removes
everything it made. For a 64-bit PC (x86-64, the linux-x64 download) or an
arm64 computer (a Raspberry Pi 4 or 5 with a 64-bit system, the linux-arm64
download) running Ubuntu, Debian or another current distribution.

The arm64 download carries no ffmpeg of its own yet. Install the system's
(sudo apt install ffmpeg) so videos get their posters and play.

To start
  1. Extract the archive somewhere you can write to (your home folder, or a
     USB drive):   tar -xzf Ninaivu-*-linux-*-portable.tar.gz
  2. Double-click "Open Ninaivu" in the folder it makes (or run ./ninaivu in
     a terminal). The Ninaivu Control Panel opens; press Start.
  3. For one click from the application menu and the Desktop from then on:
        ./ninaivu --add-to-menu
     (./ninaivu --remove-from-menu takes them out; after moving the folder,
     run --add-to-menu again).

  Without a desktop (a server, or over ssh):
     ./ninaivu --server ~/Pictures
  runs Ninaivu in that terminal with ~/Pictures as the library.

Where things are
  Open Ninaivu    the double-click: runs ninaivu, below.
  ninaivu         opens the Control Panel (--tray for the tray menu,
                  --server for the server alone, --where to see the folders).
  app/            the program and its private Python.
  data/           what Ninaivu keeps, made the first time it opens: the
                  index of your library, settings, people, thumbnails;
                  data/ai-models (the AI models, once you download them);
                  data/control (the Control Panel's own settings).
                  Do not edit what is in here.
  logs/           ninaivu.log and server.log: what Ninaivu did, for when
                  somebody asks what went wrong. Look one over before you
                  send it: it names the addresses Ninaivu answered on and
                  the setup code shown at first run.

Your photographs are not copied into this folder. You choose which folders
Ninaivu reads in the Control Panel or the console, and they stay where they are.

Starting it at boot
  The portable build does not set itself up as a service. To have systemd
  start it, see "The Linux system service" in the technical administrator
  guide, and point ExecStart at this folder's ninaivu --server.

On a USB stick
  data/ holds the index, the settings, the accounts and the keys that sign
  sessions and share links. On a stick formatted FAT32 or exFAT nothing can
  restrict who reads a file: whoever has the stick has all of it. Keep the
  folder on an ext4 disk (your home folder) or an encrypted one if the
  library is private.

Moving it
  Stop Ninaivu first (press Stop in the Control Panel and wait until it says
  Stopped), then move or copy the whole folder. The index, settings and models
  go with it. A library on another disk must be reachable at the same path, or
  chosen again.

Updating
  Extract the newer archive beside this folder, on the same drive (for
  example ~/Ninaivu-2.2.0 next to ~/Ninaivu-2.1.0), and run its ./ninaivu.
  Its Control Panel finds this folder and offers to bring the library across: it stops this Ninaivu if it is running and moves data/
  (with the models and the Control Panel's settings) into the new folder, in
  a moment, since nothing is copied. This folder keeps its program and a note,
  LIBRARY-MOVED.txt, saying where the library went.

  The first start of the newer version copies the index before it changes
  anything. Until you delete the old folder, "Go back to ..." in the newer
  Control Panel moves the library back to it as the old version left it.

  A folder from Ninaivu 1.11 or earlier kept the library in app/data; the
  newer Ninaivu finds it there too.

  Without a desktop (over ssh, say), the same two steps from the newer
  folder:  ./ninaivu --bring ~/Ninaivu-1.11.0   and   ./ninaivu --go-back

Running two
  Two copies use the same ports. Run one at a time.
