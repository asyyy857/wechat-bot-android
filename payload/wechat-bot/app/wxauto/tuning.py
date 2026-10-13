# -*- coding: utf-8 -*-
"""运行时调参（``tuning.json``）—— 不重启机器人就能拧「读屏节奏」。

背景（2026-10-04 用户要求）：

* **读屏间隔**要能在 **10 秒 ~ 1 小时**之间自己调。原来它写死在 ``config.yaml``
  的 ``pace.poll_interval``，而 config 只在启动时读一次 —— 改一次就得重启机器人
  （切群 + 读屏预热要十几秒，那一整段还容易把微信挤下虚拟屏）。
* **回复延迟**（识别到消息之后等几秒再发）同样要能随手调。

做法：控制面板写这一个 JSON 文件，机器人主循环每轮看一眼（文件极小，
比一次 OCR 便宜好几个数量级），值一变就套用，最迟一个轮询间隔生效。

语义 —— 这个文件里的值**覆盖** ``config.yaml``：

    写成 ``null``、删掉某个键、或者把整个 ``tuning.json`` 删掉，
    都等于「这一项回到 config.yaml 的值」。

所以这个文件永远可以安全删除，删了机器人立刻回到配置文件里的节奏。
``config.yaml`` 只是"出厂默认值"，被覆盖时**不会**被改写。

字段（on-disk）::

    {
      "poll_interval": 20.0,     # 读屏间隔（秒），10 ~ 3600；null = 用 config
      "delay_min": 5.0,          # 回复延迟下限（秒），0 ~ 600；null = 用 config
      "delay_max": 7.0,          # 回复延迟上限（秒），0 ~ 600，且 >= delay_min
      "updated": 1791104837.5    # 只给人看的时间戳
    }
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .paths import DATA_DIR

ROOT = DATA_DIR
TUNING_PATH = ROOT / "tuning.json"

# 用户定的范围：读屏 10 秒 ~ 1 小时
POLL_MIN, POLL_MAX = 10.0, 3600.0
# 回复延迟：0 秒（立刻发）~ 10 分钟；上限再大也没意义（早被别的规则挡掉）
DELAY_LO, DELAY_HI = 0.0, 600.0


# ------------------------------------------------------------------ 小工具
def _num(v: Any) -> Optional[float]:
    """转 float。None / 空串 / 非法 / NaN / inf 一律返回 None。"""
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _req(raw: dict, key: str, name: str, strict: bool) -> Optional[float]:
    """取一个"用户明确填了"的字段。

    空 / 缺省 = 用户没填 → None（用 config.yaml）。
    填了但不是数字 → strict 时报错（否则用户会以为保存成功，其实是清掉了覆盖）。
    """
    v = raw.get(key)
    if v is None or v == "":
        return None
    f = _num(v)
    if f is None and strict:
        raise ValueError("%s 不是有效数字：%r" % (name, v))
    return f


def _rng(name: str, v: float, lo: float, hi: float, strict: bool) -> float:
    """越界处理：strict 报错（面板要告诉用户范围），否则截断（读盘时别崩）。"""
    if lo <= v <= hi:
        return v
    if strict:
        raise ValueError("%s 必须在 %g ~ %g 之间（收到 %g）" % (name, lo, hi, v))
    return max(lo, min(hi, v))


# ------------------------------------------------------------------ 读 / 写
def clean(raw: Optional[dict], strict: bool = False) -> Dict[str, Any]:
    """把原始 dict 洗成规范形式。

    strict=True  —— 越界 / 非法就抛 ``ValueError``（面板保存时用，用户要看原因）
    strict=False —— 静默截断 / 丢弃（机器人每轮读盘时用，坏文件绝不能让机器人崩）

    返回 ``{"poll_interval": float|None, "reply_delay": [lo, hi]|None}``。
    """
    raw = raw or {}
    out: Dict[str, Any] = {"poll_interval": None, "reply_delay": None}

    pi = _req(raw, "poll_interval", "读屏间隔", strict)
    if pi is not None:
        out["poll_interval"] = _rng("读屏间隔", pi, POLL_MIN, POLL_MAX, strict)

    lo = _req(raw, "delay_min", "回复延迟下限", strict)
    hi = _req(raw, "delay_max", "回复延迟上限", strict)
    if lo is not None or hi is not None:
        if lo is None:
            lo = hi
        if hi is None:
            hi = lo
        lo = _rng("回复延迟下限", lo, DELAY_LO, DELAY_HI, strict)
        hi = _rng("回复延迟上限", hi, DELAY_LO, DELAY_HI, strict)
        if hi < lo:
            if strict:
                raise ValueError("回复延迟上限（%g）不能小于下限（%g）" % (hi, lo))
            lo, hi = min(lo, hi), max(lo, hi)
        out["reply_delay"] = [lo, hi]

    return out


def load_tuning(path=None) -> Dict[str, Any]:
    """读 tuning.json。文件不存在 / 是坏 JSON 都当成"没有覆盖"，不报错。"""
    p = Path(path) if path else TUNING_PATH
    try:
        with open(p, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (FileNotFoundError, NotADirectoryError):
        return {"poll_interval": None, "reply_delay": None}
    except Exception:
        return {"poll_interval": None, "reply_delay": None}
    if not isinstance(raw, dict):
        return {"poll_interval": None, "reply_delay": None}
    return clean(raw, strict=False)


def save_tuning(poll_interval=None, delay_min=None, delay_max=None,
                path=None, extra: Optional[dict] = None) -> Dict[str, Any]:
    """校验并写盘（原子替换，机器人不会读到写了一半的文件）。

    校验失败抛 ``ValueError``，**不会**动原来的文件。
    返回规范化的结果（同 :func:`clean` 的返回值）。
    """
    p = Path(path) if path else TUNING_PATH
    data = clean({"poll_interval": poll_interval,
                  "delay_min": delay_min,
                  "delay_max": delay_max}, strict=True)

    rd = data["reply_delay"] or [None, None]
    on_disk = {
        "poll_interval": data["poll_interval"],
        "delay_min": rd[0],
        "delay_max": rd[1],
        "updated": time.time(),
    }
    if extra:
        on_disk.update(extra)

    p.parent.mkdir(parents=True, exist_ok=True)
    # ⚠️ /sdcard 是 FUSE（sdcardfs）：os.replace 的硬链接语义会 EPERM，
    #    所以这里退化成"临时文件写完 → 直接覆盖写目标"，两步都容错。
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


def clear_tuning(path=None) -> bool:
    """删掉 tuning.json → 两边都回到 config.yaml。返回是否真的删了。"""
    p = Path(path) if path else TUNING_PATH
    try:
        p.unlink()
        return True
    except FileNotFoundError:
        return False


# ------------------------------------------------------------------ 应用到配置
def snapshot_pace(cfg) -> Dict[str, Any]:
    """记下 config.yaml 里的原始节奏值（tuning 被清掉时要还原成它）。"""
    pace = getattr(cfg, "pace", None)
    if pace is None:
        return {"poll_interval": None, "reply_delay": None}
    return {"poll_interval": getattr(pace, "poll_interval", None),
            "reply_delay": getattr(pace, "reply_delay", None)}


def apply_to_cfg(cfg, t: Dict[str, Any],
                 orig: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """把 tuning 套到 ``cfg.pace`` 上；tuning 里为 None 的项回退到 ``orig``。

    返回**实际发生变化**的项，形如 ``{"poll_interval": 30.0, "reply_delay": [3, 8]}``
    （没变就不带这个键）—— 调用方拿它决定要不要打日志、同步给调速器。
    """
    pace = getattr(cfg, "pace", None)
    if pace is None:
        return {}
    orig = orig or {}
    applied: Dict[str, Any] = {}

    # --- 读屏间隔 ---
    want = t.get("poll_interval")
    if want is None:
        want = orig.get("poll_interval")
    if want is not None:
        cur = _num(getattr(pace, "poll_interval", None))
        if cur is None or abs(cur - float(want)) > 1e-9:
            pace.poll_interval = float(want)
            applied["poll_interval"] = float(want)

    # --- 回复延迟 ---
    want_rd = t.get("reply_delay")
    if want_rd is None:
        want_rd = orig.get("reply_delay")
    if want_rd is not None:
        want_list = [float(x) for x in want_rd]
        cur_rd = getattr(pace, "reply_delay", None)
        cur_list = [float(x) for x in cur_rd] if isinstance(cur_rd, (list, tuple)) else None
        if (cur_list is None or abs(cur_list[0] - want_list[0]) > 1e-9
                or abs(cur_list[-1] - want_list[-1]) > 1e-9):
            pace.reply_delay = want_list
            applied["reply_delay"] = want_list

    return applied


def summarize(applied: Dict[str, Any]) -> str:
    """把 :func:`apply_to_cfg` 的返回值说成人话（写日志用）。"""
    bits = []
    if "poll_interval" in applied:
        bits.append("读屏间隔 → %.0f 秒" % applied["poll_interval"])
    if "reply_delay" in applied:
        lo, hi = applied["reply_delay"][0], applied["reply_delay"][-1]
        bits.append("回复延迟 → %s" % ("%.0f 秒" % lo if abs(hi - lo) < 1e-9
                                      else "%.0f~%.0f 秒" % (lo, hi)))
    return "；".join(bits) if bits else "（无变化）"
