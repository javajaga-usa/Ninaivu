# Install

Ninaivu runs on a computer that stays on: a desktop under the stairs, a mini
PC, an old laptop, a Mac mini, a Raspberry Pi. Download the installer for
that computer and run it. It brings everything Ninaivu needs with it, so
nothing else has to be installed first.

## Download

The installers are on the Ninaivu download page,
[github.com/javajaga-usa/Ninaivu/releases/latest](https://github.com/javajaga-usa/Ninaivu/releases/latest).
Pick the file for your computer:

| Computer | File |
| --- | --- |
| Windows | `Ninaivu-<version>-windows-x64.exe` |
| Mac with Apple silicon (M1 and later) | `Ninaivu-<version>-macos-arm64.dmg` |
| Mac with an Intel processor | `Ninaivu-<version>-macos-x86_64.dmg` |
| Linux PC | `Ninaivu-<version>-linux-amd64.sh` |
| Raspberry Pi 4 or 5 (64-bit) | `Ninaivu-<version>-linux-arm64.sh` |

The same page has this guide as a PDF, in English and Tamil.

## Windows

Run `Ninaivu-<version>-windows-x64.exe`. It installs for your account only
(no administrator prompt), puts a private Python and everything Ninaivu needs under
`%LOCALAPPDATA%\Programs\Ninaivu`, and adds **Ninaivu** to the Start menu.
Leave *Start Ninaivu at sign-in* ticked and it is always there. **Ninaivu**
in the Start menu, and **Ninaivu Control Panel** on the Desktop, open the
Control Panel: Start, Stop and Restart, the addresses to open, the computer's
readings and the log. *Ninaivu tray*, in the same Start menu folder, is the
small menu in the system tray for anyone who prefers that.

If the installer is not signed (the release notes say), Windows SmartScreen
says *Windows protected your PC*: choose **More info → Run anyway**.

[More on the Control Panel and the tray.](../desktop-control.md)

## macOS

Open the `.dmg` for your Mac — `arm64` for Apple silicon, `x86_64` for
Intel — and drag **Ninaivu** to Applications.

If the app is not notarised (the release notes say), macOS will not open it
the first time. Try once, then go to **System Settings → Privacy & Security**
and choose **Open Anyway**; after that it opens normally.

Opening **Ninaivu** opens the Control Panel: Start, Stop and Restart, the
addresses to open, the computer's readings and the log, and *Start Ninaivu
when I sign in*. The menu-bar tray is there too, for anyone who prefers it.

## Linux and Raspberry Pi

Use `Ninaivu-<version>-linux-amd64.sh` for a PC, or
`Ninaivu-<version>-linux-arm64.sh` for a Raspberry Pi 4 or 5 running a 64-bit
OS (or any other 64-bit Arm board). Open a terminal in the folder it was
downloaded to and run it:

```bash
sh Ninaivu-<version>-linux-arm64.sh
```

No root is needed, and the machine needs no Python: the file carries its own,
with every package Ninaivu uses, and downloads nothing. It asks once where the
photographs are, installs Ninaivu for your account, puts **Ninaivu Control
Panel** in the applications menu and on the Desktop, sets Ninaivu to start
when the computer starts and keep running after you sign out, starts it, and
prints the address to open.

## Upgrading

Download the newer installer from the same page and install it the same way.
It replaces the old Ninaivu and keeps the library, the settings, the index and
the AI models you downloaded.

!!! note "Other ways to run Ninaivu"
    Docker on a NAS, running it from the source code, and the command-line
    options are for whoever looks after the technical side. They are in the
    [technical administrator guide](../admin-guide.md), in English.

## What you see first

The first screen makes the administrator. On the computer Ninaivu runs on,
that is all, whether you open it as `localhost`, as `ninaivu.local` or by the
computer's own address. From any other device it also asks for the **setup
code**, so nobody else at home can claim a new library first. The code is
printed where Ninaivu started (the Control Panel's log, or the last lines in
the terminal) and kept in `setup-code.txt` in Ninaivu's state folder (the
lines that print the code show its full path) until the administrator exists; it stays the
same across restarts. After that the console
walks you through [the first day](first-day.md).

!!! tip "Two addresses"
    Ninaivu has two faces on two ports: the **family app**, where everyone
    looks at the library, and the **console**, where the administrator runs
    it. The console answers on this computer only until you open it to the
    home network on the Server page; the family app is on the home network.
