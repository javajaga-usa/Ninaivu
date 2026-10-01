# The Control Panel and the tray

Ninaivu has two ways to be looked after on the computer it runs on, and they
work together: the **Control Panel**, a window of its own, and a small
**tray** icon. Both start and stop the same server, and both find a server
that was started another way, such as `launcher/start.py`, a service, or the
other one.

**The installers open the Control Panel.** On Windows it is the Start menu
entry and a shortcut on the Desktop; on a Mac it is the Ninaivu app in
Applications; on Linux and a Raspberry Pi it is *Ninaivu Control Panel* in
the applications menu and on the Desktop. It is the one thing the person who
runs the house needs: Start, Stop, Restart, the addresses, the readings and
the log. The tray is there beside it for anyone who wants a menu in the
corner — *Ninaivu tray* in the Start menu folder, `open -a Ninaivu --args
--tray` on a Mac, `ninaivu-tray` on Linux.

## The tray

Ninaivu sits in the system tray on Windows and the menu bar on a Mac:

```
.venv\Scripts\pythonw -m ninaivu.desktop.tray      # Windows
.venv/bin/python -m ninaivu.desktop.tray            # macOS, Linux
```

From a checkout it needs `requirements/requirements-desktop.txt` (pystray and
Pillow). The Windows and macOS installers (`installers/`) bring everything
and open it from the Start menu or Applications; **Start at sign-in** in its
own menu keeps it there.

One icon, one menu: whether Ninaivu is running; **Open the family app** and
**Open the console**; **Start**, **Stop** and **Restart**; **Check for an
update**; **View the log**; **Trust the HTTPS certificate**; **Start at
sign-in**; **Quit the tray**. Every action reports in a notification. Stop
asks the server to finish properly through its own authenticated shutdown
and never force-kills it; Quit leaves the server running, since the icon was
never what kept it up.

## The Control Panel

**Windows:** double-click **Start - Ninaivu Control Panel.vbs** in the
Ninaivu folder. It runs the panel with the folder's `.venv`, so run
`start.cmd` once first; if the `.venv` is missing it says so.

**macOS:** run **Setup Ninaivu.command** once. It builds **Ninaivu Control
Panel** beside the Ninaivu app in your Applications folder, a copy in the
Ninaivu folder, and one in the main Applications folder when your account may
write there; open it from any of them, Launchpad or Spotlight. On a Mac set up
before the Control Panel existed, or after moving the Ninaivu folder,
`"./Setup Ninaivu.command" --apps-only` builds both apps again without
reinstalling anything. They are built on the Mac rather than shipped because
macOS will not open an unsigned app that arrived as a download.

From a terminal, on any system: `python -m ninaivu.desktop.app` with the
`.venv`'s Python.

The window needs no terminal and starts independently of the server. It is
the same panel on both systems. A server it starts runs in the background,
and on a Mac in a session of its own, so closing the panel leaves Ninaivu
running.

What it shows:

- **Start**, **Stop** and **Restart**, **Open the family app** and **Open the
  console**, and the addresses Ninaivu answers on. Stop requests a graceful
  shutdown with the run file's token and never force-kills a busy server;
  the panel shows **Stopping…** until the process has let go of the state
  folder, then **Stopped**.
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
- **Trust the HTTPS certificate** and **Start Ninaivu when I sign in**, which
  do exactly what the tray's items of the same name do (below).

The console's **Server** page has the readings, the mode and the log too; the
panel is for the times the console is not open, or not running.

The panel is drawn with Tk. The Windows installer and python.org's Python
include it. Homebrew's Python does not: `brew install python-tk@3.12` adds it
(Setup Ninaivu does this when it installs Python with Homebrew itself). It
also needs `psutil`, which the setup installs; without it the panel could not
tell whether Ninaivu is running, so it says what to install instead of
opening.

## HTTPS

New desktop installations use HTTPS on port 443. The library address is **https://ninaivu.local** and the admin console is **https://ninaivu-admin.local**; neither needs a port number. Both names share the HTTPS listener, with the admin app's existing sign-in checks. A separately restricted admin bind remains restricted. Custom ports and disabled network discovery keep their explicit configuration. On the server computer, **https://localhost** is a library fallback if the network name does not resolve.

On a Mac, **Trust the HTTPS certificate** asks first, then trusts Ninaivu's certificate for websites in the login keychain. macOS asks for your password in its own window. The tray or panel then checks that this Ninaivu's certificate verifies, and says so. It does not go through Keychain Access's import, which is where it went wrong: that offers the iCloud or Local Items keychain, which take no certificates, and refuses a certificate that is already there. Both say "unable to import", and neither marks it trusted. If trusting fails, the file is shown in Finder with the one step to do by hand: in Keychain Access, open "Ninaivu local CA" in the login keychain and set **When using this certificate** to **Always Trust**. If another certificate called "Ninaivu local CA" is in the keychains, from another Ninaivu or an earlier setup, it says so; it does nothing for this Ninaivu and can be deleted.

On Windows, **Trust the HTTPS certificate** adds the certificate to the current user's Trusted Root store after asking, and if that fails opens this installation's public CA certificate in the Windows certificate viewer. Trust it for your user account only if it is your own Ninaivu installation. Other devices must also trust this CA. Network access requires an appropriate private-network HTTPS firewall rule; neither the tray nor the panel silently changes Windows trust or firewall settings.
