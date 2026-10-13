# -*- coding: utf-8 -*-
"""自测：只验逻辑，不碰微信、不联网。

    python selftest.py
"""
import os
import random
import sys
import time

# ★ 2026-10-06：自检必须跑在手机本地时区（Asia/Shanghai）下 ——
#   bot.sh 里是 `export TZ=Asia/Shanghai`，手动跑 selftest 却没设，于是
#   `datetime.now()` 拿到 UTC，跟窗口/静默时段有关的用例全都会错位
#   （当时的症状是备稿用例挂掉；那条线 2026-10-07 已删，但时区本身照样要钉死）。
if os.environ.get("TZ") != "Asia/Shanghai":
    os.environ["TZ"] = "Asia/Shanghai"
    try:
        time.tzset()
    except Exception:
        pass

from wxauto.brain import Brain
from wxauto.config import load_config, load_persona, load_rules
from wxauto.governor import Governor, _parse_range
from wxauto.matcher import Matcher, Rule
from wxauto.reader import Message, Block, normalize
from wxauto.state import State
import types as _types          # 2026-10-08：构造假 snap 用来体检 user prompt

PASS, FAIL = [], []


def check(name, got, want):
    if got == want:
        PASS.append(name)
        print(f"  [OK]   {name}: {got!r}")
    else:
        FAIL.append(name)
        print(f"  [FAIL] {name}: got {got!r}, want {want!r}")


print("=" * 66)
print("1) 关键词匹配")
print("=" * 66)
cfg = load_config()
cfg.debug.log_file = ""      # 自测输出别污染 bot.log
rng = random.Random(42)
rule_objs = [Rule.from_dict(d) for d in load_rules()]
matcher = Matcher(rule_objs, rng)

cases = [
    ("？", "问号"),
    ("你在干嘛？", "问号"),
    # ★ 2026-10-05：用户点名删掉「六」这条规则（keywords ["6"]，老被照片上的
    #   数字误触 —— "他把图片读成乱七八糟的文字"），所以 6 / 666 现在都不该命中。
    ("6", None),
    ("666", None),
    ("这也太6了吧今天天气真好啊", None),
    ("笑死我了", "笑死"),                  # 2026-10-05 用户加的单字规则，压过「笑」发图
    ("绷不住了", "笑"),
    ("有萝莉吗", "萝莉"),
    ("看看腿", "看看腿"),
    ("甜妹呢", "甜妹"),
    ("御姐在哪", "御姐"),
    ("无敌", "无敌"),
    ("打飞机", "飞机"),
    ("cw", None),                          # cw 规则 2026-10-04 已按用户要求删掉
    ("dd", "dd"),
    ("滴滴", "滴滴"),
    ("想恋了", None),                      # 「恋」规则 2026-10-04 已删（只剩关键词表里的别的梗）
    ("今天上班好累", "摆烂"),               # "好累"现在被摆烂（发图）规则收走
    ("明天要开会", None),                  # 真正什么都不命中的例子
]
for text, want_name in cases:
    r = matcher.match(text)
    got = r.name if r else None
    check(f"match({text!r})", got, want_name)

print()
print("--- 被 @ 时的关键词让位（mention_exempt）---")
ex = (lambda r: r.mention_exempt)
r = matcher.match("你觉得呢？", skip=ex)
check("被@时'问号'让位给模型", r.name if r else None, None)
r = matcher.match("6不6", skip=ex)
check("被@时'六'让位给模型", r.name if r else None, None)
r = matcher.match("看看腿", skip=ex)
check("被@时强梗仍直接回", r.name if r else None, "看看腿")
r = matcher.match("有萝莉吗", skip=ex)
check("被@时'萝莉'仍直接回", r.name if r else None, "萝莉")

print()
print("=" * 66)
print("2) 节流")
print("=" * 66)
# ★ 2026-10-10（测试卫生，踩过）：这一段用**假时钟**（1000 / 5000 秒），而
#   Governor 默认会把发送时间戳写进**真实的** governor_sent.json —— 于是
#   **第二次跑 selftest 就失败**：文件里留着的 1000.0 让 `now - last = 0 < min_gap`，
#   "首次允许"被拦（实测：第一次 113/0，第二次 112/1，我差点当成自己改坏了）。
#   把 _STATE 指到临时文件，跑多少次都干净。
import tempfile as _tempfile
import wxauto.governor as _govmod
_govmod._STATE = os.path.join(_tempfile.mkdtemp(prefix="govtest-"),
                              "governor_sent.json")
clock = {"t": 1000.0}


def fake_clock():
    return clock["t"]


gov = Governor(cfg, clock=fake_clock)
check("首次允许", gov.check()[0], True)
gov.record()
check("间隔不足被拦", gov.check()[0], False)
clock["t"] += cfg.pace.min_reply_gap + 1
check("过了最小间隔允许", gov.check()[0], True)

check("解析静默时段 02:00-08:00", _parse_range("02:00-08:00"), (120, 480))
check("解析跨零点 23:30-08:00", _parse_range("23:30-08:00"), (1410, 480))


class FakeDT:
    def __init__(self, h, m):
        self.hour, self.minute = h, m


gov2 = Governor(cfg, clock=fake_clock)
# 2026-10-03 用户要求深夜也主动开口，quiet_hours 已清空（config.yaml pace.quiet_hours）。
# 若哪天又开了静默时段，这两条要跟着改回预期。
check("quiet_hours 为空：03:00 不静默", gov2.in_quiet_hours(FakeDT(3, 0)), False)
check("14:00 不在静默", gov2.in_quiet_hours(FakeDT(14, 0)), False)

print()
print("--- 被 @ 时豁免时间闸门，但配额不放（2026-10-05）---")
gm = Governor(cfg, clock=fake_clock)
clock["t"] = 5000.0
gm.record()                                  # 刚回过一条
clock["t"] = 5002.0
check("普通回复仍被最小间隔拦", gm.check()[0], False)
check("@ 我的回复忽略最小间隔", gm.check(mention=True)[0], True)
clock["t"] = 5003.0
gm.record()                                  # 本波第 2 条
clock["t"] = 5005.0
check("@ 我的回复忽略本波连回上限", gm.check(mention=True)[0], True)
# ⚠️ 2026-10-07：原来写死「本分钟第 3 条就满」—— per_minute 早在 2026-10-06 就
#   按用户要求从 3 调到 **6**（整体调小冷却那轮），于是这条变成**假失败**。
#   改成按**当前配置**把本分钟配额填满，以后怎么调都不会再假失败。
_pm = int(getattr(cfg.pace, "per_minute", 6) or 6)
clock["t"] = 5006.0
gm.record()
for _i in range(3, _pm):
    clock["t"] = 5006.5 + _i * 0.1
    gm.record()
clock["t"] = 5008.0
check("@ 我仍受每分钟配额约束（防刷屏）", gm.check(mention=True)[0], False)
gm_off = Governor(cfg, clock=fake_clock)
gm_off.mention_bypass = False
clock["t"] = 5020.0
gm_off.record()
# ⚠️ 同上：别用「过了 10 秒」这种贴着旧 min_reply_gap(=10) 的写法 ——
#   现在 min_reply_gap 是 3 秒，10 秒早就放行了。只过 0.5 秒，必被老闸门拦。
clock["t"] = 5020.5
check("mention_bypass=false 时 @ 也走老闸门", gm_off.check(mention=True)[0], False)

print()
print("--- 群内播报（2026-10-06 用户要求已删除）---")
# enabled 现在是用户可随时开关的，不断言具体值，只断言结构完整 + 文案还在。
# 当前生效状态：%s
# ★ 2026-10-06 用户要求「机器人播报没有用，删了吧」—— announce 配置和 bot.py 的
#   _announce() 都删了。这里反过来**断言它确实不存在**，防止哪天被误加回来。
check("群内播报已删除（config 里没有 announce 段）", hasattr(cfg, "announce"), False)
# ⚠️ 这里**不能**用 `_Bot` —— 它是 200 多行之后才 import 进来的（第一版就是这么踩的
#   NameError）。直接看源码，反正要验的就是"这个方法不在了"。
check("bot.py 里也没有 def _announce 了",
      "def _announce" not in open("wxauto/bot.py", encoding="utf-8").read(), True)

print()
print("--- 标题行定位（2026-10-05：实战台 10 点后没动静的元凶）---")
# parse_snapshot 原来只看 y0 <= CHAT_TOP+16(88)。虚拟屏上微信标题栏被 freeform
# 窗口装饰挤到 y≈200 时一个词都挑不到 → chat_title 空串 → 机器人判「不在目标群」，
# 实战台从此不再记消息（桥接 /open 走 read_title，同一时刻却匹配成功 → 死循环）。
try:
    from read import wxreader as _wxr

    def _ocr_item(x0, y0, x1, y1, t):
        return [[[x0, y0], [x1, y0], [x1, y1], [x0, y1]], t, 0.99]

    _top = [_ocr_item(190, 55, 300, 80, "...10)"), _ocr_item(110, 57, 150, 78, "12")]
    _low = [_ocr_item(190, 195, 300, 220, "...10)"), _ocr_item(110, 197, 150, 218, "12")]
    _b1 = _wxr.title_band(_top)
    _b2 = _wxr.title_band(_low)
    check("标题在顶部时能定位", bool(_b1 and 55 <= _b1[0] <= 80), True)
    check("标题被挤到 y≈200 也能定位", bool(_b2 and 195 <= _b2[0] <= 220), True)
    # 老规则在下移布局里挑不到任何词 —— 这就是 bug 本身
    _old = [it[1] for it in _low if min(p[1] for p in it[0]) <= _wxr.CHAT_TOP + 16]
    check("老规则(y0<=88)在同样的图上会挑空", _old, [])
    # parse_snapshot 里标题是按 x 排序拼起来的（「12 ...10)」）
    _joined = " ".join(it[1] for it in sorted(_low, key=lambda it: it[0][0][0]))
    check("清洗后的标题仍是 ...10", _wxr.clean_title(_joined), "...10")
except Exception as exc:      # 只有缺 numpy/PIL 才会到这儿
    print(f"  [SKIP] 读屏层不可导入：{exc!r}")

print()
print("=" * 66)
print("3) 去重状态")
print("=" * 66)
st = State()
m1 = Message(blocks=[Block("你好", 700, 200, 60, 24)], is_self=False)
check("新消息首次是新的", st.is_new(m1), True)
st.mark(m1)
check("标记后不再是新的", st.is_new(m1), False)
check("prime 会吞掉历史", st.prime([m1]), 0)

m2 = Message(blocks=[Block("群友甲 在吗", 700, 300, 100, 24)], is_self=False)
check("prime 后的新消息仍是新的", st.is_new(m2), True)

m3 = Message(blocks=[Block("我也是", 1000, 400, 60, 24)], is_self=True)
st.prime([m3])
check("自己发的单独指纹", st.is_new(m3), False)

# ★ 2026-10-08 晚（审查报告 §209）：指纹必须算上**发送者** ——
#   群里两个人先后说同一句（"笑死""+1"），原来第二条会被当成重复直接吞掉。
m4 = Message(blocks=[Block("笑死", 700, 500, 60, 24)], is_self=False, sender="张三")
m5 = Message(blocks=[Block("笑死", 700, 600, 60, 24)], is_self=False, sender="李四")
m6 = Message(blocks=[Block("笑死", 700, 700, 60, 24)], is_self=False, sender="张三")
check("张三说「笑死」是新的", st.is_new(m4), True)
st.mark(m4)
check("李四说同一句也算新的（不互吞）", st.is_new(m5), True)
check("张三再说一遍仍算重复（同一条只处理一次）", st.is_new(m6), False)
_nos = Message(blocks=[Block("笑死", 700, 800, 60, 24)], is_self=False)
check("读不出发送者时退化成原行为", State().fingerprint(m4) != State().fingerprint(_nos), True)

print()
print("=" * 66)
print("4) 判断层本地逻辑")
print("=" * 66)
brain = Brain(cfg, load_persona(), None, rng)
check("has API key", brain.available, bool(cfg.llm.api_key))
# ★ v2.0.0：昵称从 config.yaml 读，别写死作者的名字（否则别人装完自测必挂）
_ME = ""
try:
    _ME = str((cfg.me.nicknames or [""])[0])
except Exception:
    _ME = ""
check("is_mention('%s 在吗')" % _ME, brain.is_mention("%s 在吗" % _ME), True)
check("is_mention('@%s（挂机） 上号')" % _ME,
      brain.is_mention("@%s（挂机） 上号" % _ME), True)
check("is_mention('今天天气不错')", brain.is_mention("今天天气不错"), False)
print(f"       score_topic('今天打三角洲吗') = {brain.score_topic('今天打三角洲吗')}")
print(f"       score_topic('有萝莉吗')       = {brain.score_topic('有萝莉吗')}")
print(f"       score_topic('嗯')             = {brain.score_topic('嗯')}")
print(f"       score_topic('明天要开会')     = {brain.score_topic('明天要开会')}")
check("软避雷命中(插话场景)", brain.should_skip("今天又要加班了") is not None, True)
check("软避雷不拦被@", brain.should_skip("今天又要加班了", hard=True), None)
check("硬避雷拦被@", brain.should_skip("帮我转账200", hard=True) is not None, True)
check("避雷词未命中", brain.should_skip("今天打三角洲"), None)

print()
print("=" * 66)
print("5) 人设提示词")
print("=" * 66)
sp = brain.system_prompt()
print(f"  system prompt 长度: {len(sp)} 字（约 {len(sp) // 2} token）")
print(f"  含人设: {'过期男大' in sp}")
print(f"  含输出约束: {'波浪号' in sp}")

print()
print("=" * 66)
print("6) 安全阀：出站校验 / 熔断 / 急停")
print("=" * 66)
import os
import tempfile
import time
from pathlib import Path as _P
import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wxauto.paths import DATA_DIR as _DATA

from wxauto.safety import (FailureBreaker, KillSwitch, parse_hotkey,
                           validate_outgoing)

check("正常回复放行", validate_outgoing("看看腿", 40), None)
check("空内容拦下", validate_outgoing("", 40), "内容为空")
check("纯空白拦下", validate_outgoing("   ", 40), "内容为空")
check("超长拦下", validate_outgoing("字" * 41, 40), "太长（41 字 > 40）")
check("异常字符拦下", validate_outgoing("abc\ufffd", 40), "含异常字符（U+FFFD）")
check("控制字符拦下", validate_outgoing("abc\x07", 40), "含异常字符（U+0007）")
check("纯标点短回复放行（「？」「…」2026-10-03 起特批）", validate_outgoing("！！！", 40), None)
check("过长纯符号拦下", validate_outgoing("！！" * 4, 40), "没有实际文字内容")
check("正常短句放行", validate_outgoing("人老了 玩不动了", 40), None)
check("带空格的短句放行", validate_outgoing("上号 人老了还能打", 40), None)

br = FailureBreaker(3)
check("熔断：第1次失败", br.record_failure(), False)
check("熔断：第2次失败", br.record_failure(), False)
check("熔断：第3次失败触发", br.record_failure(), True)
br.record_success()
check("熔断：成功后计数清零", br.streak, 0)

HOTKEY_BASE = 0x4000
check("热键解析 ctrl+alt+q", parse_hotkey("ctrl+alt+q"),
      (0x0002 | 0x0001 | HOTKEY_BASE, 0x51))
check("热键解析 ctrl+shift+f12", parse_hotkey("ctrl+shift+f12"),
      (0x0002 | 0x0004 | HOTKEY_BASE, 0x7B))
check("热键解析 乱写返回 None", parse_hotkey("随便写"), None)
check("热键解析 只给修饰键返回 None", parse_hotkey("ctrl+alt"), None)

tmpdir = tempfile.mkdtemp()
stopfile = _P(tmpdir) / "STOP.txt"
cfg2 = load_config()
cfg2.safety = type("S", (), {"hotkey": "", "stop_file": str(stopfile)})()
ks = KillSwitch(cfg2, None)
check("急停：文件不存在时不触发", ks.check(), None)
stopfile.write_text("stop", encoding="utf-8")
check("急停：文件一出现就触发", ks.check() is not None, True)
check("急停：已标记为 stopped", ks.stopped, True)
check("急停：原因里带文件名", "STOP.txt" in ks.reason, True)

print()
print("=" * 66)
print("7) 热重载（真的动一次 rules.yaml 再还原）")
print("=" * 66)
from wxauto.bot import Bot as _Bot

rules_path = _DATA / "rules.yaml"
backup = rules_path.read_text(encoding="utf-8")
try:
    cfg3 = load_config()
    cfg3.debug.log_file = ""
    b = _Bot(cfg3, dry_run=True)
    before = len(b.matcher.rules)
    check("重载前没有这条规则", b.matcher.match("热重载哨兵"), None)

    # ★ 2026-10-10 评审修复：原来这里是 `write_text`（非原子）+ `os.utime(mtime=未来5秒)`。
    #   两个真实风险：
    #     · 写一半被正在跑的机器人读到 → 半截 YAML → 那次重载报错（2026-10-08 的 M3
    #       修复就是治这个的，而测试自己却在制造它）；
    #     · **中途崩溃会留下一个"未来时间戳"**：机器人把热重载基线记成未来值，
    #       此后真实时间的任何改动（mtime < 基线）**永远检测不到** ——
    #       表现就是"改了规则怎么都不生效"。
    #   `write_text`/atomic_write 本身就更新 mtime（st_mtime 纳秒精度），
    #   与启动时记的基线必然不同，那个 utime 是多余的。
    from wxauto.atomic import atomic_write as _atomic_write
    _atomic_write(rules_path, backup + (
        '\n  - name: 热重载测试\n'
        '    keywords: ["热重载哨兵"]\n'
        '    replies: ["收到"]\n'
        '    priority: 999\n'))

    b.reload_if_changed()
    check("重载后规则数 +1", len(b.matcher.rules), before + 1)
    hit = b.matcher.match("热重载哨兵")
    check("新规则立刻生效", hit.name if hit else None, "热重载测试")
finally:
    _atomic_write(rules_path, backup)     # 原子还原（同上面的理由）

print()
print("=" * 66)
print("8) 配置读写（改完不能把注释弄丢）")
print("=" * 66)
import shutil

from wxauto.settings import get_value as _get
from wxauto.settings import read_text as _read
from wxauto.settings import set_value as _set

cfg_path = _DATA / "config.yaml"
cfg_bak = cfg_path.with_suffix(".yaml.selftest")
shutil.copy(cfg_path, cfg_bak)
orig_key = _get("api_key")
try:
    before = _read()
    before_lines = len(before.splitlines())
    before_comments = len([l for l in before.splitlines()
                           if l.strip().startswith("#")])

    _set("api_key", "sk-selftest-123")
    _set("group", "自测群名")
    check("写进去能读回来", _get("api_key"), "sk-selftest-123")
    check("群名也写进去了", _get("group"), "自测群名")

    after = _read()
    check("总行数没变", len(after.splitlines()), before_lines)
    check("注释一行没丢",
          len([l for l in after.splitlines() if l.strip().startswith("#")]),
          before_comments)
    import yaml as _yaml
    check("api_key_env 没被误伤",
          _yaml.safe_load(after)["llm"]["api_key_env"], "DEEPSEEK_API_KEY")
    # ⚠️ 2026-10-07：别写死 25 —— per_hour 2026-10-06 已按用户要求从 25 调到 **60**。
    #   改成「和改动前一样」，这才是这条测试真正想验的（面板写配置别误伤别的项）。
    check("其他设置还在（面板改配置没误伤 per_hour）",
          _yaml.safe_load(after)["pace"]["per_hour"],
          _yaml.safe_load(before)["pace"]["per_hour"])
finally:
    shutil.move(str(cfg_bak), str(cfg_path))
check("测完已还原", _get("api_key"), orig_key)

print()
print("=" * 66)
print("9) 表情包库（规则 action=image 能否真的挑到图）")
print("=" * 66)
from wxauto.stickers import IMAGE_EXTS, StickerLibrary

lib = StickerLibrary(_DATA / "表情包")
# ★ 2026-10-08 晚：StickerLibrary 没有 exists()（审查时按死代码删掉了），
#   这里原来会 AttributeError，**直接崩掉整个自检**（第 9 项之后全不跑）。
check("表情包目录存在", (_DATA / "表情包").is_dir(), True)
check("根目录有图", lib.count("") > 0, True)

img_rules = [r for r in rule_objs if r.is_image]
check("有启用了表情包的规则", len(img_rules) > 0, True)
check("每条表情包规则都能挑到图（image_tag 没写错）",
      [r.name for r in img_rules if lib.count(r.image_tag) == 0], [])

picked = lib.pick("笑")
check("pick() 给的是真实存在的图片文件",
      picked is not None and picked.is_file(), True)
check("pick() 的后缀在允许列表里",
      picked is not None and picked.suffix.lower() in IMAGE_EXTS, True)

one_tag = "笑"
names = [p.name for p in lib._files(one_tag)]
check("count() 和实际候选数一致", lib.count(one_tag), len(names))
check("按标签挑图只收名字里带该关键词的图",
      all(one_tag in n for n in names), True)
check("同一个标签下了多个关键词也能合并候选",
      lib.count("笑,哈哈") >= lib.count("笑"), True)
check("标签写错时退回根目录（不会返回空）",
      lib.pick("这个标签肯定不存在") is not None, True)
check("候选被 avoid 光了也照样给一张",
      lib.pick(one_tag, avoid=names) is not None, True)

print()
print("=" * 66)
print("9) 说过的话 + 拒稿反馈（2026-10-05 用户要求）")
print("=" * 66)
# 用户原话：「发出去的也能记住对吧？别光记不发的」+「不让发的就下次注意不要这样说」。
# ⚠️ 2026-10-07：这一节原来还测「白天备稿」（机器人窗口外自己写稿、等主人点头）——
#    待审草稿那条线已经按用户要求整体删掉（交接说明 §46.8）。现在只剩两条：
#    「说过的话（sent.jsonl）」和「拒稿反馈（rejects.jsonl）」。
from wxauto import drafts as _drafts

# ⚠️ 这两个文件是**真数据**（你发过的话 / 你没看上过的说法），
#    测试会清空并写入，所以跑完必须原样还回去。
_bak = {}
for _p in (_drafts.REJECTS_PATH, _drafts.SENT_PATH):
    try:
        with open(_p, encoding="utf-8") as _fh:
            _bak[_p] = _fh.read()
    except Exception:
        _bak[_p] = None
try:
    for _p in (_drafts.REJECTS_PATH, _drafts.SENT_PATH):
        try:
            os.unlink(_p)
        except Exception:
            pass

    cfg4 = load_config()
    cfg4.debug.log_file = ""
    bb = _Bot(cfg4, dry_run=True)
    bb.brain.cfg.api_key = "测试用的假 key（不联网）"   # available 是只读属性

    # ---- 拒稿反馈：面板点「不发」走的就是 note_reject() ----
    check("拒稿记下来了（下次别这么说）",
          _drafts.note_reject("测试不要这句喵", src="群里原话", who="测试君",
                              why="面板：不发了"), True)
    check("同一个说法会进 recent_rejects()",
          "测试不要这句喵" in _drafts.recent_rejects(), True)

    # 反馈要真的进提示词
    b4 = Brain(cfg4, load_persona(), None, personas=bb.brain.personas)
    p4 = b4.system_prompt()
    # ★ 2026-10-08 审查修复（C1）改了位置：否稿反馈原来拼在 system prompt 末尾，
    #   而它每被否一条就变一次 —— 一变整个 system 前缀的缓存就失效（而那个缓存
    #   本来还因为另一个 bug 恒不命中，每轮重拼 6818 字）。现在它拼在
    #   **user prompt 末尾**，system 只留稳定前缀。断言跟着改成三件事一起守。
    _s4 = _types.SimpleNamespace(chat_title="自测群", messages=[],
                                 tail=lambda n: [])
    _m4 = _types.SimpleNamespace(text="在吗", is_self=False, sender="测试君")
    u4 = b4._user_prompt(_s4, _m4, "自测")
    check("否掉的说法进了 user prompt", "测试不要这句喵" in u4, True)
    check("user prompt 里有「别再照着说」的说明", "主人否掉过的说法" in u4, True)
    check("否稿反馈**不再**进 system prompt（否则前缀缓存会失效）",
          "测试不要这句喵" in p4, False)
    check("system prompt 是稳定前缀（同样输入两次结果一致）",
          b4.system_prompt() == p4, True)

    # ★ 用户 2026-10-05 下午补的要求：「发出去的也能记住对吧？别光记不发的」
    #   目的是攒素材，以后总结他要的那种说话风格。
    _drafts.note_sent("主人点头的这句喵", origin="approve", src="群里原话", who="群友A")
    _drafts.note_sent("机器人原稿喵", origin="approve")
    _drafts.note_sent("改完的这句喵", origin="edited", draft="机器人原稿喵")
    _drafts.note_sent("它自己回的那句喵", origin="reply")
    _st = _drafts.stats()
    check("发出去的也记流水", _st["sent"], 4)
    check("其中「主人的意思」3 条", _st["sent_user"], 3)
    check("最近一条主人的意思就是改完那句",
          _drafts.recent_sent(1, only_user=True)[0]["text"], "改完的这句喵")
    check("能分清是谁发的（approve/edited/reply）",
          sorted(set(_r["origin"] for _r in _drafts.recent_sent(10))),
          ["approve", "edited", "reply"])
    check("改过的样本保留着原稿",
          [r["draft"] for r in _drafts.recent_sent(10)
           if r["origin"] == "edited"], ["机器人原稿喵"])
finally:
    for _p, _txt in _bak.items():
        try:
            if _txt is None:
                os.unlink(_p)
            else:
                with open(_p, "w", encoding="utf-8") as _fh:
                    _fh.write(_txt)
        except Exception:
            pass

# ---------------------------------------------------------------------------
# 面板页面 JS 语法体检
#
# 为什么单列这一项：botctl.py 里的 <script> 是**内嵌在 Python 三引号字符串**里的，
# 写错转义（比如该写 \\n 却写了 \n）会变成真换行 → 整段 script 语法错误 →
# 浏览器一句都不执行 → 实战台的消息列表、草稿卡片全部空着。
# 2026-10-05 用户报「实战台看不见群友发的所有消息」就是栽在这上面
# （备稿卡片那次改动引入的，页面看着"没报错"，功能整段是死的）。
#
# 面板在跑就直接找它要页面（最真）；没在跑就跳过 —— 不为了这条去起面板。
print()
print("=" * 66)
print("面板页面 JS 语法")
print("=" * 66)
try:
    import re as _re
    import subprocess as _sp
    import urllib.request as _ur
    with _ur.urlopen("http://127.0.0.1:8790/", timeout=8) as _r:
        _html = _r.read().decode("utf-8", "replace")
    _scripts = [x for x in _re.findall(r"<script[^>]*>(.*?)</script>", _html, _re.S) if x.strip()]
    _bad = []
    for _i, _js in enumerate(_scripts):
        _fn = "/tmp/dsh-panel-js-%d.js" % _i
        with open(_fn, "w", encoding="utf-8") as _fh:
            _fh.write(_js)
        _res = _sp.run(["node", "--check", _fn], capture_output=True, text=True)
        if _res.returncode:
            _lines = [l for l in (_res.stderr or "").strip().splitlines() if l.strip()][:3]
            _bad.append("第 %d 段：%s" % (_i, " / ".join(_lines)))
    check("面板页面 %d 段 JS 语法全对" % len(_scripts), _bad, [])
except Exception as _exc:
    print("  [跳过] 面板没在跑（%s）—— 起面板后重跑本文件可查这项"
          % type(_exc).__name__)

print()
print("=" * 66)
print(f"结果：通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败项：" + ", ".join(FAIL))
print("=" * 66)
sys.exit(1 if FAIL else 0)
