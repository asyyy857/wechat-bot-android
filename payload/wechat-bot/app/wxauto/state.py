# -*- coding: utf-8 -*-
"""状态层：消息去重。

OCR 是逐帧读屏的，同一句话会连续出现在几十帧里。这里用一个指纹集合把它
压成"只在第一次出现时算新消息"。

⚠️ 指纹**必须容忍 OCR 抖动**，否则同一条消息会被反复当成新消息：

* 标点/符号抖动 → ``_NON_WORD`` 剥掉（2026-10-03 的「晚安发了两个」）；
* **图上的字抖动** → 分享卡片上的点赞数、水印 ID 每一帧都读得不一样
  （同一张图实测读出过 ``Q475G2F5F`` / ``475G2干5干`` / ``Q4752F5F``），
  精确指纹每帧都在变。2026-10-08 用户报「机器人对着同一个照片评论了两次」，
  真相是 **6 次**（流水：20:09 起连着回，间隔十几秒到一分钟）。
  现在精确指纹之外还有一道模糊比对兜底，见 ``_fuzzy_same``。
* **昵称也在抖** → 灰色小字的昵称比正文抖得还厉害，同一个人相邻两帧能读出
  四种花样。2026-10-09 用户报「又重复读消息了」：同一条「我小号17歌重塑丹」
  被回了 **2 次**（08:32:26「哦？17歌都没出喵」、08:33:35「淦 17歌也沉喵」），
  台账里那条消息的 sender 先后是 ``甜秋素问`` / ``甜湫素问`` / ``品淋系问`` / 空。
  ⚠️ 病根不只是"精确指纹带 sender"（那是 2026-10-08 晚为了不吞掉"两个人先后
  说同一句"故意加的，要留着），而是**模糊兜底的键也带了 sender** —— 昵称一抖，
  ``_fuzz_key`` 就对不上，兜底那道根本不比了。现在模糊那道的键**只留
  "自己/别人"**，昵称改成**相容判据**（``names_same``）：容忍形近字与漏读，
  宁可把两个人说的同一句合并（漏一条），也不重复发群。
"""
from __future__ import annotations

import difflib
import re
import time
from collections import Counter, deque
from typing import Deque, List, Optional, Set, Tuple

from .reader import Message, normalize


# 指纹里不带标点/符号：OCR 对同一句话的标点会抖（「（‘-wn）」↔「('-wn)」，
# 中文引号 ↔ ASCII 引号），指纹一变就被当成新消息 —— 同一句话会被回两次。
# 2026-10-03 用户报障「晚安发了两个，一个晚安一个晚安喵」正是踩了这个。
_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)

# 图上的字里，字母/数字是 OCR 噪声的重灾区 —— 同一张图实测读出过
# Q475G2F5F / 475G2干5干 / Q4752F5F / 4.9万…四五种花样，而这些噪声
# **正是"精确指纹每帧都变"的元凶**，只剥标点治不了（见 _fuzzy_same 第 2 级）。
_IMG_TAG = "[图片]"
_ONLY_HAN = re.compile(r"[^\u4e00-\u9fff]+")


def _img_key(msg: Message) -> str:
    """图片消息的身份：优先用桥接算好的**裁剪图感知哈希**，退回内容 md5。

    ★ 2026-10-10（识图功能）：图片消息的正文永远是 ``[图片]``（外加图上那点
    OCR 文字），拿它当指纹有两个毛病：
      · 同一个人连发两张不同的图 → 正文一模一样 → 第二张被当成重复**吞掉**，
        机器人永远只点评第一张；
      · 图上的字每帧都在抖 → 同一张图又被当成新消息反复点评。
    所以拿"这一帧裁下来的那张图"当身份。

    ★ 2026-10-10 晚（用户报「机器人一直在识别阿兰发的图，好几次了」）：
    **md5 这条路不够**。裁剪边界（``wxreader.refine_image_box``）每帧会抖
    ±1px —— 同一张图日志里读出过 362x829 / 363x829 / 361x829，而 md5 对 1px
    差异零容忍，于是同一张图每帧都算"新图"（17:24:40~17:25:30 连送 5 次
    视觉模型）。现在优先用桥接算的 **aHash**（8x8 灰度均值哈希，见
    ``wxbridge._img_ahash``）：对 1~2px 抖动稳定、对不同图仍可区分；
    只有老桥接没给该字段时才退回 md5。

    ⚠️ aHash 光靠"精确相等"还不够 —— 判重走 ``State._img_dup`` 的
    **汉明距离 ≤ 阈值**。多出来的那一层兜底是发送层的"同一个人 N 分钟只点评一张"。
    """
    h = str(getattr(msg, "img_hash", "") or "").strip()
    if h:
        return "h:" + h
    m = str(getattr(msg, "img_md5", "") or "").strip()
    return ("m:" + m) if m else ""


# 图片感知哈希的汉明距离阈值：≤ 它就算"同一张图"。
# 实测（裁剪图先裁边 10%，再缩 8x8；见 wxbridge._img_ahash）：
#   · 同一张图内容偏移 1~12px → 距离 ≤4，偏移 30px → 11；
#   · 不同的图（780 组表情包两两比）1% 分位 15、中位 31，距离 ≤12 的只占 0.26%。
# 取 12：把滚动/边界漂移都兜住，代价是 0.26% 的"两张极像的图被并成一张"
# （宁可漏点评一张，也不重复花钱看同一张 —— 项目一贯取舍）。
IMG_HASH_MAX_DIST = 12


def _img_hash_int(msg: Message) -> int:
    """图片消息的感知哈希（整数）。拿不到 / 格式不对 → -1。"""
    h = str(getattr(msg, "img_hash", "") or "").strip()
    if not h:
        return -1
    try:
        return int(h, 16)
    except ValueError:
        return -1


def _ratio_ok(a: str, b: str) -> bool:
    """两条归一化正文够不够像（长度差 + 相似度）。"""
    if len(a) < 6 or len(b) < 6:
        return False
    if abs(len(a) - len(b)) > max(2, len(b) // 5):
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.85


def _bag_same(ha: str, hb: str) -> bool:
    """两串**纯汉字**是不是"同一批字、只是顺序被打乱了"（字袋重合度）。

    ★ 2026-10-12（用户报「棉花糖这两条」：同一条 @ 间隔 11 分钟被回了两次）：
      那条 @ 的气泡有两行，OCR 两次读出来**行序反了** ——
      ``艾特我召唤猫看看高玩主人昵称娘`` ↔ ``看看高玩主人昵称艾特我召唤猫娘``，
      字一个不差、顺序整块对调，``SequenceMatcher`` 只有 0.54（门槛 0.85），
      精确指纹和两级模糊全部放行，于是被当成新消息再回一遍。
      字袋（不看顺序）重合度这里是 1.0。

    ⚠️ 门槛是**故意**卡严的：汉字 ≥ 10、长度差 ≤ 2、重合 ≥ 0.9。
      短句里"换个字意思就变"（「今晚8点打本」）不会进来；
      两句真不同的长话字袋重合到 9 成几乎不可能。
      万一误合并，代价是漏回一条 —— 项目一贯取舍：宁可漏、不可重。
    """
    if len(ha) < 10 or len(hb) < 10 or abs(len(ha) - len(hb)) > 2:
        return False
    common = sum((Counter(ha) & Counter(hb)).values())
    return common / max(len(ha), len(hb)) >= 0.9


def _prefix_same(ha: str, hb: str) -> bool:
    """两串**纯汉字**里，短的是不是长的**开头**（同一条被截断读了一次）。

    ★ 2026-10-13（用户报「机器人重复发消息」）：张三那条 @ 气泡有两行，
      第一次读屏只读到第一行（``巨大化双星河甘蔗多少钱现在@素``，第二行被检测
      漏掉），机器人接了话；下一轮读全了（``…现在@素问-兰德川酷子``），长度差
      超过 ``_ratio_ok`` 的容忍 → 当成新消息，**又主动插话了一次**。
      "短的是长的开头"说明就是同一条被读全了，不是新话。

    ⚠️ 门槛：短的汉字 ≥ 8；短 / 长 ≥ 0.5（防"哈哈哈哈哈哈哈哈"这种
      重复字被后来更长的一条吞掉）。误合并代价是漏回一条 —— 宁可漏、不可重。
    """
    if len(ha) > len(hb):
        ha, hb = hb, ha
    if len(ha) < 8 or len(ha) / max(1, len(hb)) < 0.5:
        return False
    return hb.startswith(ha)


def _fuzzy_same(a: str, b: str) -> bool:
    """两句**已归一化**的正文，是不是"同一条"（容忍 OCR 抖动）。

    两级判据，任一命中即算同一条：

    1. 原样比（口径跟 ``chatlog._fuzzy_dup`` 一致）：短句必须**完全相等** ——
       同一个人连发两条「哈哈哈」是常事，按相似度去重会把第二条吃掉；
       只有长句才允许一两个字的抖动。
    2. **只留汉字再比一次**：OCR 对同一张图上的小字（点赞数、水印 ID、
       游戏截图里的数字）每帧读得都不一样，而这些噪声全落在字母/数字上。
       ⚠️ 这一级要求汉字长度 ≥ 8，是**故意的**：短消息里的数字往往是真内容
       （「今晚8点打本」↔「今晚9点打本」剥完汉字都只剩「今晚点打本」，
       不设门槛会把第二条吞掉）。长消息里只差一串数字、汉字骨架一模一样，
       那基本就是同一张图被读了两遍。

    ⚠️ 之所以要"两级"、而不是按「消息带不带 [图片] 前缀」分流：实测同一张
    卡片，读取层有时判成图片（``[图片]那``）、有时把图上的字判成一条普通文本
    消息 —— 按类型分流会漏掉后者，重复照旧。

    ⚠️ 「空 vs 空」算同一条：两张都没读出文字的图（纯照片）本来就无从区分，
    只能按"同一个人的图，最近只回一次"处理 —— 这是**故意的**，纯图重复回
    才是用户 2026-10-08 报的那个毛病。

    取舍：宁可漏一条也别重复发 —— 漏发只是可惜，重复发群是真事故。
    """
    if a == b:
        return True
    if not a or not b:
        return False
    if _ratio_ok(a, b):
        return True
    ha, hb = _ONLY_HAN.sub("", a), _ONLY_HAN.sub("", b)
    if len(ha) < 8 or len(hb) < 8:
        return False
    return _ratio_ok(ha, hb) or _bag_same(ha, hb) or _prefix_same(ha, hb)


def names_same(a: str, b: str) -> bool:
    """两个**已归一化**的昵称，算不算同一个人（容忍 OCR 形近字与漏读）。

    ★ 2026-10-09（用户报「又重复读消息了」）：昵称是灰色小字，OCR 抖得比正文
      还凶。实测同一个人「甜湫素问」相邻几帧读成 ``甜秋素问`` / ``甜湫素问`` /
      ``品淋系问`` / 空 —— 而模糊去重当时是拿昵称**当键**的（``os_ != ns`` 就
      "换人了"直接跳过），于是同一条消息每次都被当成新消息，回了两次。

    判据故意**宽松**（共同字 ≥1 就算同一个人），因为两种错法的代价不对称：

    * 判成"同一个人"→ 最多把群里两个人先后说的同一句话合并 → **漏一条**；
    * 判成"换人了"→ 屏幕上静止的旧消息被反复当成新消息 → **重复发群**（事故）。

    项目一贯取舍：宁可漏、不可重（见 ``_fuzzy_same`` 结尾）。
    ⚠️ 只有**正文先模糊相同**了才会走到这里，所以"共享一个常见字"的误合并
      需要两个人真的说了几乎一样的话 —— 代价可控。
    """
    if a == b:
        return True
    if not a or not b:
        # 昵称没读出来（实测四帧里就有一帧是空的）→ 无从区分，按同一个人算
        return True
    # ★ 2026-10-15（用户报「机器人发了两条要图消息」）：**读残/截断**是最常见的一种
    #   抖 —— 实测同一个人「张三（今晚打本）」，同一条「p的我也喜欢」在相邻
    #   三帧里读成「(今晚打本)」和「张三」；而下面那条 `abs(len(a)-len(b)) > 3`
    #   直接把「张三」判成"换人了" → 模糊去重失效 → 同一条消息被插话两次
    #   （18:14:00 / 18:18:21，chat_log 里 18:13:53 / 18:17:04 / 18:18:10 三帧）。
    #   一个是另一个的**子串** = 截断，认成同一个人。
    #   ⚠️ 代价：昵称互为子串的两个人（「小明」vs「小明明」）可能并成一人 → 最多漏一条；
    #      反向代价是**重复发群**（事故）。项目一贯取舍：宁可漏、不可重。
    if a in b or b in a:
        return True
    if abs(len(a) - len(b)) > 3:
        # 长度差太多，多半是两个人（昵称可能被截断，但差 4 个字以上太远）
        return False
    if set(a) & set(b):
        # 「甜湫素问」vs「品淋系问」只有一个「问」字相同，也得认 —— 形近字
        # 连读错两三个是常态，靠比例判会把它们判成两个人
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.5


class State:
    def __init__(self, keep: int = 800, fuzzy_keep: int = 120) -> None:
        self._order: Deque[str] = deque(maxlen=keep)
        self._seen: Set[str] = set()
        # 模糊比对用的"最近处理过的消息"：(谁, 发送者, 归一化正文)。
        # keep(800) 只管精确指纹；模糊这一路只跟最近的比 —— 屏幕上一帧最多十
        # 来条，120 条足够覆盖"还挂在屏幕上、每帧都会重新读到"的那些旧消息。
        # ⚠️ 发送者**存在这里、但不参与"键"** —— 键只有"谁"，昵称的抖动交给
        #    ``names_same`` 相容判据（见 _fuzz_key）。
        self._recent: Deque[Tuple[str, str, str]] = deque(maxlen=max(20, int(fuzzy_keep)))
        # ★ 2026-10-10 晚：图片的感知哈希另记一路：(谁, 发送者, aHash 整数)。
        #   图片判重走**汉明距离**（见 _img_dup），不能混进上面那本靠字符串
        #   精确/相似度比的账。屏幕上一帧十来条图，60 条足够覆盖还挂着的那几张。
        self._img_recent: Deque[Tuple[str, str, int]] = deque(maxlen=60)

    @staticmethod
    def fingerprint(msg: Message) -> str:
        who = "S" if msg.is_self else "O"
        # ⚠️ 2026-10-06 **回退过一次**：为了让"自己拍自己"能重复触发，这里曾按
        #   "分钟"给拍一拍打桶（`%s#%d`）—— 结果屏幕上那条拍一拍**每分钟都复活**
        #   成新消息，冷却一过就再发一张图，用户看到的是「它自己读自己、自己发图、
        #   一直循环」（实测 13:03 / 13:05 / 13:08 三次触发源是同一条消息）。
        #   教训：**"消息还挂在屏幕上"和"又来了一次新事件"是两回事**，
        #   靠时间分桶分不开，只会把静态的旧消息变成永动机。
        #   正确语义就是最朴素的那条：**同一条消息只处理一次**。
        #   · 别人拍我 → 文本里带不同的人名 → 指纹不同 → 每次都触发 ✓
        #   · 同一个人连拍 / 自己拍自己 → 文本一样 → 只触发一次 ✓（本来也该这样）
        # ★ 2026-10-08 晚（审查报告 §209）：指纹补上**发送者**。
        #   原来只有"自己/别人 + 文本"，于是群里两个人先后说同一句
        #   （"笑死""+1""哈哈哈"这种极常见），第二条会被判成重复**直接吞掉**,
        #   机器人只回第一个人。
        #   ⚠️ 报告原本建议"再加个时间窗"，**这里故意不加**：2026-10-06 那次
        #   回退（见本函数上面的长注释）已经证明时间分桶会把"屏幕上静态的旧
        #   消息"变成每分钟复活的永动机。正确语义仍是"同一条消息只处理一次"，
        #   只是"同一条"要算上是谁说的。
        #   sender 读不出时为空串 → 指纹自动退化成加发送者之前的样子。
        # ★ 2026-10-10（识图功能）：**图片消息用裁剪图内容 md5 当正文**，
        #   不用 `[图片]` 那四个字 —— 理由见 _img_key 的注释（同人连发两张图
        #   要能分开、同一张图逐帧要能认出）。
        _sender = _NON_WORD.sub("", normalize(getattr(msg, "sender", "") or ""))
        _ik = _img_key(msg)
        _body = ("img:" + _ik) if _ik else \
            _NON_WORD.sub("", normalize(msg.text) or "")
        return who + (_sender + "#" if _sender else "") + _body

    @staticmethod
    def _fuzz_key(msg: Message) -> Tuple[str, str, str]:
        """模糊比对的键：(谁, 发送者, 归一化正文)。

        ⚠️ ``[图片]`` 标记在这里**剥掉、且不区分消息类型**：同一张卡片，读取层
        有时判成图片（``[图片]那``）、有时把图上的字判成一条普通文本消息，
        按类型分流会让后者绕过模糊这道，重复照旧发生（2026-10-08 实测）。
        剥掉标记后两种读法落到同一个键上，算不算"同一条"交给 ``_fuzzy_same``。

        ★ 2026-10-09：**键里只留"谁(自己/别人)"，不再带昵称**。
          原来键是 ``who + sender``，于是昵称一抖（``甜秋素问`` ↔ ``品淋系问`` ↔
          空）两道去重**同时失效** —— 精确指纹因为带 sender 认不出，模糊兜底
          又因为键对不上压根不比，同一条消息被回了两次。
          昵称改由 ``names_same`` 做**相容判据**：容忍形近字与漏读，但两个人
          昵称毫无共同字时仍然分开（保住"两个人先后说同一句"那条语义）。
        """
        who = "S" if msg.is_self else "O"
        sender = _NON_WORD.sub("", normalize(getattr(msg, "sender", "") or ""))
        # ★ 2026-10-10：图片消息用裁剪图内容 md5 当正文（同 fingerprint）。
        #   拿不到 md5（老桥接 / 取图失败）时退回老口径：剥掉 [图片] 的正文。
        ik = _img_key(msg)
        if ik:
            return who, sender, "img:" + ik
        text = (normalize(msg.text) or "").replace(_IMG_TAG, "")
        return who, sender, _NON_WORD.sub("", text)

    def _fuzzy_dup(self, msg: Message) -> bool:
        """这条消息跟"最近处理过的"是不是同一条（容忍 OCR 抖动）。

        ★ 2026-10-08（用户报「对着同一个照片评论了两次」）：精确指纹之外再兜
          一道模糊。同一条消息在两轮读屏里 OCR 常差一两个字，图上的小字更是
          每帧都不一样 —— 只靠精确指纹，屏幕上那条静止的旧消息会被无限
          "复活"成新消息，冷却一过就再回一次（实测同一条被回了 6 次）。
        ★ 2026-10-09：再加上发送者相容判据（``names_same``）—— 昵称也会抖。
        """
        who, sender, body = self._fuzz_key(msg)
        for old_who, old_sender, old_body in self._recent:
            if old_who != who:
                continue
            if not names_same(old_sender, sender):
                continue
            if _fuzzy_same(old_body, body):
                return True
        return False

    def _img_dup(self, msg: Message) -> bool:
        """图片消息的**近似**去重：aHash 汉明距离 ≤ 阈值就算同一张。

        ★ 2026-10-10 晚（用户报「机器人一直在识别阿兰发的图，好几次了」）：
        裁剪边界每帧抖 ±1px，内容 md5 对 1px 零容忍 → 同一张图每帧都算新图，
        17:24:40~17:25:30 连送 5 次视觉模型（白花钱）。aHash(8x8) 对 1~2px
        平移的实测汉明距离 ≤1，所以这里按阈值比、不要求精确相等。
        昵称抖动用 ``names_same`` 兜（跟文本那路一致：宁可合并，不可重复）。
        """
        h = _img_hash_int(msg)
        if h < 0:
            return False
        who = "S" if msg.is_self else "O"
        sender = _NON_WORD.sub("", normalize(getattr(msg, "sender", "") or ""))
        for old_who, old_sender, old_h in self._img_recent:
            if old_who != who:
                continue
            if not names_same(old_sender, sender):
                continue
            if bin(old_h ^ h).count("1") <= IMG_HASH_MAX_DIST:
                return True
        return False

    def is_new(self, msg: Message) -> bool:
        fp = self.fingerprint(msg)
        if not fp[1:]:
            return False
        if fp in self._seen:
            return False
        if self._img_dup(msg):
            return False
        return not self._fuzzy_dup(msg)

    def mark(self, msg: Message) -> None:
        fp = self.fingerprint(msg)
        if fp not in self._seen:
            if len(self._order) == self._order.maxlen:
                old = self._order[0]
                # deque 满了会自动丢弃最左，先同步清理集合
                self._seen.discard(old)
            self._order.append(fp)
            self._seen.add(fp)
        # ★ 2026-10-10 晚：图片另记一份感知哈希，供 _img_dup 做**近似**比对
        #   （精确指纹带 sender，昵称一抖就对不上；见 _img_dup）。
        ih = _img_hash_int(msg)
        if ih >= 0:
            self._img_recent.append(
                ("S" if msg.is_self else "O",
                 _NON_WORD.sub("", normalize(getattr(msg, "sender", "") or "")),
                 ih))
        # 正文这一路**无条件**补记（哪怕精确指纹早就有了）：模糊那道靠它当
        # "最近处理过什么"的底账，漏记就等于下次判不出重复。
        key = self._fuzz_key(msg)
        if key not in self._recent:
            self._recent.append(key)

    def prime(self, messages: List[Message]) -> int:
        """启动时把当前屏幕上的历史消息全部标记为已见，避免开机就回一堆。"""
        n = 0
        for m in messages:
            # ⚠️ 这里原来对拍一拍"网开一面"（重启后让它照常触发）——跟着分钟分桶
            #   一起回退了：重启时屏幕上那条拍一拍本来就是**旧消息**，放它进去
            #   等于"每次重启都补发一张图"，跟刷屏是同一类错。现在的规矩统一成
            #   一条：**启动时屏幕上有的，一律算已见**。
            if self.is_new(m):
                self.mark(m)
                n += 1
        return n

    def __len__(self) -> int:
        return len(self._seen)
