# 生成几张占位表情包（用户换成自己的图就行）
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(r"C:\Users\18863\Documents\deepseek-harness\default-workspace\wechat-auto\表情包")
FONT_PATH = r"C:\Windows\Fonts\msyh.ttc"
S = 240


def font(size):
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except Exception:
        return ImageFont.load_default()


def face(bg, skin, mouth="smile", text=""):
    img = Image.new("RGB", (S, S), bg)
    d = ImageDraw.Draw(img)
    d.ellipse([26, 26, S - 26, S - 26], fill=skin, outline=(120, 90, 30), width=5)
    # 眼睛
    d.ellipse([78, 92, 104, 118], fill=(50, 35, 15))
    d.ellipse([136, 92, 162, 118], fill=(50, 35, 15))
    # 嘴
    if mouth == "smile":
        d.arc([70, 118, 170, 186], start=15, end=165, fill=(50, 35, 15), width=7)
    elif mouth == "flat":
        d.rectangle([76, 152, 164, 162], fill=(50, 35, 15))
    elif mouth == "smirk":
        d.arc([96, 128, 190, 186], start=350, end=140, fill=(50, 35, 15), width=7)
    if text:
        f = font(30)
        box = d.textbbox((0, 0), text, font=f)
        d.text(((S - (box[2] - box[0])) / 2, S - 52), text,
               fill=(70, 50, 20), font=f)
    return img


def block(bg, fg, text):
    img = Image.new("RGB", (S, S), bg)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([22, 60, S - 22, S - 60], radius=26, fill=fg)
    f = font(38)
    box = d.textbbox((0, 0), text, font=f)
    d.text(((S - (box[2] - box[0])) / 2, (S - (box[3] - box[1])) / 2 - 12),
           text, fill=(255, 255, 255), font=f)
    return img


items = [
    ("开心", "示例1-哈哈哈.png", face((255, 246, 214), (255, 214, 90), "smile", "哈哈哈")),
    ("开心", "示例2-绷不住.png", face((255, 240, 235), (255, 190, 120), "smile", "绷不住")),
    ("无语", "示例1-沉默.png", face((238, 240, 243), (215, 220, 226), "flat", "...")),
    ("无语", "示例2-你说是就是.png", face((235, 238, 240), (210, 216, 222), "flat", "你说是就是")),
    ("嘲讽", "示例1-就这.png", face((255, 235, 235), (255, 180, 170), "smirk", "就这？")),
    ("摆烂", "示例1-算了.png", block((226, 232, 226), (140, 158, 140), "算了")),
    ("摆烂", "示例2-人老了.png", block((228, 234, 238), (128, 148, 162), "人老了")),
    ("开车", "示例1-你这个年龄段.png", block((255, 232, 240), (232, 140, 170), "懂自懂")),
]

for tag, name, img in items:
    d = ROOT / tag
    d.mkdir(parents=True, exist_ok=True)
    img.save(d / name)
    print(f"  {tag}/{name}")

print()
total = sum(1 for p in ROOT.rglob("*") if p.suffix.lower() in {".png", ".gif", ".jpg"})
print(f"共生成 {total} 张")
