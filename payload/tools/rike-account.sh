#!/bin/sh
# rike-account.sh —— 切账号 / 换角色（一条命令走完，每步验证 + 重试）
#
#   sh tools/rike-account.sh role                 # 同账号内换到「另一个角色」
#   sh tools/rike-account.sh account <后4位>      # 切账号（实测有 8922 / 7931 / 1640）
#   sh tools/rike-account.sh who                  # 只在角色选择页时：报告当前选中的角色
#
# ★ 为什么要有它（2026-10-10）：手工一步步做（每步抓帧→裁图→读图→确认）要约
#   **20 次模型往返 ≈ 10 分钟**，而纯设备执行只有 ~2 分钟 —— 慢的是"我每步都停下来看一眼"。
#   把流程 + 验证固化进脚本后，一次调用走完（实测执行 ~90 秒）。
#
# 实测坐标（副屏 1400×636，2026-10-10 两次端到端验证）：
#   角色选择页左上 ◇ **(59,36)** → 「确认要重新登录吗？」→ 确认 **(1278,315)**
#   标题页右上账号图标 **(1350,28)**     ｜ 设置页「切换账号 前往」= 第2个前往 **(1265,252)**
#   设置页「切换角色 前往」= 第1个前往 **(1265,173)**
#   用户中心「切换账号」金按钮 **(1085,162)** ｜ 账号行展开 ▼ ≈**(918,287)**
#   账号行 y：8922→**292** / 7931→**384** / 1640→**484**（行高≈113）
#   「登录」**(700,395)** ｜ 标题页「闯荡江湖」**(710,510)** ｜「进入游戏」**(1180,556)**
#   角色列表：第1行 y≈**127** / 第2行 y≈**213**
#     ⚠️ 列表**按最近登录排序 → 当前角色恒在第 1 行**，所以"换另一个角色"= 点第 2 行。
#     ⚠️ 但两个以上角色或登录态变化时顺序会翻，脚本会在点完后**读角色名区域核对**。
#
# ⚠️ 首次使用请盯一眼输出（尤其"找账号行"那步依赖 OCR 读脱敏手机号，没读出来会明确报错停手）。
. "$(dirname "$0")/../env.sh"
SLOT=${RIKE_SLOT:-1}
VS="node $DSH_ROOT/tools/vscreen -i $SLOT"
OT="$DSH_ROOT/tools/ocr-tap.sh"
SHOT=/tmp/rike-acct.png
L(){ echo "  $*"; }

grab(){ $VS shot "$SHOT" >/dev/null 2>&1; }
tap(){ $VS swipe "$1" "$2" "$1" "$(( $2 + 6 ))" 120 >/dev/null 2>&1; }
T=""
snap(){ grab; T=$(sh "$DSH_ROOT/tools/rike-ocr.sh" "$SHOT" 2>/dev/null); }
has(){ printf '%s' "$T" | grep -qE "$1"; }

is_rolepage(){ has '登录展示'; }
is_qy(){ has '日课点|青崖书|青崖韦|青岩书'; }
is_setting(){ has '切换角色|切换账号'; }
is_usercenter(){ has '用户中心|账号充值|实名登记|登录管理|我的消息'; }
# ★ 2026-10-10 补：账号面板（"其他账号登录/上次登录"）也是"已经在切换流程里"的状态
is_acctpanel(){ has '其他账号登录|上次登录|登录管理'; }
is_menu(){ has '约战|结义|星梦乐园|小暖厨房|珍玩收藏|休闲竞技'; }
is_title(){ has '缘定三生|闯荡江湖'; }

# 点一次就抓帧验证；check 通过即停
do_until(){  # $1=check函数 $2=最多次数 $3=x $4=y $5=描述
  c=$1; n=$2; x=$3; y=$4; d=$5; i=0
  while [ $i -lt "$n" ]; do
    i=$((i+1)); snap
    if $c; then L "✅ $d"; return 0; fi
    L "▸ $d → 点 ($x,$y)（第 $i/$n 次）"; tap "$x" "$y"; sleep 2.8
  done
  snap; $c && { L "✅ $d"; return 0; }
  L "❌ $d 失败（$n 次没生效）"; return 1
}

# ★ 用模板匹配识别"当前选中的角色名"（游戏里是艺术字，OCR 读不出 —— 2026-10-10 实测
#   全屏 OCR 只会读到 "SS0" 这种乱码；但同区域 RMSE 比对区分度 0.000(自己) vs ≥0.159(别人)）
role_of(){
  # ★ 2026-10-10：角色选择页的角色名**OCR 读得出来**（实测「醉梦辰」「玉米鹿」），
  #   比模板直观、也更好排错；模板只在 OCR 读不出时兜底
  #   （实测在**正确区域**上模板也很准：醉梦辰 0.044 vs 清月辞 0.163 / 玉米鹿 0.183）。
  convert "$SHOT" -crop 470x110+930+425 +repage /tmp/acct-name.png 2>/dev/null
  R=$(sh "$DSH_ROOT/tools/rike-ocr.sh" /tmp/acct-name.png 2>/dev/null | tr -d ' \n|')
  case "$R" in
    *玉米鹿*) echo 玉米鹿; return ;;
    *清月辞*) echo 清月辞; return ;;
    *醉梦辰*) echo 醉梦辰; return ;;
  esac
  sh "$DSH_ROOT/tools/rike-find.sh" role "$SHOT" 2>/dev/null | awk '{print $1}'
}

# 在角色列表里确认右下角展示的角色名（人眼可核，OCR 只能当辅助）
role_name(){ convert "$SHOT" -crop 470x110+930+425 +repage -resize 200% /tmp/acct-name.png 2>/dev/null; echo "/tmp/acct-name.png"; }

# 从账号下拉列表里找目标账号行的原图坐标（裁图 → tsv → 定位）
find_row(){  # $1=后4位；输出 "x y"（★ 原图坐标）
  # ★ 2026-10-10：tesseract tsv → RapidOCR 带坐标 tsv（同一 ROI 交给常驻服务，不放大）。
  #   实测账号面板 tesseract **一个词都读不出**（半透明浮层），RapidOCR 能读。
  # 🚨 同日实地踩坑并修正：daemon 的 --tsv 返回的**已经是原图坐标**（ROI 偏移在服务端加好了），
  #   旧版又加一次 (+380/+215)、还带 tesseract 时代的 +60 → 实际点到面板右下角，
  #   **账号根本没切成功**；而旧验证 `has "$ARG"` 只会看"面板里出现过这个号码"
  #   （展开的列表本来就把三个号都列出来）→ 假阳性，一路报"✅ 已选中"。
  sh "$DSH_ROOT/tools/rike-ocr.sh" --tsv --roi 380,215,760,430 "$SHOT" 2>/dev/null \
    | awk -F'\t' -v w="$1" '$1 ~ w {printf "%d %d\n", int(($2+$4)/2), int(($3+$5)/2); exit}'
}

enter_game(){  # 角色选择页 → 进入游戏 → 主世界/青崖书
  tap 1180 556; sleep 14
  i=0
  while [ $i -lt 3 ]; do
    i=$((i+1))
    tap 1143 100; sleep 0.8          # ★ 必须先关「新活动速递」，它自带遮罩会吞掉所有点击
    tap 1348 160; sleep 4.5
    snap; is_qy && { L "✅ 已进入游戏并开出青崖书"; return 0; }
    L "▸ 第 $i 次没开出青崖书，再试"
  done
  L "⚠️ 进了游戏但没开出青崖书 —— 检查一下"
}

MODE=$1; ARG=$2
case "$MODE" in role|account|who) ;; *) echo "用法: sh tools/rike-account.sh role | account <后4位> | who"; exit 2 ;; esac
snap
[ "$MODE" = "who" ] && { L "当前选中角色（模板匹配）：[$(role_of)]"; role_name >/dev/null; exit 0; }

# ① 若已在角色选择页
if is_rolepage; then
  if [ "$MODE" = "role" ]; then
    BEFORE=$(role_of)
    L "▸ 在角色选择页 → 换角色：当前[$BEFORE]，点列表第 2 行 (200,213)"
    tap 200 213; sleep 3; snap
    AFTER=$(role_of)
    L "   ▸ 角色：[$BEFORE] → [$AFTER]"
    if [ "$BEFORE" = "$AFTER" ]; then
      L "   ⚠️ 角色没变（点击被吞）→ 补点一次"; tap 200 213; sleep 3; snap
      AFTER=$(role_of); L "   ▸ 补点后：[$AFTER]"
    fi
    role_name >/dev/null
  else
    L "▸ 在角色选择页 → 换账号：点 ◇(59,36) 重新登录"
    tap 59 36; sleep 3; snap
    if has '确认|提示'; then L "▸ 弹「确认要重新登录吗？」→ 确认 (1278,315)"; tap 1278 315; sleep 7; snap; fi
  fi
fi

# ② 从"游戏内"出发：先回主世界
snap
if is_qy; then L "▸ 在青崖书 → 关掉 (57,33)"; tap 57 33; sleep 2.5; fi

# ③ 开菜单 → 设置
if [ "$MODE" = "account" ]; then
  # ★★ 2026-10-10 重构：**这条路上有几页 OCR 完全读不出字** ——
  #   实测"设置页上的账号面板"对 8 个候选词（其他账号登录/上次登录/登录/切换账号/
  #   切换角色/前往/用户中心/游戏设定）**全部 0 命中**。所以这一支不再靠"认页面"，
  #   改成三层**行为判据**：①先在展开的列表里找 → ②再试"点切换账号" → ③最后才走长路径。
  snap
  ROW=$(find_row "$ARG")
  [ -n "$ROW" ] && L "▸ 账号列表已展开，直接定位到 $ARG"

  if [ -z "$ROW" ]; then
    if is_title; then
      L "▸ 在标题页 → 点右上账号图标 (1350,28)"; tap 1350 28; sleep 5; snap
      L "▸ 展开账号列表 ▼(918,287)"; tap 918 287; sleep 3.5; snap
      ROW=$(find_row "$ARG")
    else
      # 🚨 2026-10-10 晚 修掉一个真 bug：原来这里（不在标题页时）会**盲点 (1085,162)** 试"切换账号"。
      #   但从青崖书退出来后停在**主世界**，那个坐标在主世界是「**充值**」入口 ——
      #   一点就进充值页，之后菜单/设置图标全点不到，整轮切账号失败
      #   （现场留图 /tmp/acct-fail.png 里就是充值页：充值/加赠/1元）。
      #   主世界没有"切换账号"入口 → **直接走下面的长路径**（菜单 → 设置），那条路才是稳的。
      L "▸ 不在标题页（多半在主世界）→ 不盲点，直接走长路径（菜单 → 设置）"
    fi
  fi

  if [ -z "$ROW" ]; then
    L "▸ 还没找到 → 长路径：开菜单 → 设置（模板定位设置图标）"
    SETOK=no
    for i in 1 2 3; do
      grab
      R=$(sh "$DSH_ROOT/tools/rike-find.sh" tmpl icon-设置 "$SHOT" '400x300+1150+400' 2>/dev/null)
      SC=$(printf '%s' "$R" | awk '{print $3}')
      if awk -v a="${SC:-9}" 'BEGIN{exit !(a<0.10)}'; then
        SX=$(printf '%s' "$R" | awk '{print $1}'); SY=$(printf '%s' "$R" | awk '{print $2}')
        L "▸ 模板定位到「设置」图标 ($SX,$SY) 分数=$SC → 点它"
        tap "$SX" "$SY"; sleep 3.5; snap
        if has '切换账号|切换角色'; then SETOK=yes; break; fi
        L "▸ 点了但没进设置页（可能被吞）"
      else
        L "▸ 没找到设置图标（分数 ${SC:-?}，菜单没开）→ 开一次菜单"
        tap 1350 32; sleep 3; snap
      fi
    done
    if [ "$SETOK" = yes ]; then
      L "▸ 点「切换账号 前往」(1265,252)"; tap 1265 252; sleep 5; snap
      L "▸ 点「切换账号」(1085,162) → 展开列表"; tap 1085 162; sleep 4; snap
      tap 918 287; sleep 3.5; snap
      ROW=$(find_row "$ARG")
    else
      L "❌ 3 次都没进设置页"; grab; cp "$SHOT" /tmp/acct-fail.png; L "   已留图 /tmp/acct-fail.png"; exit 1
    fi
  fi

  [ -n "$ROW" ] || { L "❌ 三轮都没找到账号 $ARG 的行"; grab; cp "$SHOT" /tmp/acct-fail.png; L "   已留图 /tmp/acct-fail.png"; exit 1; }
  L "▸ 账号 $ARG 在 ($ROW) → 点它"; tap $ROW; sleep 3.5; snap
  # ★ 真判据：点中账号行后**面板会折叠**（列表消失，只剩选中的那一行 + 「登录」按钮）。
  #   旧判据 `has "$ARG"` 是假阳性 —— 展开的列表里三个号码本来就都在，点空了也照样"通过"。
  NC=$(printf '%s' "$T" | grep -oE '[0-9]{3}\*\*\*\*[0-9]{4}' | wc -l)
  if [ "$NC" -le 1 ]; then
    L "✅ 面板已折叠（可见号码 $NC 个）→ $ARG 已选中"
  else
    L "⚠️ 面板仍展开（可见号码 $NC 个）→ 点击可能被吞，补点一次"
    tap $ROW; sleep 3.5; snap
    NC=$(printf '%s' "$T" | grep -oE '[0-9]{3}\*\*\*\*[0-9]{4}' | wc -l)
    [ "$NC" -le 1 ] && L "✅ 补点后面板折叠 → $ARG 已选中" || L "⚠️ 还是展开的 → 继续（后面靠角色名兜底）"
  fi

  L "▸ 点「登录」(700,395)"; tap 700 395; sleep 11; snap
  if is_title; then
    L "✅ 已回标题页"
  elif has '下一步|验证码|本机号码|密码|输入手机号'; then
    L "❌ 这个账号需要**人工登录**（弹出手机号/验证码页）—— agent 做不了，停手"
    grab; cp "$SHOT" /tmp/acct-needlogin.png; L "   已留图 /tmp/acct-needlogin.png"; exit 1
  else
    L "⚠️ 没回标题页，再等 6s"; sleep 6; snap
    if is_title; then L "✅ 已回标题页"; else L "⚠️ 仍未见标题页"; fi
  fi
  L "▸ 点「闯荡江湖」(710,510)"; tap 710 510; sleep 9; snap
fi

# ⑤ 到角色选择页 → 选角色 → 进入游戏
i=0
while [ $i -lt 4 ]; do
  i=$((i+1)); snap
  is_rolepage && break
  L "▸ 还没到角色选择页（第 $i 次）→ 试「进入游戏」或关公告"
  tap 757 566; sleep 5; snap
  is_rolepage && break
  L "▸ 试「闯荡江湖」(710,510)"; tap 710 510; sleep 8
done
snap
if is_rolepage; then
  L "▸ 在角色选择页 → 选列表第 1 行 (200,127)"
  tap 200 127; sleep 3; snap
  L "   ▸ 当前选中角色（模板匹配）：[$(role_of)]"
  role_name >/dev/null
  enter_game
else
  L "⚠️ 没到角色选择页 —— 当前页面特征词：$(printf '%s' "$T" | grep -oE '登录展示|缘定三生|日课点|青崖书' | head -3 | tr '\n' ' ')"
fi
