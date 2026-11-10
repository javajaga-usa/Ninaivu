#!/usr/bin/env python3
"""Draw Ninaivu's marks and every picture of them the apps ask for.

    pip install resvg-py          # once; a self-contained wheel, no system libraries
    python tools/generate_icons.py

The mark: a roof over a photograph — a home that keeps pictures. A rounded
chevron roof floats over a rounded card, and what is on the card says which
of Ninaivu's three faces this is:

* the family app (blue): a sunrise over a hill, a photograph;
* the console (violet): three sliders, the settings;
* the Control Panel (dark, the desktop app): a green power sign, on and off.

It is written here as SVG, so the same drawing is the browser-tab icon, the
brand mark in the pages (static/icons/mark.svg) and, rendered, every PNG: the
home-screen icons, the iPhone and iPad start-up screens, the Control Panel's
window and tray icon, the Windows .ico and the macOS iconset (both the
Control Panel, which is what those installers open). No source file to lose
and no artwork that is anybody's but the project's.

The start-up screens sit on the warm charcoal of the dark theme with the name
under the mark.
"""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "ninaivu" / "static"
ICONS = STATIC / "icons"
SPLASH = STATIC / "splash"
INSTALLERS = ROOT / "installers"

#: Each face's tile, light to dark from the top left. The roof and the card
#: are the same warm cream on all three, so they read as one family.
FACES = {
    "family": ("#4fb3ff", "#2a7cf0", "#5146e0"),
    "admin": ("#b39bff", "#7c5cff", "#4128c2"),
    "panel": ("#4a5550", "#26302c", "#121815"),
}

CREAM = "#fffaf2"
CHARCOAL = (20, 18, 16, 255)        # --bg of the dark theme in static/css/style.css

ROOF = "M262 418L512 236L762 418"
CARD = (290, 440, 444, 340, 60)     # x, y, width, height, corner radius
PHOTO = "M326 476H698V744H326Z"     # the picture on the family's card
CX, CY, S = 512, 612, 0.82          # the middle of the card, and the signs' scale


def _sign(face: str, i: str) -> str:
    """What is on the card."""
    if face == "family":
        return (f'<g clip-path="url(#{i}w)"><rect width="1024" height="1024" fill="url(#{i}s)"/>'
                f'<circle cx="{CX + 40 * S:g}" cy="{CY}" r="{74 * S:g}" fill="#fff6e2"/>'
                f'<path d="M0 {CY + 116 * S:g}C{CX - 120 * S:g} {CY + 40 * S:g} {CX - 40 * S:g} {CY + 36 * S:g} '
                f'{CX + 36 * S:g} {CY + 80 * S:g}S{CX + 156 * S:g} {CY + 92 * S:g} 1024 {CY + 30 * S:g}V1024H0Z" '
                f'fill="#2c6be0"/></g>')
    if face == "admin":
        out, w = [], 364 * S
        x0 = CX - w / 2
        for dy, at in ((-90, .72), (0, .3), (90, .82)):
            y, x = CY + dy * S, x0 + w * at
            out.append(f'<rect x="{x0:g}" y="{y - 14 * S:g}" width="{w:g}" height="{28 * S:g}" rx="{14 * S:g}" '
                       f'fill="#7c5cff" fill-opacity=".25"/>'
                       f'<rect x="{x0:g}" y="{y - 14 * S:g}" width="{x - x0:g}" height="{28 * S:g}" rx="{14 * S:g}" '
                       f'fill="#6a4cf0"/>'
                       f'<circle cx="{x:g}" cy="{y:g}" r="{40 * S:g}" fill="#fff" stroke="#6a4cf0" '
                       f'stroke-width="{22 * S:g}"/>')
        return "".join(out)
    r = 124 * S
    return (f'<path d="M{CX - 68 * S:g} {CY - 104 * S:g}A{r:g} {r:g} 0 1 0 {CX + 68 * S:g} {CY - 104 * S:g}" '
            f'fill="none" stroke="url(#{i}k)" stroke-width="{54 * S:g}" stroke-linecap="round"/>'
            f'<path d="M{CX} {CY - 150 * S:g}V{CY - 34 * S:g}" stroke="#1fb457" stroke-width="{54 * S:g}" '
            f'stroke-linecap="round"/>')


def mark_svg(face: str = "family", *, tile: str = "rounded", ident: str = "n") -> str:
    """The mark as an SVG document, 1024 units square.

    *face* is "family", "admin" (the console) or "panel" (the Control Panel).
    *tile* is "rounded" (the app icon and the pages), "full" (a maskable icon:
    the tile bleeds to the edges and the drawing keeps inside the safe centre)
    or "mac" (Apple's grid: an 824-unit tile centred on the canvas, which is
    how every other icon in the Dock is drawn). *ident* prefixes the gradient
    ids, so two marks inlined in one page do not share them.
    """
    t0, t1, t2 = FACES[face]
    i = ident
    if tile == "full":
        box, rx, art = (0, 1024), 0, 0.8
    elif tile == "mac":
        box, rx, art = (100, 824), 185, 0.8
    else:
        box, rx, art = (0, 1024), 230, 1.0
    at, side = box
    frame = (f'<rect x="{at}" y="{at}" width="{side}" height="{side}" rx="{rx}" fill="url(#{i}t)"/>'
             f'<rect x="{at}" y="{at}" width="{side}" height="{side}" rx="{rx}" fill="url(#{i}g)"/>')
    if rx:      # a hairline of light round the edge, so a dark tile still shows on a dark Dock
        line = 12 * side / 1024
        frame += (f'<rect x="{at + line / 2:g}" y="{at + line / 2:g}" width="{side - line:g}" '
                  f'height="{side - line:g}" rx="{rx - line / 2:g}" fill="none" stroke="#fff" '
                  f'stroke-opacity=".10" stroke-width="{line:g}"/>')
    k = (1 - art) * 512
    x, y, w, h, r = CARD
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">
<defs>
<linearGradient id="{i}t" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="{t0}"/><stop offset=".55" stop-color="{t1}"/><stop offset="1" stop-color="{t2}"/>
</linearGradient>
<radialGradient id="{i}g" cx=".2" cy=".05" r=".95">
<stop offset="0" stop-color="#fff" stop-opacity=".28"/><stop offset=".6" stop-color="#fff" stop-opacity="0"/>
</radialGradient>
<linearGradient id="{i}s" x1="0" y1="0" x2="0" y2="1">
<stop offset="0" stop-color="#ffd59a"/><stop offset="1" stop-color="#ff8f6b"/>
</linearGradient>
<linearGradient id="{i}k" x1="0" y1="0" x2="0" y2="1">
<stop offset="0" stop-color="#4ade80"/><stop offset="1" stop-color="#16a34a"/>
</linearGradient>
<clipPath id="{i}w"><path d="{PHOTO}"/></clipPath>
</defs>
{frame}
<g transform="translate({k:g} {k:g}) scale({art:g})">
<path d="{ROOF}" fill="none" stroke="{CREAM}" stroke-width="70" stroke-linecap="round" stroke-linejoin="round"/>
<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{CREAM}"/>
{_sign(face, i)}
</g>
</svg>"""


def render(svg: str, size: int) -> Image.Image:
    """An SVG drawn at *size* pixels square."""
    try:
        import resvg_py
    except ImportError as error:           # a tool for whoever changes the art
        raise SystemExit("pip install resvg-py, then run this again") from error
    png = bytes(resvg_py.svg_to_bytes(svg_string=svg, width=size, height=size))
    return Image.open(io.BytesIO(png)).convert("RGBA")


def font(size: int) -> ImageFont.FreeTypeFont:
    """The name on the start-up screens in Inter, the pages' own face (SIL
    Open Font License), or whichever of the usual sans faces this machine has."""
    for name in ("InterDisplay-SemiBold.otf", "/usr/share/fonts/opentype/inter/InterDisplay-SemiBold.otf",
                 "/Library/Fonts/InterDisplay-SemiBold.otf", "Inter-SemiBold.ttf",
                 "/System/Library/Fonts/SFNS.ttf", "/System/Library/Fonts/Helvetica.ttc",
                 "C:/Windows/Fonts/segoeuib.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                 "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


#: iPhone and iPad start-up screens, (width, height) in pixels, both ways round.
SCREENS = [
    (1320, 2868), (1290, 2796), (1206, 2622), (1179, 2556), (1284, 2778),   # iPhones @3x
    (1640, 2360), (1668, 2388), (1620, 2160), (2048, 2732), (1488, 2266),   # iPads @2x
]


def splash(mark: Image.Image, width: int, height: int, name: str) -> Image.Image:
    """The mark on the dark theme's charcoal, with the name under it."""
    short = min(width, height)
    scale = 3 if width in (1320, 1290, 1206, 1179, 1284) or height in (1320, 1290, 1206, 1179, 1284) else 2
    side = 96 * scale                      # the same size on every screen, in points
    im = Image.new("RGBA", (width, height), CHARCOAL)
    # A faint pool of the mark's colour behind it, so the dark is not flat.
    # Drawn on a canvas wider than the blur reaches, so it fades out to nothing.
    pool = int(short * 1.4)
    glow = Image.new("RGBA", (pool, pool), (0, 0, 0, 0))
    r = side * 1.6
    ImageDraw.Draw(glow).ellipse((pool / 2 - r, pool / 2 - r, pool / 2 + r, pool / 2 + r),
                                 fill=mark.getpixel((mark.width // 2, mark.height // 8))[:3] + (46,))
    glow = glow.filter(ImageFilter.GaussianBlur(side * 0.7))
    cy = height // 2 - 14 * scale
    im.alpha_composite(glow, ((width - pool) // 2, cy - pool // 2))
    icon = mark.resize((side, side), Image.LANCZOS)
    im.alpha_composite(icon, ((width - side) // 2, cy - side // 2))
    d = ImageDraw.Draw(im)
    f = font(22 * scale)
    w = d.textlength(name, font=f)
    d.text(((width - w) / 2, cy + side // 2 + 18 * scale), name, font=f, fill=(239, 233, 224, 255))
    # A few hundred colours are all a dark screen with one icon on it needs;
    # the start-up screens weigh a third of what they would in full colour.
    return im.convert("RGB").quantize(256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)


def main() -> None:
    for face, icons, splashes, name in (("family", ICONS, SPLASH, "Ninaivu"),
                                        ("admin", ICONS / "admin", SPLASH / "admin", "Ninaivu Admin")):
        icons.mkdir(parents=True, exist_ok=True)
        splashes.mkdir(parents=True, exist_ok=True)
        big = render(mark_svg(face), 1024)
        for size in (512, 192, 180):
            big.resize((size, size), Image.LANCZOS).save(icons / f"icon-{size}.png", "PNG", optimize=True)
        render(mark_svg(face, tile="full"), 512).save(icons / "icon-maskable-512.png", "PNG", optimize=True)
        (icons / "mark.svg").write_text(mark_svg(face) + "\n", encoding="utf-8")
        if face == "family":               # the documentation site's logo and tab icon
            big.resize((192, 192), Image.LANCZOS).save(ROOT / "docs" / "assets" / "icon-192.png",
                                                       "PNG", optimize=True)
        for w, h in SCREENS:
            for width, height in ((w, h), (h, w)):
                splash(big, width, height, name).save(splashes / f"splash-{width}x{height}.png",
                                                      "PNG", optimize=True)

    # The Control Panel: its window, its tray icon and the Linux menu entry.
    panel = ICONS / "panel"
    panel.mkdir(parents=True, exist_ok=True)
    big = render(mark_svg("panel"), 1024)
    for size in (512, 192):
        big.resize((size, size), Image.LANCZOS).save(panel / f"icon-{size}.png", "PNG", optimize=True)
    (panel / "mark.svg").write_text(mark_svg("panel") + "\n", encoding="utf-8")

    # The installers' icons, the Control Panel's, since that is what they open:
    # one .ico for Windows (several sizes in one file) and the PNG set the
    # macOS build turns into an .icns with iconutil.
    (INSTALLERS / "windows").mkdir(parents=True, exist_ok=True)
    # Every size as a bitmap, not PNG: NSIS cannot read PNG-compressed icons.
    render(mark_svg("panel"), 256).save(INSTALLERS / "windows" / "ninaivu.ico", "ICO", bitmap_format="bmp",
                                        sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    iconset = INSTALLERS / "macos" / "ninaivu.iconset"
    iconset.mkdir(parents=True, exist_ok=True)
    mac = render(mark_svg("panel", tile="mac"), 1024)
    for size in (16, 32, 128, 256, 512):
        mac.resize((size, size), Image.LANCZOS).save(iconset / f"icon_{size}x{size}.png", "PNG", optimize=True)
        mac.resize((size * 2, size * 2), Image.LANCZOS).save(iconset / f"icon_{size}x{size}@2x.png",
                                                            "PNG", optimize=True)
    print(f"icons written to {ICONS}, start-up screens to {SPLASH}, "
          f"and the installers' icons to {INSTALLERS}")


if __name__ == "__main__":
    main()
