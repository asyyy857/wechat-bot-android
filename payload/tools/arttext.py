#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""逆水寒风格艺术字渲染器（离线，PIL 实现）。

用法示例：
  python3 tools/arttext.py "逆水寒" -o /tmp/a.png                    # 默认：毛笔楷书 + 金字
  python3 tools/arttext.py "青崖书" --style bai --font kai -s 120
  python3 tools/arttext.py "剑影破晓" --vertical --style jin
风格 --style : jin 金字 / bai 白玉 / qing 青玉 / mo 墨迹
字体 --font  : shufa 毛笔楷书 / xingshu 行书 / xingkai 行楷 / caoshu 草书
               kai 楷体(霞鹜文楷) / hei 黑体(逆水寒游戏正文本体·苹方)
"""
import argparse
import os

from PIL import Image, ImageDraw, ImageFilter, ImageFont

DSH = os.environ.get("DSH_ROOT", "/sdcard/dsh")

FONTS = {
    "shufa":   DSH + "/fonts/MaShanZheng-Regular.ttf",   # 马善政毛笔楷书
    "xingshu": DSH + "/fonts/ZhiMangXing-Regular.ttf",   # 钟齐志莽行书
    "xingkai": DSH + "/fonts/LongCang-Regular.ttf",      # 龙藏行楷
    "caoshu":  DSH + "/fonts/LiuJianMaoCao-Regular.ttf",  # 柳建毛草
    "kai":     "/usr/share/fonts/truetype/lxgw-wenkai/LXGWWenKai-Regular.ttf",
    "hei":     DSH + "/fonts/plugin_font.ttf",           # 逆水寒游戏内正文本体
}

# top/bottom = 字身渐变；stroke = 最外描边；ring = 内层亮边；glow = 外发光
STYLES = {
    "jin":  dict(top="#fff8dc", bottom="#dfa32a", stroke="#41260a",
                 ring="#ffeaa0", glow=(255, 196, 80, 165), sw=0.09, rw=0.035),
    "bai":  dict(top="#ffffff", bottom="#c9e0ff", stroke="#0e3559",
                 ring="#ffffff", glow=(130, 195, 255, 140), sw=0.09, rw=0.035),
    "qing": dict(top="#f0fff9", bottom="#6fcfae", stroke="#0b3a2e",
                 ring="#d9fff1", glow=(110, 240, 190, 140), sw=0.09, rw=0.035),
    "mo":   dict(top="#2a2a2a", bottom="#000000", stroke=None,
                 ring=None, glow=None, sw=0.0, rw=0.0),
}


def _rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def render(text, style="jin", font_key="shufa", size=140, vertical=False,
           bg=None, pad=None):
    st = STYLES[style]
    path = FONTS[font_key]
    font = ImageFont.truetype(path, size)
    sw = max(1, int(size * st["sw"])) if st["sw"] else 0
    rw = max(1, int(size * st["rw"])) if st["rw"] else 0
    pad = size // 2 if pad is None else pad

    lines = list(text.replace("\n", "")) if vertical else text.split("\n")
    probe = ImageDraw.Draw(Image.new("L", (4, 4)))
    ws, hs, offs = [], [], []
    for ln in lines:
        b = probe.textbbox((0, 0), ln, font=font, stroke_width=sw)
        ws.append(b[2] - b[0]); hs.append(b[3] - b[1]); offs.append((b[0], b[1]))

    gap = int(size * (0.06 if vertical else 0.22))
    W = max(ws) + 2 * sw + 2 * pad
    H = sum(hs) + gap * (len(lines) - 1) + 2 * sw + 2 * pad

    def paint(img, fill, stroke_fill=None, stroke_w=0):
        d = ImageDraw.Draw(img)
        y = pad
        for i, ln in enumerate(lines):
            x = pad + (max(ws) - ws[i]) // 2      # 居中
            d.text((x - offs[i][0], y - offs[i][1]), ln, font=font, fill=fill,
                   stroke_width=stroke_w, stroke_fill=stroke_fill)
            y += hs[i] + gap
        return img

    m_body = paint(Image.new("L", (W, H), 0), 255)                       # 字身
    m_edge = paint(Image.new("L", (W, H), 0), 0, 255, sw) if sw else None  # 外描边(critical)
    m_ring = paint(Image.new("L", (W, H), 0), 0, 255, rw) if rw else None  # 内亮边

    base = Image.new("RGBA", (W, H), _rgb(bg) + (255,) if bg else (0, 0, 0, 0))

    if st["glow"] and m_edge:                       # 外发光（最底层）
        layer = Image.new("RGBA", (W, H), st["glow"][:3] + (0,))
        layer.putalpha(m_edge.filter(ImageFilter.GaussianBlur(size * 0.14))
                       .point(lambda v: min(255, int(v * st["glow"][3] / 255))))
        base = Image.alpha_composite(base, layer)

    if m_edge:                                      # 深色描边
        layer = Image.new("RGBA", (W, H), _rgb(st["stroke"]) + (255,))
        layer.putalpha(m_edge)
        base = Image.alpha_composite(base, layer)

    if m_ring:                                      # 内层亮边
        layer = Image.new("RGBA", (W, H), _rgb(st["ring"]) + (255,))
        layer.putalpha(m_ring)
        base = Image.alpha_composite(base, layer)

    grad = Image.new("RGBA", (W, H))                # 字身渐变
    top, bot = _rgb(st["top"]), _rgb(st["bottom"])
    for y in range(H):
        t = y / max(1, H - 1)
        grad.paste((int(top[0] + (bot[0] - top[0]) * t),
                    int(top[1] + (bot[1] - top[1]) * t),
                    int(top[2] + (bot[2] - top[2]) * t), 255), (0, y, W, y + 1))
    grad.putalpha(m_body)
    return Image.alpha_composite(base, grad)


def main():
    ap = argparse.ArgumentParser(description="逆水寒风格艺术字渲染器")
    ap.add_argument("text")
    ap.add_argument("-o", "--out", default="/tmp/arttext.png")
    ap.add_argument("--style", default="jin", choices=list(STYLES))
    ap.add_argument("--font", default="shufa", choices=list(FONTS))
    ap.add_argument("-s", "--size", type=int, default=140)
    ap.add_argument("--vertical", action="store_true")
    ap.add_argument("--bg", default=None, help="背景色如 #101018，默认透明")
    a = ap.parse_args()
    img = render(a.text, a.style, a.font, a.size, a.vertical, a.bg)
    img.save(a.out)
    print(f"{a.out} {img.width}x{img.height}")


if __name__ == "__main__":
    main()
