# Running the checks on the Mac mini

The source repository is private. A private repository on GitHub Free gets
2,000 Actions minutes a month, with Windows counted twice and macOS ten times.
One run of Tests & Linting on 27 October 2026 took 50 macOS minutes, 131
Windows minutes and about 150 Linux minutes: about 910 of those 2,000. There
were about fifteen such runs that day. So the checks run at home, on the Mac
mini, and GitHub's own runners are kept for what the Mac cannot do.

Nothing changes until the repository variables below are set. With none of
them set, every job runs on GitHub's runners as before, so they can be unset
again at any time (a Mac that is retired, a month of travelling).

## What runs where

| Job | With the variables set | Without them |
| --- | --- | --- |
| Tests on Linux (3.12 and 3.14), Lint, Dependency audit, Browser tests, the release gate, the docs site | a Linux runner in Docker on the Mac (`ninaivu-linux`) | `ubuntu-latest` |
| Basic tier | the 2 GB Linux runner on the Mac (`ninaivu-linux-2gb`) | `ubuntu-latest`, in a 2 GB systemd scope |
| Full tier, the macOS download, the real-models check on Apple silicon | the Mac itself (`ninaivu-mac`) | `macos-14` |
| The Linux arm64 download, the Docker image's arm64 check | a Linux runner on the Mac (they are arm64) | `ubuntu-24.04-arm` (free for public repositories only) |
| Tests on Windows | Sunday nights and "Run workflow", Python 3.12 only (`WINDOWS_TESTS` = `weekly`) | every run, 3.12 and 3.14 |
| The Windows and Linux x64 downloads, publishing a release, the Docker image | GitHub's runners (a few minutes per release) | the same |

A check waits while the Mac is asleep or off, and starts when it is back. The
release gate waits for Tests & Linting on the commit as before, so a release
waits for the Mac too.

## Setting it up

You need Docker on the Mac (Immich already uses it) and about 20 GB free for
the runners' work folders.

### 1. The macOS runner

1. Open [Ninaivu's runner settings](https://github.com/javajaga-usa/Ninaivu/settings/actions/runners/new)
   and choose **macOS** and **ARM64**.
2. In Terminal, run the commands it shows, in `~/actions-runner`. When
   `./config.sh` asks:
   - name: `mac-mini`
   - additional labels: `ninaivu-mac`
   - work folder: the default (`_work`)
3. Make it start by itself, at sign-in, as a background service:

       cd ~/actions-runner
       ./svc.sh install
       ./svc.sh start

The runner runs as you, so a job can read what your account can. Only this
private repository's own workflows run on it. It never uses `sudo`: the one
step that did (a fix for GitHub's own macOS 15 machines) is skipped on it.

### 2. The Linux runners, in Docker

1. Make a fine-grained token at
   [github.com/settings/personal-access-tokens/new](https://github.com/settings/personal-access-tokens/new):
   repository access **Only select repositories → Ninaivu**, permission
   **Administration: Read and write**, nothing else.
2. In the checkout:

       cd installers/ci-runner
       cp .env.example .env
       # put the token after ACCESS_TOKEN= in .env
       docker compose up -d

3. Four runners appear under **Settings → Actions → Runners**: three
   `ninaivu-linux` and one `ninaivu-linux-2gb`. Each takes one job and
   restarts clean.

Docker Desktop (or OrbStack) needs enough memory for three 8 GB runners and
one 2 GB one at the same time: 26 GB of the Mac's 48 in its settings.

### 3. The repository variables

At [Settings → Secrets and variables → Actions → Variables](https://github.com/javajaga-usa/Ninaivu/settings/variables/actions),
add:

| Name | Value |
| --- | --- |
| `MAC_RUNNER` | `ninaivu-mac` |
| `LINUX_RUNNER` | `ninaivu-linux` |
| `LINUX_ARM64_RUNNER` | `ninaivu-linux` |
| `BASIC_TIER_RUNNER` | `ninaivu-linux-2gb` |
| `WINDOWS_TESTS` | `weekly` |

Set them before the repository is made private, and watch one pull request's
checks go green on the Mac first.

## Things to know

- The Linux runners are arm64, and GitHub's were x64. A test that only fails
  on one of them is a real difference worth a look, not a flaky runner.
- The Mac is also the household's server. A full run keeps several cores busy
  for most of an hour; Tuning's "Automatic" keeps Ninaivu's own work polite,
  and the daily catch-up waits for an idle Mac as before.
- Signing: with the Apple signing secrets set
  ([signing-releases.md](signing-releases.md)), the macOS download job makes
  a temporary keychain and puts it in your keychain search list. On the Mac
  mini that is your own account's list: before adding those secrets, run the
  macOS download job on GitHub's runner instead (unset `MAC_RUNNER` for that
  release) or ask for the job to restore the list afterwards.
- `RELEASES_REPO` (optional) names the public repository the downloads and
  the docs site go to; it defaults to `javajaga-usa/Ninaivu-releases`.
  `RELEASES_TOKEN` (a secret) is the fine-grained token that may write there.
