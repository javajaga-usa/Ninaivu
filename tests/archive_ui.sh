#!/usr/bin/env bash
# Build a throwaway library, a throwaway set of "old drives", and a fresh
# Ninaivu instance, then drive the Archive tab through a real consolidation.
#
#   tests/archive_ui.sh [workdir]
set -euo pipefail

WORK="${1:-/tmp/ninaivu-archive-check}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"

pkill -9 -f "[p]ython3 -m ninaivu" 2>/dev/null || true
sleep 1
rm -rf "$WORK"
mkdir -p "$WORK"/{cardA/DCIM,cardB/2016,lib}

python3 - "$WORK" <<'PY'
import shutil, sys, pathlib
from datetime import datetime, timedelta
from PIL import Image

work = pathlib.Path(sys.argv[1])

def shot(path, when, seed, size=(320, 240)):
    img = Image.new("RGB", size, ((seed*37) % 256, (seed*91) % 256, (seed*13) % 256))
    ex = img.getexif(); ex[0x9003] = when.strftime("%Y:%m:%d %H:%M:%S"); ex[0x0110] = "TestCam"
    img.save(path, "JPEG", exif=ex)

for i in range(6):
    shot(work/f"cardA/DCIM/IMG_{i:04d}.jpg", datetime(2019,3,4)+timedelta(days=i*220), i+1)
for i in range(5):
    shot(work/f"cardB/2016/PIC_{i:04d}.jpg", datetime(2015,7,1)+timedelta(days=i*260), 100+i, (280,210))

shutil.copy(work/"cardA/DCIM/IMG_0000.jpg", work/"cardB/same_photo_again.jpg")  # byte-identical
shutil.copy(work/"cardB/2016/PIC_0002.jpg", work/"cardB/NOEXTENSION")           # deep scan only
(work/"cardA/readme.txt").write_text("not media")
Image.new("RGB", (140,140), (200,40,40)).save(work/"lib/existing.jpg")
print("fixture: 11 unique photos, 1 duplicate, 1 extensionless, 1 non-media")
PY

cd "$HERE"
NINAIVU_STATE_DIR="$WORK/state" nohup python3 -m ninaivu "$WORK/lib" \
  --ai off --no-watch --admin dad:correcthorse1 > "$WORK/server.log" 2>&1 &

for _ in $(seq 1 40); do
  sleep 1
  curl -sf -o /dev/null "http://localhost:3000/" && break
done

node "$HERE/tests/archive_ui.mjs" 3000 "$WORK/cardA" "$WORK/cardB" "$WORK/Master"
STATUS=$?
pkill -9 -f "[p]ython3 -m ninaivu" 2>/dev/null || true
exit $STATUS
