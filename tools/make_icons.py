#!/usr/bin/env python3
"""生成应用图标（不依赖外部素材）。

用法：python tools/make_icons.py
产出：
    ICON.PNG            64x64
    ICON_256.PNG        256x256
    app/ui/images/icon-64.png
    app/ui/images/icon-256.png
"""
import os
import sys

try:
    from PIL import Image, ImageDraw, ImageFilter
except ImportError:
    sys.exit("需要 Pillow：pip install pillow")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SS = 4  # 超采样倍数

BG_TOP = (28, 38, 58)
BG_BOTTOM = (11, 16, 26)
BODY = (255, 74, 92)
BODY_DARK = (206, 45, 66)
TIP = (238, 242, 250)


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def render(size):
    S = size * SS
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # 背景渐变
    grad = Image.new("RGB", (1, S))
    gd = ImageDraw.Draw(grad)
    for y in range(S):
        gd.point((0, y), lerp(BG_TOP, BG_BOTTOM, y / float(S - 1)))
    grad = grad.resize((S, S))
    img.paste(grad, (0, 0))

    # 圆角遮罩
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1],
                                           radius=int(S * 0.22), fill=255)
    img.putalpha(mask)

    # 顶部高光
    shine = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shine)
    sd.ellipse([-S * 0.35, -S * 0.55, S * 1.35, S * 0.34], fill=(255, 255, 255, 20))
    img = Image.alpha_composite(img, shine)

    # 磁铁形状
    cx, cy = S * 0.5, S * 0.47
    R = S * 0.245
    th = S * 0.165
    leg = S * 0.225

    glyph = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    g = ImageDraw.Draw(glyph)

    # 阴影
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    sh = ImageDraw.Draw(shadow)
    sh.arc([cx - R - th / 2, cy - R - th / 2, cx + R + th / 2, cy + R + th / 2],
           180, 360, fill=(0, 0, 0, 130), width=int(th))
    sh.rectangle([cx - R - th / 2, cy, cx - R + th / 2, cy + leg], fill=(0, 0, 0, 130))
    sh.rectangle([cx + R - th / 2, cy, cx + R + th / 2, cy + leg], fill=(0, 0, 0, 130))
    shadow = shadow.filter(ImageFilter.GaussianBlur(S * 0.035))
    glyph = Image.alpha_composite(glyph, shadow)
    g = ImageDraw.Draw(glyph)

    # 本体（半环 + 两条腿）
    g.arc([cx - R - th / 2, cy - R - th / 2, cx + R + th / 2, cy + R + th / 2],
          180, 360, fill=BODY, width=int(th))
    g.rectangle([cx - R - th / 2, cy, cx - R + th / 2, cy + leg], fill=BODY)
    g.rectangle([cx + R - th / 2, cy, cx + R + th / 2, cy + leg], fill=BODY)

    # 右侧暗部，做出立体感（只作用在腿上，避免在弧形上探头）
    g.rectangle([cx + R - th * 0.20, cy, cx + R + th / 2, cy + leg], fill=BODY_DARK)
    g.arc([cx - R - th / 2, cy - R - th / 2, cx + R + th / 2, cy + R + th / 2],
          265, 352, fill=BODY_DARK, width=int(th * 0.3))

    # 白色磁极
    tip_h = S * 0.085
    g.rectangle([cx - R - th / 2, cy + leg - tip_h, cx - R + th / 2, cy + leg], fill=TIP)
    g.rectangle([cx + R - th / 2, cy + leg - tip_h, cx + R + th / 2, cy + leg], fill=TIP)

    img = Image.alpha_composite(img, glyph)
    return img.resize((size, size), Image.LANCZOS)


def main():
    targets = [
        (64, [os.path.join(ROOT, "ICON.PNG"),
              os.path.join(ROOT, "app", "ui", "images", "icon-64.png")]),
        (256, [os.path.join(ROOT, "ICON_256.PNG"),
               os.path.join(ROOT, "app", "ui", "images", "icon-256.png")]),
    ]
    for size, paths in targets:
        im = render(size)
        for p in paths:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            im.save(p, "PNG", optimize=True)
            print("写入", p, os.path.getsize(p), "bytes")


if __name__ == "__main__":
    main()
