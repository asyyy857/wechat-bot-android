#!/usr/bin/env python3
"""重新生成「交接说明-目录.md」：列出交接说明里每个 `## ` 小节的行号。
交接说明是按时间往后追加的，追加之后跑一次：python3 gen_handoff_index.py
"""
import os
here = os.path.dirname(os.path.abspath(__file__))
src = os.path.join(here, "交接说明-新会话接手.md")
dst = os.path.join(here, "交接说明-目录.md")
lines = open(src, encoding="utf-8").read().split("\n")
rows, fence = [], False
for i, l in enumerate(lines, 1):
    if l.startswith("```"):
        fence = not fence
    if not fence and l.startswith("## "):
        rows.append((i, l[3:].strip()))
out = ["# 交接说明 · 目录", "",
       f"> 自动生成（`python3 gen_handoff_index.py`）。原文 `交接说明-新会话接手.md` 共 {len(lines)} 行，",
       "> 别通读：先看下面「先读这几节」，其余按标题跳读（`read` 工具用 offset 跳到行号）。", "",
       "## 先读这几节", "",
       "- **§0 一句话现状**、**§3 一键起停**、**§4 十七个致命坑**、**§10 安全红线**",
       "- 其余多是按日期追加的修复流水，遇到对应问题再翻。", "",
       "## 备份在哪", "",
       "文档里写的 `xxx.bak-…` 备份已统一挪到 `历史备份/` 下：",
       "`历史备份/根目录/`（顶层文件）、`历史备份/wxauto/`、`历史备份/read/`、",
       "`历史备份/personas/`、`历史备份/技能文档/`。文档里写的回退命令，把文件名换成这些目录里的路径即可。", "",
       "## 全部小节", "", "| 行号 | 小节 |", "|---|---|"]
out += [f"| {n} | {t} |" for n, t in rows]
open(dst, "w", encoding="utf-8").write("\n".join(out) + "\n")
print("小节数", len(rows), "->", dst)
