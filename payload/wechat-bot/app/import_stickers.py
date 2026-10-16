# 把 zip 里的 161 张表情包导入 表情包\ ，文件名用它的中文描述
import json
import re
import shutil
import zipfile
from pathlib import Path

ROOT = Path(r"改成你的 wechat-bot/app 目录")        # 例：C:\Users\你的用户名\...\wechat-bot\app
STICKERS = ROOT / "表情包"
P = Path(r"改成你的贴纸 zip 路径")                  # 微信里收到的那个 zip

# 1) 清掉我造的占位图
removed = 0
for sub in list(STICKERS.iterdir()):
    if sub.is_dir():
        removed += len(list(sub.glob("*.png")))
        shutil.rmtree(sub)
print(f"删掉占位图 {removed} 张")

# 2) 解压 + 按描述命名
z = zipfile.ZipFile(P)
items = json.loads(z.read("custom_stickers.json").decode("utf-8"))

BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
used = {}
written = 0
skipped = []

for it in items:
    desc = (it.get("description") or "").strip() or "未命名"
    safe = BAD.sub("_", desc).strip().rstrip(".")      # Windows 不允许结尾的点
    if not safe:
        safe = "未命名"
    # 万一重名就加序号
    if safe in used:
        used[safe] += 1
        safe = f"{safe}_{used[safe]}"
    else:
        used[safe] = 0

    try:
        data = z.read(it["fileName"])
    except KeyError:
        skipped.append(it["fileName"])
        continue
    (STICKERS / f"{safe}.png").write_bytes(data)
    written += 1

print(f"导入 {written} 张，失败 {len(skipped)} 张")
print()
print("=" * 70)
print("现在 表情包\\ 里是什么样")
print("=" * 70)
files = sorted(STICKERS.glob("*.png"))
print(f"根目录：{len(files)} 张")
print()
for i, f in enumerate(files, 1):
    print(f"{f.stem}", end="   " if i % 5 else "\n")
print()
print()
total_kb = sum(f.stat().st_size for f in files) / 1024
print(f"总大小：{total_kb:.0f} KB，平均 {total_kb / max(1, len(files)):.0f} KB/张")
big = [f for f in files if f.stat().st_size > 500 * 1024]
print(f"超过 500KB 的：{len(big)} 张")
for f in big:
    print(f"   {f.stat().st_size / 1024:.0f} KB  {f.name}")
