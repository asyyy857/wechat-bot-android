# -*- coding: utf-8 -*-
"""群聊流水：机器人每轮读屏顺手把消息落盘，给控制面板显示「群里在说什么」。

为什么是落盘、而不是让面板自己去读屏：

* 机器人本来就每 20 秒读一次目标群，顺手记一笔几乎零成本；
* 桥接的 ``/snapshot`` 只有"当前屏幕上那几条"，形不成记录；
* 面板进程自己去读屏，还要跟机器人抢桥接那把全局锁，划不来。

范围（2026-10-04 用户拍板）：**只记机器人待的那个群**，不碰别的会话。

落盘格式 ``chat_log.json``（原子写，``tmp`` + ``os.replace``）::

    {"chat": "示例群名",
     "seen": 1759550000.0,        # 机器人最后一次成功读到屏的时刻
     "updated": 1759550000.0,     # 最后一次写盘
     "messages": [{"ts": 1759550000.0, "sender": "张三",
                   "text": "猫猫", "self": false}, ...]}

注意：``ts`` 是**机器人看到这条消息**的时刻，不是微信里的发言时间 ——
截图上没有可靠、可解析的时间戳。只为"大概知道他们说了啥/多久之前"服务。
"""
from __future__ import annotations

import difflib
import json
import os
import re
import time
from collections import deque
from typing import Deque, Dict, List

# 昵称相容判据跟机器人去重共用一份（2026-10-09）：昵称 OCR 抖动是同一类问题，
# 两处各写一遍迟早会飘（见 state.names_same 的说明）。
from .state import names_same


def _msg_key(m: Dict) -> str:
    """去重指纹：谁 + 发了什么（自己的消息也带标记）。"""
    return "%d|%s|%s" % (1 if m.get("self") else 0,
                         m.get("sender") or "", m.get("text") or "")


def _norm_text(s) -> str:
    """归一化：只留汉字/字母/数字，把标点、空白、装饰符、emoji 全剥掉。"""
    return re.sub(r"[^\w\u4e00-\u9fff]", "", str(s or "")).lower()


def _fuzzy_dup(a: str, b: str) -> bool:
    """两句话是不是「同一条」（容忍 OCR 抖动）。

    ⚠️ 短句必须**完全相等**才算重复：群里同一个人连发两条「哈哈哈」是常事，
    按相似度去重会把第二条吃掉。只有长句才允许一两个字的抖动 —— RapidOCR
    对同一屏内容两次识别经常差一两个字（实测同一句读成「麻麻孖素据说…」和
    「麻麻孖素a据说…」），精确指纹认不出，面板上就冒出两条一模一样的。
    """
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) < 6 or len(b) < 6:
        return False
    if abs(len(a) - len(b)) > max(2, len(b) // 5):
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.85


class ChatLog:
    """环形缓冲 + 内容去重的一本"群聊流水账"。"""

    def __init__(self, path, limit: int = 1000, dedup: int = 60, chat_name: str = ""):
        self.path = str(path)
        self.limit = max(50, int(limit))
        # 面板上显示的群名：用 config 里的 target.group，而不是屏幕标题 ——
        # 虚拟屏只有 636 宽，标题会被截成「...111」，拿它当名字没法看。
        self.chat_name = str(chat_name or "")
        # 最近见过的指纹（跨帧去重：同一屏内容下一帧还在，不能重复记）
        self._fp: Deque[str] = deque(maxlen=max(20, int(dedup)))
        self._chat = ""
        self._seen = 0.0
        self._msgs: List[Dict] = []
        self._load()

    # ------------------------------------------------------------ 读写
    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:
            return
        self._chat = str(d.get("chat") or "")
        try:
            self._seen = float(d.get("seen") or 0)
        except Exception:
            self._seen = 0.0
        msgs = list(d.get("messages") or [])[-self.limit:]
        self._msgs = [m for m in msgs if isinstance(m, dict)]
        for m in self._msgs:
            self._fp.append(_msg_key(m))

    def _save(self) -> None:
        # ★ 2026-10-08 审查修复（M11）：os.replace 在 /sdcard（FUSE）上会 EPERM，
        #   原来那个 except 把失败**整个吞掉** —— 面板会一直显示旧数据而没人知道。
        #   现在走统一的 atomic_write（tmp → replace，失败退化为直接覆盖）。
        try:
            from .atomic import atomic_write_json
            atomic_write_json(self.path, {"chat": self._chat, "seen": self._seen,
                                          "updated": time.time(),
                                          "messages": self._msgs})
        except Exception:
            pass

    # ------------------------------------------------------------ 记录
    def record(self, snap) -> int:
        """把一帧里看到的消息并进流水，返回新记了几条。

        永远不抛异常给调用方（外层的 ``ChatBot._record_chat`` 还会再兜一层）。
        """
        items = getattr(snap, "messages", None) or []
        chat = str(getattr(snap, "chat_title", "") or "").strip()
        try:
            ts = float(getattr(snap, "captured_at", 0) or 0)
        except Exception:
            ts = 0.0
        ts = ts or time.time()
        added = 0
        _chat_before = self._chat
        _seen_before = self._seen
        # 去重只跟**本轮之前**记下的比。同一帧截图里出现两条一样的消息，说明群里
        # 真的发了两条（同一位置不可能同时有两条），不该合并 —— 所以精确指纹也要
        # 排掉本轮刚加进去的那些。
        start = len(self._msgs)
        prev_fp = set(self._fp)
        for m in items:
            text = str(getattr(m, "text", "") or "").strip()
            if not text:
                continue
            item = {"ts": round(ts, 3),
                    "sender": str(getattr(m, "sender", "") or "").strip(),
                    "text": text,
                    "self": bool(getattr(m, "is_self", False))}
            k = _msg_key(item)
            if k in prev_fp:
                continue
            # 精确指纹之外再兜一道模糊去重：同一句话在两轮读屏里 OCR 常差一两个字
            # （自己发的消息也会这样），精确指纹认不出，面板上就冒出重复条目。
            # 只跟**本轮之前最近 12 条**比：同一个人隔很久真的又说了一遍同样的话，该记。
            # ★ 2026-10-09：「换人了」的判据换成 names_same（容忍昵称 OCR 抖动）。
            #   原来要求昵称相等或互为子串，而昵称是灰字、抖得厉害 —— 实测同一条
            #   消息的 sender 读到「甜秋素问 / 甜湫素问 / 品淋系问 / 空」四种，
            #   每次都被当成"换人了"，于是台账（跟机器人）都冒出重复条目。
            nt = _norm_text(text)
            ns = _norm_text(item["sender"])
            dup = False
            for old in self._msgs[max(0, start - 12):start]:
                if bool(old.get("self")) != item["self"]:
                    continue
                if not names_same(_norm_text(old.get("sender")), ns):
                    continue          # 真是两个人，那就是另一条
                if _fuzzy_dup(_norm_text(old.get("text")), nt):
                    dup = True
                    break
            if dup:
                continue
            self._fp.append(k)
            self._msgs.append(item)
            added += 1
        if len(self._msgs) > self.limit:
            self._msgs = self._msgs[-self.limit:]
        if self.chat_name:
            self._chat = self.chat_name
        elif chat:
            self._chat = chat
        self._seen = ts
        # ★ 2026-10-08 审查修复（M11/C5）：原来**每一轮都全量重写**这个 95KB 的
        #   JSON（12 秒一帧 ≈ 每天 7000 次，而绝大多数轮次群里根本没有新消息），
        #   且 os.replace 在 /sdcard 上失败时被静默吞掉。
        #   现在：没有新消息、群名与"最后读到时间"也没变 → 不落盘；
        #   真要写就走原子写（内部对 /sdcard 有退化处理）。
        if added or self._chat != _chat_before or self._seen != _seen_before:
            self._save()
        return added

