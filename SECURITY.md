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
attempts are rate-limited per address and per profile. The admin console is
bound to localhost by default. The state backup holds session tokens and the
TLS CA key; keep it on the machine or encrypt it if it moves.
