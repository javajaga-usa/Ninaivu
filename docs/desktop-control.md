# The Control Panel and the tray

Ninaivu is one program on the computer it runs on: the **Control Panel**, a
window of its own, with Ninaivu's icon in the **tray** (the menu bar on a
Mac). The icon is the window minimised, not a second program. The panel
starts and stops the server, and finds a server that was started another way,
such as `launcher/start.py`, a service or a start at sign-in.

**Each portable download has one thing to open: Ninaivu.** On Windows it is
`Ninaivu.exe` in the folder; on a Mac, `Ninaivu.app`; on Linux, `ninaivu`
(double-click it, or run `./ninaivu`). Opening it opens the Control Panel,
puts the icon in the tray and starts Ninaivu. Closing the window puts it in
the tray, and Ninaivu keeps running; a click on the icon, or opening Ninaivu
again, brings the window back. **Quit** in the icon's menu ends the window
and the icon, and still leaves Ninaivu running: **Stop** is what stops it.
`--tray` (`open -a Ninaivu --args --tray` on a Mac, `./ninaivu --tray` on
Linux) opens it minimised.

## Shortcuts on the Desktop

The first time the Control Panel opens it puts three shortcuts on the
Desktop, each with its own icon:

- **Ninaivu**: the family app, in the browser (blue);
- **Ninaivu Console**: the console, in the browser (violet);
- **Ninaivu Control Panel**: this window (dark, with a power sign).

The first two start Ninaivu when it is stopped and open the page once it
answers, with the Control Panel waiting in the tray; when Ninaivu is open
already they only open the page. So a shortcut never opens a page that does
not load, and still works when the port changes. They open Ninaivu itself
(`Ninaivu.exe`, `Ninaivu.app`, `ninaivu`, or a checkout's Control Panel)
with `--open family` or `--open console`, which works from a terminal too.

It happens once. A shortcut you delete is not put back. The ones still there
follow the copy of Ninaivu that last opened: a newer portable copy that
brought the library across takes them over. A file of your own on the Desktop
under one of these names is left alone. On a Mac, macOS asks once whether
Ninaivu may use the Desktop folder; saying no leaves the Desktop as it is.

```
python -m ninaivu.desktop.shortcuts --add       # put back any that are missing
python -m ninaivu.desktop.shortcuts --remove    # take away the ones Ninaivu put there
```

From a portable folder these run with its Python: on a Mac
`Ninaivu.app/Contents/Resources/python/bin/python3 -m …`, on Linux
`app/python/bin/python3 -m …`, on Windows `app\python\python.exe -m …`.

## The tray

The icon's menu is short, because everything else is one click away in the
window: whether Ninaivu is running; **Show Ninaivu** (what clicking the icon
does); **Open the family app**; **Start** or **Stop**, whichever can be
pressed; and **Quit**. Stop asks the server to finish properly through its own
authenticated shutdown and never force-kills it. Something begun from the
icon while the window is hidden answers in a notification.

How each system holds the icon beside the window, in one program:

- **Windows**: the icon has its own message loop, on a thread of its own.
- **macOS**: the window and the icon share the one event loop macOS allows,
  on the main thread. The Dock's icon also brings the window back, and Quit
  in the Dock or ⌘Q is the panel's Quit.
- **Linux**: the X11 tray (XEmbed), which Cinnamon, MATE, Xfce and KDE have.
  A click on the icon shows the window, where everything is. GNOME has no
  such tray without an extension: there is no icon, and closing the window
  closes it, as before; Ninaivu keeps running, and opening Ninaivu again
  brings the window back.

From a checkout it needs `requirements/requirements-desktop.txt` (pystray and
Pillow). Without Tk, `python -m ninaivu.desktop.app` opens the icon alone
with a fuller menu instead (Start, Stop, Restart, the log, the certificate,
Start at sign-in).

## The Control Panel

The portable downloads open it (above). From a checkout, after
`pip install -r requirements/requirements-desktop.txt` into its environment:

```
python -m ninaivu.desktop.app            # any system
```

`launcher\Start - Ninaivu Control Panel.vbs` (Windows) and
`launcher/Start - Ninaivu Control Panel.sh` (Linux) open it without a
terminal once `launcher\start.cmd` or `sh launcher/start.sh` has made the
checkout's `.venv`; `sh launcher/start.sh --add-to-menu` puts it in a Linux
desktop's application menu.

**If you used Setup Ninaivu.command or Setup Ninaivu.sh.** They are gone in
4.0. On a Mac or a Linux PC, use the portable download, whose `Ninaivu.app`
or `ninaivu` is the one thing to open; it offers to bring your library across
from the folder beside it. From a checkout, `python -m ninaivu` starts the
server and `python -m ninaivu.desktop.app` the Control Panel. The apps
Setup Ninaivu.command made (**Ninaivu** and **Ninaivu Control Panel** in
Applications) still point at the checkout's old root files and no longer
open: delete them, and on Linux run `sh launcher/start.sh --remove-from-menu`
and then `--add-to-menu` to replace the menu entries.

New Linux installations keep the index and settings in **`~/.ninaivu/` in your
home folder**. Existing app-local libraries or earlier user data folders
continue to be used, and `NINAIVU_STATE_DIR` still overrides the default.
The Control Panel and terminal launcher use the same location. Move an existing
index from **Migration** in the administrator console when you want to change
that location. The Linux portable build also defaults to `~/.ninaivu/`, and keeps
using an existing `data/` library or its forwarding note.

**Start**, **Stop**, **Restart**, resource modes and logs work through the same
controller as on the Mac. **Start Ninaivu when I sign in** is available on Linux
through a user XDG autostart entry; stopping Ninaivu leaves it stopped until the
next sign-in. A different portable copy cannot disable your selected copy's
sign-in entry. If Python has no Tk, install it on Mint/Ubuntu/Debian with
`sudo apt install python3-tk`. The portable build includes its own Tk.

The window needs no terminal. It is the same panel on every system. A
server it starts runs in the background, and on a Mac in a session of its
own, so closing the panel, or quitting it, leaves Ninaivu running.

What it shows:

- **Start**, **Stop** and **Restart**, **Open the family app** and **Open the
  console**, and the addresses Ninaivu answers on. The Open buttons open
  `https://127.0.0.1` on this computer (`ninaivu.local` is for the other
  devices, and Windows does not always find it), and start Ninaivu first
  when it is stopped. Stop requests a graceful
  shutdown with the run file's token and never force-kills a busy server;
  the panel shows **Stopping…** until the process has let go of the state
  folder, then **Stopped**.
- **A progress card while Ninaivu is not serving yet**, under the buttons:
  what is holding the start up, how far it has got with a bar and a
  percentage, how long it has been, and the time left once it can be judged.
  It appears for a start or restart begun anywhere (here, the console's
  Restart, the tray, a start at sign-in) and covers the slow steps: the old
  server finishing what it has in hand during a restart, an index moved or
  switched to another folder, one brought in from another computer, the copy
  taken before an update, and a check or repair of the index. A step that
  cannot say how far it has got shows a bar that moves without filling. The
  card goes when Ninaivu is serving.

  A start that has not moved on (the same step at the same figure) for 15
  minutes is called stuck: the card says so in amber, the panel stops
  waiting on it, and **Stop this start…** ends it, after saying what that
  does at its step. Every step before Ninaivu serves can be stopped safely:
  a move or switch of the index leaves it where it was and is tried again at
  the next start, a check is done again, a repair keeps the original beside
  it, the copy before an update is made again. While an index from another
  computer is being put in place, Stop is offered only once it is stuck.
  Only a Ninaivu process is ended: asked first, then by force after 20
  seconds. A process waiting on a disk that does not answer cannot end until
  the disk does, and the panel says so.
- **The window always answers and always closes.** It only draws: the
  readings, the run file, the log and the panel's actions are read and done
  on threads of their own, so a drive that is slow to wake (a USB disk with
  the index) slows a reading, not the window. Closing it while Ninaivu
  starts or stops leaves that going on; closing it half-way through a
  restart hands the rest to the restart helper, so Ninaivu is not left
  stopped. Only *Go back*, which moves the library's files, is waited for.
- The whole computer's CPU and memory, Ninaivu's own CPU, memory and threads,
  the battery, free disk space, and a minute of CPU history. Battery power is
  the measured whole-device discharge rate, where the battery reports an
  absolute wattage (Windows); on AC power or other hardware it shows
  unavailable rather than inventing a per-app estimate. Ollama, when used,
  is a separate process; its use shows in the computer's totals.
- The **resource mode**. Standard is the default; Performance allows more
  scanner workers, AI threads and request threads; Power-saving uses fewer
  and, on Windows, asks for execution-speed throttling. Each mode shows what
  it gives this computer. **Apply mode** gracefully restarts a running server
  so the new limits take effect, keeping its other arguments, ports included.
  Modes change neither image quality nor the system power plan.
- **View logs** opens a live log pane below the dashboard. Drag the divider to
  resize it, choose **Server** or **Local AI**, pause updates, turn off
  auto-scroll to read earlier output, or find text. **Clear view** clears
  only the display, never the log file. The pane reads at most 64 KB per
  update and shows at most 2,000 lines or 200 KB; warnings and errors are
  coloured.
- **Trust the HTTPS certificate** (shown only while Ninaivu runs on HTTPS) and
  **Start Ninaivu when I sign in** (below).

The console's **Server** page has the readings, the mode and the log too; the
panel is for the times the console is not open, or not running.

The panel is drawn with Tk. The portable downloads and python.org's Python
include it. Homebrew's Python does not: `brew install python-tk@3.12` adds it.
It also needs `psutil` (in `requirements-desktop.txt`); without it the panel could not
tell whether Ninaivu is running, so it says what to install instead of
opening.

## HTTPS

New desktop installations use HTTPS on port 443. The library address is **https://ninaivu.local** and the admin console is **https://ninaivu-admin.local**; neither needs a port number. Both names share the HTTPS listener, with the admin app's existing sign-in checks. A separately restricted admin bind remains restricted. Custom ports and disabled network discovery keep their explicit configuration. On the server computer, **https://localhost** is a library fallback if the network name does not resolve.

On a Mac, **Trust the HTTPS certificate** asks first, then trusts Ninaivu's certificate for websites in the login keychain. macOS asks for your password in its own window. The tray or panel then checks that this Ninaivu's certificate verifies, and says so. It does not go through Keychain Access's import, which is where it went wrong: that offers the iCloud or Local Items keychain, which take no certificates, and refuses a certificate that is already there. Both say "unable to import", and neither marks it trusted. If trusting fails, the file is shown in Finder with the one step to do by hand: in Keychain Access, open "Ninaivu local CA" in the login keychain and set **When using this certificate** to **Always Trust**. If another certificate called "Ninaivu local CA" is in the keychains, from another Ninaivu or an earlier setup, it says so; it does nothing for this Ninaivu and can be deleted.

On Windows, **Trust the HTTPS certificate** adds the certificate to the current user's Trusted Root store after asking, and if that fails opens this installation's public CA certificate in the Windows certificate viewer. Trust it for your user account only if it is your own Ninaivu installation. Other devices must also trust this CA. Network access requires an appropriate private-network HTTPS firewall rule; neither the tray nor the panel silently changes Windows trust or firewall settings.
