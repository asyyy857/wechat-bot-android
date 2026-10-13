#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""安全测发送耗时：只在「文件传输助手」里发，绝不碰真群。

背景：2026-10-08 下午，上一个会话为了查「发一条要 17.5~35.1 秒」花在哪，
直接在真群模式下调 /send —— 测试消息会真的发进目标群，
用户不得不撤回。本脚本把「只准在文件传输助手测」从"靠自觉"变成
"结构上发不出去"。

三道闸门：
  1. 安全会话名**硬编码**，不接受命令行覆盖（没有 --chat 这种参数）；
  2. 切过去后用 /where 读回**真实标题**，标题里没有「文件传输助手」→ 拒发；
  3. 无论成功、失败、异常，finally 里都把会话切回 config.yaml 配的真群。

用法：
    python3 measure_send.py "测试喵"              # 发 1 条并报耗时
    python3 measure_send.py "测试喵" --times 3    # 连发 3 条
    python3 measure_send.py --dry                 # 只切会话+校验，不发送
"""
import json
import os
import re
import sys
import time
import urllib.request

BRIDGE = os.environ.get("WXBRIDGE_URL", "http://127.0.0.1:8765")
SAFE_CHAT = "文件传输助手"          # ★ 硬编码：这是唯一允许发送测试的会话
DIR = os.path.dirname(os.path.abspath(__file__))
WB_LOG = "/tmp/wxbridge.log"


def post(path, payload, timeout=300):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(BRIDGE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
    return (json.loads(body) if body.strip() else {}), time.time() - t0


def get(path, timeout=120):
    t0 = time.time()
    with urllib.request.urlopen(BRIDGE + path, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
    return (json.loads(body) if body.strip() else {}), time.time() - t0


def looks_like(shown, target):
    """标题是否**确实是**目标会话（容忍微信的省略号截断，但不接受乱码）。

    这里不能用 wxreader.match_title —— 它有一条"标题里一个中文都没有也放行"
    的规则（给 /where 判"有没有离开群"用的），那条会让 OCR 读成 '...10' 的乱码
    也被判成"匹配"，闸门就形同虚设。
    """
    s0 = (shown or "").replace("…", "...").strip()
    t = (target or "").strip()
    if not s0 or not t:
        return False
    if not re.search(r"[\u4e00-\u9fff]", s0):
        return False                       # 读不出中文 → 一律不认（fail-closed）
    if s0 in t:
        return True
    m = re.search(r"\.{2,}", s0)
    if not m:
        return False
    head, tail = s0[:m.start()].strip(), s0[m.end():].strip()
    if head and tail:
        return t.startswith(head) and t.endswith(tail)
    if head:
        return t.startswith(head) and len(head) >= 2
    return False


def target_group():
    """真群名从 config.yaml 读 —— 切回时要用，绝不硬编码。"""
    try:
        import yaml
        with open(os.path.join(DIR, "config.yaml"), encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        return str((cfg.get("target") or {}).get("group") or "")
    except Exception as exc:
        print("⚠️ 读 config.yaml 失败：%r" % (exc,))
        return ""


def log_offset():
    try:
        return os.path.getsize(WB_LOG)
    except OSError:
        return 0


def log_since(off):
    try:
        with open(WB_LOG, "rb") as fh:
            fh.seek(off)
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def main():
    argv = sys.argv[1:]
    times = 1
    if "--times" in argv:
        i = argv.index("--times")
        try:
            times = max(1, int(argv[i + 1]))
        except (IndexError, ValueError):
            print("用法错误：--times 后面要跟数字")
            return 2
        del argv[i:i + 2]
    dry = "--dry" in argv
    if dry:
        argv.remove("--dry")
    text = argv[0] if argv else "耗时测量喵"

    group = target_group()
    print("真群（config.yaml）：%r" % group)
    if not group:
        print("🛑 读不到真群名，不敢动会话 —— 先修 config.yaml")
        return 2

    rc = 0
    try:
        print("\n① 切到安全会话「%s」…" % SAFE_CHAT)
        r, dt = post("/open", {"chat": SAFE_CHAT}, timeout=240)
        print("   /open 用时 %.2fs → %s" % (dt, json.dumps(r, ensure_ascii=False)[:140]))

        print("② /where 回读真实标题（硬闸门）…")
        w, _ = get("/where")
        title = str(w.get("chat_title") or "")
        print("   chat_title = %r   in_target = %r" % (title, w.get("in_target")))
        if not looks_like(title, SAFE_CHAT):
            print("🛑 拒绝发送：当前会话不是「%s」。测试一律不发。" % SAFE_CHAT)
            return 3
        print("   ✅ 闸门通过：确实在文件传输助手")

        if dry:
            print("\n（--dry 演练：只切会话不发送）")
            return 0

        off = log_offset()
        print("\n③ 发送 %d 条（文本：%r）" % (times, text))
        for i in range(times):
            r, dt = post("/send", {"text": text}, timeout=300)
            ok = r.get("ok")
            print("   第 %d 条 /send 用时 %.2fs → ok=%s %s"
                  % (i + 1, dt, ok, json.dumps(r, ensure_ascii=False)[:140]))
            if not ok:
                rc = 1

        new = log_since(off)
        tim = [l for l in new.splitlines() if "[timing]" in l]
        print("\n=== 桥接内部分段耗时（%d 行）===" % len(tim))
        if tim:
            for l in tim:
                print("  " + l.strip())
        else:
            print("  （没有 [timing] 行 —— 桥接没带 WXBRIDGE_TIMING=1 启动）")
            print("  让桥接带上计时：WXBRIDGE_TIMING=1 bash wxbridge_ctl.sh restart")
        return rc
    finally:
        print("\n④ 切回真群「%s」…" % group)
        try:
            r2, dt2 = post("/open", {"chat": group}, timeout=240)
            print("   /open 用时 %.2fs → %s"
                  % (dt2, json.dumps(r2, ensure_ascii=False)[:140]))
            w2, _ = get("/where")
            print("   回位校验 chat_title = %r" % (w2.get("chat_title"),))
        except Exception as exc:
            print("   ⚠️ 切回失败：%r —— 请确认机器人已回到真群！" % (exc,))


if __name__ == "__main__":
    sys.exit(main())
