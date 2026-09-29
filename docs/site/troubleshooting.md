# Troubleshooting

## The phone cannot open Ninaivu

- The phone must be on the home Wi-Fi, not mobile data.
- Use the address the console's **Server** page lists under *for the
  household*. `localhost` only works on the server itself.
- **Network access** on the Server page must be on; off, Ninaivu answers on
  the server only. Turning it on needs a restart.
- Windows: if the firewall blocks it, the startup log prints the two `netsh`
  commands that open the ports; run them once in a terminal opened as
  administrator.

## I cannot open the console from my laptop

The console answers on the computer Ninaivu runs on only, until you tick
*Open this console from other devices at home too* on the Server page (at
that computer). Ninaivu restarts, and the console is then on the home
network too.

## The browser warns about the certificate

Ninaivu serves HTTPS with its own certificate authority. Trust it once per
device: the tray's **Trust the HTTPS certificate**, or the Server page's
button, and the [tray page](../desktop-control.md) for doing it by hand on
Windows and macOS. Or put Tailscale or a reverse proxy in front, which bring
their own.

## Nothing appears after choosing the folder

The library is indexed in the background; a large one takes a while. The
Overview's **Library** numbers and the Server page's log show it going. If
the folder is on a drive that is not plugged in, the gallery says so rather
than showing nothing.

## The scan is slow

**System → Performance** says what this computer can do and what would help.
On a small machine, turn off the passes you do not need on **AI models**
(text in photographs and video moments are the expensive ones), or choose
*power-saving* on the Server page while the household is using the
computer. A scan can be stopped and picks up where it left off.

## A model would not download

The models come from GitHub and Hugging Face. Check the machine has internet
at the moment of the download; the AI models page says which file failed.
Once a model is here nothing is asked of the internet again.

## Mugil says the copy did not restore

The weekly test brings a few files back and compares them. If it fails,
**Mugil → Test restores** says which file and why; the usual causes are a
changed Google password (sign in again on Mugil) and a full Drive.

## Mugil says nothing is uploading

Encryption is on by default, and nothing is uploaded until the encryption
key exists. Make it on the Mugil page and keep the recovery file.

## Ninaivu will not start

The tray's **View the log** (or `.ninaivu-control/server.log` beside a
checkout) has the reason. The common ones: the port is taken by another
program (Ninaivu then moves to the next free one and says so; `--port`
chooses another, `--strict-port` refuses to move), the state folder is on a
drive that is not mounted, or another Ninaivu is already running from the
same state folder.

## Moving to another computer

**System → Move to another computer** lists the three things that make an
installation and, on the new machine, points the index at wherever the
library landed without re-indexing. [The details.](../moving-to-another-machine.md)

## Getting help

[Issues on GitHub](https://github.com/javajaga-usa/Ninaivu/issues). Say
which version (**System → Settings** shows it under *This home*), what you did, and what the log said.
