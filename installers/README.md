# Installers

How to run Ninaivu on a machine that stays on.

| Folder | For |
| --- | --- |
| `docker/` | `Dockerfile`, `docker-compose.yml`. Build context is the repository root: `docker compose -f installers/docker/docker-compose.yml up -d` |
| `systemd/` | `ninaivu.service` for Linux |
| `windows/` | `install-service.ps1`, which registers Ninaivu as a Windows service |
| `caddy/`, `nginx/` | Reverse-proxy examples with HTTPS |

Signed Windows and macOS installers and a tray application are Phase 1 of the
[roadmap](../ROADMAP.md). Until then, `python start.py <folder>` from the
repository root is the way to run it on a desktop.

See `docs/operations/production.md` for the full operations guide.
