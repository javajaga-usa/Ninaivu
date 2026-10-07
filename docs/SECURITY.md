# Security

Ninaivu is meant to run on a home network, or behind a private network such as
Tailscale or WireGuard. Do not forward router ports to it.

## Reporting

Report a vulnerability privately through GitHub's "Report a vulnerability"
button on the Security tab of the repository, or by email to the address in
the repository's profile. Please do not open a public issue for a security
problem. You will get an acknowledgement within a few days and a fix or a
plan before anything is published.

## What is in scope

- Anything that lets one role see or change what another role may not.
- Anything that lets a share link reach beyond what its maker could see.
- Anything that sends a photograph, a name or a location off the machine
  without the administrator turning that on.
- Anything that can modify or delete a source photograph.

## Design notes

Sessions are HttpOnly cookies bound to one of the two apps. PIN and password
attempts are rate-limited per address and per profile, and wrong
administrator passwords pause sign-in by username for longer each time. The
admin console is bound to localhost by default.

Every request is judged as coming from home (the home network, this
computer, Tailscale, WireGuard) or from the internet (a public address, a
forwarded port, a proxy or tunnel not named in `trusted_proxies`, Tailscale
Funnel). From the internet, profiles without a PIN do not open, nobody browses
without signing in, plain HTTP is refused, and the console refuses every
request unless `console_from_internet` is turned on. The state backup holds session tokens and the
TLS CA key; keep it on the machine or encrypt it if it moves.

## Plain HTTP

Some ways of running Ninaivu serve the family app over plain HTTP: the Linux
installer's service, `python -m ninaivu` without `--https`, the Docker image
(port 5000) and `installers/windows/install-service.ps1` (port 5000). On plain
HTTP, passwords, PINs, session cookies and the photographs themselves cross
the home network unencrypted, and anyone on the same network (a guest on the
Wi-Fi, a compromised device) can read them or take over a session. The
launcher (`start.cmd`, `launcher/start.sh`) serves HTTPS by default. Otherwise
use `--https`, a reverse proxy with HTTPS (`installers/caddy`,
`installers/nginx`), or reach Ninaivu only over a private network such as
Tailscale or WireGuard. Never forward a router port to a plain-HTTP Ninaivu;
since 1.0.5 Ninaivu refuses plain HTTP from a public address in any case.

## Checking a download

Each release has a `SHA256SUMS.txt` beside the installers, and a
`*-packages.txt` for each installer listing every package inside it with its
own SHA-256. To check an installer, in the folder it was downloaded to:

```bash
sha256sum -c --ignore-missing SHA256SUMS.txt                   # Linux
shasum -a 256 Ninaivu-<version>-macos-arm64.dmg                 # macOS: compare with its line
```

```powershell
Get-FileHash .\Ninaivu-<version>-windows-x64.exe -Algorithm SHA256   # Windows: compare with its line
```

The checksums come from the same release page as the installers: they show
that a download is complete and is the file the release published, not that
the release itself is genuine. A signed installer (the release notes say
whether it is) is the stronger check: Windows shows the publisher, and macOS
opens a notarised app without a warning. The release workflow publishes only
from a commit whose tests passed, and refuses to publish over a release that
already exists.
