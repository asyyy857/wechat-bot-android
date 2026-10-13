# -*- coding: utf-8 -*-
"""兴趣点清单：把群聊流水 + 机器人的备稿/否稿/已发，汇总成一份人看的报告。

    python3 interest_report.py            # 写到 兴趣点清单.md
    python3 interest_report.py --print    # 直接打到终端

为什么单独有这个（用户 2026-10-05）：「白天不主动发，我在收集兴趣点」——
面板上只能一条条翻，这里按话题聚类、把"机器人挑过的稿 / 你否掉的 / 真发出去的"
和来源原话对起来，一眼看完。**纯本地统计，不联网、不调模型、不花 Token。**

数据来源（都在本目录）：
    chat_log.json            机器人读到的群聊流水（谁在聊什么）
    sent.jsonl               真发到群里的话（带 origin：谁的意思）
    rejects.jsonl            你点过「不发」的说法
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from wxauto.brain import INTEREST_WORDS          # 已经在表里的词就别再推荐了
except Exception:                                    # 拿不到（改坏了）也不影响出报告
    INTEREST_WORDS = {}

_APP = os.path.dirname(os.path.abspath(__file__))
import sys as _sys
_sys.path.insert(0, _APP)
from wxauto.paths import DATA_DIR
ROOT = str(DATA_DIR)
CHAT = os.path.join(ROOT, "chat_log.json")
SENT = os.path.join(ROOT, "sent.jsonl")
REJECTS = os.path.join(ROOT, "rejects.jsonl")
OUT = os.path.join(ROOT, "兴趣点清单.md")

# ---- 话题桶：命中任意一个词就归到这一桶（一条消息可以落多桶）----
TOPICS = [
    ("恋爱 / 男女关系", [
        "恋爱", "谈恋爱", "处对象", "搞对象", "异地", "网恋", "对象", "男朋友", "女朋友",
        "男友", "女友", "前任", "分手", "复合", "表白", "告白", "暗恋", "相亲", "暧昧",
        "暖昧", "忽冷忽热", "拉扯", "追人", "追求", "约会", "舔狗", "备胎", "海王", "渣男",
        "渣女", "绿茶", "白月光", "出轨", "劈腿", "小三", "结婚", "离婚", "彩礼", "脱单",
        "喜欢", "心动", "吃醋", "狗粮", "发糖", "磕", "cp", "单身", "老婆", "老公", "媳妇",
        "直男", "直女", "pua", "冷暴力", "撩", "真爱", "爱情", "告白", "复合"]),
    ("外貌 / 身材（人设兴奋点）", [
        "好看", "腿", "萝莉", "御姐", "甜妹", "身材", "脸", "颜值", "可爱", "漂亮", "美",
        "自拍", "腹肌", "胖", "瘦", "高", "矮", "发型"]),
    ("游戏 / 开黑", [
        "逆水寒", "三角洲", "王者", "上号", "pvp", "开黑", "打本", "副本", "赛季", "爆装",
        "装备", "职业", "龙吟", "素问", "铁衣", "碎梦", "神相", "九灵", "血河", "手游", "端游"]),
    ("情绪 / 深夜", [
        "emo", "深夜", "难过", "累", "烦", "破防", "崩溃", "孤独", "想哭", "摆烂", "活着",
        "没意思", "不开心", "焦虑", "失眠", "睡不着"]),
    ("吃喝 / 生活", [
        "吃", "饭", "外卖", "奶茶", "咖啡", "西瓜", "水果", "夜宵", "喝", "零食", "做饭",
        "好吃", "食堂", "火锅", "烧烤"]),
    ("工作 / 钱", [
        "上班", "加班", "下班", "工资", "老板", "同事", "面试", "辞职", "赚钱", "穷", "学费",
        "打工", "实习", "考试", "论文"]),
    ("抽象整活", [
        "抽象", "整活", "绷不住", "笑死", "离谱", "逆天", "绝了", "离谱", "搞", "典", "破防",
        "懂了", "6", "牛的", "狠"]),
    ("图片 / 表情包", ["[图片]"]),
]

ORIGIN_CN = {
    "approve": "你点头发的", "edited": "你改完发的", "manual": "你手打的",
    "rule": "关键词自动回", "mention": "被 @ 回的", "proactive": "它主动插话",
    "reply": "它自动回的", "image": "表情包",
}

STOP = set("""什么 这个 那个 就是 不是 没有 可以 我们 你们 他们 她们 自己 现在 今天 明天
昨天 哈哈 哈哈哈 知道 觉得 应该 真的 感觉 然后 因为 所以 但是 如果 时候 一个 一下 这样
那样 怎么 为什么 多少 还有 已经 出来 起来 下来 上去 里面 外面 之后 之前 东西 事情 问题
地方 朋友 老师 其实 反正 也是 还是 就是 可能 不会 不能 不要 这么 那么 一样 有点 一点
笑死 真的 好像 直接 完全 突然 终于 结果 开始 结束 有点 不然 要不 要是 于是 而且 或者
不过 只是 一直 从来 每次 有人 没人 大家 别人 这些 那些 哪里 这儿 那儿""".split())


# n-gram 会切出「那我不」「到真爱」「才能得」这种跨词碎片 —— 只要含这些虚词单字就丢掉。
STOP_CHARS = set("我你他她它们个的了是不那这得才到能会要就都也很在有和与被把给让从向对为"
                 "以及或但而却只更最太再还又没吗呢吧啊呀哦嗯么之于此其如若因所当然已经过"
                 "种些什么怎样然后因为所以但是就是还是可以自己一个一下这样那样多少还有")


def _clean_grams(t):
    """一句话里值得当"候选兴趣词"的 2~3 字片段。"""
    out = set()
    for seg in re.findall(r"[\u4e00-\u9fff]{2,}", t):
        for n in (2, 3):
            for i in range(len(seg) - n + 1):
                g = seg[i:i + n]
                if any(ch in STOP_CHARS for ch in g):
                    continue
                out.add(g)
    return out


def _dedup_similar(ms):
    """同一句话被几个人复读时只列一条（标 ×N）—— 群里复读是常态，不合并会把专题刷满。"""
    groups, order = {}, []
    for m in ms:
        key = re.sub(r"\s+", "", str(m.get("text") or ""))[:14]
        if key not in groups:
            groups[key] = {"m": m, "n": 0, "who": set()}
            order.append(key)
        groups[key]["n"] += 1
        groups[key]["who"].add(str(m.get("sender") or "?"))
    return [(groups[k]["m"], groups[k]["n"], sorted(groups[k]["who"])) for k in order]


def _read_jsonl(path):
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    return out


def _msgs():
    try:
        with open(CHAT, encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        return []
    return [m for m in (d.get("messages") or []) if isinstance(m, dict)]


def _hhmm(ts):
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%m-%d %H:%M")
    except Exception:
        return "?"


def _clip(s, n=34):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def main() -> int:
    msgs = _msgs()
    users = [m for m in msgs if not m.get("self") and str(m.get("text") or "").strip()]
    mine = [m for m in msgs if m.get("self")]
    sent = _read_jsonl(SENT)
    rejects = _read_jsonl(REJECTS)

    lines = []
    add = lines.append
    add("# 群兴趣点清单")
    add("")
    if msgs:
        add("> 群聊流水 %d 条（群友 %d / 机器人 %d）· 时间 %s ~ %s"
            % (len(msgs), len(users), len(mine),
               _hhmm(msgs[0].get("ts")), _hhmm(msgs[-1].get("ts"))))
    else:
        add("> 还没有群聊流水（机器人跑起来之后才会有）")
    add("> 生成时间 %s · 由 `interest_report.py` 本地统计，不联网" % _hhmm(datetime.now().timestamp()))
    add("")

    # ---------- 一、话题热度 ----------
    hits = defaultdict(list)
    for m in users:
        t = str(m.get("text") or "")
        for name, words in TOPICS:
            if any(w in t for w in words):
                hits[name].append(m)
    add("## 一、话题热度（群友消息命中条数）")
    add("")
    add("| 话题 | 条数 | 占群友消息 | 谁在聊 |")
    add("|---|---|---|---|")
    for name, _ in TOPICS:
        lst = hits.get(name) or []
        if not lst:
            add("| %s | 0 | – | – |" % name)
            continue
        who = Counter(str(m.get("sender") or "?") for m in lst)
        top = "、".join("%s(%d)" % (w, c) for w, c in who.most_common(3))
        add("| %s | %d | %.0f%% | %s |" % (name, len(lst), 100.0 * len(lst) / max(1, len(users)), top))
    add("")

    # ---------- 二、恋爱专题 ----------
    love = hits.get("恋爱 / 男女关系") or []
    add("## 二、恋爱 / 男女关系 专题（你说你最关心的）")
    add("")
    if not love:
        add("（这段时间还没有命中这条线的消息）")
    else:
        add("**最近 %d 条里出现 %d 次**，聊得最多的：%s"
            % (len(users), len(love),
               "、".join("%s(%d)" % (w, c) for w, c in
                         Counter(str(m.get("sender") or "?") for m in love).most_common(5))))
        add("")
        for m, n, who in _dedup_similar(love)[-12:]:
            tag = ("（%d 人在复读）" % n) if n > 1 else ""
            add("- `%s` **%s**%s：%s" % (_hhmm(m.get("ts")),
                                        "、".join(who[:3]) if n > 1 else (m.get("sender") or "?"),
                                        tag, _clip(m.get("text"), 60)))
    add("")

    # ---------- 三、机器人发出去的话 ----------
    # ⚠️ 2026-10-07：原来这里还列「它挑的稿（待审 / 被覆盖归档）」—— 待审草稿那条线
    #    已按用户要求删掉（交接说明 §46.8），只剩"真发出去的话"。
    add("## 三、机器人发出去的话（最近 20 条）")
    add("")
    if sent:
        add("**真发出去的（最近 20 条）**")
        add("")
        for r in sent[-20:]:
            o = ORIGIN_CN.get(str(r.get("origin") or ""), str(r.get("origin") or "?"))
            src = ("　← 接的是：%s" % _clip(r.get("src"), 30)) if r.get("src") else ""
            add("- `%s` [%s] %s%s" % (_hhmm(r.get("ts")), o, _clip(r.get("text"), 40), src))
        add("")
    if not sent:
        add("（还没有发过话）")
        add("")

    # ---------- 四、你否掉的 ----------
    add("## 四、你否掉的（下次别这么说）")
    add("")
    if not rejects:
        add("（还没有否过）")
    else:
        for r in rejects[-15:]:
            add("- `%s` %s　← 来源：%s" % (_hhmm(r.get("ts")), _clip(r.get("text"), 40),
                                          _clip(r.get("src"), 30)))
    add("")

    # ---------- 五、候选兴趣词（群里常聊但表里没收的） ----------
    names = {str(m.get("sender") or "") for m in msgs}
    gram = Counter()
    seen_msgs = set()
    for m in users:
        t = str(m.get("text") or "")
        if len(t) > 40:
            continue                     # 长文案（广告/充值链接/复读长句）不参与切词，
                                         # 否则会切出一堆「值优惠 / 领取超」这种碎片
        ukey = re.sub(r"\s+", "", t)
        if ukey in seen_msgs:
            continue                     # 同一句话被几个人复读，只算一次
        seen_msgs.add(ukey)
        seen = {g for g in _clean_grams(t)
                if g not in STOP and not any(nm and nm in g for nm in names)}
        gram.update(seen)
    known = {w for _, ws in TOPICS for w in ws} | set(INTEREST_WORDS or {})
    # 先按"出现多、且更长"排，再把**是某个更长候选词子串**的碎片丢掉
    #（复读句会切出「遇见喜 / 见喜 / 懂拉扯」这种半截词，留下「遇见喜欢」就够了）
    raw = [(g, c) for g, c in gram.most_common(400)
           if c >= 3 and g not in known and not any(g in k for k in known)]
    raw.sort(key=lambda x: (-x[1], -len(x[0])))
    cand = []
    for g, c in raw:
        if any(g != k and g in k for k, _ in cand):
            continue
        cand.append((g, c))
        if len(cand) >= 20:
            break
    add("## 五、候选兴趣词（群里常出现、但 `INTEREST_WORDS` 里还没有）")
    add("")
    add("想让我给哪类话题加权，直接把词说给我，或自己往 `wxauto/brain.py` 的")
    add("`INTEREST_WORDS` 里加（改完要重启机器人）。")
    add("")
    if cand:
        add("、".join("%s(%d)" % (g, c) for g, c in cand))
    else:
        add("（没挑出明显的候选）")
    add("")

    text = "\n".join(lines) + "\n"
    if "--print" in sys.argv:
        print(text)
    else:
        with open(OUT, "w", encoding="utf-8") as fh:
            fh.write(text)
        print("写好了：%s（%d 字节）" % (OUT, len(text.encode("utf-8"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
