# Troubleshooting

## The phone cannot open Ninaivu

- The phone must be on the home Wi-Fi, not mobile data.
- Use the address the console's **Server** page lists under *for the
  household*. `localhost` only works on the server itself.
- **Network access** on the Server page must be on; off, Ninaivu answers on
  the server only. Turning it on needs a restart.
- Windows: the first start asks to allow Ninaivu through the firewall; if
  that was refused, allow it in Windows Security → Firewall → Allow an app.

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
**Health** says which file and why; the usual causes are a changed Google
password (sign in again on Mugil) and a full Drive.

## Ninaivu will not start

The tray's **View the log** (or `.ninaivu-control/server.log` beside a
checkout) has the reason. The common ones: the port is taken by another
program (`--port` or the Server page changes it), the state folder is on a
drive that is not mounted, or another Ninaivu is already running from the
same state folder.

## Moving to another computer

**System → Move to another computer** lists the three things that make an
installation and, on the new machine, points the index at wherever the
library landed without re-indexing. [The details.](../moving-to-another-machine.md)

## Getting help

[Issues on GitHub](https://github.com/javajaga-usa/Ninaivu/issues). Say
which version (**Settings** shows it), what you did, and what the log said.
