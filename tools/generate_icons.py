#!/usr/bin/env python3
"""Draw Ninaivu's mark and every picture of it the apps ask for.

    pip install resvg-py          # once; a self-contained wheel, no system libraries
    python tools/generate_icons.py

The mark: a house whose walls frame a photograph, a sunrise over two hills —
a home that keeps pictures. It is written here as SVG, so the same drawing is
the browser-tab icon, the brand mark in the pages (static/icons/mark.svg) and,
rendered, every PNG: the home-screen icons, the iPhone and iPad start-up
screens, the Windows .ico, the macOS iconset and the Control Panel's icon.
No source file to lose and no artwork that is anybody's but the project's.

Two colourings: the family app in its blue, the console in its violet. The
start-up screens sit on the warm charcoal of the dark theme with the name
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

#: The tile and the hills take the face's own colours; the sky, the sun and
#: the house are the same warm cream and gold on both, so the two read as one.
FACES = {
    "family": {"tile": ("#46a8f7", "#1677d8", "#4a45d1"),
               "far": "#8f86f5", "near": ("#2f8ef0", "#1663c7")},
    "admin": {"tile": ("#a08cff", "#6d5efc", "#3c2bb3"),
              "far": "#c49cf6", "near": ("#7d6cff", "#4b39c9")},
}

CREAM = "#fffaf2"
CHARCOAL = (20, 18, 16, 255)        # --bg of the dark theme in static/css/style.css


def mark_svg(face: str = "family", *, tile: str = "rounded", ident: str = "n") -> str:
    """The mark as an SVG document, 1024 units square.

    *tile* is "rounded" (the app icon and the pages), "full" (a maskable icon:
    the tile bleeds to the edges and the drawing keeps inside the safe centre)
    or "mac" (Apple's grid: an 824-unit tile centred on the canvas, which is
    how every other icon in the Dock is drawn). *ident* prefixes the gradient
    ids, so two marks inlined in one page do not share them.
    """
    c = FACES[face]
    t0, t1, t2 = c["tile"]
    n0, n1 = c["near"]
    if tile == "full":
        frame, art = '<rect width="1024" height="1024" fill="url(#{i}t)"/>', 0.8
    elif tile == "mac":
        frame, art = ('<rect x="100" y="100" width="824" height="824" rx="185" fill="url(#{i}t)"/>'
                      '<rect x="100.5" y="100.5" width="823" height="823" rx="184.5" fill="url(#{i}g)"/>'), 0.8
    else:
        frame, art = ('<rect width="1024" height="1024" rx="232" fill="url(#{i}t)"/>'
                      '<rect width="1024" height="1024" rx="232" fill="url(#{i}g)"/>'), 1.0
    if tile == "full":
        frame += '<rect width="1024" height="1024" fill="url(#{i}g)"/>'
    frame = frame.format(i=ident)
    k = (1 - art) * 512
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">
<defs>
<linearGradient id="{ident}t" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="{t0}"/><stop offset=".5" stop-color="{t1}"/><stop offset="1" stop-color="{t2}"/>
</linearGradient>
<radialGradient id="{ident}g" cx=".22" cy=".08" r=".9">
<stop offset="0" stop-color="#fff" stop-opacity=".30"/><stop offset=".55" stop-color="#fff" stop-opacity="0"/>
</radialGradient>
<linearGradient id="{ident}s" x1="0" y1="0" x2="0" y2="1">
<stop offset="0" stop-color="#ffdca8"/><stop offset="1" stop-color="#ffa86b"/>
</linearGradient>
<linearGradient id="{ident}h" x1="0" y1="0" x2="0" y2="1">
<stop offset="0" stop-color="{n0}"/><stop offset="1" stop-color="{n1}"/>
</linearGradient>
<clipPath id="{ident}w"><path d="M512 386 694 528V738H330V528Z"/></clipPath>
</defs>
{frame}
<g transform="translate({k:g} {k:g}) scale({art:g})">
<path d="M512 232 806 470V796H218V470Z" fill="#000" fill-opacity=".14" stroke="#000" stroke-opacity=".14" stroke-width="72" stroke-linejoin="round"/>
<path d="M512 214 806 452V778H218V452Z" fill="{CREAM}" stroke="{CREAM}" stroke-width="72" stroke-linejoin="round"/>
<g clip-path="url(#{ident}w)">
<rect x="300" y="360" width="424" height="400" fill="url(#{ident}s)"/>
<circle cx="566" cy="566" r="96" fill="#fff4dc" fill-opacity=".55"/>
<circle cx="566" cy="566" r="62" fill="#fffaf0"/>
<path d="M300 670C380 598 450 584 530 626S650 640 724 600V760H300Z" fill="{c['far']}"/>
<path d="M300 700C372 656 446 654 520 690S640 716 724 672V760H300Z" fill="url(#{ident}h)"/>
</g>
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

    # The installers' icons: one .ico for Windows (several sizes in one file)
    # and the PNG set the macOS build turns into an .icns with iconutil.
    (INSTALLERS / "windows").mkdir(parents=True, exist_ok=True)
    # Every size as a bitmap, not PNG: NSIS cannot read PNG-compressed icons.
    render(mark_svg("family"), 256).save(INSTALLERS / "windows" / "ninaivu.ico", "ICO", bitmap_format="bmp",
                                         sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    iconset = INSTALLERS / "macos" / "ninaivu.iconset"
    iconset.mkdir(parents=True, exist_ok=True)
    mac = render(mark_svg("family", tile="mac"), 1024)
    for size in (16, 32, 128, 256, 512):
        mac.resize((size, size), Image.LANCZOS).save(iconset / f"icon_{size}x{size}.png", "PNG", optimize=True)
        mac.resize((size * 2, size * 2), Image.LANCZOS).save(iconset / f"icon_{size}x{size}@2x.png",
                                                            "PNG", optimize=True)
    print(f"icons written to {ICONS}, start-up screens to {SPLASH}, "
          f"and the installers' icons to {INSTALLERS}")


if __name__ == "__main__":
    main()
