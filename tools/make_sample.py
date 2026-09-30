#!/usr/bin/env python3
"""Generate a sample library so you can try Ninaivu without your own photos.

    python tools/make_sample.py ~/mv-sample --count 240

Creates dated folders of synthetic images (with real EXIF), a couple of short
videos when ffmpeg is present, silent audio files, and a deliberate duplicate
pair so the duplicate detector has something to find.
"""

from __future__ import annotations

import argparse
import random
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

SCENES = [
    ("beach", (250, 214, 160), (110, 190, 215)),
    ("forest", (46, 92, 52), (150, 190, 120)),
    ("city", (70, 78, 96), (220, 190, 130)),
    ("sunset", (250, 140, 80), (60, 40, 90)),
    ("snow", (232, 240, 248), (150, 175, 200)),
    ("desert", (222, 176, 108), (140, 190, 220)),
]


def gradient(size, top, bottom):
    img = Image.new("RGB", size)
    draw = ImageDraw.Draw(img)
    for y in range(size[1]):
        t = y / max(1, size[1] - 1)
        draw.line(
            [(0, y), (size[0], y)],
            fill=tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3)),
        )
    return img


def make_image(path: Path, scene, index: int, when: datetime) -> None:
    width = random.choice([1600, 1400, 1200, 2000])
    height = random.choice([1200, 900, 1600, 1000])
    name, top, bottom = scene
    img = gradient((width, height), top, bottom)
    draw = ImageDraw.Draw(img, "RGBA")

    random.seed(f"{name}{index}")
    for _ in range(random.randint(4, 12)):
        x = random.randint(0, width)
        y = random.randint(int(height * 0.35), height)
        r = random.randint(30, 220)
        shade = random.randint(0, 90)
        draw.ellipse([x - r, y - r // 2, x + r, y + r // 2],
                     fill=(shade, shade + 12, shade + 20, 120))
    sun = (random.randint(100, width - 100), random.randint(60, int(height * 0.4)))
    draw.ellipse([sun[0] - 70, sun[1] - 70, sun[0] + 70, sun[1] + 70],
                 fill=(255, 245, 200, 190))
    img = img.filter(ImageFilter.GaussianBlur(0.6))

    exif = img.getexif()
    exif[0x0110] = "SampleCam X100"          # Model
    exif[0x010F] = "MediaVault"              # Make
    exif[0x0132] = when.strftime("%Y:%m:%d %H:%M:%S")
    exif[0x9003] = when.strftime("%Y:%m:%d %H:%M:%S")
    img.save(path, "JPEG", quality=88, exif=exif)


def make_video(path: Path, seconds: int = 3) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    result = subprocess.run(
        [ffmpeg, "-y", "-v", "quiet", "-f", "lavfi",
         "-i", f"testsrc=size=960x540:rate=24:duration={seconds}",
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",   # silence: a sample library should not beep
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-shortest", str(path)],
        check=False,
    )
    return result.returncode == 0


def make_audio(path: Path, seconds: int = 5) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    return subprocess.run(
        [ffmpeg, "-y", "-v", "quiet", "-f", "lavfi",
         "-i", "anullsrc=r=44100:cl=mono", "-t", str(seconds),
         "-metadata", "title=Sample Audio", "-metadata", "artist=Ninaivu",
         str(path)],
        check=False,
    ).returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("target", nargs="?", default="sample-library")
    parser.add_argument("--count", type=int, default=120)
    parser.add_argument("--clean", action="store_true")
    args = parser.parse_args()

    root = Path(args.target).expanduser().resolve()
    if args.clean and root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)

    # Photos cluster into "events" — a handful on one day, then a gap — which
    # is what real libraries look like and what the day-grouped grid is for.
    random.seed(7)
    when = datetime.now() - timedelta(days=540)
    made = 0
    event = 0
    while made < args.count:
        burst = min(args.count - made, random.choice([2, 3, 4, 5, 6, 8, 11]))
        scene = SCENES[event % len(SCENES)]
        folder = root / when.strftime("%Y/%m/%d")
        folder.mkdir(parents=True, exist_ok=True)
        for shot in range(burst):
            taken = when + timedelta(minutes=shot * random.randint(2, 25))
            make_image(folder / f"{scene[0]}_{made:04d}.jpg", scene, made, taken)
            made += 1
        event += 1
        when += timedelta(days=random.randint(2, 21), hours=random.randint(-4, 6))
        if event % 6 == 0:
            print(f"  {made} images…")

    # A near-duplicate pair for the duplicate detector.
    first = next(root.rglob("*.jpg"))
    shutil.copy(first, first.with_name(f"{first.stem}_copy.jpg"))

    videos = root / "2024/07/04"
    videos.mkdir(parents=True, exist_ok=True)
    if make_video(videos / "fireworks.mp4"):
        make_video(videos / "parade.mp4", 4)
        print("  2 videos")
    else:
        print("  (skipped videos — ffmpeg not found)")

    if make_audio(videos / "voice-note.mp3"):
        print("  1 audio file")

    print(f"\nSample library ready at {root}")
    print(f"Start it with:  python start.py {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
