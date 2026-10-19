#!/usr/bin/env python3
"""Put the pinned ffmpeg and ffprobe into a portable build's program files.

    python tools/bundle_ffmpeg.py DEST [--machine win32-x64|darwin-arm64|linux-x64]

The same builds the console's Extras page downloads (``FFMPEG_BUILDS`` in
ninaivu/media/components.py: an exact release, its size and its SHA-256),
fetched and checked the same way, and tried once to see they start. Run by
installers/windows/build-portable.ps1, installers/macos/build.sh and
installers/linux/build.sh with the Python being bundled, so a portable
Ninaivu makes video posters and plays every video from its first start,
with nothing to install. ``components.bundled_tools_dir`` is where Ninaivu
looks for them.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ninaivu.media import components  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("dest", type=Path, help="the folder to put ffmpeg and ffprobe in")
    parser.add_argument("--machine", default=None,
                        help="which build, as FFMPEG_BUILDS names it (default: this computer)")
    args = parser.parse_args(argv)
    key = args.machine or components.machine_key()
    build = components.FFMPEG_BUILDS.get(key)
    if build is None:
        print(f"no pinned ffmpeg for {key}", file=sys.stderr)
        return 1
    args.dest.parent.mkdir(parents=True, exist_ok=True)
    components.fetch_build(build, lambda line: print(line, flush=True), folder=args.dest)
    # ffmpeg is free software under the GPL, not Ninaivu's MIT licence: it
    # travels as a program of its own, with where it and its source come from.
    sources = "\n".join(f"    {item['url']}\n      SHA-256 {item['sha256']}"
                        for item in build["files"])
    (args.dest / "ABOUT-FFMPEG.txt").write_text(
        f"ffmpeg and ffprobe {build['version']}, built by {build['source']}, unchanged.\n\n"
        f"They are FFmpeg (https://ffmpeg.org), free software under the GNU General Public\n"
        f"License, and are not part of Ninaivu or under its licence: Ninaivu runs them as\n"
        f"separate programs. Their source code is at https://ffmpeg.org/download.html and\n"
        f"from {build['source']}, which says how each build was made.\n\n"
        f"Downloaded from:\n{sources}\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
