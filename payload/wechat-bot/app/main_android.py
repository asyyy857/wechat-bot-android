#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手机端入口：读/发都走 wxbridge（微信跑在虚拟屏，见 wxbridge.py）。

用法::

    python3 main_android.py --probe              # 切到目标群 + 读一帧就退出
    python3 main_android.py --dry-run --rounds 5 # 全流程跑 5 轮，只记录不发
    python3 main_android.py                      # 常驻

跟 PC 版（main.py）的区别：读屏不是 OCR 本机窗口，而是问手机上的
HTTP 桥要结构化结果（截图 + RapidOCR + 像素气泡分析）；发消息走
无键盘输入法 commitText。逻辑层（Bot/Matcher/Brain/Governor）一行没改。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# ⚠️ 容器时区是 UTC，比手机（北京时间）慢 8 小时。机器人的**人设时段**
# （白天贴吧老哥 / 深夜猫娘）、主动开口窗口、零点文案、夜间慢速轮询
# 全靠本地时间判断 —— 必须在这里兜底，不能只靠 bot.sh 里那句 export。
# 实测踩坑：用 recover.sh 启动时没带 TZ，日志写成 03:47「进入夜间慢速轮询」，
# 白天被当成凌晨：10 分钟才读一次屏（漏消息），人设也换成了深夜猫娘。
os.environ.setdefault("TZ", "Asia/Shanghai")
try:
    time.tzset()
except Exception:      # pragma: no cover
    pass

from wxauto import Bot, load_config
from wxauto.platforms.android import AndroidReader, AndroidSender

from wxauto.paths import DATA_DIR as _DATA_DIR
KEY_FILE = _DATA_DIR / ".deepseek_key"


def apply_key_file(cfg) -> str:
    """API Key 的第三个来源：同目录下的 .deepseek_key 文件。

    比直接写进 config.yaml 干净 —— config.yaml 容易被整份贴出去，
    这个文件不会。优先级：config.yaml / 环境变量 > .deepseek_key。
    """
    if str(getattr(cfg.llm, "api_key", "") or ""):
        return "config.yaml 或环境变量"
    if KEY_FILE.exists():
        key = KEY_FILE.read_text(encoding="utf-8").strip()
        if key:
            cfg.llm.api_key = key
            return str(KEY_FILE)
    return ""


def cmd_check_llm(bot) -> int:
    """手机上验证 API Key / 接口 / 模型名（PC 版 main.py 依赖 winapi，手机上跑不了）。"""
    cfg = bot.cfg.llm
    key = str(getattr(cfg, "api_key", "") or "")
    print(f"接口地址 : {cfg.base_url}")
    print(f"模型名   : {cfg.model}")
    if not key:
        print("API Key  : (空)")
        print()
        print("两种填法，任选其一：")
        print(f"  1) 把 key 写进 {KEY_FILE}")
        print("  2) 打开 config.yaml，填 llm.api_key")
        print("申请地址：https://platform.deepseek.com")
        return 1
    print(f"API Key  : {key[:6]}…{key[-4:]}（共 {len(key)} 位）")
    if not bot.brain.available:
        print("llm.enabled 是 false，模型通道被关掉了")
        return 1

    print("发送最小测试请求…")
    t0 = time.time()
    body = bot.brain._post({
        "model": cfg.model,
        "messages": [{"role": "user", "content": "回复一个字：好"}],
        "max_tokens": 8,
        "stream": False,
    })
    dt = time.time() - t0
    if not body:
        print(f"失败（耗时 {dt:.1f}s），具体原因看上面的报错。")
        return 1
    try:
        text = body["choices"][0]["message"]["content"]
    except Exception:
        print(f"返回结构异常：{str(body)[:300]}")
        return 1
    print(f"成功（耗时 {dt:.1f}s）")
    print(f"模型回复：{text!r}")
    if body.get("usage"):
        print(f"Token 用量：{body['usage']}")

    print()
    print("---- 按人设生成一句试试 ----")
    from wxauto.reader import Block, Message, Snapshot
    fake = Snapshot(chat_title="(测试)", width=636, height=1400, messages=[])
    msg = Message(blocks=[Block("有人打洲嘛", 140, 900, 200, 30)], is_self=False,
                  sender="张三")
    fake.messages = [msg]
    t0 = time.time()
    out = bot.brain.compose(fake, msg, "有人@你")
    print(f"耗时 {time.time() - t0:.1f}s   生成结果：{out!r}")
    print()
    print(f"本次用量：{bot.brain.usage()}")
    return 0 if out else 1


def cmd_probe(reader, sender) -> int:
    reader.attach()
    snap = reader.read()
    if snap is None:
        print("读屏失败：桥接服务没返回结果（看看 logs/wxbridge.log）")
        return 1
    print(f"当前会话      : {snap.chat_title!r}")
    print(f"是否目标群    : {reader.in_target_group(snap)}")
    print(f"画面尺寸      : {snap.width} x {snap.height}")
    print(f"解析出 {len(snap.messages)} 条消息：")
    for i, m in enumerate(snap.messages):
        who = "我" if m.is_self else "他"
        tag = f"<{m.sender}>" if m.sender else ""
        print(f"  [{i:3d}] {who} {tag:12s} {m.text!r}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="微信人设自动回复机器人 · 手机端")
    ap.add_argument("--url", default=os.environ.get("WX_BRIDGE_URL", "http://127.0.0.1:8765"))
    ap.add_argument("--dry-run", action="store_true", help="只记录不真发")
    ap.add_argument("--probe", action="store_true", help="切群 + 读一帧就退出")
    ap.add_argument("--no-open", action="store_true", help="不先切群（假定已经在对的会话里）")
    ap.add_argument("--rounds", type=int, default=0, help="跑够多少轮就退出（测试用）")
    ap.add_argument("--check-llm", action="store_true", help="验证 API Key / 模型通道")
    args = ap.parse_args(argv)

    cfg = load_config()
    src = apply_key_file(cfg)
    if src and src != "config.yaml 或环境变量":
        print(f"（API Key 读自 {src}）")
    group = str(getattr(cfg.target, "group", "") or "")
    reader = AndroidReader(args.url, group)
    sender = AndroidSender(args.url, target_group=group)
    bot = Bot(cfg, dry_run=args.dry_run, reader=reader, sender=sender)

    try:
        reader.attach()
    except Exception as exc:
        print(f"桥接服务连不上：{exc}")
        return 2

    if not args.no_open and group and not args.check_llm:
        print(f"切到目标群：{group} …")
        t0 = time.time()
        ok = reader.open_chat(group)
        print(f"{'切群成功' if ok else '切群失败'}（{time.time() - t0:.1f}s）")
        if not ok:
            print("标题没能匹配上目标群 —— 为安全起见不往下跑。")
            return 2

    if args.check_llm:
        return cmd_check_llm(bot)

    if args.probe:
        return cmd_probe(reader, sender)

    bot.run(max_rounds=args.rounds or None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
