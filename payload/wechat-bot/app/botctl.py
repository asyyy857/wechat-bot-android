#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""botctl.py —— 微信机器人控制面板（容器内 HTTP 服务）

手机上用浏览器打开 http://127.0.0.1:8790 就能看到状态和开关；
浏览器菜单「添加到主屏幕」之后就是一个桌面图标，点开即用。

为什么不做 Android app：容器和 Android **共享 127.0.0.1**（`phone` 桥 :3099、
adb :42427 都是这个机制），一个本地网页就够了 —— 不用装东西，改一行刷新生效。
"""
from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from wxauto.paths import (APP_DIR as _APP_DIR, DATA_DIR as _DATA_DIR,
                              LOGS_DIR as _LOGS_DIR)
except Exception:                       # 极端情况：包导入失败也别让面板起不来
    _APP_DIR = _Path(os.path.dirname(os.path.abspath(__file__)))
    _DATA_DIR = _Path("/sdcard/dsh/wechat-bot/data")
    _LOGS_DIR = _Path("/sdcard/dsh/wechat-bot/logs")

# ⚠️ 两个目录别搞混（v2.0.0 是"程序 / 数据"分离）：
#   DIR    = **数据**目录（chat_log.json / config.yaml / 表情包 / STOP.txt …）
#   APPDIR = **程序**目录（bot.sh / wxbridge_ctl.sh / simulate.py / wxauto 包 …）
# 2026-10-13 用户报「面板主动接话坏了」的根因就在这里：迁移时只把 DIR 指到
# data/，可**脚本路径和 `sys.path` 还挂在 DIR 上** → 子进程"文件不存在"、
# `import wxauto` 也失败，面板一半按钮静默坏掉（/fight/pick 直接 500）。
DIR = str(_DATA_DIR)
APPDIR = str(_APP_DIR)
PORT = int(os.environ.get("BOTCTL_PORT", "8790"))
BRIDGE = "http://127.0.0.1:8765"
PHONE = "http://127.0.0.1:3099"      # 无障碍桥：能问出物理屏前台是哪个 App
LOG = str(_LOGS_DIR / "console.log")
PAT = r"main_android\.py"
# ★ 2026-10-08 审查修复（S3）：面板的"会真的动手"的路由。它们统一要求同源来源，
#   且 main() 里只绑 127.0.0.1（同一个 WiFi 的别人连不上）。
_DESTRUCTIVE_PATHS = {
    "/start", "/dry", "/stop", "/reclaim", "/repair", "/adb", "/takeover",
    "/fight/send", "/fight/send_image", "/fight/grab", "/fight/clear", "/tune",
    "/tune/reset", "/api/learn", "/follow",
    # ★ 2026-10-12：知识/记忆卡片的写操作（加删记忆、手动跑摘要、开关向量服务）
    "/api/mem/add", "/api/mem/del", "/api/sum/run", "/api/vec",
}


# 面板代发的最小间隔（秒）：机器人那个 Governor 在另一个进程里，读不到它的内存账本，
# 所以面板侧只兜住"手滑连点"。正常使用（点一条、改一改、再发）完全无感。
_MANUAL = {"last": 0.0}
_MANUAL_MIN_GAP = 8.0


def _same_origin(origin: str) -> bool:
    """请求来源是不是"本机打开的面板"。

    浏览器直开 http://127.0.0.1:8790 时 Origin/Referer 是 127.0.0.1 / localhost
    （或干脆没有）；恶意网页替我们发的请求会带它自己的域名。只认前两种。
    """
    try:
        host = urllib.parse.urlparse(origin).hostname or ""
    except Exception:
        return False
    return host in ("127.0.0.1", "localhost", "::1", "")

# 机器人写的群聊流水（见 wxauto/chatlog.py）——「实战台」的消息列表就来自这里
CHAT_LOG = os.path.join(DIR, "chat_log.json")


# ---------------------------------------------------------------- 读屏节奏（运行时调参）
# 2026-10-04 用户要求：能自己拧「读屏间隔（10 秒 ~ 1 小时）」和「识别到消息之后
# 等几秒再发」。面板写 tuning.json，机器人每轮看一眼就套用 —— **不用重启机器人**。
# 这里只是面板侧的读写与显示，真正生效在 wxauto/tuning.py + wxauto/bot.py。
def _num_txt(x):
    """20.0 -> '20'，5.5 -> '5.5'（输入框里别显示多余的小数点）。"""
    try:
        f = float(x)
        return str(int(f)) if abs(f - int(f)) < 1e-9 else ("%g" % f)
    except (TypeError, ValueError):
        return ""


def tune_state():
    """当前生效的节奏 + 它来自哪儿（tuning.json 覆盖 config.yaml）。"""
    out = {"ok": True, "error": "",
           "poll": None, "poll_from": "config.yaml",
           "dmin": None, "dmax": None, "delay_from": "config.yaml",
           "slow_window": "", "slow_interval": None,
           "poll_min": 10, "poll_max": 3600, "delay_hi": 600}
    try:
        if APPDIR not in sys.path:
            sys.path.insert(0, APPDIR)
        from wxauto import tuning as T
        from wxauto.config import load_config
        out["poll_min"], out["poll_max"] = int(T.POLL_MIN), int(T.POLL_MAX)
        out["delay_hi"] = int(T.DELAY_HI)
        pace = load_config().pace
        t = T.load_tuning()
        if t["poll_interval"] is not None:
            out["poll"], out["poll_from"] = t["poll_interval"], "面板设定"
        else:
            out["poll"] = getattr(pace, "poll_interval", None)
        if t["reply_delay"] is not None:
            out["dmin"], out["dmax"] = t["reply_delay"][0], t["reply_delay"][-1]
            out["delay_from"] = "面板设定"
        else:
            rd = getattr(pace, "reply_delay", None)
            if isinstance(rd, (list, tuple)) and rd:
                out["dmin"], out["dmax"] = float(rd[0]), float(rd[-1])
        out["slow_window"] = str(getattr(pace, "slow_poll_window", "") or "")
        out["slow_interval"] = getattr(pace, "slow_poll_interval", None)
    except Exception as exc:
        out["ok"], out["error"] = False, str(exc)
    return out


def tune_save(body):
    """保存面板上的节奏设定。返回一句人话给用户看；参数非法抛 ValueError。"""
    if APPDIR not in sys.path:
        sys.path.insert(0, APPDIR)
    from wxauto import tuning as T
    data = T.save_tuning(poll_interval=body.get("poll_interval"),
                         delay_min=body.get("delay_min"),
                         delay_max=body.get("delay_max"))
    bits = []
    if data["poll_interval"] is not None:
        bits.append("每 %s 秒读一次屏" % _num_txt(data["poll_interval"]))
    if data["reply_delay"] is not None:
        lo, hi = data["reply_delay"]
        bits.append("回复延迟 %s 秒" % (_num_txt(lo) if abs(hi - lo) < 1e-9
                                       else "%s ~ %s" % (_num_txt(lo), _num_txt(hi))))
    return "、".join(bits) if bits else "两项都清空了（回到 config.yaml 的值）"


def tune_reset():
    """删掉 tuning.json → 读屏间隔和回复延迟都回到 config.yaml。"""
    if APPDIR not in sys.path:
        sys.path.insert(0, APPDIR)
    from wxauto import tuning as T
    return T.clear_tuning()


# ---------------------------------------------------------------- 特别关注（2026-10-16）
# 用户要求：「微信机器人增加功能特别关注，按在面板上，机器人看见特别关注的 id
# 会立即回复」。名单写 data/follow.json，机器人每轮看 mtime 热加载（wxauto/follow.py
# 负责读写，wxauto/bot.py 的 _is_followed 负责匹配）。
def follow_state():
    """面板「⭐ 特别关注」卡片要显示的东西：名单 + 开关。"""
    out = {"ok": True, "error": "", "enabled": True, "ids": [],
           "desc": "没有特别关注的人", "max": 200}
    try:
        if APPDIR not in sys.path:
            sys.path.insert(0, APPDIR)
        from wxauto import follow as F
        data = F.load_follow()
        out["enabled"] = bool(data.get("enabled", True))
        out["ids"] = list(data.get("ids") or [])
        out["desc"] = F.describe(data)
        out["max"] = int(F.IDS_MAX)
    except Exception as exc:
        out["ok"], out["error"] = False, str(exc)
    return out


def follow_save(body):
    """保存面板上的特别关注名单。返回一句人话；参数非法抛 ValueError。"""
    if APPDIR not in sys.path:
        sys.path.insert(0, APPDIR)
    from wxauto import follow as F
    enabled = body.get("enabled", True)
    data = F.save_follow(body.get("ids"), enabled=enabled)
    if not data["ids"]:
        return "名单已清空 —— 不再特别关注任何人"
    return "已保存：%s（%s）" % (F.describe(data),
                                "生效中" if data["enabled"] else "已暂停，名单留着不回复")


def chat_payload(limit=80):
    """读群聊流水，给实战台用（`fight_state()` 调它）。文件不存在不算错误。"""
    try:
        with open(CHAT_LOG, encoding="utf-8") as fh:
            d = json.load(fh)
    except FileNotFoundError:
        return {"ok": False, "chat": "", "seen": 0, "total": 0, "messages": [],
                "note": "还没有记录 —— 机器人跑起来之后才会有（只记它待的那个群）"}
    except Exception as exc:
        return {"ok": False, "chat": "", "seen": 0, "total": 0, "messages": [],
                "note": "读聊天记录失败：%s" % (exc,)}
    msgs = [m for m in (d.get("messages") or []) if isinstance(m, dict)]
    try:
        seen = float(d.get("seen") or 0)
    except Exception:
        seen = 0.0
    # total 是**截断前**的"群友消息"条数（不含机器人自己发的、不含空文本）：
    # 实战台的「↓ N 条新消息」靠它算增量，用截断后的 len(msgs) 会一直顶在
    # limit 上、永不增长（角标就不出来了）。
    users = [m for m in msgs if not m.get("self") and (m.get("text") or "").strip()]
    return {"ok": True, "chat": str(d.get("chat") or ""), "seen": seen,
            "total": len(users),
            "messages": msgs[-max(1, int(limit)):]}


def _sh(args, timeout=180):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=APPDIR)
        return (r.stdout or "") + (r.stderr or "")
    except Exception as exc:
        return "执行失败：%r" % (exc,)


def _get(url, timeout=4):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


# ------------------------------------------------------- 生成引擎（实战台共用）
# 实战台要「让机器人对这句话说一遍」，走 simulate.py 子进程（它自带 wxauto 那套依赖：
# 真实的 rules.yaml + personas + Brain，只生成、绝不发送）。
# 2026-10-04 晚用户要求：把原来独立的「演练台」并进「实战台」——
# 读消息 / 选消息 / 生成 / 换一条 / 改成我想发的话 / 发出去，一个卡片全包。
# （数据文件 演练台.json / 演练反馈.md / 演练评价.json 保留在原地，只是面板不再用。）
SIM_SCRIPT = os.path.join(APPDIR, "simulate.py")
_LOCK = threading.Lock()      # 实战台候选 / 发送记录等共享状态


# ---------------------------------------------------------------- 实战台
# 「我选一条群消息 → 机器人生成一句 → 我点发送 or 换一条」（用户 2026-10-04 要求）。
# 跟演练台的区别：**这里真的会发**，但必须用户点头；每一句都能先看、先换。
_FIGHT = {"who": "", "msg": "", "cand": "", "kind": "", "rule": "", "ms": 0.0,
          "sent": [], "error": ""}


def _post_json(url, obj, timeout=60):
    try:
        req = urllib.request.Request(
            url, data=json.dumps(obj or {}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def target_group():
    """从 config.yaml 读机器人要待的群（target.group）。"""
    try:
        with open(os.path.join(DIR, "config.yaml"), encoding="utf-8") as fh:
            for line in fh:
                s = line.split("#", 1)[0].strip()
                if s.startswith("group:"):
                    v = s.split(":", 1)[1].strip().strip('"').strip("'")
                    if v:
                        return v
    except Exception:
        pass
    return ""


def fight_state():
    # 可挑的消息直接看**聊天流水**（机器人每 20 秒在写，比"上次演练的快照"新）。
    #
    # ⚠️ 2026-10-05 用户报「实战台看不见群友发的所有消息，只有机器人打算回复的话」：
    #    根因就在这里 —— 原来有**两道截断**：
    #      chat_payload(40)  只拿 chat_log 最后 40 条；
    #      picked[-12:]      再只留最后 12 条。
    #    实测当时 chat_log 里有 362 条群友消息（总共 429 条），面板上只看得见 12 条。
    #    现在**全给**（chat_log 自己是 limit=1000 的环形缓冲，取满就是"所有记录"），
    #    前端 #fightlist 本来就是可滚动 + 带「↓ N 条新消息」角标的，能翻历史。
    #    机器人自己的话也一并列出来（self=true，前端灰绿底、点了不会生成回复），
    #    这样"谁在接谁的话"看得完整，不会像一堆没头没尾的自言自语。
    log = chat_payload(1000)
    msgs = log.get("messages") or []
    picked = [{"who": m.get("sender") or "?", "text": m.get("text") or "",
               "self": bool(m.get("self")), "ts": m.get("ts") or 0}
              for m in msgs if (m.get("text") or "").strip()]
    return {"target": target_group(), "pickable": picked,
            "chat": log.get("chat") or target_group(),
            "seen": log.get("seen") or 0,
            # 截断前的群友消息总数（面板拿它算「↓ N 条新消息」的增量）
            "total": log.get("total") or len(msgs),
            "who": _FIGHT["who"], "msg": _FIGHT["msg"], "cand": _FIGHT["cand"],
            "kind": _FIGHT["kind"], "rule": _FIGHT["rule"], "ms": _FIGHT["ms"],
            "sent": _FIGHT["sent"][-6:],
            # ★ 2026-10-05：chat_payload 读台账失败时会给一句 note（文件不存在/JSON 坏了）。
            #   以前这里把它扔掉，页面就只剩一句「还没有可选的消息（机器人还没读到群里的话）」
            #   —— 用户看到的"实战台消息没了"有一半是这种误导。现在透出来给页头显示。
            "error": _FIGHT["error"] or (log.get("note") or "")}


def fight_clear(remember: bool = True):
    """「不发了」：把选区、候选、报错都清掉（发送记录留着）。

    ``remember=True`` 时，如果框里当时确实有一条机器人写的候选，就把这句记进
    拒稿记录 —— 用户的语义是「我没看上，下次别这么说」（2026-10-05，
    见 wxauto/drafts.py 的 note_reject）。
    """
    with _LOCK:
        cand = str(_FIGHT.get("cand") or "").strip()
        src = str(_FIGHT.get("msg") or "").strip()
        who = str(_FIGHT.get("who") or "").strip()
        _FIGHT.update(who="", msg="", cand="", kind="", error="")
    if remember and cand:
        try:
            _drafts_mod().note_reject(cand, src=src, who=who, why="面板：不发了")
        except Exception:
            pass


def _gen_reply(text, timeout=200, mention=False, quote=False):
    """让机器人对这句话生成一条（走真实的 rules + Brain，绝不发送）。

    ``mention=True``：按「被 @ 点名问到」那一档生成（会展开答，见 config 的
    ``llm.mention_max_tokens`` / ``behavior.mention_max_chars``）。
    ★ 2026-10-09 用户要求「在机器人面板里输入问题，然后机器人回答我」。
    """
    cmd = [sys.executable, SIM_SCRIPT, "--json", "--text", str(text)]
    if mention:
        cmd.append("--mention")
    if quote:
        cmd.append("--quote")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=APPDIR)
    except Exception as exc:
        return "", "", "%s: %s" % (type(exc).__name__, exc), ""
    out = (r.stdout or "").strip().splitlines()
    try:
        d = json.loads(out[-1]) if out else None
    except Exception:
        d = None
    items = (d or {}).get("items") or []
    if not items:
        return "", "", ((r.stderr or "").strip()[-200:] or "生成失败"), ""
    it = items[0]
    # 第 4 个返回值是**命中的规则名**：面板要靠它把「图片规则」变成真的发图
    # （2026-10-09：原来只返回文本，于是 meitu 规则只能发一句占位说明）。
    return ((it.get("reply") or ""), (it.get("kind") or ""), "",
            (it.get("rule") or ""))


def fight_pick(who, msg):
    with _LOCK:
        _FIGHT.update(who=str(who or ""), msg=str(msg or ""), cand="",
                      kind="", error="")
    if not str(msg or "").strip():
        return False
    cand, kind, err, rname = _gen_reply(msg)
    with _LOCK:
        _FIGHT.update(cand=cand, kind=kind, rule=rname, error=err)
    return bool(cand)


def fight_again():
    with _LOCK:
        msg = _FIGHT["msg"]
    if not msg:
        return False
    return fight_pick(_FIGHT["who"], msg)


def fight_ask(text, quote=True):
    """★ 2026-10-09 用户要求：在面板里**自己提问**，机器人按人设回答我。

    和「点一条群消息生成」的区别：这条路固定按**被 @ 问到**那一档生成 ——
    用户就是在直接问它，希望它把话说透（`simulate.py --mention`），而不是回一句
    敷衍。同样**只生成、不发送**：发不发还是他点。生成的候选会填进编辑框，
    可以随便改，也可以点「换一条」重生。

    ``quote=True``（默认，2026-10-09 用户要求「还要把问题和答案都发出去，不能
    没头没脑的一份答案」）：让它**把问题当话题先点出来**再答 —— 群里的人看不到
    他在面板上问的那句，不带的话群友根本不知道在聊什么。
    """
    text = str(text or "").strip()
    if not text:
        return False
    with _LOCK:
        _FIGHT.update(who="我（面板提问）", msg=text, cand="", kind="", error="")
    cand, kind, err, rname = _gen_reply(text, mention=True, quote=bool(quote))
    with _LOCK:
        _FIGHT.update(cand=cand, kind=kind, rule=rname, error=err)
    return bool(cand)


def _is_chat_mismatch(r):
    """桥接这次失败是「会话不对」吗（而不是「抢不到输入焦点」之类）。

    只有这一种失败值得去切群重发 —— 别的失败切了群也没用，还会白折腾用户的屏。
    三种文本都来自 wxbridge.py 的 `/send` 会话校验（2026-10-08 审查修复 S1）。
    """
    e = str((r or {}).get("error") or "")
    return ("不在目标会话" in e) or ("无法确认当前会话" in e) or ("会话校验异常" in e)


def _shell(cmd, timeout=25):
    """跑一条 adb shell（面板进程自己有 adb，容器里就能用）。"""
    try:
        r = subprocess.run(["adb", "shell", str(cmd)], capture_output=True,
                           text=True, timeout=timeout)
        return (r.stdout or "").strip()
    except Exception:
        return ""


def fight_grab():
    """★ 2026-10-09 用户要求：面板上一个「抢一下」按钮。

    他报的原话：「我没动还是发不出去。另外面板上没有抢一下再发的按钮」。
    查下来卡住的是**两件**事，而原来的"抢"一件都没解决：

    ① **微信实例在物理屏上**。桥接的「抢」只是给物理屏按一次 HOME
       （见 wxbridge.release_physical_input）—— 它抢的是**输入焦点**，
       **不会把微信搬回虚拟屏**。日志实测：`物理屏占着输入焦点
       （com.tencent.mm/.ui.chatting.ChattingUI）` 那几轮，微信就在他手上，
       虚拟屏根本没有微信，于是"等你让位 8 秒 → 放弃"永远等不到。
       → 这里补 `/takeover`：把微信拿回虚拟屏。
    ② **粘住的输入法会话**。他在手机浏览器里点过面板输入框之后，
       `mServedView` 会一直挂在那个 WebView 上（实测 `android.webkit.WebView
       ... 1264x1584`，`mInputShown=false`，人却在桌面）—— 虚拟屏微信因此
       拿不到键盘。切一次输入法再切回来能把这根线断掉。
       ⚠️ 切回去的是**他原来那个**输入法，不留在我们这边。
    """
    done = []
    try:
        o = _post_json(BRIDGE + "/takeover", {}, timeout=120)
        done.append("微信已拿回虚拟屏" if (o and o.get("ok"))
                    else "拿微信失败（%s）" % str((o or {}).get("error") or "接口没回 ok"))
    except Exception as exc:
        done.append("拿微信出错：%s" % type(exc).__name__)
    try:
        cur = _shell("settings get secure default_input_method")
        if cur:
            other = ("com.dsh.clip/.DshIme"
                     if cur.startswith("com.dsh.clip") else
                     "com.iflytek.inputmethod/.FlyIME")
            _shell("ime set " + other)
            time.sleep(0.8)
            _shell("ime set " + cur)          # ★ 还回他自己那个
            done.append("输入法会话已重置")
    except Exception:
        pass
    ok = any("已拿回" in d for d in done)
    return ok, "；".join(done)


def _rule_by_name(name):
    """按名字从 rules.yaml 找一条规则（拿 meitu_source / replies / image_tag）。"""
    name = str(name or "").strip()
    if not name:
        return None
    try:
        if APPDIR not in sys.path:
            sys.path.insert(0, APPDIR)
        from wxauto.config import load_rules
        from wxauto.matcher import Rule
        for d in load_rules():
            r = Rule.from_dict(d)
            if r.name == name:
                return r
    except Exception:
        return None
    return None


def fight_send_image(rule_name="", source="", text="", origin="manual", who="",
                     draft=""):
    """面板代发一张图。★ 2026-10-09 用户要求：「我要机器人能发图出去」。

    为什么需要它：实战台点中「看看腿」「看看腹肌」「今日美图」这种**图片规则**时，
    `simulate.py` 只能给出一句**演练占位文本**（「（会发一张今日美图 · 分类：看看腿）」）
    —— 那是给人看的说明，根本不是能发的话。用户 2026-10-09 真把它点发出去了，
    群里就多了一句没头没脑的假消息（他截图给我看的正是这条）。
    图片规则本来就该**发图**，所以这里补上真正的发图路径。

    顺序照抄机器人那条路（`bot._send_meitu`）：**先图后配文** —— 图没发成时，
    群里只剩一句配文看着像坏了；只发出去一张图则完全正常。**配文失败不算整条失败**。
    """
    if os.path.exists(os.path.join(DIR, "STOP.txt")):
        return False, ("急停生效中（STOP.txt 存在）—— 面板也不发。想恢复先删掉它。")
    _now = time.time()
    if _MANUAL["last"] and (_now - _MANUAL["last"]) < _MANUAL_MIN_GAP:
        return False, ("刚发过一条（%.1f 秒前）—— 面板代发最小间隔 %.0f 秒，"
                       "防手滑连点" % (_now - _MANUAL["last"], _MANUAL_MIN_GAP))
    r = _rule_by_name(rule_name)
    src = str(source or "").strip()
    if not src and r is not None:
        src = str(getattr(r, "meitu_source", "") or "").strip()
    path = ""
    try:
        if APPDIR not in sys.path:
            sys.path.insert(0, APPDIR)
        from wxauto.config import load_config
        cfg = load_config()
        if r is not None and bool(getattr(r, "is_image", False)):
            # ★ 2026-10-09 自查修掉的分发 bug：表情包规则（image_tag）得走**表情包库**。
            #   原来所有规则都塞给 MeituFetcher，而表情包规则的 meitu_source 是空的
            #   → `fetch("")` 会**兜到网络图源**，于是"让它发张表情包"变成发一张
            #   不相干的网图（而且它其实是拿 image_tag 当分类的）。
            from wxauto.stickers import StickerLibrary
            st = getattr(cfg, "stickers", None)
            lib = StickerLibrary(os.path.join(DIR, str(
                getattr(st, "dir", "表情包") if st else "表情包")))
            p = lib.pick(str(getattr(r, "image_tag", "") or ""))
            path = str(p) if p else ""
        else:
            from wxauto.meitu import MeituFetcher
            fetcher = MeituFetcher(getattr(cfg, "meitu", None), root=DIR)
            path = str(fetcher.fetch(src) or "")
    except Exception as exc:
        return False, "取图出错：%s: %s" % (type(exc).__name__, exc)
    if not path:
        if r is not None and bool(getattr(r, "is_image", False)):
            return False, "表情包里没挑到图（那个标签的文件夹是空的？）"
        return False, "没取到图（本地图集是空的？网络也不通？）"
    tgt = target_group()
    r1 = _post_json(BRIDGE + "/send_image",
                    {"path": str(path), "chat": str(tgt or "")}, timeout=300)
    if not (r1 and r1.get("ok")):
        return False, "发图失败：%s" % str((r1 or {}).get("error") or "接口没回 ok")
    _MANUAL["last"] = time.time()
    # 配文：框里写了就用他的，没写就用规则自带的候选抽一句
    cap = str(text or "").strip()
    if not cap and r is not None:
        import random as _rnd
        reps = [str(x).strip() for x in (getattr(r, "replies", None) or [])
                if str(x).strip()]
        if reps:
            cap = _rnd.choice(reps)
    if cap:
        time.sleep(3.5)      # 先图后字，隔一下 —— 紧挨着发像连发
        r2 = _post_json(BRIDGE + "/send", {"text": cap}, timeout=300)
        if not (r2 and r2.get("ok")):
            self_err = str((r2 or {}).get("error") or "接口没回 ok")
            try:
                _drafts_mod().note_sent("[图片] %s" % os.path.basename(str(path)),
                                        origin=origin, src=src or rule_name,
                                        who=who, draft=draft)
            except Exception:
                pass
            # 图进去了就算成功；配文掉了单独说一句，别让用户以为整条都失败
            return True, "（图发出去了，配文没跟上：%s）" % self_err
    try:
        _drafts_mod().note_sent(cap or ("[图片] %s" % os.path.basename(str(path))),
                                origin=origin, src=src or rule_name, who=who,
                                draft=draft)
    except Exception:
        pass      # 记账失败不影响"已经发出去了"这个事实
    return True, ""


def fight_send(text, target="", origin="manual", draft="", src="", who="",
               force=False):
    """真发。**先切回目标群**再发 —— 免得在别的会话里发错地方（不可逆）。

    ``origin`` / ``draft``：这条是谁的意思、机器人原稿是什么（发给
    ``drafts.note_sent`` 记流水，2026-10-05 用户要求「发出去的也能记住」）。
    """
    text = str(text or "").strip()
    if not text:
        return False, "没有内容"
    # ★ 2026-10-08 审查修复（S4）：面板代发原来是**完全绕过安全闸门**的
    #   —— 不走 Governor（per_minute/per_hour/per_day、burst 全不数）、
    #   不查急停（STOP.txt 存在时机器人已经闭嘴了，面板却还能往群里发）、
    #   也不做 validate_outgoing。这里补上两道最要紧的：
    #   ① 急停生效中 → 一律不发（"按下去了"必须是全局状态，不能分进程）；
    #   ② 面板连点限速（机器人那个 Governor 在另一个进程里，读不到它的内存账本，
    #      所以这里用"面板侧最小间隔"兜住手滑连点；正常使用完全无感）。
    _stop = os.path.join(DIR, "STOP.txt")
    if os.path.exists(_stop):
        return False, ("急停生效中（%s 存在）—— 面板也不发。想恢复先删掉它。"
                       % os.path.basename(_stop))
    _now = time.time()
    _gap = _now - _MANUAL["last"]
    if _MANUAL["last"] and _gap < _MANUAL_MIN_GAP:
        return False, ("刚发过一条（%.1f 秒前）—— 面板代发最小间隔 %.0f 秒，"
                       "防手滑连点" % (_gap, _MANUAL_MIN_GAP))
    tgt = str(target or target_group() or "").strip()
    # ★ 2026-10-09 用户反馈「我在面板里打的字主动发出去，好像也很慢」—— 病根找到了：
    #   这里原来**每次**都先 `/open` 切一次群，而切群实测要 **15 秒**
    #   （搜会话 → 点结果 → 校验标题），发送本身才 8 秒上下。
    #   但桥接的 `/send` **自己就有 fail-closed 的会话校验**（2026-10-08 审查修复 S1：
    #   读到别的会话名 → 拒发；读不出 → 先自己切回目标群再核对，仍认不出就拒发），
    #   所以外面这次切群在"本来就在目标群"的常见情况下是**纯浪费**。
    #   改成：先直接发，只有它明确说会话不对时才切群重发。
    #   ⚠️ 为什么这样也安全：`/send` 的会话校验发生在**碰输入框之前**，不通过就直接
    #      返回 —— 所以"先发一次"既不会发错群，也不会造成重复发送。
    r = _post_json(BRIDGE + "/send", {"text": text, "force": bool(force)}, timeout=300)
    ok = bool(r and r.get("ok"))
    if not ok and tgt and _is_chat_mismatch(r):
        o = _post_json(BRIDGE + "/open", {"chat": tgt}, timeout=240)
        if not (o and o.get("ok")):
            return False, "切回「%s」失败（机器人可能不在群里），这次没敢发" % tgt
        r = _post_json(BRIDGE + "/send", {"text": text, "force": bool(force)},
                       timeout=300)
        ok = bool(r and r.get("ok"))
    if ok:
        _MANUAL["last"] = time.time()      # 面板限速（上一段的说明）
        try:
            _drafts_mod().note_sent(text, origin=origin, src=src, who=who, draft=draft)
        except Exception:
            pass      # 记账失败不影响"已经发出去了"这个事实
    # ★ 2026-10-05：把桥接的中文原因透传给面板 —— 以前只会报「发送接口没回 ok」，
    #   用户看到这句根本不知道是"你在用手机、微信抢不到输入焦点"（实测的弹窗就是它）。
    return ok, "" if ok else str((r or {}).get("error") or "发送接口没回 ok")


# ------------------------------------------------ 说过的话 / 拒稿记录
# 2026-10-05 用户要求「发出去的也能记住」+「不让发的下次注意」。
# 数据都在 wxauto/drafts.py 里，这里只做面板侧的读写。
# ⚠️ 2026-10-07：这里原来还有「待审草稿」那条线（机器人窗口外自己写稿、等主人点头），
#    proactive_window 改成全天之后它永远走不到，已按用户要求删掉（交接说明 §46.8）。
def _drafts_mod():
    if APPDIR not in sys.path:
        sys.path.insert(0, APPDIR)
    from wxauto import drafts as D
    return D


# ------------------------------------------------ 学习记录（人设素材 + 总结）
# 2026-10-05 用户要求：「发出去的也能记住对吧？别光记不发的，到时候方便总结这个
# 人设符合……我心中理想情况下的话」。所以这里把 sent.jsonl / rejects.jsonl 读出来
# 给面板看，并且能**一键让模型总结成几条人设建议**（只输出文字，不改任何文件）。
SUGGEST_FILE = os.path.join(DIR, "人设建议.md")
_LEARN = {"summary": "", "at": 0.0, "busy": False, "error": ""}


def _read_suggest():
    try:
        with open(SUGGEST_FILE, encoding="utf-8") as fh:
            return fh.read()
    except Exception:
        return ""


_LEARN["summary"] = _read_suggest()


def learn_state():
    """面板「📚 学习记录」卡片用：说过的话 + 否掉的话 + 上次的总结。"""
    out = {"ok": True, "error": _LEARN["error"], "busy": bool(_LEARN["busy"]),
           "summary": _LEARN["summary"], "summary_at": _LEARN["at"],
           "sent": [], "rejects": [], "counts": {"sent": 0, "user": 0, "rejects": 0}}
    try:
        D = _drafts_mod()
        st = D.stats()
        sent = D.recent_sent(40)
        rej = D.recent_rejects_full(20)
    except Exception as exc:
        out.update(ok=False, error="%s: %s" % (type(exc).__name__, exc))
        return out

    def cn(o):
        return D.ORIGIN_CN.get(o, o)

    out["sent"] = [{"text": r.get("text") or "", "origin": r.get("origin") or "",
                    "origin_cn": cn(r.get("origin") or ""),
                    "at": float(r.get("ts") or 0), "who": r.get("who") or "",
                    "src": r.get("src") or "",
                    "draft": r.get("draft") or "",
                    "edited": bool(r.get("draft")) and r.get("draft") != r.get("text")}
                   for r in sent]
    out["rejects"] = [{"text": r.get("text") or "", "at": float(r.get("ts") or 0),
                       "who": r.get("who") or "", "src": r.get("src") or "",
                       "why": r.get("why") or ""} for r in rej]
    out["counts"] = {"sent": int(st.get("sent") or 0),
                     "user": int(st.get("sent_user") or 0),
                     "rejects": int(st.get("rejects") or 0)}
    return out


def _llm_conf():
    """拿 base_url / key / model（走 wxauto.config + 同目录 .deepseek_key）。"""
    if APPDIR not in sys.path:
        sys.path.insert(0, APPDIR)
    from wxauto.config import load_config
    cfg = load_config()
    llm = getattr(cfg, "llm", None)
    base = str(getattr(llm, "base_url", "") or "https://api.deepseek.com/v1").rstrip("/")
    model = str(getattr(llm, "model", "") or "deepseek-chat")
    key = str(getattr(llm, "api_key", "") or "")
    if not key:
        try:
            with open(os.path.join(DIR, ".deepseek_key"), encoding="utf-8") as fh:
                key = fh.read().strip()
        except Exception:
            key = ""
    return base, key, model


def learn_summarize():
    """把「说出去的话 + 否掉的话」交给模型，总结成几条人设建议。

    **只产出文字**，写进 人设建议.md 并在面板上显示 —— 不自动改 personas/，
    改不改、怎么改，由主人自己决定（人设是他的东西）。
    """
    if _LEARN["busy"]:
        return False, "上一次总结还在跑"
    _LEARN.update(busy=True, error="")
    try:
        D = _drafts_mod()
        sent = D.recent_sent(40)
        rej = D.recent_rejects_full(20)
        user_ok, edited, manual, auto_sent = [], [], [], []
        for r in reversed(sent):          # 时间正序，读起来像一条时间线
            o = r.get("origin")
            t = str(r.get("text") or "").strip()
            if not t:
                continue
            if o == "approve":
                user_ok.append(t)
            elif o == "edited":
                edited.append("机器人原稿「%s」→ 主人改成「%s」"
                              % (str(r.get("draft") or "").strip(), t))
            elif o == "manual":
                manual.append(t)
            else:
                auto_sent.append(t)

        lines = []
        if user_ok:
            lines.append("【主人点头放行的（原稿没改）】")
            lines += ["- " + t for t in user_ok[-25:]]
        if edited:
            lines.append("\n【主人改过才放行的（最值钱：原稿 vs 改成什么样）】")
            lines += ["- " + t for t in edited[-25:]]
        if manual:
            lines.append("\n【主人自己手写发的】")
            lines += ["- " + t for t in manual[-25:]]
        rej_txt = [str(r.get("text") or "").strip() for r in reversed(rej)
                   if str(r.get("text") or "").strip()]
        if rej_txt:
            lines.append("\n【主人否掉的（看到就划掉了，说明这种说法不行）】")
            lines += ["- " + t for t in rej_txt[-25:]]
        if auto_sent:
            lines.append("\n【机器人自己自动发的（仅供参考，不代表主人认可）】")
            lines += ["- " + t for t in auto_sent[-20:]]
        if not lines:
            _LEARN.update(busy=False)
            return False, "还没有素材：先去实战台发几条 / 点几次「不发」"
        corpus = "\n".join(lines)

        base, key, model = _llm_conf()
        if not key:
            _LEARN.update(busy=False)
            return False, "没有 API Key（看 .deepseek_key）"
        sysmsg = ("你是一个说话风格分析师。用户会给你一批**微信群里的短句**："
                  "一部分是他自己认可/改过的，一部分是他否掉的。"
                  "请总结出他心目中「理想的说法」到底是什么样，"
                  "并给出可以直接写进机器人人设文件的规则。")
        if len(corpus) > 6000:
            corpus = corpus[-6000:]
        usermsg = (corpus + "\n\n请输出：\n"
                   "1) 一句话概括这个人想要的说话风格；\n"
                   "2) 3~6 条建议写进人设的规则，每条一行、以「- 」开头，"
                   "要具体（长度/语气/用词/什么时候不接），"
                   "别写「自然一点」「有趣一点」这种空话；\n"
                   "3) 单独一行写「反例：」+ 最该避免的 1~2 种说法。"
                   "全文中文，不要客套，不要重复我给你的原句。")
        payload = json.dumps({"model": model, "stream": False, "temperature": 0.4,
                              "max_tokens": 900,
                              "messages": [{"role": "system", "content": sysmsg},
                                           {"role": "user", "content": usermsg}]},
                             ).encode("utf-8")
        req = urllib.request.Request(base + "/chat/completions", data=payload,
                                     method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + key)
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            text = body["choices"][0]["message"]["content"].strip()
        except Exception as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            _LEARN.update(busy=False, error="模型调用失败：%s %s" % (type(exc).__name__, detail))
            return False, _LEARN["error"]
        try:
            with open(SUGGEST_FILE, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
        except Exception:
            pass
        _LEARN.update(busy=False, summary=text, at=time.time(), error="")
        return True, ""
    except Exception as exc:
        _LEARN.update(busy=False, error="%s: %s" % (type(exc).__name__, exc))
        return False, _LEARN["error"]


def bot_pids():
    try:
        r = subprocess.run(["pgrep", "-f", PAT], capture_output=True, text=True, timeout=10)
        return [p for p in r.stdout.split() if p]
    except Exception:
        return []


def bot_running():
    return bool(bot_pids())


def bot_mode():
    """现在跑的是干跑还是真跑 —— 看日志里**最后一次**启动那行。

    ⚠️ 2026-10-05 修：原来读的是 `fh.read(4000)`，也就是**文件开头**，而日志是
    append 的（已经几千行）——于是它永远报**最早那次**启动的模式。用户刚从真跑
    切成干跑，面板却还写着「真跑（会往群里发消息）」，反过来也一样容易误判
    （以为它在群里说话 / 以为安全）。改成读尾部、从后往前找最近一次标记。
    """
    try:
        size = os.path.getsize(LOG)
        with open(LOG, encoding="utf-8", errors="replace") as fh:
            if size > 8000:
                fh.seek(size - 8000)
            tail = fh.read()
    except Exception:
        return ""
    idx = tail.rfind("演练模式：")
    if idx < 0:
        return ""
    seg = tail[idx:idx + 12]
    if "开" in seg:
        return "干跑（只记录不发）"
    if "关" in seg:
        return "真跑（会往群里发消息）"
    return ""


def bridge_state():
    d = _get(BRIDGE + "/health")
    if d and d.get("ok"):
        return True, "正常（虚拟屏 %s）" % d.get("displayId")
    return False, "没在跑"


TOKEN_FILES = ("/run/dsh-phone/token", "/sdcard/dsh/.phone-token")


def _phone_token():
    for p in TOKEN_FILES:
        try:
            with open(p, encoding="utf-8") as fh:
                t = fh.read().strip()
            if t:
                return t
        except Exception:
            pass
    return ""


def adb_ok():
    """无线调试一关，adb 就 offline —— 这是机器人快不快的关键。"""
    try:
        r = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=10)
        return any(line.endswith("\tdevice") for line in r.stdout.splitlines())
    except Exception:
        return False


_BATT = {"at": 0.0, "text": ""}


def battery_line():
    """电量看护那一行（30 秒缓存 —— 面板每几秒刷一次，别每次都去问 dumpsys battery）。

    数据源就是 battery_guard.sh --status（它自己读 adb 的 dumpsys battery），
    所以面板显示的口径和真正动手的那个看护完全一致。
    """
    now = time.time()
    if _BATT["text"] and now - _BATT["at"] < 30:
        return _BATT["text"]
    out = _sh(["bash", os.path.join(APPDIR, "battery_guard.sh"), "--status"], 40) or ""
    text = " · ".join(l.strip() for l in out.strip().splitlines() if l.strip())
    _BATT.update(at=now, text=text or "读不到（battery_guard.sh 没响应）")
    return _BATT["text"]


def battery_row():
    t = battery_line()
    color = "#0a7d32" if "看护 开" in t else "#8a8a8a"
    if "已停机" in t:
        color = "#c0392b"
    return '<span style="color:%s;font-weight:600">%s</span>' % (color, html.escape(t))


def vsd_port():
    """受信任虚拟屏守护的端口：优先 env.sh 里的 VSD_PORT。

    ⚠️ 老版本写死 18240。2026-10-03 那个端口被"失效端口绑定"劫持了 ——
    TCP 连得上，但服务端永远不回数据（accept 队列一路堆积），
    于是面板长期误报「虚拟屏守护 挂了」，而机器人其实读得好好的。
    真正的守护在 env.sh 的 VSD_PORT=18250 上。
    """
    p = os.environ.get("VSD_PORT", "")
    if p.isdigit():
        return p
    try:
        with open("/sdcard/dsh/env.sh", encoding="utf-8") as fh:
            for line in fh:
                s = line.strip()
                if s.startswith("export VSD_PORT=") or s.startswith("VSD_PORT="):
                    v = s.split("=", 1)[1].split()[0]
                    v = v.replace("${VSD_PORT:-", "").rstrip("}")   # 解 ${VSD_PORT:-18250}
                    if v.isdigit():
                        return v
    except Exception:
        pass
    return "18250"


def vsd_live_port():
    """哪个端口真的在回 /list（返回 JSON 数组才算活着），没有就返回 ""。"""
    ports = [vsd_port(), "18250", "18252", "18240", "18242"]
    seen = set()
    for p in ports:
        if p in seen:
            continue
        seen.add(p)
        try:
            with urllib.request.urlopen("http://127.0.0.1:%s/list" % p, timeout=2) as r:
                if r.read().strip().startswith(b"["):
                    return p
        except Exception:
            continue
    return ""


def vscreen_ok():
    return bool(vsd_live_port())


def physical_foreground():
    """物理屏现在是哪个 App —— 用来提示"你把微信开在手上了，机器人抢不动"。

    问的是无障碍桥 :3099（返回纯文本），token 在 /run/dsh-phone/token。
    """
    tok = _phone_token()
    if not tok:
        return ""
    try:
        url = "%s/status?token=%s" % (PHONE, urllib.parse.quote(tok))
        with urllib.request.urlopen(url, timeout=4) as r:
            text = r.read().decode("utf-8", "replace")
    except Exception:
        return ""
    for line in text.splitlines():
        if "当前前台" in line:
            return line.split(":", 1)[1].strip()
    return ""


def log_tail(n=14):
    try:
        with open(LOG, encoding="utf-8", errors="replace") as fh:
            return "\n".join(fh.read().splitlines()[-n:])
    except Exception:
        return "(还没有日志 —— 机器人从没启动过)"


def _fmt_dur(sec: float) -> str:
    sec = int(sec or 0)
    if sec < 60:
        return "%d 秒" % sec
    if sec < 3600:
        return "%d 分钟" % (sec // 60)
    return "%d 小时 %d 分" % (sec // 3600, (sec % 3600) // 60)


def bot_uptime() -> float:
    """机器人主进程已经跑了多久（秒）；拿不到给 0。

    给面板显示"运行中（12 分钟）" —— 用户能一眼看出它是不是一直在跑，
    而不是刚重启完（那就说明它其实反复在挂）。
    """
    pids = bot_pids()
    if not pids:
        return 0.0
    try:
        r = subprocess.run(["ps", "-o", "etimes=", "-p", pids[0]],
                           capture_output=True, text=True, timeout=6)
        return float((r.stdout or "").strip() or 0)
    except Exception:
        return 0.0


def _line_age(line: str) -> float:
    """日志行开头那个 HH:MM:SS 距离现在多少秒（解析不出来给 0）。"""
    m = re.match(r"\s*(\d{2}):(\d{2}):(\d{2})", line or "")
    if not m:
        return 0.0
    h, mi, s = (int(x) for x in m.groups())
    t = time.localtime()
    return float(((t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec)
                  - (h * 3600 + mi * 60 + s)) % 86400)


def screen_health() -> str:
    """最近是不是读不到画面 —— 区分「进程死了」和「进程活着但瞎了」。

    2026-10-03：用户报「又停了」，其实进程好端端跑着，只是他点开微信把实例抢回
    物理屏了，机器人读到的是空画面（日志里只有一句 PC 版味道的「截图为空」）。
    这里翻最近的日志，给出人话结论。
    """
    if not adb_ok():
        # adb 一断，读屏失败的原因就锁定了：2026-10-04 实测 WiFi 断开 →
        # Android 的「无线调试」被系统关掉 → adb 断 → 微信搬不回虚拟屏、发也发不出。
        # 这时不能再说成「你把微信抢回手上了」，否则用户照着修白折腾。
        #
        # ⚠️ 也**别拿守护的 frames 判断抓帧死活**：实测 slot0 的 frames 在正常时
        # 同样不涨（只有 slot1 在涨），用它必然误报。真要看读屏通不通，就看
        # `/snapshot` 成不成功（`bash recover.sh --check` 也能看）。
        return "adb 断了（无线调试被系统关掉）—— 机器人读不到也发不出"
    try:
        with open(LOG, encoding="utf-8", errors="replace") as fh:
            lines = [l for l in fh.read().splitlines()[-60:] if l.strip()]
    except Exception:
        return ""
    for line in reversed(lines):
        if "读屏恢复正常" in line:
            break                    # 恢复正常标记：这条之后就没事了
        if "读不到画面" in line or "截图为空" in line:
            # ⚠️ 机器人正常轮询是**不打日志**的，所以"最后一条是错误"不等于
            #    "现在还在错"。再看时间戳：一分半以前的错误，多半早恢复了
            #    （2026-10-03 实测误报过：桥接被看护修好 2 分钟了，面板还挂着红字）
            if _line_age(line) > 90:
                return ""
            if "微信不在虚拟屏" in line:
                return "微信不在虚拟屏 —— 多半是你点开微信把它抢回手上了"
            return line.strip()[:70]
        if "启动完成" in line or "当前会话" in line:
            break                    # 已经追到启动那一步，说明之后没报过错
    return ""


# 正在启动/停止的标记：面板必须能显示「启动中…」，否则用户点完一刷新看到的还是
# 「已停止 / 桥接没在跑」的中间态（启动要 7~40 秒），以为没点上又要再点一次
# —— 2026-10-03 用户就是这么被坑的（连点 6 次启动，每次都在切群阶段杀掉上一个）。
_BUSY = {"what": "", "since": 0.0}
# 最近一次手动操作的结果 —— 面板上要看得见「修完到底成没成」，
# 不然用户点完只能盯着"运行中/已停止"猜（2026-10-03 用户要求加手动按钮时加的）
_LAST = {"what": "", "output": "", "ok": None, "at": 0.0}


def _busy_wrap(what: str, args, timeout: int):
    _BUSY.update(what=what, since=time.time())

    def run():
        try:
            out = (_sh(args, timeout) or "").strip()
            bad = ("❌" in out) or ("不通" in out) or ("失败" in out)
            _LAST.update(what=what, output=out[-700:], ok=not bad, at=time.time())
        except Exception as exc:
            _LAST.update(what=what, output="执行异常：%r" % (exc,), ok=False,
                         at=time.time())
        finally:
            _BUSY.update(what="", since=0.0)

    threading.Thread(target=run, daemon=True).start()


def start(dry: bool):
    args = ["bash", os.path.join(APPDIR, "bot.sh"), "start"] + (["--dry-run"] if dry else [])
    _busy_wrap("启动（干跑）" if dry else "启动（真跑）", args, 240)


def stop():
    # ★ 2026-10-12：带上身份，机器人日志里才看得出是面板点的（见 botpids.sh::_note_stop）
    _busy_wrap("停止", ["env", "WXBOT_STOP_WHO=面板", "WXBOT_STOP_WHY=你在面板上点了「停止」",
                      "bash", os.path.join(APPDIR, "bot.sh"), "stop"], 60)


def _post_local(url, timeout=90):
    """给本机端点发 POST，返回 dict（失败给 {"ok": False, "error": …}）。"""
    try:
        req = urllib.request.Request(url, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except Exception as exc:
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}


def takeover():
    """把微信放到虚拟屏 —— **不启动机器人**。

    桥接没通就先把桥接拉起来再搬。给用户的场景：不小心点了微信，想把它弄回虚拟屏，
    但又不想重启整个机器人。
    """
    _BUSY.update(what="把微信放到虚拟屏", since=time.time())

    def job():
        lines = []
        res = _post_local("http://127.0.0.1:8765/takeover")
        lines.append("① 叫桥接搬：%s" % json.dumps(res, ensure_ascii=False))
        if not res.get("ok"):
            lines.append("② 桥接没通 / 没搬成 → 先把桥接拉起来")
            lines.append((_sh(["bash", os.path.join(APPDIR, "wxbridge_ctl.sh"),
                               "start"], 200) or "").strip()[-300:])
            time.sleep(2)
            res = _post_local("http://127.0.0.1:8765/takeover")
            lines.append("③ 再搬一次：%s" % json.dumps(res, ensure_ascii=False))
        _LAST.update(what="把微信放到虚拟屏", output="\n".join(lines).strip(),
                     ok=bool(res.get("ok")), at=time.time())
        _BUSY.update(what="", since=0.0)

    threading.Thread(target=job, daemon=True).start()


def reclaim_wechat():
    """收回微信：搬回虚拟屏 **并切回目标群**，不重启机器人。

    跟 ``takeover()`` 的区别：takeover 只搬屏，搬完微信停在用户最后看的那个会话上，
    机器人接着报「不是目标群，待命中」还是不干活；reclaim 顺手把会话切回
    ``config.yaml`` 的 target.group，机器人当场复活（2026-10-04 用户要求）。

    ⚠️ 现在桥接还有自动归还（见 wxbridge.py::reclaim_watch）：用户拿走微信、
    又切走了，10 秒后自动收回 —— 这个按钮是「想立刻收回」用的。
    """
    _BUSY.update(what="收回微信", since=time.time())

    def job():
        lines = []
        res = _post_local("http://127.0.0.1:8765/reclaim", timeout=150)
        lines.append("① 叫桥接收回：%s" % json.dumps(res, ensure_ascii=False))
        if not res.get("ok"):
            lines.append("② 桥接没通 / 没收回 → 先把桥接拉起来")
            lines.append((_sh(["bash", os.path.join(APPDIR, "wxbridge_ctl.sh"),
                               "start"], 200) or "").strip()[-300:])
            time.sleep(2)
            res = _post_local("http://127.0.0.1:8765/reclaim", timeout=150)
            lines.append("③ 再收一次：%s" % json.dumps(res, ensure_ascii=False))
        _LAST.update(what="收回微信", output="\n".join(lines).strip(),
                     ok=bool(res.get("ok")), at=time.time())
        _BUSY.update(what="", since=0.0)

    threading.Thread(target=job, daemon=True).start()


def repair():
    """守护 / 微信 / 桥接 一把修 —— recover.sh --no-bot，不碰机器人。"""
    _busy_wrap("修复连接", ["bash", os.path.join(APPDIR, "recover.sh"), "--no-bot"], 500)


def reconnect_adb():
    """重连无线调试（adb 掉了的时候用）。"""
    _busy_wrap("重连 adb", ["sh", "/sdcard/dsh/connect-adb.sh"], 300)


def stop_hud():
    """收起浮窗（停掉 hud 服务）。

    ★ 2026-10-09 用户要求：跟「拉起浮窗」配一对 —— 他不想看的时候一键收掉，
      不用跑去通知栏划（划掉通知是"系统强杀"，这个是"体面地退"）。
      走 `stop.sh`（内部 `hud --off` → `GET /quit` → `svc.shutdown()`），
      脚本里还有一次 curl 兜底。
    ⚠️ 注意：这只是把**现在这个**浮窗收掉；机器人下一次操作手机时，
      `phone` 包装层还会自动把它拉起来（那是进度反馈的设计）。要连这个也停，
      得改 phone-wrapper.sh —— 用户没要求，先不动。
    """
    _busy_wrap("关掉浮窗", ["sh", "/sdcard/dsh/hud/stop.sh"], 30)


def start_hud():
    """拉起浮窗服务。

    ★ 2026-10-09 用户要求：「在机器人面板里加一个启动旋钮窗的按钮，因为有时候
      我会不会觉得它碍事把它划掉，有时有。需要它把它打开」——
      浮窗是 `com.dsh.hud` 的**前台服务**：在通知栏把「DSH 浮窗待命」划掉 =
      服务被杀、浮窗也就没了（之后只有再调一次 `hud` 才会自己拉起来）。
      这个按钮让他随时手动叫回来。走命令守护，**不需要 adb**。
    """
    _busy_wrap("拉起浮窗", ["sh", "/sdcard/dsh/hud/start.sh"], 40)


# ---------------------------------------------------------------- 自动看护
# 用户问（2026-10-03）：「这些是全自动的不用我管吗？」—— 启动流程是自动的，但桥接崩了、
# 守护挂了、adb 断了，之前没人管。这里补一层：每 45 秒体检一次，**连续两轮**不正常才动手，
# 而且动手时绝不碰用户手上的微信（物理屏开着微信就不重启机器人）。
_WATCH = {"on": True, "last": "", "last_at": 0.0, "fails": 0, "actions": 0}


def _infra():
    """体检：返回 (adb_ok, 虚拟屏守护_ok, 桥接_ok)。"""
    return bool(adb_ok()), bool(vsd_live_port()), bool(bridge_state()[0])


def _auto_heal(bad):
    """自动修 —— 动作能小就小：adb 断了就重连，桥接挂了就重启桥接，
    只有守护真挂了才动大手术（重守护会重建虚拟屏、换屏号，机器人得跟着重启）。"""
    what = "自动修复：" + "+".join(bad)
    _BUSY.update(what=what, since=time.time())
    lines = ["体检发现问题：" + "、".join(bad)]
    try:
        was_running = bot_running()
        if "adb" in bad:
            lines.append("① 重连 adb")
            # ★2026-10-12：带 ADB_CONNECT_AUTO=1 —— 自动修属于"自动调用"，
            #   拿不到全局锁就跳过（让看护去连），不在这里阻塞等待。
            lines.append((_sh(["env", "ADB_CONNECT_AUTO=1", "sh",
                               "/sdcard/dsh/connect-adb.sh"], 240) or "").strip()[-250:])
            time.sleep(2)
        if "虚拟屏守护" in bad:
            lines.append("② 重启虚拟屏守护 + 桥接（不搬微信）")
            lines.append((_sh(["bash", os.path.join(APPDIR, "recover.sh"),
                               "--no-bot", "--no-takeover"], 520) or "").strip()[-400:])
        elif "桥接" in bad:
            lines.append("② 重启桥接服务")
            lines.append((_sh(["bash", os.path.join(APPDIR, "wxbridge_ctl.sh"),
                               "start"], 240) or "").strip()[-300:])

        a, v, b = _infra()
        lines.append("修完体检：adb=%s 守护=%s 桥接=%s"
                     % ("ok" if a else "断", "ok" if v else "挂", "ok" if b else "断"))

        # 守护被重建过 → 屏号变了，机器人会一直"读不到画面"，必须重启
        if "虚拟屏守护" in bad and was_running:
            if physical_foreground().startswith("com.tencent.mm"):
                lines.append("③ 你正在手机上用微信 —— 不重启机器人（免得把微信抢走）。"
                             "不用了之后点一下「启动」就行。")
            else:
                lines.append("③ 重启机器人")
                lines.append((_sh(["bash", os.path.join(APPDIR, "bot.sh"),
                                   "start"], 260) or "").strip()[-220:])
    except Exception as exc:
        lines.append("看护动作异常：%r" % (exc,))
    finally:
        _WATCH["last"] = "\n".join(lines).strip()
        _WATCH["last_at"] = time.time()
        _WATCH["actions"] += 1
        _BUSY.update(what="", since=0.0)


def _watch_loop():
    time.sleep(30)                       # 开机先安稳一会儿，别跟启动流程抢
    while True:
        try:
            if _WATCH["on"] and not _BUSY["what"]:
                a, v, b = _infra()
                bad = []
                if not a:
                    bad.append("adb")
                if not v:
                    bad.append("虚拟屏守护")
                if not b:
                    bad.append("桥接")
                if bad:
                    _WATCH["fails"] += 1
                    # 连续三轮才动手：重启桥接会把正在读屏的机器人切断，
                    # 代价远高于多等一轮 45 秒。2026-10-03 实测偶发探测超时
                    # 会让两轮判据误触发，形成「重启→更不稳→再重启」的循环。
                    if _WATCH["fails"] >= 3:
                        _WATCH["fails"] = 0
                        _auto_heal(bad)
                else:
                    _WATCH["fails"] = 0
        except Exception as exc:
            print("[botctl] 看护循环异常：%r" % (exc,), flush=True)
        time.sleep(45)


def _page_ver():
    """面板前端代码的版本号（就用 botctl.py 的 mtime）。"""
    try:
        return str(int(os.path.getmtime(__file__)))
    except Exception:
        return "0"


def _with_ver_watch(html_text):
    """往面板页面末尾塞一段「版本变了就自动刷新」的小脚本。

    ★ 2026-10-04 晚：改前端 JS（点「发到群里」时 blur 掉编辑框）之后，
    服务端是新版、可用户浏览器里跑的还是**旧页面**，于是又白失败了一次。
    以后页面每 20 秒问一次 /api/ver，对不上就自己 reload，不用再手动刷。
    """
    tag = ('<script>(function(){var V="%s";setInterval(function(){'
           'fetch("/api/ver",{cache:"no-store"}).then(function(r){return r.json();})'
           '.then(function(d){if(d.ver&&d.ver!==V){location.reload();}})'
           '.catch(function(){});},20000);})();</script>' % _page_ver())
    if "</body>" in html_text:
        return html_text.replace("</body>", tag + "</body>", 1)
    return html_text + tag



# ===========================================================================
# 知识与记忆（2026-10-12 加）
#
# 为什么加：机器人这半年长出了一堆"配置在文件里、只有 agent 能改"的东西
#   （记忆库 / 规则 / 群情摘要 / 向量服务 / 工具用量）—— 用户想看一眼、
#   改一条，都得叫我。这几个接口把它们搬到面板上（用户 2026-10-12 要求
#   「插件化 + 面板配置化」里的"面板"那一半）。
#
# ⚠️ 纪律：这些接口**只读为主**，写操作都进了 `_DESTRUCTIVE_PATHS`（同源校验）；
#   手滑不能把机器人搞坏 —— 所以记忆库只允许加/删单条，不给"整库覆盖"。
# ===========================================================================
VEC_HEALTH = "http://127.0.0.1:8766/health"


def _ensure_path():
    if APPDIR not in sys.path:
        sys.path.insert(0, APPDIR)


def vec_status(timeout: float = 2.5) -> dict:
    """向量服务在不在（面板只显示状态，不替用户做决定）。"""
    try:
        with urllib.request.urlopen(VEC_HEALTH, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
        return {"running": bool(d.get("ok")), "dim": d.get("dim"),
                "cached": d.get("cached"), "hit": d.get("hit"), "miss": d.get("miss")}
    except Exception as exc:
        return {"running": False, "error": str(exc)[:80]}


def know_state() -> dict:
    out = {"ok": True, "memory": [], "summary": {"count": 0, "items": []},
           "vector": {}, "tools": {}}
    try:
        with open(os.path.join(DIR, "记忆库.json"), encoding="utf-8") as fh:
            d = json.load(fh)
        for it in (d.get("items") or []):
            if isinstance(it, dict) and it.get("name"):
                out["memory"].append({"kind": it.get("kind") or "person",
                                      "name": it.get("name"),
                                      "note": it.get("note") or "",
                                      "aliases": it.get("aliases") or []})
    except Exception as exc:
        out["memoryErr"] = str(exc)[:90]
    try:
        with open(os.path.join(DIR, "群情摘要.json"), encoding="utf-8") as fh:
            d = json.load(fh)
        segs = [x for x in (d.get("segments") or []) if isinstance(x, dict)]
        out["summary"] = {
            "count": len(segs), "lastTo": d.get("last_to") or 0,
            "items": [{"to": x.get("to") or 0, "msgs": x.get("msgs") or 0,
                       "text": x.get("text") or ""} for x in segs[-3:]],
        }
    except Exception as exc:
        out["summaryErr"] = str(exc)[:90]
    out["vector"] = vec_status()
    try:
        _ensure_path()
        from wxauto import tools as _T
        out["tools"] = _T.stats(os.path.join(DIR, "工具用量.json"))
    except Exception:
        out["tools"] = {}
    return out


def mem_add(body: dict) -> dict:
    try:
        _ensure_path()
        from wxauto.memory import Memory
        name = str(body.get("name") or "").strip()
        note = str(body.get("note") or "").strip()
        if not name or not note:
            return {"ok": False, "error": "名字和内容都要填"}
        kind = str(body.get("kind") or "person")
        raw = str(body.get("alias") or "").replace("，", ",").replace("、", ",")
        alias = [a.strip() for a in raw.split(",") if a.strip()]
        res = Memory(os.path.join(DIR, "记忆库.json")).add(kind, name, note, aliases=alias)
        return {"ok": bool(res), "result": str(res)}
    except Exception as exc:
        return {"ok": False, "error": repr(exc)[:140]}


def mem_del(body: dict) -> dict:
    try:
        _ensure_path()
        from wxauto.memory import Memory
        name = str(body.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "要删哪一条？"}
        res = Memory(os.path.join(DIR, "记忆库.json")).remove(name)
        return {"ok": bool(res), "result": str(res)}
    except Exception as exc:
        return {"ok": False, "error": repr(exc)[:140]}


def mem_test(body: dict) -> dict:
    """试一句话：分别用「纯子串」和「带向量」跑一遍，把差异摆出来。"""
    text = str(body.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "先写一句话"}
    try:
        _ensure_path()
        from wxauto.memory import Memory
        m = Memory(os.path.join(DIR, "记忆库.json"))
        plain = m.search(text, vector=False)
        withv = m.search(text, vector=True)
        return {"ok": True, "text": text, "plain": plain, "vector": withv,
                "added": [x for x in withv if x not in plain]}
    except Exception as exc:
        return {"ok": False, "error": repr(exc)[:140]}


def sum_run() -> dict:
    """手动跑一段群情摘要（**会调一次模型**，约 3 秒）。"""
    try:
        _ensure_path()
        from wxauto.config import load_config, load_persona, load_personas
        from wxauto.brain import Brain
        from wxauto import groupsum
        cfg = load_config(os.path.join(DIR, "config.yaml"))
        try:
            from main_android import apply_key_file
            apply_key_file(cfg)
        except Exception:
            pass
        brain = Brain(cfg, load_persona(), None, personas=load_personas())
        rep = groupsum.maybe_run(brain, DIR, None,
                                 getattr(cfg, "summary", None), force=True)
        return {"ok": bool(rep.get("ran")), "report": rep}
    except Exception as exc:
        return {"ok": False, "error": repr(exc)[:150]}


def vec_op(op: str) -> dict:
    op = "stop" if str(op) == "stop" else "start"
    try:
        r = subprocess.run(["bash", os.path.join(APPDIR, "embed_ctl.sh"), op],
                           capture_output=True, text=True, timeout=180)
        lines = [x for x in (r.stdout or "").strip().splitlines() if x.strip()]
        return {"ok": r.returncode == 0, "op": op,
                "out": (lines[-1] if lines else "")[:150]}
    except Exception as exc:
        return {"ok": False, "op": op, "error": repr(exc)[:120]}


know_card = """
<style>
 .kbtn{font-size:13px;padding:3px 10px;margin-left:5px;border:1px solid #c7c7cc;
       background:#f6f6fa;border-radius:9px;color:#111}
 .krow{margin:7px 0}
 .kitem{background:#f7f7fa;border-radius:10px;padding:8px 10px;margin:6px 0;
        font-size:13px;line-height:1.5}
 .kname{font-weight:600}
 .knote{color:#444;word-break:break-all}
</style>
<div class="card">
<div class="k">🧠 知识与记忆</div>
<div class="hint">向量检索 · 记忆库 · 群情摘要 · 工具用量（都能在这儿看一眼、改一条）</div>
<div id="knowBody">正在加载…</div>
</div>
<script>
(function(){
  var box = document.getElementById("knowBody");
  if (!box) return;
  function esc(s){ return String(s==null?"":s).replace(/[&<>"]/g, function(c){
     return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]; }); }
  function when(t){ try{ return new Date((t||0)*1000).toLocaleString("zh-CN",
      {month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}); }
      catch(e){ return ""; } }
  function post(url, body){
    return fetch(url, {method:"POST", headers:{"Content-Type":"application/json"},
                       body: JSON.stringify(body||{})}).then(function(r){ return r.json(); });
  }
  function render(d){
    var v = d.vector||{}, s = d.summary||{}, mem = d.memory||[], tl = d.tools||{};
    var h = "";
    h += '<div class="krow"><b>向量服务</b>：' + (v.running
        ? ("✅ 在跑（缓存 " + (v.cached||0) + " 条 · 命中 " + (v.hit||0) + "）")
        : ("⭕ 没在跑" + (v.error ? "（"+esc(v.error)+"）" : "")))
       + '<button class="kbtn" data-act="vec" data-op="start">启动</button>'
       + '<button class="kbtn" data-act="vec" data-op="stop">停掉</button></div>';
    h += '<div class="krow"><b>记忆库</b>：' + mem.length + ' 条'
       + '<button class="kbtn" data-act="add">加一条</button>'
       + '<button class="kbtn" data-act="test">试一句话</button></div>';
    if (mem.length){
      h += '<div class="kitem">';
      for (var i=0;i<mem.length;i++){
        var m = mem[i];
        h += '<div style="margin:3px 0"><span class="kname">' + esc(m.name) + '</span>'
           + ' <span class="hint">' + (m.kind==="topic"?"梗/事件":"人") + '</span>'
           + '<button class="kbtn" data-act="del" data-name="' + esc(m.name) + '">删</button>'
           + '<div class="knote">' + esc(m.note) + '</div></div>';
      }
      h += '</div>';
    }
    h += '<div class="krow"><b>群情摘要</b>：' + (s.count||0) + ' 段'
       + '<button class="kbtn" data-act="sum">立刻跑一段</button>'
       + '<span class="hint">（会调一次模型，约 3 秒）</span></div>';
    var items = (s.items||[]).slice().reverse();
    for (var j=0;j<items.length;j++){
      var it = items[j];
      h += '<div class="kitem"><div class="hint">' + when(it.to) + ' · ' + (it.msgs||0)
         + ' 条消息</div><div class="knote">' + esc(it.text) + '</div></div>';
    }
    var used = (tl.today||{});
    var names = Object.keys(used);
    h += '<div class="krow"><b>工具用量</b>（今天）：'
       + (names.length ? names.map(function(k){ return esc(k)+" "+used[k]+" 次"; }).join(" · ")
                       : '<span class="hint">今天还没用过</span>') + '</div>';
    box.innerHTML = h;
  }
  function load(){
    fetch("/api/know").then(function(r){ return r.json(); }).then(render)
      .catch(function(){ box.innerHTML = '<span class="hint">加载失败</span>'; });
  }
  box.addEventListener("click", function(ev){
    var t = ev.target;
    if (!t || !t.dataset || !t.dataset.act) return;
    var act = t.dataset.act;
    if (act === "vec"){
      t.textContent = "处理中…";
      post("/api/vec", {op: t.dataset.op}).then(load).catch(load);
    } else if (act === "sum"){
      t.textContent = "生成中…";
      post("/api/sum/run", {}).then(function(r){
        if (!r.ok) alert("没跑成：" + (r.report && r.report.reason || r.error || ""));
        load();
      }).catch(load);
    } else if (act === "del"){
      var nm = t.dataset.name || "";
      if (!confirm("删掉记忆「" + nm + "」？")) return;
      post("/api/mem/del", {name: nm}).then(load).catch(load);
    } else if (act === "add"){
      var n1 = prompt("记谁 / 记什么梗？（名字，2 个字以上）");
      if (!n1) return;
      var n2 = prompt("一句话说清它是什么（别超过 60 字）");
      if (!n2) return;
      var k = confirm("确定 = 记「人」；取消 = 记「梗 / 事件」") ? "person" : "topic";
      post("/api/mem/add", {kind:k, name:n1, note:n2}).then(function(r){
        if (!r.ok) alert("没加上：" + (r.error||r.result||""));
        load();
      }).catch(load);
    } else if (act === "test"){
      var q = prompt("试一句话（看它会注入哪几条记忆）：", "上次那个瓜的结论是啥");
      if (!q) return;
      post("/api/mem/test", {text:q}).then(function(r){
        if (!r.ok){ alert("没试成：" + (r.error||"")); return; }
        var msg = "纯子串命中 " + (r.plain||[]).length + " 条\\n"
                + (r.plain||[]).join("\\n")
                + "\\n\\n带向量命中 " + (r.vector||[]).length + " 条\\n"
                + (r.vector||[]).join("\\n");
        if ((r.added||[]).length) msg += "\\n\\n向量多补的：\\n" + r.added.join("\\n");
        alert(msg);
      });
    }
  });
  load();
})();
</script>
"""


def page():
    running = bot_running()
    mode = bot_mode() if running else ""
    bok, btxt = bridge_state()
    fg = physical_foreground()
    wx_in_hand = fg.startswith("com.tencent.mm")

    busy = _BUSY["what"]
    if busy:
        # 启动/停止进行中：这几十秒里进程状态就是"已停止"，必须说清楚在干什么，
        # 否则用户一刷新以为失败，又点一次（每次都会把上一个正在切群的实例杀掉）
        state = "%s中…（已 %s）" % (busy, _fmt_dur(time.time() - _BUSY["since"]))
        color = "#c77700"
    elif running:
        up = _fmt_dur(bot_uptime())
        state, color = (("运行中（%s）" % up) if up else "运行中"), "#0a7d32"
    else:
        state, color = "已停止", "#8a8a8a"
    screen_bad = screen_health() if running else ""

    notes = []
    if busy:
        notes.append("正在%s，通常 10~40 秒（要等切群），别急着再点 —— "
                     "页面会自己刷新，也可以手动下拉刷新。" % busy)
    if not running and not busy:
        if wx_in_hand:
            notes.append("你的手机现在正开着微信（%s）。微信只有一个实例 —— 机器人需要把它"
                         "放到虚拟屏，所以请先退出微信（回到桌面）再点启动。" % fg)
        elif not fg:
            notes.append("启动前请先在手机上退出微信（回到桌面）。微信只有一个实例，"
                         "机器人要把它放到虚拟屏上，如果你的微信正开着会被「抢走」。")
        else:
            notes.append("当前手机前台是 %s，可以点启动。启动后请别再点微信图标 —— "
                         "那会把微信抢回手上，机器人会短暂瞎掉。" % fg)
    if running and not bok:
        notes.append("机器人进程在，但桥接服务没通 —— 可能在启动中，稍等刷新。")
    if screen_bad:
        notes.append("机器人进程活着，但**读不到画面**：%s。它不会去抢你手上的微信"
                     "（这是故意的，免得你正用着被抢走）—— 把微信退回后台（回桌面），"
                     "它自己会恢复；急着用就点一次「启动」。" % screen_bad)
    if not adb_ok():
        bad = "adb 断了（无线调试关着）"
        if not vscreen_ok():
            bad += "，虚拟屏守护也挂了"
        notes.append(bad + " —— 机器人现在跑不起来。"
                     "修复：手机上打开「开发者选项 → 无线调试」，"
                     "然后让 DSH 里的 agent 跑一次 connect-adb.sh 并重启两个守护。")

    def btn(label, action, bg, confirm=""):
        c = ' onclick="return confirm(%s)"' % json.dumps(confirm) if confirm else ""
        return ('<form method="post" action="%s" style="margin:0">'
                '<button%s style="width:100%%;padding:17px;font-size:18px;border:0;'
                'border-radius:14px;background:%s;color:#fff;font-weight:600">%s</button>'
                '</form>' % (action, c, bg, label))

    vsd = vsd_live_port()
    rows = [
        ("状态", '<span style="font-size:26px;font-weight:700;color:%s">%s</span>' % (color, state)),
        ("模式", html.escape(mode or "—")),
        ("桥接服务", '<span style="color:%s;font-weight:600">%s</span>'
         % ("#0a7d32" if bok else "#c0392b", html.escape(btxt))),
        ("手机前台", html.escape(fg or "—")),
        ("adb 直连", '<span style="color:%s;font-weight:600">%s</span>'
         % ("#0a7d32" if adb_ok() else "#c0392b",
            "已连接（快）" if adb_ok() else "断了")),
        ("虚拟屏守护", '<span style="color:%s;font-weight:600">%s</span>'
         % ("#0a7d32" if vsd else "#c0392b",
            "正常（:%s）" % vsd if vsd else "挂了 → 抓不到画面")),
        ("读屏", ('<span style="color:%s;font-weight:600">%s</span>'
                  % ("#c0392b" if screen_bad else "#0a7d32",
                     "读不到画面" if screen_bad else "正常")) if running
         else '<span style="color:#8a8a8a">—</span>'),
        # ★ 2026-10-05 用户要求「自动看护页面也隐藏一下，现在 UI 太乱了」——
        #   功能照跑（_watch_loop 不动），只是状态卡不再占一行。
        ("电量看护", battery_row()),
        ("进程", html.escape(" ".join(bot_pids()) or "—")),
    ]
    rows_html = "".join(
        '<div class="row"><span class="k">%s</span><span>%s</span></div>' % (k, v)
        for k, v in rows)

    warn = "".join('<div class="warn">%s</div>' % html.escape(n) for n in notes)

    # ★ 2026-10-05：手动工具用户说用不上，它的反馈卡片也一起隐藏。
    last_html = ""
    if False and _LAST["what"]:
        last_html = (
            '<div class="card"><div class="k" style="margin-bottom:7px">最近一次操作：'
            '%s <span style="color:%s;font-weight:600">%s</span>'
            '<span style="color:#8a8a8a"> %s</span></div><pre>%s</pre></div>'
            % (html.escape(_LAST["what"]),
               "#0a7d32" if _LAST["ok"] else "#c0392b",
               "成功" if _LAST["ok"] else "失败",
               time.strftime("%H:%M:%S",
                             time.localtime(_LAST["at"] or time.time())),
               html.escape(_LAST["output"] or "")))

    # ★ 2026-10-05 用户要求隐藏自动看护的 UI（功能照跑，只是不占页面）。
    #   想恢复：去掉下面这段的注释符。
    watch_html = ""
    if False and _WATCH["last"]:
        watch_html = (
            '<div class="card"><div class="k" style="margin-bottom:7px">自动看护：'
            '最后一次动作 <span style="color:#8a8a8a">%s</span></div><pre>%s</pre></div>'
            % (time.strftime("%H:%M:%S",
                             time.localtime(_WATCH["last_at"] or time.time())),
               html.escape(_WATCH["last"])))

    # 「实战台」——读消息 → 选一条 → 生成 → 改成我想发的话 → 发 / 换一条 / 不发了。
    # 2026-10-04 晚用户要求：把「演练台」并进来，一个卡片全包。
    fight_card = """
<style>
 .simbtn{width:100%;padding:13px;font-size:15px;border:0;border-radius:12px;
         background:#2b6cb0;color:#fff;margin-top:7px;cursor:pointer}
 .simbtn:active{opacity:.8}
 .simbtn:disabled{background:#9a9a9f}
 .simbtn.alt{background:#0a7d32}
 .simbtn.gray{background:#6b6b70}
 .simrow{background:#f6f6fa;border-radius:10px;padding:8px 10px;margin-top:6px;font-size:14px}
 .simwho{color:#3b6ea5;font-weight:600;margin-right:6px}
 .simwho.me{color:#0a7d32}
 .selfrow{background:#eaf5ec;color:#4a4a4a}   /* 机器人自己发的话：可看、不可点 */
 .simtag{color:#8a8a8a;font-size:12px;margin-top:4px}
 .fightlist{max-height:38vh;overflow-y:auto;-webkit-overflow-scrolling:touch;
            overflow-anchor:none}
 .fightwrap{position:relative}
 .fightjump{position:absolute;right:9px;bottom:9px;border:0;border-radius:999px;
            background:rgba(43,108,176,.93);color:#fff;font-size:12px;
            padding:5px 12px;box-shadow:0 1px 6px rgba(0,0,0,.28);cursor:pointer}
 .fightjump:active{opacity:.8}
 .fighttext{width:100%;box-sizing:border-box;padding:9px 10px;font-size:15px;line-height:1.5;
            border:1px solid #c9c9cf;border-radius:10px;background:#fff;color:#111;
            font-family:inherit;resize:vertical;min-height:66px}
 .dcard{background:#fff8e6;border:1px solid #f0d9a8;border-radius:11px;
        padding:9px 11px;margin-top:7px}
 .dcard .dsrc{color:#8a7a55;font-size:12px;line-height:1.45}
 .dcard .dtxt{font-size:16px;color:#111;font-weight:600;margin:4px 0 0;word-break:break-all}
 .dbtns{display:flex;gap:6px;margin-top:8px}
 .dbtns button{flex:1;padding:9px 0;font-size:13px;border:0;border-radius:9px;
               color:#fff;cursor:pointer}
 .dbtns button:active{opacity:.8}
 .dbtns .ok{background:#0a7d32}
 .dbtns .ed{background:#2b6cb0}
 .dbtns .no{background:#8a8a8a}
 /* ★ 2026-10-09：面板上自己提问那一行（问它一句 → 它答 → 可改可发） */
 .askrow{display:flex;gap:8px;align-items:stretch;margin:0 0 9px}
 /* flex:1 1 0% 而不是 flex:1：基准宽度取 0，剩余空间全给输入框，最不容易被挤扁 */
 .askrow input{flex:1 1 0%;width:auto;min-width:0;padding:9px 10px;
               border:1px solid #ccc;border-radius:8px;font-size:14px}
 /* ⚠️ 2026-10-09 用户截图报「ui 不对」：`.simbtn` 本身是 width:100% 的大按钮
    （给下面"发到群里"那几个用的），直接放进这一行会把整行占满、把输入框挤成
    一个小方块。这里必须把它按回"内容宽度"（`flex:0 0 auto` + `width:auto`），
    padding / 字号也按行内按钮收小一档。 */
 .askrow .simbtn{flex:0 0 auto;width:auto;margin:0;padding:9px 14px;
                 font-size:14px;border-radius:9px;white-space:nowrap}
 .askopt{display:flex;gap:6px;align-items:flex-start;font-size:12.5px;
         color:#555;margin:-4px 0 9px;line-height:1.45}
 .askopt input{margin:2px 0 0}
</style>
<div class="card">
  <div class="k" style="margin-bottom:7px">实战台
    <span class="chatmeta" id="fightmeta">（你点头才发）</span></div>
  <div class="chatempty" style="margin-bottom:6px">
    <b>① 点一条群消息</b> → <b>② 它说一句</b> → <b>③ 想改就改</b> → <b>④ 发 / 换一条</b><br>
    上面那几坨是<b>它白天自己挑、自己写</b>的稿子 —— 发 / 拿去改 / 不发，你说了算；
    点「不发」它就记住这句，下次换一种说法。<br>
    下面框里就是要发出去的话，随便改；改过之后原样发出（不再自动加「喵」）。
    不选消息也行 —— 直接在框里自己写一句发出去。
  </div>
  <div class="fightwrap">
    <div id="fightlist" class="fightlist"></div>
    <button type="button" id="fightjump" class="fightjump"
            style="display:none">↓ 新消息</button>
  </div>
  <div class="askrow">
    <input id="asktext" type="text" autocomplete="off"
           placeholder="问它一句（会展开答）；也可以直接打关键词：看看腿 / 今日美图 / 拍一拍… —— 命中了就点下面的「让机器人发图」">
    <button type="button" class="simbtn" id="askbtn">💬 问它</button>
  </div>
  <label class="askopt">
    <input type="checkbox" id="askquote" checked>
    <span>回答里<b>带上问题</b> —— 群里的人看不到你问的那句，不带就成了一份
      没头没脑的答案（它会把话题点一下再答，仍然是一条）</span>
  </label>
  <div id="fightbox">
    <div class="simrow" id="fightselrow" style="display:none">
      <div><span class="simwho" id="fightwho"></span><span id="fightsel"></span></div>
      <div class="simtag" id="fightkind"></div>
    </div>
    <div id="fighthint" style="font-size:13px;color:#555;margin:9px 0 4px">要发出去的话（可以直接改）：</div>
    <textarea id="fighttext" class="fighttext" rows="3"
              placeholder="在这里写 / 改你要发的话"></textarea>
    <button class="simbtn alt" id="fightsend">✅ 发到群里</button>
    <button class="simbtn" id="fightagain" disabled>🔄 换一条</button>
    <button class="simbtn gray" id="fightclear" disabled>✖️ 不发了</button>
    <!-- ★ 2026-10-09 用户要求：「面板上没有抢一下再发的按钮」。
         机器人发不出去时（浮窗说「你一直占着键盘」）点它 —— 它做两件事：
         ① 把微信实例从物理屏拿回虚拟屏（原来那个「抢」只按 HOME，
            不会搬微信，所以微信在你手上时怎么等都发不出去）；
         ② 重置粘住的输入法会话（在浏览器里点过面板输入框就会粘住）。 -->
    <button class="simbtn alt" id="fightgrab">📱 抢一下（发不出去时点它）</button>
  </div>
  <div id="fightsent" class="chatempty" style="margin-top:6px"></div>
</div>
<script>
(function(){
  var listEl = document.getElementById('fightlist');
  var jumpEl = document.getElementById('fightjump');
  var metaEl = document.getElementById('fightmeta');
  var sentEl = document.getElementById('fightsent');
  var textEl = document.getElementById('fighttext');
  var busy = false, lastTarget = '', lastCand = '', forceCand = false;
  var lastKind = '', lastRule = '';   // 命中的是图片规则时，按钮要变成「发图」
  var listKey = '', curPicks = [], atBottom = true, unseen = 0, lastTotal = 0;

  // ★ 2026-10-04 晚：消息列表原来只在内容变化时重建、重建后停在原地，
  // 用户往下看不到刚读到的消息（还以为没消息）。现在分两种：
  //   贴着底部看 → 跟着滚到最新；自己翻上去看历史 → 原地不动，
  //   右下角给个「↓ N 条新消息」按钮，点一下回到底部。
  // ★ 2026-10-07 用户：「实战台那些消息又不翻到最底下了」+「加一个往下翻到最底下的按钮」。
  //   两处改：
  //   ① `scroll` 事件是**异步**的，常在我们刚设完 scrollTop、布局还没稳时触发 ——
  //      一算 `gapToBottom()` 就把 atBottom 弄成 false，之后 pinBottom 再也不钉了。
  //      所以给"程序钉底"开一个时间窗，窗口内的 scroll 事件**不重算** atBottom。
  //   ② 角标改成"**不贴底就显示**"：有新的写「↓ N 条新消息」，没有就写「↓ 到底」——
  //      等于给用户一个常驻的"翻到最底下"按钮。
  var _pinUntil = 0;
  function gapToBottom(){
    return listEl.scrollHeight - listEl.clientHeight - listEl.scrollTop;
  }
  function jumpBadge(){
    if (!jumpEl) return;
    if (!atBottom){
      jumpEl.style.display = 'block';
      jumpEl.textContent = unseen ? ('↓ ' + unseen + ' 条新消息') : '↓ 到底';
    } else { jumpEl.style.display = 'none'; }
  }
  // ★ 2026-10-07 用户报「一打开实战台，列表停在最上面，不是最新那条」。
  //   设一次 scrollTop 不稳：innerHTML 刚换完、字体/行高还在回流时设的值
  //   会被随后的布局顶回去；手机 WebView 的 -webkit-overflow-scrolling:touch
  //   还会把这次滚动变成异步的。所以「该贴着底部」时多钉几次。
  function pinBottom(){
    if (!atBottom) return;                       // 用户在翻历史 → 绝不打扰
    _pinUntil = Date.now() + 500;                // 这 500ms 内的 scroll 是程序干的
    var pin = function(){
      if (!atBottom) return;                     // 用户已经滚走了 → 这一次别钉
      _pinUntil = Date.now() + 250;              // 我这一下引发的 scroll 也别当真
      listEl.scrollTop = listEl.scrollHeight;
    };
    pin();
    if (window.requestAnimationFrame) requestAnimationFrame(pin);
    setTimeout(pin, 60);
    setTimeout(pin, 200);
  }
  // ★ 2026-10-10 用户报：「一打开实战台，它总是先跳到最早的那条消息上，
  //   过一会儿才能拉到最新 —— 能不能一上来就显示最新的（哪怕是过时的最新也行）」。
  //   根因：首屏的 listEl 高度**不是一次到位的** —— 几十条消息一次 innerHTML 插进来，
  //   字体/行高/折行还要再回流一轮；pinBottom() 那几下点钉（本帧/下一帧/60ms/200ms）
  //   全落在布局稳下来之前。而 scrollHeight 事后变大**不会触发 scroll 事件**，
  //   atBottom 一直是 true，可视觉上就停在顶部（scrollTop 还是 0 = 最早那条），
  //   要等下一次 6 秒轮询的 render 才把它钉下去 —— 这就是"过一会儿才到最新"。
  //   修法：列表**重建**时开一个"持续钉底"窗口（每帧钉一次，默认 2 秒），
  //   不再指望某几下点钉恰好踩在布局稳定之后；用户一旦自己动手滚就立刻让位。
  var _burstUntil = 0, _burstRAF = 0;
  function burstStep(){
    _burstRAF = 0;
    if (!atBottom) return;                       // 用户滚走了 → 让位
    if (Date.now() > _burstUntil) return;        // 窗口到期
    _pinUntil = Date.now() + 500;                // 这段滚动是程序干的
    listEl.scrollTop = listEl.scrollHeight;
    if (window.requestAnimationFrame) _burstRAF = window.requestAnimationFrame(burstStep);
  }
  function pinBottomBurst(ms){
    _burstUntil = Date.now() + (ms || 2000);
    if (!_burstRAF && window.requestAnimationFrame) _burstRAF = window.requestAnimationFrame(burstStep);
  }
  function toBottom(){
    atBottom = true; unseen = 0; jumpBadge(); pinBottom(); pinBottomBurst();
  }
  listEl.addEventListener('scroll', function(){
    if (Date.now() < _pinUntil) return;          // ← 程序钉底引起的，别动 atBottom
    atBottom = gapToBottom() <= 24;
    if (atBottom) unseen = 0;
    jumpBadge();
  });
  // 用户一动手（触摸/滚轮/键盘）就停掉"持续钉底"、并解除屏蔽窗口，
  // 好让他自己的滚动立刻被认出来（否则窗口期内上滑会被程序一直拽回底部）。
  ['touchstart','wheel','keydown','mousedown'].forEach(function(t){
    listEl.addEventListener(t, function(){ _burstUntil = 0; _pinUntil = 0; }, {passive:true});
  });
  if (jumpEl) jumpEl.addEventListener('click', toBottom);
  function esc(s){ return String(s == null ? '' : s).replace(/[&<>"]/g, function(c){
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
  function post(path, body){
    return fetch(path, {method:'POST', headers:{'Content-Type':'application/json'},
                        body: JSON.stringify(body || {})}).then(function(r){ return r.json(); });
  }
  function kindText(k){
    if (k === 'rule')  return '这条命中了关键词规则：固定话术，换一条也还是它';
    if (k === 'image') return '这条命中**表情包规则** —— 点下面的「\U0001f5bc 让机器人发图」，它会真发一张表情包（框里写了字就当配文）';
    if (k === 'meitu') return '这条命中**美图规则** —— 点下面的「\U0001f5bc 让机器人发图」，它真发一张图（框里写了字就当配文，留空就用规则自带的话）';
    return '（走模型生成，可以随便改）';
  }
  function setCand(v){ lastCand = v || ''; textEl.value = lastCand; }
  function render(st){
    lastTarget = st.target || '';
    var t = '（你点头才发 → ' + (lastTarget || '?') + '）';
    if (st.seen){
      var ago = Math.max(0, Math.round(Date.now()/1000 - st.seen));
      t = (ago > 90) ? ('⚠️ 机器人现在没在读屏（最后读到 ' + ago + ' 秒前）')
                     : ((lastTarget || '?') + ' · 最后读到 ' + ago + ' 秒前');
    }
    metaEl.textContent = st.error ? ('⚠️ ' + st.error) : t;
    // 消息列表只在内容真的变了才重建 DOM —— 否则 6 秒一刷会把用户正在看的
    // 滚动位置顶回去，手指刚要点的那条也会跳走。重建后要不要跟着滚到最新，
    // 看用户当时是不是贴在底部（见上面的 atBottom）。
    var picks = st.pickable || [];
    if (!atBottom && st.total && lastTotal && st.total > lastTotal){
      unseen += st.total - lastTotal;
    }
    lastTotal = st.total || lastTotal;
    var key = JSON.stringify(picks);
    if (key !== listKey){
      var firstDraw = !listKey;
      var prevTop = listEl.scrollTop;
      var gapBefore = gapToBottom();
      var stick = firstDraw || atBottom || gapBefore <= 24;
      listKey = key;
      curPicks = picks;
      var html = '';
      for (var i = 0; i < picks.length; i++){
        var pk = picks[i];
        // 机器人自己发的话也列出来（灰绿底、不可点）：不列的话上下文断掉，
        // 看着就像"群里没人接话"。2026-10-05 用户要求看见"所有消息"。
        html += pk.self
          ? ('<div class="simrow selfrow" data-i="' + i + '">'
             + '<span class="simwho me">（我说的）</span>' + esc(pk.text) + '</div>')
          : ('<div class="simrow" style="cursor:pointer" data-i="' + i + '">'
             + '<span class="simwho">' + esc(pk.who) + '</span>' + esc(pk.text) + '</div>');
      }
      listEl.innerHTML = html
        || '<div class="chatempty">还没有可选的消息（机器人还没读到群里的话）</div>';
      if (stick){
        // 跟着滚到最新那条（pinBottom 点钉几下 + pinBottomBurst 持续钉 2 秒，
        // 兜住"首屏高度晚一步才稳"的情况 —— 见上面 2026-10-10 的说明）
        atBottom = true; unseen = 0; pinBottom(); pinBottomBurst();
      } else {
        // 用户自己在翻历史：新消息是在底部追加的，把他正看的位置原样还给他
        listEl.scrollTop = prevTop;
        atBottom = false;
      }
      jumpBadge();
      var rows = listEl.querySelectorAll('.simrow');
      for (var j = 0; j < rows.length; j++){
        rows[j].addEventListener('click', function(ev){
          if (busy) return;
          var i = parseInt(ev.currentTarget.getAttribute('data-i'), 10);
          var p = curPicks[i];
          if (!p) return;
          if (p.self){ metaEl.textContent = '这条是机器人自己发的 —— 挑群友的话才让它回'; return; }
          busy = true; metaEl.textContent = '生成中…（十几秒）';
          post('/fight/pick', {who: p.who, text: p.text}).then(function(){
            busy = false; forceCand = true; tick();
          }).catch(function(){ busy = false; });
        });
      }
    }
    var selRow = document.getElementById('fightselrow');
    var hintEl = document.getElementById('fighthint');
    if (st.msg){
      selRow.style.display = 'block';
      document.getElementById('fightagain').disabled = false;
      document.getElementById('fightclear').disabled = false;
      document.getElementById('fightwho').textContent = (st.who || '你') + '：';
      document.getElementById('fightsel').textContent = st.msg;
      document.getElementById('fightkind').textContent = kindText(st.kind);
      lastKind = st.kind || ''; lastRule = st.rule || '';
      // ★ 2026-10-09：图片规则（看看腿/今日美图…）在面板上要**真发图**，
      //   所以按钮得换成「发图」—— 不然点下去发的是那句演练占位文本。
      document.getElementById('fightsend').textContent =
        ((lastKind === 'meitu' || lastKind === 'image')
         ? '\U0001f5bc 让机器人发图' : '\u2705 发到群里');
      var isPic = (lastKind === 'meitu' || lastKind === 'image');
      hintEl.textContent = isPic
        ? '图后面跟的话（**留空就只发图**，不填它就用规则自带的那几句）：'
        : '要发出去的话（可以直接改）：';
      var editing = (document.activeElement === textEl);
      if (forceCand || (!editing && (textEl.value === '' || textEl.value === lastCand))){
        // ★ 2026-10-09：图片规则的候选是**演练占位文本**
        //   （「（会发一张今日美图 · 分类：X）」）—— 那是写给人看的，
        //   一旦落进编辑框就会被当成配文真发出去（用户已经踩过一次）。
        //   所以这种情况**不往框里填**，留空 = 只发图（配文走规则自带候选）。
        setCand(isPic ? '' : (st.cand || ''));
        forceCand = false;
      } else {
        lastCand = st.cand || lastCand;
      }
    } else {
      // 没选消息 = 「自己写一句」模式：编辑框常显，只是没有候选可换
      selRow.style.display = 'none';
      document.getElementById('fightagain').disabled = true;
      document.getElementById('fightclear').disabled = true;
      hintEl.textContent = '不选消息也行，直接在这里写一句发出去：';
      lastKind = ''; lastRule = '';
      document.getElementById('fightsend').textContent = '\u2705 发到群里';
      if (forceCand){ setCand(''); forceCand = false; }
    }
    var s = st.sent || [];
    sentEl.innerHTML = s.length
      ? ('已经发出去过：' + s.map(function(x){ return '「' + esc(x.text) + '」'; }).join('、'))
      : '';
    if (atBottom) pinBottom();   // ★ 2026-10-07：贴底时每渲染一帧都重新钉住
  }
  function tick(){
    fetch('/api/fight', {cache:'no-store'}).then(function(r){ return r.json(); })
      .then(render).catch(function(){});
  }
  // ★ 2026-10-09 用户要求：「我想在机器人面板里输入问题，然后机器人回答我」。
  //   走 /fight/ask —— 后端固定按**被 @ 问到**那一档生成（会展开答，见
  //   config 的 mention_max_*）。答完的候选照样填进编辑框：可以改、可以换一条、
  //   也可以点「不发了」。
  var askEl = document.getElementById('asktext');
  var askBtn = document.getElementById('askbtn');
  var askQuoteEl = document.getElementById('askquote');
  var ASK_IDLE = '💬 问它';
  function askIt(){
    if (busy) return;
    var q = (askEl.value || '').trim();
    if (!q){ alert('先在上面写一句你想问它的'); return; }
    busy = true;
    askBtn.disabled = true;
    askBtn.textContent = '💭 它在想…';
    function done(){
      busy = false; askBtn.disabled = false; askBtn.textContent = ASK_IDLE;
    }
    // quote：让回答里带上问题（默认开 —— 群里看不到你问的那句）
    post('/fight/ask', {text: q, quote: (!askQuoteEl || askQuoteEl.checked)}
    ).then(function(){
      done();
      forceCand = true;   // 新候选要**强制**填进编辑框，否则会被"你正在编辑"挡住
      tick();
    }).catch(done);
  }
  var grabBtn = document.getElementById('fightgrab');
  if (grabBtn){
    grabBtn.addEventListener('click', function(){
      if (busy) return;
      busy = true;
      grabBtn.disabled = true;
      var old = grabBtn.textContent;
      grabBtn.textContent = '📱 正在把微信拿回来…';
      post('/fight/grab', {}).then(function(r){
        busy = false; grabBtn.disabled = false; grabBtn.textContent = old;
        alert(((r && r.ok) ? '好了：' : '没弄成：') + ((r && r.message) || '')
              + '。机器人会自己接着把攒的话发出去。');
      }).catch(function(){
        busy = false; grabBtn.disabled = false; grabBtn.textContent = old;
        alert('请求出错');
      });
    });
  }
  askBtn.addEventListener('click', askIt);
  askEl.addEventListener('keydown', function(e){
    if (e.key === 'Enter'){ e.preventDefault(); askIt(); }
  });

  document.getElementById('fightsend').addEventListener('click', function(){
    if (busy) return;
    var t = (textEl.value || '').trim();
    // ★ 2026-10-09：命中的是**图片规则**（看看腿 / 今日美图…）时，框里那句是
    //   simulate 的**演练占位文本**（「（会发一张今日美图 · 分类：X）」）——
    //   那根本不是能发的话（用户 2026-10-09 真把它点出去过，群里就多了一句
    //   没头没脑的假消息）。所以这条路改走 /fight/send_image：**真发图**，
    //   框里的字当配文（留空就用规则自带的那几句候选）。
    if (lastKind === 'meitu' || lastKind === 'image'){
      if (!confirm('让机器人发一张图到「' + (lastTarget || '目标群') + '」？'
                   + (t ? ('\\n\\n图后面跟一句：' + t) : '\\n\\n（后面不跟话）'))) return;
      try { if (document.activeElement && document.activeElement.blur) document.activeElement.blur(); } catch (e) {}
      busy = true;
      metaEl.textContent = '正在取图并发出去…（可能要下载 / 转格式，十几秒）';
      post('/fight/send_image', {text: t}).then(function(r){
        busy = false;
        if (r && r.ok){
          forceCand = true;
          alert(r.error ? ('图发出去了' + r.error) : '图发出去了');
        } else {
          alert('没发出去：' + ((r && r.error) || '未知原因'));
        }
        tick();
      }).catch(function(){ busy = false; alert('请求出错'); });
      return;
    }
    if (!t){ alert('框里还没有要发的话'); return; }
    if (!confirm('真的发到「' + (lastTarget || '目标群') + '」？\\n\\n' + t)) return;
    // ★ 2026-10-04 晚：先把浏览器这边的输入焦点交还掉。焦点若留在编辑框里，
    // 系统输入法就绑在物理屏的 WebView 上，虚拟屏微信的输入框收不到 commitText
    // （面板报「发送接口没回 ok」的另一半原因）。
    try { if (document.activeElement && document.activeElement.blur) document.activeElement.blur(); } catch (e) {}
    busy = true;
    metaEl.textContent = '发送中…（切回群 + 输入 + 点发送，十几秒）';
    function doSend(force){
      metaEl.textContent = force ? '抢一下再发…' : '发送中…（切回群 + 输入 + 点发送，十几秒）';
      return post('/fight/send', {text: t, force: !!force}).then(function(r){
        busy = false;
        if (r.ok){ forceCand = true; tick(); return; }
        var e = r.error || '未知原因';
        // ★ 2026-10-05：最常见的失败是"你在用手机，微信抢不到输入焦点"（桥接为了
        //   不把 DSH 弄黑屏，默认不去抢）。那种情况给用户一次**知情**的抢发机会：
        //   点「确定」就按一次物理屏 HOME 把焦点要回来，你正在看的页面会到后台。
        if (!force && /用手机|焦点/.test(e)
            && confirm('没发出去：' + e + '—— 要不要让它抢一下、马上再发一次？（手机会被切回桌面，你正在看的页面会到后台）')){
          return doSend(true);
        }
        alert('没发出去：' + e);
        tick();
      }).catch(function(){ busy = false; });
    }
    doSend(false);
  });
  document.getElementById('fightagain').addEventListener('click', function(){
    if (busy) return;
    busy = true; metaEl.textContent = '换一条…';
    post('/fight/again').then(function(){ busy = false; forceCand = true; tick(); })
      .catch(function(){ busy = false; });
  });
  document.getElementById('fightclear').addEventListener('click', function(){
    if (busy) return;
    post('/fight/clear').then(function(){ forceCand = true; tick(); });
  });
  tick();
  setInterval(function(){ tick(); }, 6000);
  // ★ 2026-10-07：首屏的 tick() 可能跑在最终布局（字体/滚动条宽度）之前，
  //   load 完再钉一次，保证"打开就是最新那条"。
  window.addEventListener('load', function(){
    setTimeout(function(){ if (atBottom){ pinBottom(); pinBottomBurst(); } }, 300);
  });
  // ★ 2026-10-05：用户报「切出去再回来，实战台的消息一直空着/像没了」。手机浏览器
  //   在后台会把 setInterval 节流甚至冻结，回到前台还要等下一个 6 秒周期才刷新 ——
  //   这段时间里页面就停在旧状态（哪怕那一帧恰好是空的）。重新可见时立刻补一次。
  document.addEventListener('visibilitychange', function(){
    if (!document.hidden){ tick(); if (atBottom) pinBottomBurst(); }
  });
})();
</script>
"""


    # 「📚 学习记录」——2026-10-05 用户要求：发出去的也要记，方便以后总结人设。
    # ⚠️ 这段字符串会被 page() 用 % 格式化，里面**不能出现裸的百分号**。

    # 「读屏节奏」——2026-10-04 用户要求：读屏间隔（10 秒~1 小时）和回复延迟
    # 都要能自己随手调，而且**不用重启机器人**。面板只管写 tuning.json，
    # 机器人那边每轮看一眼就套用（见 wxauto/tuning.py）。
    st = tune_state()
    if st["ok"]:
        lo, hi = st["dmin"], st["dmax"]
        dly = (_num_txt(lo) if (lo is not None and hi is not None
                                and abs(hi - lo) < 1e-9)
               else "%s ~ %s" % (_num_txt(lo), _num_txt(hi)))
        cur_line = ("现在：每 <b>%s</b> 秒读一次屏（%s）；识别到消息后等 <b>%s</b> 秒再发（%s）。"
                    % (_num_txt(st["poll"]), html.escape(st["poll_from"]),
                       dly or "—", html.escape(st["delay_from"])))
        slow_note = ""
        if st["slow_window"] and st["slow_interval"]:
            slow_note = ("<div class=\"warn\" style=\"margin:9px 0 0\">夜间 %s 是慢速时段，"
                         "那儿仍按 %s 秒一帧跑（拿漏消息换电）。想全天统一，"
                         "改 config.yaml 的 pace.slow_poll_window。</div>"
                         % (html.escape(st["slow_window"]), _num_txt(st["slow_interval"])))
    else:
        cur_line = "读不到节奏配置：%s" % html.escape(st["error"])
        slow_note = ""

    tune_card = """
<style>
 .trow{display:flex;gap:8px;align-items:center;margin-top:9px;flex-wrap:wrap}
 .trow label{flex:none;font-size:14px;color:#333}
 .trow input{width:80px;padding:9px;font-size:16px;border:1px solid #c9c9cf;
             border-radius:10px;background:#fff;color:#111;box-sizing:border-box}
 .trow .u{font-size:12px;color:#8a8a8a}
 .tbtn{width:100%%;padding:14px;font-size:16px;border:0;border-radius:12px;
       background:#0a7d32;color:#fff;margin-top:10px;font-weight:600;cursor:pointer}
 .tbtn.gray{background:#6b6b70;font-weight:400;font-size:14px;padding:11px}
</style>
<div class="card">
  <div class="k" style="margin-bottom:7px">读屏节奏
    <span class="chatmeta">（改完最迟 2 秒生效，不用重启）</span></div>
  <div style="font-size:13px;color:#555;line-height:1.65">%s</div>
  <div class="trow">
    <label>读屏间隔</label>
    <input id="tunePoll" type="number" min="%d" max="%d" step="1" value="%s">
    <span class="u">秒（%d ~ %d）</span>
  </div>
  <div class="trow">
    <label>回复延迟</label>
    <input id="tuneDmin" type="number" min="0" max="%d" step="0.5" value="%s">
    <span class="u">~</span>
    <input id="tuneDmax" type="number" min="0" max="%d" step="0.5" value="%s">
    <span class="u">秒（识别到之后等这么久再发）</span>
  </div>
  %s
  <button class="tbtn" id="tuneSave">保存节奏</button>
  <button class="tbtn gray" id="tuneReset">恢复默认（读 config.yaml）</button>
  <div class="chatempty" id="tunemsg" style="margin-top:7px">
    读屏间隔越小越跟得上，也越费电（OCR 一帧约 0.6 秒，手机实测常驻 2.6~2.9 个核）；
    间隔里刷过去的群消息它永远看不到 —— 这是拿漏消息换电的旋钮。
  </div>
</div>
<script>
(function(){
  function post(path, body){
    return fetch(path, {method:'POST', headers:{'Content-Type':'application/json'},
                        body: JSON.stringify(body || {})}).then(function(r){ return r.json(); });
  }
  var msg = document.getElementById('tunemsg');
  document.getElementById('tuneSave').addEventListener('click', function(){
    msg.textContent = '保存中…';
    post('/tune', {poll_interval: document.getElementById('tunePoll').value,
                   delay_min: document.getElementById('tuneDmin').value,
                   delay_max: document.getElementById('tuneDmax').value}).then(function(r){
      msg.textContent = r.ok ? ('✅ 已保存：' + r.msg + '（最迟 2 秒生效）')
                             : ('⚠️ ' + (r.error || '保存失败'));
    }).catch(function(){ msg.textContent = '⚠️ 面板连不上，稍后再试'; });
  });
  document.getElementById('tuneReset').addEventListener('click', function(){
    if (!confirm('恢复成 config.yaml 里的默认节奏？')) return;
    post('/tune/reset', {}).then(function(r){
      msg.textContent = '✅ ' + (r.msg || '已恢复默认');
    }).catch(function(){ msg.textContent = '⚠️ 面板连不上，稍后再试'; });
  });
})();
</script>
""" % (cur_line, st["poll_min"], st["poll_max"], _num_txt(st["poll"]),
       st["poll_min"], st["poll_max"], st["delay_hi"], _num_txt(st["dmin"]),
       st["delay_hi"], _num_txt(st["dmax"]), slow_note)

    # 「⭐ 特别关注」——2026-10-16 用户要求：按在面板上，名单上的人一说话
    # 机器人立即回复。名单填的是**昵称**（机器人读屏只拿得到昵称）。
    fst = follow_state()
    if fst["ok"]:
        fdesc = fst["desc"]
        fnote = ("<span style=\"color:#0a7d32;font-weight:600\">生效中</span>"
                 if fst["enabled"] else
                 "<span style=\"color:#c77700;font-weight:600\">已暂停</span>")
        ftext = html.escape("\n".join(fst["ids"]))
        fmax = int(fst.get("max") or 200)
    else:
        fdesc = "⚠️ 读不到名单：%s" % html.escape(str(fst["error"]))
        fnote, ftext, fmax = "—", "", 200

    follow_card = """
<style>
 .farea{width:100%%;padding:10px;font-size:16px;border:1px solid #c9c9cf;
        border-radius:10px;background:#fff;color:#111;box-sizing:border-box;
        min-height:112px;line-height:1.6;margin-top:9px}
 .fchk{display:flex;align-items:center;gap:7px;font-size:14px;color:#333;margin-top:10px}
 .fchk input{width:18px;height:18px}
</style>
<div class="card">
  <div class="k" style="margin-bottom:7px">⭐ 特别关注
    <span class="chatmeta">（名单上的人一说话，机器人立即回复）</span></div>
  <div style="font-size:13px;color:#555;line-height:1.7">
    <b>填 TA 在群里的昵称</b>（机器人读屏只认得昵称，拿不到微信号）；
    可以只填主名 —— 名单写「张三」，屏上读到「张三（今晚打本）」也认得出。
    <br>每行一个名字（逗号、顿号也行）。现在：<b>%s</b> · %s
    <br>·「立即回复」= 这个人说话就当他被 @：不靠关键词、不靠话题打分抽签，直接生成并发出；
    静默时段 / 最小回复间隔 / 本波连回上限都豁免，<b>但每分钟、每小时、每天的条数上限照旧</b>
    （防封号底线，跟被 @ 一个待遇）。
    <br>· 只对机器人待着的那个群生效（见 <code>config.yaml → target.group</code>）。私聊它管不到 ——
    机器人只在群里读屏。
    <br>· 存 <code>data/follow.json</code>，保存后最迟一轮读屏生效，不用重启机器人；
    删掉这个文件就等于取消全部特别关注。
  </div>
  <textarea id="followIds" class="farea" placeholder="张三&#10;李四">%s</textarea>
  <div class="fchk">
    <input type="checkbox" id="followOn"%s>
    <label for="followOn">生效（取消勾选 = 名单留着，但不回复）</label>
  </div>
  <button class="tbtn" id="followSave">保存名单</button>
  <div class="chatempty" id="followmsg" style="margin-top:7px">
    名单最多 %d 人。名字写错不会报错，只是匹配不上 —— 以机器人实际读到的昵称为准；
    把名字全删掉保存 = 取消全部特别关注。
  </div>
</div>
<script>
(function(){
  function post(path, body){
    return fetch(path, {method:'POST', headers:{'Content-Type':'application/json'},
                        body: JSON.stringify(body || {})}).then(function(r){ return r.json(); });
  }
  var btn = document.getElementById('followSave');
  if (!btn) return;
  var msg = document.getElementById('followmsg');
  btn.addEventListener('click', function(){
    var on = document.getElementById('followOn');
    var box = document.getElementById('followIds');
    if (!box.value.trim() && !confirm('名单是空的 —— 保存后会取消全部特别关注，确定？')) return;
    msg.textContent = '保存中…';
    post('/follow', {ids: box.value,
                     enabled: !on || on.checked}).then(function(r){
      msg.textContent = r.ok ? ('✅ ' + r.msg + '（最迟一轮读屏生效）')
                             : ('⚠️ ' + (r.error || '保存失败'));
    }).catch(function(){ msg.textContent = '⚠️ 面板连不上，稍后再试'; });
  });
})();
</script>
""" % (fdesc, fnote, ftext, " checked" if fst["enabled"] else "", fmax)

    return """<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#111">
<title>微信机器人</title>
<style>
 body{margin:0;padding:16px;background:#f2f2f7;color:#111;
      font-family:-apple-system,"PingFang SC","Noto Sans CJK SC",sans-serif}
 h1{font-size:21px;margin:2px 0 14px}
 .card{background:#fff;border-radius:16px;padding:14px;margin-bottom:13px;
       box-shadow:0 1px 3px rgba(0,0,0,.08)}
 .row{display:flex;justify-content:space-between;align-items:baseline;gap:10px;
      padding:7px 0;font-size:15px;border-bottom:1px solid #f2f2f2}
 .row:last-child{border-bottom:0}
 .k{color:#6b6b70;flex:none}
 .row span:last-child{text-align:right;word-break:break-all}
 pre{background:#1c1c1e;color:#d6d6d6;padding:11px;border-radius:12px;
     font-size:11px;line-height:1.5;overflow-x:auto;margin:0;white-space:pre-wrap}
 .grid{display:grid;gap:9px}
 .warn{background:#fff4e5;color:#8a5300;padding:11px 13px;border-radius:12px;
       font-size:13px;line-height:1.55;margin-bottom:11px}
 .hint{color:#8a8a8a;font-size:12px;text-align:center;line-height:1.7}
 .chatmeta{font-weight:400;color:#8a8a8a;font-size:12px;margin-left:6px}
 .chatempty{color:#9a9a9f;font-size:13px;padding:4px 0;line-height:1.6}
</style></head><body>
<h1>微信机器人</h1>
<!-- ★ 2026-10-12 用户要求：「启动、停止、抢一下置顶，后面放实战台」。
     这三个是最常用的，放在页面最上面，打开就能点，
     不用往下翻到实战台里。 -->
<div class="card grid">
  %s
  %s
  %s
</div>
%s
<div class="card">%s</div>
<div class="card grid">
  %s
</div>
<div class="card grid">
  <form method="post" action="/hud/start" style="margin:0">
    <button style="width:100%%;padding:15px;font-size:16px;border:0;border-radius:14px;background:#2f6bd8;color:#fff;font-weight:600">🪟 拉起浮窗（划掉了就点它）</button>
  </form>
  <form method="post" action="/hud/stop" style="margin:0">
    <button style="width:100%%;padding:15px;font-size:16px;border:0;border-radius:14px;background:#8a8f98;color:#fff;font-weight:600">🚫 关掉浮窗（现在不想看）</button>
  </form>
</div>
<div class="card" style="display:none">
  <div class="k" style="margin-bottom:7px">手动工具（机器人没跑也能用）</div>
  <div class="grid">
    %s
    %s
    %s
    %s
  </div>
</div>
%s
%s
%s
<div class="card">
  <div class="k" style="margin-bottom:7px">最近日志</div>
  <pre>%s</pre>
</div>
<style>
 /* 2026-10-06 用户要求：「太臃肿了，不用的时候收起来，需要时点开」。
    纯加法：不动任何卡片结构，只给每张卡的标题行挂折叠开关。
    默认：第 1 张（状态卡）和 .card.grid（启停按钮）保持展开，其余收起。 */
 .foldhead{cursor:pointer;-webkit-user-select:none;user-select:none}
 .foldhead:after{content:" ▾";color:#9aa0aa;font-weight:400}
 .card.folded>.foldhead:after{content:" ▸"}
 .card.folded>*:not(.foldhead){display:none !important}
</style>
<script>
(function(){
  try{
    var cards = document.querySelectorAll('div.card');
    for (var i = 0; i < cards.length; i++){
      var c = cards[i], head = null;
      // ⚠️ 必须取**直接子元素**的 .k：折叠的 CSS 是 `.card.folded>*:not(.foldhead)`，
      //    只认直接子元素。原来用 `c.querySelector('.k')`（任意深度）会让
      //    「.k 包在 .row 里」的状态卡也被加 folded，而 CSS 隐藏的是整个
      //    .row —— 于是**整张卡变成一块空白**。
      //    2026-10-12 实测：把三个按钮置顶后，状态卡不再是第一张 → 露出这个 bug。
      for (var j = 0; j < c.children.length; j++){
        var ch = c.children[j];
        if (ch.classList && ch.classList.contains('k')){ head = ch; break; }
      }
      if (!head) continue;
      head.classList.add('foldhead');
      head.setAttribute('title', '点一下收起 / 展开');
      if (!((i === 0) || c.classList.contains('grid'))) c.classList.add('folded');
      head.addEventListener('click', function(ev){
        var card = ev.currentTarget.parentNode;
        if (card) card.classList.toggle('folded');
      });
    }
  }catch(e){}
})();
</script>
<div class="hint">浏览器菜单 →「添加到主屏幕」= 桌面图标<br>面板端口 %d</div>
<script>
 // 只在"还在动"的时候自动刷：启动/停止进行中（busy）才需要一直盯着看，
 // 一旦落定（跑起来了 / 停稳了）就停手 —— 否则页面没完没了地刷，用户会以为坏了。
 // ⚠️ 刷的时候必须把 spin=1 带着：老版本刷成 "/?t=…" 把标记丢了，于是**只自动刷一次**，
 //    用户点完启动看到的就是"已停止"，自然以为失败（2026-10-03 实测）。
 window.__KEEP_SPIN = %d;
 if (location.search.indexOf("spin=1") >= 0 && window.__KEEP_SPIN) {
   let n = 0;
   const t = setInterval(() => { n++; if (n > 30) clearInterval(t);
     else location.replace("/?spin=1&t=" + Date.now()); }, 2500);
 }
</script>
</body></html>""" % (
        # ★ 置顶的三个：启动 / 停止 / 抢一下
        #   「抢一下」复用 /fight/grab（fight_grab）：把微信 /takeover 拿回虚拟屏
        #   + 重置粘住的输入法会话。原来它只在实战台里，
        #   发不出去时还得往下翻。
        btn("启动 / 重启（真跑）", "/start", "#0a7d32",
            "重启后它会开始回群里的消息（真跑）。确定？" if running else "真的要在群里发消息了，确定？"),
        btn("停止", "/stop", "#c0392b", "停掉机器人（现在不会往群里说话）。确定？"),
        btn("📱 抢一下（把微信拿回虚拟屏）", "/fight/grab", "#b06a00"),
        warn, rows_html,
        btn("启动（干跑 · 只记录不发）", "/dry", "#2b6cb0"),
        btn("🫸 收回微信（并切回群）", "/reclaim", "#5b4b8a",
            "把微信搬回虚拟屏并切回目标群（只搬微信，不重启机器人）。"
            "你拿走微信后，只要两屏前台都不是微信、持续 10 秒，它也会自动收回"),
        btn("修复连接（守护+微信+桥接）", "/repair", "#0f766e",
            "会重启虚拟屏守护并重建屏幕，机器人得重新启动，确定？"),
        btn("重连 adb（无线调试）", "/adb", "#6b5b00"),
        # ★ 2026-10-05 用户要求隐藏；功能还在，直接 POST /watch 或去掉这层 span 即可。
        '<span style="display:none">' + btn(
            "自动看护：关掉" if _WATCH["on"] else "自动看护：打开", "/watch",
            "#c0392b" if _WATCH["on"] else "#0a7d32",
            "关掉之后桥接/守护/adb 出问题就不会自动修了，确定？" if _WATCH["on"] else "")
        + '</span>',
        last_html,
        watch_html,
        # ★ 实战台 + 调参 + 知识记忆：按用户要求**放到按钮后面**
        fight_card + tune_card + follow_card + know_card,
        html.escape(log_tail()), PORT, 1 if busy else 0)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _html(self, body, code=200, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._html(json.dumps(obj, ensure_ascii=False), code,
                   ctype="application/json; charset=utf-8")

    def _body(self):
        """POST 的 JSON body（拿不到就给空 dict，别让面板 500）。"""
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}

    def do_GET(self):
        if self.path.startswith("/api/fight"):
            self._json(fight_state())
            return
        if self.path.startswith("/api/learn"):
            self._json(learn_state())
            return
        if self.path.startswith("/api/know"):
            self._json(know_state())
            return
        if self.path.startswith("/api/ver"):
            self._json({"ok": True, "ver": _page_ver()})
            return
        if self.path.startswith("/api"):
            bok, btxt = bridge_state()
            self._html(json.dumps({"running": bot_running(), "mode": bot_mode(),
                                   "bridge": bok, "bridgeText": btxt,
                                   "physical": physical_foreground(),
                                   "pids": bot_pids()}, ensure_ascii=False),
                       ctype="application/json; charset=utf-8")
            return
        self._html(_with_ver_watch(page()))

    def do_POST(self):
        p = self.path.split("?")[0]
        # ★ 2026-10-08 审查修复（S3）：破坏性路由加一道**同源校验**。
        #   原来面板绑 0.0.0.0 且零鉴权 —— 同一个 WiFi 下任何设备都能 POST /start
        #   让机器人以主人的身份往群里说话（/stop、/fight/send、/tune 同理）。
        #   现在：① main() 改成只绑 127.0.0.1（外部根本连不上）；
        #   ② 这里再挡一层"网页发起的跨站请求"（CSRF）：浏览器直开面板时
        #      Origin/Referer 是本机地址或为空，恶意网页代发的请求会带上它自己的
        #      域名 —— 那种一律拒绝。
        if p in _DESTRUCTIVE_PATHS:
            _org = (self.headers.get("Origin") or self.headers.get("Referer") or "")
            if _org and not _same_origin(_org):
                self._json({"ok": False,
                            "error": "拒绝跨站请求（来源 %s）" % _org[:80]}, 403)
                return
        # ---- 知识与记忆（2026-10-12）----
        if p == "/api/mem/add":
            self._json(mem_add(self._body()))
            return
        if p == "/api/mem/del":
            self._json(mem_del(self._body()))
            return
        if p == "/api/mem/test":
            self._json(mem_test(self._body()))
            return
        if p == "/api/sum/run":
            self._json(sum_run())
            return
        if p == "/api/vec":
            self._json(vec_op(self._body().get("op")))
            return
        # ---- 实战台（我选消息 → 生成 → 我点发送 / 换一条）----
        if p == "/fight/pick":
            b = self._body()
            self._json({"ok": fight_pick(b.get("who"), b.get("text"))})
        elif p == "/fight/again":
            self._json({"ok": fight_again()})
        elif p == "/fight/ask":
            # ★ 2026-10-09：面板上自己提问 → 按「被 @」那档生成（只生成，不发送）。
            #   ⚠️ 不进 _DESTRUCTIVE_PATHS：它不动手发消息，和 /fight/pick 一个级别。
            b = self._body()
            self._json({"ok": fight_ask(b.get("text"),
                                        quote=b.get("quote", True))})
        elif p == "/fight/clear":
            # ★ 2026-10-05：这里也是"我没看上"的一种 —— 让机器人自己生成了一条、
            #   主人觉得不行。既然他明确说过「不让发的就下次注意不要这样说」，
            #   这一下也要记进拒稿记录（有候选才记，纯清空不算）。
            fight_clear(remember=True)
            self._json({"ok": True})
        elif p == "/fight/grab":
            # ★ 2026-10-09：把微信拿回虚拟屏 + 重置输入法会话（见 fight_grab）
            ok, msg = fight_grab()
            self._json({"ok": ok, "message": msg})
        elif p == "/fight/send_image":
            # ★ 2026-10-09：图片规则（看看腿 / 今日美图…）在面板上是**真发图**，
            #   不是发那句「（会发一张今日美图 · 分类：X）」的演练占位文本。
            b = self._body()
            with _LOCK:
                _rname = str(_FIGHT.get("rule") or "")
                _cand = str(_FIGHT.get("cand") or "")
                _msg = str(_FIGHT.get("msg") or "")
                _who = str(_FIGHT.get("who") or "")
            ok, err = fight_send_image(_rname, text=str(b.get("text") or ""),
                                       origin="manual", who=_who, draft=_cand)
            with _LOCK:
                if ok:
                    _FIGHT["sent"].append({"text": "[图片] " + (_rname or "图"),
                                           "at": time.time()})
                    _FIGHT["cand"] = ""
                    _FIGHT["msg"] = ""
                    _FIGHT["rule"] = ""
                else:
                    _FIGHT["error"] = err
            self._json({"ok": ok, "error": err})
        elif p == "/learn/summarize":
            ok, err = learn_summarize()
            self._json({"ok": ok, "error": err})
        elif p == "/fight/send":
            b = self._body()
            txt = str(b.get("text") or "").strip()
            # 分清这条是「照机器人给的原样发」还是「改过再发」——后者是总结人设时
            # 最值钱的样本（原稿 vs 主人改成什么样），2026-10-05。
            with _LOCK:
                _cand = str(_FIGHT.get("cand") or "").strip()
                _who = str(_FIGHT.get("who") or "").strip()
                _msg = str(_FIGHT.get("msg") or "").strip()
            if not _cand:
                _origin = "manual"
            elif txt == _cand:
                _origin = "approve"
            else:
                _origin = "edited"
            ok, err = fight_send(txt, origin=_origin, draft=_cand, src=_msg, who=_who,
                                 force=bool(b.get("force")))
            with _LOCK:
                if ok:
                    _FIGHT["sent"].append({"text": txt, "at": time.time()})
                    _FIGHT["cand"] = ""
                    _FIGHT["msg"] = ""      # 发完收起编辑框，避免误连发
                else:
                    _FIGHT["error"] = err
            self._json({"ok": ok, "error": err})
        elif p in ("/start", "/dry"):
            start(p == "/dry")
            self.send_response(303)
            self.send_header("Location", "/?spin=1")
            self.end_headers()
        elif p == "/stop":
            stop()
            time.sleep(1.0)
            self.send_response(303)
            self.send_header("Location", "/?spin=1")
            self.end_headers()
        elif p == "/reclaim":
            reclaim_wechat()
            time.sleep(1.0)
            self.send_response(303)
            self.send_header("Location", "/?spin=1")
            self.end_headers()
        elif p == "/repair":
            repair()
            time.sleep(1.0)
            self.send_response(303)
            self.send_header("Location", "/?spin=1")
            self.end_headers()
        elif p == "/adb":
            reconnect_adb()
            time.sleep(1.0)
            self.send_response(303)
            self.send_header("Location", "/?spin=1")
            self.end_headers()
        elif p == "/hud/start":
            # 2026-10-09：面板上的「拉起浮窗」按钮（用户划掉了通知就想点它叫回来）
            start_hud()
            time.sleep(1.2)
            self.send_response(303)
            self.send_header("Location", "/?spin=1")
            self.end_headers()
        elif p == "/hud/stop":
            # 2026-10-09：面板上的「关掉浮窗」按钮
            stop_hud()
            time.sleep(1.0)
            self.send_response(303)
            self.send_header("Location", "/?spin=1")
            self.end_headers()
        elif p == "/watch":
            _WATCH["on"] = not _WATCH["on"]
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
        # ---- 读屏节奏：写 tuning.json，机器人下一轮自己套用（不用重启）----
        elif p == "/tune":
            b = self._body()
            try:
                self._json({"ok": True, "msg": tune_save(b)})
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)})
        elif p == "/tune/reset":
            try:
                done = tune_reset()
                self._json({"ok": True,
                            "msg": "已恢复 config.yaml 的默认节奏" if done
                                   else "本来就没设过（一直是 config.yaml 的值）"})
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)})
        # ---- 特别关注名单：写 data/follow.json，机器人下一轮热加载（不用重启）----
        elif p == "/follow":
            b = self._body()
            try:
                self._json({"ok": True, "msg": follow_save(b)})
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)})
        else:
            self._html(page(), 404)


def main():
    # ★ 2026-10-08 审查修复（S3）：默认只绑回环。
    #   原来绑 0.0.0.0 且完全没有鉴权 —— 同一个 WiFi 下任何设备 POST /start
    #   就能让机器人用主人的微信身份往群里发消息（/stop、/fight/send、/tune 同理），
    #   而面板页面上还挂着"启动/停止/代发"按钮。手机和容器共享回环，所以
    #   127.0.0.1:8790 在手机上照常打开，功能一点不少。
    #   确实需要局域网访问时（例如从电脑上看）：BOTCTL_BIND=0.0.0.0 bash botctl_ctl.sh restart
    bind = os.environ.get("BOTCTL_BIND", "127.0.0.1") or "127.0.0.1"
    print("[botctl] 控制面板已启动（绑定 %s，端口 %d）→ http://127.0.0.1:%d"
          % (bind, PORT, PORT), flush=True)
    if bind not in ("127.0.0.1", "localhost", "::1"):
        print("[botctl] ⚠️ 绑定的是 %s：面板**没有登录鉴权**，同网段的人都能"
              "启停机器人、代发消息；仅在你清楚风险时这样用。" % bind, flush=True)
    threading.Thread(target=_watch_loop, daemon=True, name="watchdog").start()
    print("[botctl] 自动看护：开（每 45 秒体检一次，连续两轮不正常才动手）", flush=True)
    ThreadingHTTPServer((bind, PORT), H).serve_forever()


if __name__ == "__main__":
    main()
