# -*- coding: utf-8 -*-
"""逆水寒常识的自动学习闭环（2026-10-10 用户要求）。

用户原话：「加常识库，不懂的就去搞懂下次再说」。

闭环怎么转
----------
1. **发现**：机器人本来就每轮把群聊落盘（`chat_log.json`，见 `wxauto/chatlog.py`）。
   这里从台账里挑出「像在聊逆水寒」的消息 —— 判据是一份宽泛的领域信号词表
   （宁可多收，噪声让模型忽略就好）。
2. **去重**：处理过的消息指纹记在 `nsh_learn_state.json`，同一条不学第二遍。
3. **查证**：把一批新消息交给 DeepSeek，让它归纳出其中的游戏术语/黑话，
   **并要求它自己标 `confident`**。
4. **入库**：
   - 有把握的 → 追加进 `逆水寒自学区.md`，**这份文件注入 system prompt**，
     所以下一轮回复就用得上；
   - 没把握的 → 丢进 `逆水寒待核实.md`，**不注入** ——
     宁可让它认怂说"没听过"，也不能教它一个可能是错的东西。

⚠️ 模型的归纳**可能有错**，所以两份文件都是明文的、随时可删可改；
   人确认过的说法请挪进《逆水寒常识.md》（那份是人工维护的真源）。

用法::

    python3 nsh_learn.py --dry-run        # 只看看会学哪些消息（不联网、不写文件）
    python3 nsh_learn.py                 # 真学一轮（默认最多 40 条）
    python3 nsh_learn.py --limit 80      # 一次多学点
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_APP = Path(__file__).resolve().parent
sys.path.insert(0, str(_APP))
from wxauto.paths import DATA_DIR

ROOT = DATA_DIR

CHAT = ROOT / "chat_log.json"
STATE = ROOT / "nsh_learn_state.json"
AUTO = ROOT / "逆水寒自学区.md"
PENDING = ROOT / "逆水寒待核实.md"
VOCAB = ROOT / "逆水寒术语.json"

# 领域信号词：出现任何一个，就认为这条消息可能是在聊逆水寒。
# 故意收得宽 —— 学习是离线批处理，多收一点只是多花几厘钱 token，
# 漏收才是真损失（那意味着群里的说法永远学不到）。
_TRIGGER_TEXT = """
逆水寒 帮战 帮会 领地战 约战 团本 开团 副本 独珍 赤灵 寄盗 大铁 小铁 星沙 天赏
重塑丹 灵韵 保底 cw 橙武 玄号 号价 号贩子 交易行 铜币 铜价 拍卖 顶价 底价 补差价
代抢 代练 代种 上架 出价 血赚 礼盒 顶奢 时装 染色 转盘 脚印 配饰 捏脸 衣柜
奇石 丹方 配方 情书 沧海 混分 结义 情缘 茶壶 夺舍 统战 占场 拔旗 对拆 试训
摇人 防守玄 天榜 毕业装备 流派平衡 玄机 碎梦 龙吟 素问 铁衣 血河 神相 九灵
潮光 沧澜 沧铁 鸿音 云瑶 荒羽 菠萝岛 上岛 巨大化 超巨大化 杂交 收菜 松土
神赐 神秘商人 传说天气 神秘天气 雷暴雨 渠道服 开壶 长草期 打本 上号 抢旗子
幽蛊南疆 神鬼乱劫 千蛊迷阵 承影 武蕴 灵窍 果实银行 翻牌 太阳花 菠小萝 兰沧寨
战山河 昆仑虚 六道冥途 六合青龙 关山止戈 万象秘境 星梦华宴 一掷千金 月刃破晓
月岭瑶歌 明星赛 转性 玉华 轻容 劫狱 越狱 荣誉挑战 荣誉木桩 祈珍 转职 流派翻新
武魂 觉醒 内功 心法 洗练 打造 特技 珍品 词条 秒伤 打桩 评分 虚战
"""
TRIGGERS = [w for w in _TRIGGER_TEXT.split() if w]

SYS = (
    "你是《逆水寒》玩家圈的资料整理员。下面是一个逆水寒玩家微信群的聊天记录片段。\n"
    "请挑出里面出现的**游戏术语、黑话、道具名、玩法名、门派名、帮会用语、账号交易用语**，"
    "每条给一句解释（说清「是什么、群里一般拿它来干嘛」）。\n\n"
    "要求：\n"
    "1. 只输出 JSON 数组，不要任何别的文字、不要 markdown 代码块。\n"
    "   形如：[{\"term\":\"重塑丹\",\"mean\":\"洗装备词条的丹，出什么属性看脸\",\"confident\":true}]\n"
    "2. 每个 term **必须真的在上面这段聊天里出现过**，不要凭空补充群里没提过的词。\n"
    "3. 你**有把握**才写 \"confident\": true；靠猜的、含糊的、拿不准的写 false。\n"
    "   —— 标错代价很大：标 true 会被机器人当成事实拿去接话，标 false 只是先存着。\n"
    "4. 一般闲聊词（哈哈哈、表情、人名、日常用品）不要列。\n"
    "5. 最多 20 条；一条都没有就输出 []。"
)

AUTO_HEAD = """# 逆水寒自学区（机器人自己学来的）

> 由 `nsh_learn.py` 自动写入：它从群聊台账里挑出逆水寒相关的说法，交给模型归纳成一句话。
> **这份文件会注入机器人的 system prompt**，所以它学到的下一轮就用得上
> （用户原话：「不懂的就去搞懂下次再说」）。
>
> ⚠️ 机器自学，**可能有错** —— 看到不对的直接删掉那一行即可（改完即时生效，不用重启）。
> 想长期留、又确认过没错的，请挪进《逆水寒常识.md》（那份是人工维护的真源）。
>
> 只保留最近 3 批。
"""

PENDING_HEAD = """# 逆水寒待核实（机器自学、但自己没把握的）

> `nsh_learn.py` 把**模型自己都不敢确定**的说法丢在这里。
> ⚠️ **这份文件不注入机器人** —— 宁可让它认怂说「这个没听过」，也不能教它一个可能是错的东西。
> 有空核实一下：确认无误就挪进《逆水寒常识.md》，错的直接删。
"""


# ------------------------------------------------------------------ 小工具
def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, type(default)) else default
    except Exception:
        return default


def _save_json(path, obj):
    try:
        from wxauto.atomic import atomic_write_json
        atomic_write_json(path, obj)
    except Exception:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2)


def _key(m):
    return "%d|%s" % (int(m.get("ts") or 0), (m.get("text") or "")[:80])


def _hits(text):
    t = (text or "").lower()
    return any(w.lower() in t for w in TRIGGERS)


def pick(limit, skip, min_age=0.0):
    """从台账里挑还没学过的、像逆水寒的消息（按时间正序返回）。"""
    d = _load_json(CHAT, {})
    msgs = d.get("messages") or []
    now = time.time()
    out = []
    for m in reversed(msgs):
        if len(out) >= limit:
            break
        if not isinstance(m, dict) or m.get("self"):
            continue
        txt = (m.get("text") or "").strip()
        if len(txt) < 4:
            continue
        if min_age and (now - float(m.get("ts") or 0)) < min_age:
            continue
        if not _hits(txt):
            continue
        if _key(m) in skip:
            continue
        out.append(m)
    out.reverse()
    return out


# ------------------------------------------------------------------ 已知词
# 从常识库文本里扒词时，这些字一旦出现就说明那是个句子、不是术语。
_KNOWN_BAD = set("的了吗呢吧啊是不别没这那你我他她它用来做去说道会能要就都很个"
                 "把被给让对从和与及或等顿")


def _ok_known(w):
    if not (1 < len(w) <= 6) or " " in w:
        return False
    return not any(ch in _KNOWN_BAD for ch in w)


def known_from_library():
    """《逆水寒常识.md》里已经解释过的词 —— 让模型别再重复列一遍。

    不这么做的话，台账里每出现一次「重塑丹」，模型就再解释一遍，
    自学区迅速被常识库已有的词灌满（既费 token 又可能和常识库打架）。
    """
    words = set()
    try:
        txt = (ROOT / "逆水寒常识.md").read_text(encoding="utf-8")
    except Exception:
        return words
    segs = [m.group(1) for m in re.finditer(r"\*\*([^*\n]{1,40})\*\*", txt)]
    for line in txt.splitlines():
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if cells:
                segs.append(cells[0])
    for seg in segs:
        for w in re.split(r"[／/、，,（）()|]+", seg):
            w = w.strip(" 　*：:")
            if _ok_known(w):
                words.add(w)
    return words


# ------------------------------------------------------------------ 模型
def _parse_array(s):
    s = (s or "").strip()
    i, j = s.find("["), s.rfind("]")
    if i < 0 or j <= i:
        return []
    try:
        arr = json.loads(s[i:j + 1])
    except Exception:
        return []
    out = []
    for it in arr if isinstance(arr, list) else []:
        if not isinstance(it, dict):
            continue
        term = str(it.get("term") or "").strip()
        mean = str(it.get("mean") or "").strip()
        conf = it.get("confident", False)
        if isinstance(conf, str):
            conf = conf.strip().lower() in ("true", "1", "yes", "是")
        if term and mean and len(term) <= 20 and len(mean) <= 200:
            out.append({"term": term, "mean": mean, "confident": bool(conf)})
    return out


def ask(cfg, msgs, known):
    lines = "\n".join("- %s：%s" % (m.get("sender") or "群友", m.get("text") or "")
                      for m in msgs)
    user = "群聊记录：\n" + lines
    if known:
        user += "\n\n已知词（不用再列）：" + "、".join(sorted(known)[:200])
    base = str(getattr(cfg, "base_url", "")).rstrip("/")
    payload = {
        "model": getattr(cfg, "model", "deepseek-chat"),
        "temperature": 0.2,
        "max_tokens": 1500,
        "messages": [{"role": "system", "content": SYS},
                     {"role": "user", "content": user}],
    }
    req = urllib.request.Request(base + "/chat/completions",
                                 data=json.dumps(payload).encode("utf-8"),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer %s" % getattr(cfg, "api_key", ""))
    with urllib.request.urlopen(req, timeout=float(getattr(cfg, "timeout", 60))) as resp:
        d = json.loads(resp.read().decode("utf-8"))
    txt = d["choices"][0]["message"]["content"]
    return _parse_array(txt)


# ------------------------------------------------------------------ 落盘
def _trim_sections(text, keep=3):
    """只保留最近 keep 个 `## ` 小节（连同开头的说明）。"""
    parts = text.split("\n## ")
    if len(parts) <= keep + 1:
        return text
    head = parts[0].rstrip()
    return head + "\n\n## " + "\n## ".join(parts[-keep:])


def _append(path, head, title, items, note):
    old = ""
    try:
        if path.exists():
            old = path.read_text(encoding="utf-8")
    except Exception:
        old = ""
    if not old.strip():
        old = head
    body = "\n".join("- **%s** —— %s" % (it["term"], it["mean"]) for it in items)
    sec = "\n## %s\n\n> %s\n\n%s\n" % (title, note, body)
    try:
        from wxauto.atomic import atomic_write
        atomic_write(path, _trim_sections(old.rstrip() + sec))
    except Exception:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(_trim_sections(old.rstrip() + sec))


def main(argv):
    dry = "--dry-run" in argv
    limit = 40
    keep = 3
    min_age = 60.0
    for i, a in enumerate(argv):
        if a == "--limit" and i + 1 < len(argv):
            limit = max(1, int(argv[i + 1]))
        if a == "--keep" and i + 1 < len(argv):
            keep = max(1, int(argv[i + 1]))
        if a == "--min-age" and i + 1 < len(argv):
            min_age = float(argv[i + 1])

    state = _load_json(STATE, {})
    skip = set(state.get("processed") or [])
    vocab = _load_json(VOCAB, {})

    msgs = pick(limit, skip, min_age=min_age)
    if not msgs:
        print("没有新东西可学（台账里没有新的逆水寒消息）")
        return 0

    t0 = time.strftime("%m-%d %H:%M", time.localtime(msgs[0]["ts"]))
    t1 = time.strftime("%m-%d %H:%M", time.localtime(msgs[-1]["ts"]))
    print("挑出 %d 条待学消息（%s ~ %s）" % (len(msgs), t0, t1))
    if dry:
        for m in msgs:
            print("  - %s：%s" % (m.get("sender") or "群友", (m.get("text") or "")[:70]))
        print("（--dry-run：没有联网、没有写文件）")
        return 0

    try:
        from wxauto import load_config
        cfg = load_config().llm
        # API Key 的第三个来源，口径跟 main_android.apply_key_file 一致：
        # config.yaml / 环境变量优先，其次才是同目录的 .deepseek_key。
        if not str(getattr(cfg, "api_key", "") or ""):
            kf = ROOT / ".deepseek_key"
            if kf.exists():
                cfg.api_key = kf.read_text(encoding="utf-8").strip()
    except Exception as e:
        print("读配置失败：%s" % e)
        return 2
    if not str(getattr(cfg, "api_key", "") or ""):
        print("没有 API Key（config.yaml 或 .deepseek_key 里都没有），学不了")
        return 2

    try:
        items = ask(cfg, msgs, set(vocab.keys()) | known_from_library())
    except urllib.error.HTTPError as e:
        print("模型接口 HTTP %s：%s" % (e.code, e.read().decode("utf-8", "replace")[:200]))
        return 2
    except Exception as e:
        print("问模型失败：%s" % e)
        return 2

    if not items:
        print("模型没归纳出东西（这轮不算学，下次再试）")
        return 0

    # 同一个词只留第一次；已经学过的不用再写
    uniq, seen = [], set()
    for it in items:
        if it["term"] in seen:
            continue
        seen.add(it["term"])
        uniq.append(it)

    ok = [it for it in uniq if it["confident"]]
    unsure = [it for it in uniq if not it["confident"]]
    date = time.strftime("%Y-%m-%d %H:%M")
    note = ("来源：群聊台账 %d 条（%s ~ %s）· 由 %s 归纳 · 自动学习，可能有错"
            % (len(msgs), t0, t1, getattr(cfg, "model", "?")))

    if ok:
        _append(AUTO, AUTO_HEAD, date, ok, note)
    if unsure:
        _append(PENDING, PENDING_HEAD, date,
                unsure, note + " · ⚠️ 模型自己没把握，**未注入机器人**")

    for it in uniq:
        if it["term"] not in vocab:
            vocab[it["term"]] = {"mean": it["mean"],
                                 "confident": it["confident"],
                                 "ts": time.time()}
    _save_json(VOCAB, vocab)

    done = list(skip) + [_key(m) for m in msgs]
    state = {"processed": done[-3000:], "last_run": time.time(),
             "runs": int(state.get("runs") or 0) + 1}
    _save_json(STATE, state)

    print("学到 %d 条（有把握 %d / 待核实 %d）；术语表累计 %d 个"
          % (len(uniq), len(ok), len(unsure), len(vocab)))
    for it in ok:
        print("  ✓ %s —— %s" % (it["term"], it["mean"]))
    for it in unsure:
        print("  ? %s —— %s" % (it["term"], it["mean"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
