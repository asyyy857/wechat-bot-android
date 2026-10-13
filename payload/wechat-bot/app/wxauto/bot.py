# -*- coding: utf-8 -*-
"""主循环：读屏 -> 匹配 -> 判断 -> 发送。

两条硬规矩：
* 一轮只允许发一条消息，发完立刻回到"重新读屏"，避免基于过期画面连发。
* 任何时候 :class:`wxauto.safety.KillSwitch` 一响，立刻什么都不做。

支持热重载：改 ``rules.yaml``（关键词）、``persona.md`` / ``personas/``（人设）、
以及 ``tuning.json``（读屏间隔 / 回复延迟，控制面板上的「读屏节奏」卡片写的）
**都不用重启**，下一轮自动生效。模型这类设置仍然要重启。
"""
from __future__ import annotations

import json
import logging
import logging.handlers          # RotatingFileHandler 在这个子模块里
import os
import random
import re
import shutil
import subprocess
import sys
import time
from collections import Counter, deque
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .brain import Brain
from .chatlog import ChatLog
from .config import ROOT, load_config, load_persona, load_personas, load_rules
from .paths import LOGS_DIR
from .governor import Governor
from .matcher import Matcher, Rule
from .meitu import MeituFetcher
from .reader import Message, PcReader, Snapshot, normalize
from .safety import (IS_WINDOWS, FailureBreaker, KillSwitch, strip_action,
                    validate_outgoing, with_meow)
from .sender import PcSender
from .state import State
from .stickers import StickerLibrary
from . import drafts
from . import tuning as tune
from .watchdog import StallWatchdog

# ★ 2026-10-05：主动插话的概率是否随话题分数放大（见 reply 流程里那段注释）。
#   0 = 关掉（够阈值的话题一律用 behavior.proactive_chance）。
PROACTIVE_SCORE_CHANCE = os.environ.get("WX_PROACTIVE_SCORE_CHANCE", "1") != "0"
PROACTIVE_CHANCE_MAX = float(os.environ.get("WX_PROACTIVE_CHANCE_MAX", "3"))

# ---- 补发队列（2026-10-09 用户拍板）----
# 背景：一条话如果发送失败，原来是**永久丢了** —— `step()` 里 `state.mark(m)` 在
#   `handle(m)` **之前**执行，所以这条消息的指纹早就记进"见过"名单，下一轮读屏时
#   `is_new()` 返回 False，根本不会走到发送那一步（用户看到浮窗写着"下一轮会再试
#   这条"，其实从来没兑现过）。现在把没发出去的话挂进这个队列，下一轮优先补。
# 为什么要"下一轮补"而不是当场重试：发送失败多半是"你正在用手机"（抢不到输入
#   焦点），当场死磕只会把这一轮拖长（实测最坏 46 秒）；隔一轮（10 秒）再试，
#   你那会儿往往已经切走了。
# ⚠️ 时效宁可短：太久以前那句突然冒出来比漏发更奇怪。
# ★ 2026-10-10 改：90 → **300 秒**。因为同一天改了抢屏口径 —— 你在打字时
#   机器人当时**再也不抢键盘**（preflight_yield 的打字保护 —— ⚠️ 那条已于
#   2026-10-10 晚删除，现在"不管啥情况都抢"，见 wxbridge.py），
#   这段时间攒下的回复全靠补发；90 秒只够短消息，你打一段长字就全过期丢了
#   （用户 10-10 追问「我打完字切走了，它会自动判断我走了然后发消息是吗」）。
#   队列上限 3 → 6 条同理：打得久，攒的回复也多。
RESEND_TTL = float(os.environ.get("WX_RESEND_TTL", "300"))      # 秒：过期就丢
# ★ 2026-10-10 晚用户拍板：**2 → 1，只补发一次**。他的原话：
#   「改成只补发一次吧。补发也不成功，我会注意到的，然后我就来修」
#   —— 不要自动反复重试，试一次不成就放弃、浮窗明说"这条最终没发出去"，
#   他自己看到会来查。⚠️ 别再"顺手"改回 2：反复补发会连带**反复抢他键盘**
#   （补发的 `/send` 同样会走 preflight_yield 抢屏），那是他明确不要的。
RESEND_MAX_TRIES = int(os.environ.get("WX_RESEND_MAX_TRIES", "1"))   # 最多补发几次
RESEND_MAX = int(os.environ.get("WX_RESEND_MAX", "6"))          # 队列最多堆几条

# ★ 2026-10-10 晚用户报「群友只发了一个，他自己回了两个」：
#   **同一句模板回复，短时间内不重复发**（默认 180 秒）。
#   规则冷却（`governor.record_rule`）挡不住这种 —— 它挡的是"同一条**规则**"，
#   而群里连着发「大大大」「大大大大」「大大大大大」是**三条不同的消息**，
#   命中同一条规则、间隔又刚好超过规则 cooldown(30s)：实测 22:38:27 和 22:39:10
#   各回了一模一样的「大大大喵」，用户看到的就是"重复连发"。
#   ⚠️ 只对**模板回复**（rule_name 非空）生效，**不碰模型回复** ——
#   被 @ 问到、或者不同的人先后问同一件事，模型该答还是要答。
RULE_DEDUP_SECS = float(os.environ.get("WX_RULE_DEDUP_SECS", "180"))   # 秒

# ★ 2026-10-09 晚：**发图**的补发（表情包 / 美图那条路）。
#   起因：用户报「机器人有一些没发出去的」。查 22:00:19 那张「贴.png」的真相是
#   ——他当时**正在物理屏用微信**（拿它截图），微信单实例，虚拟屏上就没有微信了，
#   桥接连着 8 轮截不到画面（白跑 ≈26 秒），还把这个临时占用误报成「列表里没找到
#   （机器人不在群里？）」。所以桥接那侧改成「3 轮拿不到画面就收手 + 如实上报 +
#   标 retryable」，这里负责**等你放下手机再补发**。
#   ⚠️ 只在"桥接确认没点过发送键"的失败上补（retryable=True）—— 点过发送键的失败
#   可能其实已经发出去了，补发 = 重复发图，那是真事故（宁可漏，不可重）。
RESEND_IMAGE_TTL = float(os.environ.get("WX_RESEND_IMAGE_TTL", "180"))        # 秒
RESEND_IMAGE_MAX_TRIES = int(os.environ.get("WX_RESEND_IMAGE_MAX_TRIES", "2"))
RESEND_IMAGE_MAX = int(os.environ.get("WX_RESEND_IMAGE_MAX", "2"))            # 最多堆几张

# ★ 2026-10-10（用户报「机器人一直抢键盘、把我打的字重置成目标群，他是不是回不去群了」）：
#   **自动切群的失败退避**。老逻辑是"连续 3 轮不在目标群就切一次、失败也不管"，
#   于是切群一坏就变成每 15 秒抢一次物理屏输入焦点（用户在打字，搜索框被塞成
#   「目标群」）—— 实测 16:54~17:11 连报 30+ 次「自动切回失败」，用户只能把机器人停掉
#   才能打字。现在连续失败 `REGROUP_FAIL_MAX` 次就冷却 `REGROUP_COOLDOWN` 秒：
#   冷却期间**一次屏都不抢**，安静待命，并在浮窗提醒用户一次。
REGROUP_FAIL_MAX = int(os.environ.get("WX_REGROUP_FAIL_MAX", "3"))
REGROUP_COOLDOWN = float(os.environ.get("WX_REGROUP_COOLDOWN", "600"))        # 秒
REGROUP_COOLDOWN_MAX = float(os.environ.get("WX_REGROUP_COOLDOWN_MAX", "3600"))  # 秒

# ★ 2026-10-09（续24 用户口径）：「艾特他的时候以整句话为主，尽量不要触发关键词，
#   除非那句话里全是关键词」。被 @ 时把命中的关键词从消息里抠掉，**剩几个字**才
#   算"全是关键词"（见 `Bot._keyword_is_whole`）。2 = 允许"好骚啊""6啊"这种。
_KEYWORD_WHOLE_LEFT = int(os.environ.get("WX_KEYWORD_WHOLE_LEFT", "2"))

# ★ 2026-10-09 晚（用户报「群里我让你查一下，但是机器人以为是识别图片」）：
#   主人喊它时，老逻辑是「**旁边有图就先识图**」—— 而群里它旁边经常有图，于是
#   一句「昵称，给他一个学习网站」被答成「学习网站本喵没有 自己搜去喵」
#   （bot.log 21:57:58 就是这条）。所以只有**这句话本身在说图**时，识图才抢在
#   办事（agent）前面；不说图的话，先让它去办事。见 `Bot._looks_like_image_request`。
_IMAGE_ASK_WORDS = ("看图", "看看图", "图片", "照片", "相片", "截图", "表情包",
                    "这图", "那张", "这张", "图里", "图上", "像不像", "是不是我",
                    "认得", "这是谁", "识别", "长得")

# ★ 2026-10-10（读聊天记录卡片）：主人喊它读那张转发卡片时的词表。
#   判据是「提到聊天记录」**且**「带动作 / 疑问」两个都中才动手 —— 因为这一步
#   会**替主人点屏幕**（桥接 /read_record 要单击卡片、进详情页、再退出来），
#   不能因为他在群里随口提一句"聊天记录"就去点。
_RECORD_WORDS = ("聊天记录", "聊天纪录", "转发记录", "记录合集", "记录里")
# ★ 2026-10-10（用户口径：「补读只在我叫的时候翻」）：主人喊它**往回翻补读**的词表。
#   动机：群里爆发时一屏只有 11 条，间隔里刷过去的消息从来没上过屏（见交接说明续30/32）。
#   两个词表都中才动手 —— 这一步会**滚他的屏幕**（虽然翻完一定切回最新），
#   不能因为他在群里随口提一句"漏"就去滚。
_BACKFILL_WORDS = ("翻", "漏", "补", "上面", "前面的")
_BACKFILL_HINTS = ("回去", "往回", "往上", "刚才", "漏", "补", "错过",
                   "没看到", "看看", "看一下")
# ⚠️ 动作词必须**具体**，不能放裸的「读」「看」：主人一句「刚才那份聊天记录我看了」
#    也会命中裸「看」，那就变成"他在陈述、机器人却去点他的屏幕"。
#    实测（test_read_record.py 第 4 节）只有用「看一下 / 看看 / 读一下…」这种
#    请求式说法，正例才全中、反例才全不中。
_RECORD_HINTS = ("读一下", "读读", "读一读", "看一下", "看看", "看下", "点评",
                 "说说", "讲讲", "什么", "啥", "内容", "里面", "整理", "总结",
                 "翻译")
# 读到的原文存一份在这里（跟 ppt_seen.json 一个思路：事后能核查它到底读到了啥）
RECORD_ARCHIVE_DIR = os.path.join(ROOT, "聊天记录存档")

# ★ 2026-10-09（用户要求「让它帮我干点事情」）：转给 DSH headless agent 的活儿按天
#   计数（防滥用/防烧时间），落盘在项目根 —— 重启不清零。见 `_agent_should_take`。
AGENT_USED_FILE = os.path.join(ROOT, "agent_used.json")
# ★ 2026-10-12：工具层用量（冷却 / 每日配额），见 wxauto/tools.py
TOOL_USED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              os.pardir, "工具用量.json")


def setup_logger(cfg) -> logging.Logger:
    log = logging.getLogger("wxauto")
    if log.handlers:
        return log
    # ★ 2026-10-08 审查修复（M16）：原来级别写死 DEBUG —— bot.log 里混着
    #   "不接（避雷词「加班」）"这类决策细节，5 天就涨到 500 KB。改成可配，
    #   默认 INFO；排查问题或沿用旧行为就写 debug.log_level: DEBUG。
    _lvl = str(getattr(getattr(cfg, "debug", None), "log_level", "INFO") or "INFO").upper()
    log.setLevel(getattr(logging, _lvl, logging.INFO))

    # 用 pythonw 启动（没有控制台窗口）时 sys.stdout 是 None，
    # 这时候加 StreamHandler 会在写日志时炸掉，直接跳过。
    if sys.stdout is not None:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
        log.addHandler(sh)

    log_file = getattr(cfg.debug, "log_file", "")
    if log_file:
        p = Path(log_file)
        if not p.is_absolute():
            p = LOGS_DIR / p       # v2.0.0：日志进 logs/，不再落数据目录
        # ★ 2026-10-08 审查修复（M16）：换成轮转 handler。原来 FileHandler 只增不减
        #   （实测约 100 KB/天 → 36 MB/年），排查时还得忍受满屏历史告警。
        #   默认 2 MB × 3 份，config.yaml → debug.log_bytes / log_backups 可调。
        _mb = int(getattr(getattr(cfg, "debug", None), "log_bytes", 2 * 1024 * 1024)
                  or 2 * 1024 * 1024)
        _bk = int(getattr(getattr(cfg, "debug", None), "log_backups", 3) or 3)
        fh = logging.handlers.RotatingFileHandler(
            p, maxBytes=_mb, backupCount=_bk, encoding="utf-8")
        fh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
        log.addHandler(fh)
    return log


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _dir_mtime(path: Path, pattern: str = "*.md") -> float:
    """目录里匹配 ``pattern`` 的文件的最新 mtime —— 用来看 personas/ 有没有增删改。

    ★ 2026-10-10 评审修复：原来用不带过滤的 `iterdir()`，而 `personas/` 下曾躺着
    13 个 `.md.bak-*` 历史备份 —— 于是**每做一次备份都会触发一次
    「检测到 personas/ 改动，人设已重载」**（备份并不改任何生效文件，日志却在
    报"人设重载"，把真正的人设改动淹没掉）。现在只认 `.md`，与 `load_personas()`
    的 `glob("*.md")` 同一口径：能被人设加载器读到的文件，才配当热重载的触发源。
    （那些备份本身已挪到 `历史备份/personas/`，这一层过滤是防复发。）
    """
    try:
        stamps = [f.stat().st_mtime for f in path.glob(pattern) if f.is_file()]
        return max(stamps) if stamps else 0.0
    except OSError:
        return 0.0


def _window_hit(spec: str, cur: int) -> bool:
    """当天第 ``cur`` 分钟是否落在窗口里。

    ``spec`` 形如 ``"07:00-24:00"``，逗号可并列多段；左闭右开，支持跨零点
    （``"22:00-07:00"`` = 22 点到次日 7 点）。
    """
    for part in str(spec or "").split(","):
        part = part.strip()
        if "-" not in part:
            continue
        a_s, b_s = part.split("-", 1)
        try:
            ah, am = [int(x) for x in a_s.strip().split(":")]
            bh, bm = [int(x) for x in b_s.strip().split(":")]
        except Exception:
            continue
        a, b = ah * 60 + am, bh * 60 + bm
        if a <= b:
            if a <= cur < b:
                return True
        else:                                   # 跨零点
            if cur >= a or cur < b:
                return True
    return False


class Bot:
    def __init__(self, cfg=None, dry_run: bool = False,
                 reader=None, sender=None) -> None:
        """``reader`` / ``sender`` 不传就用 PC（Windows 微信）实现。

        传实现了 :class:`wxauto.drivers.Reader` / :class:`wxauto.drivers.Sender`
        的对象进来，就能把整套逻辑搬到别的平台（比如 Android 无障碍）。
        """
        self.cfg = cfg or load_config()
        self.rng = random.Random()
        self.dry_run = dry_run
        self.log = setup_logger(self.cfg)

        # ---- 平台相关部分 ----
        if reader is None:
            reader = PcReader(
                window_title=str(getattr(self.cfg.target, "window_title", "微信")),
                target_group=str(getattr(self.cfg.target, "group", "")),
                debug_dir="debug_frames",
                save_frames=bool(getattr(self.cfg.debug, "save_frames", False)),
            )
        self.reader = reader
        self.sender = sender if sender is not None else PcSender(self.reader, self.cfg, self.log)

        # ---- 以下与平台无关 ----
        self.matcher = Matcher([Rule.from_dict(d) for d in load_rules()], self.rng)
        # 多人设：personas/ 里每个 .md 是一个人设，按 behavior.persona_mode
        # 随机（默认）或轮流出场；目录为空就退回单文件 persona.md。
        self.persona_files = load_personas()
        self.brain = Brain(self.cfg, load_persona(), self.log, self.rng,
                           personas=self.persona_files)
        # ★ 2026-10-12（用户要求「插件化」）：plugins/<名字>/plugin.py —— 见 wxauto/plugins.py
        #   ⚠️ sender 用 lambda 延迟绑定：这时候 _reply 已经在了，但别把 self 存进去更省心。
        from .plugins import Plugins
        self.plugins = Plugins(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            self.log, sender=lambda t: self._reply(t, "插件发的"))
        if self.brain.personas:
            self.log.info("人设 %d 个（%s）：%s"
                          % (len(self.brain.personas), self.brain.persona_mode,
                             "、".join(n for n, _ in self.brain.personas)))
        self.governor = Governor(self.cfg)
        self.state = State()

        # ★ 2026-10-04 晚：读屏偶尔会把机器人**自己刚发的气泡**判成"别人说的"
        #   （虚拟屏上图片气泡和文字气泡挨着时会串行，实测 2026-10-04 22:13
        #   的 cw → 大大大喵 被读成 `[图片]大大大喵`，于是又回一次、再被读到、
        #   再回 —— 用户看到的就是同一句刷三遍）。这里记住自己刚发出的原话，
        #   读屏再看到就当自己发的，不触发任何规则。
        self._mine_said = deque(maxlen=32)
        self._mine_window = 60.0      # 秒：多久之内算"我自己的回声"（别人的消息）
        # ★ 2026-10-10（自激事故）：**自己账号发出去的**消息要用长得多的窗口。
        #   实测 00:26 那次：机器人 00:23 发的配文「看完记得说谢谢喵」，3 分钟后
        #   被读屏当成新消息读回来（is_self=True）→ 命中「猫猫」（它认单字"喵"）
        #   → 又发一张表情包 → 表情包+文字气泡挨着又被串读成「[图片]大大」→
        #   再命中「大大大」→ 再发 …… 群里看到的就是"机器人自己在发图/表情包/
        #   大大大喵"。60 秒挡不住：发送 → 截图 → OCR → 滚屏本来就要好几秒，
        #   中间还可能重启（重启会把 `_mine_said` 清空，见 `_restore_mine_said`）。
        try:
            self._mine_window_own = float(
                os.environ.get("WX_MINE_OWN_WINDOW") or 1800)
        except ValueError:
            self._mine_window_own = 1800.0

        # ---- 群聊流水（给控制面板显示「群里在说什么」）----
        # 2026-10-04 用户要求：想大概知道群里在聊啥 / 有没有新消息。
        # 机器人本来就每 20 秒读一次目标群，顺手把消息落盘几乎零成本；
        # 只记**它待的那个群**，不碰别的会话（也不额外切屏）。
        self.chatlog = ChatLog(ROOT / "chat_log.json",
                               chat_name=str(getattr(self.cfg.target, "group", "") or ""))
        self._chatlog_warned = False

        # ---- 安全阀 ----
        # v2.0.0：急停文件统一放 data/ —— stop.sh / botctl 面板 / adb-keepalive
        # 现在都按这个口径找，别再让"在哪个目录建 STOP.txt"各说各话。
        try:
            _sec = getattr(self.cfg, "safety", None)
            _sf = str(getattr(_sec, "stop_file", "") or "") if _sec else ""
            if _sec is not None and _sf and not os.path.isabs(_sf):
                _sec.stop_file = os.path.join(str(ROOT), _sf)
        except Exception:
            pass
        self.kill = KillSwitch(self.cfg, self.log)
        safety = getattr(self.cfg, "safety", None)
        limit = int(getattr(safety, "max_consecutive_failures", 3)) if safety else 3
        self.breaker = FailureBreaker(limit)
        # ★ 2026-10-10 评审：默认 **关**（与 config.yaml 的 verify_send 对齐）——
        #   自检的产物只有一行日志，却要花一整帧 OCR 且频繁假报；排障时再打开。
        self.verify_send = bool(getattr(safety, "verify_send", False)) if safety else False
        # ★ 2026-10-08 审查修复（F1）：发送自检不可靠（OCR 抖动/别人插话都会让它
        #   报"没看到自己刚发的"）。它现在只做告警，失败次数单独计在这里，
        #   绝不进 FailureBreaker —— 否则 3 次假失败就把机器人整体停掉（实测发生过）。
        self._verify_bad = 0
        # 2026-10-05：停止时的告别语只能发一次 —— 信号处理和 finally 两条路都会到，
        # 用这个标志去重（重复告别比不告别更糟）。

        # ---- 热重载基线 ----
        self._rules_path = ROOT / "rules.yaml"
        self._persona_path = ROOT / "persona.md"
        self._persona_dir = ROOT / "personas"
        self._rules_mtime = _mtime(self._rules_path)
        self._persona_mtime = _mtime(self._persona_path)
        self._persona_dir_mtime = _dir_mtime(self._persona_dir)

        # ---- 运行时调参（控制面板上的「读屏节奏」卡片）----
        # 2026-10-04 用户要求：能自己拧「读屏间隔（10 秒 ~ 1 小时）」和
        # 「识别到消息之后等几秒再发」，而且**不想每次重启机器人**。
        # 这两个值放在 tuning.json 里（面板写、这里每轮读），config.yaml 只当
        # 出厂默认值 —— tuning.json 里没写的项就回落到 config.yaml 的值。
        self._tuning_path = ROOT / "tuning.json"
        self._tuning_mtime = _mtime(self._tuning_path)
        self._pace_orig = tune.snapshot_pace(self.cfg)
        self._apply_tuning(quiet=True)      # 启动时先套一次（面板可能已经设过）

        self._group_warned = False
        # 连续多少轮「不是目标群」了 —— 用来决定要不要自己切回去（见 step()）
        self._off_group = 0
        # 自动切群连续失败次数 / 冷却截止时间（见 step() 里那段退避）
        self._regroup_fails = 0
        self._regroup_cool_count = 0
        self._regroup_cooldown_until = 0.0
        self._regroup_cool_logged = False

        # ---- 表情包 ----
        st = getattr(self.cfg, "stickers", None)
        self.stickers_enabled = bool(getattr(st, "enabled", False)) if st else False
        self.stickers = StickerLibrary(
            ROOT / str(getattr(st, "dir", "表情包") if st else "表情包"), self.log)
        self.sticker_cooldown = float(getattr(st, "cooldown", 90)) if st else 90.0
        self._sticker_avoid = int(getattr(st, "avoid_recent", 8)) if st else 8
        self._recent_stickers: List[str] = []
        self._last_sticker_at = 0.0

        # ---- 今日美图（2026-10-08 用户要求）----
        # 群里有人发「今日美图」→ 联网随机取一张发出去（每次换一张）。
        # 跟表情包的区别：图是**下载**来的（webp → JPEG，见 meitu.py），
        # 所以有超时、有本地图库兜底、也有自己的冷却。
        mt = getattr(self.cfg, "meitu", None)
        self.meitu_enabled = bool(getattr(mt, "enabled", False)) if mt else False
        self.meitu = MeituFetcher(mt, root=ROOT, logger=self.log)
        self.meitu_cooldown = float(getattr(mt, "cooldown", 20)) if mt else 20.0
        self._meitu_avoid = int(getattr(mt, "avoid_recent", 10)) if mt else 10
        # 网络和本地都取不到图时，回一句话（留空 = 什么都不说）
        self.meitu_empty_reply = str(getattr(mt, "empty_reply", "") or "") if mt else ""
        self._recent_meitu: List[str] = []
        self._last_meitu_at = 0.0

        # ---- 主动发美图（2026-10-08 用户要求：「群友傻傻的不会触发」）----
        # 机器人到点**自己**往群里发一张，不用等谁说话。跟零点文案一个套路：
        # 到点入队 → 主循环消费 → **真发出去才算数**；区别是多一层落盘
        # （meitu_auto.json）—— "今天这张发过了"必须扛得住重启，否则重启撞上
        # 窗口就会在群里发第二张。
        au = getattr(mt, "auto", None) if mt else None
        self.meitu_auto_enabled = bool(getattr(au, "enabled", False)) if au else False
        self._meitu_auto_times = [str(x).strip()
                                  for x in (getattr(au, "times", None) or [])
                                  if str(x).strip()]
        self._meitu_auto_window = int(getattr(au, "window_min", 20) or 20)
        self._meitu_auto_max = max(1, int(getattr(au, "max_per_day", 1) or 1))
        self._meitu_auto_captions = [str(x).strip()
                                     for x in (getattr(au, "captions", None) or [])
                                     if str(x).strip()]
        self._auto_meitu_pending: List[str] = []
        self._auto_meitu_retry_at = 0.0     # 发失败后的下次重试时间（见 run 里那段）
        self._auto_meitu_sent: Dict[str, List[str]] = self._load_auto_meitu_state()

        # ---- 深夜主动回复（2026-10-03 用户要求）----
        # 只在 pace.proactive_window（默认 00:00-07:00）里主动开口，白天照旧被动回复；
        # 主动开口之间再额外等 pace.proactive_gap（默认 180~300 秒），保守、不扰民。
        self._last_proactive_at = 0.0
        pg = list(getattr(self.cfg.pace, "proactive_gap", None) or [180, 300])
        self._proactive_gap = (float(pg[0]), float(pg[-1])) if pg else (180.0, 300.0)

        # ---- 零点定时文案：每天只发一次 ----
        self._midnight_sent_on = ""
        self._midnight_started = ""      # 今天是否已"发起过"（防同一分钟重复入队）
        self._midnight_pending: List[str] = []

        # ★ 2026-10-10：同一句**模板回复**上次发出去的时刻（配合 RULE_DEDUP_SECS）
        self._rule_replied: Dict[str, float] = {}

        # ---- 补发队列（2026-10-09 用户拍板，见文件顶部 RESEND_* 的说明）----
        # 每条是 {"text", "reason", "ts", "tries"}；消费在 run() 里，发成功才出队。
        self._resend_pending: List[Dict[str, object]] = []
        # ---- 发图补发队列（★ 2026-10-09 晚，见文件顶部 RESEND_IMAGE_* 的说明）----
        # 每条是 {"path", "reason", "kind", "rule_name", "captions", "ts", "tries"}。
        self._resend_images: List[Dict[str, object]] = []

        # ---- 群友发的 PPT（吃瓜素材，2026-10-07 用户要求）----
        # 机器人的「眼睛」只有 OCR，看不到文件卡片里的内容；这里补上后半段：
        # 用户在微信里点开 PPT → 文件落到微信落点 → 取走抽文字 → 按人设点评。
        # 落点、解析、去重细节全在 wxauto/ppt.py。
        self._docs = None            # DocWatcher，用到才建
        self._doc_scan_at = 0.0      # 上次扫落点的时间（扫描节流，见 _check_group_doc）
        self._doc_cool_until = 0.0   # 两份点评之间的冷却
        self._doc_count = 0          # 今天点评了几份
        self._doc_day = ""           # 上面那个计数的日期

        # ---- 群友发的照片（识图，2026-10-10 用户要求）----
        # 「群里发照片他可以识别，然后能点评的他点评」。读屏只能给出一个
        # `[图片]` 字符串，图的内容要另外走多模态（brain.comment_on_image）；
        # 桥接在同一帧把图裁好（wxbridge._crop_message_image），这里只管节流+发。
        self._img_cool_until = 0.0   # 两张点评之间的冷却（用户定：30 秒一张）
        self._img_count = 0          # 今天点评了几张
        self._img_day = ""           # 上面那个计数的日期
        # 发送者 -> 上次点评他发图的时间。★ 防"同一张图被滚动后重复点评"：
        # 图片在屏幕上被顶上去以后，裁出来的内容变了、指纹也就变了，state 那道
        # 去重挡不住 —— 靠这个冷却兜（见 config.yaml 的 image.sender_cooldown_secs）。
        self._img_sender_at: Dict[str, float] = {}
        # 图片专用的去重账本（**跟文字那本分开**）：文字走过去那本 state 时，
        # 一条图片消息可能因为同轮先回了别人的话而"这一轮不点评"，那本账却已经
        # 把它记成"见过"了 —— 那张图就再也不会被点评。所以图片自己记一本。
        self._img_seen = State()
        # 机器人自己上一次**发出图片**的时间（见 _send_sticker / _send_meitu）
        self._img_self_sent_at = 0.0

    # ------------------------------------------------------------ 热重载
    def _apply_tuning(self, quiet: bool = False) -> dict:
        """把 tuning.json 套到 ``cfg.pace`` 上，返回实际变化的项。

        ``quiet=True`` 时"没有变化"不写日志（启动时用，免得每次启动多一行）。
        坏文件 / 越界值一律吞掉并用旧节奏 —— 这个函数绝不能把主循环搞挂。
        """
        try:
            applied = tune.apply_to_cfg(self.cfg, tune.load_tuning(self._tuning_path),
                                        self._pace_orig)
        except Exception as exc:
            self.log.error(f"应用 tuning.json 失败（继续用旧节奏）：{exc}")
            return {}
        if "reply_delay" in applied:
            # human_delay() 读的是调速器自己的副本，得同步过去，否则只改了配置不生效
            self.governor.reply_delay = applied["reply_delay"]
        if applied:
            if quiet:
                # ★ 2026-10-10 评审修复（§4.1）：**启动时套用不算"检测到改动"**。
                #   config.yaml 与 tuning.json 的差异是固定的（yaml 写 [3,4]、
                #   tuning 是 1.0/2.0），每次启动都会命中 —— 原来那句「检测到调参
                #   改动…已生效」看起来像"有人刚改过"，把"yaml 里写的值其实不生效"
                #   这件事瞒了很久。现在启动说清是"谁优先"，运行中改了才叫"检测到"。
                self.log.info("运行时调参已套用（tuning.json 优先于 config.yaml）：%s"
                              % tune.summarize(applied))
            else:
                self.log.info("检测到调参改动（tuning.json），已生效：%s"
                              % tune.summarize(applied))
        elif not quiet:
            self.log.info("tuning.json 有改动，但节奏值和当前一致（无变化）")
        return applied

    def reload_if_changed(self) -> None:
        """rules.yaml / persona.md / tuning.json 一变就重载，不用重启。"""
        if not bool(getattr(self.cfg.debug, "hot_reload", True)):
            return

        # ★ 2026-10-12：插件也热重载（plugins/ 下任何文件 mtime 变了就重载）
        try:
            self.plugins.load_if_changed()
        except Exception as exc:
            self.log.warning("插件重载出错：%r" % (exc,))

        m = _mtime(self._rules_path)
        if m and m != self._rules_mtime:
            try:
                rules = [Rule.from_dict(d) for d in load_rules()]
                self.matcher = Matcher(rules, self.rng)
            except Exception as exc:
                # ★ 2026-10-08 审查修复（M3）：原来这里是**先推进基线再重载**，
                #   一旦某次读到半截文件（rule_tool.py 是截断式写）或坏 YAML，
                #   基线已经前进，于是这次改动**永远不会再生效**，而且只报一句
                #   "继续用旧的" —— 用户看到的就是"改了规则没用"。
                #   现在失败不推进基线，下一轮重试（写入侧也已改成原子写）。
                self.log.error(f"重载 rules.yaml 失败（下轮重试，继续用旧的）：{exc}")
            else:
                self._rules_mtime = m
                self.log.info(f"检测到 rules.yaml 改动，已重载 {len(rules)} 条关键词规则")

        m = _mtime(self._persona_path)
        if m and m != self._persona_mtime:
            try:
                self.brain.persona = load_persona().strip()
            except Exception as exc:
                self.log.error(f"重载 persona.md 失败（下轮重试，继续用旧的）：{exc}")
            else:
                self._persona_mtime = m
                self.brain._system = None          # 清掉缓存的 system prompt
                self.log.info("检测到 persona.md 改动，人设已重载")

        # personas/ 目录：任何一个 .md 增删改都重扫一遍
        m = _dir_mtime(self._persona_dir)
        if m and m != self._persona_dir_mtime:
            try:
                self.brain.set_personas(load_personas())
            except Exception as exc:
                self.log.error(f"重载 personas/ 失败（下轮重试，继续用旧的）：{exc}")
            else:
                self._persona_dir_mtime = m
                self.log.info("检测到 personas/ 改动，人设已重载：%s"
                              % "、".join(n for n, _ in self.brain.personas))

        # 运行时调参（面板上的「读屏节奏」）：文件一变就套用，**不用重启机器人**。
        # 删掉 tuning.json 时 mtime 变回 0.0，同样会触发 —— 于是自动回落到
        # config.yaml 的值（_apply_tuning 里用 _pace_orig 兜底）。
        m = _mtime(self._tuning_path)
        if m != self._tuning_mtime:
            self._tuning_mtime = m
            self._apply_tuning()

    # ------------------------------------------------------------ 发送自检
    def _verify_sent(self, text: str) -> bool:
        """发完重读一次，确认屏幕上最后一条自己发的消息就是刚才那句。

        ⚠️ 2026-10-10 评审后默认**不调用**（`safety.verify_send: false`）：
        结果只进日志（`_verify_bad` 没有任何消费者），代价是 0.6s + 一整帧 OCR，
        而 OCR 抖动会让它频繁假报。要排障时把配置打开即可 —— 这条路径本身
        没有删掉。
        """
        time.sleep(0.6)
        snap = self.reader.read()
        if snap is None:
            self.log.warning("发送自检：读屏失败，这次按下成功处理")
            return True
        if not self.reader.in_target_group(snap):
            return True

        mine = [m for m in snap.messages if m.is_self]
        if not mine:
            self.log.warning("发送自检：屏幕上没看到自己刚发的消息，可能没发出去")
            return False

        got = normalize(mine[-1].text)
        want = normalize(text)
        if not want:
            return True
        if want in got or got in want:
            return True
        # ★ 2026-10-08：OCR 经常把标点读丢/读错 —— 11:43 那条「今天的骚度已超标，
        #   本喵要报警了喵」就是被读成没逗号的版本，判成"发送失败"。这种假失败会把
        #   连续失败计数推到急停（当天 14:41 就是这么停的）。所以去掉标点再比一次。
        #   注意这条**不会**放过"框里是别人文字"的情况：那种连汉字都对不上。
        g2 = re.sub(r"[\s\W_]+", "", got)
        w2 = re.sub(r"[\s\W_]+", "", want)
        if w2 and (w2 in g2 or g2 in w2):
            return True

        self.log.warning(f"发送自检不通过：屏幕上最后一条是 {mine[-1].text!r}，"
                         f"期望 {text!r}")
        return False

    # ------------------------------------------------------------ 回复动作
    # ★ 2026-10-09（动作描写）：比对回声时先把标点括号抹掉 —— 机器人现在会发
    #   「本喵才不稀罕喵（甩了甩尾巴）」，而 OCR 读回来常把括号读丢 / 读成别的
    #   符号（见 SKILL 续7 的形近字问题）。只比汉字的话，自己的话被当成群友的话、
    #   再反过来触发一遍规则的概率就小得多。判据本身没放宽：仍然要求 ≥4 字且互相包含。
    _ECHO_STRIP = re.compile(
        r"[\s（）()\[\]【】「」『』,，。.、!！?？~～…—\-_:：;；'\"“”‘’]+")
    # 读屏把「图片气泡 + 紧挨着的文字气泡」串成一条时的前缀（2026-10-10）
    _HAN_ONLY = re.compile(r"[^\u4e00-\u9fff]+")
    _IMG_ECHO_PREFIX = re.compile(r"^(?:\[图片\]|\[动画表情\]|\[视频\])+")

    def _restore_mine_said(self) -> None:
        """重启后从发送流水（sent.jsonl）恢复"我刚说过什么"。

        ★ 2026-10-10（自激事故）：`_mine_said` 只在内存里，**一重启就清空**，
        而屏幕上还留着重启前自己发的带「喵」的回复 —— 读屏一读回来就命中
        「猫猫」发表情包，接着表情包和文字气泡串行又被读成「[图片]大大」……
        每条真发出去的话本来就落一份流水，直接拿来打底，重启不再失忆。
        """
        path = ROOT / "sent.jsonl"
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[-40:]
        except OSError:
            return
        n = 0
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            try:
                ts = float(rec.get("ts") or 0)
            except (TypeError, ValueError):
                continue
            txt = normalize(rec.get("text") or "")
            if txt and ts:
                self._mine_said.append((ts, txt))
                n += 1
        if n:
            self.log.info(f"已从发送流水恢复 {n} 条自己发过的话（重启后防自激）")

    def _is_my_echo(self, text: str, window: float = 0.0) -> bool:
        """这句话是不是我刚发出去的？

        兜底读屏的 self 误判：**短的（<4 字）只认全等**，否则机器人回一个「？」
        之后，60 秒内群里任何带问号的话都会被误当成自己的回声吞掉。

        ★ 2026-10-10：`window` 可以调大 —— **自己账号**发出来的消息用
        `_mine_window_own`（默认 30 分钟），因为它们被读回来可能隔好几分钟。
        """
        t = normalize(text)
        if not t:
            return False
        # 图片气泡和自己的文字气泡挨着时 OCR 会串成「[图片]大大」—— 先把
        # «[图片]» 这段剥掉再比（2026-10-04 就踩过，2026-10-10 又复发）。
        img_prefix = t.startswith("[图片]")
        t = self._IMG_ECHO_PREFIX.sub("", t)
        t2 = self._ECHO_STRIP.sub("", t)
        win = self._mine_window if window <= 0 else float(window)
        now = time.time()
        for ts, mine in list(self._mine_said):
            if now - ts > win or not mine:
                continue
            if mine == t:
                return True
            # 窗口内我发过图/表情包，而这句又是「[图片]…」串出来的 → 是我的回音
            if img_prefix and mine == normalize("[图片]"):
                return True
            if len(mine) >= 4 and (mine in t or t in mine):
                return True
            m2 = self._ECHO_STRIP.sub("", mine)
            if len(m2) >= 4 and t2 and (m2 == t2 or m2 in t2 or t2 in m2):
                return True
            # ★ 2026-10-13（用户报「机器人重复发消息」）：自己发的长句在气泡里
            #   换行，OCR 两行的顺序读反了（「本喵还没见过喵巨大化双星河？新出的吗」），
            #   等于/包含都对不上 → 被当成主人说的话，命中「猫猫」又发了张表情包。
            #   汉字字袋（不看顺序）重合 ≥0.9 就是同一句。门槛同 state._bag_same。
            h1 = self._HAN_ONLY.sub("", mine)
            h2 = self._HAN_ONLY.sub("", t)
            if (len(h1) >= 10 and len(h2) >= 10 and abs(len(h1) - len(h2)) <= 2
                    and sum((Counter(h1) & Counter(h2)).values())
                    / max(len(h1), len(h2)) >= 0.9):
                return True
        return False

    def _reply(self, text: str, reason: str, rule_name: Optional[str] = None,
               proactive: bool = False, no_meow: bool = False,
               mention: bool = False, max_chars: int = 0) -> bool:
        """``mention=True`` 表示这是"别人 @ 我"的回话 —— 交给节流层豁免
        时间闸门（见 Governor.check 的说明）。"""
        # 0) 急停优先，任何情况下都先看这个
        stop = self.kill.check()
        if stop:
            self.log.warning(f"急停生效中（{stop}），不发送")
            return False

        # 1) 发送层统一追加「喵」（模板回复和模型回复走同一条路，双人设共用）。
        #    纯符号 / 纯数字 / 已带喵 / 加了超长 —— 这四种情况不加。
        # max_chars > 0 时覆盖默认上限（吃瓜点评单独一档，见 config 的
        # ppt.reply_max_chars —— 素材信息量大，说太短群友看不懂）
        limit = int(max_chars or getattr(self.cfg.behavior, "max_reply_chars", 40))
        # ★ 2026-10-10：**先剥掉括号动作**（用户要求删掉，见 safety.strip_action）。
        #   人设是软约束，这里才是硬保证 —— 不问哪套人设、统一剥。
        text = strip_action(text)
        if not text:
            self.log.info(f"剥掉括号动作后是空的 → 不发   ← {reason}")
            return False
        text = with_meow(text, limit, no_meow=no_meow or not self._persona_meows())

        # ★★ 2026-10-10 用户报「群友只发了一个，他自己回了两个」：同一句**模板**
        #   回复短时间内不重复发（见文件顶部 RULE_DEDUP_SECS 的说明）。
        #   ⚠️ 只对模板（rule_name 非空）生效，模型回复不碰。
        if rule_name:
            _now = time.time()
            _last = self._rule_replied.get(text)
            if _last and (_now - _last) < RULE_DEDUP_SECS:
                self.log.info(f"跳过（这一句模板 {_now - _last:.0f}s 前刚发过）："
                              f"{text!r} ← {reason}")
                return False
            self._rule_replied[text] = _now
            if len(self._rule_replied) > 200:      # 顺手清过期的，别让它无限长
                _cut = _now - RULE_DEDUP_SECS
                self._rule_replied = {k: v for k, v in self._rule_replied.items()
                                      if v >= _cut}

        # 2) 出站校验：像乱码就不发
        bad = validate_outgoing(text, limit)
        if bad:
            self.log.warning(f"拦下一条可疑回复（{bad}）：{text!r}  ← {reason}")
            return False

        # 3) 节流
        allow, why = self.governor.check(mention=mention)
        if not allow:
            self.log.info(f"跳过（{why}） ← {reason}")
            return False

        if self.dry_run:
            self.log.info(f"[演练] 本该发送：{text}   （{reason}）")
            self.governor.record()
            return True

        # ---- 这一条要真发了：**先记账，再发** ----
        # ★ 2026-10-08 审查修复（F1）：原来这些记账全在"自检通过"之后，而自检
        #   （_verify_sent）本身不可靠 —— 消息明明进了群，只要那 0.6 秒后重读的一帧
        #   没认出自己那句话，就判失败，于是：配额/规则冷却/回声防护全都没记（同一句
        #   3 秒后又能再发一次），假失败累计 3 次还会触发熔断把机器人整体停掉
        #   （2026-10-08 14:41 实测发生过，停到 15:36 才人工重启）。
        #   现在口径与桥接层一致：**只要真的调了发送接口，就当作"已经开口"记账**；
        #   自检降级成只记日志的旁路信号，由 _verify_bad 单独计数，不再驱动熔断。
        _origin = ("rule" if rule_name else
                   "mention" if mention else
                   "proactive" if proactive else "reply")
        self.governor.record()                      # 配额：按"要发"记，不按"自检认可"记
        if rule_name:
            self.governor.record_rule(rule_name)    # 规则冷却同上
        # ★ 2026-10-10 评审修复（§5.2）：防复读台账 `_mine_said` 挪到**发送成功
        #   之后**再记（见下面 `if ok:` 分支）—— 原来在发送前记，于是"根本没发出去"
        #   的话也被记成"我说过"，60 秒内群里别人说同一句会被 `_is_my_echo` 当成
        #   自己的回声**吞掉**。同类问题（配额）2026-10-08 已修过一次（F1），
        #   台账这一笔当时没复核。
        #   ⚠️ 配额（governor.record）与规则冷却**必须留在发送前** —— F1 的结论是
        #      "自检假失败不能让配额不记，否则同一句 3 秒后又能再发一次"。
        if proactive:
            self._last_proactive_at = time.time()
        # ★ 2026-10-05 用户要求：「发出去的也能记住对吧？别光记不发的」——
        #   每一条真发进群的话都落一份流水（sent.jsonl），标清来源。
        drafts.note_sent(text, origin=_origin, src=reason)

        delay = self.governor.human_delay(self.rng)
        self.log.info(f"等 {delay:.1f}s 后发送「{text}」（{reason}）")
        time.sleep(delay)

        # 拟人延迟期间你可能按了急停，也可能切走了群
        stop = self.kill.check()
        if stop:
            self.log.warning(f"发送前急停生效（{stop}），取消本次发送")
            return False
        if not self.sender.is_ready():
            self.log.info("等待期间已离开目标群，取消本次发送")
            return False

        ok = self.sender.send(text)

        # 4) 发送自检 —— 只告警，不驱动熔断（见上面 F1 的说明）
        if ok and self.verify_send and not self._verify_sent(text):
            self._verify_bad += 1
            self.log.warning(
                f"发送自检未通过（已按发出去记账，只告警；累计 {self._verify_bad} 次）"
                f"：期望 {text!r}")

        if not ok:
            # ★ 2026-10-10：**占用类失败不计熔断**。用户打字时桥接不抢键盘
            #   （wxbridge.py::preflight_yield 的打字保护）→ 这条返回失败，
            #   而且带 retryable=True。他连打几条就能攒出 3 次失败，老代码会把
            #   机器人自己停掉（「连续 3 次发送异常，已自动停止」）。这种失败
            #   只是攒着补发，不驱动熔断 —— 跟发图那条路一个口径。
            _occ = getattr(self.sender, "last_send_retryable", False) is True
            _err = str(getattr(self.sender, "last_send_error", "") or "")
            self.log.warning(
                "发送接口报错（消息可能没进群）：%s%s"
                % (_err or "原因未回", "（你占用着手机，稍后补发、不计熔断）" if _occ else ""))
            # ★ 2026-10-09 用户拍板：**挂进补发队列**，别让这条永远丢了。
            #   为什么不必担心重复发：桥接对「点发送」有全局只执行一次的保证
            #   （wxbridge.py::send_text —— 输入可以重试，点发送只点一次），
            #   它返回 False 的语义就是"**没有点发送键**"（抢不到焦点 / 字没进框 /
            #   adb 断），所以重发是安全的；而漏发的代价是这条彻底消失。
            #   ⚠️ 仍然守着那条铁律 —— 宁可漏、不可重：补发重发的是**同一句原文**，
            #   不是重新生成一句（重新生成会让"两个版本都进了群"变成可能）。
            # ★ 2026-10-12：桥接超时 / 断连 = **不确定**发送键点没点过（消息可能已进群）。
            #   上面「False = 没点发送键」只对桥接**明确回 False** 成立；不确定的这种
            #   补发就是在群里把同一句再说一遍——宁可漏，不可重。
            #   台账照记：这句多半已经进群，别让它的回声被当成别人说的话再触发回复。
            if getattr(self.sender, "last_send_uncertain", False) is True:
                self.log.warning("这条发送结果不确定（桥接超时/断连），按「可能已发出」处理，"
                                 "不补发：%r" % (text,))
                self._mine_said.append((time.time(), normalize(text)))
            else:
                self._queue_resend(text, reason)
            if _occ:
                pass                    # 你占用手机 → 不计熔断（见上面的说明）
            elif self.breaker.record_failure():
                self.kill.trigger(
                    f"连续 {self.breaker.streak} 次发送异常，已自动停止（防止刷屏）")
            return False

        # ★ 2026-10-10 评审：真发出去了才记"我说过这句话"（见上面的说明）
        self._mine_said.append((time.time(), normalize(text)))
        self.breaker.record_success()
        return True

    # ------------------------------------------------------------ 补发
    # 2026-10-09 用户拍板：「发送失败就永久丢」修成真重试队列。
    # 起因是用户问「下一轮是什么样？怎么判定的」—— 一查发现 `step()` 里
    # `state.mark(m)` 在 `handle(m)` **之前**执行，所以发送失败的那条消息指纹早就
    # 记进"见过"名单，下一轮读屏时 `is_new()` 直接返回 False，压根不会再走到发送；
    # 浮窗上那句"下一轮会再试这条，你不用管"从来没兑现过（bot.log 里 11 次
    # 「发送接口报错」= 11 条回复石沉大海）。
    def _hud_note(self, title: str, detail: str = "") -> None:
        """往浮窗写一条提示（尽力而为：没有 hud、超时、报错都不影响任何流程）。

        为什么机器人这一侧也要能写浮窗：发送失败的提示一直是**桥接**写的
        （`wxbridge.py::send_text` 失败时写「这条没发出去 · 下一轮会自动重试」），
        但"**最终放弃**这条"发生在机器人这边 —— 原来这里只写日志，于是用户在浮窗上
        永远停在那句"会自动重试"上（2026-10-09 13:20 他截图看到的正是这个：那条
        其实补发两次都没成、早就放弃了）。
        """
        try:
            exe = shutil.which("hud")
            if not exe:
                return
            subprocess.run([exe, "-t", title, "-d", detail, title],
                           timeout=6, capture_output=True)
        except Exception:
            pass

    # ---- 自动切群的失败退避（2026-10-10，见文件顶部 REGROUP_FAIL_MAX 的说明）----
    def _regroup_gate(self, now=None):
        """现在允许自动切群吗？返回 ``(allow, 剩余秒数)``。

        连续失败到上限后进入冷却。冷却期间**一次 `/open` 都不调** —— `/open`
        会抢物理屏输入焦点，那正是「用户没法打字」的来源。
        """
        now = time.time() if now is None else now
        left = float(getattr(self, "_regroup_cooldown_until", 0.0) or 0.0) - now
        if left > 0:
            return False, left
        return True, 0.0

    def _regroup_note_result(self, ok):
        """记一次自动切群的结果；连续失败到上限就进冷却 + 浮窗提醒一次。

        冷却时长**逐次翻倍**（600s → 1200s → … 封顶 3600s）：切群真坏掉时
        （比如群被解散、微信没登录）没必要每 10 分钟抢一次用户的键盘。
        """
        if ok:
            self._regroup_fails = 0
            self._regroup_cool_count = 0
            self._regroup_cooldown_until = 0.0
            self._regroup_cool_logged = False
            return
        self._regroup_fails = int(getattr(self, "_regroup_fails", 0) or 0) + 1
        if self._regroup_fails < REGROUP_FAIL_MAX:
            return
        self._regroup_cool_count = int(getattr(self, "_regroup_cool_count", 0) or 0) + 1
        cool = min(REGROUP_COOLDOWN * self._regroup_cool_count, REGROUP_COOLDOWN_MAX)
        self._regroup_cooldown_until = time.time() + cool
        self._regroup_cool_logged = False
        self._hud_note("⚠️ 切不回目标群了",
                       "连试 %d 次都没成：先不抢你的键盘，%d 分钟后再试"
                       % (self._regroup_fails, max(1, int(cool // 60))))
        self.log.warning("自动切群连续失败 %d 次 → 冷却 %d 秒（期间不抢屏、不切群）"
                         % (self._regroup_fails, int(cool)))

    def _queue_resend(self, text: str, reason: str) -> None:
        """把一条没发出去的话挂进补发队列。

        只在 `_reply` 里"桥接明确说没发出去"那条分支调用 —— 桥接的语义是
        **没点发送键**（点发送全局只执行一次），所以重发不会造成重复发言。
        """
        try:
            if not text:
                return
            for it in self._resend_pending:      # 同一句已经在队列里就别重复挂
                if it.get("text") == text:
                    return
            self._resend_pending.append({"text": text, "reason": reason,
                                         "ts": time.time(), "tries": 0})
            # 上限：堆太多说明一直发不出去（多半是你占着手机），丢最早的，
            # 免得十分钟后一口气把攒的话全倒进群里。
            while len(self._resend_pending) > max(1, RESEND_MAX):
                dropped = self._resend_pending.pop(0)
                self.log.warning(f"补发队列满了，丢掉最早那条：{dropped.get('text')!r}")
            self.log.info(f"已挂进补发队列（{len(self._resend_pending)} 条，"
                          f"{RESEND_TTL:.0f} 秒内有效）：{text!r}")
        except Exception as exc:
            self.log.warning(f"挂补发队列出错（不影响主流程）：{exc!r}")

    def _resend_one(self, text: str, reason: str) -> bool:
        """补发一条 —— **只做发送，不再记账**。

        为什么不复用 `_reply`：`_reply` 会重新 `governor.record()` +
        `drafts.note_sent()` + `with_meow()`，而这些它在**第一次尝试时就已经
        做过了**（记账写在发送之前）。再走一遍的后果是：一条话吃两份配额、
        sent.jsonl 里出现两条、末尾再多一个「喵」。
        熔断也不在这里计 —— 第一次失败时已经记过一次，同一条消息重试两次就
        触发急停是不对的（急停是防刷屏，不是防重试）。
        """
        try:
            if self.kill.check():
                return False
            if not self.sender.is_ready():
                return False
        except Exception:
            return False
        try:
            ok = self.sender.send(text)
        except Exception as exc:
            self.log.warning(f"补发异常：{exc!r}")
            return False
        if not ok:
            self.log.info(f"补发还是没发出去（{reason}）→ 下一轮再试")
        return bool(ok)

    def _resend_tick(self) -> None:
        """主循环每轮调一次：把队列里最该补的那条试着补上。

        ⚠️ **成败都继续读屏**（调用方不要 `continue`）：补发失败往往是"你还在用
        手机"，死磕它会让群里新消息都读不到 —— 那是更大的损失。所以这里只试
        一条（队首），不管结果如何都交还给正常流程。
        """
        if not self._resend_pending:
            # ★ 2026-10-09 晚：文字补发优先；没有文字要补时才轮到"发图补发"
            #   （发一张图十几到四十秒，同一轮里两样都做会把读屏拖太久）。
            self._resend_image_tick()
            return
        now = time.time()
        # 先清过期的：话是有时效的，三分钟前那句突然冒出来比不发更奇怪
        while self._resend_pending:
            it = self._resend_pending[0]
            try:
                age = now - float(it.get("ts") or 0)
            except Exception:
                age = RESEND_TTL + 1
            if age > RESEND_TTL:
                self._resend_pending.pop(0)
                self.log.info(f"补发过期丢弃（已经 {age:.0f} 秒了，"
                              f"上限 {RESEND_TTL:.0f}）：{it.get('text')!r}")
                continue
            break
        if not self._resend_pending:
            return
        it = self._resend_pending[0]
        try:
            tries = int(it.get("tries") or 0)
        except Exception:
            tries = 0
        if tries >= max(1, RESEND_MAX_TRIES):
            self._resend_pending.pop(0)
            self.log.warning(f"补发试了 {tries} 次都没成，放弃这条：{it.get('text')!r}")
            # 浮窗上那句「下一轮会自动重试」是桥接写的 —— 到这里必须把它改掉，
            # 否则用户永远以为还会重试（2026-10-09 13:20 的浮窗截图就是这情况）。
            try:
                self._hud_note("微信机器人 · 这条最终没发出去",
                               "试了 %d 次都没成，放弃：%s"
                               % (tries, str(it.get("text") or "")[:16]))
            except Exception:
                pass          # 浮窗只是提示，任何情况下都不许影响补发/主流程
            return
        it["tries"] = tries + 1
        text = str(it.get("text") or "")
        if self._resend_one(text, str(it.get("reason") or "")):
            self._resend_pending.pop(0)
            # 浮窗不用在这里补：补发走的是 `sender.send()` → 桥接 `/send`，
            # 它成功后本来就会 `_hud_sent()` 弹一条「已发出」（wxbridge.py）。
            self.log.info(f"✅ 补发成功（第 {tries + 1} 次尝试）：{text!r}")
        elif getattr(self.sender, "last_send_uncertain", False) is True:
            # ★ 2026-10-12：补发这一下桥接超时 / 断连，消息可能已进群 —— 出队，别补第二次
            self._resend_pending.pop(0)
            self._mine_said.append((time.time(), normalize(text)))
            self.log.warning(f"补发结果不确定（桥接超时/断连），按「可能已发出」处理，"
                             f"不再补：{text!r}")

    # ------------------------------------------------------ 发图补发（★ 2026-10-09 晚）
    def _queue_resend_image(self, path, reason: str, kind: str = "sticker",
                            rule_name: Optional[str] = None,
                            captions: Optional[List[str]] = None) -> None:
        """把一张没发出去的图挂进发图补发队列（与文字补发队列并列）。

        只在桥接说 ``retryable=True`` 时调用 —— 那表示**发送键根本没点过**
        （你占用着手机 / 列表里没找到会话行 / 预览页对不上），补发不会重复发图。
        """
        try:
            p = str(path or "")
            if not p:
                return
            for it in self._resend_images:      # 同一张已经在队列里就别重复挂
                if it.get("path") == p:
                    return
            self._resend_images.append({"path": p, "reason": reason, "kind": kind,
                                        "rule_name": rule_name,
                                        "captions": list(captions or []),
                                        "ts": time.time(), "tries": 0})
            while len(self._resend_images) > max(1, RESEND_IMAGE_MAX):
                dropped = self._resend_images.pop(0)
                self.log.warning("发图补发队列满了，丢掉最早那张：%s"
                                 % os.path.basename(str(dropped.get("path") or "")))
            self.log.info("已挂进发图补发队列（%d 张，%.0f 秒内有效）：%s"
                          % (len(self._resend_images), RESEND_IMAGE_TTL,
                             os.path.basename(p)))
        except Exception as exc:
            self.log.warning(f"挂发图补发队列出错（不影响主流程）：{exc!r}")

    def _resend_image_tick(self) -> None:
        """主循环每轮调一次：把队首那张没发出去的图补上。

        ⚠️ 跟文字补发同一条铁律：**成败都继续读屏**（调用方不要 continue）。发图很慢
        （十几到四十秒），而且失败往往还是"你还在用手机" —— 死磕它群里新消息就都读不到了。
        所以每轮最多试一张。
        """
        q = self._resend_images
        if not q:
            return
        now = time.time()
        while q:
            it = q[0]
            try:
                age = now - float(it.get("ts") or 0)
            except Exception:
                age = RESEND_IMAGE_TTL + 1
            if age > RESEND_IMAGE_TTL:
                q.pop(0)
                self.log.info("发图补发过期丢弃（已经 %.0f 秒了，上限 %.0f）：%s"
                              % (age, RESEND_IMAGE_TTL,
                                 os.path.basename(str(it.get("path") or ""))))
                continue
            break
        if not q:
            return
        it = q[0]
        try:
            tries = int(it.get("tries") or 0)
        except Exception:
            tries = 0
        name = os.path.basename(str(it.get("path") or ""))
        if tries >= max(1, RESEND_IMAGE_MAX_TRIES):
            q.pop(0)
            self.log.warning("发图补发试了 %d 次都没成，放弃这张：%s" % (tries, name))
            try:
                self._hud_note("微信机器人 · 这张图最终没发出去",
                               "试了 %d 次都没成，放弃：%s" % (tries, name))
            except Exception:
                pass
            return
        try:
            if self.kill.check():
                return
            if not self.sender.is_ready():
                return
        except Exception:
            return
        it["tries"] = tries + 1
        path = str(it.get("path") or "")
        try:
            ok = bool(self.sender.send_image(path))
        except Exception as exc:
            self.log.warning(f"发图补发异常：{exc!r}")
            return
        if not ok:
            err = str(getattr(self.sender, "last_image_error", "") or "环境忙")
            self.log.info("发图补发还是没发出去（%s）→ 下一轮再试" % err)
            return
        q.pop(0)
        self.log.info("✅ 发图补发成功（第 %d 次尝试）：%s" % (tries + 1, name))
        self._after_image_resent(it)

    def _after_image_resent(self, it: Dict[str, object]) -> None:
        """发图补发成功后的记账 —— 照抄 `_send_sticker` / `_send_meitu` 成功分支那几样。

        为什么补发要在这儿补记账：发图的记账**只写在成功分支里**（跟文字不一样，
        文字是发送前就记账了），所以第一次失败时什么都没记。
        """
        try:
            kind = str(it.get("kind") or "sticker")
            name = os.path.basename(str(it.get("path") or ""))
            # ★ 先记最关键的这两样：读屏层把自己发的图判成"别人发的"，机器人就会
            #   对着自己的图再回一次（`_mine_said` 存在的全部意义）。
            self._mine_said.append((time.time(), normalize("[图片]")))
            self._img_self_sent_at = time.time()
            # 配额/熔断是次要的：出错也不该把上面那两样一起带走
            try:
                self.breaker.record_success()
                self.governor.record()
                rn = str(it.get("rule_name") or "")
                if rn:
                    self.governor.record_rule(rn)
            except Exception as exc:
                self.log.warning(f"发图补发后的配额记账出错（不影响）：{exc!r}")
            if kind == "meitu":
                self._last_meitu_at = time.time()
                self._recent_meitu.append(name)
                if len(self._recent_meitu) > self._meitu_avoid:
                    self._recent_meitu.pop(0)
                caps = [str(x) for x in (it.get("captions") or []) if str(x).strip()]
                if caps:
                    # ★ 先图后字：图已经发出去了，配文是附赠（发不成也不影响）
                    self._send_caption_after_image(caps, str(it.get("reason") or "补发"))
            else:
                self._last_sticker_at = time.time()
                self._recent_stickers.append(name)
                if len(self._recent_stickers) > self._sticker_avoid:
                    self._recent_stickers.pop(0)
        except Exception as exc:
            self.log.warning(f"发图补发后的记账出错（不影响主流程）：{exc!r}")

    # ------------------------------------------------------------ 发表情包
    def _send_sticker(self, tag: str, reason: str,
                      rule_name: Optional[str] = None,
                      mention: bool = False) -> bool:
        """发一张表情包。安全检查跟发文字共用同一套。

        和发文字的区别：**不发完重读校验**。校验器是按文本比对设计的，
        图片消息本来就没文字可比。这里靠 sender 内部的多道检查兜底
        （图能不能读、窗口能不能激活、在不在目标群）。
        """
        stop = self.kill.check()
        if stop:
            self.log.warning(f"急停生效中（{stop}），不发图")
            return False

        if not self.stickers_enabled:
            self.log.info(f"表情包功能关着（config 里 stickers.enabled）← {reason}")
            return False

        if not hasattr(self.sender, "send_image"):
            self.log.warning("当前平台不支持发图（sender 没有 send_image），跳过")
            return False

        # 全局限速：发图也算一次"发言"，不能绕过 per_minute / per_hour
        allow, why = self.governor.check(mention=mention)
        if not allow:
            self.log.info(f"跳过（{why}） ← {reason}")
            return False

        # 表情包自己的冷却：防止同一类图连着刷
        gap = time.time() - self._last_sticker_at
        if self._last_sticker_at and gap < self.sticker_cooldown:
            self.log.info(f"跳过（表情包冷却中，还要 {self.sticker_cooldown - gap:.0f}s）"
                          f" ← {reason}")
            return False

        path = self.stickers.pick(tag, avoid=self._recent_stickers)
        if path is None:
            self.log.warning(f"表情包里没图（找的是「{tag or '根目录'}」），跳过 ← {reason}")
            return False

        if self.dry_run:
            self.log.info(f"[演练] 本该发这张表情包：{path.name}"
                          f"（标签「{tag or '根目录'}」）  ← {reason}")
            self.governor.record()
            return True

        delay = self.governor.human_delay(self.rng)
        self.log.info(f"等 {delay:.1f}s 后发表情包「{path.name}」（{reason}）")
        time.sleep(delay)

        stop = self.kill.check()
        if stop:
            self.log.warning(f"发图前急停生效（{stop}），取消")
            return False
        if not self.sender.is_ready():
            self.log.info("等待期间已离开目标群，取消本次发图")
            return False

        ok = bool(self.sender.send_image(path))

        if ok:
            self.breaker.record_success()
            self.governor.record()
            if rule_name:
                self.governor.record_rule(rule_name)
            self._last_sticker_at = time.time()
            self._recent_stickers.append(path.name)
            if len(self._recent_stickers) > self._sticker_avoid:
                self._recent_stickers.pop(0)
            # ★ 2026-10-08 审查修复（M5）：表情包也要进"我说过什么"的记录 ——
            #   零点/表情包原来都不记，读屏一旦把自己发的图判成别人发的，
            #   就会对着自己的话再回一次（_mine_said 存在的全部意义）。
            #   记成 [图片]，与读屏层对图片消息的表示一致（reader 侧也是这个串）。
            self._mine_said.append((time.time(), normalize("[图片]")))
            # ★ 2026-10-10（识图）：刚发出去的这张图**不能被识图那条路点评**。
            #   机器人和主人共用一个微信号，读屏偶尔会把自己的气泡判成"别人发的"
            #   （图片气泡和文字气泡挨着时就会串行，2026-10-04 踩过）。文本有
            #   `_mine_said` 兜着，图片没有可比的文本 —— 用这个时间戳兜：
            #   发图后的一小段时间里，屏幕上的图片消息一律不当"群友发的照片"。
            self._img_self_sent_at = time.time()
            return True

        self.log.warning("本次发图判定为失败")
        # ★ 2026-10-09 晚：桥接标了 retryable → 这是"你占用着手机"这类临时状态
        #   （图根本没进分享流程）。挂补发，而且**不算发送异常** —— 否则你连着用几次
        #   手机，就会被"连续 3 次发送异常"逼到自动停机（那看着像机器人挂了）。
        err = str(getattr(self.sender, "last_image_error", "") or "")
        if getattr(self.sender, "last_image_retryable", False) is True:
            self.log.info("这张表情包先攒着（%s），等你放下手机再补发 ← %s"
                          % (err or "环境忙", reason))
            self._queue_resend_image(path, reason, kind="sticker", rule_name=rule_name)
            return False
        if self.breaker.record_failure():
            self.kill.trigger(
                f"连续 {self.breaker.streak} 次发送异常，已自动停止（防止刷屏）")
        return False

    # ------------------------------------------------------------ 今日美图
    def _send_meitu(self, source: str, reason: str,
                    rule_name: Optional[str] = None,
                    mention: bool = False,
                    captions: Optional[List[str]] = None) -> bool:
        """发一张「今日美图」——联网随机取，取不到就退本地图库。

        跟发表情包（``_send_sticker``）的三处不同：

        1. 图要先**下载 + 转格式**（webp → JPEG，见 meitu.py），要几秒、也可能失败，
           所以把它放在"人类延迟"**之前**做 —— 免得白等一场才发现没图；
        2. 网络和本地图库都没图时**回一句话**（``meitu.empty_reply``），而不是像
           表情包那样静默跳过 —— 群友发了「今日美图」却一点动静都没有，看着像机器人死了；
        3. 冷却独立（默认 20 秒），不受表情包那 90 秒的牵连。
        安全检查、急停、全局限速跟发文字/发表情包**共用同一套**，一条都不放。
        """
        stop = self.kill.check()
        if stop:
            self.log.warning(f"急停生效中（{stop}），不发美图")
            return False

        if not self.meitu_enabled:
            self.log.info(f"今日美图关着（config 里 meitu.enabled）← {reason}")
            return False

        if not hasattr(self.sender, "send_image"):
            self.log.warning("当前平台不支持发图（sender 没有 send_image），跳过")
            return False

        # 全局限速：发美图也算一次"发言"，不能绕过 per_minute / per_hour / per_day
        allow, why = self.governor.check(mention=mention)
        if not allow:
            self.log.info(f"跳过（{why}） ← {reason}")
            return False

        gap = time.time() - self._last_meitu_at
        if self._last_meitu_at and gap < self.meitu_cooldown:
            self.log.info(f"跳过（美图冷却中，还要 {self.meitu_cooldown - gap:.0f}s）"
                          f" ← {reason}")
            return False

        if self.dry_run:
            # 演练模式不下载（省流量），只记一笔"本该取哪类图"
            self.log.info(f"[演练] 本该取一张美图发出去"
                          f"（分类「{source or '全部混池'}」）  ← {reason}")
            self.governor.record()
            return True

        path = self.meitu.fetch(source, avoid=self._recent_meitu)
        if path is None:
            self.log.warning(f"美图没取到（网络 + 本地图库都没图）← {reason}")
            # 本地图集空着 = 用户还没往里放图（比如刚建好的「看看腹肌」），
            # 那种时候静默跳过；只有"网络美图取不到"才值得在群里说一句。
            if self.meitu_empty_reply and not self.meitu.is_local_set(source):
                # 走 _reply 让它自己再走一遍节流/校验；这里不预先记账，免得算两次
                return self._reply(self.meitu_empty_reply,
                                   f"美图取不到，回话兜底 ← {reason}",
                                   rule_name, mention=mention)
            return False

        delay = self.governor.human_delay(self.rng)
        self.log.info(f"等 {delay:.1f}s 后发美图「{path.name}」（{reason}）")
        time.sleep(delay)

        stop = self.kill.check()
        if stop:
            self.log.warning(f"发美图前急停生效（{stop}），取消")
            return False
        if not self.sender.is_ready():
            self.log.info("等待期间已离开目标群，取消本次发美图")
            return False

        ok = bool(self.sender.send_image(path))

        if ok:
            self.breaker.record_success()
            self.governor.record()
            if rule_name:
                self.governor.record_rule(rule_name)
            self._last_meitu_at = time.time()
            self._recent_meitu.append(path.name)
            if len(self._recent_meitu) > self._meitu_avoid:
                self._recent_meitu.pop(0)
            # 跟表情包一样要进"我说过什么"的台账，否则读屏把自己的图判成
            # 别人发的，就会对着自己的图再回一次（_mine_said 的意义所在）
            self._mine_said.append((time.time(), normalize("[图片]")))
            # ★ 2026-10-10（识图）：刚发出去的这张图**不能被识图那条路点评**。
            #   机器人和主人共用一个微信号，读屏偶尔会把自己的气泡判成"别人发的"
            #   （图片气泡和文字气泡挨着时就会串行，2026-10-04 踩过）。文本有
            #   `_mine_said` 兜着，图片没有可比的文本 —— 用这个时间戳兜：
            #   发图后的一小段时间里，屏幕上的图片消息一律不当"群友发的照片"。
            self._img_self_sent_at = time.time()
            # 配文（规则里配了 replies 就有）：★ 只在这里发，因为**图已经成功**了
            if captions:
                self._send_caption_after_image(captions, reason)
            return True

        self.log.warning("本次发美图判定为失败")
        # ★ 2026-10-09 晚：同表情包 —— 临时占用就攒着补发（配文一起带上），不算发送异常
        err = str(getattr(self.sender, "last_image_error", "") or "")
        if getattr(self.sender, "last_image_retryable", False) is True:
            self.log.info("这张美图先攒着（%s），等你放下手机再补发 ← %s"
                          % (err or "环境忙", reason))
            self._queue_resend_image(path, reason, kind="meitu",
                                     rule_name=rule_name, captions=captions)
            return False
        if self.breaker.record_failure():
            self.kill.trigger(
                f"连续 {self.breaker.streak} 次发送异常，已自动停止（防止刷屏）")
        return False

    def _send_caption_after_image(self, captions, reason: str) -> None:
        """图发成功后跟一句配文（「看看腿」/主动美图都用它）。

        ★ 顺序上必须**先图后字**，而且中间要等一个回复间隔：
        · 先图后字：图没发成时群里只会剩一句没有图的话，看着像坏了；
        · 图走的是另一条通道、不更新"上一句回复"的时间戳，所以紧接着发字会被
          governor 的 min_reply_gap 当场拦掉 —— 2026-10-08 真机实测踩过
          （日志：「主动美图配文跳过（距上次回复仅 0.0s（下限 3.0s））」）。
        配文发没发成**都不影响调用方**：它是附赠，主菜是图。
        """
        pool = [str(x).strip() for x in (captions or []) if str(x).strip()]
        if not pool:
            return
        gap = float(getattr(self.cfg.pace, "min_reply_gap", 3) or 3) + 1.0
        time.sleep(gap)
        self._send_instant(self.rng.choice(pool), f"{reason}：配文",
                           label="美图配文")

    # ------------------------------------------------ 主动发美图（定时）
    AUTO_MEITU_STATE = "meitu_auto.json"

    def _load_auto_meitu_state(self) -> Dict[str, List[str]]:
        """读「哪天的哪个时间点已经主动发过了」。

        读坏了一律当空表 —— 宁可多发一次，也不能因为一个坏文件把功能整个卡死
        （跟 ppt_seen.json / 瓜词表.json 一个口径）。
        """
        try:
            d = json.loads((ROOT / self.AUTO_MEITU_STATE).read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(d, dict):
            return {}
        out: Dict[str, List[str]] = {}
        for k, v in d.items():
            if isinstance(v, (list, tuple)):
                out[str(k)] = [str(x) for x in v]
        return out

    def _mark_auto_meitu_sent(self, slot: str) -> None:
        """记下「今天这个点已经发过了」并落盘（只留最近 7 天）。"""
        from .atomic import atomic_write_json
        today = datetime.now().strftime("%Y-%m-%d")
        cur = list(self._auto_meitu_sent.get(today) or [])
        if slot not in cur:
            cur.append(slot)
        keep: Dict[str, List[str]] = {today: cur}
        for k in sorted(self._auto_meitu_sent.keys(), reverse=True):
            if k != today and len(keep) < 7:
                keep[k] = self._auto_meitu_sent[k]
        self._auto_meitu_sent = keep
        try:
            atomic_write_json(ROOT / self.AUTO_MEITU_STATE, keep)
        except Exception as exc:
            self.log.warning(f"主动美图记录写不进去（{exc}）—— 重启后可能重发一张")

    def _maybe_learn_nsh(self) -> None:
        """每天一次，把群里新出现的逆水寒说法交给模型"搞懂"。

        用户原话（2026-10-10）：「加常识库，不懂的就去搞懂下次再说」。
        真正的活在 `nsh_learn.py` 里（挑台账 → 问模型 → 写《逆水寒自学区.md》），
        这里只负责**到点起一个子进程**：

        * **不等它**：学习要调模型（实测十几秒），卡在主循环里这十几秒群里的
          消息就读不到；子进程崩了也跟机器人无关。
        * **间隔 20 小时**，不是"每天几点"——用户经常关机/重启，定点会永远错过；
          起点读 `nsh_learn_state.json` 的 `last_run`，所以重启不会重复学。
        * 学到的写进《逆水寒自学区.md》，那份文件在 `brain._knowledge()` 的
          注入清单里 → **下一轮重拼 system prompt 就生效**，不用重启。
        """
        if not bool(getattr(getattr(self.cfg, "behavior", None), "nsh_learn", True)):
            return
        last = getattr(self, "_nsh_learn_at", None)
        root = ROOT
        if last is None:
            last = 0.0
            try:
                st = json.loads((root / "nsh_learn_state.json").read_text(encoding="utf-8"))
                last = float(st.get("last_run") or 0.0)
            except Exception:
                last = 0.0
            self._nsh_learn_at = last
        now = time.time()
        if now - last < 20 * 3600:
            return
        script = root / "nsh_learn.py"
        if not script.is_file():
            return
        self._nsh_learn_at = now          # 先记上，失败也别每轮重试
        try:
            fh = open(root / "nsh_learn.log", "a", encoding="utf-8")
            subprocess.Popen([sys.executable, str(script)], stdout=fh, stderr=fh,
                             cwd=str(root), stdin=subprocess.DEVNULL,
                             start_new_session=True)
            self.log.info("逆水寒常识：起了学习子进程（学到什么看 nsh_learn.log）")
        except Exception as exc:
            self.log.warning("逆水寒常识：学习子进程起不来（%s）" % exc)

    def _maybe_auto_meitu(self) -> None:
        """到点了就往队列里放一张主动美图（窗口内没发成，后面几轮还会再试）。"""
        if not (self.meitu_enabled and self.meitu_auto_enabled):
            return
        if not self._meitu_auto_times or self._auto_meitu_pending:
            return
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        sent = list(self._auto_meitu_sent.get(today) or [])
        if len(sent) >= self._meitu_auto_max:
            return
        cur = now.hour * 60 + now.minute
        for slot in self._meitu_auto_times:
            if slot in sent:
                continue
            m = re.match(r"^(\d{1,2}):(\d{1,2})$", slot)
            if not m:
                self.log.warning(f"主动美图的时间点写法不对，跳过：{slot!r}（要 HH:MM）")
                continue
            start = int(m.group(1)) * 60 + int(m.group(2))
            if start <= cur <= start + max(1, self._meitu_auto_window):
                self._auto_meitu_pending.append(slot)
                self.log.info(f"主动美图触发（{slot} 那一张，"
                              f"窗口 {self._meitu_auto_window} 分钟）")
                return

    def _send_auto_meitu(self, slot: str) -> bool:
        """把主动那一张发出去：**先图后配文**。

        顺序是故意的：万一图没发成，群里只剩一句「今日美图来了喵」却没有图，
        看着像坏了；反过来只发出去一张图则完全正常 —— 就像群友随手丢的图。
        所以图失败就整条算失败，留在队列里、窗口内接着试。

        配文发没发成**不算失败**（图才是主菜）：否则配文一直被节流就会把同一张
        图反复发出去，那是真事故。
        """
        label = f"主动美图（{slot}）"
        if not self._send_meitu("", f"{label}：先发图"):
            return False
        if self._meitu_auto_captions:
            self._send_caption_after_image(self._meitu_auto_captions, label)
        self._mark_auto_meitu_sent(slot)
        return True

    # -------------------------------------------------- 人设按时段出场
    def _apply_persona_schedule(self) -> str:
        """按 ``behavior.persona_schedule`` 把当前时段的人设固定下来，返回人设名。

        2026-10-03 用户要求：白天固定「贴吧老哥」，不再随机。配置示例：:

            persona_mode: schedule
            persona_schedule:
              - {window: "07:00-24:00", name: "贴吧老哥"}
              - {window: "00:00-07:00", name: "猫娘"}

        按顺序匹配，第一条命中的生效；没配就什么都不做（沿用 persona_mode）。
        """
        sched = getattr(self.cfg.behavior, "persona_schedule", None)
        if not sched:
            return ""
        now = datetime.now()
        cur = now.hour * 60 + now.minute
        for item in sched:
            if not isinstance(item, dict):
                continue
            win = str(item.get("window", "") or "").strip()
            name = str(item.get("name", "") or "").strip()
            if not (win and name) or not _window_hit(win, cur):
                continue
            self.brain.persona_mode = "fixed"
            self.brain.persona_fixed = name
            # 关键词回复不调模型、不会触发 pick_persona，这里顺手把当前人设名定好，
            # 好让 _reply 判断这条要不要加「喵」。
            for n, _t in (self.brain.personas or []):
                if n == name or name in n:
                    self.brain.persona_name = n
                    break
            return name
        return ""

    def _persona_meows(self) -> bool:
        """当前人设说话带不带「喵」尾巴。

        配了 ``behavior.meow_personas`` 就按它 —— 白天的贴吧老哥不在名单里，
        于是不会加喵（否则「有一说一喵」当场穿帮）；没配则沿用旧行为：一律加。
        """
        names = getattr(self.cfg.behavior, "meow_personas", None)
        if not names:
            return True
        who = str(getattr(self.brain, "persona_name", "") or "")
        return any(str(n) in who for n in names)

    # -------------------------------------------------- 深夜主动回复（辅助）
    def _in_proactive_window(self) -> bool:
        """现在能不能主动开口 —— 时区内 + 没被急停/配额挡住。"""
        behavior = self.cfg.behavior
        if not bool(getattr(behavior, "proactive", False)):
            return False
        rng = str(getattr(self.cfg.pace, "proactive_window", "") or "").strip()
        if not rng:
            return True
        now = datetime.now()
        cur = now.hour * 60 + now.minute
        for part in rng.split(","):
            part = part.strip()
            if "-" not in part:
                continue
            a_s, b_s = part.split("-", 1)
            try:
                ah, am = [int(x) for x in a_s.strip().split(":")]
                bh, bm = [int(x) for x in b_s.strip().split(":")]
            except Exception:
                continue
            a, b = ah * 60 + am, bh * 60 + bm
            if a <= b:
                if a <= cur < b:
                    return True
            else:                                   # 跨零点
                if cur >= a or cur < b:
                    return True
        return False

    def _proactive_ok(self) -> bool:
        """主动开口之间的额外间隔（默认 3~5 分钟）——被动回复不受它影响。"""
        now = time.time()
        gap = self.rng.uniform(*self._proactive_gap)
        if self._last_proactive_at and now - self._last_proactive_at < gap:
            rest = gap - (now - self._last_proactive_at)
            self.log.info(f"主动回复间隔未到（还要 {rest:.0f}s），这轮不插话")
            return False
        return True

    def _maybe_midnight(self) -> None:
        """到点发深夜文案：固定开场 + 一条随机 EMO，一天一次。

        这是**定时发送**，不是对某条消息的回复，所以不走 handle 的召回逻辑；
        仍然过急停、频率配额和出站校验，怕吵就调 pace.midnight_window。
        """
        mk = getattr(self.cfg, "midnight", None)
        if not bool(getattr(mk, "enabled", False)):
            return
        now = datetime.now()
        win = str(getattr(self.cfg.pace, "midnight_window", "00:00-00:10"))
        if not self._in_window(now, win):
            # ★ 2026-10-08 审查修复（F2 配套）：窗口关闭时把当天没发出去的残留丢掉
            #   —— 否则它会一直排在队首、每轮白试一次，甚至跨天还在占位。
            if self._midnight_pending:
                self.log.info("零点文案窗口已过，丢弃没发出去的 %d 条"
                              % len(self._midnight_pending))
                self._midnight_pending.clear()
            return
        today = now.strftime("%Y-%m-%d")
        if self._midnight_sent_on == today:
            return
        texts = [str(getattr(mk, "open_text", "") or "").strip()]
        emo = [str(x).strip() for x in (getattr(mk, "emo_texts", []) or []) if str(x).strip()]
        if emo:
            texts.append(self.rng.choice(emo))
        texts = [t for t in texts if t]
        if not texts:
            return
        # ★ 2026-10-08 审查修复（F2）：这里原来先写 `_midnight_sent_on = today` 占位。
        #   后果：① 队列里的两条文案只要有一条被节流/失败，当天就再也不补发
        #   （实测第二条 EMO 自 2026-10-06 起一次都没发出去）；② 「texts 为空」也占位。
        #   现在改成：**发起时用 _midnight_started 防同一分钟重复触发，
        #   只有真的发出去了才在 _send_instant 里写 _midnight_sent_on**。
        if self._midnight_started == today:
            return
        self._midnight_started = today
        self.log.info("零点文案触发：%s" % " / ".join(texts))
        self._midnight_pending.extend(texts)

    @staticmethod
    def _in_window(now, rng) -> bool:
        cur = now.hour * 60 + now.minute
        for part in str(rng or "").split(","):
            part = part.strip()
            if "-" not in part:
                continue
            a_s, b_s = part.split("-", 1)
            try:
                ah, am = [int(x) for x in a_s.strip().split(":")]
                bh, bm = [int(x) for x in b_s.strip().split(":")]
            except Exception:
                continue
            a, b = ah * 60 + am, bh * 60 + bm
            if (a <= b and a <= cur < b) or (a > b and (cur >= a or cur < b)):
                return True
        return False

    def _poll_interval(self) -> float:
        """当前这一轮该隔多久再读下一次屏。

        夜间低峰（``pace.slow_poll_window``，默认 00:00-08:00）用
        ``slow_poll_interval`` 秒一帧省电，白天用 ``pace.poll_interval``。

        ⚠️ 慢速是拿「漏消息」换电，不是拿「延迟」换电：机器人靠读屏，只看得见
        当前屏幕上那一帧，间隔里刷过去的消息它**永远看不到**。所以慢速窗口
        只该覆盖群里没人说话的时段。
        """
        pace = self.cfg.pace
        fast = float(getattr(pace, "poll_interval", 2.5))
        slow = float(getattr(pace, "slow_poll_interval", 0) or 0)
        win = str(getattr(pace, "slow_poll_window", "") or "").strip()
        slow_now = bool(slow > 0 and win and self._in_window(datetime.now(), win))
        interval = max(fast, slow) if slow_now else fast

        # ★ 2026-10-10 用户要求：「群里的聊天记录是爆发式的，机器人读平时一直 10 秒……
        #   能不能改？一下有人发消息他就开始增大频率，然后没人了再慢慢把频率降下来。」
        #   规则见 _adaptive_interval。**只在白天生效** —— 夜间慢速窗口是他明确要的
        #   "整段省电"，跟自适应是两个诉求，不该被它压回去。
        if not slow_now and bool(getattr(pace, "adaptive_poll", False)):
            # ★★ 2026-10-10 评审修复（§1.2）：原来是 `max(interval, ...)` ——
            #   而 `_adaptive_interval` 在爆发档（`burst_interval`，实测配 0）
            #   返回的是 **0.2**，`max(10, 0.2)` 又把它抬回 10 ⇒ 爆发提速
            #   **从来没生效**，可 `_adaptive_interval` 里照样打「读屏全速连读」，
            #   日志把这件事瞒了整整一天（实测爆发期一直 10.0 秒一轮）。
            #   "放慢"那一半本来就在 `_adaptive_interval` 内部走 `max`，所以这里
            #   直接取它的返回值；夜间慢速与读屏失败退避在下面各自用 max 压住
            #   —— 那两个本来就该是"上限"，不受影响。
            interval = self._adaptive_interval(fast)

        # 连续读不到画面（典型是 adb 断了 / 微信不在虚拟屏）→ 退避省电，别 20 秒白试一次。
        # 2026-10-04 实测：用户出门断 WiFi，Android 把「无线调试」一起关掉 → adb 断 →
        # 机器人整整半小时每 20 秒空转一次（手机只剩 29% 电）。退避到 5 分钟一帧，
        # 读屏一恢复（下一轮读到画面）就自动回到正常节奏，不需要人工干预。
        BACKOFF_ROUNDS, BACKOFF_SECONDS = 15, 300.0
        fails = int(getattr(self, "_read_fails", 0) or 0)
        if fails >= BACKOFF_ROUNDS:
            interval = max(interval, BACKOFF_SECONDS)
            if not getattr(self, "_read_backoff", False):
                self._read_backoff = True
                self.log.warning(
                    f"已连续 {fails} 轮读不到画面（多半是 adb 断了 / 微信不在虚拟屏），"
                    f"轮询退避到 {interval:.0f} 秒省电 —— 读屏一恢复会自动回到正常节奏")
        elif getattr(self, "_read_backoff", False):
            self._read_backoff = False
            self.log.info("读屏已恢复，退出省电退避")

        # 进出慢速时段各说一声，否则日志静悄悄，看不出机器人在按哪个节奏跑
        if slow_now != getattr(self, "_slow_poll_state", None):
            self._slow_poll_state = slow_now
            if slow_now:
                self.log.info(f"进入夜间慢速轮询：每 {interval:.0f} 秒一帧"
                              f"（窗口 {win}）—— 期间刷过去的群消息会被漏掉")
            else:
                self.log.info(f"回到白天轮询：每 {interval:.1f} 秒一帧")
        return interval

    def _adaptive_interval(self, fast: float) -> float:
        """按「上一轮群里有没有新消息」决定这一轮隔多久再读屏。

        ★ 2026-10-10 用户口述的规则：**有人说话就加快、没人了就慢慢降回来**。
        输入是 `self._calm_rounds`（`step()` 每轮记：有新消息清零、没有就 +1）；
        输出就是日志里那个"多久一帧"。

        ⚠️ 为什么"加快"的效果注定有限：full 档一轮读屏（截图 + OCR）本身就 **~9 秒**，
        而 `poll_interval` 是"读完到下次读之间还要等多久" —— 现在 10 秒里真正在等的
        只有 1 秒。所以自适应真正省电的是**放慢那一半**（安静时 10 秒爬到 60 秒 ≈
        省 6 倍 CPU），"加快"最多把 10 秒压到 9 秒。想更快只能往上翻补读历史，
        那是另一件事，别在这儿硬压间隔。
        """
        calm = int(getattr(self, "_calm_rounds", 0) or 0)
        raw_steps = getattr(self.cfg.pace, "idle_steps", None) or []
        steps = []
        for x in raw_steps:                       # 配置里可能写成 null/空串，跳过
            try:
                steps.append(float(x))
            except (TypeError, ValueError):
                continue
        # ★ 2026-10-10：爆发期（上一轮有新消息）用 burst_interval —— 实测一轮读屏本身
        #   要 6~7 秒，所以「间隔 10 秒」里有一半在干等；设 0 = 读完立刻读下一轮。
        burst = getattr(self.cfg.pace, "burst_interval", None)
        try:
            burst = float(burst) if (burst is not None and str(burst).strip()) else None
        except (TypeError, ValueError):
            burst = None
        if calm <= 0:
            step = -1
            out = fast if burst is None else max(0.2, min(burst, fast))
        elif not steps:
            step, out = -1, fast
        else:
            per = max(1, int(getattr(self.cfg.pace, "idle_step_rounds", 3) or 3))
            step = min((calm - 1) // per, len(steps) - 1)
            out = max(fast, steps[step])
        # 只在**档位变化**时打日志（每轮都打会把 bot.log 刷爆）
        prev = getattr(self, "_poll_step", None)
        prev = -1 if prev is None else int(prev)
        if step != prev:
            self._poll_step = step
            if step < 0:
                if burst is not None and out < fast:
                    self.log.info("群里来消息了 → 读屏全速连读（读完立刻读下一轮）")
                else:
                    self.log.info(f"群里来消息了 → 读屏回到全速（每 {out:.0f} 秒一帧）")
            else:
                self.log.info(f"已连续 {calm} 轮没有新消息 → "
                              f"读屏放慢到每 {out:.0f} 秒一帧")
        return out

    def _sleep_interval(self, seconds: float) -> None:
        """按读屏间隔等待，但 tuning.json 一变就**立刻**回主循环。

        为什么要分片睡：用户如果把读屏间隔拧到 1 小时（上限就是 3600 秒），
        再想拧回 10 秒，一整轮 sleep 可能正好睡在那一小时里 —— 得等满一小时
        改动才生效。这里每 2 秒醒一次看一眼调参文件的 mtime，变了就提前返回，
        下一轮顶部的 reload_if_changed() 会把新节奏套上。

        额外开销：每 2 秒一次 stat（比一次 OCR 便宜 5 个数量级），可忽略。
        """
        end = time.time() + max(0.0, seconds)
        last_peek = time.time()
        last_screen = time.time()      # ★ 2026-10-11：锁屏检查的计时（见下面循环尾）
        while True:
            remain = end - time.time()
            if remain <= 0:
                return
            time.sleep(min(2.0, remain))
            try:
                if _mtime(self._tuning_path) != self._tuning_mtime:
                    self.log.info("调参文件有改动，提前结束本轮等待并重算读屏节奏")
                    return
            except Exception:
                return
            # ★ 2026-10-10（用户问「为什么会退避五分钟」）：**退避期间轻探**。
            #   原来连续 15 轮读不到画面就把间隔拉到 300 秒，而恢复判据只有
            #   "下一轮读到画面" —— 微信 10 秒就回来了，机器人也要瞎等满 5 分钟。
            #   现在退避时每 15 秒问桥接一次"微信在不在虚拟屏"（一次 dumpsys，
            #   比一轮 full OCR 便宜得多），一回来就立刻结束等待、下一轮全速。
            if getattr(self, "_read_backoff", False) and time.time() - last_peek >= 15.0:
                last_peek = time.time()
                probe = getattr(self.sender, "wechat_on_screen", None)
                if probe is not None:
                    try:
                        if probe():
                            self._read_fails = 0
                            self._read_backoff = False
                            self.log.info("轻探到微信已回到虚拟屏 → 立刻结束省电退避")
                            return
                    except Exception:
                        pass
            # ★ 2026-10-11：锁屏时**立刻结束本轮等待**，好让主循环走到 _wait_awake
            #   去整体挂起（不读屏、不调模型）。每 10 秒问一次屏幕状态，
            #   成本 ≈ 一次 dumpsys power，比一轮 OCR 便宜几个数量级。
            if time.time() - last_screen >= 10.0:
                last_screen = time.time()
                probe2 = getattr(self.sender, "screen_awake", None)
                if probe2 is not None:
                    try:
                        if probe2() is False:
                            self.log.info("锁屏了 → 提前结束本轮等待，转入挂起")
                            return
                    except Exception:
                        pass

    # ------------------------------------------------------ 锁屏挂起（★ 2026-10-11）
    def _wait_awake(self) -> None:
        """锁屏（熄屏）时整体挂起：不读屏、不调模型、不发送；亮屏后立刻恢复。

        ★ 2026-10-11 用户报「锁屏时发不出消息、一直在震动」，随后拍板
          「锁屏时连读屏和模型也停掉，亮屏再唤醒」。为什么必须停：
            · 锁屏下系统不给非锁屏窗口输入连接（实测 `mServedView=null`），
              机器人**根本发不出去**，读屏 + 调模型全是白烧电、白烧 token；
            · 继续跑还会每条消息都走一次"发送前协商"：熄屏后 topActivity 停在微信
              （陈旧记录），会被误判成"你在用手机"，于是弹带震动+响铃的抢屏预告
              —— 这就是用户听到的"一直在震动"。
        ⚠️ 判据三态：True（亮着）/ False（熄屏）/ **None（读不到）→ 什么都不做**。
           按看护的铁律，绝不把"读不到"当成"用户在睡觉"—— 否则 adb 一抖，
           机器人就自己睡过去，亮着屏也不回消息。
        """
        try:
            probe = getattr(self.sender, "screen_awake", None)
            if probe is None:
                return
            state = probe()
        except Exception:
            return
        if state is not False:
            return
        self.log.info("锁屏中 → 挂起读屏与模型调用（亮屏后自动恢复）")
        # ★ 2026-10-11 晚（用户要求）：「锁屏已恢复这个就不用通知了，我不用每次打开
        #   屏幕就看见这个弹窗」—— 锁屏挂起 / 亮屏恢复**只写日志、不浮窗**。
        #   浮窗留给真正需要他知道的事（发送失败、要借屏、拦下疑似发错）。
        #   ⚠️ 别再顺手把这两条 _hud_note 加回来。
        # 每 15 秒看一眼亮没亮 —— 亮屏最多 15 秒内恢复；熄屏期间不读屏、不调模型。
        while True:
            try:
                if self.kill.check():
                    return
            except Exception:
                pass
            time.sleep(15.0)
            try:
                if self.sender.screen_awake() is not False:
                    break
            except Exception:
                break
        self.log.info("亮屏了 → 恢复读屏与模型调用")
        # 同上：恢复也不浮窗（用户 2026-10-11 晚要求）。

    # ------------------------------------------------------ 记忆库自动整理（★ 2026-10-11）
    def _maybe_memory_learn(self) -> None:
        """每天一次：让模型从最近的群聊里学几条「人 / 梗」写进记忆库。

        用户 2026-10-11 拍板「每天自动学一次」。跑没跑过由 `wxauto/memlearn.py`
        用 `记忆_auto.json` 自己判（**成功失败都记** —— 接口抽风时宁可今天不学，
        也不能每轮重试把钱烧掉）。这里只负责读配置、调它、把结果写日志。
        ⚠️ 默认是开着的（config.yaml → memory.auto.enabled）；要关就改 false。
        """
        try:
            mcfg = getattr(self.cfg, "memory", None)
            acfg = getattr(mcfg, "auto", None) if mcfg is not None else None
            if acfg is None or not bool(getattr(acfg, "enabled", False)):
                return
            hours = float(getattr(acfg, "hours", 24) or 24)
            max_items = int(getattr(acfg, "max_items", 5) or 5)
            base = ROOT
            from . import memlearn
            memlearn.learn(self.brain, base, self.log, hours=hours, max_items=max_items)
        except Exception as exc:                 # 绝不影响主流程
            try:
                self.log.warning("记忆库自动整理跳过：%r" % (exc,))
            except Exception:
                pass

    def _maybe_group_summary(self) -> None:
        """到点就把这一段群聊压成「群情摘要」（借鉴 AstrBot 的自动上下文压缩）。

        ★ 2026-10-12 用户要求「借鉴一下功能，优化一下」。现状：上下文只有最近
          16 条 / 10 分钟窗 —— 昨天那场瓜的结论、正在延续的事它都不知道。
        「跑没跑、跑到哪儿了」由 `wxauto/groupsum.py` 用 `群情摘要.json` 自己判
        （时间戳怎么推进、失败为什么不推进，都在它那边）；这里只读配置、调它、打日志。
        ⚠️ 默认开（config.yaml → summary.enabled），要关就改 false。
        """
        try:
            scfg = getattr(self.cfg, "summary", None)
            if scfg is None or not bool(getattr(scfg, "enabled", True)):
                return
            base = ROOT
            from . import groupsum
            groupsum.maybe_run(self.brain, base, self.log, scfg)
        except Exception as exc:                 # 绝不影响主流程
            try:
                self.log.warning("群情摘要跳过：%r" % (exc,))
            except Exception:
                pass

    def _send_instant(self, text: str, reason: str,
                      label: str = "零点文案") -> bool:
        """到点就发（不带上一条消息的上下文），但仍然走急停 / 配额 / 校验。

        ``label`` 只影响日志措辞 —— 主动发美图的配文也走这条路（2026-10-08），
        日志里得能分清是零点还是美图。判"今天已经发过零点文案"仍然**认 reason**，
        所以别的调用方传自己的 reason 就不会误写零点的标记。
        """
        stop = self.kill.check()
        if stop:
            self.log.warning(f"急停生效中（{stop}），不发{label}")
            return False
        limit = int(getattr(self.cfg.behavior, "max_reply_chars", 40))
        # ★ 2026-10-10：这里原来无条件补「喵」—— 换成不带喵的人设（女仆）后，
        # 零点文案 / 美图配文会照旧冒出一个「喵」当场穿帮。跟 _reply 同口径判一次。
        text = strip_action(text)          # ★ 2026-10-10：见 safety.strip_action
        if not text:
            self.log.info("剥掉括号动作后是空的 → 不发")
            return False
        text = with_meow(text, limit, no_meow=not self._persona_meows())
        bad = validate_outgoing(text, limit)
        if bad:
            self.log.warning(f"{label}被拦下（{bad}）：{text!r}")
            return False
        allow, why = self.governor.check()
        if not allow:
            self.log.info(f"{label}跳过（{why}）")
            return False
        if self.dry_run:
            self.log.info(f"[演练] {label}本该发送：{text}")
            # 演练模式也记账（否则干跑时同一窗口会每分钟重报一次），
            # 这与 _reply 的演练分支口径一致。
            if reason == "零点文案":
                self._midnight_sent_on = datetime.now().strftime("%Y-%m-%d")
            return True
        delay = self.governor.human_delay(self.rng)
        self.log.info(f"等 {delay:.1f}s 后发送{label}「{text}」（{reason}）")
        time.sleep(delay)
        stop = self.kill.check()
        if stop:
            return False
        if not self.sender.is_ready():
            self.log.info(f"发送{label}时已离开目标群，取消")
            return False
        ok = self.sender.send(text)
        if not ok and getattr(self.sender, "last_send_uncertain", False) is True:
            # ★ 2026-10-12：桥接超时 / 断连，消息可能已进群。按「已发」收尾（出队、
            #   不重试）—— 零点文案 / 美图配文重复发一遍比漏一条更糟。不动熔断。
            self.log.warning(f"{label}发送结果不确定（桥接超时/断连），按「可能已发出」处理，"
                             f"不重试：{text!r}")
            self.governor.record()
            self._mine_said.append((time.time(), normalize(text)))
            if reason == "零点文案":
                self._midnight_sent_on = datetime.now().strftime("%Y-%m-%d")
            return True
        if ok:
            self.governor.record()
            self.breaker.record_success()
            # ★ 2026-10-08 审查修复（F2）：「今天已经发过零点文案」这个标记从此处
            #   才置位（原来是确定内容时就占位）。放到这里的原因：只有真的发出去
            #   才算数 —— 被节流/失败的那些句子留在队列里，窗口内还会再试。
            self._mine_said.append((time.time(), normalize(text)))
            if reason == "零点文案":
                self._midnight_sent_on = datetime.now().strftime("%Y-%m-%d")
        else:
            self.log.warning(f"{label}发送失败：{text!r}")
            # 注意：**不**记熔断 —— 零点是一次性的定时动作，把连续失败计数推起来
            # 只会在深夜把机器人整体停掉，得不偿失（与 _reply 的口径一致）。
        return bool(ok)

    # ★ 2026-10-06 用户要求「机器人播报没有用，删了吧」—— 原来这里有 _announce()：
    #   启动往群里说「喵来视奸你们了喵」、停止说「喵走了喵会想念你们的喵」。
    #   连同 config.yaml 的 announce 段、selftest 的三条断言、面板按钮上那两句
    #   "会在群里说…"的确认文案一起删了。启动/停止现在**不往群里说任何话**。
    #   想恢复：看 wxauto/bot.py.bak-1005-noannounce。

    @staticmethod
    def _stop_why_text() -> str:
        """★ 2026-10-12：读 /tmp/wxbot.stopwhy（bot.sh/stop.sh/面板/电量看护停机前写的），
        120 秒内的才算数。读不到就说"没人留话"——那也是线索：说明不是我们的脚本发的。
        ⚠️ 在信号处理里跑：只做一次小文件读取，所有异常吞掉。"""
        try:
            import time as _t
            with open("/tmp/wxbot.stopwhy", encoding="utf-8") as fh:
                ts = int((fh.readline() or "0").strip() or 0)
                why = (fh.readline() or "").strip()
            if 0 <= _t.time() - ts <= 120 and why:
                return f"（谁停的：{why}）"
        except Exception:
            pass
        return "（谁停的：没人留话 —— 不是 bot.sh / stop.sh / 面板 / 电量看护发的，可能是手动 kill 或别的进程）"

    def _install_stop_signal(self) -> None:
        """接住 SIGTERM/SIGINT，干净地退出。

        Python 默认收到就直接终止、**连 finally 都不走**，所以自己接信号。

        ★ 2026-10-06：原来这里要"先发一句告别再退出"（群内播报），用户说播报没用
        已经删掉，现在只做一件事 —— 把第二次及以后的信号忽略掉（`stop.sh` 会对同一
        个进程连发两次 kill），然后正常退出。

        ⚠️ 别再往这个 handler 里加发送动作：它是信号上下文，任何阻塞都可能把停止流程
        卡死（这条是原来告别播报留下的教训，留着）。
        """
        import signal

        seen = {"n": 0}

        def _handler(signum, _frame):
            seen["n"] += 1
            name = getattr(signal.Signals(signum), "name", str(signum))
            if seen["n"] > 1:
                self.log.info(f"又收到一次 {name}，忽略")
                return
            self.log.info(f"收到 {name}，退出{self._stop_why_text()}")
            raise SystemExit(0)

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError) as exc:
                self.log.warning(f"信号 {sig!r} 没接上（{exc}）")

    # ------------------------------------------------------------ 单条决策
    def handle_own(self, msg: Message, snap: Snapshot) -> bool:
        """**主人自己发的**消息 —— 关键词规则 + 「喊它名字」时走模型（2026-10-09）。

        场景一：他在群里发一句「看看腿」当遥控器，机器人（同一个微信号）要认这条指令
        并真把图发出去。以前这条路是没有的（`not m.is_self` 一律跳过）。

        场景二（★ 2026-10-09 续12 用户追加）：「我希望机器人在识别到昵称这三个字
        的时候能主动回复，因为这是我对机器人常用的称呼，我也想和机器人互动一下」
        —— 所以他喊它的名字（`brain.is_mention`）时**走模型**，被 @ 一个待遇；
        那条路的细节见下面那段注释。

        为什么关键词这条路**只跑规则、不跑模型**：
          · 模型那条路是"接别人的话"，对着自己的**每一句**话接就变成自言自语；
          · 规则是**确定的文本 / 动作**（发表情包、发图、固定话术），命中就执行、
            命不中就安静 —— 而且机器人自己发出去的文本都记在 `_mine_said` 里，
            上面已经用 `_is_my_echo` 滤过一遍，不存在"自己触发自己"的循环。
        ⚠️ 传 `mention=True` 是**故意的**：那个参数的语义正是"豁免时间闸门"
          （`min_reply_gap` / `burst_limit` / 静默时段），而主人自己点名要的东西
          不该被"距上次回复仅 1.2 秒"这种理由挡掉（2026-10-09 实测踩过：
          群友发的「看看腿」就是这么被挡的）。配额 per_minute/per_hour/per_day
          **照常计**，规则 cooldown 也照常算，防刷屏的底线一条没松。
        """
        text = normalize(msg.text)
        if not text:
            return False
        behavior = self.cfg.behavior
        hard_block = self.brain.should_skip(text, hard=True)
        if hard_block:
            self.log.info(f"不接（硬避雷：{hard_block}）：{text}（你自己发的）")
            return False

        # ★ 2026-10-09（续12）用户要求：「我希望机器人在识别到昵称这三个字的时候
        #   能主动回复，因为这是我对机器人常用的称呼，我也想和机器人互动一下」。
        #   —— 给他的**点名**开一条模型通道。原来自己发的消息只跑关键词规则
        #   （见本方法开头的说明），那条规矩管的是"别对着自己的每句话都走模型"；
        #   而"他喊它的名字"是明确的对话邀请，不是自言自语。
        #
        #   三道闸门保证它不会变成刷屏机器：
        #     ① 只有消息里出现"在叫它"的称呼才走（`brain.is_mention`：昵称 / 小AI /
        #        你昵称的 OCR 误读变体 …；比较是大小写不敏感的），普通话照旧只跑规则；
        #     ② 走"被 @"同一条档位（`mention=True`：豁免 min_reply_gap / burst_limit /
        #        静默时段，但 per_minute / per_hour / per_day **一条都不放**）；
        #     ③ ★ **防自触发**：模型写出来的话如果**又带上了那个称呼**，就换一条重写；
        #        第二次还带就整条丢弃 —— 机器人发出去的文本里永远不含触发词，
        #        于是"它自己的话把自己再叫一遍"这个循环从根上不成立。
        #        （`_is_my_echo` 是第一道防线，这道不依赖 OCR 读不读得准自己的话。）
        if self.brain.is_mention(text) \
                and bool(getattr(behavior, "reply_on_mention", True)):
            if not self.brain.available:
                self.log.info(f"你叫了它，但模型不可用（没配 key / 关了）：{text}")
                return False
            _mt = int(getattr(getattr(self.cfg, "llm", None),
                              "mention_max_tokens", 0) or 0)
            _mc = int(getattr(behavior, "mention_max_chars", 0) or 0)
            tag = f"你叫它：{text}"
            # ★ 2026-10-10（用户口径：「被艾特让看看图的时候识图」）：喊了它名字、
            #   旁边又正好有一张图 → **先让它看图回答**，别答非所问。
            #   `allow_self=True`：他自己发的图也是 is_self（共用一个微信号），
            #   不放行的话"我发张图问它"这条路永远走不通 —— 而这正是他最常用的用法。
            # ★ 2026-10-09 晚（用户报「群里我让你查一下，但是机器人以为是识别图片」）：
            #   原来这里是**无条件**"旁边有图就先识图"，而群里它旁边经常有图 ——
            #   于是一句「昵称，给他一个学习网站」被答成「学习网站本喵没有 自己搜去喵」
            #   （bot.log 21:57:58 就是这么答的）。
            #   现在收成他 10-10 自己说过的那个口径：「**被艾特让看看图的时候识图**」
            #   ——只有这句话确实在说图（`_look`）才去找图、才识图；其余一律先办事/聊天。
            #   ⚠️ 别再退回"有图就识图"：群聊里图是常态，那等于把办事请求全抢走。
            _look = self._looks_like_image_request(text)
            img = self._nearby_image(snap, msg, allow_self=True) if _look else None
            # ★ 2026-10-10（用户要求「想个法子让他能读到聊天记录里的内容」）：
            #   主人叫它「读一下这个聊天记录」→ 点开屏幕上那张转发卡片、整页 OCR、
            #   按人设点评发群。
            #   ⚠️ 必须排在 agent 前面：这件事本地就能办（实测 20~30 秒），而转给
            #   agent 要 45~90 秒、那个 headless agent 的命令沙箱连 bash 都跑不起来
            #   （实测），它读不到卡片 —— 14:42 那次就是这么回了一句"我读不到"。
            #   `_look` 为真时不抢（"看看聊天记录里这张图"那种话里两张词都有）。
            if not _look and self._looks_like_record_request(text):
                if self._read_record_and_comment(text, snap):
                    return True
            # ★ 2026-10-10（用户选「补读只在我叫的时候翻」）：往回翻两屏补读。
            if not _look and self._looks_like_backfill_request(text):
                if self._backfill_and_reply(text, snap):
                    return True
            _ask = False if _look else self._agent_should_take(text)
            if img is not None:
                self.log.info("你叫它看图：%s（%dx%d）"
                              % (os.path.basename(img.img_path or ""),
                                 int(img.img_w or 0), int(img.img_h or 0)))
                self._hud_note("机器人在看图", "你叫它看这张图，正在识别")
                try:
                    got = self.brain.comment_on_image(
                        img.img_path, sender="", snap=snap, max_reply=_mc,
                        addressed=True, addressed_text=msg.text)
                except Exception as exc:
                    got = None
                    self.log.warning("识图异常：%s: %s" % (type(exc).__name__, exc))
                if got:
                    # 跟下面 compose 那条路同一条规矩：回复里**不许再出现称呼**，
                    # 否则机器人自己的话会把自己再叫一遍（续12 的防自触发）。
                    if not self.brain.is_mention(got):
                        return self._reply(got, tag + "（看图）", mention=True,
                                           max_chars=_mc)
                    self.log.info("识图回复里又出现了称呼，丢弃这一条：%s" % got)
                else:
                    self.log.info("识图没给出内容，退回普通回复")
            # ★ 2026-10-09（用户要求）：「我想让这个机器人更智能一点，可以让它帮我
            #   干点事情」+「让机器人直接调用你的模型不就行了吗？」—— 主人喊它办事
            #   （"帮我查查…""那句原话是什么"）时，把活儿转给 DSH 的 headless agent
            #   （`dsh --profile headless`，**带全套工具**：真搜索、读网页、跑命令），
            #   再把答复发回群里。失败/超时/超配额就往下走普通的模型回复。
            #   ⚠️ 这条只存在于 `handle_own`（主人自己发的）—— 群友 @ 它不走 agent。
            if _ask:
                # ★ 2026-10-09 实测：agent 走一趟要 **45~90 秒**（搜索工具链 + 起步），
                #   群里干等着会以为它死了 —— 先应一声再去办（`agent.ack` 留空就不发）。
                _ack = str(getattr(self.cfg.agent, "ack", "") or "")
                if _ack:
                    self._reply(_ack, "办事先应一声：" + text, mention=True)
                got = self._ask_agent(text)
                if got and not self.brain.is_mention(got):
                    return self._reply(got, "交给 agent 办的：" + text,
                                       mention=True, max_chars=_mc)
                self.log.info("交给 agent 没办成（或答复里带称呼），退回普通回复：%s"
                              % text)
            # ★ 2026-10-12（工具层，见 wxauto/tools.py）：agent 管"办不了的交给人"，
            #   这里管"本地立刻能办的"（现在就是发表情包）。同样：拿不准就 self。
            _tool = self._pick_tool(text)
            if _tool != "self" and self._run_tool(_tool, text):
                return True
            for attempt in (1, 2):
                reply = self.brain.compose(snap, msg, "主人自己在群里叫它",
                                           max_tokens=_mt, max_chars=_mc,
                                           expand=bool(_mc), own_ask=True)
                if not reply:
                    break
                if not self.brain.is_mention(reply):
                    return self._reply(reply, tag, mention=True, max_chars=_mc)
                self.log.info(f"回复里又出现了称呼（第{attempt}次），"
                              f"换一条重写：{reply}")
            self.log.info(f"你叫了它，但没生成出能发的回复：{text}")
            return False

        if not bool(getattr(behavior, "reply_on_keyword", True)):
            return False
        rule = self.matcher.match(text)
        if rule is None:
            return False
        ok, why = self.governor.rule_allowed(rule.name, rule.cooldown)
        if not ok:
            self.log.info(f"跳过（{why}） ← {text}（你自己发的）")
            return False
        tag = f"你发的关键词「{rule.name}」← {text}"
        if rule.is_image:
            return self._send_sticker(rule.image_tag, tag, rule.name, mention=True)
        if rule.is_meitu:
            return self._send_meitu(rule.meitu_source, tag, rule.name,
                                    mention=True, captions=rule.replies)
        reply = self.matcher.reply_for(rule)
        if reply:
            return self._reply(reply, tag, rule.name,
                               no_meow=bool(rule.no_meow), mention=True)
        return False

    # ------------------------------------------------------------ 交给 agent
    # ★ 2026-10-09（用户要求「让它帮我干点事情」+「让机器人直接调用你的模型」）：
    #   主人喊它办事时，把活儿转给 DSH 的 headless agent（带全套工具）去办。
    #   详细安全边界见 wxauto/agentask.py 顶部。
    def _tool_cfg(self):
        return getattr(self.cfg, "tools", None)

    def _pick_tool(self, text: str) -> str:
        """按工具表路由 —— **只处理"本地能立刻办"的那几个**。

        ★ 2026-10-12：见 `wxauto/tools.py` 的文件头。
        ⚠️ 刻意**不把 agent 塞进来**：它有自己的老路（`_agent_should_take`，含
          触发词后门 + 实测调优的配额），重复路由只会多花钱。
        ⚠️ 也刻意**不做成每轮都问**：只有主人自己发言（`handle_own`）才走这里，
          群友 @ 它不走（`owner_only`）—— 群友那份有 `rules.yaml` 的显式规则兜着。
        """
        cfg = self._tool_cfg()
        if cfg is None or not bool(getattr(cfg, "enabled", True)):
            return "self"
        try:
            from . import tools as T
            cand = [t for t in self._all_tools() if t.name not in ("self", "agent")]
            usable = [t.name for t in cand if T.allowed(t, TOOL_USED_FILE)[0]]
            if not usable:
                return "self"
            lines = ["self —— 自己回（闲聊、接梗、常识、按人设发挥 —— 你能直接答的）"]
            for t in cand:
                if t.name in usable:
                    lines.append("%s —— %s（%s）" % (t.name, t.desc, t.when))
            name = self.brain.pick_tool(text, "\n".join(lines),
                                        allowed=set(usable) | {"self"})
            self.log.info("工具路由：%s → %s" % (text[:24], name))
            return name if name in usable else "self"
        except Exception as exc:
            self.log.warning("工具路由跳过：%r" % (exc,))
            return "self"

    def _all_tools(self):
        """内置工具 + 插件注册的工具（后者包成同样的 `Tool`，配额/冷却共用一套）。"""
        from . import tools as T
        out = list(T.TOOLS)
        try:
            for pt in self.plugins.tools():
                out.append(T.Tool(pt["name"], pt["desc"], pt["when"],
                                  owner_only=bool(pt.get("owner_only", True)),
                                  cooldown=float(pt.get("cooldown") or 0),
                                  max_per_day=int(pt.get("max_per_day") or 0)))
        except Exception:
            pass
        return out

    def _run_tool(self, name: str, text: str) -> bool:
        """执行一个本地工具；返回 True = 这条已经办完（调用方直接 return）。"""
        # 插件工具优先（用户自己写的，允许盖过内置同名工具）
        try:
            for pt in self.plugins.tools():
                if pt["name"] != name:
                    continue
                from . import tools as T
                T.bump(TOOL_USED_FILE, name)          # 先记账再动手
                try:
                    return bool(pt["handler"](text))
                except Exception as exc:
                    self.log.warning("插件工具 %s 出错：%r" % (name, exc))
                    return False
        except Exception:
            pass
        if name != "meme":
            return False
        from . import tools as T
        T.bump(TOOL_USED_FILE, "meme")          # ★ 先记账再动手（失败也别反复撞配额）
        try:
            if self._send_sticker("表情包", "工具路由：" + (text or "")[:20]):
                return True
        except Exception as exc:
            self.log.warning("工具 meme 出错：%r" % (exc,))
        self.log.info("工具 meme 没发成 → 退回普通回复")
        return False

    def _agent_cfg(self):
        return getattr(self.cfg, "agent", None)

    def _agent_count(self) -> int:
        """今天已经转给 agent 几件活儿（落盘，重启不清零）。"""
        try:
            with open(AGENT_USED_FILE, encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:
            d = {}
        today = time.strftime("%Y-%m-%d")
        return int((d if isinstance(d, dict) else {}).get(today, 0) or 0)

    def _agent_bump(self) -> None:
        from .atomic import atomic_write_json
        today = time.strftime("%Y-%m-%d")
        try:
            with open(AGENT_USED_FILE, encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:
            d = {}
        if not isinstance(d, dict):
            d = {}
        d[today] = int(d.get(today, 0) or 0) + 1
        for k in sorted(d)[:-7]:            # 只留最近 7 天
            d.pop(k, None)
        try:
            atomic_write_json(AGENT_USED_FILE, d)
        except Exception as exc:
            self.log.warning("记 agent 用量失败：%s" % exc)

    def _agent_should_take(self, text) -> bool:
        """主人这句话是不是「让它办事」（查资料 / 考据这类）。

        ⚠️ 只在"主人自己喊它名字"那条路上问 —— 群友 @ 它**不走 agent**
        （否则群里任何人都能指挥一个有 bash/文件/手机权限的 agent）。
        """
        cfg = self._agent_cfg()
        if cfg is None or not bool(getattr(cfg, "enabled", False)):
            return False
        limit = int(getattr(cfg, "max_per_day", 20) or 20)
        if self._agent_count() >= limit:
            self.log.info("今天转给 agent 的活儿已到上限（%d 件），这件不转" % limit)
            return False
        t = normalize(text or "")
        for k in (getattr(cfg, "triggers", None) or []):
            kk = normalize(str(k))
            if kk and kk in t:
                return True                    # ① 强制后门：说了这些词就一定转
        # ② ★ 2026-10-09（用户要求）：「我让小a干点什么事儿他干不了，他再把活儿交给你」
        #    —— 不用再记触发词：让它自己花一次很轻的调用判断"这条我办不办得动"
        #    （`brain.classify_task`，约 1 秒、只回一个词）。办不动才转给 agent。
        if bool(getattr(cfg, "auto", True)) and getattr(self, "brain", None) is not None:
            try:
                if self.brain.classify_task(text):
                    self.log.info("它判断这条自己办不了 → 交给 agent：%s" % text)
                    return True
            except Exception as exc:
                self.log.warning("判断要不要找 agent 时出错（按「自己答」处理）：%s" % exc)
        return False

    def _ask_agent(self, text) -> str:
        """把活儿转给 DSH 的 headless agent，返回它的答复（失败给空串）。

        ⚠️ 这一步是**阻塞**的（最多 `agent.timeout` 秒，默认 180）—— 期间机器人
        不读屏，群里那段时间的消息会漏。所以超时别调太大，日志/浮窗都会写明。
        """
        from . import agentask
        cfg = self._agent_cfg()
        timeout = float(getattr(cfg, "timeout", 180) or 180)
        max_chars = int(getattr(cfg, "reply_max_chars", 220) or 220)
        self.log.info("交给 agent 去办（最多等 %.0f 秒）：%s" % (timeout, text))
        self._hud_note("交给 agent 去办", (text or "")[:44])
        self._agent_bump()
        t0 = time.time()
        got = agentask.ask(text, timeout=timeout, max_chars=max_chars)
        self.log.info("agent %s（%.1fs）"
                      % (("答复了 %d 字" % len(got)) if got else "没答复",
                         time.time() - t0))
        return got or ""

    def _mention_keyword_defers(self, rule, text) -> bool:
        """被 @ 时，这条关键词规则要不要让位给模型？

        ★ 2026-10-12：规则写了 `mention_ok: true` 的**不让位** —— 这是给
        「看看腿 / 看看腹肌 / 今日美图」这类**动作指令**开的口子。背景：群里习惯
        每次都带 @，而 2026-10-09 的"被 @ 以整句为主"判据会把关键词让给模型 ——
        实测 23:11 那条 `@我（群昵称）看看腿`（读屏**读全了**）只回了句
        「拿竞价区给本喵看腿…」，**图没发**，也就是说这个功能在群里等于失效。
        其余规则维持原口径（整句不"全是关键词"就让位，见 `_keyword_is_whole`）。
        """
        if getattr(rule, "mention_ok", False):
            return False
        return not self._keyword_is_whole(rule, text)

    def _keyword_is_whole(self, rule, text) -> bool:
        """被 @ 时，这句话是不是「基本就是关键词」——只有这种才走固定话术。

        ★ 2026-10-09（续24 用户口径）：「艾特他的时候以整句话为主，尽量不要触发
        关键词，除非那句话里全是关键词」。

        做法：把这条规则的词从消息里抠掉，看还剩几个**实义字**（汉字/字母/数字；
        标点、空格、下划线不算）。剩 ≤ `_KEYWORD_WHOLE_LEFT`(2) 个字 =
        「全是关键词」，其余情况一律交给模型去理解整句：
            「骚」             → 剩 0 个    → 固定话术 ✓
            「好骚啊」          → 剩 2 个    → 固定话术 ✓
            「6666」           → 剩 0 个    → 固定话术 ✓
            「看见内裤就说骚…」  → 剩 20+ 个  → 交给模型 ✓
        为什么宁可变长：2026-10-09 实测，用户问「…昵称你找找原话」就是被「骚」
        抢成一句梗、正事没人答 —— 被点名时"答到点上"比"省一次模型调用"重要得多。
        """
        def _squeeze(s):
            return re.sub(r"[\s\W_]+", "", normalize(s or ""))
        rest = _squeeze(text)
        for kw in (getattr(rule, "keywords", None) or []):
            k = _squeeze(kw)
            if k:
                rest = rest.replace(k, "")
        return len(rest) <= _KEYWORD_WHOLE_LEFT

    def handle(self, msg: Message, snap: Snapshot) -> bool:
        text = normalize(msg.text)
        if not text:
            return False
        # ★ 2026-10-12：插件钩子（零 Token 的本地扩展点，见 wxauto/plugins.py）。
        #   ⚠️ 排在关键词规则**前面** —— 插件是用户自己写的，优先级该比内置高；
        #   但整段单独兜异常：插件写坏了，后面的内置流程照走。
        try:
            _pg = self.plugins.run_message(
                text, str(getattr(msg, "sender", "") or ""),
                bool(getattr(msg, "is_self", False)))
        except Exception as exc:
            self.log.warning("插件钩子整体出错：%r" % (exc,))
            _pg = None
        if _pg:
            self.log.info("插件回了一条：%s" % _pg[:40])
            return self._reply(_pg, "插件回的")

        behavior = self.cfg.behavior
        mentioned = self.brain.is_mention(text)
        hard_block = self.brain.should_skip(text, hard=True)
        # ★ 2026-10-08 审查修复（S2）：硬避雷词原来**只挡 @ 那条路**（判断写在
        #   下面的 @我 分支里），关键词规则完全不看它 —— 于是"借钱/投资/政治"
        #   这类消息只要顺手命中一条规则（「问号」「笑死」之类）就会被回。
        #   设计意图是「连被 @ 都不回」，那就该在三条路**之前**一刀切掉。
        if hard_block:
            self.log.info(f"不接（硬避雷：{hard_block}）：{text}")
            return False

        # 1) 关键词 —— 模板回复，0 Token
        #    被 @ 时跳过"泛化"关键词（问号 / 6 / 恋 这类），
        #    否则别人问"@你 你觉得呢？"会被"问号"规则抢走，回一个"？"。
        if bool(getattr(behavior, "reply_on_keyword", True)):
            skip = (lambda r: r.mention_exempt) if mentioned else None
            rule = self.matcher.match(text, skip=skip)
            # ★ 2026-10-09（续24 用户口径）：「艾特他的时候以整句话为主，尽量不要
            #   触发关键词，除非那句话里全是关键词」。
            #   原来被 @ 时只有 `mention_exempt: true` 的规则让位，其余照抢 ——
            #   实测「看见内裤就说骚，鲁迅…昵称你找找原话」被「骚」抢成一句梗，
            #   正事没人答。现在被 @ 时先看整句（见 `_keyword_is_whole`）。
            # ★ 2026-10-12：`mention_ok: true` 的规则（看看腿/看看腹肌/今日美图）
            #   不在让位之列 —— 被 @ 也照常触发发图，见 `_mention_keyword_defers`。
            if rule is not None and mentioned and self._mention_keyword_defers(rule, text):
                self.log.info("被 @ → 以整句为主，跳过关键词「%s」交给模型 ← %s"
                              % (rule.name, text))
                rule = None
            if rule is not None:
                ok, why = self.governor.rule_allowed(rule.name, rule.cooldown)
                if not ok:
                    self.log.info(f"跳过（{why}） ← {text}")
                elif rule.is_image:
                    return self._send_sticker(
                        rule.image_tag, f"关键词「{rule.name}」← {text}", rule.name,
                        mention=mentioned)
                elif rule.is_meitu:
                    # ★ 2026-10-08：meitu 规则的 replies 现在是**配文候选**
                    #   （跟着图说的那句），不是单独的文字回复 —— 见
                    #   _send_caption_after_image。留空就只发图。
                    return self._send_meitu(
                        rule.meitu_source, f"关键词「{rule.name}」← {text}", rule.name,
                        mention=mentioned, captions=rule.replies)
                else:
                    reply = self.matcher.reply_for(rule)
                    if reply:
                        return self._reply(reply, f"关键词「{rule.name}」← {text}",
                                           rule.name, no_meow=bool(rule.no_meow),
                                           mention=mentioned)

        # 2) 有人在叫我 —— 必回，走模型
        #    （硬避雷已在 handle 开头一刀切掉，这里不再重复判断）
        if bool(getattr(behavior, "reply_on_mention", True)) and mentioned:
            # ★ 2026-10-09 用户要求：「被艾特的时候……一句话概括的话，他说的很没意思，
            #   你得展开讲讲，他才有意思，才能聊得下去，而不是一句话敷衍」。
            #   ⚠️ 这条路原来有**四道墙**同时卡着，只松一道没用：
            #     ① `llm.max_tokens`(60) —— 模型的输出上限，中文约 40~50 字。
            #        这是最硬的一道（PPT 点评配的 100 字上限就是被它卡死的）；
            #     ② `behavior.max_reply_chars`(40) —— `_clean` 超了是**直接丢弃**
            #        （不是截断），所以字多了反而变成"这条不回"；
            #     ③ 人设「一句话 3~15 个字，绝不写小作文」—— 模型自己不肯写长；
            #     ④ `_clean` 默认只取第一行 —— 想分两句说就只剩第一句。
            #   这里给 @ 单独一档，**平时闲聊仍然是短句**。
            _mt = int(getattr(getattr(self.cfg, "llm", None),
                              "mention_max_tokens", 0) or 0)
            _mc = int(getattr(behavior, "mention_max_chars", 0) or 0)
            # ★ 2026-10-10（用户口径：「被艾特让看看图的时候识图」）：有人 @ 它、
            #   旁边又有一张图 → **看图回答**，而不是干巴巴接一句文字（那会答非所问：
            #   人家问的是"这张图咋样"）。图拿不到 / 模型没给出内容时才退回普通回复。
            img = self._nearby_image(snap, msg)
            if img is not None:
                self.log.info("被 @ 的时候旁边有图（%dx%d），识图回答…"
                              % (int(img.img_w or 0), int(img.img_h or 0)))
                self._hud_note("机器人在看图", "有人 @ 它看这张图，正在识别")
                try:
                    got = self.brain.comment_on_image(
                        img.img_path, sender=msg.sender, snap=snap,
                        max_reply=_mc or 60, addressed=True,
                        addressed_text=msg.text)
                except Exception as exc:
                    got = None
                    self.log.warning("识图异常：%s: %s" % (type(exc).__name__, exc))
                if got:
                    return self._reply(got, f"@我+图：{text}", mention=True,
                                       max_chars=_mc)
                self.log.info("识图没给出内容，退回普通回复")
            reply = self.brain.compose(snap, msg, "有人@你", max_tokens=_mt,
                                       max_chars=_mc, expand=bool(_mc))
            if reply:
                return self._reply(reply, f"@我：{text}", mention=True,
                                   max_chars=_mc)
            self.log.info(f"@我但模型没给出回复：{text}")
            return False

        # 3) 主动开口 —— 本地打分够了才动用模型。这里分两条路（2026-10-05）：
        #    · 深夜（pace.proactive_window，默认 00:00-07:00）= 直接发（原行为，用户选了保留）
        #    · 白天 = **只备稿**，塞进待审队列等主人点头，一个字都不发
        if self.brain.available:
            skip = self.brain.should_skip(text, hard=False)
            if skip:
                if getattr(self.cfg.debug, "log_decisions", True):
                    self.log.debug(f"不接（{skip}）：{text}")
                return False
            score = self.brain.score_topic(text)

            if self._in_proactive_window():
                threshold = int(getattr(behavior, "proactive_min_score", 2))
                chance = float(getattr(behavior, "proactive_chance", 0.2))
                # ★ 2026-10-05：用户要求「恋爱/男女关系这类话题增加主动回复权重」。
                #   光把那些词的分数抬高只解决"够不够得着阈值"，抽签概率还是一刀切
                #   的 0.18 —— 分数再高也是同样的机会。这里让概率跟着分数走：
                #   刚好过线 = 原概率，分数翻倍 = 概率翻倍，最多放
                #   PROACTIVE_CHANCE_MAX 倍（默认 3，即 0.18 → 0.54）。
                #   想关掉（回到一刀切）：WX_PROACTIVE_SCORE_CHANCE=0。
                if PROACTIVE_SCORE_CHANCE and threshold > 0:
                    chance = min(chance * min(score / float(threshold),
                                              PROACTIVE_CHANCE_MAX), 0.9)
                if score < threshold or self.rng.random() >= chance:
                    return False
                # ★ 主动开口的额外闸门（用户要求：别扰民）
                if not self._proactive_ok():
                    return False
                # 深夜主动回复固定用猫娘人设（用户要求）
                if bool(getattr(behavior, "proactive_catgirl", True)):
                    self.brain.persona_mode = "fixed"
                    self.brain.persona_fixed = str(
                        getattr(behavior, "proactive_catgirl_name", "猫娘") or "猫娘")
                # ★ 2026-10-09 用户要求：主动插话"愿意多说就多说一点，但不要空话、
                #   不要复读、要能让人接得下去"。跟 @ 那档同一套做法 ——
                #   光改人设没用，`llm.max_tokens`(60) 会把话卡成一句短评
                #   （实测 47 条平均 9.8 字）。所以吃 proactive_max_* 两档，
                #   并让 user prompt 换成"接话档"（要钩子、禁空话）。
                _pmt = int(getattr(getattr(self.cfg, "llm", None),
                                   "proactive_max_tokens", 0) or 0)
                _pmc = int(getattr(behavior, "proactive_max_chars", 0) or 0)
                reply = self.brain.compose(snap, msg, f"话题对味(打分 {score})",
                                           max_tokens=_pmt, max_chars=_pmc,
                                           proactive=bool(_pmc))
                if reply:
                    return self._reply(reply, f"主动插话(打分 {score}) ← {text}",
                                       proactive=True)
                return False

            # ⚠️ 2026-10-07：这里原来还有一条「白天备稿」的路 —— 不在自动发窗口时
            #    只把想说的话写成草稿、一个字都不发，等主人回面板点头。
            #    proactive_window 改成**全天**之后它永远走不到（闸门恒不成立），
            #    已经按用户要求连同 drafts.py 那条线一起删掉（交接说明 §46.8）。
        return False

    # ------------------------------------------------------------ 主循环
    def _check_group_doc(self, snap) -> bool:
        """群友发的 PPT（吃瓜素材）→ 取文字 → 按人设点评 → 发群。

        为什么要人点一下：机器人没有「点开文件卡片」的能力（桥接只有
        send / snapshot / open 几个接口），所以下载那一步由人代劳；文件一
        落地这里就接管。落点、解析、去重全在 wxauto/ppt.py。

        返回 True 表示"这一轮已经出了话"，调用方应当重新读屏。
        """
        pcfg = getattr(self.cfg, "ppt", None)
        if not pcfg or not bool(getattr(pcfg, "enabled", False)):
            return False
        if not self.brain.available:
            return False

        now = time.time()
        if now < self._doc_cool_until:
            return False

        # ★ 2026-10-08 审查修复（C4/A12）：扫描节流。
        #   原来这里每轮都会往下走，而进去就是一次 `adb shell find` 扫三个微信
        #   目录 —— 当前 12~20 秒一帧，等于**没有任何 PPT 的时候也在每轮 fork
        #   一个 adb 进程**；adb 掉线时那次调用还要吃满 25 秒超时，把读屏节奏从
        #   12 秒拖成 37 秒（这段时间群里的消息全漏）。文件落点不会秒级出现，
        #   60 秒扫一次足够；有文件时照样下一轮就被发现。
        _scan_every = float(getattr(pcfg, "scan_interval", 60) or 60)
        if now - self._doc_scan_at < _scan_every:
            return False
        self._doc_scan_at = now

        # 每天上限（防"有人一口气发十份 PPT"刷屏）
        today = time.strftime("%Y-%m-%d")
        if self._doc_day != today:
            self._doc_day, self._doc_count = today, 0
        if self._doc_count >= int(getattr(pcfg, "max_per_day", 8) or 8):
            return False

        if self._docs is None:
            from .ppt import DocWatcher
            self._docs = DocWatcher(self.log, pcfg)

        try:
            entries = self._docs.poll()
        except Exception as exc:
            self.log.warning("扫 PPT 落点出错：%s: %s" % (type(exc).__name__, exc))
            return False
        if not entries:
            return False

        entry = entries[0]
        name = os.path.basename(str(entry.get("path") or ""))

        from .ppt import fetch_and_extract
        try:
            text = fetch_and_extract(
                entry, self.log, int(getattr(pcfg, "max_chars", 3000) or 3000))
        except Exception as exc:
            self._docs.mark_seen(entry)
            self.log.warning("取/解文档失败：%s: %s" % (type(exc).__name__, exc))
            return False

        if len((text or "").strip()) < 20:
            self._docs.mark_seen(entry)
            self.log.info("文档里没抽出多少字（%d 字），跳过：%s"
                          % (len(text or ""), name))
            return False

        self.log.info("发现群友的文档「%s」（%d 字节 → %d 字），点评中…"
                      % (name, int(entry.get("size") or 0), len(text)))

        # ★ 2026-10-07：顺手把这期瓜的主角记下来（当期瓜词表），免得每换一批人
        #   就得手工往 INTEREST_WORDS 里加名字（见 brain.learn_doc_people）。
        #   纯旁路：学不到就算了，点评照走。
        try:
            self.brain.learn_doc_people(text, name)
        except Exception as exc:
            self.log.warning("学本期瓜词失败（不影响点评）：%s" % exc)
        rmax = int(getattr(pcfg, "reply_max_chars", 100) or 100)
        try:
            reply = self.brain.comment_on_ppt(text, doc_name=name, snap=snap,
                                              max_reply=rmax)
        except Exception as exc:
            self._docs.mark_seen(entry)
            self.log.warning("点评接口异常：%s: %s" % (type(exc).__name__, exc))
            return False
        if not reply:
            self._docs.mark_seen(entry)
            self.log.info("模型没写出点评（多半超长被丢了），跳过")
            return False

        # ★ 发送**前**就记账（2026-10-07 踩坑后改的，别再改回去）：
        #   _reply 的发送自检（_verify_sent）失败时会返回 False，但消息
        #   **其实可能已经进群了** —— 那时若"没发成功就下轮重试"，群里就会看到
        #   同一份 PPT 被点评第二次（实测那天第二条差点重复）。
        #   漏发一条只是可惜，重复发群是真事故：宁可漏、不可重。
        self._docs.mark_seen(entry)
        ok = self._reply(reply, "群友发的文档「%s」" % name, max_chars=rmax)
        if ok:
            self._doc_count += 1
            self._doc_cool_until = now + float(
                getattr(pcfg, "cooldown_secs", 120) or 0)
        return ok

    # ------------------------------------------------ 读聊天记录卡片（被叫才读）
    def _looks_like_record_request(self, text: str) -> bool:
        """这句话是不是在叫它「把那张聊天记录读一下」？（本地词表，不花模型调用）

        ★ 2026-10-10 加。跟 `_looks_like_image_request` 一个路数，但要求**两个
        词表都中**：既要提到聊天记录，又要带动作 / 疑问词。为什么这么严 ——
        这一步会替主人点屏幕（点开卡片 → 进详情页 → 再退出来），误触的代价不是
        "多说一句话"，而是"在他的手机上乱点东西"。
        """
        t = normalize(text or "")
        if not any(w in t for w in _RECORD_WORDS):
            return False
        return any(w in t for w in _RECORD_HINTS)

    def _read_record_and_comment(self, text: str, snap,
                                 max_chars: int = 0) -> bool:
        """被叫时点开屏幕上的聊天记录卡片，读完按人设点评发群。

        返回 True = 这条指令已经处理掉（**成功失败都回了一句**），调用方不要再
        往下走 agent / compose —— 否则读不到时 agent 还会再答一遍，群里就看到两句
        风马牛不相及的话。
        """
        rcfg = getattr(self.cfg, "record", None)
        if rcfg is not None and not bool(getattr(rcfg, "enabled", True)):
            self.log.info("读聊天记录被配置关掉了（record.enabled=false）")
            return False
        read = getattr(self.sender, "read_record", None)
        if not callable(read):
            self.log.info("当前发送端不支持读聊天记录（不是手机端？）")
            return False
        self.log.info("你叫它读聊天记录，去点开卡片……（约 20~30 秒）")
        self._hud_note("机器人在读聊天记录", "点开卡片 → 整页 OCR → 读完自动退回")
        try:
            got = read()
        except Exception as exc:
            got = None
            self.log.warning("读聊天记录异常：%s: %s" % (type(exc).__name__, exc))
        if not got:
            err = str(getattr(self.sender, "last_record_error", "") or "读不到")
            self.log.info("读聊天记录失败：%s" % err)
            self._reply("这张聊天记录本喵没读到（%s）" % err,
                        "读聊天记录失败：" + text, mention=True)
            return True
        title = str(got.get("title") or "聊天记录")
        content = str(got.get("text") or "")
        self.log.info("读到了 %r：%d 字" % (title, len(content)))
        try:
            self._save_record(title, content)
        except Exception as exc:
            self.log.warning("存聊天记录失败：%s" % exc)
        if not content.strip():
            self._reply("这张聊天记录里没读出文字（可能整条都是图片）",
                        "读聊天记录空：" + text, mention=True)
            return True
        limit = int(max_chars or getattr(rcfg, "reply_max_chars", 0)
                    or getattr(self.cfg.behavior, "max_reply_chars", 40))
        cmt = None
        try:
            cmt = self.brain.comment_on_record(title, content, snap=snap,
                                               max_reply=limit)
        except Exception as exc:
            self.log.warning("点评聊天记录异常：%s: %s" % (type(exc).__name__, exc))
        if cmt and not self.brain.is_mention(cmt):
            return self._reply(cmt, "读完聊天记录点评：" + text,
                               mention=True, max_chars=limit)
        self.log.info("读完聊天记录但没生成出能发的点评：%s" % text)
        return True

    def _save_record(self, title: str, content: str) -> str:
        """把读到的原文存一份（纯文本，事后能核查它到底读到了什么）。"""
        os.makedirs(RECORD_ARCHIVE_DIR, exist_ok=True)
        safe = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", title or "记录")[:40]
        path = os.path.join(RECORD_ARCHIVE_DIR,
                            "%s-%s.txt" % (time.strftime("%Y%m%d-%H%M%S"), safe))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# %s\n\n%s\n" % (title or "", content or ""))
        return path

    # ------------------------------------------------ 往回翻补读（只在被叫时翻）
    def _looks_like_backfill_request(self, text: str) -> bool:
        """这句话是不是在叫它「往上翻翻，看看刚才漏了啥」？"""
        t = normalize(text or "")
        if not any(w in t for w in _BACKFILL_WORDS):
            return False
        return any(w in t for w in _BACKFILL_HINTS)

    def _backfill_and_reply(self, text: str, snap, max_chars: int = 0) -> bool:
        """被叫时往上翻两屏补读，把内容交给模型说一句发群。

        返回 True = 这条指令处理掉了（成功失败都回了一句）。
        """
        bf = getattr(self.sender, "backfill", None)
        if not callable(bf):
            self.log.info("当前发送端不支持补读（不是手机端？）")
            return False
        self.log.info("你叫它往回翻补读……（约 20~30 秒）")
        self._hud_note("机器人在往回翻补读", "往上翻 2 屏，读完自动切回最新")
        group = str(getattr(self.cfg.target, "group", "") or "")
        try:
            got = bf(2, group)
        except Exception as exc:
            got = None
            self.log.warning("补读异常：%s: %s" % (type(exc).__name__, exc))
        if not got:
            err = str(getattr(self.sender, "last_backfill_error", "") or "翻不动")
            self.log.info("补读失败：%s" % err)
            self._reply("往回翻不动（%s）" % err, "补读失败：" + text, mention=True)
            return True
        frames = got.get("frames") or []
        lines, seen = [], set()
        for fr in frames:
            for m in fr:
                if m.get("is_self"):
                    continue
                t = normalize(str(m.get("text") or ""))
                if not t:
                    continue
                key = (str(m.get("sender") or ""), t)
                if key in seen:          # 相邻两屏有重叠，去重
                    continue
                seen.add(key)
                who = str(m.get("sender") or "").strip()
                lines.append(("%s：%s" % (who, t)) if who else t)
        if not lines:
            self._reply("往上翻了两屏，没读到什么内容", "补读空：" + text, mention=True)
            return True
        self.log.info("补读读到 %d 条（%d 屏）" % (len(lines), len(frames)))
        limit = int(max_chars
                    or getattr(getattr(self.cfg, "record", None), "reply_max_chars", 0)
                    or getattr(self.cfg.behavior, "max_reply_chars", 40))
        cmt = None
        try:
            cmt = self.brain.summarize_backfill("\n".join(lines[-40:]), snap=snap,
                                                max_reply=limit)
        except Exception as exc:
            self.log.warning("补读总结异常：%s: %s" % (type(exc).__name__, exc))
        if cmt and not self.brain.is_mention(cmt):
            return self._reply(cmt, "补读总结：" + text, mention=True, max_chars=limit)
        self.log.info("补读总结没生成出来：%s" % text)
        return True

    # ------------------------------------------------------------ 识图（群友发的照片）
    def _looks_like_image_request(self, text: str) -> bool:
        """这句话本身是不是在"让你看图"？（本地词表，不花模型调用）

        ★ 2026-10-09 晚加。用户的抱怨：「群里我让你查一下，但是机器人以为是识别
        图片儿呢。我可没让机器人识别图片，我只是让机器人查一下」—— 病根是
        `handle_own` 里"旁边有图就先识图"，把办事请求抢走了。
        判据故意用**具体词**（看图 / 这图 / 照片 / 截图…），不用单字「看」：
        「看看这个网站」不该被当成看图请求。
        """
        t = normalize(text or "")
        return any(w in t for w in _IMAGE_ASK_WORDS)

    def _nearby_image(self, snap, anchor, allow_self: bool = False):
        """找「这条消息旁边那张图」—— 有人 @ 它让它看图时用。

        ★ 2026-10-10 用户改的口径：**主动识图关掉，改成"被 @ 让看图的时侯才识图"**。
        微信里 @ 和图片是**两条独立的消息**，所以只能按位置找：先看 @ 之后
        （「@你 看看这张」然后发图，最常见），再看 @ 之前一格
        （先把图甩出来再 @ 也常见）。前后各看 2 条，再远就不是同一个回合了。

        ``allow_self=True`` 给"主人自己喊昵称"那条路用 —— 机器人和主人共用一个
        微信号，主人发的图也是 is_self，不放行就永远认不到他自己发的图。
        返回 Message 或 None。
        """
        msgs = list(getattr(snap, "messages", None) or [])
        if not msgs:
            return None
        i = next((k for k, m in enumerate(msgs) if m is anchor), None)
        if i is None:
            return None
        for d in (1, -1, 2, -2):          # 先往后看（先说后发图），再往前看
            j = i + d
            if not (0 <= j < len(msgs)):
                continue
            m = msgs[j]
            if getattr(m, "kind", "") != "image":
                continue
            if not getattr(m, "img_path", ""):
                continue                   # 桥接没取到图（太小 / 取图失败）
            if m.is_self and not allow_self:
                continue                   # 群友的图才是"别人让它看的"
            return m
        return None

    def _collect_image_candidates(self, snap) -> list:
        """挑出这一轮"还没处理过"的图片消息（**只挑别人发的**）。

        ★ 2026-10-10 用户要求：「群里发照片他可以识别，然后能点评的他点评」。
        走自己那本账（``_img_seen``），跟文字那本 state 完全分开：
        · 这里**只挑不记账** —— 真正点评（或决定跳过）时才 ``mark``，
          否则同轮先回了别人的话，这张图就永远轮不到了；
        · ``is_self`` 一律跳过：机器人和主人共用一个微信号，自己发出去的
          表情包/美图不能被当成"群友发来的照片"再点评一遍。
        """
        # 机器人刚发过图（表情包 / 美图）→ 屏幕上那张多半就是它自己发的，
        # 这一小段时间内不当"群友发的照片"看（见 _send_sticker 里的说明）。
        if time.time() - float(getattr(self, "_img_self_sent_at", 0.0) or 0.0) < 15:
            return []
        out = []
        for m in snap.messages:
            if getattr(m, "kind", "") != "image" or m.is_self:
                continue
            if not getattr(m, "img_path", ""):
                continue          # 桥接没取到图（太小/取图失败）→ 看不了
            if self._img_seen.is_new(m):
                out.append(m)
        return out

    def _image_proactive(self) -> bool:
        """主动识图开着吗（`image.proactive`，2026-10-10 用户要求默认**关**）。

        关着的时候：它**不会**对群里随便发的照片开口；只有被 @ / 被喊「昵称」
        并且旁边有图时才识图（见 handle / handle_own 里的 `_nearby_image`）。
        """
        icfg = getattr(self.cfg, "image", None)
        return bool(icfg and getattr(icfg, "enabled", False)
                    and getattr(icfg, "proactive", False))

    def _check_group_image(self, snap, cands) -> bool:
        """群友发了照片 → 看一眼，值得点评就开口。返回 True = 这一轮出了话。

        节流（都在 config.yaml 的 ``image`` 段，用户定的口径）：
        ``cooldown_secs`` 30 秒一张、``max_per_day`` 30 张、
        ``sender_cooldown_secs`` 同一个人 5 分钟内只点评一张。

        什么时候记账（``_img_seen.mark``）—— 跟 PPT 那条路同一个道理，
        但更细，因为识图要花钱调视觉模型：
        · 发出去了 / 模型说"这张不值得点评" → **记**（别再花钱重看）；
        · 同一个发送者还在冷却里 → **记**（我们就是不想点评他这几张）；
        · 模型调用**失败**（网络/超时） → **不记**，下一轮还能重试。
        """
        icfg = getattr(self.cfg, "image", None)
        if not icfg or not bool(getattr(icfg, "enabled", False)):
            return False
        if not cands or not self.brain.available:
            return False
        # 候选在 _collect_image_candidates 里已经筛过一遍，这里再筛一次：
        # 万一调用方把"已经点评过"的图又递进来（比如它自己攒了队列），
        # 也不会白花一次视觉调用。
        cands = [m for m in cands if self._img_seen.is_new(m)]
        if not cands:
            return False

        now = time.time()
        if now < self._img_cool_until:
            return False

        today = time.strftime("%Y-%m-%d")
        if self._img_day != today:
            self._img_day, self._img_count = today, 0
        max_day = int(getattr(icfg, "max_per_day", 30) or 30)
        if self._img_count >= max_day:
            self.log.debug("今天识图点评已到上限 %d 张，跳过" % max_day)
            return False

        # 取**最新**那张：群里连发好几张时，点评刚发的那张最像真人
        msg = cands[-1]
        sender = normalize(getattr(msg, "sender", "") or "")
        scool = float(getattr(icfg, "sender_cooldown_secs", 300) or 0)
        if scool and sender:
            last = float(self._img_sender_at.get(sender, 0.0) or 0.0)
            if now - last < scool:
                self._img_seen.mark(msg)
                self.log.debug("图不点评（%s 刚发过图，冷却还剩 %.0f 秒）：%s"
                               % (sender, scool - (now - last),
                                  os.path.basename(msg.img_path)))
                return False

        try:
            size = "%dx%d" % (int(msg.img_w), int(msg.img_h))
        except Exception:
            size = "?"
        self.log.info("群友%s发了张图（%s），识图中…"
                      % (("「%s」" % sender) if sender else "", size))
        self._hud_note("机器人在看图", "「%s」发的图片，正在识别" % (sender or "群友"))

        rmax = int(getattr(icfg, "reply_max_chars", 60) or 60)
        mtok = int(getattr(icfg, "max_tokens", 0) or 0)
        try:
            reply = self.brain.comment_on_image(msg.img_path, sender=sender,
                                                snap=snap, max_reply=rmax,
                                                max_tokens=mtok)
        except Exception as exc:
            self.log.warning("识图接口异常：%s: %s" % (type(exc).__name__, exc))
            return False                       # 不记账 → 下轮重试

        # ★ 「模型调用成功但选择不说话」（表情包/截图/广告这些不值得点评的）
        #   跟「调用失败」必须分开：前者要记账（别再花钱重看），后者要留着重试。
        #   brain 用 last_call_ok 告诉我们这次到底调没调通。
        if not bool(getattr(self.brain, "last_call_ok", True)):
            self.log.info("识图没调通（网络/超时），这张留到下一轮再看")
            return False

        self._img_seen.mark(msg)
        # ★ 2026-10-10 晚（用户报「机器人一直在识别阿兰发的图」）：发送者冷却
        #   **必须在这里就记**，不能等到 `_reply` 之后 —— 原来赋值在最后，
        #   而模型判断"不值得点评（表情包/截图）"时在那之前就 return 了，
        #   于是这条 300 秒冷却对**表情包从来没生效过**（阿兰发的恰好都是
        #   表情包，所以同一张图被反复送进视觉模型）。模型已经调通过 = 这次
        #   钱花过了，就该算"这个人最近发过图"，不论点评与否。
        if sender:
            self._img_sender_at[sender] = now
        if not reply:
            self.log.info("这张图不值得点评（模型判断是表情包/截图/海报之类），跳过")
            return False

        ok = self._reply(reply, "群友发的照片（%s）" % (sender or "群友"),
                         max_chars=rmax)
        # 无论发没发成，都按"这一张已经处理过"算 —— _reply 的自检失败也可能
        # 其实已经进群了（同 PPT 那条路的教训：宁可漏，不可重）。
        if ok:
            self._img_count += 1
            self._img_cool_until = now + float(
                getattr(icfg, "cooldown_secs", 30) or 0)
        return ok

    def _record_chat(self, snap) -> None:
        """把这一帧的消息记进群聊流水（控制面板的「群里在说什么」用它）。

        ⚠️ 这是**纯粹的旁路功能**：任何异常都不许影响机器人的正常干活，
        所以这里自己吞掉、并且只提醒一次（日志刷屏比出错更烦）。
        """
        try:
            self.chatlog.record(snap)
        except Exception as exc:
            if not self._chatlog_warned:
                self._chatlog_warned = True
                self.log.warning("聊天流水记录失败（不影响机器人）：%s: %s"
                                 % (type(exc).__name__, exc))

    def step(self) -> None:
        # 每轮先按时间定人设（白天贴吧老哥 / 深夜猫娘），再读屏
        self._apply_persona_schedule()
        snap = self.reader.read()
        if snap is None:
            why = str(getattr(self.reader, "last_error", "") or "")
            self.log.warning("读不到画面%s，跳过本轮"
                             % (f"：{why}" if why else
                                "（PC 上通常是微信窗口被最小化）"))
            self._read_failed = True
            self._read_fails = int(getattr(self, "_read_fails", 0) or 0) + 1
            return
        if getattr(self, "_read_failed", False):
            # 恢复也要留一条：机器人正常轮询是**不打日志**的，不标记的话面板从日志尾部
            # 只能看到最后一条错误，会一直误报「读不到画面」（2026-10-03 踩过）
            self.log.info("读屏恢复正常")
            self._read_failed = False
            self._read_fails = 0

        if not self.reader.in_target_group(snap):
            # 2026-10-04：用户把微信拿到手机上「看一眼」、再切走之后，桥接会把微信
            # 拉回虚拟屏（snapshot() 里的 ensure_wechat_front），但界面停在用户最后
            # 看的那个会话上 —— 老逻辑只会一直「待命中」装死，用户看到的就是
            # 「机器人又坏了」。所以连续几轮都不在目标群就自己切回去。
            # 轮间隔 = poll_interval + 读屏 ≈ 3~5 秒，3 轮 ≈ 10~15 秒。
            OFF_GROUP_TRIES = 3
            self._off_group = getattr(self, "_off_group", 0) + 1
            if not self._group_warned:
                self.log.info(f"当前会话是「{snap.chat_title}」，不是目标群，待命中…")
                self._group_warned = True
            if self._off_group >= OFF_GROUP_TRIES:
                self._off_group = 0
                grp = str(getattr(self.cfg.target, "group", "") or "")
                if grp:
                    # ★ 2026-10-10：切群失败要退避（见文件顶部 REGROUP_FAIL_MAX 处的说明）。
                    #   冷却期间连一次 /open 都不调 —— /open 内部会抢物理屏输入焦点，
                    #   那正是"用户没法打字"的来源。
                    _allow, _left = self._regroup_gate()
                    if not _allow:
                        if not self._regroup_cool_logged:
                            self._regroup_cool_logged = True
                            self.log.warning(
                                "自动切群冷却中（还剩约 %d 秒）——期间不再抢键盘切群"
                                % int(_left))
                        return
                    self.log.info(f"连续 {OFF_GROUP_TRIES} 轮不在目标群，自动切回「{grp}」…")
                    ok = False
                    try:
                        ok = self.reader.open_chat(grp)
                        self.log.info("自动切回%s" % ("成功" if ok else "失败"))
                    except Exception as exc:
                        self.log.warning(f"自动切回目标群异常：{type(exc).__name__}: {exc}")
                    self._regroup_note_result(ok)
            return
        self._group_warned = False
        self._off_group = 0
        # 真回到目标群了 → 把切群的失败计数一起清掉
        self._regroup_fails = 0
        self._regroup_cool_count = 0
        self._regroup_cooldown_until = 0.0
        self._regroup_cool_logged = False
        # 在目标群 → 顺手把这一屏的消息记进「群里在说什么」
        self._record_chat(snap)

        # 群友发的 PPT（吃瓜素材）：用户在微信里点开、文件落地后由这里接管。
        # 放在处理消息之前 —— 点评是"这一轮唯一要发的一条"，发完就重新读屏。
        if self._check_group_doc(snap):
            return

        # ★ 2026-10-10（识图）：群友发的照片**先挑候选**（只读账、不记账），
        #   真正的点评放在这一轮的文字都处理完之后 —— @ 我 / 关键词优先，
        #   别让一张图把正经回复挤掉。挑的时候用图片自己那本账（_img_seen），
        #   所以"这一轮没轮上"的图下一轮还在候选里（见 _collect_image_candidates）。
        # ★ 2026-10-10 用户改口径：**主动识图默认关**（`image.proactive: false`），
        #   改成"被 @ 让它看图的时候才识图" —— 那条路在 handle / handle_own 里。
        #   这里留着开关：哪天想让它自己对群里的照片开口，把 proactive 改成 true。
        img_cands = []
        if self._image_proactive():
            img_cands = self._collect_image_candidates(snap)

        fresh = [m for m in snap.messages if self.state.is_new(m)]
        # ★ 2026-10-10 自适应读屏节奏的输入：这一轮**有没有**新消息。
        #   必须在任何 return 之前记（下面处理消息时随时会 return），否则
        #   "刚聊完就安静下来"那种情况永远收敛不到慢速档。
        self._calm_rounds = (0 if fresh
                             else int(getattr(self, "_calm_rounds", 0) or 0) + 1)
        if fresh:
            # ★★ 2026-10-10 评审修复（§1.1/§2.1）：**判一条、标一条**。
            #   原来是"先把这一波全部 mark、再逐条处理"，而下面只要有一条真的
            #   发出回复就 `return`（"发了就重读，别基于旧画面继续"）—— 于是同一
            #   波里排在它后面的消息**已经被标成已见**，下一轮 `is_new()` 直接
            #   False，永远不会被处理（评审实测：3 条一波、回完第一条、其余 2 条
            #   永久丢）。群聊越热闹（一帧 5~11 条）丢得越多，而那正是最该接话
            #   的时候。现在 `mark` 挪到"确定要处理这一条"的那一刻；被 return
            #   跳过、压根没轮到的消息保持"新"，下一轮读屏还会进 fresh。
            #   ⚠️ 判过但没出话的（打分不够/被冷却/开关关了）仍然 mark —— 语义
            #      是"这条我看过了"，与修复前一致，否则同一句会被反复评估、
            #      白烧模型钱。

            # ★ 2026-10-09 用户要求：「我要机器人能发图出去。这样**我自己也能触发我自己
            #   关键词**了」—— 他要在群里自己发「看看腿」当遥控器。
            #   原来自己发的消息在这里被 `not m.is_self` 一律跳过：机器人和主人**共用
            #   同一个微信号**，分不出"主人手动打的"和"自己刚发的"，只能全不碰
            #   （防自问自答）。现在**只对自己发的消息放行"关键词规则"这一条路**
            #   （见 handle_own）：不走模型、不走主动插话，而且照样要过
            #   `_is_my_echo`（机器人自己刚发的话不算）和 state 去重（同一条只处理一次）。
            own = [m for m in fresh
                   if m.is_self and normalize(m.text)
                   and not self._is_my_echo(m.text, self._mine_window_own)]

            if bool(getattr(self.cfg.debug, "verbose", False)):
                for m in fresh:
                    self.log.debug(f"  新消息 {m!r}")

            # 只看别人说的话（读屏偶尔会把自己刚发的判成别人的 → 再兜一层）
            pending = [m for m in fresh
                       if not m.is_self and normalize(m.text)
                       and not self._is_my_echo(m.text)]

            # @ 我的排在最前面处理，免得被同一波里的闲聊挤掉
            mentions = [m for m in pending if self.brain.is_mention(m.text)]
            mention_ids = {id(m) for m in mentions}
            others = [m for m in pending if id(m) not in mention_ids]

            # 处理顺序与修复前完全一致：自己发的 → @ 我的 → 其他人
            for m in own + mentions + others:
                self.state.mark(m)        # ← 判一条、标一条（见上面的说明）
                if m.is_self:
                    if self.handle_own(m, snap):
                        return
                elif self.handle(m, snap):
                    return  # 发了就重读，别基于旧画面继续

        # 文字都处理完了（这一轮没出话）→ 主动识图那条路（默认关着，见上）
        if img_cands and self._check_group_image(snap, img_cands):
            return

    def run(self, max_rounds: Optional[int] = None) -> None:
        """常驻主循环。``max_rounds`` 只给测试用（跑够轮数就返回）。"""
        self.log.info("=" * 62)
        self.log.info(f"  微信自动回复机器人  群：{self.cfg.target.group}")
        self.log.info(f"  窗口：{self.cfg.target.window_title}")
        self.log.info(f"  模型：{self.cfg.llm.model if self.brain.available else '未配置 API Key（只跑关键词）'}")
        self.log.info(f"  演练模式：{'开' if self.dry_run else '关'}")
        self.log.info(f"  关键词规则：{len(self.matcher.rules)} 条")
        if self.stickers_enabled:
            n_img = sum(1 for r in self.matcher.rules if r.is_image)
            self.log.info(f"  发表情包的规则：{n_img} 条")
            self.log.info(f"  {self.stickers.summary()}")
        self.log.info("=" * 62)

        self.reader.attach()
        self.log.info("读取层预热中…")
        self.reader.warmup()

        snap = self.reader.read()
        if snap is None:
            self.log.error("第一次读屏就失败了（PC 上通常是微信窗口被最小化）")
            return
        self.log.info(f"当前会话：{snap.chat_title or '(没识别到标题)'}")
        if self.reader.in_target_group(snap):
            # 启动时先把屏幕上这几条记进流水，面板不至于一开始就空白
            self._record_chat(snap)
        self._restore_mine_said()
        n = self.state.prime(snap.messages)
        # 图片那本账同样打底：屏幕上原本就挂着的照片**不当新图**，否则一开机
        # 就对着几分钟前那张图点评一句（2026-10-10 识图功能）。
        ni = self._img_seen.prime([m for m in snap.messages
                                   if getattr(m, "kind", "") == "image"])
        self.log.info(f"启动完成，已忽略屏幕上原有的 {n} 条消息"
                      f"（其中图片 {ni} 张），只看之后的新消息")

        # 急停开关
        self.kill.arm()
        ways = []
        if self.kill.active_hotkey:
            ways.append(f"按 {self.kill.active_hotkey}")
        if self.kill.stop_file is not None:
            ways.append(f"在本目录新建 {self.kill.stop_file.name}")
        if IS_WINDOWS:
            ways.append("Ctrl+C")   # 手机上没有终端，说了也没用
        self.log.info("急停方式：" + "  /  ".join(ways))
        if self.kill.stop_file is not None and self.kill.stop_file.exists():
            self.log.warning(f"注意：{self.kill.stop_file.name} 现在就存在，"
                             f"删掉它才会开始工作")

        # 接住 SIGTERM/SIGINT，好让停止流程走完 finally
        self._install_stop_signal()

        rounds = 0
        last_hb = time.time()
        # ★ 2026-10-12：主循环卡死诊断看门狗（只记主线程调用栈，不杀不重启）。
        #   WX_WATCHDOG_SECS 环境变量 / behavior.watchdog_secs 配置；0 = 关闭，默认 300 秒。
        try:
            _wd_secs = float(os.environ.get(
                "WX_WATCHDOG_SECS", getattr(self.cfg.behavior, "watchdog_secs", 300)))
        except Exception:
            _wd_secs = 300.0
        self._wd = StallWatchdog(self.log, _wd_secs)
        if self._wd.enabled:
            self.log.info(f"诊断看门狗已开启：一轮超过 {_wd_secs:.0f} 秒没走完，"
                          f"就把主线程调用栈写进日志（只记录、不干预）")
        try:
            while max_rounds is None or rounds < max_rounds:
                # 急停优先于一切
                stop = self.kill.check()
                if stop:
                    self.log.warning(f"已停止：{stop}")
                    break

                self.reload_if_changed()

                # ★ 2026-10-11：锁屏就整体挂起（不读屏、不调模型、不发送），
                #   亮屏后自动恢复 —— 用户当天拍板的口径，见 _wait_awake。
                self._wd.disarm()          # 锁屏挂起可以睡几个小时，不算卡
                self._wait_awake()
                self._wd.arm("本轮")

                # ★ 2026-10-11：记忆库自动整理（每天一次；它自己判今天跑过没）
                self._maybe_memory_learn()

                # ★ 2026-10-12：群情摘要（每小时一段；借鉴 AstrBot 的自动上下文压缩）。
                #   它自己判"到点没 / 这段流水够不够"，不够就不花调用。
                self._maybe_group_summary()

                # 零点定时文案优先（它本来就该在整点刚过时发出去）
                # ★ 2026-10-08 审查修复（F2）：原来先 pop 再发，被节流/失败的那条
                #   就永远丢了（EMO 文案自 10-06 起一条都没发出去）。现在**发成功
                #   才出队**；没发成的留在队首，下一轮继续尝试。
                # ★ 2026-10-10 空转修复：原来不论发没发成都 `continue`。被 3 秒间隔
                #   节流拦下时，主循环没有任何等待，00:00:49~52 这 3 秒里空转了约 480 次
                #   （刷 483 条日志、不读屏）；而且 `continue` 让下面的 _maybe_midnight()
                #   永远跑不到，"窗口过了就丢弃残留"的清理也就不会执行。
                #   现在：发成功 → 照旧跳过本轮读屏；没发成 → 往下走，照常读屏并
                #   按轮询间隔等待，下一轮再试（队首那条还留着）。
                if self._midnight_pending:
                    txt = self._midnight_pending[0]
                    if self._send_instant(txt, "零点文案"):
                        self._midnight_pending.pop(0)
                        continue
                self._maybe_midnight()

                # 主动美图（2026-10-08 用户要求：「群友傻傻的不会触发」）——
                # 到点了机器人自己发一张，不用等谁说话。和零点文案一样：本轮
                # 发图就不读屏（发一张要 12 秒，那一轮本来也读不成）。
                # ⚠️ 失败后等 60 秒再试：取图失败多半是网络问题，每 20 秒撞一次
                #    只是白耗电；窗口有 20 分钟，够重试好几轮。
                #    顺序是「先入队、再消费」—— 这样到点那一轮就发出去；
                #    反过来写（零点文案那种先消费后入队）会白等一个轮询周期，
                #    实测 19:02:19 触发、19:02:40 才开始取图，晚了 21 秒。
                self._maybe_auto_meitu()
                if (self._auto_meitu_pending
                        and time.time() >= self._auto_meitu_retry_at):
                    if self._send_auto_meitu(self._auto_meitu_pending[0]):
                        self._auto_meitu_pending.pop(0)
                    else:
                        self._auto_meitu_retry_at = time.time() + 60
                    continue

                # 补发（2026-10-09 用户拍板）：刚才没发出去的话趁这一轮补上。
                # ⚠️ 放在读屏**之前**（先把自己欠的话还了），但**不 continue** ——
                #   补发失败多半是"你还在用手机"，死磕它会让群里新消息都读不到，
                #   那是更大的损失。所以这里只试队首一条，然后照常读屏。
                self._resend_tick()

                # 逆水寒常识的自动学习（2026-10-10 用户要求：「不懂的就去搞懂
                # 下次再说」）——每天最多一次，**起子进程、不等它**：学习要调
                # 模型（十几秒），卡在主循环里这段时间群里的消息就读不到了。
                self._maybe_learn_nsh()

                t0 = time.time()
                try:
                    self.step()
                except Exception as exc:  # 单轮出错不影响常驻
                    self.log.error(f"本轮异常：{type(exc).__name__}: {exc}")
                self._wd.disarm()          # 干完了，下面是睡眠间隔，不算卡
                rounds += 1
                # 夜间慢速时段一轮要 5 分钟，光靠「每 200 轮」心跳会十几小时不吭声，
                # 所以再加上 15 分钟兜底，保证日志里能看出机器人还活着。
                if rounds % 200 == 0 or time.time() - last_hb >= 900:
                    self.log.info(f"[心跳] 已轮询 {rounds} 轮 | {self.governor.stats()} | {self.brain.usage()}")
                    last_hb = time.time()
                interval = self._poll_interval()        # 每轮重算，跨时段自动切换
                elapsed = time.time() - t0
                self._sleep_interval(max(0.2, interval - elapsed))
        except KeyboardInterrupt:
            self.log.info("收到 Ctrl+C，退出。")
        finally:
            self._wd.disarm()
            self.kill.disarm()
            self.log.info(f"本次运行：{self.governor.stats()} | {self.brain.usage()}")
