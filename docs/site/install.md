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

Or, once the manifest is merged: `winget install Ninaivu.Ninaivu`.

Opening **Ninaivu** puts an icon in the system tray. Its menu starts and stops
the server and opens the family app and the console. [More on the tray.](../desktop-control.md)

## macOS

Download the `.dmg` for your Mac — `arm64` for Apple silicon, `x86_64` for
Intel — from the [latest release](https://github.com/javajaga-usa/Ninaivu/releases/latest),
open it and drag **Ninaivu** to Applications. Or `brew install --cask ninaivu`.

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

The family app answers on port 5000 and the console on 3000. The
[operations guide](../operations/production.md) covers systemd, reverse
proxies, HTTPS and large libraries.

## From a checkout

```bash
git clone https://github.com/javajaga-usa/Ninaivu.git
cd Ninaivu
start.cmd ~/Pictures                 # Windows: or double-click start.cmd
sh launcher/start.sh ~/Pictures      # macOS and Linux
```

The launcher makes a virtual environment, installs what is missing, finds
free ports and opens your browser.

## What you see first

The first screen makes the administrator. After that the console walks you
through [the first day](first-day.md).

!!! tip "Two addresses"
    Ninaivu has two faces on two ports: the **family app**, where everyone
    looks at the library, and the **console**, where the administrator runs
    it. The console is bound to the computer itself by default; the family
    app is on the home network.
