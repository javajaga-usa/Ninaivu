import os
from PIL import Image

def create_admin_icon():
    im = Image.open('ninaivu/static/icons/icon-512.png').convert('RGBA')
    pixels = im.load()
    target_rgb = (109, 94, 252) # #6d5efc

    for y in range(im.height):
        for x in range(im.width):
            pr, pg, pb, pa = pixels[x, y]
            if pa > 0:
                # If pixel has color (not pure white)
                if not (pr > 245 and pg > 245 and pb > 245):
                    pixels[x, y] = (target_rgb[0], target_rgb[1], target_rgb[2], pa)

    out_dir = 'ninaivu/static/icons/admin'
    os.makedirs(out_dir, exist_ok=True)
    im.save(os.path.join(out_dir, 'icon-512.png'), 'PNG', optimize=True)

    # Generate 180 and 192
    im180 = im.resize((180, 180), Image.Resampling.LANCZOS)
    im180.save(os.path.join(out_dir, 'icon-180.png'), 'PNG', optimize=True)

    im192 = im.resize((192, 192), Image.Resampling.LANCZOS)
    im192.save(os.path.join(out_dir, 'icon-192.png'), 'PNG', optimize=True)

    # Maskable icon (with padding on background)
    maskable = Image.new('RGBA', (512, 512), (18, 22, 28, 255))
    padded_icon = im.resize((410, 410), Image.Resampling.LANCZOS)
    maskable.paste(padded_icon, ((512 - 410)//2, (512 - 410)//2), padded_icon)
    maskable.save(os.path.join(out_dir, 'icon-maskable-512.png'), 'PNG', optimize=True)
    print("Admin icons generated.")
    return im

def create_admin_splashes(admin_icon):
    bg_color = (18, 22, 28, 255)
    targets = [
        # iPhone 16 Pro Max (440x956 pt @3x)
        ('splash-1320x2868.png', 1320, 2868, 360),
        ('splash-2868x1320.png', 2868, 1320, 360),
        # iPhone 15 Pro Max / 16 Plus / 14 Pro Max (430x932 pt @3x)
        ('splash-1290x2796.png', 1290, 2796, 360),
        ('splash-2796x1290.png', 2796, 1290, 360),
        # iPhone 16 Pro (402x874 pt @3x)
        ('splash-1206x2622.png', 1206, 2622, 360),
        ('splash-2622x1206.png', 2622, 1206, 360),
        # iPhone 15 Pro / 14 Pro (393x852 pt @3x)
        ('splash-1179x2556.png', 1179, 2556, 360),
        ('splash-2556x1179.png', 2556, 1179, 360),
        # iPhone 14 Plus (and 13 Pro Max, 12 Pro Max - 428x926 pt @3x)
        ('splash-1284x2778.png', 1284, 2778, 360),
        ('splash-2778x1284.png', 2778, 1284, 360),
        # iPad 10.9" / 11" (iPad 10th gen, iPad Air 4/5/M2 - 820x1180 pt @2x)
        ('splash-1640x2360.png', 1640, 2360, 380),
        ('splash-2360x1640.png', 2360, 1640, 380),
        # iPad Pro 11" (834x1194 pt @2x)
        ('splash-1668x2388.png', 1668, 2388, 380),
        ('splash-2388x1668.png', 2388, 1668, 380),
        # iPad 10.2" (7th/8th/9th gen - 810x1080 pt @2x)
        ('splash-1620x2160.png', 1620, 2160, 360),
        ('splash-2160x1620.png', 2160, 1620, 360),
        # iPad Pro 12.9" / 13" (1024x1366 pt @2x)
        ('splash-2048x2732.png', 2048, 2732, 420),
        ('splash-2732x2048.png', 2732, 2048, 420),
        # iPad mini (744x1133 pt @2x)
        ('splash-1488x2266.png', 1488, 2266, 360),
        ('splash-2266x1488.png', 2266, 1488, 360),
    ]

    icons_dir = 'ninaivu/static/splash/admin'
    os.makedirs(icons_dir, exist_ok=True)
    for filename, w, h, icon_sz in targets:
        filename_clean = filename.replace('admin-', '')
        out_path = os.path.join(icons_dir, filename_clean)
        img = Image.new('RGBA', (w, h), bg_color)
        resized = admin_icon.resize((icon_sz, icon_sz), Image.Resampling.LANCZOS)
        x = (w - icon_sz) // 2
        y = (h - icon_sz) // 2
        img.paste(resized, (x, y), resized)
        img.save(out_path, 'PNG', optimize=True)
        print(f"Created {filename} ({w}x{h})")

    print("All admin splash screens generated.")

if __name__ == '__main__':
    icon = create_admin_icon()
    create_admin_splashes(icon)
