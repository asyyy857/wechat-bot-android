# -*- coding: utf-8 -*-
"""工具层 —— 把「这件事该谁办」从硬编码 if 链升级成一张**工具表**。

★ 2026-10-12（用户让借鉴 B 站那几个开源项目）：AstrBot 有 Skills / MCP，
  LangBot 有插件 + 沙箱。我们原来只有两条路 —— `brain.classify_task()` 判
  SELF / ASK_AGENT，其余能力（读转发卡片、补读、识图、发表情包）全是
  `bot.handle_own` 里一串硬编码 if，**加一个能力就要动骨架**。

这里立一张表：每个工具写清「叫什么 / 干什么 / 什么时候该用 / 谁能用 /
多久能用一次 / 一天最多几次」。然后
  · `brain.pick_tool()` 只回**一个工具名**（max_tokens=8、温度 0，约 1 秒）；
  · bot 按名字分发 —— 以后加能力 = 表里加一行 + 一个 handler。

⚠️ 三条纪律（都是踩过坑才立起来的）：
  ① **本地快路径优先**：能一眼看出来的（关键词、补读、读卡片）继续走本地判断，
     工具路由只在本地判不出来时兜底 —— 省一次模型调用，也少一份不确定性；
  ② **会往群里发东西的工具默认只对主人开放**（`owner_only=True`）且带冷却/配额：
     群友 @ 它不能让机器人往群里发图（那件事有 `rules.yaml` 的显式规则兜着）；
  ③ **拿不准就 self**：多回一句废话远好过乱发一张图。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

FILE = "工具用量.json"


@dataclass(frozen=True)
class Tool:
    name: str
    desc: str                 # 给模型看：这个工具干什么
    when: str                 # 给模型看：什么时候该用它
    owner_only: bool = True   # 只有主人自己发的话才允许
    cooldown: float = 0.0     # 同一工具两次之间的最小间隔（秒）
    max_per_day: int = 0      # 每天最多用几次（0 = 不限）


#: 工具表。加新能力就往这里加一行，再在 bot 里写一个 handler。
TOOLS = (
    Tool("self", "自己回", "闲聊、接梗、互怼、常识、按人设发挥 —— 你能直接答的",
         owner_only=False),
    Tool("meme", "发一张表情包（用户自己攒的图库）",
         "对方**明确在要图/要表情包**，或者正在开玩笑、画面感很强适合甩一张图应景",
         owner_only=True, cooldown=45.0, max_per_day=12),
    Tool("agent", "把活儿交给后台的编程 agent（它能上网、能跑命令）",
         "需要实时信息（天气/新闻/比分）、上网查资料、读网页、跑命令、改文件、"
         "翻台账日志 —— 你是纯文本模型，这些自己办不到",
         owner_only=True),
)

BY_NAME = {t.name: t for t in TOOLS}


def describe(names=None, exclude=()) -> str:
    """拼给模型看的工具清单（`names` 为空表示全部）。"""
    out = []
    for t in TOOLS:
        if names is not None and t.name not in names:
            continue
        if t.name in exclude:
            continue
        out.append("%s —— %s（%s）" % (t.name, t.desc, t.when))
    return "\n".join(out)


def parse(reply: str, allowed=None) -> str:
    """把模型回的那句话解析成工具名；认不出来一律当 self。"""
    t = str(reply or "").strip().lower()
    for ch in "`'\"*。，,.:：\n\r ":
        t = t.replace(ch, " ")
    for tok in t.split():
        for name in BY_NAME:
            if tok == name:
                if allowed is None or name in allowed:
                    return name
    return "self"


# --------------------------------------------------------------------------
# 用量（落盘，重启不清零）
# --------------------------------------------------------------------------
def _load(path) -> dict:
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(path, d: dict) -> None:
    try:
        from .atomic import atomic_write_json
        atomic_write_json(path, d)
    except Exception:
        pass


def today(now: float = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now if now else time.time()))


def used_today(path, name: str, now: float = None) -> int:
    d = _load(path)
    day = d.get(today(now)) or {}
    try:
        return int(day.get(name, 0) or 0)
    except Exception:
        return 0


def last_at(path, name: str) -> float:
    try:
        return float((_load(path).get("_last") or {}).get(name, 0) or 0)
    except Exception:
        return 0.0


def allowed(tool, path, now: float = None) -> tuple:
    """``(能不能用, 原因)`` —— 冷却 / 每日配额都在这里判。"""
    now = float(now if now else time.time())
    if tool.cooldown > 0:
        gap = now - last_at(path, tool.name)
        if gap < tool.cooldown:
            return False, "冷却中（还要 %.0f 秒）" % (tool.cooldown - gap)
    if tool.max_per_day > 0 and used_today(path, tool.name, now) >= tool.max_per_day:
        return False, "今天已经用过 %d 次（上限 %d）" % (
            used_today(path, tool.name, now), tool.max_per_day)
    return True, ""


def bump(path, name: str, now: float = None) -> None:
    """记一次用量（用**之前**记，避免失败时反复重试烧配额）。"""
    now = float(now if now else time.time())
    d = _load(path)
    day = today(now)
    d.setdefault(day, {})
    if not isinstance(d[day], dict):
        d[day] = {}
    d[day][name] = int(d[day].get(name, 0) or 0) + 1
    last = d.get("_last")
    if not isinstance(last, dict):
        last = {}
    last[name] = now
    d["_last"] = last
    for k in sorted(k for k in d if k not in ("_last",))[:-7]:   # 只留最近 7 天
        d.pop(k, None)
    _save(path, d)


def stats(path) -> dict:
    d = _load(path)
    day = d.get(today()) or {}
    return {"today": day if isinstance(day, dict) else {},
            "last": d.get("_last") or {}}
