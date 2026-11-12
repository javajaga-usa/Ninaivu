#!/usr/bin/env python3
"""Draw Ninaivu's marks and every picture of them the apps ask for.

    pip install resvg-py          # once; a self-contained wheel, no system libraries
    python tools/generate_icons.py

The album and Admin marks share a roof over a card, recalling the original
home for photographs: a sun and mountain for the album, sliders for Admin.
The Control Panel keeps the angular N / viewfinder monogram. Strong blue,
orange and graphite distinguish the three while the gradients unify them.

It is written here as SVG, so the same drawing is the browser-tab icon, the
brand mark in the pages (static/icons/mark.svg) and, rendered, every PNG: the
home-screen icons, the iPhone and iPad start-up screens, the Control Panel's
window and tray icon, the Windows .ico and the macOS iconset (both the
Control Panel, which is what those installers open). No source file to lose
and no artwork that is anybody's but the project's.

Pages use the transparent mark.svg, with mark.png available as a raster
export. Installed app icons keep their tiles. The start-up screens sit on the
warm charcoal of the dark theme with the name under the transparent mark.
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

#: Shared geometry, with a distinct colourway for each application surface.
FACES = {
    "family": ("#1677ff", "#1259e5", "#104acb"),
    "admin": ("#ff972f", "#f57416", "#db540b"),
    "panel": ("#283f46", "#152a32", "#0b161f"),
}
CHARCOAL = (20, 18, 16, 255)        # --bg of the dark theme


def mark_svg(face: str = "family", *, tile: str = "rounded", ident: str = "n") -> str:
    """The symbol: transparent for pages, tiled for installed app icons."""
    t0, t1, t2 = FACES[face]
    accent = {"family": "#c8e5ff", "admin": "#ffe5c2", "panel": "#84f5cf"}[face]
    if tile == "transparent":
        at, side, rx, art = 0, 1024, 0, 1.3
        accent = "#258cae" if face == "panel" else t2
    elif tile == "full":
        at, side, rx, art = 0, 1024, 0, .8
    elif tile == "mac":
        at, side, rx, art = 100, 824, 185, .8
    else:
        at, side, rx, art = 0, 1024, 230, 1
    k = (1 - art) * 512
    i = ident
    if face == "panel":
        symbol = (f'<path d="M288 392V288H496M736 632V736H528" fill="none" stroke="{accent}" stroke-width="40" stroke-linejoin="round"/>'
                  f'<path d="M320 688V416L704 688V416" fill="none" stroke="url(#{i}a)" stroke-width="88" stroke-linecap="square" stroke-linejoin="round"/>'
                  f'<circle cx="672" cy="304" r="48" fill="{accent}"/>')
    else:
        symbol = (f'<path d="M238 416L512 234L786 416" fill="none" stroke="url(#{i}a)" stroke-width="64" stroke-linecap="round" stroke-linejoin="round"/>'
                  f'<rect x="280" y="444" width="464" height="332" rx="58" fill="url(#{i}a)"/>')
        if face == "family":
            symbol += (f'<clipPath id="{i}photo"><rect x="322" y="486" width="380" height="248" rx="23"/></clipPath>'
                       f'<g clip-path="url(#{i}photo)"><rect x="322" y="486" width="380" height="248" fill="#ffd9b5"/>'
                       f'<circle cx="611" cy="550" r="37" fill="#fffaf2"/>'
                       f'<path d="M304 710L437 558L561 698L629 619L722 718V754H304Z" fill="#5b9afa"/>'
                       f'<path d="M306 741L476 624L593 726L710 666V756H306Z" fill="#104bbf"/></g>')
        else:
            track = "#fff0de" if tile == "transparent" else "#ed9a51"
            knob = "#fffaf2" if tile == "transparent" else "#e76b15"
            for y, x in ((524, 578), (610, 442), (696, 602)):
                symbol += (f'<path d="M354 {y}H670" stroke="{track}" stroke-width="22" stroke-linecap="round"/>'
                           f'<circle cx="{x}" cy="{y}" r="32" fill="{knob}" stroke="{t1}" stroke-width="12"/>')
    frame = "" if tile == "transparent" else (
        f'<rect x="{at}" y="{at}" width="{side}" height="{side}" rx="{rx}" fill="url(#{i}t)"/>'
        f'<rect x="{at}" y="{at}" width="{side}" height="{side}" rx="{rx}" fill="url(#{i}g)"/>'
        f'<rect x="{at + 7}" y="{at + 7}" width="{side - 14}" height="{side - 14}" rx="{max(0, rx - 7)}" fill="none" stroke="#fff" stroke-opacity=".14" stroke-width="2"/>'
    )
    ink = ("#3bc7a0" if face == "panel" else t0) if tile == "transparent" else "#fff"
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">
<defs>
<linearGradient id="{i}t" x1="0" y1="0" x2="1" y2="1">
<stop stop-color="{t0}"/><stop offset=".5" stop-color="{t1}"/><stop offset="1" stop-color="{t2}"/>
</linearGradient>
<linearGradient id="{i}a" x1="0" y1="1" x2="1" y2="0">
<stop stop-color="{ink}"/><stop offset="1" stop-color="{accent}"/>
</linearGradient>
<radialGradient id="{i}g" cx=".12" cy="0" r="1">
<stop stop-color="#fff" stop-opacity=".16"/><stop offset="1" stop-color="#fff" stop-opacity="0"/>
</radialGradient>
</defs>
{frame}
<g transform="translate({k:g} {k:g}) scale({art:g})">
{symbol}
</g>
</svg>'''


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
        (icons / "mark.svg").write_text(mark_svg(face, tile="transparent") + "\n", encoding="utf-8")
        clear = render(mark_svg(face, tile="transparent"), 1024)
        clear.resize((512, 512), Image.LANCZOS).save(icons / "mark.png", "PNG", optimize=True)
        if face == "family":               # the documentation site's logo and tab icon
            big.resize((192, 192), Image.LANCZOS).save(ROOT / "docs" / "assets" / "icon-192.png",
                                                       "PNG", optimize=True)
        for w, h in SCREENS:
            for width, height in ((w, h), (h, w)):
                splash(clear, width, height, name).save(splashes / f"splash-{width}x{height}.png",
                                                      "PNG", optimize=True)

    # The Control Panel: its window, its tray icon and the Linux menu entry.
    panel = ICONS / "panel"
    panel.mkdir(parents=True, exist_ok=True)
    big = render(mark_svg("panel"), 1024)
    for size in (512, 192):
        big.resize((size, size), Image.LANCZOS).save(panel / f"icon-{size}.png", "PNG", optimize=True)
    (panel / "mark.svg").write_text(mark_svg("panel", tile="transparent") + "\n", encoding="utf-8")
    render(mark_svg("panel", tile="transparent"), 512).save(panel / "mark.png", "PNG", optimize=True)

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
