# -*- coding: utf-8 -*-
"""从 chat_log.json + 群成员前缀 构建 名字库.json。

为什么要有名字库：实测 chat_log 里 **5~8% 的 sender 是「昵称和正文被 OCR 粘成
一个框」**（`群友I潮光：因为我们宿舍六点半大门才开`），同一个人在 memory 里被记成
几十个不同的人。有了名字库就能拿它做前缀切分，把昵称切出来。

数据来源两条：
  ① chat_log.json —— 机器人自己读过的历史（完整昵称，但它本身也是 OCR 来的，
     所以混着错字和噪声）；
  ② 群成员前缀.json —— 群成员页采集的**前缀**（那页昵称超 5 字会被截断成
     「十里共...」，所以只有前缀）。它的作用是**校验**：chat_log 里的可疑名字
     如果在群成员里找不到对应，多半是噪声（`聊天记录` / `转文字` / `售记录`）。

用法：
    python3 build_namebook.py            # 重建
    python3 build_namebook.py --show     # 只看结果
"""
import collections
import difflib
import io
import json
import os
import re
import sys

_APP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _APP)
from wxauto.paths import DATA_DIR

BASE = str(DATA_DIR)
CHAT = os.path.join(BASE, "chat_log.json")
PREFIX = os.path.join(BASE, "群成员前缀.json")
OUT = os.path.join(BASE, "名字库.json")

HIGH = 8        # 出现这么多次就免检（OCR 不会稳定地错这么多次）
SIM = 0.6       # 与群成员前缀的相似度下限


def norm(x):
    """归一化：去掉空格/标点/装饰符，只留字与数字字母。"""
    return re.sub(r"[^\w\u4e00-\u9fff]", "", str(x or ""))


# 微信 UI 上的固定字样 —— 它们偶尔会被当成昵称读进来（实测「聊天记录」x4）
UI_WORDS = {"聊天记录", "转文字", "售记录", "群公告", "群聊名称", "群二维码",
            "查找聊天记录", "消息免打扰", "置顶聊天", "保存到通讯录", "备注",
            "拍摄", "撤回", "查看更多", "更多群成员", "收起"}


def keep(name, count, prefixes):
    """这个名字值不值得进库。

    ⚠️ 2026-10-12 第一版太严（只认群成员前缀匹配），把「小楼微雨」(x4)、
    「赵恋凡」(x3) 这种真名也剔了 —— 群成员页只采到 79 个前缀（共 109 人），
    没采到的成员就匹配不上。改成：**先放宽中频短名，再用前缀校验低频名** ——
    宁可留几个噪声，也不要漏真名（漏了真名的后果是那条粘连名永远切不开）。
    """
    n = norm(name)
    if not n:
        return False
    if str(name).strip() in UI_WORDS:
        return False
    if len(n) > 10:                      # 太长：多半是粘连的正文 / 游戏截图文字
        return False
    if "：" in str(name) or ":" in str(name):
        return False                     # 「昵称：正文」的粘连体，切分后自然消失
    if count >= HIGH:
        return True                      # 高频免检
    if count >= 3 and len(n) <= 6:
        return True                      # 中频短名：宁可留
    for p in prefixes:
        pn = norm(p)
        if not pn:
            continue
        if n == pn:                      # 精确命中（含单字名）
            return True
        if len(n) < 2 or len(pn) < 2:
            continue
        if n.startswith(pn) or pn.startswith(n):
            return True
        if difflib.SequenceMatcher(None, n, pn).ratio() >= SIM:
            return True
    return False


def collect_senders():
    if not os.path.exists(CHAT):
        return []
    try:
        d = json.load(io.open(CHAT, encoding="utf-8"))
    except Exception as exc:
        print("! 读 chat_log 失败：%r" % (exc,))
        return []
    items = d.get("messages") if isinstance(d, dict) else d
    return [str(m.get("sender") or "").strip() for m in (items or [])
            if isinstance(m, dict)]


def main():
    prefixes = []
    if os.path.exists(PREFIX):
        try:
            prefixes = json.load(io.open(PREFIX, encoding="utf-8")).get("prefix") or []
        except Exception as exc:
            print("! 读群成员前缀失败：%r" % (exc,))
    senders = [s for s in collect_senders() if s]

    # 归一化聚合：同一个人的不同写法（群友I 潮光 / 群友I潮光）算一个
    agg = collections.Counter()
    best = {}
    for s in senders:
        k = norm(s)
        if not k:
            continue
        agg[k] += 1
        if k not in best or senders.count(s) > senders.count(best[k]):
            best[k] = s
    # 用精确计数修正（上面的 count 写法太慢且不准）
    raw = collections.Counter(senders)
    for k in list(best):
        best[k] = max((s for s in raw if norm(s) == k), key=lambda s: raw[s])

    names, noise = {}, []
    for k, n in agg.items():
        nm = best[k]
        if keep(nm, n, prefixes):
            names[nm] = n
        else:
            noise.append((nm, n))
    noise.sort(key=lambda x: -x[1])

    data = {
        "updated": int(__import__("time").time()),
        "group": json.load(io.open(PREFIX, encoding="utf-8")).get("group", "")
        if os.path.exists(PREFIX) else "",
        "src_msgs": len(senders),
        "src_prefix": len(prefixes),
        "names": dict(sorted(names.items(), key=lambda x: -x[1])),
        "noise": [n for n, _ in noise],
    }
    io.open(OUT, "w", encoding="utf-8").write(
        json.dumps(data, ensure_ascii=False, indent=1))

    print("消息 %d 条 → 归一化 %d 个名字" % (len(senders), len(agg)))
    print("进库 %d 个（含高频免检 %d 个）" % (
        len(names), sum(1 for v in names.values() if v >= HIGH)))
    print("剔除 %d 个（疑似噪声/错字）：" % len(noise))
    for nm, n in noise[:12]:
        print("   x%-2d %s" % (n, nm))
    print("\n名字库前 12：")
    for nm, n in list(names.items())[:12]:
        print("   x%-3d %s" % (n, nm))
    if "--show" in sys.argv:
        return
    print("\n已写 %s（%d 字节）" % (OUT, os.path.getsize(OUT)))


if __name__ == "__main__":
    main()
