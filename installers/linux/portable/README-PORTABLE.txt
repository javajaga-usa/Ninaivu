Ninaivu, portable, for Linux
============================

The ninaivu script, the app folder and the logs folder are all there is.
Nothing is installed: the folder carries its own Python and every package,
so the computer needs none, and deleting the folder removes everything it
made. For a 64-bit PC (x86-64) running Ubuntu, Debian or another current
distribution.

To start
  1. Extract the archive somewhere you can write to (your home folder, or a
     USB drive):   tar -xzf Ninaivu-*-linux-x64-portable.tar.gz
  2. Run ./ninaivu in the folder it makes. The Ninaivu Control Panel opens;
     press Start.

  Without a desktop (a server, or over ssh):
     ./ninaivu --server ~/Pictures
  runs Ninaivu in that terminal with ~/Pictures as the library.

Where things are
  ninaivu         opens the Control Panel (--tray for the tray menu,
                  --server for the server alone, --where to see the folders).
  app/            the program, its private Python, and what Ninaivu keeps:
                  app/data (the index of your library, settings, people, thumbnails)
                  app/ai-models (the AI models, once you download them).
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
  app/data holds the index, the settings, the accounts and the keys that sign
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
  Stop Ninaivu, extract the newer archive into a new folder, and move your
  app/data and app/ai-models folders from the old one into the new app
  folder, and app/.ninaivu-control/settings.json too (the Control Panel's own
  settings).

Running two
  Two copies use the same ports. Run one at a time.
