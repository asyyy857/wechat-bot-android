# -*- coding: utf-8 -*-
"""特别关注名单（``follow.json``）—— 名单上的人一说话，机器人**立即回复**。

2026-10-16 用户要求：「微信机器人增加功能特别关注，按在面板上，机器人看见
特别关注的 id 会立即回复」。

设计要点：

* **id 就是昵称。** 机器人读屏（OCR）只拿得到群里显示的昵称，拿不到 wxid ——
  所以面板上填的是「TA 在群里的名字」，可以只填主名（「张三」能匹到
  「张三（今晚打本）」）。匹配口径复用 bot 的 ``_sender_matches``
  （变体相等 / 互为子串），比 ``state.names_same`` 严，免得「张三」匹到「张语」。
* **立即 = 必回 + 豁免时间闸门。** 名单上的人说话，等同于被 @ ——
  不靠关键词命中、不靠主动开口打分抽签，直接走模型生成并发送；
  节流层按 ``mention=True`` 处理（豁免静默时段 / 最小间隔 / 本波连回上限），
  但**每分钟 / 每小时 / 每天的条数配额一条都不放**（防封号的底线，同 @ 的待遇）。
* **热加载。** 面板写这个 JSON，机器人主循环每轮看一眼 mtime，一变就重载，
  **不用重启机器人**（同 ``tuning.json`` 的做法）。

字段（on-disk）::

    {
      "enabled": true,               # 总开关；false = 名单留着但不生效
      "ids": ["张三", "李四"],      # 名单（昵称，可只写主名）
      "updated": 1792100000.0        # 只给人看的时间戳
    }

这个文件永远可以安全删除 —— 删了就等于「没有特别关注」。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .paths import DATA_DIR

ROOT = DATA_DIR
FOLLOW_PATH = ROOT / "follow.json"

#: 单个名字最长字符数（昵称再长也不至于）；整份名单最多几条。
ID_MAX_LEN = 40
IDS_MAX = 200


def split_ids(raw) -> List[str]:
    """把面板上那坨文本（每行一个 / 逗号分隔）洗成规范名单。

    规则：去首尾空白、丢掉空行、**去重但保序**、每个名字截到
    :data:`ID_MAX_LEN`、整份最多 :data:`IDS_MAX` 条。
    也接受已经是 list 的输入（``follow.json`` 里就是 list）。
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        parts = [str(x) for x in raw]
    else:
        txt = str(raw).replace("\r\n", "\n").replace("\r", "\n")
        # 中英文逗号 / 顿号 / 分号 / 竖线都当分隔符 —— 用户爱怎么敲就怎么敲
        for ch in ("，", ",", "、", "；", ";", "|", "｜"):
            txt = txt.replace(ch, "\n")
        parts = txt.split("\n")

    out: List[str] = []
    seen = set()
    for one in parts:
        s = str(one).strip()
        if not s:
            continue
        s = s[:ID_MAX_LEN].strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= IDS_MAX:
            break
    return out


def clean(raw: Optional[dict], strict: bool = False) -> Dict[str, Any]:
    """把原始 dict 洗成规范形式。

    ⚠️ **空名单是合法的**（= 取消全部特别关注）：面板上把名字删光再保存，
    用户的意思就是"不要特别关注谁了"，这里报错反而让他清不掉
    （2026-10-16 写第一版时踩过，测试当场抓出来）。防手滑交给面板弹确认。
    ``strict`` 目前只保留给"将来要加的更严校验"，两条路都不抛错。
    """
    raw = raw if isinstance(raw, dict) else {}
    ids = split_ids(raw.get("ids"))
    enabled = raw.get("enabled", True)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() not in ("0", "false", "no", "off", "")
    return {"enabled": bool(enabled), "ids": ids}


def load_follow(path=None) -> Dict[str, Any]:
    """读 ``follow.json``。文件不存在 / 坏 JSON / 结构不对 → 当「没有特别关注」。"""
    p = Path(FOLLOW_PATH if path is None else path)
    try:
        with open(p, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception:
        return {"enabled": True, "ids": []}
    return clean(raw, strict=False)


def save_follow(ids, enabled: bool = True, path=None) -> Dict[str, Any]:
    """校验并写盘。校验失败抛 ``ValueError``，**不动**原来的文件。

    写盘用「临时文件写完 → 覆盖目标」两步（``/sdcard`` 是 FUSE，
    ``os.replace`` 的硬链接语义会 EPERM，同 ``tuning.py`` 的处理）。
    返回规范化后的结果。
    """
    p = Path(FOLLOW_PATH if path is None else path)
    data = clean({"ids": ids, "enabled": enabled}, strict=True)
    on_disk = {
        "enabled": data["enabled"],
        "ids": data["ids"],
        "updated": _now(),
    }
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(p) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(on_disk, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    try:
        os.replace(tmp, p)
    except OSError:
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(on_disk, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return data


def _now() -> float:
    import time
    return time.time()


def describe(data: Dict[str, Any]) -> str:
    """名单说成一句人话（面板和日志共用）。"""
    ids = (data or {}).get("ids") or []
    if not ids:
        return "没有特别关注的人"
    head = "、".join(ids[:5])
    more = "" if len(ids) <= 5 else " 等 %d 人" % len(ids)
    return "%d 人：%s%s" % (len(ids), head, more)
