# -*- coding: utf-8 -*-
"""记忆库工具：看 / 加 / 删 / 试检索 / 从群聊流水里挖人名候选。

    python3 memory_tool.py --list
    python3 memory_tool.py --list --kind person
    python3 memory_tool.py --add person 某群友 "爱发水獭表情包"
    python3 memory_tool.py --add topic 玄狐洞 "新副本，5灵不够别碰首席" --alias 玄狐
    python3 memory_tool.py --del 某群友
    python3 memory_tool.py --search "有没有人打玄狐洞"
    python3 memory_tool.py --mine --days 7 --top 30

⚠️ `--add` / `--del` **会写文件**（跟 rule_tool.py 一个纪律：敲之前先想清楚）。
`--mine` 只统计、不写入、不联网、不花 Token —— 它吐的是**候选**，note 得人来填；
空 note 的条目不会被注入（见 wxauto/memory.py 的说明）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

_APP = Path(__file__).resolve().parent
sys.path.insert(0, str(_APP))
from wxauto.paths import DATA_DIR

BASE = DATA_DIR

from wxauto.memory import Memory, KIND_NAMES          # noqa: E402


def _chat_messages():
    p = BASE / "chat_log.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                return v
    return []


def cmd_list(mem: Memory, kind=None):
    rows = mem.items(kind)
    st = mem.stats()
    print("记忆库：%s（共 %d 条 · 人 %d · 梗 %d · 有内容 %d）\n"
          % (mem.path, st["total"], st["person"], st["topic"], st["with_note"]))
    for it in sorted(rows, key=lambda i: (i["kind"], -i["pri"], i["name"])):
        tag = KIND_NAMES.get(it["kind"], it["kind"])
        al = ("（别名：%s）" % "、".join(it["aliases"])) if it["aliases"] else ""
        note = it["note"] or "⚠️ 还没写内容（不会被注入）"
        print("  [%s] %s%s\n        %s" % (tag, it["name"], al, note))


def cmd_mine(mem: Memory, days: float, top: int):
    msgs = _chat_messages()
    since = time.time() - days * 86400
    cnt, last = Counter(), {}
    for m in msgs:
        if not isinstance(m, dict) or m.get("self"):
            continue
        try:
            ts = float(m.get("ts") or 0)
        except Exception:
            ts = 0.0
        if ts < since:
            continue
        s = str(m.get("sender") or "").strip()
        if len(s) < 2:
            continue
        cnt[s] += 1
        last[s] = max(last.get(s, 0.0), ts)
    known = set()
    for it in mem.items():
        known.add(it["name"])
        known.update(it["aliases"])
    rows = [(n, c) for n, c in cnt.most_common() if n not in known]
    print("最近 %.0f 天：群里出现过 %d 个昵称，其中 %d 个还没进记忆库\n"
          % (days, len(cnt), len(rows)))
    print("候选（按出现次数）：")
    for n, c in rows[:top]:
        when = time.strftime("%m-%d %H:%M", time.localtime(last.get(n, 0)))
        print("  %-18s %3d 次   最近 %s" % (n, c, when))
    if not rows:
        print("  （没有新候选 —— 要么都记过了，要么流水里 sender 是空的）")
    print("\n想记就把上面某个名字配一句说明加进去，例如：\n"
          "  python3 memory_tool.py --add person 名字 \"他常聊什么 / 口头禅 / 什么梗出自他\"")


def main():
    ap = argparse.ArgumentParser(description="机器人记忆库工具", add_help=True)
    ap.add_argument("--list", action="store_true", help="列出全部记忆")
    ap.add_argument("--kind", choices=("person", "topic"), help="配合 --list 过滤")
    ap.add_argument("--add", nargs=3, metavar=("KIND", "NAME", "NOTE"),
                    help="加一条：--add person 某群友 \"爱发水獭表情包\"")
    ap.add_argument("--alias", action="append", default=[], help="别名，可重复 --alias")
    ap.add_argument("--pri", type=float, default=0, help="优先级（越大越靠前）")
    ap.add_argument("--del", dest="delete", metavar="名字", help="删除（按名字/别名）")
    ap.add_argument("--search", metavar="文本", help="试一下：这段消息会注入哪几条")
    ap.add_argument("--mine", action="store_true", help="从群聊流水挖人名候选（只统计）")
    ap.add_argument("--days", type=float, default=7, help="配合 --mine：看最近几天")
    ap.add_argument("--top", type=int, default=30, help="配合 --mine：最多列几个")
    ap.add_argument("--file", default=None, help="记忆库文件（默认 记忆库.json）")
    args = ap.parse_args()

    mem = Memory(args.file) if args.file else Memory(BASE / "记忆库.json")

    if args.add:
        kind, name, note = args.add
        r = mem.add(kind, name, note, aliases=args.alias, pri=args.pri)
        print({"added": "✅ 已加", "merged": "✅ 已合并到已有条目"}.get(r, "❌ 没加成"))
        return
    if args.delete:
        n = mem.remove(args.delete)
        print("✅ 删掉 %d 条" % n if n else "⚠️ 没找到匹配的条目")
        return
    if args.search:
        lines = mem.search(args.search)
        print("这段消息会注入 %d 条：" % len(lines))
        for l in lines:
            print("  " + l)
        if not lines:
            print("  （没有命中：名字没出现，或者命中的条目 note 还是空的）")
        return
    if args.mine:
        cmd_mine(mem, args.days, args.top)
        return
    cmd_list(mem, args.kind)


if __name__ == "__main__":
    main()
