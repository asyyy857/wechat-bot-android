# -*- coding: utf-8 -*-
"""说出去的话 + 拒稿反馈（2026-10-05 用户要求；2026-10-07 删掉待审草稿）。

主人原话：

    让他选感兴趣的[话]，然后自己生成，但是我可以选择让不让他发。
    不让发的就是我没看上的，就下次注意不要这样说。

⚠️ **2026-10-07 用户拍板删掉「待审草稿」那条线**（原 ``drafts.json`` +
``drafts_overflow.jsonl`` 环形覆盖归档）：``behavior.proactive_window`` 从
2026-10-06 起变成**全天**，机器人看到值得接的话**当场就发**，「只在窗口外备稿」
那道闸门永远不成立 —— 那条线早就是死的（交接说明 §41 / §46.8）。
现在这个模块只剩两件事：

1. **说过的话**（``sent.jsonl``）—— 用户 2026-10-05 说：
   「发出去的也能记住对吧？别光记不发的……我心中理想情况下的话」。
   所以**每一条真发到群里的话**都记一笔，并标清这是谁的意思：
   面板上主人点的（``approve`` / ``edited`` / ``manual``）还是机器人自己发的
   （``rule`` / ``mention`` / ``proactive`` / ``reply``）。``edited`` 最有价值 ——
   原稿和主人改完的样子都在，一对比就知道"理想的说法"差在哪。

2. **拒稿记录**（``rejects.jsonl``）—— 主人点「不发」（控制面板实战台那条路）
   就是"我没看上"。最近若干条会被 ``Brain.system_prompt`` 注入提示词，
   让它下次**别再这么说**。追加写，超过 ``REJECT_KEEP`` 条就重写成最近的那些。

设计上刻意"哑"：所有函数都不抛异常给调用方 —— 记录挂了绝不能影响机器人正常回话。
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import time
from typing import Dict, List, Optional

from .paths import DATA_DIR as _DATA_DIR

ROOT = str(_DATA_DIR)
REJECTS_PATH = os.path.join(ROOT, "rejects.jsonl")
SENT_PATH = os.path.join(ROOT, "sent.jsonl")
LOCK_PATH = os.path.join(ROOT, "drafts.lock")

REJECT_KEEP = 200          # rejects.jsonl 最多留多少条（超了重写）
SENT_KEEP = 500            # sent.jsonl 同上（"说出去的话"流水，攒人设素材用）
REJECT_HINT_MAX = 10       # 喂给模型的拒稿条数
REJECT_HINT_CHARS = 40     # 每条截断长度


# ---------------------------------------------------------------- 小工具
@contextlib.contextmanager
def _locked():
    """跨进程互斥。拿不到锁（比如文件系统不支持 flock）就照常干，不报错。"""
    fh = None
    try:
        fh = open(LOCK_PATH, "a+")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    except Exception:
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass
            fh = None
    try:
        yield
    finally:
        if fh is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                fh.close()
            except Exception:
                pass


def _norm(s) -> str:
    """归一化：只留汉字/字母/数字，用来判断"是不是同一句"。"""
    return re.sub(r"[^\w\u4e00-\u9fff]", "", str(s or "")).lower()


# ---------------------------------------------------------------- 拒稿反馈
def note_reject(text: str, src: str = "", who: str = "", why: str = "") -> bool:
    """记一条「主人没看上这个说法」。返回是否真记下了。

    只追加，不抛异常 —— 面板点一下「不发」，绝不能因为写文件失败而报错。
    """
    text = str(text or "").strip()
    if not text:
        return False
    row = {"ts": round(time.time(), 3), "text": text,
           "src": str(src or "").strip(), "who": str(who or "").strip(),
           "why": str(why or "").strip()}
    try:
        with _locked():
            with open(REJECTS_PATH, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            _trim_rejects()
        return True
    except Exception:
        return False


def _trim_rejects() -> None:
    """超过 REJECT_KEEP 条就只留最近的（在锁里调，别在外面调）。"""
    try:
        with open(REJECTS_PATH, encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    except Exception:
        return
    if len(lines) <= REJECT_KEEP:
        return
    keep = lines[-REJECT_KEEP:]
    tmp = REJECTS_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(keep) + "\n")
        os.replace(tmp, REJECTS_PATH)
    except Exception:
        pass


def recent_rejects(limit: int = REJECT_HINT_MAX) -> List[str]:
    """最近被否掉的说法（去重、按新→旧）。给提示词用。"""
    try:
        with open(REJECTS_PATH, encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    except Exception:
        return []
    out: List[str] = []
    seen = set()
    for ln in reversed(lines):
        try:
            row = json.loads(ln)
        except Exception:
            continue
        t = str(row.get("text") or "").strip()
        k = _norm(t)
        if not t or k in seen:
            continue
        seen.add(k)
        out.append(t[:REJECT_HINT_CHARS])
        if len(out) >= max(1, int(limit)):
            break
    return out


def rejects_stamp() -> float:
    """rejects.jsonl 的 mtime —— Brain 用它决定要不要重拼 system prompt。"""
    try:
        return os.path.getmtime(REJECTS_PATH)
    except Exception:
        return 0.0


def stats() -> Dict:
    return {"rejects": len(_read_jsonl(REJECTS_PATH, REJECT_KEEP)),
            "sent": len(_read_jsonl(SENT_PATH, SENT_KEEP)),
            "sent_user": len([r for r in _read_jsonl(SENT_PATH, SENT_KEEP)
                              if r.get("origin") in ("approve", "edited", "manual")])}


# ---------------------------------------------------------------- 说过的话（人设素材）
VALID_ORIGINS = ("approve", "edited", "manual", "rule", "mention", "proactive", "reply")

ORIGIN_CN = {
    "approve": "你点头发的（原稿没改）",
    "edited": "你改过再发的",
    "manual": "你自己手写的",
    "rule": "它按关键词自动回的",
    "mention": "别人 @ 它，它回的",
    "proactive": "它深夜主动插的话",
    "reply": "它自动回的",
}


def note_sent(text: str, origin: str = "user", src: str = "",
              who: str = "", draft: str = "") -> bool:
    """记一条**真的发到群里**的话。返回是否记下了。

    用户 2026-10-05：「发出去的也能记住对吧？别光记不发的，到时候方便总结这个人设
    符合……我心中理想情况下的话」。所以这里是**素材库**，不是"好话榜"——
    机器人自动发的也照记，只是 origin 不同，面板上分开展示。

    ``draft`` 传"机器人原本写的稿"（如果这条来自备稿）。主人改过的话，
    ``text != draft``，这个对比就是最值钱的东西。
    """
    text = str(text or "").strip()
    if not text:
        return False
    origin = str(origin or "reply")
    if origin not in VALID_ORIGINS:
        origin = "reply"
    row = {"ts": round(time.time(), 3), "text": text, "origin": origin,
           "src": str(src or "").strip(), "who": str(who or "").strip(),
           "draft": str(draft or "").strip()}
    try:
        with _locked():
            with open(SENT_PATH, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            _trim(SENT_PATH, SENT_KEEP)
        return True
    except Exception:
        return False


def _trim(path: str, keep: int) -> None:
    """通用：文件超过 keep 行就只留最近的那些（在锁里调）。"""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    except Exception:
        return
    if len(lines) <= keep:
        return
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines[-keep:]) + "\n")
        os.replace(tmp, path)
    except Exception:
        pass


def _read_jsonl(path: str, limit: int = 0):
    try:
        with open(path, encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    except Exception:
        return []
    if limit:
        lines = lines[-limit:]
    out = []
    for ln in lines:
        try:
            row = json.loads(ln)
        except Exception:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def recent_sent(limit: int = 60, only_user: bool = False) -> List[Dict]:
    """最近发出去的话（新→旧）。``only_user=True`` 只看主人点/改/写的那几条。"""
    rows = _read_jsonl(SENT_PATH, SENT_KEEP)
    if only_user:
        rows = [r for r in rows if r.get("origin") in ("approve", "edited", "manual")]
    rows = rows[-max(1, int(limit)):]
    rows.reverse()
    return rows


def recent_rejects_full(limit: int = 60) -> List[Dict]:
    """带完整字段的否稿记录（面板展示用）。"""
    rows = _read_jsonl(REJECTS_PATH, REJECT_KEEP)
    rows = rows[-max(1, int(limit)):]
    rows.reverse()
    return rows

