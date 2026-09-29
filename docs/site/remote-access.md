# Remote access

At home, Ninaivu answers to a name on the home network (`ninaivu.local`).
Away from home, it is reached through something the household set up — and
Ninaivu treats *how* as a choice, made once on **System → Server → Away from
home**.

| Choice | What it is | What it needs |
| --- | --- | --- |
| **Tailscale** | A private network between your own devices; the easiest | Tailscale installed on the server and on each phone |
| **WireGuard** | The same idea, set up by hand | the tunnel, and its subnets typed here |
| **A tunnel** | Cloudflare Tunnel or the like: a public name that reaches the house | the tunnel, and the name typed here |
| **Your own reverse proxy** | Caddy or nginx in front, with a certificate | the proxy ([examples](../operations/production.md)) |
| **Nothing** | Home network only | — |

The default works out Tailscale by itself when it is on the machine.

Each choice says what it still needs, decides which addresses count as
*away from home*, which addresses are listed for the household, and whose
certificate is served.

## HTTPS

Ninaivu makes its own certificate authority and serves HTTPS with it. Each
device that should not see a warning trusts that certificate once; the tray
and the console's Server page have the button, and the
[tray page](../desktop-control.md) has the details for Windows and macOS.
Tailscale and a reverse proxy bring their own certificates.

## What never goes out

Faces, names, the index, the photographs: none of it is sent anywhere by any
of these. Remote access is a way *in* to the house. The only thing Ninaivu
sends out on its own is the encrypted backup, to the account you chose, and
a once-a-day request to GitHub's releases page to see whether a newer
version is out (**Server** has the switch).
