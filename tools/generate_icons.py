#!/usr/bin/env python3
"""Draw Ninaivu's mark and every size of it the apps ask for.

    python tools/generate_icons.py

The mark: a roof over a photograph — a home that keeps pictures. Drawn here in
code, at 2048 px and scaled down, so there is no source file to lose and no
artwork that is anybody's but the project's. The family app's icon is the
accent blue; the console's, its purple (tools/generate_admin_assets.py
recolours it and makes the splash screens from it).
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
ICONS = ROOT / "ninaivu" / "static" / "icons"

BLUE = (11, 127, 212, 255)          # --accent in static/css/style.css
WHITE = (255, 255, 255, 255)


def draw_mark(size: int, colour=BLUE, *, maskable: bool = False) -> Image.Image:
    """The mark at *size*. Maskable icons keep the drawing inside the safe
    centre 80%, so a round or squircle mask on Android does not clip it."""
    s = 2048                               # drawn large, scaled at the end
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    if maskable:
        d.rectangle((0, 0, s, s), fill=colour)
    else:
        d.rounded_rectangle((0, 0, s - 1, s - 1), radius=s * 0.22, fill=colour)

    scale = 0.80 if maskable else 1.0
    cx = cy = s / 2
    w = 1.0 * scale                        # the stroke and geometry, in units of s

    def P(x, y):                           # a point in unit coordinates around the centre
        return cx + (x - 0.5) * s * scale, cy + (y - 0.5) * s * scale

    stroke = int(s * 0.075 * w)
    # The roof: a chevron with a small overhang each side.
    roof = [P(0.17, 0.50), P(0.50, 0.19), P(0.83, 0.50)]
    d.line(roof, fill=WHITE, width=stroke, joint="curve")
    for x, y in roof:
        d.ellipse((x - stroke / 2, y - stroke / 2, x + stroke / 2, y + stroke / 2), fill=WHITE)
    # The photograph under it: a rounded frame.
    x0, y0 = P(0.27, 0.46)
    x1, y1 = P(0.73, 0.82)
    d.rounded_rectangle((x0, y0, x1, y1), radius=s * 0.05 * scale, outline=WHITE, width=stroke)
    # Inside the frame: a sun and a hill, the way a picture is drawn.
    sx, sy = P(0.39, 0.585)
    r = s * 0.045 * scale
    d.ellipse((sx - r, sy - r, sx + r, sy + r), fill=WHITE)
    hill = [P(0.305, 0.775), P(0.45, 0.63), P(0.55, 0.71), P(0.615, 0.66), P(0.695, 0.775)]
    d.polygon(hill, fill=WHITE)
    return im.resize((size, size), Image.LANCZOS)


def main() -> None:
    ICONS.mkdir(parents=True, exist_ok=True)
    for size in (512, 192, 180):
        draw_mark(size).save(ICONS / f"icon-{size}.png", "PNG", optimize=True)
    draw_mark(512, maskable=True).save(ICONS / "icon-maskable-512.png", "PNG", optimize=True)
    # The installers' icons: one .ico for Windows (several sizes in one file)
    # and the PNG set the macOS build turns into an .icns with iconutil.
    installers = ICONS.parents[2] / "installers"
    (installers / "windows").mkdir(parents=True, exist_ok=True)
    draw_mark(256).save(installers / "windows" / "ninaivu.ico", "ICO",
                        sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    iconset = installers / "macos" / "ninaivu.iconset"
    iconset.mkdir(parents=True, exist_ok=True)
    for size in (16, 32, 128, 256, 512):
        draw_mark(size).save(iconset / f"icon_{size}x{size}.png", "PNG", optimize=True)
        draw_mark(size * 2).save(iconset / f"icon_{size}x{size}@2x.png", "PNG", optimize=True)
    print(f"icons written to {ICONS}, {installers / 'windows' / 'ninaivu.ico'} and {iconset}")


if __name__ == "__main__":
    main()
