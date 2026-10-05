# -*- coding: utf-8 -*-
"""生成程序图标（.ico + .png）。

风格：柔和紫粉渐变圆角方块 + 奶白月亮吉祥物（带腮红笑脸）+ 小星星。
纯 Pillow 绘制，超采样后缩放保证边缘平滑。
"""
import math
import os
import sys
import tempfile

from PIL import Image, ImageDraw

# 默认输出到系统临时目录下的 abicon（可用第一个命令行参数覆盖）
OUT_DIR = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    tempfile.gettempdir(), "abicon")
ICO = os.path.join(OUT_DIR, "app.ico")
PNG = os.path.join(OUT_DIR, "app.png")
os.makedirs(OUT_DIR, exist_ok=True)

SS = 8          # 超采样倍数
W = H = 256     # 输出尺寸


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def star_points(cx, cy, r_out, r_in, n=5, rot=-math.pi / 2):
    pts = []
    for i in range(n * 2):
        r = r_out if i % 2 == 0 else r_in
        ang = rot + i * math.pi / n
        pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    return pts


def build():
    S = W * SS
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # ---------- 背景：紫粉垂直渐变 + 圆角 ----------
    c_tl = (196, 168, 255)   # #C4A8FF 淡紫
    c_br = (255, 158, 196)   # #FF9EC4 粉
    grad = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    dg = ImageDraw.Draw(grad)
    for y in range(S):
        dg.line([(0, y), (S, y)],
                fill=lerp(c_tl, c_br, y / max(1, S - 1)) + (255,))

    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, S - 1, S - 1], radius=int(S * 0.235), fill=255)
    img.paste(grad, (0, 0), mask)

    # ---------- 内侧柔光（顶部高光，立体感） ----------
    gm = Image.new("L", (S, S), 0)
    ImageDraw.Draw(gm).ellipse([-int(S * 0.25), -int(S * 0.55),
                                int(S * 1.25), int(S * 0.55)], fill=46)
    gloss = Image.new("RGBA", (S, S), (255, 255, 255, 255))
    img.paste(gloss, (0, 0), gm)

    d = ImageDraw.Draw(img)
    # 细白边，糖果感
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.235),
                        outline=(255, 255, 255, 125),
                        width=max(2, int(S * 0.012)))

    # ---------- 月亮 ----------
    cx, cy, r = int(S * 0.415), int(S * 0.435), int(S * 0.205)

    # 用 mask 挖出月牙
    mm = Image.new("L", (S, S), 0)
    dm = ImageDraw.Draw(mm)
    dm.ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
    cut_cx, cut_cy, cut_r = cx + int(r * 0.60), cy - int(r * 0.32), int(r * 0.94)
    dm.ellipse([cut_cx - cut_r, cut_cy - cut_r, cut_cx + cut_r, cut_cy + cut_r],
               fill=0)
    img.paste(Image.new("RGBA", (S, S), (0, 0, 0, 0)), (0, 0), mm)

    d = ImageDraw.Draw(img)
    # 月亮本体（奶白）
    moon = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(moon).ellipse([cx - r, cy - r, cx + r, cy + r],
                                 fill=(255, 249, 238, 255))
    img.paste(moon, (0, 0), mm)
    d = ImageDraw.Draw(img)

    # ---------- 笑脸 ----------
    eye_r = max(2, int(S * 0.0170))
    ex_off = int(r * 0.36)
    ey = cy + int(r * 0.04)
    # 闭眼：向下弯的弧（甜甜的笑眼）
    for sgn in (-1, 1):
        ex = cx + sgn * ex_off
        d.arc([ex - eye_r * 1.6, ey - eye_r * 1.9,
               ex + eye_r * 1.6, ey + eye_r * 1.3],
              start=20, end=160, fill=(126, 94, 165, 255),
              width=max(2, int(S * 0.0145)))
    # 腮红
    blush_r = int(r * 0.21)
    for sgn in (-1, 1):
        bx = cx + sgn * int(r * 0.62)
        by = cy + int(r * 0.32)
        d.ellipse([bx - blush_r, by - int(blush_r * 0.70),
                   bx + blush_r, by + int(blush_r * 0.70)],
                  fill=(255, 150, 186, 150))
    # 小嘴
    mw = int(r * 0.19)
    my = cy + int(r * 0.22)
    d.arc([cx - mw, my - int(mw * 0.62), cx + mw, my + int(mw * 0.70)],
          start=20, end=160, fill=(158, 110, 186, 255),
          width=max(2, int(S * 0.0130)))

    # ---------- 星星 ----------
    def draw_star(x, y, rad, alpha, rot=0.0):
        d.polygon(star_points(x, y, rad, rad * 0.44, rot=rot),
                  fill=(255, 255, 255, alpha))

    draw_star(int(S * 0.760), int(S * 0.205), int(S * 0.060), 246, -0.35)
    draw_star(int(S * 0.860), int(S * 0.370), int(S * 0.038), 205, 0.25)
    draw_star(int(S * 0.672), int(S * 0.108), int(S * 0.029), 170, 0.60)
    draw_star(int(S * 0.138), int(S * 0.735), int(S * 0.032), 155, 0.15)

    # ---------- 底部云朵 ----------
    cyy = int(S * 0.848)
    cloud = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    dc = ImageDraw.Draw(cloud)
    for cxr, rr in ((0.355, 0.068), (0.468, 0.090), (0.585, 0.070), (0.672, 0.048)):
        dc.ellipse([int(S * (cxr - rr)), cyy - int(S * rr),
                    int(S * (cxr + rr)), cyy + int(S * rr)],
                   fill=(255, 255, 255, 215))
    dc.rounded_rectangle([int(S * 0.295), cyy - int(S * 0.026),
                          int(S * 0.715), cyy + int(S * 0.042)],
                         radius=int(S * 0.036), fill=(255, 255, 255, 215))
    img.alpha_composite(cloud)

    # ---------- 小光点 ----------
    for (fx, fy, fr, fa) in ((0.248, 0.212, 0.0125, 195),
                             (0.556, 0.702, 0.0100, 155),
                             (0.782, 0.632, 0.0130, 175),
                             (0.902, 0.182, 0.0090, 145),
                             (0.112, 0.428, 0.0100, 135)):
        rr = int(S * fr)
        d.ellipse([int(S * fx) - rr, int(S * fy) - rr,
                   int(S * fx) + rr, int(S * fy) + rr],
                  fill=(255, 255, 255, fa))

    return img.resize((W, H), Image.LANCZOS)


if __name__ == "__main__":
    im = build()
    im.save(ICO, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                        (128, 128), (256, 256)])
    im.save(PNG)
    print("ico:", ICO, os.path.getsize(ICO), "bytes")
    print("png:", PNG, os.path.getsize(PNG), "bytes")