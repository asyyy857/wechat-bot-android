#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""演练：让机器人对群里的消息"说一遍"，但**一个字都不发**。

为什么要单独一个脚本（2026-10-04 用户需求）：
用户想先看看机器人**会怎么说话**，再决定人设/规则怎么改。
它只做两件事：读桥接的 /snapshot（和机器人看到的一样），
然后走一遍**真实的** Matcher + Brain（同一份 rules.yaml / personas / config）。
**不碰发送通道**，所以群里不会有任何动静，机器人也可以照常在跑。

用法::

    cd /sdcard/dsh/wechat-bot/app
    python3 simulate.py                        # 人看的文本
    python3 simulate.py --json --count 8       # 给控制面板用（结构化，日志走 stderr）
    python3 simulate.py --count 12
    python3 simulate.py --rules-only           # 只跑关键词规则，0 Token
    python3 simulate.py --text "有点像阿兰"     # 指定一句，不看群
    python3 simulate.py --persona 猫娘          # 强制用某个人设

JSON 里每条会带 ``will_reply``：**真跑时机器人到底会不会理它**
（关键词命中或被 @ 才会，其余默认闭嘴 —— 这是用户 2026-10-04 明确的期望）。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
import urllib.request

# ⚠️ 2026-10-13：原来这里写死 v1 目录 `/sdcard/dsh/wechat-bot/app` 并插到 sys.path
#   **最前面** —— 而那个目录里**也有 wxauto/**，于是面板生成回复时 import 的是
#   **v1 的旧代码 / 旧 config / 旧人设**，跟正在跑的机器人不是一套（v2.0.0 之后
#   程序在 app/、数据在 data/）。现在一律以本文件所在的 app/ 为准。
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

# 容器是 UTC，比手机（北京时间）慢 8 小时 —— 人设时段表靠本地时间判断，
# 不锁时区就会用错人设（跟 main_android.py 同一处理）。
os.environ.setdefault("TZ", "Asia/Shanghai")
try:
    time.tzset()
except Exception:      # pragma: no cover
    pass

try:                      # 凭据在数据目录（v2.0.0 起 app/ 与 data/ 分离）
    from wxauto.paths import DATA_DIR as _DATA_DIR
except Exception:         # pragma: no cover
    _DATA_DIR = ROOT
KEY_FILE = os.path.join(str(_DATA_DIR), ".deepseek_key")

from wxauto.brain import Brain                       # noqa: E402
from wxauto.config import (load_config, load_persona,  # noqa: E402
                           load_personas, load_rules)
from wxauto.matcher import Matcher, Rule             # noqa: E402
from wxauto.reader import Block, Message, Snapshot   # noqa: E402
from wxauto.safety import with_meow                  # noqa: E402

BRIDGE = "http://127.0.0.1:8765"


def fetch_snapshot(timeout=30):
    with urllib.request.urlopen(BRIDGE + "/snapshot", timeout=timeout) as r:
        return json.load(r)


def build_snapshot(d):
    """把桥接的 JSON 还原成 wxauto 的 Snapshot（Brain 的 prompt 需要它）。"""
    msgs = []
    for i, m in enumerate(d.get("messages") or []):
        msgs.append(Message(
            blocks=[Block(text=str(m.get("text") or ""), x=0, y=i * 20,
                          w=int(m.get("width") or 0) or 200, h=12)],
            is_self=bool(m.get("is_self")),
            sender=str(m.get("sender") or "")))
    return Snapshot(chat_title=str(d.get("chat_title_raw") or ""), messages=msgs,
                    width=int(d.get("width") or 636),
                    height=int(d.get("height") or 1400))


def window_hit(win, cur):
    try:
        a, b = win.split("-")

        def t(s):
            hh, mm = s.strip().split(":")
            return int(hh) * 60 + int(mm)
        s, e = t(a), t(b)
    except Exception:
        return False
    return s <= cur < e if s <= e else (cur >= s or cur < e)


def _persona_meows(cfg, brain):
    """当前人设说话带不带「喵」尾巴 —— 与 ``bot.py::_persona_meows`` 同一口径。

    ``behavior.meow_personas`` 留空 = 一律加喵（老行为）；配了名单就按名字判
    （女仆那套不在名单里 → 不加）。两者改一处必须改另一处。
    """
    names = getattr(cfg.behavior, "meow_personas", None)
    if not names:
        return True
    who = str(getattr(brain, "persona_name", "") or "")
    return any(str(n) in who for n in names)


def force_persona(cfg, brain, want):
    """定下这次演练用哪套人设，返回 (名字, 理由)。"""
    if want:
        brain.persona_mode = "fixed"
        brain.persona_fixed = want
        name, _ = brain.pick_persona()
        return name, "命令行指定"
    sched = getattr(cfg.behavior, "persona_schedule", None) or []
    lt = time.localtime()
    cur = lt.tm_hour * 60 + lt.tm_min
    for item in sched:
        if not isinstance(item, dict):
            continue
        w = str(item.get("window") or "").strip()
        n = str(item.get("name") or "").strip()
        if w and n and window_hit(w, cur):
            brain.persona_mode = "fixed"
            brain.persona_fixed = n
            return n, "按时段 %s" % w
    name, _ = brain.pick_persona()
    return name, "按 persona_mode=%s" % (getattr(brain, "persona_mode", "?"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=6, help="看最近几条（默认 6）")
    ap.add_argument("--text", default="", help="指定一句话，不读群")
    ap.add_argument("--persona", default="", help="强制人设（如 猫娘 / 贴吧老哥）")
    ap.add_argument("--rules-only", action="store_true",
                    help="只跑关键词规则，不调模型（0 Token）")
    ap.add_argument("--mention", action="store_true",
                    help="按「被 @ 点名问到」这条路生成（用 mention_max_* 那两档，"
                         "否则测不出放宽后的效果 —— 平时那条路是短句档）")
    ap.add_argument("--proactive", action="store_true",
                    help="按「自己凑过去接一句」这条路生成（吃 proactive_max_* 两档："
                         "要钩子、禁空话/复读）")
    ap.add_argument("--quote", action="store_true",
                    help="回答里**带上问题**（面板提问用：群里的人看不到你问的那句，"
                         "所以要让它先把话题点出来，别只扔一个没头没脑的答案）")
    ap.add_argument("--own", action="store_true",
                    help="按「主人自己在群里喊它名字」这条路生成 —— "
                         "跟 bot.handle_own 用同一组参数（own_ask 提示词 + @ 档长度）")
    ap.add_argument("--json", action="store_true", help="结构化输出（面板用）")
    args = ap.parse_args()

    # 一律写 stderr：JSON 模式下 stdout 只能有那一份 JSON
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="      · %(message)s")
    log = logging.getLogger("simulate")

    def warn(msg):
        if args.json:
            print(msg, file=sys.stderr)
        else:
            print(msg)

    cfg = load_config()
    # API Key 的第三个来源：同目录的 .deepseek_key（跟 main_android.py 一致）
    if not str(getattr(cfg.llm, "api_key", "") or ""):
        try:
            with open(KEY_FILE, encoding="utf-8") as fh:
                _k = fh.read().strip()
            if _k:
                cfg.llm.api_key = _k
        except Exception:
            pass
    rng = random.Random()
    matcher = Matcher([Rule.from_dict(d) for d in load_rules()], rng)
    brain = Brain(cfg, load_persona(), log, rng, personas=load_personas())

    snap = None
    if args.text:
        chat = "(不读群，用你给的句子)"
        snap = Snapshot(chat_title="演练", messages=[])
        items = [("你", args.text, Message(blocks=[Block(args.text, 0, 0, 200, 12)]))]
    else:
        try:
            snap = build_snapshot(fetch_snapshot())
        except Exception as exc:
            warn("读不到群里消息（桥接没通？）：%r" % (exc,))
            return 2
        chat = snap.chat_title
        picked = []
        for m in snap.messages:
            t = (m.text or "").strip()
            if m.is_self or not t:
                continue
            picked.append((m.sender or "?", t, m))
        items = picked[-max(1, args.count):]

    pname, pwhy = force_persona(cfg, brain, args.persona)
    n_img = sum(1 for r in matcher.rules if r.is_image)

    results = []
    for who, text, msg in items:
        rule = matcher.match(text)
        mention = bool(brain.is_mention(text))
        it = {"who": who, "text": text, "rule": "", "kind": "none",
              "reply": "", "will_reply": bool(rule is not None or mention),
              "mention": mention, "ms": 0.0}
        if rule is not None:
            it["rule"] = rule.name
            if rule.is_image:
                it["kind"] = "image"
                tag = rule.image_tag or "根目录"
                it["reply"] = "（会发一张表情包 · 标签：%s）" % tag
            elif rule.is_meitu:
                # 2026-10-08：今日美图。演练时**不下载**（省流量，下载也可能要几秒），
                # 只告诉你会发哪一类，真跑时由 bot._send_meitu 去取图。
                it["kind"] = "meitu"
                it["reply"] = "（会发一张今日美图 · 分类：%s）" % (
                    rule.meitu_source or "全部混池")
            else:
                it["kind"] = "rule"
                pick = rng.choice(rule.replies) if rule.replies else ""
                it["reply"] = with_meow(pick, no_meow=rule.no_meow)
        elif args.rules_only:
            it["kind"] = "skipped"
        elif not brain.available:
            it["kind"] = "nokey"
        else:
            t0 = time.time()
            if args.proactive:
                # ★ 2026-10-09：跟 bot.handle 里主动插话那条路用**同一组参数** ——
                #   否则测出来的还是"回一句"短评档，看不出接话档的效果。
                _mt = int(getattr(getattr(cfg, "llm", None),
                                  "proactive_max_tokens", 0) or 0)
                _mc = int(getattr(cfg.behavior, "proactive_max_chars", 0) or 0)
                r = brain.compose(snap, msg, "话题对味（演练：机器人自己凑过去接）",
                                  max_tokens=_mt, max_chars=_mc,
                                  proactive=bool(_mc))
            elif args.own:
                # ★ 2026-10-09（续12）：主人自己喊它名字那条路 —— 跟
                #   `bot.handle_own` 里的模型分支用**同一组参数**（own_ask 提示词告诉
                #   模型"这句是主人本人打的" + @ 档的 token/字数）。
                _mt = int(getattr(getattr(cfg, "llm", None),
                                  "mention_max_tokens", 0) or 0)
                _mc = int(getattr(cfg.behavior, "mention_max_chars", 0) or 0)
                r = brain.compose(snap, msg, "主人自己在群里叫它",
                                  max_tokens=_mt, max_chars=_mc,
                                  expand=bool(_mc), own_ask=True)
            elif args.mention or args.quote:
                # ★ 2026-10-09：模拟「被 @ 点名问到」—— 和 bot.handle 里那条路
                #   用**同一组参数**，否则测的还是短句档（看不出放宽的效果）。
                #   `--quote`（面板提问用）额外要求它**把问题当话题带出来**。
                _mt = int(getattr(getattr(cfg, "llm", None),
                                  "mention_max_tokens", 0) or 0)
                _mc = int(getattr(cfg.behavior, "mention_max_chars", 0) or 0)
                r = brain.compose(
                    snap, msg,
                    ("主人在面板上问你一句，他要直接发到群里" if args.quote
                     else "有人@你"),
                    max_tokens=_mt, max_chars=_mc, expand=bool(_mc),
                    quote_ask=bool(args.quote))
            else:
                r = brain.compose(snap, msg, "演练：如果这时候让它开口说话")
            it["ms"] = round(time.time() - t0, 2)
            it["kind"] = (("proactive" if args.proactive
                           else "own" if args.own
                           else "mention" if (args.mention or args.quote)
                           else "model") if r else "empty")
            # ★ 2026-10-10：原来无条件 with_meow(r) —— 换成不带喵的人设（女仆）后，
            #   演练会把真实发送层**不会**补的「喵」照样打出来，试听看到的全是假尾巴
            #   （实测切到女仆后演练仍显示「是呢 女仆哦喵」）。这里跟 bot.py::_persona_meows
            #   同一口径判一次：名单里没有当前人设 → 不补。
            it["reply"] = (with_meow(r, no_meow=not _persona_meows(cfg, brain))
                           if r else "")
            it["chars"] = len(it["reply"]) if r else 0
        results.append(it)

    payload = {"ok": True, "chat": chat, "persona": pname, "persona_why": pwhy,
               "at": time.time(), "model": getattr(cfg.llm, "model", "?"),
               "has_model": bool(brain.available), "rules": len(matcher.rules),
               "rules_image": n_img, "items": results}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    print("═" * 52)
    print("  演练（**不会真发**，群里不会有任何动静）")
    print("  群　：%s" % chat)
    print("  人设：%s（%s）" % (pname, pwhy))
    print("  模型：%s（%s）" % (payload["model"],
                              "可用" if brain.available else "不可用（缺 API Key）"))
    print("  规则：%d 条（其中图片规则 %d 条）" % (len(matcher.rules), n_img))
    print("═" * 52)
    if not results:
        print("没抓到别人的消息（群里太安静？）")
        return 0
    for i, it in enumerate(results, 1):
        print("─" * 52)
        print("[%d] %s：%s" % (i, it["who"], it["text"]))
        if it["kind"] == "rule":
            print("    ✅ 关键词规则「%s」命中 → 发：「%s」（0 Token）"
                  % (it["rule"], it["reply"]))
        elif it["kind"] == "image":
            print("    🖼 图片规则「%s」命中 → %s" % (it["rule"], it["reply"]))
        elif it["kind"] == "meitu":
            # ★ 2026-10-09 修：这条打印分支 2026-10-08 加 meitu 动作时漏了 ——
            #   kind="meitu" 掉进最后的 else，演练时显示成「⚠️ 模型没给出内容」，
            #   看着像"没命中规则"，其实是命中了的（真跑不受影响，真跑在 bot.py）。
            print("    🖼 图片规则「%s」命中 → %s" % (it["rule"], it["reply"]))
        elif it["kind"] == "proactive":
            print("    💬 模拟「自己凑过去接一句」（接话档，%d 字）→「%s」"
                  % (it.get("chars", len(it["reply"])), it["reply"]))
        elif it["kind"] == "mention":
            print("    🎯 模拟「被 @ 问到」（mention 档，%d 字）→「%s」"
                  % (it.get("chars", len(it["reply"])), it["reply"]))
        elif it["kind"] == "own":
            print("    🐾 模拟「你自己在群里喊它」（被叫档，%d 字）→「%s」"
                  % (it.get("chars", len(it["reply"])), it["reply"]))
        elif it["kind"] == "model":
            print("    🤖 真跑时**不会回**这条（没关键词、没 @）；若强行开口 →「%s」"
                  % it["reply"])
        elif it["kind"] == "skipped":
            print("    ❌ 没有关键词命中（--rules-only：不调模型）")
        elif it["kind"] == "nokey":
            print("    ❌ 没有关键词命中，模型也不可用（缺 API Key）")
        else:
            print("    ⚠️ 模型没给出内容")
    print("─" * 52)
    print("以上都只是打印，**没有发送**。要改哪句就直接说「第 N 条应该……」，")
    print("我会去改 personas/*.md（说话风格）或 rules.yaml（固定话术）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
