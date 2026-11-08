Ninaivu, portable, for Linux
============================

ninaivu and app/ carry the program and its own Python and packages, so the computer needs none. The default library data
lives in ~/.ninaivu in your home folder and stays when the program is deleted
or replaced. For a 64-bit PC (x86-64) running Debian 12 or 13, Ubuntu or
another current distribution. There is no download for arm64 computers such
as a Raspberry Pi.

To start
  1. Extract the archive somewhere you can write to (your home folder, or a
     USB drive):   tar -xzf Ninaivu-*-linux-*-portable.tar.gz
  2. Double-click "ninaivu" in the folder it makes (or run ./ninaivu in a
     terminal). The Ninaivu Control Panel opens and starts Ninaivu, and its
     icon goes in the tray. Closing the window puts it there; Ninaivu keeps
     running. Opening ninaivu again brings the window back.
  3. For one click from the application menu and the Desktop from then on:
        ./ninaivu --add-to-menu
     (./ninaivu --remove-from-menu takes them out; after moving the folder,
     run --add-to-menu again).

  Without a desktop (a server, or over ssh):
     ./ninaivu --server ~/Pictures
  runs Ninaivu in that terminal with ~/Pictures as the library.

Where things are
  ninaivu         the one thing to open: the Control Panel, its tray icon,
                  and Ninaivu started (--tray opens it minimised, --server
                  runs the server alone, --where shows the folders).
  app/            the program and its private Python (app/ninaivu.sh is
                  what ninaivu runs).
  ~/.ninaivu/     what Ninaivu keeps, made the first time it opens: the
                  index of your library, settings, people, thumbnails;
                  ~/.ninaivu/ai-models (the AI models, once you download them);
                  ~/.ninaivu/control (the Control Panel's own settings).
                  Do not edit what is in here.
  logs/           ninaivu.log and server.log: what Ninaivu did, for when
                  somebody asks what went wrong. Look one over before you
                  send it: it names the addresses Ninaivu answered on and
                  the setup code shown at first run.

Your photographs are not copied into this folder. You choose which folders
Ninaivu reads in the Control Panel or the console, and they stay where they are.

Starting at sign-in
  Turn on "Start Ninaivu when I sign in" in the Control Panel. The Linux
  desktop starts this portable launcher with the same index and settings.
  Stop stays stopped; this does not continuously restart a stopped server.

Starting it at boot
  The portable build does not set itself up as a service. To have systemd
  start it, see "The Linux system service" in the technical administrator
  guide, and point ExecStart at this folder's ninaivu --server.

On a USB stick
  The default index stays in this computer's ~/.ninaivu even when the
  program is carried on a USB stick. A relocated or older app-local data
  folder holds the accounts and keys too. On FAT32 or exFAT nothing can
  restrict who reads a file: whoever has the stick has all of it. Keep the
  folder on an ext4 disk (your home folder) or an encrypted one if the
  library is private.

Moving it
  Stop Ninaivu first (press Stop in the Control Panel and wait until it says
  Stopped), then move or copy the program folder. The default index, settings and
  models stay in ~/.ninaivu; move those separately when changing computers. A library on another disk must be reachable at the same path, or
  chosen again.

Updating
  Extract the newer archive beside this folder, on the same drive (for
  example ~/Ninaivu-3.5.1 next to ~/Ninaivu-3.4.0), and run its ./ninaivu.
  Its Control Panel finds the active program and offers to take over the
  library. It stops the older Ninaivu first. The home-folder or relocated
  index stays in place; an existing app-local library is moved into the new
  program folder. The older folder keeps its program and a note,
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

Relocated indexes and updates
  An index moved through the console stays in its chosen folder during an
  update. The new portable program follows that location instead of using
  the recovery copy left in the old folder. Go back restores the older
  index there, keeps a backup of the newer work, and makes the newer
  program inactive until you bring the library across again. Keep the
  older program and the forwarding files until you no longer need Go back.
  Earlier portable libraries in data/ or app/data are still found.
