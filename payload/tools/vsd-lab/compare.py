# 把虚拟屏帧与物理屏截图缩到同一尺寸算平均灰度差，判断是否镜像物理屏
import sys, io
from PIL import Image, ImageChops

frame_path, phys_path = sys.argv[1], sys.argv[2]
f = Image.open(io.BytesIO(open(frame_path, 'rb').read())).convert('RGB')
p = Image.open(io.BytesIO(open(phys_path, 'rb').read())).convert('RGB')
print(f"  虚拟屏帧 {f.size}  物理屏 {p.size}  宽高比 {f.size[0]/f.size[1]:.3f} vs {p.size[0]/p.size[1]:.3f}")
size = (64, 64)
d = ImageChops.difference(f.resize(size), p.resize(size)).convert('L')
vals = list(d.getdata())
avg = sum(vals) / len(vals)
print(f"  64x64 平均灰度差 = {avg:.1f} / 255")
print(f"  判定：{'高度相似 → 疑似镜像物理屏' if avg < 12 else '明显不同 → 独立内容（未镜像）'}")
