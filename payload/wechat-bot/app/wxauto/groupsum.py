# -*- coding: utf-8 -*-
"""群情摘要 —— 把超出「10 分钟上下文窗」的群聊背景压成一小段，注入 user prompt。

★ 2026-10-12 用户拿了几个 B 站开源项目让借鉴（AstrBot / LangBot / 鲸鱼娘）。
  其中 AstrBot 明确有一项 **Auto Context Compression**（自动上下文压缩）。
  我们这边的现状：`brain._context_lines` 只看**最近 16 条 / 10 分钟窗** ——
  昨天那场瓜的结论、群里正在延续的事、谁跟谁刚吵过，它一概不知道。

做法（关键：**不递归摘要**）：
  · 每 `interval_secs` 检查一次，把**这一段新增的原始流水**交给模型，
    压成 ≤ `max_chars` 字的「这段时间群里发生了什么」；
  · 结果作为**一段**追加进 `群情摘要.json`（最多留 `keep_segments` 段）；
  · 注入时只取最近 `inject_segments` 段，总字数另有上限。

  ⚠️ 为什么不是「上一次摘要 + 新消息 → 新摘要」：那会**滚雪球** —— 错一次就
     永远错下去，而且越滚越抽象（最后只剩「大家在聊天」）。每段都只由**原始
     流水**生成，就不存在累积失真；旧段到期直接丢，**不做二次压缩**。

⚠️ 流水里有明显 OCR 错字（实测 `队最约20新活常在0:25` 这种），prompt 里必须
  写明「读不通就当没看见，别猜」—— 否则错字会被写进背景，**比没有摘要更糟**：
  那是个会被一直复用的"事实"。

调用方：`bot.py::_maybe_group_summary()`（主循环每轮问一次，它自己判时间）。
手动跑：`python3 -m wxauto.groupsum --dry-run` / `--once`
"""

from __future__ import annotations

from .paths import APP_DIR, DATA_DIR
import json
import time
from pathlib import Path

from .atomic import atomic_write_json

FILE = "群情摘要.json"
CHAT_FILE = "chat_log.json"
VERSION = 1

DEFAULTS = {
    "interval_secs": 3600.0,     # 多久攒一段（默认 1 小时）
    "min_msgs": 20,              # 这一段少于这么多条就不值得花一次调用
    "max_chars": 240,            # 每段摘要的字数上限
    "keep_segments": 12,         # 文件里最多留几段（12 段 ≈ 半天）
    "inject_segments": 2,        # 每轮注入最近几段
    "inject_chars": 400,         # 注入总字数上限
    "max_lines": 400,            # 单段最多喂多少行给模型
    "max_input_chars": 6000,     # 单段输入字符上限
}


def load(path) -> dict:
    """读摘要文件；坏了/没有都返回空骨架（绝不让调用方炸）。"""
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("segments"), list):
            return d
    except Exception:
        pass
    return {"version": VERSION, "last_to": 0.0, "segments": []}


def _slice(msgs, since: float, until: float, max_lines: int,
           max_chars: int):
    """取 (since, until] 窗口内的流水 → ``(["昵称：内容", ...], 原始条数)``。

    ★ 跟 `memlearn.recent_lines` 的两点不同：
      · **包含机器人和主人自己发的**（写成「我」）—— 摘要是给"接话"用的，
        群里别人回的是不是我们说的那句，丢了这个就接不上；
      · 按**时间窗**切，不是按"最近 N 小时"。
    """
    out, total, chars = [], 0, 0
    for m in (msgs or []):
        if not isinstance(m, dict):
            continue
        try:
            ts = float(m.get("ts") or 0)
        except Exception:
            ts = 0.0
        if ts <= since or ts > until:
            continue
        txt = " ".join(str(m.get("text") or "").split())
        if not txt:
            continue
        total += 1
        who = "我" if m.get("self") else (str(m.get("sender") or "").strip() or "群友")
        line = "%s：%s" % (who, txt[:80])
        if len(out) >= max_lines or chars + len(line) > max_chars:
            continue          # 只跳过后面的，仍然数条数（total 是真实消息量）
        out.append(line)
        chars += len(line) + 1
    return out, total


def _clean(text: str, max_chars: int) -> str:
    """把模型输出洗成一段话（去换行/引号/markdown），超长截断。"""
    t = str(text or "").strip()
    t = t.replace("```", " ").replace("\r", " ").replace("\n", " ")
    t = t.replace("**", "").replace("「", "").replace("」", "")
    t = " ".join(t.split())
    if len(t) > max_chars:
        t = t[:max_chars].rstrip("，。、,;；") + "…"
    return t


def _bad_reply(t: str) -> str:
    """机械校验（不花额外 token）：空 / 太短 / 答非所问的元话语。"""
    if not t or len(t) < 8:
        return "太短"
    for w in ("作为AI", "作为 AI", "我无法", "无法完成", "抱歉", "我是一个"):
        if w in t:
            return "元话语"
    return ""


def _build_prompt(lines, max_chars: int) -> str:
    return (
        "下面是一个微信群里刚过去一段时间的聊天记录（格式「昵称：内容」）。\n"
        "请用**一小段中文**概括「这段时间群里发生了什么」，给一个不在场的人看，"
        "让他能接上话。\n\n"
        "写什么：\n"
        "  · 在聊什么事、聊到什么结论（有结论才写结论，没有就写还在聊什么）；\n"
        "  · 谁和谁在互动、谁提了什么（点名到昵称）；\n"
        "  · 正在进行中的事（约了什么、在等谁、谁还没回）。\n\n"
        "硬要求：\n"
        "  · 只写**流水里真有的**，不确定就少写 —— 绝对不许脑补；\n"
        "  · ⚠️ 流水是**截图 OCR** 来的，有错字、有断句（比如"
        "「队最约20新活常在0:25」）。读不通的那句就**当没看见**，别猜它什么意思、"
        "别把错字当原话；\n"
        "  · 不写隐私（真名 / 电话 / 住址 / 身份证 / 单位 / 账号 / 钱），"
        "不写露骨内容；\n"
        "  · 纯文本一段话，不超过 %d 字，不要换行、不要引号、不要 markdown，"
        "不要以「这段时间」开头；\n"
        "  · 只输出这段概括本身，不要解释你在做什么。\n\n"
        "----- 聊天记录开始 -----\n%s\n----- 聊天记录结束 -----"
        % (int(max_chars), "\n".join(lines))
    )


def summarize(brain, lines, max_chars: int, log=None) -> str:
    """调一次模型，把这段流水压成一段话。失败返回空串（**不抛异常**）。"""
    try:
        if not getattr(brain, "available", False):
            return ""
        payload = {
            "model": getattr(brain.cfg, "model", "deepseek-chat"),
            "messages": [
                {"role": "system",
                 "content": "你是群聊摘要工具：只输出一段概括，不解释、不寒暄、"
                            "不编造流水里没有的事。"},
                {"role": "user", "content": _build_prompt(lines, max_chars)},
            ],
            "max_tokens": 400, "temperature": 0.2, "stream": False,
        }
        body = brain._post(payload)
        raw = ""
        try:
            raw = body["choices"][0]["message"]["content"] or ""
        except Exception:
            raw = ""
        t = _clean(raw, max_chars)
        bad = _bad_reply(t)
        if bad:
            try:
                if log is not None:
                    log.warning("群情摘要被丢弃（%s）：%r" % (bad, t[:60]))
            except Exception:
                pass
            return ""
        return t
    except Exception as exc:
        try:
            if log is not None:
                log.warning("群情摘要生成失败：%r" % (exc,))
        except Exception:
            pass
        return ""


def maybe_run(brain, base_dir, log=None, cfg=None, force: bool = False,
              dry_run: bool = False, now: float = None) -> dict:
    """到点就攒一段。返回报告 dict（**不抛异常**）。

    ``cfg`` 是 config.yaml 的 ``summary`` 段（NS 对象或 dict 都吃）。
    ``now`` 只是为了测试能注入时间。
    """
    base = Path(base_dir)
    path = base / FILE
    rep = {"ran": False, "reason": "", "msgs": 0, "text": "", "segments": 0}

    def _cfg(key):
        want = DEFAULTS[key]
        if cfg is None:
            return want
        try:
            v = cfg.get(key, want) if isinstance(cfg, dict) else getattr(cfg, key, want)
        except Exception:
            return want
        try:
            v = float(v) if isinstance(want, float) else int(v)
        except Exception:
            return want
        return v

    def _say(level, msg):
        try:
            if log is not None:
                getattr(log, level, log.info)(msg)
        except Exception:
            pass

    try:
        t_now = float(now if now is not None else time.time())
        interval = max(60.0, _cfg("interval_secs"))
        st = load(path)
        last_to = float(st.get("last_to") or 0.0)
        if force and not last_to:
            last_to = t_now - interval
        if last_to <= 0:
            # 第一次跑：只看最近一个 interval，别把一整天都吞进来
            last_to = t_now - interval
        if not force and t_now - last_to < interval:
            rep["reason"] = "还没到点（%.0f 秒后再看）" % (interval - (t_now - last_to))
            return rep

        try:
            msgs = json.loads((base / CHAT_FILE).read_text(encoding="utf-8"))
        except Exception:
            msgs = []
        if isinstance(msgs, dict):
            msgs = msgs.get("messages") or []
        lines, total = _slice(msgs, last_to, t_now,
                              int(_cfg("max_lines")), int(_cfg("max_input_chars")))
        rep["msgs"] = total
        min_msgs = int(_cfg("min_msgs"))
        if total < min_msgs:
            # 流水太少：**不花调用**，但把时间戳往前推，免得下次重扫同一段
            st["last_to"] = t_now
            st["version"] = VERSION
            if not dry_run:
                atomic_write_json(path, st)
            rep["reason"] = "这段流水太少（%d < %d）" % (total, min_msgs)
            return rep

        if dry_run:
            rep["reason"] = "dry-run：没调模型"
            rep["lines"] = len(lines)
            rep["prompt_chars"] = len(_build_prompt(lines, int(_cfg("max_chars"))))
            return rep

        text = summarize(brain, lines, int(_cfg("max_chars")), log=log)
        if not text:
            # 失败**不推进 last_to** —— 下一轮（interval 之后）重试这一段
            rep["reason"] = "模型没给出可用的摘要"
            return rep

        segs = list(st.get("segments") or [])
        segs.append({"from": round(last_to, 3), "to": round(t_now, 3),
                     "msgs": total, "text": text})
        keep = max(1, int(_cfg("keep_segments")))
        st["segments"] = segs[-keep:]
        st["last_to"] = t_now
        st["version"] = VERSION
        atomic_write_json(path, st)
        rep.update({"ran": True, "reason": "ok", "text": text,
                    "segments": len(st["segments"])})
        _say("info", "群情摘要已更新（%d 条消息 → %d 字，共 %d 段）：%s"
             % (total, len(text), len(st["segments"]), text))
        return rep
    except Exception as exc:                      # 绝不影响主流程
        rep["reason"] = "异常：%r" % (exc,)
        _say("warning", "群情摘要跳过：%r" % (exc,))
        return rep


def block(path, inject_segments: int = 2, inject_chars: int = 400) -> str:
    """注入用：最近几段拼成一小段（没有就返回空串，零成本）。"""
    try:
        st = load(path)
        segs = [s for s in (st.get("segments") or []) if isinstance(s, dict)]
        segs = segs[-max(1, int(inject_segments)):]
        text = " / ".join(str(s.get("text") or "").strip() for s in segs)
        text = " ".join(text.split())
        if not text:
            return ""
        if len(text) > inject_chars:
            text = text[-int(inject_chars):]      # 留最近的部分
        return ("【这段时间群里的大致情况（**背景**，不是现在正在说的话）——"
                "只用来理解上下文：别把它当新消息回、别复述、别拿它当话题】\n"
                + text + "\n\n")
    except Exception:
        return ""


def main(argv=None) -> int:
    import argparse
    import sys

    sys.path.insert(0, str(APP_DIR))
    ap = argparse.ArgumentParser(description="群情摘要（手动跑一次 / 看结果）")
    ap.add_argument("--dry-run", action="store_true", help="只算 prompt，不调模型")
    ap.add_argument("--force", action="store_true", help="忽略时间间隔，立刻跑一段")
    ap.add_argument("--show", action="store_true", help="只打印现有摘要")
    args = ap.parse_args(argv)

    base = DATA_DIR
    if args.show:
        st = load(base / FILE)
        print(json.dumps(st, ensure_ascii=False, indent=1)[:4000])
        return 0
    from .config import load_config, load_persona, load_personas
    from .brain import Brain
    cfg = load_config(str(base / "config.yaml"))
    try:                       # API Key 跟 main_android 一样从 .deepseek_key 读
        from main_android import apply_key_file
        apply_key_file(cfg)
    except Exception:
        pass
    brain = Brain(cfg, load_persona(), None, personas=load_personas())
    rep = maybe_run(brain, base, None, getattr(cfg, "summary", None),
                    force=args.force, dry_run=args.dry_run)
    print(json.dumps(rep, ensure_ascii=False, indent=1)[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
