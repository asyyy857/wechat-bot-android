# -*- coding: utf-8 -*-
"""把活儿交给 DSH 的 headless agent（2026-10-09 用户要求）。

用户原话：「我想让这个机器人更智能一点，可以让它帮我干点事情」，随后自己点破：
**「让机器人直接调用你的模型不就行了吗？」** —— 对。DSH 自带
`dsh --profile headless "<任务>"`：它起的是一个**带全套工具的 agent**
（真搜索、读网页、跑命令、操作手机），比机器人自己去抓维基百科强得多。
这条路就是"主人喊它办事 → 转给 agent → 把答复发回群里"。

⚠️ 安全边界（这段别删）：
  · **只有主人自己**（`msg.is_self`，他喊「昵称 …」那条路）才会走到这儿 ——
    群里任何人都能指挥一个有 bash/文件/手机权限的 agent，那是不能接受的；
  · 任务外面套一层硬约束（`_WRAP`）：只查资料/只回答，禁止改文件、禁止改变
    系统状态、禁止碰手机；
  · 有超时（`agent.timeout`）和每日次数上限（`agent.max_per_day`）。

用法::

    from wxauto import agentask
    ans = agentask.ask("鲁迅那句'一见短袖子'出自哪篇？")     # -> str | None
"""
from __future__ import annotations

import os
import subprocess

# 任务外面套的硬约束 —— 让 agent 知道"你只是被叫来查一件事的"
_WRAP = (
    "【微信机器人转来的临时求助】只做这一件事，做完用一两句中文回答，"
    "不要复述这段说明。\n"
    "硬性限制：① 只允许查资料、阅读、回答问题；② **不要创建/修改/删除任何文件**，"
    "不要执行会改变系统状态的命令（查时间/日期这类读取命令可以）；"
    "③ **不要操作手机**（不要用 phone 工具）；④ 查不到就直说查不到，不要编。\n"
    "⑤ **要快**：群里的人在等，最多搜一两次就用现有信息作答，别反复验证、别写长报告。\n"
    "任务：%s"
)


def available() -> bool:
    """`dsh` 在不在 PATH 里。"""
    from shutil import which
    return bool(which("dsh"))


def ask(task, timeout=180.0, cwd="/root", max_chars=0):
    """把一句话交给 headless agent，返回它的最终答复（失败/超时给 None）。

    `cwd` 用 /root：那儿只有工作区指令（含"一律说中文"），没有机器人的代码，
    agent 不会被满屏项目文件带偏、也少了误改的机会。
    """
    task = (task or "").strip()
    if not task or not available():
        return None
    try:
        r = subprocess.run(
            ["dsh", "--profile", "headless", _WRAP % task],
            capture_output=True, text=True, timeout=timeout, cwd=cwd,
            env=dict(os.environ))
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None
    out = (r.stdout or "").strip()
    if not out:
        return None
    # 只留最后一段（模型偶尔会在正文前再写一句"好的，我来查"）
    out = out.strip().strip('"').strip()
    if max_chars and len(out) > max_chars:
        out = out[:max_chars].rstrip() + "…"
    return out
