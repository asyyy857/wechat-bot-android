# -*- coding: utf-8 -*-
"""命令行入口。

用法：
    python main.py                 常驻运行
    python main.py --probe         只读一帧，打印 OCR 解析结果（不发送）
    python main.py --probe -v      同上，但把每个文本块的坐标也打出来
    python main.py --dry-run       全流程跑，但只记录不发消息
    python main.py --send 你好     测试发送链路（直接发到目标群，慎用）
"""
from __future__ import annotations

import argparse
import sys
import time

from wxauto import Bot, load_config
from wxauto.reader import build_layout, normalize
from wxauto import winapi


def cmd_probe(bot: Bot, verbose: bool) -> int:
    bot.reader.attach()
    print("OCR 引擎预热中…")
    t0 = time.time()
    bot.reader.ocr.warmup()
    print(f"预热耗时 {time.time() - t0:.2f}s")

    snap = bot.reader.read()
    if snap is None:
        print("读屏失败：微信窗口可能被最小化")
        return 1

    lay = snap.layout
    print()
    if lay is not None:
        print(f"窗口尺寸      : {snap.width} x {snap.height}")
        print(f"聊天区 x 范围 : {lay.chat_left} ~ {lay.chat_right}")
        print(f"聊天区 y 范围 : {lay.chat_top} ~ {lay.chat_bottom}")
        print(f"判定'我发的'阈值: x >= {lay.self_x_threshold}")
        print(f"输入框点击点  : {lay.input_click}")
        print(f"发送按钮点击点: {lay.send_click}")
    else:
        print("（该读取层不提供布局信息，手机端无障碍读的是结构化文本）")
    print()
    print(f"识别到标题    : {snap.chat_title!r}")
    print(f"是否目标群    : {bot.reader.in_target_group(snap)}")
    print(f"OCR 耗时      : {snap.ocr_ms} ms")
    print()
    print(f"=== 解析出 {len(snap.messages)} 条消息 ===")
    for i, m in enumerate(snap.messages):
        who = "我" if m.is_self else "他"
        print(f"  [{i:3d}] {who}  y={m.top:4d} x={m.left:4d}-{m.right:4d}  {m.text!r}")

    if verbose:
        print()
        print("=== 原始文本块 ===")
        img = winapi.grab_window(bot.reader.hwnd)
        blocks, _ = bot.reader.ocr.run(img)
        for b in sorted(blocks, key=lambda b: (b.y, b.x)):
            print(f"  [{b.x:4d},{b.y:4d} {b.w:4d}x{b.h:3d}] {b.score:.2f}  {b.text!r}")
    return 0


def cmd_send(bot: Bot, text: str) -> int:
    bot.reader.attach()
    print("OCR 预热…")
    bot.reader.ocr.warmup()
    ok = bot.sender.send(text)
    print("发送成功" if ok else "发送失败")
    return 0 if ok else 1


def cmd_check_llm(bot: Bot) -> int:
    """用最小请求验证 API Key / 接口 / 模型名是否可用。"""
    cfg = bot.cfg.llm
    print(f"接口地址 : {cfg.base_url}")
    print(f"模型名   : {cfg.model}")
    key = str(getattr(cfg, "api_key", "") or "")
    if not key:
        print("API Key  : (空)")
        print()
        print("还没配置 API Key。两种填法，任选其一：")
        print("  1) 打开 config.yaml，把 llm.api_key 填上")
        print("  2) 设置环境变量 DEEPSEEK_API_KEY")
        print()
        print("申请地址：https://platform.deepseek.com")
        return 1
    print(f"API Key  : {key[:6]}…{key[-4:]}（共 {len(key)} 位）")
    print()

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
        print(f"失败（耗时 {dt:.1f}s）。上面应该打印了具体原因。")
        return 1
    try:
        text = body["choices"][0]["message"]["content"]
    except Exception:
        print(f"返回结构异常：{str(body)[:300]}")
        return 1
    print(f"成功（耗时 {dt:.1f}s）")
    print(f"模型回复：{text!r}")
    usage = body.get("usage") or {}
    if usage:
        print(f"Token 用量：{usage}")
    print()
    print("---- 按人设生成一句试试 ----")
    from wxauto.reader import Block, Layout, Message, Snapshot
    lay = Layout(width=1342, height=971, chat_left=461, chat_right=1322,
                 chat_top=92, chat_bottom=859)
    fake = Snapshot(chat_title="(测试)", width=1342, height=971, layout=lay, messages=[])
    msg = Message(blocks=[Block("群友 今晚打游戏吗", 672, 200, 200, 26)], is_self=False)
    fake.messages = [msg]
    t0 = time.time()
    out = bot.brain.compose(fake, msg, "有人@你")
    print(f"耗时 {time.time() - t0:.1f}s   生成结果：{out!r}")
    print()
    print(f"本次测试：{bot.brain.usage()}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="微信人设自动回复机器人")
    ap.add_argument("-c", "--config", help="配置文件路径", default=None)
    ap.add_argument("--probe", action="store_true", help="只读一帧并打印解析结果")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印原始 OCR 文本块")
    ap.add_argument("--dry-run", action="store_true", help="只记录不真发")
    ap.add_argument("--check-llm", action="store_true", help="测试 API Key 与模型通道")
    ap.add_argument("--send", metavar="TEXT", help="测试发送一句话")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    bot = Bot(cfg, dry_run=args.dry_run)

    if args.probe:
        return cmd_probe(bot, args.verbose)
    if args.check_llm:
        return cmd_check_llm(bot)
    if args.send:
        return cmd_send(bot, args.send)
    bot.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
