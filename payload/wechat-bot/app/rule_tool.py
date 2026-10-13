#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rule_tool.py —— 关键词规则小工具（微信机器人 wxauto）。

给 rules.yaml **增 / 删 / 查 / 试跑**，并自动触发机器人热重载。
命令行入口是同目录的 ``rule.sh``（用法见那个脚本头部）。

四条设计原则，每条都是踩过的坑：

1. **绝不整体 YAML dump** —— rules.yaml 里全是手写注释，PyYAML 一 dump 全没了。
   所有改动都是「文本插入 / 文本删除」，注释原样保留。
2. **写完必须动 mtime** —— 机器人的热重载靠 ``rules.yaml`` 的 mtime 比对；
   某些写入方式不会更新 mtime（本仓库的 edit 工具就踩过），
   所以这里显式 ``os.utime()``，不靠运气。
3. **改完先自校验，不过关就回滚** —— yaml 解析 + 真跑一遍 Matcher；
   新版发不出去就立刻从备份恢复，绝不给机器人留一个坏文件。
4. **回复默认自动加「喵」** —— 发送层统一处理，规则里别自己写喵；
   确实要原样发的（比如「喵喵喵 → 老吴！」）加 ``--no-meow``。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
RULES = os.path.join(ROOT, "rules.yaml")
LOG = str(__import__("os").environ.get("WXBOT_LOG_DIR") or "/sdcard/dsh/wechat-bot/logs") + "/console.log"
ANCHOR = "  # ====================== 你指定的：看见就回 ======================"
BOT_PAT = r"main_android\.py"
LOCK_PATH = "/tmp/wechat-auto.lock"  # 跨进程锁，/tmp 合适（不进 data/）      # 与 wxlock.sh 共用同一把锁

sys.path.insert(0, ROOT)

import yaml                                          # noqa: E402
from wxauto.config import load_config, load_rules    # noqa: E402
from wxauto.matcher import Matcher, Rule             # noqa: E402
from wxauto.safety import validate_outgoing, with_meow   # noqa: E402


# ------------------------------------------------------------------ 小工具
def die(msg: str, code: int = 1):
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(code)


def acquire_lock(what: str):
    """抢「微信机器人操作锁」（和 bash 的 wxlock.sh 共用 /tmp/wechat-auto.lock）。

    只有**改动 rules.yaml** 的命令才需要它 —— 两个会话同时「读全文 → 插入 → 写回」
    时，后写的会把先加的规则静默覆盖掉。抢不到就直接退出，绝不硬写。
    返回的 fd 要一直持有（脚本退出时 OS 自动释放 flock，不会留下死锁）。
    """
    fd = open(LOCK_PATH, "a+")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fd.seek(0)
        info = fd.read().strip() or "(锁文件是空的)"
        die("另一个会话正在操作微信机器人，先等它做完：\n     "
            + info.replace("\n", "\n     ")
            + f"\n     （锁：{LOCK_PATH} —— 对方进程结束会自动释放，过几秒重试）")
    fd.seek(0)
    fd.truncate()
    fd.write(f"PID {os.getpid()}  {what}  开始 "
             + _dt.datetime.now().strftime("%F %T") + "\n")
    fd.flush()
    return fd


def _ts() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def read_rules_text() -> str:
    with open(RULES, encoding="utf-8") as f:
        return f.read()


def write_rules_text(text: str) -> None:
    # ★ 2026-10-08 审查修复（M3）：原来直接 open(...,"w") 截断式写，
    #   而机器人每轮都在看这个文件的 mtime —— 正好落在"已截断、还没写完"
    #   那一瞬就会读到空/半截 YAML（旧代码还会把 mtime 基线推进，于是这次
    #   改动从此不再生效）。改写走 atomic_write（tmp → replace，/sdcard 兜底）。
    from wxauto.atomic import atomic_write
    atomic_write(RULES, text)
    os.utime(RULES, None)          # ★ 关键：不碰 mtime，机器人就永远不重载


def backup() -> str:
    dst = f"{RULES}.bak-{_ts()}"
    shutil.copy2(RULES, dst)
    return dst


def restore(src: str) -> None:
    shutil.copy2(src, RULES)
    os.utime(RULES, None)


def make_matcher() -> Matcher:
    return Matcher([Rule.from_dict(d) for d in load_rules()])


def reply_limit() -> int:
    try:
        return int(getattr(load_config().behavior, "max_reply_chars", 40))
    except Exception:
        return 40


def q(s) -> str:
    """转成 YAML/JSON 通用的双引号字符串，中文不转义、引号自动转义。"""
    return json.dumps(str(s), ensure_ascii=False)


def unquote(s) -> str:
    return str(s).strip().strip('"\'“”‘’')


def split_multi(s: str):
    """``"a|b｜c"`` -> ``["a","b","c"]``（要加引号，否则 shell 会当管道）。"""
    return [p.strip() for p in re.split(r"[|｜]", s or "") if p.strip()]


# ------------------------------------------------------------- 「喵」的规范化
# 用户手写回复时经常自己就带上了「喵」。发送层（safety.with_meow）的规矩是：
#   · 结尾正好是「喵」→ 不再补（所以「哪里骚了喵」发出来还是「哪里骚了喵」，没问题）
#   · 但「喵」后面还有标点（「哪里骚了喵！」）→ 它认为没带喵，会补成「…喵！喵」
# 所以入库前统一收拾：
_MIAO_TAIL_RE = re.compile(r"喵+$")
_MIAO_PUNCT_RE = re.compile(r"喵+([！!。.～~…?？]+)$")


def normalize_replies(replies, no_meow):
    """把回复里用户自己写的「喵」规范化。返回 (新回复列表, no_meow, 提示列表)。

    · 结尾正好是「喵」→ 去掉它，交给发送层统一加（发出来一字不差，且规则表干净）
    · 「喵」后面还有标点 → 保留原样，并把这条改成 no_meow（原样发），
      否则会变成「哪里骚了喵！喵」
    """
    out, notes = [], []
    for r in replies:
        if not r:
            continue
        if _MIAO_PUNCT_RE.search(r):
            notes.append(f"「{r}」的喵后面还有标点 → 这条改成原样发（不再自动补喵）")
            no_meow = True
            out.append(r)
        elif _MIAO_TAIL_RE.search(r) and _MIAO_TAIL_RE.sub("", r).strip():
            cleaned = _MIAO_TAIL_RE.sub("", r).strip()
            notes.append(f"「{r}」结尾已经带喵 → 存成「{cleaned}」，发送时统一加回")
            out.append(cleaned)
        else:                      # 回复就是「喵」「喵喵喵」这种，原样留着（不会双喵）
            out.append(r)
    return out, no_meow, notes


# ------------------------------------------------------------------ 生成规则块
def build_block(kws, replies, a) -> str:
    name = a.name or re.sub(r"[^\w\u4e00-\u9fff]+", "", kws[0]) or "rule"
    head = " / ".join(kws[:2])
    tail = "" if a.image else (replies[0] if replies else "")
    lines = [f"  # ---- {_dt.date.today()} 加的：看见「{head}」就回「{tail}」----"]
    lines.append(f"  - name: {q(name)}")
    lines.append("    keywords: [" + ", ".join(q(k) for k in kws) + "]")
    lines.append(f"    mode: {a.mode}")
    if a.max_len:
        lines.append(f"    max_len: {a.max_len}")
    if a.image:
        lines.append("    action: image")
        lines.append(f"    image_tag: {q(a.image)}")
    else:
        lines.append("    replies: [" + ", ".join(q(r) for r in replies) + "]")
    lines.append(f"    priority: {a.priority}")
    lines.append(f"    cooldown: {a.cooldown}")
    if a.mention_exempt:
        lines.append("    mention_exempt: true")
    if a.no_meow:
        lines.append("    no_meow: true")
    return "\n".join(lines)


def insert_block(text: str, block: str) -> str:
    i = text.find(ANCHOR)
    if i < 0:                                   # 锚点没了就退回 rules: 之后
        j = text.find("rules:")
        if j < 0:
            die("rules.yaml 里找不到 rules: 也没有锚点注释，文件结构变了？")
        ins = text.index("\n", j) + 1
    else:
        ins = text.index("\n", i) + 1
    pre = "" if text[ins:ins + 1] == "\n" else "\n"
    # 后面也补一个空行：连续加第二条时，新块才会和上一条之间有空行分隔
    post = "" if text[ins:ins + 1] == "\n" else "\n"
    return text[:ins] + pre + block + "\n" + post + text[ins:]


def delete_block(text: str, target: str):
    """按规则名或关键词删掉一整块（含它上方的注释行）。返回 (新文本, 规则名)。"""
    lines = text.split("\n")
    starts = [i for i, ln in enumerate(lines) if re.match(r"^  - name:", ln)]
    for si, s in enumerate(starts):
        e = starts[si + 1] if si + 1 < len(starts) else len(lines)
        for k in range(s + 1, e):               # 块里只允许 4 空格缩进行
            ln = lines[k]
            if ln.strip() and not ln.startswith("    "):
                e = k
                break
        while e - 1 > s and not lines[e - 1].strip():
            e -= 1
        block_text = "\n".join(lines[s:e])
        try:
            data = yaml.safe_load(block_text) or [{}]
            r0 = data[0] if isinstance(data, list) else data
        except Exception:
            r0 = {}
        nm = unquote(r0.get("name", ""))
        kws = [str(x) for x in (r0.get("keywords") or [])]
        if target not in (nm, *kws):
            continue
        a = s
        while a - 1 >= 0 and lines[a - 1].startswith("  #") and "===" not in lines[a - 1]:
            a -= 1                              # 上吞自己那条注释
        del lines[a:e]
        out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
        return out, nm
    return None, None


# ------------------------------------------------------------------ 热重载确认
def bot_running() -> bool:
    try:
        out = subprocess.run(["pgrep", "-f", BOT_PAT],
                             capture_output=True, text=True)
        return bool(out.stdout.strip())
    except Exception:
        return False


def wait_reload(expect_n: int, timeout: int = 45):
    """等机器人写出「已重载 N 条」。返回日志行；没在跑返回 None；超时返回 ""。"""
    if not bot_running():
        return None
    try:
        start = os.path.getsize(LOG)
    except OSError:
        start = 0
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with open(LOG, encoding="utf-8", errors="replace") as f:
                f.seek(start)
                tail = f.read()
        except OSError:
            tail = ""
        for ln in tail.splitlines():
            if "rules.yaml" in ln and "重载" in ln:
                return ln.strip()
        time.sleep(2)
    return ""


def report_reload(n: int, no_wait: bool):
    if no_wait:
        print("   （--no-wait：没等热重载，机器人下一轮会自己读）")
        return
    got = wait_reload(n)
    if got is None:
        print("   ⚠️ 机器人当前没在跑 —— 改动会在下次 `bash bot.sh start` 时生效")
    elif got:
        print(f"   ✅ 机器人已热重载：{got}")
    else:
        print("   ⚠️ 等超时了，看 `bash bot.sh status`；实在没重载就重启机器人")


# ------------------------------------------------------------------ 子命令
def cmd_list():
    rules = load_rules()
    lim = reply_limit()
    print(f"共 {len(rules)} 条规则   文件：{RULES}\n")
    for r in rules:
        kws = " / ".join(str(x) for x in (r.get("keywords") or []))
        if str(r.get("action", "reply")) == "image":
            eff = f"[表情包 {r.get('image_tag', '')}]"
        elif str(r.get("action", "reply")) == "meitu":
            eff = f"[今日美图 {r.get('meitu_source', '') or '全部混池'}]"
        else:
            reps = r.get("replies") or []
            eff = " / ".join(with_meow(x, lim, no_meow=bool(r.get("no_meow")))
                             for x in reps)
        star = "★不加喵" if r.get("no_meow") else "      "
        print(f"  {star}  {str(r.get('name')):<12} p{str(r.get('priority', 0)):<5}"
              f" {kws:<22} → {eff}")


def cmd_test(msg: str):
    m = make_matcher()
    hit = m.match(msg)
    lim = reply_limit()
    print(f"输入：{msg!r}")
    if not hit:
        print("命中：无 —— 关键词规则不接这条（只有被 @ 或轮到主动插话才走模型）")
        return
    print(f"命中：{hit.name}（priority {hit.priority}，cooldown {hit.cooldown}s）")
    if hit.is_meitu:
        # 「今日美图」没有文字模板，说清楚它真跑时会干什么就返回 ——
        # 不然下面会拿 None 去走 with_meow / 出站校验，那是给文字用的（2026-10-08）
        print(f"（这条是「今日美图」规则：真跑时联网随机取一张图发出去，"
              f"分类={hit.meitu_source or '全部混池'}，不发文字）")
        return
    rep = m.reply_for(hit)
    out = with_meow(rep, lim, no_meow=bool(hit.no_meow))
    bad = validate_outgoing(out, lim)
    print(f"模板：{rep!r}   →   最终发出：{out!r}")
    if hit.is_image:
        print(f"（这条是发图规则：image_tag={hit.image_tag!r}）")
    if bad:
        print(f"⚠️ 会被出站校验拦下：{bad}")


def cmd_add(a):
    kws = split_multi(a.keyword) + [x for x in (a.kw or []) if x]
    kws = [x.strip() for x in kws if x and x.strip()]
    replies = split_multi(a.reply) + [x for x in (a.reply_list or []) if x]
    replies = [x.strip() for x in replies if x and x.strip()]
    if not kws:
        die("要给个关键词，例如：bash rule.sh 骚 哪里骚了")
    if not replies and not a.image:
        die("要给个回复，或者用 --image 发表情包，例如：bash rule.sh 骚 哪里骚了")

    # 你自己写的「喵」在这里统一收拾：结尾的喵去掉（发送层会加回来），
    # 「喵」后面还跟标点的改成原样发 —— 保证怎么都不会出现两个喵。
    if not a.image:
        replies, no_meow, notes = normalize_replies(replies, bool(a.no_meow))
        a.no_meow = no_meow
        for _n in notes:
            print(f"   ℹ️ {_n}")
        if not replies:
            die("回复被清理成空的了，换一个吧")

    _lock = acquire_lock('rule.sh 加规则 ' + '/'.join(kws))

    for r in load_rules():                       # 查重（关键词完全一样才拦）
        for k in (r.get("keywords") or []):
            if str(k) in kws and not a.force:
                die(f"关键词「{k}」已经在规则「{r.get('name')}」里了（要覆盖加 --force）")

    bk = backup()
    block = build_block(kws, replies, a)
    try:
        write_rules_text(insert_block(read_rules_text(), block))
        yaml.safe_load(read_rules_text())        # 语法自校验
        m = make_matcher()
        n = len(m.rules)
        if a.mode != "regex":                    # 真跑一遍，确保新词能命中
            hit = m.match(kws[0])
            if hit is None:
                raise RuntimeError(f"加完了却匹配不上（kws={kws}），回滚")
            if not a.image and m.reply_for(hit) not in replies:
                raise RuntimeError(f"命中到了别的规则「{hit.name}」，回滚")
    except Exception as exc:
        restore(bk)
        die(f"新规则没通过校验，已回滚 → {exc}")

    shown = (f"[表情包 {a.image}]" if a.image else
             " / ".join(with_meow(x, reply_limit(), no_meow=a.no_meow)
                        for x in replies))
    print(f"✅ 已加规则：{' / '.join(kws)}  →  {shown}")
    print(f"   规则数：{n} 条    优先级 p{a.priority}    备份：{os.path.basename(bk)}")
    report_reload(n, a.no_wait)


def cmd_delete(a):
    target = a.delete
    _lock = acquire_lock(f'rule.sh 删规则 {target}')
    bk = backup()
    new, nm = delete_block(read_rules_text(), target)
    if new is None:
        os.remove(bk)
        die(f"找不到规则「{target}」（用 --list 看看名字）")
    try:
        write_rules_text(new)
        yaml.safe_load(read_rules_text())
        n = len(make_matcher().rules)
    except Exception as exc:
        restore(bk)
        die(f"删完校验不过，已回滚 → {exc}")
    print(f"🗑  已删除规则「{nm}」    剩余 {n} 条    备份：{os.path.basename(bk)}")
    report_reload(n, a.no_wait)


# ------------------------------------------------------------------ 入口
def main():
    p = argparse.ArgumentParser(
        prog="rule.sh",
        description="微信机器人关键词规则小工具（增 / 删 / 查 / 试跑）")
    p.add_argument("keyword", nargs="?", help="触发词；多个用 | 分隔（记得加引号）")
    p.add_argument("reply", nargs="?", help="回复内容（发送层会自动加「喵」）")
    p.add_argument("--kw", action="append", help="追加一个关键词（可重复）")
    p.add_argument("--reply", dest="reply_list", action="append",
                   help="追加一条候选回复（可重复，命中时随机抽一条）")
    p.add_argument("--name", help="规则名（默认取关键词）")
    p.add_argument("--priority", type=int, default=58, help="默认 58，越大越优先")
    p.add_argument("--cooldown", type=int, default=120, help="默认 120 秒")
    p.add_argument("--mode", default="contains",
                   choices=["contains", "exact", "regex"])
    p.add_argument("--max-len", dest="max_len", type=int,
                   help="消息长度超过它就不触发")
    p.add_argument("--no-meow", dest="no_meow", action="store_true",
                   help="这条原样发，不追加「喵」")
    p.add_argument("--mention-exempt", dest="mention_exempt", action="store_true",
                   help="被 @ 时不走这条，交给模型")
    p.add_argument("--image", help="改成发表情包：填 image_tag 关键词")
    p.add_argument("--force", action="store_true", help="关键词重复也照加")
    p.add_argument("--no-wait", dest="no_wait", action="store_true",
                   help="不等热重载确认")
    p.add_argument("--list", action="store_true", help="列出所有规则")
    p.add_argument("--test", metavar="文本", help="试跑一句话，不写文件")
    p.add_argument("--del", dest="delete", metavar="名字或关键词", help="删除规则")

    a = p.parse_args()
    if a.list:
        cmd_list()
    elif a.test is not None:
        cmd_test(a.test)
    elif a.delete:
        cmd_delete(a)
    elif a.keyword:
        cmd_add(a)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
