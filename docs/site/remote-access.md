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

## The console from other devices

The family app answers on the whole home network; the **console answers on
this computer only** until you tick *Open this console from other devices at
home too* on the Server page (Ninaivu restarts). `--admin-host` sets its
address outright.

## Everything Ninaivu sends out on its own

Remote access is a way *in* to the house. Faces, names and photographs are
never sent anywhere by it. This is the complete list of what Ninaivu asks of
the internet without a person pressing something at that moment:

| What | When | How to stop it |
| --- | --- | --- |
| A request to GitHub's releases page: is there a newer version? Nothing about this computer is in it. | Once a day | **Server** → the update check |
| Mugil: the photographs and the daily copy of the index, encrypted, to the Google account you connected | While Mugil is on | **Mugil** |
| Tailscale's HTTPS certificate for this computer, through Tailscale | With HTTPS on and Tailscale installed, every few weeks. The name `<computer>.<tailnet>.ts.net` then appears in public certificate logs, as every Let's Encrypt name does. | **Advanced settings** → `tailnet_https` |
| The image model's weights, from Hugging Face | Once, at the first start after search by description was added; nothing after that | Do not add search by description |
| A model download that stopped half-way | At the next start, until it finishes | **AI models** |
| The list of places, from GeoNames | Once, at the first scan after *Name the places* was switched on | Leave it off |
| Map tiles, from OpenStreetMap — they show roughly where your photographs were taken | Whenever someone opens the map, if `map_tiles` is on (off by default; the map uses a built-in outline otherwise) | **Advanced settings** → `map_tiles` |

Model downloads, notifications by email or webhook, and extensions such as
Gemini happen only once an administrator has set them up.
