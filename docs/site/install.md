# Install

Ninaivu runs on a computer that stays on: a desktop under the stairs, a mini
PC, an old laptop, a Mac mini. Nothing is installed: Ninaivu comes as one
folder that brings everything it needs with it — its own Python and every
package — so nothing else has to be installed first, and deleting the folder
removes everything it made.

## Download

The downloads are on the Ninaivu download page,
[github.com/javajaga-usa/Ninaivu-releases/releases/latest](https://github.com/javajaga-usa/Ninaivu-releases/releases/latest).
Pick the file for your computer:

| Computer | File |
| --- | --- |
| Windows (64-bit) | `Ninaivu-<version>-windows-x64-portable.zip` |
| Mac with Apple silicon (M1 and later) | `Ninaivu-<version>-macos-arm64-portable.zip` |
| Linux PC (64-bit: Ubuntu, Debian and the like) | `Ninaivu-<version>-linux-x64-portable.tar.gz` |

The same page has this guide as a PDF, in English and Tamil.

Put the folder somewhere you can write to and that stays put — your
Documents, or a second disk. Ninaivu keeps the index of your library, the
settings and the AI models inside it, so moving or copying the folder takes
them along. Your photographs are not copied into it: they stay where they are.

## Windows

Right-click the zip and choose **Extract All**, then open **Ninaivu.exe** in
the folder it makes. The Control Panel opens: Start, Stop and Restart, the
addresses to open, the computer's readings and the log, and *Start Ninaivu
when I sign in*. The small menu in the system tray is there too, for anyone
who prefers it.

If Ninaivu.exe is not signed (the release notes say), Windows SmartScreen
says *Windows protected your PC*: choose **More info → Run anyway**.

[More on the Control Panel and the tray.](../desktop-control.md)

## macOS

Double-click the zip, move the **Ninaivu** folder it makes to your Documents
(or a drive), and open **Ninaivu.app** inside it.

If the app is not notarised (the release notes say), macOS will not open it
the first time. Try once, then go to **System Settings → Privacy & Security**
and choose **Open Anyway**; after that it opens normally. If Ninaivu says macOS
opened it from a temporary copy, open Terminal, type
`xattr -dr com.apple.quarantine ` (with the space), drag the Ninaivu folder
onto the window, press Return and open it again.

Opening **Ninaivu** opens the Control Panel: Start, Stop and Restart, the
addresses to open, the computer's readings and the log, and *Start Ninaivu
when I sign in*. The menu-bar tray is there too, for anyone who prefers it.

## Linux

Open a terminal in the folder it was downloaded to, unpack it and start it:

```bash
tar -xzf Ninaivu-<version>-linux-x64-portable.tar.gz
cd Ninaivu-<version>-linux-x64-portable
./ninaivu
```

No root is needed, and the machine needs no Python: the folder carries its
own, with every package Ninaivu uses, and downloads nothing. `./ninaivu`
opens the Control Panel. On a machine with no desktop (a server, or over
ssh), `./ninaivu --server ~/Pictures` runs Ninaivu in that terminal with
`~/Pictures` as the library; to have it start with the computer, see "The
Linux system service" in the technical administrator guide.

## Upgrading

Ninaivu does not look on the internet for newer versions. When you want
one, download the newer file from the same page.

Unpack the newer version into a new folder **beside the old one, on the same
drive**, and open it (`Ninaivu.exe`, `Ninaivu.app` or `./ninaivu`). Its
Control Panel finds the older folder and asks whether to bring your library
across. Say yes: it stops the older Ninaivu if it is running and moves the
library, the settings and the AI models into the new folder. Nothing is
copied, so it takes a moment however large the library is. Then press
**Start**. `README-PORTABLE.txt` in each folder says the same.

**Your photographs are never touched by an update.** The settings, the index
(names, albums, faces, share links), the people's accounts and the AI models
you downloaded all move to the new folder. The old folder keeps its program
and a note, `LIBRARY-MOVED.txt`, saying where the library went.

Changed your mind? **Go back to …** in the new Control Panel moves everything
back to the old folder, as the old version left it, for as long as you keep
the old folder. If the new version cannot start on your computer at all, it does
this by itself after the second try and opens the old one.

The first time the new version starts, it makes a copy of the settings and
the index before it changes anything, then starts as before. That copy is
what **Go back** uses to give the older version its index as it left it (the
[technical administrator guide](../admin-guide.md), "Updating to a new
version", says more).

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
