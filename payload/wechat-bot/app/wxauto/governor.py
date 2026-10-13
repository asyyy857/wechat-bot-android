# -*- coding: utf-8 -*-
"""节流层：所有"要不要发"的判断都集中在这里。

这是防封号的核心。宁可少回，不要密集刷屏。
"""
from __future__ import annotations

import json
import os
import time
from collections import deque
from datetime import datetime
from typing import Deque, Dict, Optional, Tuple


def _parse_range(s: str) -> Optional[Tuple[int, int]]:
    """'02:00-08:00' -> (120, 480) 分钟。支持跨零点。"""
    try:
        a, b = str(s).split("-")
        ah, am = [int(x) for x in a.strip().split(":")]
        bh, bm = [int(x) for x in b.strip().split(":")]
        return ah * 60 + am, bh * 60 + bm
    except Exception:
        return None


# ★ 2026-10-08 晚（审查报告 §206）：发送时间戳**落盘**，重启不再清零配额。
#   `sent` 原来只在内存里 —— 机器人一重启，per_day（每天上限）就归零，调试期
#   一天重启七八次等于把这个闸门废掉。
#   状态文件跟项目其它状态（chat_log.json / 瓜词表.json …）一样放仓库根目录。
_HERE = os.path.dirname(os.path.abspath(__file__))
from .paths import DATA_DIR as _DATA_DIR
_STATE = os.path.join(str(_DATA_DIR), "governor_sent.json")
_STATE_MAXAGE = 86400          # 只留 24 小时内的：更早的对任何窗口都没用


class Governor:
    def __init__(self, cfg, clock=time.time) -> None:
        p = cfg.pace
        self.clock = clock
        self.min_gap = float(p.min_reply_gap)
        self.max_gap = float(p.max_reply_gap)
        self.per_minute = int(p.per_minute)
        self.per_hour = int(p.per_hour)
        self.per_day = int(p.per_day)
        self.burst_limit = int(p.burst_limit)
        self.burst_window = float(p.burst_window)
        # 2026-10-04：新增 reply_delay（秒 / 秒区间），顶替 human_delay 里那套
        # 「min_reply_gap/2 ~ max_reply_gap」的老算法（实测跑成 10~15 秒，太磨叽）。
        self.reply_delay = getattr(p, "reply_delay", None)
        self.quiet = [r for r in (_parse_range(x) for x in (p.quiet_hours or [])) if r]
        # 2026-10-05 用户要求：被 @ 时无视"时间限制"（最小间隔 / 本波连回上限），
        # 但**条数配额一条都不放** —— 回得快，但不会刷屏。开关见 config 的
        # pace.mention_bypass，设 false 就退回老行为（@ 也照样被时间闸门挡）。
        self.mention_bypass = bool(getattr(p, "mention_bypass", True))

        self.sent: Deque[float] = self._load_sent()   # 每次发送的时间戳（★ 从盘上恢复，见 _load_sent）
        self.last_sent = 0.0
        self.rule_last: Dict[str, float] = {}
        self._burst_start = 0.0
        self._burst_count = 0

    # ------------------------------------------------------------ 时段
    def in_quiet_hours(self, now: Optional[datetime] = None) -> bool:
        now = now or datetime.now()
        cur = now.hour * 60 + now.minute
        for a, b in self.quiet:
            if a <= b:
                if a <= cur < b:
                    return True
            else:  # 跨零点，例如 23:30-08:00
                if cur >= a or cur < b:
                    return True
        return False

    # ------------------------------------------------------------ 配额
    def _load_sent(self) -> Deque[float]:
        """从盘上恢复**近 24 小时**的发送时间戳（★ 2026-10-08 晚）。

        读失败一律当空队列 —— 退化成"重启即清零"的老行为，
        绝不让落盘的问题影响机器人启动。
        """
        d: Deque[float] = deque(maxlen=2000)
        try:
            with open(_STATE, encoding="utf-8") as fh:
                data = json.load(fh)
            now = self.clock()
            for t in data:
                t = float(t)
                if 0 <= now - t < _STATE_MAXAGE:
                    d.append(t)
        except Exception:
            pass
        return d

    def _save_sent(self) -> None:
        """把时间戳原子落盘。失败静默 —— 记账不能反过来拖垮发送。"""
        try:
            from .atomic import atomic_write_json
            atomic_write_json(_STATE, list(self.sent), indent=0)
        except Exception:
            pass

    def _count_since(self, seconds: float) -> int:
        t = self.clock() - seconds
        return sum(1 for x in self.sent if x >= t)

    def check(self, mention: bool = False) -> Tuple[bool, str]:
        """能不能发？返回 (是否允许, 原因)。

        ``mention=True`` 表示这条是别人 **@ 我**。2026-10-05 用户要求：被 @ 时
        无视那些"时间限制"闸门（静默时段 / 最小回复间隔 / 本波连回上限），
        免得群里几个人积极 @ 你的时候，第二、第三个人被机械间隔挡掉、看起来像装死。
        **但防刷屏的条数配额一条都不放**（每分钟 / 每小时 / 每天）——
        被 @ 也只是回得快一点，不会连珠炮，更不会把配额烧穿。
        """
        bypass = mention and self.mention_bypass

        if self.in_quiet_hours() and not bypass:
            return False, "静默时段"

        now = self.clock()
        gap = now - self.last_sent
        if not bypass and self.last_sent and gap < self.min_gap:
            return False, f"距上次回复仅 {gap:.1f}s（下限 {self.min_gap}s）"

        # ↓ 以下三条是"不要刷屏"的硬底线，被 @ 也不豁免
        if self._count_since(60) >= self.per_minute:
            return False, f"每分钟上限 {self.per_minute}"
        if self._count_since(3600) >= self.per_hour:
            return False, f"每小时上限 {self.per_hour}"
        if self._count_since(86400) >= self.per_day:
            return False, f"每天上限 {self.per_day}"

        # 一波消息里的连续回复限制（被 @ 时豁免，见上面的 bypass）
        if bypass or now - self._burst_start > self.burst_window:
            return True, "ok"
        if self._burst_count >= self.burst_limit:
            return False, f"本波已连回 {self._burst_count} 条（上限 {self.burst_limit}）"
        return True, "ok"

    def rule_allowed(self, rule_name: str, cooldown: float) -> Tuple[bool, str]:
        if cooldown <= 0:
            return True, "ok"
        last = self.rule_last.get(rule_name, 0.0)
        if last and self.clock() - last < cooldown:
            return False, f"规则「{rule_name}」冷却中"
        return True, "ok"

    # ------------------------------------------------------------ 记账
    def record(self) -> None:
        now = self.clock()
        self.sent.append(now)
        self._save_sent()          # ★ 落盘（每条一次，频率很低，失败静默）
        self.last_sent = now
        if now - self._burst_start > self.burst_window:
            self._burst_start = now
            self._burst_count = 1
        else:
            self._burst_count += 1

    def record_rule(self, rule_name: str) -> None:
        self.rule_last[rule_name] = self.clock()

    # ------------------------------------------------------------ 拟人延迟
    def human_delay(self, rng) -> float:
        """发之前随机等一会儿，模拟人在打字。

        ``pace.reply_delay`` 优先（写成 ``[5, 7]`` 区间或单个数 ``6``）；
        没配就回退老算法 ``min_reply_gap/2 ~ max_reply_gap``。
        """
        rd = self.reply_delay
        if rd not in (None, "", []):
            try:
                vals = [float(x) for x in (rd if isinstance(rd, (list, tuple)) else [rd])]
                if vals:
                    return rng.uniform(min(vals), max(vals))
            except (TypeError, ValueError):
                pass        # 配错了就当没配，别让一行配置把发送卡死
        lo = max(0.0, self.min_gap * 0.5)
        hi = max(lo + 0.1, self.max_gap)
        return rng.uniform(lo, hi)

    def stats(self) -> str:
        return (f"近1分钟 {self._count_since(60)}/{self.per_minute}"
                f"  近1小时 {self._count_since(3600)}/{self.per_hour}"
                f"  近24小时 {self._count_since(86400)}/{self.per_day}")
