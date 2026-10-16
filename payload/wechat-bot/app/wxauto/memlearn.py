# -*- coding: utf-8 -*-
"""记忆库的自动整理（2026-10-11 用户拍板：「每天自动学一次」）。

读最近 N 小时的群聊流水 → 让模型吐出 ≤5 条「人 / 梗」增量 → 去重后写进记忆库。

为什么是**每天一次**、而不是每轮：
  · 记忆是慢变量（谁是谁、什么梗），一天积累一次足够；
  · 每轮都学 = 每条消息多一次模型调用，成本直接翻倍；
  · 凌晨是错峰半价时段（见浮窗的波峰/波谷），跑在这里最便宜。

「今天跑过没有」记在 `记忆_auto.json`：**成功失败都记** —— 接口抽风时宁可今天不学，
也不能每轮重试把钱烧掉。

调用方：`bot.py::_maybe_memory_learn()`（主循环里每轮问一次，它自己判日期）。
手动跑：`python3 -c "from wxauto import memlearn; ..."` 或看 `--dry-run`。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .atomic import atomic_write_json
from .memory import Memory, KINDS, _norm

STATE_FILE = "记忆_auto.json"
# ★ 2026-10-15 用户要求「记忆库判据松一点」。原来的三道判据都偏紧，实测代价：
#   · MIN_LINES=10  → 夜里流水 6~9 条的日子直接不学；
#   · MAX_LEN_NAME=12 → 长昵称（「某某（emo版）」「张三（今晚打本）」）
#     连**候选说话人**都进不去 senders，模型学出来的人一律被机械校验毙掉；
#   · MAX_LEN_NOTE=60 → 模型多写半句就被整条丢。
#   下面放宽，但**不放掉机械校验**（那是防"把梗当人"的关键，见 learn()）。
MIN_LINES = 5           # 流水太少就别浪费一次调用（5 条起）
MAX_LEN_NAME = 16       # 单条记忆的名字上限（放宽后长昵称也能进）
MAX_LEN_NOTE = 80       # note 上限（prompt 仍要求 40 字以内；这只是解析兜底）
MAX_SENDER_LEN = 24     # senders（"真说过话的昵称"白名单）的长度上限


def _load_state(path) -> dict:
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def recent_lines(chat_path, hours: float, max_lines: int = 400,
                 max_chars: int = 6000):
    """最近 N 小时的群聊流水 → ``(["昵称：内容", ...], {昵称, ...})``。

    第二个返回值是**真正说过话的昵称集合** —— 用来机械校验模型学出来的
    "人"是不是真的人（见 ``learn()`` 里的说明）。跳过机器人自己发的。
    """
    try:
        data = json.loads(Path(chat_path).read_text(encoding="utf-8"))
    except Exception:
        return [], set()
    msgs = data if isinstance(data, list) else []
    if not msgs and isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                msgs = v
                break
    since = time.time() - hours * 3600
    out, senders, chars = [], set(), 0
    for m in msgs:
        if not isinstance(m, dict) or m.get("self"):
            continue
        try:
            ts = float(m.get("ts") or 0)
        except Exception:
            ts = 0.0
        if ts < since:
            continue
        who = str(m.get("sender") or "").strip() or "?"
        txt = " ".join(str(m.get("text") or "").split())
        if not txt:
            continue
        # ★ 2026-10-15 放宽：原来要求昵称 2~12 字才算"候选人"，
        #   长昵称（带括号后缀那种）整批进不了白名单 → 模型学出来的人全被丢掉。
        #   现在只排除**明显是 OCR 整行误判**的（含冒号的 "六少：怎么了" 这种）。
        if (who != "?" and len(who) <= MAX_SENDER_LEN
                and "：" not in who and ":" not in who):
            senders.add(who)
        line = "%s：%s" % (who, txt[:80])
        if len(out) >= max_lines or chars + len(line) > max_chars:
            break
        out.append(line)
        chars += len(line) + 1
    return out, senders


def _build_prompt(lines, known_names, max_items: int) -> str:
    return (
        "下面是这个微信群最近一段时间的聊天记录（格式「昵称：内容」）。\n"
        "请从中挑出**值得长期记住**的信息，输出一个 JSON 数组（最多 %d 条；"
        "实在没有就输出 []）。\n\n"
        "只记这两类：\n"
        '  1) {"kind":"person","name":"昵称","note":"这人是谁 / 常聊什么 / 口头禅"} —— '
        "记**群里的人**；\n"
        '  2) {"kind":"topic","name":"梗或事件的名字","note":"是什么、谁跟谁、一两句讲清"} —— '
        "记**群里的梗 / 事件**。\n\n"
        "硬要求：\n"
        "  · name 必须是上面流水里**原样出现过**的昵称或词，**不许自己编**；\n"
        "  · note 一句话、不超过 40 字、不带换行和引号；\n"
        "  · 只记**稳定的**东西（人、长期在用的说法、群里的固定梗）；"
        "临时的事（今天谁吃饭、几点打本、谁刚上线、谁生病了）**不要记** —— "
        "note 里**不许出现**「最近 / 今天 / 刚刚」这类时间词；\n"
        "  · `person` 的 name **必须**是流水里某一行的**说话人昵称**（冒号左边那个），"
        "**不许**把一句话里提到的词当成人名；\n"
        "  · **不许记**隐私：真名、电话、住址、身份证、单位、账号、财务，也不要露骨内容；\n"
        "  · 下面「已有记忆」里已经有的人名/词，**不要重复输出**。\n\n"
        "已有记忆（只有名字）：%s\n\n"
        "----- 聊天记录开始 -----\n%s\n----- 聊天记录结束 -----"
        % (max(1, int(max_items)),
           "、".join(sorted(known_names)) or "（空）",
           "\n".join(lines))
    )


def _parse_items(raw: str, max_items: int):
    """从模型输出里抠出条目（容忍 ```json 包裹、前后废话、单引号）。"""
    if not raw:
        return []
    t = raw.strip()
    m = re.search(r"```(?:json)?\s*(.+?)\s*```", t, re.S)
    if m:
        t = m.group(1)
    if "[" in t and "]" in t:
        t = t[t.find("["):t.rfind("]") + 1]
    try:
        data = json.loads(t)
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    out = []
    for it in data:
        if not isinstance(it, dict):
            continue
        kind = str(it.get("kind") or "").strip().lower()
        if kind not in KINDS:
            # ★ 2026-10-15 放宽：模型写 people / 人物 / 群友 / name 之类也要认成
            #   person（原来只认 "people"/"人"，其余一律当 topic —— 人就被记成梗）。
            kind = ("person"
                    if any(w in kind for w in ("person", "people", "人", "name",
                                               "群友", "成员"))
                    else "topic")
        name = " ".join(str(it.get("name") or "").split()).strip("。，,、：:")
        note = " ".join(str(it.get("note") or "").split()).strip()
        if not (2 <= len(name) <= MAX_LEN_NAME):
            continue
        if not (2 <= len(note) <= MAX_LEN_NOTE):
            continue
        if not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", name):
            continue
        out.append({"kind": kind, "name": name, "note": note})
        if len(out) >= max(1, int(max_items)):
            break
    return out


def learn(brain, base_dir, log=None, hours: float = 24, max_items: int = 5,
          force: bool = False, dry_run: bool = False) -> dict:
    """跑一次自动整理。返回一个报告 dict（**不抛异常**）。

    ``brain`` 是 ``wxauto.brain.Brain``（只用它的 ``_post`` 和 ``available``）。
    ``base_dir`` 是项目根目录（记忆库 / 流水 / 状态文件都在那儿）。
    """
    base = Path(base_dir)
    mem_path = base / "记忆库.json"
    chat_path = base / "chat_log.json"
    state_path = base / STATE_FILE
    rep = {"day": time.strftime("%Y-%m-%d"), "ran": False, "learned": 0,
           "reason": "", "items": []}

    def _say(level, msg):
        try:
            if log is not None:
                getattr(log, level, log.info)(msg)
        except Exception:
            pass

    try:
        if not force and not dry_run:
            st = _load_state(state_path)
            if str(st.get("last_date") or "") == rep["day"]:
                rep["reason"] = "今天已经整理过"
                return rep
        if not getattr(brain, "available", False):
            rep["reason"] = "模型不可用（没配 API Key？）"
            return rep
        lines, senders = recent_lines(chat_path, hours)
        if len(lines) < MIN_LINES:
            rep["reason"] = "最近流水太少（%d 条），不值得花一次调用" % len(lines)
            _mark_day(state_path, rep["day"], 0, rep["reason"])
            return rep
        mem = Memory(mem_path).load()
        known = set()
        for it in mem.items():
            known.add(it["name"])
            known.update(it["aliases"])
        prompt = _build_prompt(lines, known, max_items)
        if dry_run:
            rep["reason"] = "dry-run：没调模型"
            rep["prompt_chars"] = len(prompt)
            rep["lines"] = len(lines)
            return rep
        payload = {
            "model": getattr(brain.cfg, "model", "deepseek-chat"),
            "messages": [
                {"role": "system",
                 "content": "你是信息抽取工具，只输出要求的 JSON，不解释、不寒暄。"},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 500, "temperature": 0.2, "stream": False,
            # ★ 2026-10-15（修 bug 的关键一行）：抽取类请求**不要**让 _post 自动补
            #   reasoning_effort —— 实测传 high 时 reasoning 1392 字吃光 500 token、
            #   JSON 被截断，记忆库连着两天学 0 条。详见 brain._post 的 `_no_effort`。
            "_no_effort": True,
        }
        body = brain._post(payload)
        raw = ""
        try:
            raw = body["choices"][0]["message"]["content"] or ""
        except Exception:
            raw = ""
        items = _parse_items(raw, max_items)
        # ★ 2026-10-15（用户问「记忆库正常工作吗」→ 发现连续两天 learned=0）：
        #   把模型的**原始返回**留在报告里。之前只写一句「这次没有值得记的新东西」，
        #   分不清三种情况：① 模型直接回了空数组；② 回了内容但 JSON 抠不出来；
        #   ③ 解析出来了却被后面的机械校验/去重丢掉。实测复现：ran=true、
        #   learned=0、**连 dropped 都没有** —— 连我这轮都只能靠猜。
        rep["raw"] = (raw or "").strip()[:400]
        # ★ 2026-10-11 首次实测的教训：模型会把**梗**当成"人"（第一次跑就把
        #   「穿镜子」「奶奶的玲珑吸血鬼」写进了 person）。这里加一道**机械校验**
        #   （不花额外 token）：
        #     · person 的 name 必须是流水里真正说过的**昵称**（或其包含关系）；
        #     · topic 的 name 必须在流水正文里出现过。
        # ★ 2026-10-15 用户要求「判据松一点」—— 校验**留着**（它是防"梗当人"的
        #   关键），但比较改为**归一化**（去空白 + 小写，见 memory._norm），并给
        #   person 补第二条通路（名字在流水里以「名字：」出现过）。原来的坑：
        #   「某某 某某」这种带空格昵称、以及带括号的长昵称，模型怎么写都对不上，
        #   整条被静默丢掉。
        text_all = "\n".join(lines)
        text_norm = _norm(text_all)          # 抗 OCR / 昵称里的空格抖动
        send_norm = [_norm(s) for s in senders if _norm(s)]
        kept = []
        for it in items:
            nm = it["name"]
            k = _norm(nm)
            if it["kind"] == "person":
                if (not any(k and (k == sn or k in sn or sn in k) for sn in send_norm)
                        and (k + "：") not in text_norm
                        and (k + ":") not in text_norm):
                    continue
            elif k not in text_norm:
                continue
            kept.append(it)
        dropped = len(items) - len(kept)
        if dropped:
            rep["dropped"] = dropped
            rep["dropped_names"] = [it["name"] for it in items if it not in kept][:5]
        items = kept
        rep["ran"] = True
        added = 0
        for it in items:
            r = mem.add(it["kind"], it["name"], it["note"])
            if r:
                added += 1
                rep["items"].append("%s（%s）" % (it["name"], it["note"]))
        rep["learned"] = added
        _mark_day(state_path, rep["day"], added, "")
        if added:
            _say("info", "记忆库自动整理：学了 %d 条 → %s" % (added, "；".join(rep["items"])))
        else:
            # ★ 2026-10-15：日志带上足够排查的细节（原来只有一句"没有值得记的新东西"）
            _say("info", "记忆库自动整理：没有学到的（模型回了 %d 字、解析出 %d 条、"
                         "机械校验丢掉 %d 条%s）%s"
                 % (len(raw or ""), len(items), int(rep.get("dropped") or 0),
                    ("：" + "、".join(rep.get("dropped_names") or []))
                    if rep.get("dropped_names") else "",
                    ("，模型原文：" + (raw or "").strip().replace("\n", " ")[:160])
                    if raw else ""))
        return rep
    except Exception as exc:                      # 绝不连累主循环
        rep["reason"] = "出错：%r" % (exc,)
        try:
            _mark_day(state_path, rep["day"], rep.get("learned", 0), rep["reason"])
        except Exception:
            pass
        _say("warning", "记忆库自动整理出错（不影响别的）：%r" % (exc,))
        return rep


def _mark_day(state_path, day: str, learned: int, reason: str = "") -> None:
    """记下"今天整理过了"（**成功失败都记**，避免接口抽风时反复重试烧钱）。"""
    try:
        st = _load_state(state_path)
        st["last_date"] = day
        st["last_ts"] = int(time.time())
        st["last_learned"] = int(learned)
        if reason:
            st["last_reason"] = str(reason)[:120]
        atomic_write_json(state_path, st, indent=1)
    except Exception:
        pass
