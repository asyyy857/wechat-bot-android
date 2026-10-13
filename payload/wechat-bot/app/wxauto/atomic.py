# -*- coding: utf-8 -*-
"""原子写文件 —— 一个地方管住"写一半被读到"的问题。

为什么需要它：这套机器人的几个关键文件是**一边写、另一边每轮读**的
（`rules.yaml` 被 bot 每轮看 mtime 后重载、`瓜词表.json` 被打分时读、
`ppt_seen.json` 被扫描时读）。而 `/sdcard` 是 FUSE（sdcardfs），
`os.replace` 的硬链接语义会 `EPERM` —— 于是历史上有几处直接
`open(path, "w")` 写，读到半截文件就会：
  · `rules.yaml` → YAML 解析失败（旧代码还把 mtime 基线推进了，改动从此不生效）
  · `瓜词表.json` / `ppt_seen.json` → json 坏掉，静默当空表处理

`wxauto/tuning.py` 早就踩过这个坑并写了兜底，这里把它抽成公共实现，
统一给 rule_tool / brain / ppt / chatlog 用（2026-10-08 审查修复 M3）。
"""

from __future__ import annotations

import os
import json
from pathlib import Path
from typing import Union


def atomic_write(path: Union[str, Path], data: str) -> None:
    """把 ``data`` 整体写到 ``path``：tmp 写完再替换，失败退化为直接覆盖。

    调用方拿到的保证：**读者要么看到旧内容、要么看到新内容，不会看到半截**。
    """
    p = Path(path)
    tmp = str(p) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(data)
    try:
        os.replace(tmp, p)
    except OSError:
        # /sdcard 上 os.replace 可能 EPERM —— 退化成直接覆盖写（尽力而为）
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(data)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def atomic_write_json(path: Union[str, Path], obj, *, indent: int = 2) -> None:
    """原子写 JSON（ensure_ascii=False，中文原样落盘）。"""
    atomic_write(path, json.dumps(obj, ensure_ascii=False, indent=indent) + "\n")
