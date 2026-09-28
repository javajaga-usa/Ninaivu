#!/usr/bin/env bash
# Fixture + fresh instance for the media-type selection check.
#   tests/selection_ui.sh [workdir]
set -euo pipefail

WORK="${1:-/tmp/ninaivu-selection}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"

pkill -9 -f "[p]ython3 -m ninaivu" 2>/dev/null || true
sleep 1
rm -rf "$WORK"
mkdir -p "$WORK"/{shoebox,lib}

python3 - "$WORK" <<'PY'
import pathlib, sys
from datetime import datetime, timedelta
from PIL import Image

work = pathlib.Path(sys.argv[1])
for i in range(4):
    img = Image.new("RGB", (320, 240), ((i*37) % 256, 90, (i*13) % 256))
    ex = img.getexif(); ex[0x9003] = (datetime(2018,5,2)+timedelta(days=i*90)).strftime("%Y:%m:%d %H:%M:%S")
    img.save(work/f"shoebox/IMG_{i:04d}.jpg", "JPEG", exif=ex)
for i in range(3):
    (work/f"shoebox/CLIP_{i}.mp4").write_bytes(b"\0" * (2*1024*1024))
(work/"shoebox/song.mp3").write_bytes(b"\0" * (1024*1024))
Image.new("RGB", (140,140), (200,40,40)).save(work/"lib/existing.jpg")
print("fixture: 4 photos, 3 video (2 MB each), 1 audio")
PY

cd "$HERE"
NINAIVU_STATE_DIR="$WORK/state" nohup python3 -m ninaivu "$WORK/lib" \
  --ai off --no-watch --admin dad:correcthorse1 > "$WORK/server.log" 2>&1 &

for _ in $(seq 1 40); do
  sleep 1
  curl -sf -o /dev/null "http://localhost:3000/" && break
done

node "$HERE/tests/selection_ui.mjs" 3000 "$WORK/shoebox" "$WORK/Master"
STATUS=$?
pkill -9 -f "[p]ython3 -m ninaivu" 2>/dev/null || true
exit $STATUS
