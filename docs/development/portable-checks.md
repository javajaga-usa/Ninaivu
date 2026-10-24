# Checking the portable downloads

Every build the release workflow makes (`.github/workflows/release.yml`) is
used as a household would use it before it can be released, on the runner of
its own system: Windows x64, macOS on Apple silicon, Linux x64 and Linux
arm64. The same jobs run on every pull request that touches the builds, the
two-computer code, Extras or the desktop launchers.

## One computer

    python tools/check_portable.py check <download>

Unpacks the download and checks, with the download's own Python only:

- its Python is the one `installers/python-version.txt` names (the same
  CPython on every system), and packages install into the folder;
- what the system would otherwise need is inside: ffmpeg and ffprobe (with
  every ffmpeg on the PATH hidden), libheif, Tk, OpenCV, RAW, Tamil text
  layout, TLS and pip;
- the Control Panel opens and draws (under Xvfb on Linux);
- the first start on a library with Tamil and accented names, a folder with
  a space, an iPhone HEIC and a video: the first administrator is made, and
  every file is indexed with its EXIF date, a thumbnail, and the video's
  length;
- Extras installs ONNX Runtime into the folder's Python, the AI models page
  downloads the upscale model (36 MB), and it runs once;
- a second start on the same folder, and no photograph changed.

`--no-ai` leaves the AI step out (it needs PyPI and Hugging Face).

## Two computers on one

    python tools/check_portable.py pair <download>

Unpacks the download twice, makes one the Secondary with a folder for the
originals and the other the Primary, pairs them with the code the Secondary
shows, sends the originals, and checks every one byte for byte where the
Secondary keeps it.

## Two computers, two systems

The `pair-across` jobs run the Secondary and the Primary on two runners at
once, joined to a tailnet:

| Pair | Primary | Secondary |
| --- | --- | --- |
| mac-linux | macOS | Linux x64 |
| windows-linux | Windows | Linux x64 |
| mac-windows | macOS | Windows |

    python tools/check_portable.py secondary <download> --referee-port 8765
    python tools/check_portable.py primary <download> --partner <address> --referee-port 8765

The Secondary serves its pairing code, and at the end its verdict on what
arrived, on the referee port; the Primary finds it, pairs, sends and asks for
the verdict. Both can be run by hand on two real computers on the same
network or tailnet.

### The secrets they need

Without them the jobs say so and pass. To turn them on, once:

1. In the Tailscale admin console, **Access controls**: add a tag `tag:ci`
   owned by you, and allow `tag:ci` to reach `tag:ci` (with the default
   "allow all" policy nothing more is needed):

        "tagOwners": { "tag:ci": ["autogroup:admin"] },

2. **Settings → OAuth clients → Generate OAuth client**, with the
   **Auth Keys: write** scope and the tag `tag:ci`. Copy the client ID and the
   secret.
3. In the GitHub repository, **Settings → Secrets and variables → Actions**:
   add `TS_OAUTH_CLIENT_ID` and `TS_OAUTH_SECRET`.

Each runner joins as an ephemeral machine named
`ninaivu-ci-<run>-<attempt>-<pair>-<role>`, tagged `tag:ci`, and leaves when
its job ends. The CI machines only talk to each other; nothing on the
household's own machines is reached. To keep them apart from the household's
machines entirely, give `tag:ci` an ACL that reaches only `tag:ci`.
