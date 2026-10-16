#!/bin/sh
# rike-enter.sh —— 冷启动 → 主世界 → 青崖书（副屏模式）
#   用法: sh rike-enter.sh [--restart]
#
# ★★ 2026-10-09 第五次重写（探针版 + 流程提速）。两件事分开看：
#
# 【一】等待机制：换成 tools/rike-probe.cjs 廉价探针（不再"每轮烧一次全屏 OCR"）
#   旧版一轮 = 抓帧 + 放大200%(1.4s) + 全屏 tesseract(2.6~6s) + sleep2 ≈ 6s，只是"看一眼"；
#   且 `_w+=4` 与真实 6s 错配 → 标称 90s 实际等 130s+。新版：64px 小帧签名（单次 16~85ms），
#   **画面在动就不 OCR**，静止才看一次、同一画面只 OCR 一次，
#   OCR 先走原图快路径（1.2~2.5s，8 张历史帧召回与 200% 放大完全一致），没认出才兜底。
#
# 【二】流程本身：实测发现"点击被吞"才是 159 秒里的最大浪费（45s + 59s 两段干等）。
#   病根：点完只等**下一个特定页面**，一旦点击被吞（页面没变）→ 正则永远不命中 → 干等到超时。
#   改法：`click()` = 点一下 → 立刻用 **ANY（含当前页特征）** 重新识别当前是什么页 → 交给下一轮判断。
#      · 点击生效 → 新页面 OCR 0.5~2s → 立刻推进；
#      · 点击被吞 → 缓存秒命中"还是这一页" → 下一轮**自动重点一次**（同一页最多重点 3 次，
#        之后改为纯等待，避免对着加载中的按钮狂点）。
#      · "正在初始化游戏"单独判：那时登录按钮不可点，点也是白点 → 只等不点。
#   坐标/判断顺序/特征词一律沿用实测值（技能 §2.2），本次未改任何坐标。
#
# 【三】2026-10-09 冷启动 5 轮实测又挖出两条硬结论（都是"卡 45 秒"的真身）：
#   · 标题页的特征词**不能用「闯荡江湖」**（发光美术字，OCR 读不出，实测 0 命中）→
#     改用「**缘定三生**」（服务器名，画面右侧那一行，实测稳定可读）；
#   · 主世界**根本没有稳定特征词**（「侧图标定位」那行提示实测不在画面上；场景文字随场景变）→
#     按技能正解走**行为判据**：点一下日课按钮 (1348,160)，能开出青崖书 = 已经在主世界。
#     所以"认不出页面"不再傻等 3 轮，而是直接去试开青崖书（开不出再关活动弹窗试一次）。
. "$(dirname "$0")/../env.sh"
SLOT=${RIKE_SLOT:-1}
# ⚠️ 2026-10-05：displayId 每次重建虚拟屏都会变，写死必错（严重时游戏会掉到物理屏）。
if [ -z "$RIKE_DISPLAY" ]; then
  ID=$(curl -s --max-time 5 "http://127.0.0.1:$VSD_PORT/$SLOT/info" | sed -n 's/.*"displayId":\([0-9]*\).*/\1/p')
fi
ID=${RIKE_DISPLAY:-$ID}
[ -n "$ID" ] || { echo "❌ 查不到副屏 $SLOT 的 displayId（守护没跑？）—— 先跑 sh ensure-vsd.sh"; exit 1; }
VS="$DSH_ROOT/tools/vscreen"; SHD="$DSH_ROOT/tools/shd.sh"; PROBE="$DSH_ROOT/tools/rike-probe.cjs"
# ★ 2026-10-11：**进场前先把副屏锁成横屏** —— 否则剧组站那类竖屏模块会把副屏整个转 90°
#   （逻辑尺寸 1400×636 → 636×1400），而截图仍是 1400×636、`input` 却按逻辑坐标走
#   → 两套坐标系差 90°，表现成"点了没反应/点到别的元素"。详见 tools/rike-landscape.sh。
sh "$DSH_ROOT/tools/rike-landscape.sh" "$SLOT" 2>&1 | sed 's/^/  /' 
PKG=com.netease.nshm; ACT=$PKG/com.netease.ntunisdk.external.protocol.ProtocolLauncher
TRACE=/tmp/rike-enter-trace.tsv; OCRF=/tmp/rike-probe.ocr
: > "$TRACE"

# ★ 2026-10-10：进场点击也先**等画面静止**再点。
#   实测吞点击主因不是注入方式，而是"游戏在加载/准备界面时抑制输入"
#   （同批实验：关面板 16/16 成功，开面板仅 18/24）→ 别在加载的那几帧点。
#   settle 纯采样零 OCR（~0.35s）；画面本来就在动时会等满 timeout 再点（默认 0.9s，不阻塞）。
press(){ node "$PROBE" settle --still 2 --gap 80 --timeout "${3:-0.6}" >/dev/null 2>&1
         node "$VS" -i "$SLOT" swipe "$1" "$2" "$1" "$(( $2 + 6 ))" 120 >/dev/null 2>&1
         # 同时上报给直播页（用户能看见我点了哪儿）；没起服务时静默失败
         curl -s -m 0.4 -X POST -H 'Content-Type: application/json' \
           -d "$(printf '{"x":%s,"y":%s}' "$1" "$2")" \
           "http://127.0.0.1:${RIKE_LIVE_PORT:-18800}/click" >/dev/null 2>&1 & }
has(){ grep -qE "$1" "$OCRF" 2>/dev/null; }   # ★ 必须 -E（ERE）：BRE 下 a|b 是字面量，"青崖书|青崖韦"永远匹配不到
hitword(){ grep -oE "$1" "$OCRF" 2>/dev/null | head -1; }
T0=$(date +%s)
NOW(){ echo "+$(( $(date +%s) - T0 ))s"; }
SIG=""
probe(){   # $1=正则  $2=超时秒；0=命中（文本在 $OCRF）
  out=$(node "$PROBE" wait --match "$1" --timeout "$2" --slot "$SLOT" --trace "$TRACE" 2>&1)
  rc=$?
  SIG=$(sed -n 's/^sig=//p' /tmp/rike-probe.meta 2>/dev/null)
  return $rc
}

# 页面特征词（ANY 用于"点完随便出现什么已知页面都算进展"）
# ★ 标题页特征词 =「缘定三生」（服务器名，实测读得出）；「闯荡江湖」是发光美术字，OCR **读不出来**
#   （2026-10-09 冷启动 4 轮实测：卡在标题页 45 秒认不出，全因旧词表只有「闯荡江湖」）
TITLE='缘定三生|闯荡江湖'
ANY="其他账号登录|上次登录|正在初始化游戏|$TITLE|登录展示|进入游戏|顶号|已经登录|青崖书|青崖韦|青岩书|上岩书|殊途同归|侧图标定位|日课点"
NOTINIT="其他账号登录|上次登录|$TITLE|登录展示|进入游戏|青崖书|青崖韦|青岩书|上岩书|殊途同归|侧图标定位|日课点"
# ⚠️「殊途同归」是**弱判据**：实测「段位（殊途同归）」子页面的标题也带它 → 光凭它会误判，
#   所以配一个排除词「段位」（见 is_qy）。「日课点」是青崖书专有，最硬。
QYPAT='青崖书|青崖韦|青岩书|上岩书|日课点|殊途同归'
# 🚨 2026-10-10：排除词从「段位」改成「**段位重置**」——
#   RapidOCR 能读出青崖书上的「活力领奖段位」，用「段位」会把青崖书自己否掉
#   （实测进场因此绕到段位子页，白花 100 多秒）。「段位重置倒计时」才是段位页专有。
is_qy(){ has "$QYPAT" && ! has '段位重置'; }

[ "$1" = "--restart" ] && { echo "  ▸ 强制重启游戏"; sh "$SHD" "am force-stop $PKG" >/dev/null 2>&1; sleep 3; }
echo "  ▸ am start（display $ID）"
sh "$SHD" "am start --display $ID --windowingMode 1 -n $ACT" >/dev/null 2>&1
echo "  ▸ 等初始化完成（探针：画面在动就不 OCR，最多 60s）"
# 60s：冷启动实测到已知页面用 20~42s；给 60s 留余量，又不至于"停在未知页面"时白等太久
if probe "$ANY" 60; then echo "     ✅ $(NOW) 出现已知页面：「$(hitword "$ANY")」"; else echo "     ⚠️ $(NOW) 60s 没等到任何已知页面"; fi

# ★ 2026-10-12 加：进场阶段的**场景逃生**（与 rike.sh 的 escape_scene 同源，这里是轻量版）
#   ── 动机：今天冷启动卡在「**菠萝农场 + 音乐登录弹窗**」上，12 步全空转 237s。
#      两样东西脚本都认不出：① 音乐弹窗带遮罩，**吞掉一切点击**；
#      ② 农场场景里 (1348,160) 不是日课按钮（那里是草地），(1143,100) 也不是活动弹窗。
#   ── 实测有效的完整逃生链（当天人工走了 5~6 步）：
#      音乐弹窗 →(取消 568,403) 农场日志浮层 →(离开此地 1076,158) 确认框 →(确认 1293,316)
#      → 落点常是「他人庄园」（不是主世界）→ 再走一遍 离开此地 + 确认 → 真回主世界。
#   ── 纪律：命中**专有词**才动；每次最多动一次手（靠外层轮数提供重复机会）。
farm_escape(){
  node "$VS" -i "$SLOT" shot /tmp/rike-enter-esc.jpg >/dev/null 2>&1
  T=$(sh "$DSH_ROOT/tools/rike-ocr.sh" /tmp/rike-enter-esc.jpg 2>/dev/null)
  # 🚪 ★ 2026-10-14 加：**月卡嘉赠签到页**（用户口径：每天登录先领月卡）。
  #   冷启动/重新登录后会盖一张全屏签到页（左侧竖排「月卡嘉赠」、右下金色「领取」），
  #   脚本原本认不出它 → 12 步全空转 247s（§9 第 74 条）。
  #   出口只有一个：「领取」= OCR 文字中心 **(1170,576)**（文字框实测 1147~1193 × 564~589）。
  #   ⚠️ 这一下**常被吞**（当天点到第 3 次才生效）→ 连点两次。
  #   ⚠️ 领取后落点是**角色选择页的 3D 背景（UI 未渲染、OCR 几乎读不出）**，
  #      下一步就是「进入游戏」(1180,555) → 顺手补点，否则会一路认不出、空转到最后。
  if printf '%s' "$T" | grep -qE '月卡嘉赠|月卡赠|已签到[0-9]?天'; then
    echo "  🚪 月卡嘉赠签到页 → 点「领取」(1170,576)（每天第一步）"
    press 1170 576; sleep 3
    press 1170 576; sleep 3
    echo "     ▸ 落点是角色选择页背景（UI 未渲染）→ 补点「进入游戏」(1180,555)"
    press 1180 555; sleep 3
    return 0
  fi
  if printf '%s' "$T" | grep -qE '网易云音乐|当前歌曲需要登录'; then
    echo "  🚪 场景逃生：音乐登录弹窗（吞点击）→ 点「取消」(568,403)"
    press 568 403; sleep 2; return 0
  fi
  if printf '%s' "$T" | grep -qE '你确定要离开此地'; then
    echo "  🚪 场景逃生：「离开此地」确认框 → 点「确认」(1293,316)"
    press 1293 316; sleep 2.5; return 0
  fi
  if printf '%s' "$T" | grep -qE '农场日志|农场简报|呵护太阳花|他人庄园|跨服庄园'; then
    echo "  🚪 场景逃生：农场/他人庄园 → 点「离开此地」(1076,158)"
    # ⚠️ ★2026-10-14：「离开此地」图标位置**随场景变** —— 2026-10-12 菠萝农场实测 (1076,158)；
    #   而当天「他人庄园」实测 (1076,158) **连点 7 次全空**、(1132,158) **一次就弹出确认框**（差 56px）。
    #   → 两个都点：先 (1132,158)（他人庄园），再 press (1076,158)（菠萝农场，可能是旧场景的值）。
    #   后一下若落在确认框的遮罩上无害（确认框本体在屏幕中央 y≈250~350，不在这两点上）。
    press 1132 158; sleep 2; press 1076 158; sleep 2.5; return 0
  fi
  return 1
}
step=0; LAST=""; RETRY=0; UNK=0; ENTERED=0   # ENTERED=1 表示确实到了青崖书/主世界
while [ $step -lt 12 ]; do
  step=$((step+1))
  # ① 终点：已在青崖书 / 主世界
  if is_qy; then echo "  ✅ $(NOW) 已在青崖书（第 $step 步）"; ENTERED=1; break; fi
  if has '侧图标定位'; then echo "  ✅ $(NOW) 已在主世界（第 $step 步）"; ENTERED=1; break; fi
  # ② 待机画面（竹林 +「江湖匆匆」）吞掉一切输入，检测词只能是「匆匆」
  if has '匆匆'; then
    echo "  [$step $(NOW)] 待机画面 → 唤醒"
    node "$VS" -i "$SLOT" swipe 300 318 1000 318 300 >/dev/null 2>&1; sleep 3
    LAST=""; RETRY=0; probe "$ANY" 30; continue
  fi
  # ③ 各页面分支（顺序=实测踩坑顺序，别调）
  if has '顶号|已经登录'; then LBL="顶号弹窗"; X=1298; Y=318
  elif has '正在初始化游戏'; then
    echo "  [$step $(NOW)] 登录页【初始化中】→ 只等不点"
    LAST=""; RETRY=0; probe "$NOTINIT" 30; continue
  elif has '其他账号登录|上次登录'; then LBL="登录页"; X=699; Y=398
  elif has '登录展示'; then
    # ★ 2026-10-10 加：**顺便报一下"当前自动登录的是谁"**。
    #   为什么要在这里做：用户口径是固定做玉米鹿，而冷启动自动登录的是
    #   "上次停在的那个角色"；而角色名**只有角色选择页读得出来**
    #   （主世界/青崖书实测读不出，§9 第 46 条）→ 错过这一页就只能靠猜。
    #   成本：一次抓帧 + 裁 470×110+930+425 + OCR ≈ 1s，**不影响控制流**。
    node "$VS" -i "$SLOT" shot /tmp/rike-rolecheck.jpg >/dev/null 2>&1
    convert /tmp/rike-rolecheck.jpg -crop 470x110+930+425 +repage /tmp/rike-rolecheck.png 2>/dev/null
    RN=$(sh "$DSH_ROOT/tools/rike-ocr.sh" /tmp/rike-rolecheck.png 2>/dev/null | tr -d ' \n|')
    printf '%s\n' "${RN:-读不出}" > /tmp/rike-role.txt
    echo "  ★ 当前角色：[${RN:-读不出}]（已写 /tmp/rike-role.txt）"
    LBL="角色选择页"; X=1180; Y=555
  elif has "$TITLE"; then
    echo "  [$step $(NOW)] 标题页 → 「闯荡江湖」(710,495)"
    press 710 495; sleep 8
    : > "$OCRF"          # 清掉旧文本：进主世界后认不出，下一轮会自动去试开青崖书
    UNK=1                # 只等一次就试开青崖书，别再空转
    continue
  elif has '进入游戏'; then
    # ★「进游戏时可能有一条公告」两条路都通（用户 2026-10-09 口径）：
    #   ① 直接点公告下面的「进入游戏」；② 先关掉公告（右上 ✕）→ 回标题页 → 再点「闯荡江湖」。
    #   坐标由**用户提供的公告页截图**标定（物理屏 2780×1264 按比例换算到副屏 1400×636；
    #   两边逻辑宽度都约 800dp，布局一致）：
    #     「进入游戏」= 中心 (757,566)，范围 x617~897 / y545~587（旧记的 (760,556) 仍落在按钮内 ✅）
    #     关闭键 ✕（深蓝菱形）= (1243,99)；关掉后回标题页，判据 = 读出「缘定三生」
    #   ⚠️ 这两个都是**美术字/图形按钮，OCR 读不出**（实测 `ocr-tap "进入游戏"` 在公告页上认不出）
    #      → 只能死点 + 靠"页面变了没"验证，所以下面按次数在两条路之间切换。
    if [ "$LAST" = "公告页" ]; then RETRY=$((RETRY+1)); else RETRY=0; LAST="公告页"; fi
    if [ $RETRY -ge 3 ]; then
      echo "  [$step $(NOW)] 公告页点了 3 次没进展 → 纯等待"; probe "$ANY" 20; continue
    fi
    # ★ 留证据：万一副屏上的公告页与用户截图有出入，这里会自动留帧（debug/last-gonggao.jpg）
    if [ $RETRY -eq 0 ]; then
      node "$VS" -i "$SLOT" shot /sdcard/dsh/debug/last-gonggao.jpg >/dev/null 2>&1
      cp "$OCRF" /sdcard/dsh/debug/last-gonggao.txt 2>/dev/null
      echo "  [$step $(NOW)] 公告页 → 点「进入游戏」(757,566)"
      press 757 566
    elif [ $RETRY -eq 1 ]; then
      echo "  [$step $(NOW)] 公告页还在 → 再点一次「进入游戏」（第一次可能被吞）"
      press 757 566
    else
      # ⚠️ 关公告键 ✕(1243,99) 是**物理屏截图按比例换算**来的，副屏上没验证过；同法换算「宠物」
      #   菜单实测偏了 70px → 这个 ✕ 有可能点空、甚至落到右侧图标栏上，所以只当最后手段，且点完必须验证。
      echo "  [$step $(NOW)] 公告页第 $((RETRY+1)) 次 → 最后手段：点 ✕ 关公告 (1243,99)【坐标待副屏确认】"
      press 1243 99; sleep 1
      if probe "$TITLE" 10; then echo "     ✅ 公告已关、回到标题页 → 下一步点「闯荡江湖」"; continue; fi
      echo "     ⚠️ 没关掉（坐标可能不准）→ 下一轮回到「进入游戏」"
    fi
    UNK=0; sleep 1; probe "$ANY" 25; continue
  elif [ "$UNK" -lt 1 ]; then
    # ④ 认不出：**先试场景逃生**（今天卡死的菠萝农场/音乐弹窗就走这里）。
    #   逃掉一步就 continue，UNK 保持 0 → 下一轮继续逃，直到真的离开那个场景。
    if farm_escape; then continue; fi
    # 没有可逃的场景 → 可能还在加载：先等一小会（不点）
    UNK=1
    echo "  [$step $(NOW)] 认不出页面（可能还在加载）→ 等 10s"
    probe "$ANY" 10; continue
  elif [ "$UNK" -lt 2 ]; then
    if farm_escape; then continue; fi
    # ⑤ ★ 2026-10-10 新增：公告页。它的「进入游戏」是**美术字，整屏 OCR 读不出来**
    #   （当天实测：OCR 只出公告正文，`has '进入游戏'` 永远为假 → 上面那个公告分支根本进不去，
    #    于是被当成"认不出页面"、白去试开青崖书）。这里按坐标试一次。
    #   按钮中心实测 (752,566)；用户截图换算的 (757,566) 也落在按钮内。
    UNK=2
    echo "  [$step $(NOW)] 认不出页面 → 试点公告页「进入游戏」(757,566)"
    press 757 566
    probe "$ANY" 12; continue
  elif [ "$UNK" -lt 4 ]; then
    if farm_escape; then continue; fi
    # ⑥ ★行为判据（技能 §2.2 正解）：主世界没有稳定特征词，那就"点日课按钮看能不能开出青崖书"
    UNK=$((UNK+1))
    echo "  [$step $(NOW)] 认不出页面 → 试开青崖书（能开出来 = 已在主世界）"
    press 1348 160
    if probe "$QYPAT" 12 && is_qy; then echo "  ✅ $(NOW) 青崖书已开（行为判据）"; ENTERED=1; break; fi
    echo "     ▸ 关「新活动速递」(1143,100) 再试一次"
    press 1143 100; press 1348 160
    if probe "$QYPAT" 12 && is_qy; then echo "  ✅ $(NOW) 青崖书已开（关弹窗后）"; ENTERED=1; break; fi
    continue
  else
    if farm_escape; then continue; fi
    # ⑥ 连续认不出：多半是停在某个全屏子页面（段位/帮会/活动页…）——
    #    盲点 (1348,160) 只会越点越偏（实测被带进「段位」页）→ 按左上 ◇ 退一层自愈
    echo "  [$step $(NOW)] 认不出页面 → 按左上 ◇ (50,32) 退一层"
    press 50 32
    UNK=0
    probe "$ANY" 12
    continue
  fi
  # 认出一个已知页面 → 点它（每个分支自己 continue；这里只处理尚未单独写 continue 的页面）
  if [ "$LBL" = "$LAST" ]; then RETRY=$((RETRY+1)); else RETRY=0; LAST="$LBL"; fi
  if [ $RETRY -ge 3 ]; then
    echo "  [$step $(NOW)] 「$LBL」已点 3 次没进展 → 改为纯等待（可能在加载）"
    probe "$ANY" 20; continue
  fi
  UNK=0
  echo "  [$step $(NOW)] $LBL → 点 ($X,$Y)$([ $RETRY -gt 0 ] && echo "（第 $((RETRY+1)) 次）")"
  press "$X" "$Y"; sleep 1
  probe "$ANY" 25      # ★ 关键：不带 --not-sig。点击被吞时缓存会秒命中"还是这一页"→ 下一轮自动重试
done
[ "$ENTERED" = 1 ] || echo "  ⚠️ 12 步都没进主世界 —— 需要人工看"

echo "  ▸ 确认青崖书已开"
if is_qy; then echo "  ✅ 青崖书已开"
elif has '侧图标定位'; then
  echo "     在主世界 → 点日课按钮 (1348,160) 开青崖书"
  press 1348 160; probe "$QYPAT" 15 && echo "  ✅ 青崖书已开" || echo "  ⚠️ 没开出来"
else echo "  ⚠️ 不确定在哪个界面（最后识别：「$(hitword "$ANY")」）"; fi
OCR_N=$(grep -c 'OCR #' "$TRACE" 2>/dev/null)
echo "  ⏱ **总耗时 $(( $(date +%s) - T0 ))s** · 探针 OCR $OCR_N 次"
