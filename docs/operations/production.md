# Ninaivu — Production Deployment & Operations Guide

> **Your family's media, at home.** A fast, private, AI-indexed library for photos, videos, and audio with role-based access control, zero telemetry, local AI search, and verified multi-source archiving.

This comprehensive guide details the architecture, deployment strategies, network hardening, performance tuning, and operational runbooks for running Ninaivu in high-reliability 24/7 production environments (home servers, NAS devices, bare-metal Linux, Docker clusters, macOS mini, and Windows servers).

---

## Table of Contents

1. [Architecture & Security Model](#1-architecture-security-model)
   - [The Two-Port Security Boundary](#the-two-port-security-boundary)
   - [Identity, Sessions & Scrypt Hashing](#identity-sessions-scrypt-hashing)
   - [Zero-Path Media Resolution](#zero-path-media-resolution)
   - [Internal CA & TLS Infrastructure](#internal-ca-tls-infrastructure)
2. [Production Deployment Blueprints](#2-production-deployment-blueprints)
   - [Option A: Docker & Docker Compose (Recommended)](#option-a-docker-docker-compose-recommended)
   - [Option B: Linux Systemd Service](#option-b-linux-systemd-service)
   - [Option C: Windows 24/7 Home Server](#option-c-windows-247-home-server)
   - [Option D: macOS Launchd Daemon](#option-d-macos-launchd-daemon)
3. [Reverse Proxy, Domains & SSL Configuration](#3-reverse-proxy-domains-ssl-configuration)
   - [Caddy (Automatic HTTPS & LAN Isolation)](#caddy-automatic-https-lan-isolation)
   - [Nginx (High-Performance Caching & HTTP/2 / HTTP/3)](#nginx-high-performance-caching-http2-http3)
   - [Secure Remote Access: Tailscale, WireGuard & Cloudflare Tunnels](#secure-remote-access-tailscale-wireguard-cloudflare-tunnels)
4. [Performance Tuning & Large Library Scaling (100k+ Items)](#4-performance-tuning-large-library-scaling-100k-items)
   - [SQLite WAL & Memory Optimization](#sqlite-wal-memory-optimization)
   - [Scanner Concurrency & Worker Tuning](#scanner-concurrency-worker-tuning)
   - [AI Engine Resource Management](#ai-engine-resource-management)
   - [Client-Side Virtual DOM Rendering](#client-side-virtual-dom-rendering)
5. [Backup, Disaster Recovery & High Availability](#5-backup-disaster-recovery-high-availability)
   - [Live Hot Backups with `ninaivu backup`](#live-hot-backups-with-ninaivu-backup)
   - [Automated Cloud Backup (Google Drive)](#automated-cloud-backup-google-drive)
   - [Archive Engine Consolidation](#archive-engine-consolidation)
   - [Disaster Recovery Procedure](#disaster-recovery-procedure)
6. [Monitoring, Health Checks & Maintenance](#6-monitoring-health-checks-maintenance)
   - [Health Probes (`/healthz`)](#health-probes-healthz)
   - [Scheduled Database Maintenance (`tools/db_maintenance.py`)](#scheduled-database-maintenance-toolsdb_maintenancepy)
7. [Production Troubleshooting & Runbook](#7-production-troubleshooting-runbook)
   - [Network Timeout vs Connection Refused](#network-timeout-vs-connection-refused)
   - [Database Lock Resolution](#database-lock-resolution)
   - [Orientation & Rotation Triage](#orientation-rotation-triage)
   - [Visibility Rollback & Safety Net](#visibility-rollback-safety-net)

---

## 1. Architecture & Security Model

The diagram below shows the container/reverse-proxy layout using internal ports 5000 and 3000. The desktop launcher instead defaults to HTTPS on port 443. With distinct advertised names and the same network bind, `ninaivu.local` routes to the family app and `ninaivu-admin.local` routes to the admin app on that shared listener. Administrative authorization remains enforced by the admin app. Restricting `--admin-host` to a different bind disables shared-listener admin routing; see [desktop controls](../desktop-control.md).

```
                     ┌──────────────────────────────────────────────────────────┐
                     │                      Local Network                       │
                     └──────────────────────────────────────────────────────────┘
                                   │                               │
                      Port 5000    │                  Port 3000    │ (Admin LAN only)
                     (Family App)  ▼                              ▼
                 ┌───────────────────────────┐      ┌───────────────────────────┐
                 │    Family Gallery App     │      │   Admin Management App    │
                 │  - Profile Picker (PIN)   │      │  - User & Profile Manager │
                 │  - Public & Family Media  │      │  - Visibility & Folders   │
                 │  - Fast Virtual Grid      │      │  - Archive Engine         │
                 │  - AI Natural Search      │      │  - Cloud Backup Sync      │
                 └─────────────┬─────────────┘      └─────────────┬─────────────┘
                               │                                  │
                               └─────────────────┬────────────────┘
                                                 │
                               ┌─────────────────▼─────────────────┐
                               │           Shared Core             │
                               │  - SQLite (WAL Mode + FTS5)       │
                               │  - Threaded Background Scanner    │
                               │  - OpenCLIP / PyTorch AI Engine   │
                               │  - KeepAwake Power Management     │
                               └─────────────────┬─────────────────┘
                                                 │
                        ┌────────────────────────┴────────────────────────┐
                        ▼                                                 ▼
             ┌─────────────────────┐                           ┌─────────────────────┐
             │    Media Folders    │                           │   State Directory   │
             │   (Read-Only Ops)   │                           │     (~/.ninaivu)     │
             │ - D:\Photos         │                           │ - index.db          │
             │ - /mnt/nas/pictures │                           │ - archive.db        │
             │ - _deleted (Bin)    │                           │ - thumbs/ (WebP)    │
             └─────────────────────┘                           │ - config.json       │
                                                               │ - certificates      │
                                                               └─────────────────────┘
```

### The Two-Port Security Boundary

Separate ports are an optional deployment arrangement, not an authorization mechanism. Host-based routing may expose the admin app on the shared HTTPS port; do not rely on blocking port 3000 alone to restrict it. Use the admin bind restriction and network policy appropriate to the deployment.

Ninaivu runs two distinct Flask applications inside a single optimized Python process:

1. **Family App (Port `5000`)**: The everyday interface for the household. Includes profile picker (with optional PIN or password), virtualized gallery, timeline, favorites, and AI natural language search.
   - **Zero Admin Routing**: Management routes (e.g. `/api/people`, `/api/scan`, `/api/cloud/*`, `/api/archive/*`) do not exist on this port. Requests return a genuine HTTP `404 Not Found` rather than `403 Forbidden`, eliminating admin route attack surface.
2. **Admin Console (Port `3000`)**: System operations interface behind an administrative authentication check. Handles library paths, user roles, folder visibility inheritance rules, consolidation jobs, and Google Drive cloud backup sync.
   - Sessions established on Port 5000 cannot access Port 3000 even if the user profile is an administrator.

### Identity, Sessions & Scrypt Hashing

- **Password & PIN Hashing**: All administrative credentials and profile PINs are hashed with scrypt (`ninaivu/utils/kdf.py`) with 16-byte random salts, $N=16384$, $r=8$, and $p=1$.
- **Opaque Server-Side Sessions**: Session tokens are URL-safe random strings made from 32 cryptographically secure random bytes (`secrets.token_urlsafe(32)`), stored in the SQLite `sessions` table. Disabling a profile or changing a password immediately revokes all active sessions across all devices.
- **Brute-Force Rate Limiting**: Sign-in endpoints implement IP and account-level rate limiting, essential for preventing dictionary attacks on 4-to-8-digit PINs.

### Zero-Path Media Resolution

No public API endpoint accepts a direct filesystem path. Originals, downloads, and thumbnails are addressed solely by positive integer `asset_id`. When resolving an asset:
1. The asset record is queried from `index.db`.
2. The user's role ceiling (`max_visibility`) and folder `scope` are strictly verified against the asset's attributes.
3. The canonical path is constructed and validated against the configured root (`os.path.commonpath`) to prevent path traversal attacks.

### Internal CA & TLS Infrastructure

When started with `--https`, Ninaivu creates an embedded small Certificate Authority (CA) in the state directory (`tls/ninaivu-ca.crt` and `tls/ninaivu-ca.key`).
- The CA is name-constrained: devices that trust it accept its certificates only for `.local`, `.home.arpa`, `localhost`, single-label host names and private IP addresses, never for public websites. A CA made by an earlier Ninaivu version has no constraint; delete `tls/` and reinstall the new `ninaivu-ca.crt` on devices to replace it.
- Generates 397-day certificates (the maximum accepted by iOS/macOS Safari) with Subject Alternative Names (SANs) for the local IP, hostname, and `ninaivu.local`.
- When the host IP changes (e.g. DHCP renewal), Ninaivu quietly re-issues the server certificate under the same CA without breaking client trust.
- Devices install `http://<ip>:5000/ninaivu-ca.crt` once for ordinary green-padlock TLS without browser interstitial warnings.

---

## 2. Production Deployment Blueprints

### Option A: Docker & Docker Compose (Recommended)

Docker provides an isolated, rootless, and multi-stage container deployment with automatic restarts and health monitoring.

#### 1. Setup Directories & Environment
```bash
git clone https://github.com/javajaga-usa/Ninaivu.git /opt/ninaivu
cd /opt/ninaivu
cp installers/docker/.env.example installers/docker/.env
```

Edit `installers/docker/.env` (it sits beside `docker-compose.yml`, which reads it) to set your media path:
```env
MEDIA_DIR=/mnt/storage/photos
```

`.env` is for folders and ports only. How Ninaivu behaves (workers, the AI engine, who may browse) is chosen in the console; a `NINAIVU_*` value for one of those only fills in what the console has not been used to choose.

#### 2. Build & Launch Container
```bash
docker compose -f installers/docker/docker-compose.yml up -d --build
```

#### 3. Verify Health & Logs
```bash
docker compose ps
docker compose logs -f ninaivu
```

In a container, **Restart** on the console's Server page makes the server
exit (code 75) and leaves the starting to the container's restart policy -
`restart: unless-stopped` in the compose file. A plain `docker run` needs
`--restart unless-stopped` for the same. The **Network access** switch cannot
be turned off in a container (listening on `127.0.0.1` there would leave
nothing able to reach it, the console included); publish the ports on
`127.0.0.1` instead (`127.0.0.1:5000:5000`). A container is recognised by
`/.dockerenv` or `/run/.containerenv`; `NINAIVU_IN_CONTAINER=1` or `=0` says
so outright.

#### 4. Container Management Commands
```bash
# Update container after code changes
docker compose build --no-cache && docker compose up -d

# Check live health status
docker inspect --format='{{json .State.Health}}' ninaivu | python -m json.tool
```

---

### Option B: Linux Systemd Service

For bare-metal Ubuntu, Debian, RHEL, or Arch Linux servers.

#### 1. Create Dedicated Service User
```bash
sudo useradd -r -s /bin/false -d /var/lib/ninaivu -m ninaivu
```

#### 2. Install Code & Build Virtual Environment
```bash
sudo mkdir -p /opt/ninaivu
sudo cp -r . /opt/ninaivu/
sudo chown -R ninaivu:ninaivu /opt/ninaivu /var/lib/ninaivu

# Set up virtualenv
sudo -u ninaivu python3 -m venv /opt/ninaivu/.venv
sudo -u ninaivu /opt/ninaivu/.venv/bin/pip install --upgrade pip
sudo -u ninaivu /opt/ninaivu/.venv/bin/pip install -r /opt/ninaivu/requirements/requirements.txt
# Search by description (optional). Torch from PyTorch's CPU index, the rest
# from PyPI: that index carries torch and little else, so pointing the whole
# file at it leaves open_clip_torch unresolvable.
sudo -u ninaivu /opt/ninaivu/.venv/bin/pip install "torch>=2.0,<3" --index-url https://download.pytorch.org/whl/cpu
sudo -u ninaivu /opt/ninaivu/.venv/bin/pip install -r /opt/ninaivu/requirements/requirements-ai.txt
```

#### 3. Install & Enable Systemd Unit
```bash
sudo cp installers/systemd/ninaivu.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ninaivu.service
```

Under systemd (recognised by `INVOCATION_ID` with systemd as the parent),
**Restart** on the console's Server page makes the server exit (code 75) and
`Restart=always` in the unit starts it again, so `systemctl stop` still
reaches it. `systemctl stop` lets the server finish what is in hand; the unit
allows 45 seconds (`TimeoutStopSec=45s`), and Docker the same
(`stop_grace_period: 45s` in `docker-compose.yml`). A resource-mode change from the console is refused there; set
`NINAIVU_RESOURCE_MODE` or `--workers` in the unit instead.

#### 4. Monitor & Inspect Service
```bash
sudo systemctl status ninaivu.service
sudo journalctl -u ninaivu.service -f
```

---

### Option C: Windows 24/7 Home Server

Running Ninaivu on a Windows PC acting as an always-on home server.

#### Automated Setup via PowerShell:
Run PowerShell as the account whose photographs Ninaivu serves (it asks to
elevate itself):
```powershell
Set-ExecutionPolicy Bypass -Scope Process -Force
cd C:\Github\Ninaivu
.\installers\windows\install-service.ps1 -Action Install -MediaFolder "D:\Photos"
```

This automates:
1. Creating Windows Firewall exceptions for TCP ports `5000` (Family) and `3000` (Admin).
2. Registering a scheduled task that runs at system boot **as the installing
   account, with ordinary (limited) rights**, whether or not anyone is signed
   in (logon type S4U, so no password is stored). It used to run as `SYSTEM`
   with highest privileges, which let anyone able to write to the checkout run
   code as `SYSTEM`. Pass `-RunAsUser DOMAIN\name` to choose another account.
   The task is given `--state-dir` (that account's `%USERPROFILE%\.ninaivu`
   unless `NINAIVU_STATE_DIR` says otherwise), so the tray sees the same server.
3. Enabling Windows Away Mode (`SetThreadExecutionState`) to prevent OS sleep while keeping the monitor off.

An S4U task cannot reach network shares (`\\server\share`, mapped drives):
keep the library on a local disk. The account needs the *Log on as a batch job*
right, which ordinary accounts have unless a policy removed it. A task
registered by an earlier version still runs as `SYSTEM`; run `-Action Install`
again to replace it.

The task passes `--supervised`: **Restart** on the console's Server page makes
the server exit (code 75), and a keep-alive trigger starts it again within a
minute - the task stays in charge of it. A resource-mode change cannot be
applied that way (the task's arguments decide the worker count) and is refused.
The console's **Stop** is also followed by a start within a minute; to keep it
stopped, use `-Action Stop`, which disables the task until `-Action Start`.

To manage the Windows service:
```powershell
.\installers\windows\install-service.ps1 -Action Status
.\installers\windows\install-service.ps1 -Action Stop
.\installers\windows\install-service.ps1 -Action Start
.\installers\windows\install-service.ps1 -Action Uninstall
```

---

### Option D: macOS Launchd Daemon

For a headless Mac Mini or Apple Silicon server.

The simplest way is built in: tick **Start Ninaivu when I sign in** in Ninaivu
Control Panel, or run `.venv/bin/python -m ninaivu.desktop.autostart --enable`.
That writes `~/Library/LaunchAgents/local.ninaivu.start.plist`, which starts
Ninaivu at sign-in with the control panel's own settings (HTTPS, port, resource
mode), after waiting for a network address and the library drive. It does not
keep Ninaivu alive, so Stop in the panel stays stopped. With FileVault on, a
Mac waits at the sign-in screen after a restart; to have it come back after a
power cut, turn on "Start up automatically after a power failure" in System
Settings.

The hand-written agent below also works, but its `KeepAlive` starts Ninaivu
again whenever it is stopped, including from the control panel, and it runs
without the panel's arguments. Use one or the other.

#### 1. Create Launchd Agent Configuration
Save as `~/Library/LaunchAgents/local.ninaivu.plist`:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>local.ninaivu</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/youruser/Ninaivu/.venv/bin/python</string>
        <string>-m</string>
        <string>ninaivu</string>
        <string>/Users/youruser/Pictures</string>
        <string>--host</string>
        <string>0.0.0.0</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/youruser/Ninaivu</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/youruser/.ninaivu/output.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/youruser/.ninaivu/error.log</string>
</dict>
</plist>
```

#### 2. Load Agent
```bash
launchctl load ~/Library/LaunchAgents/local.ninaivu.plist
```

---

## 3. Reverse Proxy, Domains & SSL Configuration

### Caddy (Automatic HTTPS & LAN Isolation)

Caddy automatically provisions Let's Encrypt certificates and supports HTTP/3. Use the supplied [installers/caddy/Caddyfile](https://github.com/javajaga-usa/Ninaivu/blob/main/installers/caddy/Caddyfile):

```caddyfile
# Family Gallery (Public HTTPS)
photos.yourfamily.net {
    encode gzip zstd
    reverse_proxy http://127.0.0.1:5000 {
        flush_interval -1
    }
}

# Admin Console (Subnet-Restricted)
admin.photos.yourfamily.net {
    @blocked not remote_ip 127.0.0.1/32 192.168.0.0/16 10.0.0.0/8
    respond @blocked "Forbidden: LAN access only." 403

    reverse_proxy http://127.0.0.1:3000 {
        flush_interval -1
    }
}
```

### Nginx (High-Performance Caching & HTTP/2 / HTTP/3)

The supplied [installers/nginx/ninaivu.conf](https://github.com/javajaga-usa/Ninaivu/blob/main/installers/nginx/ninaivu.conf) optimizes static thumbnail delivery and disables buffering for real-time progress events:

```nginx
# Critical for real-time SSE progress events: the archive's progress stream,
# which only the console (port 3000) serves
location = /api/archive/stream {
    proxy_pass http://127.0.0.1:3000;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 86400s;
}

# 1-Year Immutable Caching for Derived Thumbnails
location ~* ^/api/thumb/ {
    proxy_pass http://127.0.0.1:5000;
    expires 365d;
    add_header Cache-Control "public, max-age=31536000, immutable";
}
```

### Secure Remote Access: Tailscale, WireGuard & Cloudflare Tunnels

> [!WARNING]
> Never expose Port 3000 (Admin Console) directly to the public internet via a forwarded router port.

Since 1.0.5 Ninaivu also guards against this itself. A public address, a
proxy or tunnel not named in `trusted_proxies`, and Tailscale Funnel all count
as the internet: profiles without a PIN do not open, nobody browses without
signing in, plain HTTP is refused, and the console refuses every request
unless `console_from_internet` is on. The [technical administrator
guide](../admin-guide.md#reaching-ninaivu-from-outside) has the full list and
the set-up steps.

- **Tailscale / WireGuard (Recommended)**: Run Tailscale on the host machine and on each family device. See *Tailscale, step by step* below.
- **Cloudflare Tunnels**: Route only the family app's port through `cloudflared`. Protect the domain with Cloudflare Access (One-Time PIN / OAuth) for zero-trust remote access.

#### Tailscale, step by step

Ninaivu listens on every network the computer is on, the tailnet included, and
answers the computer's tailnet name with the certificate Tailscale gives it.
So devices signed in to your tailnet, and nobody else, reach it from anywhere:

| | Address |
|---|---|
| Family app | `https://<computer>.<tailnet>.ts.net` |
| Admin console | `https://<computer>.<tailnet>.ts.net:3000` |

1. Install Tailscale on the Ninaivu computer and sign in. Install it on each
   phone or laptop that should reach Ninaivu, signed in to the same tailnet
   (or share the Ninaivu computer with a family member's account).
2. In Tailscale's admin console, **DNS**: MagicDNS on, and **HTTPS
   Certificates → Enable**. Tailscale then gets a Let's Encrypt certificate
   for the computer's `ts.net` name. Certificates are published in public
   Certificate Transparency logs, so the name (not the photographs, and not a
   way in) becomes visible there.
3. Restart Ninaivu, or wait: it asks Tailscale for the certificate when it
   starts and every six hours (`tailnet_https`, on by default; it does nothing
   without Tailscale or before step 2). Then check, on the Ninaivu computer:
   ```bash
   python3 tools/tailscale_access.py enable
   ```
   It checks each address answers with a certificate a phone accepts, and
   `status` shows the same any time.
4. On an iPhone, turn on **VPN On Demand** in the Tailscale app so it
   connects by itself away from home, and open the family app at the `ts.net`
   address. It is a different address from `ninaivu.local`, so it signs in and
   installs to the home screen separately.

Why this and not Tailscale Serve: on a Mac, Serve cannot take port 443 while
Ninaivu holds it. The Tailscale app runs as the user, and macOS lets a user bind
a port below 1024 only on every address, which Ninaivu already has: Serve then
held 443 only for IPv6, the tailnet name reached Ninaivu with the wrong
certificate, and a restart of Ninaivu found 443 taken and moved to 444. The
tool takes off Serve entries for Ninaivu's ports that an earlier version made.

Why this is safe:

- **Never Funnel.** Funnel publishes to the whole internet. Nothing here uses
  it, and `status` warns if anything has turned it on.
- **Only the tailnet can connect.** The `ts.net` name resolves to the tailnet
  address, which only signed-in devices can reach. Who that is, is who is in
  the tailnet; Tailscale's access rules can narrow it, for example keeping
  port 3000 to you alone. Never forward router ports to Ninaivu.
- Ninaivu sees each device's own tailnet address, so sign-in limits apply per
  device, and nothing that only the Ninaivu computer may do (stopping the
  server, the console's file browser) is open to them. Profile PINs of 6 or
  more digits are still the better choice.
- Keep `trusted_proxies` at 0 unless a proxy really is the only way in.
  When it is set, forwarded headers are believed only from a proxy on this
  computer. A proxy on another machine or in another container must be
  listed in `NINAIVU_TRUSTED_PROXY_ADDRESSES` (comma-separated addresses or
  ranges, for example `172.18.0.0/16`); from anywhere else the headers are
  dropped and the log says so once.
---

## 4. Performance Tuning & Large Library Scaling (100k+ Items)

### SQLite WAL & Memory Optimization

Ninaivu configures SQLite with WAL (Write-Ahead Logging) and `synchronous = NORMAL`, allowing concurrent reader threads without locking writer transactions.

For libraries over 200,000 items:
1. **Memory Mapping**: Set `PRAGMA mmap_size = 2147483648;` (2 GB) to enable zero-copy kernel reads directly into process memory.
2. **Cache Size**: SQLite default page cache can be increased in `ninaivu/storage/db.py`:
   ```python
   conn.execute("PRAGMA cache_size = -64000")  # 64 MB RAM cache
   ```

### Scanner Concurrency & Worker Tuning

The media scanner splits work into:
1. **Fast Scandir Walk**: Enumerates directory structures and performs stat cache lookups (`mtime`, `size`).
2. **Threaded Worker Pool**: Performs EXIF parsing, thumbnail resizing, perceptual hashing, and blurhash computation.

Tuning guidelines:
- **Fast NVMe SSD**: `--workers 8` or `--workers 12`
- **Spinning Hard Drives (HDDs) / Network SMB Mounts**: Set `--workers 2` or `--workers 4` to prevent disk head thrashing.

### AI Engine Resource Management

| Model Tier | Memory (RAM) | Speed (CPU) | Capability |
| :--- | :--- | :--- | :--- |
| **`clip` (OpenCLIP ViT-B-32)** | ~1.2 GB | ~8–12 images/sec | Natural-language semantic search, visual similarity, 156 zero-shot tags |
| **`light` (Pure Python/Pillow)** | ~80 MB | ~150+ images/sec | Color tags, panoramic/aspect ratio classification, basic keyword search |
| **`off`** | ~40 MB | Instant | Gallery only, no AI classification |

To limit CPU overhead on low-power NAS devices (e.g. Raspberry Pi 4 / Intel Celeron), use `--ai light` or configure environment variable `NINAIVU_AI_ENGINE=light`.

### Client-Side Virtual DOM Rendering

Ninaivu loads zero heavy client frameworks. The UI in `grid.js` uses a custom virtualized DOM layout:
- Renders only ~50 tiles in the DOM at any scroll position.
- Uses CSS `transform: translate3d()` for hardware acceleration.
- Coalesces scroll measurements inside `requestAnimationFrame` maintaining steady 60 FPS on mobile devices.

---

## 5. Backup, Disaster Recovery & High Availability

### Live Hot Backups with `ninaivu backup`

SQLite databases cannot be copied safely using standard filesystem tools while writes are occurring. The included hot backup utility uses SQLite's native Online Backup API to produce consistent, verified snapshots without taking Ninaivu offline:

```bash
# Execute hot backup
ninaivu backup --out /mnt/backups/ninaivu

# List available snapshots
ninaivu list-backups /mnt/backups/ninaivu
```

Snapshot bundles contain:
- `index.db` & `archive.db` (Verified via SQLite backup API)
- `config.json` (Runtime preferences and roots)
- `avatars/` (User profile pictures)
- `tls/` (Ninaivu's certificate authority and server certificate)
- `cloud-encryption.json` & `google.json` (cloud backup encryption key and Google sign-in, when set up)
- `pending-uploads/` (family uploads awaiting approval)
- `backup_manifest.json` (SHA-256 integrity checksums)

Bundles contain private keys and credentials. They are written readable only by the account Ninaivu runs as; keep them somewhere equally private.

### Automated Cloud Backup (Google Drive)

Ninaivu includes an integrated, one-way Google Drive backup daemon:
- **One-Way Protection**: Files only move outbound. Ninaivu never deletes local files based on remote state.
- **Resumable Chunks**: Uploads check the pause signal between chunks, resuming instantly where they left off.
- **Time Windows & Rate Limiting**: Restrict cloud uploads to off-peak hours (e.g. `23:00` to `06:00`) and cap bandwidth (`NINAIVU_CLOUD_RATE_KBPS=2000`).

### Archive Engine Consolidation

Consolidate legacy flash drives, SD cards, and phone backups into an immutable `YYYY/MM/DD` directory:
- **Strict Byte-for-Byte Verification**: Every file is SHA-256 hashed upon read, written to a temporary name, and re-read from disk for hash verification before being committed.
- **No Overwrite Guarantee**: Duplicate files are indexed in `archive.db` and left untouched on the source.
- **Disconnect Recovery**: A missing removable source or destination pauses
  without trusting partial output. Ninaivu validates the returning volume and
  resumes automatically from verified database state.
- **Adaptive Resource Use**: Windows EcoQoS keeps ordinary serving efficient
  and is lifted for finite archive work. Archive I/O is paced on battery or
  under thermal pressure and safely parked at critical thresholds.
- **Archive Guardian**: A daily rotating checksum sample plus free-space check
  provides a low-cost health signal. Use Archive Audit for a full byte scrub.
- **Portable Recovery**: Export `recovery.json` and keep it off-machine.
  `tools/verify_recovery_report.py` verifies it using only standard Python,
  without Ninaivu or SQLite.

### Disaster Recovery Procedure

To restore Ninaivu on a clean system or hardware replacement:

Stop any existing Ninaivu process and disable automatic restarts first. Restore
validates the complete manifest and SQLite databases, prepares replacement state,
and retains the original directory for recovery. The destination must be a
regular directory that can be renamed; container volume roots may require a
host-side restore or a state subdirectory. See [backup recovery](../backup-recovery.md)
for space requirements, rollback behavior, and interrupted-restore recovery.

```bash
# 1. Install Ninaivu dependencies
python -m venv .venv && source .venv/bin/activate
pip install -r requirements/requirements.txt

# 2. Restore state from snapshot
ninaivu restore /mnt/backups/ninaivu/ninaivu_backup_20260830_120000.tar.gz

# 3. Start server pointing at restored or original media folder
python -m ninaivu /mnt/storage/photos
```

---

## 6. Monitoring, Health Checks & Maintenance

### Health Probes (`/healthz`)

Ninaivu exposes a lightweight JSON health probe on both ports:

```http
GET /healthz HTTP/1.1
Host: localhost:5000

HTTP/1.1 200 OK
Content-Type: application/json

{"ok": true, "time": 1788110400.123}
```

Use this in container orchestrators (Kubernetes liveness probes, Docker healthchecks) or monitoring suites (Uptime Kuma, Prometheus Blackbox Exporter).

Ninaivu also exposes a readiness probe on both ports:

```http
GET /readyz HTTP/1.1
Host: localhost:5000

HTTP/1.1 200 OK
Content-Type: application/json
Cache-Control: no-store

{"checks":{"database":"ok","library":"ok"},"ok":true,"time":1788110400.123}
```

Use `/healthz` for **liveness** (should the process be restarted) and `/readyz`
for **readiness** (should new traffic be sent to it). Readiness returns `503`
when the index database cannot be queried or every configured library folder is
unavailable, as when an external drive is unplugged. A library that has not yet
been configured is still ready so the initial setup screens remain reachable;
its library check is reported as `not-configured`. Probe responses never expose
library or database paths.

### Scheduled Database Maintenance (`tools/db_maintenance.py`)

Run weekly via cron or Task Scheduler to optimize SQLite page layouts and reclaim unallocated disk space:

```bash
# Linux Cron (/etc/cron.weekly/ninaivu-maintenance)
0 3 * * 0 ninaivu /opt/ninaivu/.venv/bin/python /opt/ninaivu/tools/db_maintenance.py --all >> /var/log/ninaivu_maint.log 2>&1
```

Options:
- `--check`: Verifies B-Tree pages and foreign key constraints (`PRAGMA integrity_check`).
- `--checkpoint`: Truncates the Write-Ahead Log (`PRAGMA wal_checkpoint(TRUNCATE)`).
- `--vacuum`: Defragments database pages and shrinks database file size.
- `--reindex`: Rebuilds FTS5 full-text search indexes.
- `--prune-thumbs`: Identifies and removes disk thumbnail files whose parent assets have been deleted.
- `--stats`: Outputs storage metrics and media distribution tables.

---

## 7. Production Troubleshooting & Runbook

### Network Timeout vs Connection Refused

| Symptom | Underlying Cause | Resolution |
| :--- | :--- | :--- |
| **Instant "Connection Refused"** | Ninaivu is not running or listening only on `127.0.0.1`. | Restart with `--host 0.0.0.0`. Check `systemctl status ninaivu`. |
| **30-Second Spinning Timeout** | Firewall is silently dropping packets, or phone is on Guest Wi-Fi / AP Isolation. | Windows: allow the ports in Windows Firewall (`python tools/netcheck.py` prints the exact command). Linux: `sudo ufw allow 5000/tcp`. Verify device is on same subnet with `python tools/netcheck.py`. |
| **SSL Certificate Warning** | Client has not installed Ninaivu's private CA. | Open `http://<ip>:5000/ninaivu-ca.crt` on device and enable Trust in OS Certificate Settings. |

### Database Lock Resolution

If you encounter `sqlite3.OperationalError: database is locked`:
1. Check for zombie background processes:
   ```bash
   # Linux/macOS
   ps aux | grep ninaivu
   # Windows
   Get-Process python*
   ```
2. Flush WAL log with maintenance tool:
   ```bash
   python tools/db_maintenance.py --checkpoint
   ```

### Orientation & Rotation Triage

Ninaivu automatically detects rotation for unoriented photos (scanned prints, messaging app exports) using facial geometry classifiers.
- **Manual Rotation**: Administrators and family members can press the Rotate button in the photo viewer to save the correction. Guests receive a temporary viewer-only turn. Ninaivu rewrites the thumbnail on disk and updates `rotation` in `index.db` with `rot_source='manual'`.
- **Cache Invalidation**: Thumbnails use versioned query strings (`?s=256&v=<indexed_at>`) so client browsers immediately reflect rotations despite `Cache-Control: immutable`.

### Visibility Rollback & Safety Net

Bulk visibility changes on folders create snapshot records in `visibility_history`. If a folder is accidentally set to *Public* or *Nobody*:
1. Open **Admin Console → Folders**.
2. Click **Undo** on the action banner.
3. Ninaivu restores both the previous folder rule and the exact individual per-item visibility states for all affected media.
