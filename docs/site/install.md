# Install

Ninaivu runs on a computer that stays on: a desktop under the stairs, a mini
PC, an old laptop, a Mac mini, a NAS that runs Docker. It needs Python 3.12
or newer when run from a checkout; the installers bring their own.

## Windows

Download `Ninaivu-<version>-windows-x64.exe` from the
[latest release](https://github.com/javajaga-usa/Ninaivu/releases/latest) and
run it. It installs for your account only (no administrator prompt), puts a
private Python and everything Ninaivu needs under
`%LOCALAPPDATA%\Programs\Ninaivu`, and adds **Ninaivu** to the Start menu.
Leave *Start Ninaivu at sign-in* ticked and it is always there.

If the installer is not signed (the release notes say), Windows SmartScreen
says *Windows protected your PC*: choose **More info → Run anyway**. Once
Ninaivu is in the winget catalogue, `winget install Ninaivu.Ninaivu` will
work too.

Opening **Ninaivu** puts an icon in the system tray. Its menu starts and stops
the server and opens the family app and the console. [More on the tray.](../desktop-control.md)

## macOS

Download the `.dmg` for your Mac — `arm64` for Apple silicon, `x86_64` for
Intel — from the [latest release](https://github.com/javajaga-usa/Ninaivu/releases/latest),
open it and drag **Ninaivu** to Applications.

If the app is not notarised (the release notes say), macOS will not open it
the first time. Try once, then go to **System Settings → Privacy & Security**
and choose **Open Anyway**; after that it opens normally. Once Ninaivu is in
Homebrew, `brew install --cask ninaivu` will work too.

Ninaivu lives in the menu bar, not the Dock. Its menu starts and stops the
server, opens the family app and the console, and can start at sign-in.

## Docker

For a NAS or a Linux box:

```bash
git clone https://github.com/javajaga-usa/Ninaivu.git
cd Ninaivu
cp installers/docker/.env.example installers/docker/.env   # set MEDIA_DIR to your photo folder
docker compose -f installers/docker/docker-compose.yml up -d
```

The family app answers on port 5000 to the whole network; the console on
3000, published on the NAS itself only (`127.0.0.1:3000`). To make the
administrator from another computer, open the console through an SSH tunnel
or on the NAS, and give the **setup code** printed in `docker logs ninaivu`.
The `.env` file is only for folders and ports: everything about how Ninaivu
behaves is chosen in the console, and nothing in `.env` undoes it at the next
restart. The image leaves out search by description unless built with
`--build-arg WITH_AI=1`. The [operations guide](../operations/production.md)
covers systemd, reverse proxies, HTTPS and large libraries.

## From a checkout

```bash
git clone https://github.com/javajaga-usa/Ninaivu.git
cd Ninaivu
start.cmd ~/Pictures                 # Windows: or double-click start.cmd
sh launcher/start.sh ~/Pictures      # macOS and Linux
```

The launcher makes a virtual environment, installs what is missing (nothing
large: search by description is added later, if you want it), serves HTTPS
on port 443 — or 8080 on Linux, where an ordinary user may not use 443 —
finds free ports and opens your browser. `python -m ninaivu <folder>` runs
the server directly, on plain HTTP and port 80 (8080 without the right to
use 80), unless given `--https`.

On Windows, `start.cmd` needs no Python beforehand: when it finds no Python
3.12 or newer, it installs Python 3.12 for the current user first (with
winget, or from python.org when winget is missing), then carries on. No
administrator rights are needed.

On a Mac, double-click **Setup Ninaivu.command** in the Ninaivu folder once
instead of using a terminal. It clears the download quarantine, installs
Python 3.12 if the Mac has no Python 3.12 or newer (after asking: with
Homebrew when the Mac has it, otherwise the installer from python.org, which
is run only when it carries the Python Software Foundation's signature, and
for which macOS asks your password), installs what Ninaivu needs, and puts
two apps in Applications: **Ninaivu**, which starts the library in a Terminal
window and asks once which folder holds your photos, and the **Ninaivu
Control Panel**, which starts and stops it without one. `sh
launcher/start.sh` offers the same Python install. On Windows, **Start -
Ninaivu Control Panel.vbs** opens the Control Panel once `start.cmd` has run.
[More on the Control Panel.](../desktop-control.md)

## What you see first

The first screen makes the administrator. On the computer Ninaivu runs on,
that is all; from any other device it also asks for the **setup code**
printed where Ninaivu started (the terminal, the log, or `docker logs`), so
nobody else at home can claim a new library first. After that the console
walks you through [the first day](first-day.md).

!!! tip "Two addresses"
    Ninaivu has two faces on two ports: the **family app**, where everyone
    looks at the library, and the **console**, where the administrator runs
    it. The console answers on this computer only until you open it to the
    home network on the Server page; the family app is on the home network.
