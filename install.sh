#!/bin/sh
# ============================================================================
#  微信机器人 v2.0.0 · 安装 / 升级程序
#
#  用法（⚠️ /sdcard 是 noexec，必须用解释器调用，不能 ./install.sh）：
#      sh install.sh                 # 智能安装：新装铺全量；已装则只换程序
#      sh install.sh --dry-run       # 只打印要做什么，一个字节都不改
#      sh install.sh --no-ocr        # 跳过解包 OCR 运行时（已有 /root/ocrlibs）
#      sh install.sh --no-apt        # 跳过容器依赖安装（离线 / 已装过）
#      sh install.sh --force         # 不备份，直接覆盖（不推荐）
#      sh install.sh --migrate       # 把旧版 wechat-auto 里的数据搬到新目录
#
#  安装位置（固定，机器人代码与外部守护脚本都按这个路径找）：
#      /sdcard/dsh/wechat-bot/app     程序  ← 升级时被覆盖
#      /sdcard/dsh/wechat-bot/data    你的数据 ← 升级时**一个字节都不动**
#      /sdcard/dsh/wechat-bot/logs    日志
#
#  数据目录布局与升级策略见《快速开始.md》；回滚步骤见《排障手册.md》。
# ============================================================================
set -e

HERE=$(cd "$(dirname "$0")" && pwd)
PAYLOAD="$HERE/payload"
DEST=/sdcard/dsh
BOT="$DEST/wechat-bot"
APP="$BOT/app"
DATA="$BOT/data"
LOGS="$BOT/logs"
OLD="$DEST/wechat-auto"
SCHEMA=2

DRY=0; FORCE=0; NO_OCR=0; NO_APT=0; MIGRATE=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    --force)   FORCE=1 ;;
    --no-ocr)  NO_OCR=1 ;;
    --no-apt)  NO_APT=1 ;;
    --migrate) MIGRATE=1 ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "未知参数：$a（--dry-run --force --no-ocr --no-apt --migrate）"; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
hr()  { say "------------------------------------------------------------"; }
run() { if [ "$DRY" = "1" ]; then say "   [dry-run] $*"; else "$@"; fi; }

hr; say "微信机器人 v2.0.0  安装程序"; hr

# ---------------------------------------------------------------- 0) 环境自检
[ -d "$PAYLOAD" ] || { say "❌ 找不到 $PAYLOAD —— 请先完整解压压缩包再运行本脚本"; exit 1; }
[ -d "$DEST" ]    || { say "❌ 找不到 $DEST —— 这个包要在「DSH 容器」里安装（手机上装了 DSH App 才有这个目录）"; exit 1; }
if [ ! -w "$DEST" ]; then
  say "❌ $DEST 不可写 —— 请确认 DSH App 正在运行、且给了存储权限"; exit 1
fi
for c in sh curl unzip node; do
  command -v "$c" >/dev/null 2>&1 || say "⚠️  缺少命令：$c（后面的步骤可能会失败）"
done
say "✅ 环境自检通过（安装到 $BOT）"

# ---------------------------------------------------------------- 1) 现状判断
MODE="新装"
if [ -d "$APP" ]; then MODE="升级"; fi
if [ -d "$OLD" ] && [ ! -d "$DATA" ]; then
  if [ "$MIGRATE" = "1" ] || [ "$MODE" = "新装" ]; then MODE="新装+迁移旧数据"; fi
fi
say "   模式：$MODE"
if [ -d "$DATA" ]; then
  say "   已存在数据目录（升级不会动它）：$DATA"
  if [ -f "$DATA/.schema_version" ]; then
    say "   数据格式版本：$(cat "$DATA/.schema_version")（本程序需要 $SCHEMA）"
  else
    say "   数据格式版本：无标记（按 $SCHEMA 处理）"
  fi
fi
[ "$DRY" = "1" ] && say "   （--dry-run：只打印，不修改任何文件）"

# ---------------------------------------------------------------- 2) 备份
if [ "$FORCE" = "1" ]; then
  say "⚠️  --force：直接覆盖，不备份"
elif [ -d "$BOT" ]; then
  BK="$DEST/backups/wechat-bot-安装前-$(date +%Y%m%d-%H%M%S)"
  say "备份现有安装到：$BK"
  run mkdir -p "$BK"
  # 只备份程序与配置（数据本来就保留在原地，不用进备份）
  run cp -a "$APP" "$BK/app"
  if [ "$DRY" != "1" ] && [ -d "$BK/app/models" ]; then
    rm -rf "$BK/app/models"      # 模型可从包里重铺，备份里省掉 130MB
    say "   （备份里的 models/ 已省去 130MB —— 需要时从包里重铺）"
  fi
  say "✅ 备份完成"
fi

# ---------------------------------------------------------------- 3) 铺程序（app/ 整体替换）
hr; say "[1/5] 铺程序到 $APP ..."
run mkdir -p "$APP" "$LOGS"
# ★ 用 rsync 式语义：先铺再删多于文件，保证 app/ 与包内一致（但保留 models 以外不动）
if command -v rsync >/dev/null 2>&1; then
  run rsync -a --delete "$PAYLOAD/wechat-bot/app/" "$APP/"
else
  # 没有 rsync：整个目录换掉（models 会被重新拷一次，稍慢但结果一致）
  run rm -rf "$APP.new"
  run cp -a "$PAYLOAD/wechat-bot/app" "$APP.new"
  run rm -rf "$APP"
  run mv "$APP.new" "$APP"
fi
say "✅ 程序就位"

# ---------------------------------------------------------------- 4) 数据目录
hr; say "[2/5] 数据目录 ..."
if [ -d "$DATA" ]; then
  say "   · 已存在 → 原样保留（升级不覆盖你的配置/记忆/表情包）"
  # 新版本新增的配置项：只提示，不自动改（见《配置手册》的「升级后要补什么」）
  if [ -f "$PAYLOAD/wechat-bot/app/defaults/config.yaml" ] && [ -f "$DATA/config.yaml" ]; then
    _new=$(grep -E '^[a-z_]+:' "$PAYLOAD/wechat-bot/app/defaults/config.yaml" \
           | sed 's/:.*//' | sort -u)
    _old=$(grep -E '^[a-z_]+:' "$DATA/config.yaml" | sed 's/:.*//' | sort -u)
    _miss=$(printf '%s\n' $_new | while read -r k; do
              printf '%s\n' $_old | grep -qx "$k" || echo "$k"; done | tr '\n' ' ')
    if [ -n "$_miss" ]; then
      say "   ⚠️ 新版多了这些配置段，你的 config.yaml 里还没有：$_miss"
      say "      不补也能跑（程序有默认值）；想用新功能可对照"
      say "      $PAYLOAD/wechat-bot/app/defaults/config.yaml 手工加一段。"
    fi
  fi
else
  say "   · 不存在 → 从包内模板初始化"
  run mkdir -p "$DATA"
  run cp -a "$PAYLOAD/wechat-bot/app/defaults/." "$DATA/"
  say "✅ 已初始化 data/（配置是模板，必改项见《快速开始.md》）"
fi

# 旧版数据迁移
if [ -d "$OLD" ] && { [ "$MIGRATE" = "1" ] || [ "$MODE" = "新装+迁移旧数据" ]; }; then
  say "   · 发现旧版目录 $OLD → 迁移数据（**复制**，旧目录原样留着）"
  if [ "$DRY" = "1" ]; then
    say "   [dry-run] 会从 $OLD 复制 config/rules/personas/表情包/记忆库/聊天流水等到 $DATA"
  else
    mkdir -p "$DATA"
    for f in config.yaml rules.yaml persona.md tuning.json .deepseek_key \
             chat_log.json sent.jsonl rejects.jsonl drafts.lock governor_sent.json \
             ppt_seen.json agent_used.json nsh_learn_state.json \
             记忆库.json 记忆_auto.json 名字库.json 群成员前缀.json 群情摘要.json \
             瓜词表.json 瓜词黑名单.txt 群瓜备忘.md 人设建议.md 演练反馈.md 兴趣点清单.md; do
      [ -e "$OLD/$f" ] && cp -a "$OLD/$f" "$DATA/" || true
    done
    for d in personas 表情包 美图库 美图缓存; do
      [ -d "$OLD/$d" ] && [ ! -e "$DATA/$d" ] && cp -a "$OLD/$d" "$DATA/" || true
    done
    for f in 逆水寒常识.md 逆水寒自学区.md 逆水寒术语.json 逆水寒待核实.md; do
      [ -e "$OLD/$f" ] && cp -a "$OLD/$f" "$DATA/" || true
    done
    say "✅ 旧数据已复制过来（$OLD 没动，确认没问题后可以自己删）"
  fi
fi
run sh -c "printf '%s\n' $SCHEMA > '$DATA/.schema_version'" 2>/dev/null || true

# ---------------------------------------------------------------- 5) 依赖
hr; say "[3/5] 容器依赖（adb / java / 时区）..."
if [ "$NO_APT" = "1" ]; then
  say "   · 按参数跳过（--no-apt）"
elif command -v adb >/dev/null 2>&1 && command -v java >/dev/null 2>&1; then
  say "   · adb / java 都在，跳过"
else
  say "   · 缺依赖，开始安装（要联网，几分钟）..."
  run sh "$PAYLOAD/bootstrap.sh" || say "   ⚠️ bootstrap 没跑完 —— 联网后可重跑：sh $PAYLOAD/bootstrap.sh"
fi
# 容器时区（机器人按北京时间判断时段，错 8 小时会全乱）
if [ "$(readlink /etc/localtime 2>/dev/null)" != "/usr/share/zoneinfo/Asia/Shanghai" ]; then
  run ln -sfn /usr/share/zoneinfo/Asia/Shanghai /etc/localtime || true
  say "   · 已把容器时区拽到 Asia/Shanghai"
fi

hr; say "[4/5] Python 运行时（OCR 认字 + PDF 解析）..."
if [ "$NO_OCR" = "1" ]; then
  say "   · 按参数跳过 OCR（--no-ocr）"
elif [ -d /root/ocrlibs/rapidocr_onnxruntime ] && [ -d /root/ocrlibs/numpy ]; then
  say "   · /root/ocrlibs 已就绪，跳过"
else
  say "   · 从 $PAYLOAD/ocrlibs.tar.gz 解包到 /root/ocrlibs（约 300MB，请稍等）..."
  run sh "$APP/setup_ocr.sh" || say "   ⚠️ OCR 解包没成功 —— 可重跑：sh $APP/setup_ocr.sh"
fi
if [ -d "$PAYLOAD/pylibs/pypdf" ]; then
  run mkdir -p /root/pylibs
  run cp -a "$PAYLOAD/pylibs/." /root/pylibs/
  say "   · pypdf 已就位（读群里 PDF 用）"
fi

# ---------------------------------------------------------------- 6) 环境件
hr; say "[5/5] 环境件（虚拟屏守护 / 命令守护 / 输入法 / 工具链）..."
for item in vscreen-daemon shell-daemon clip-app vscreen-src tools; do
  if [ -d "$PAYLOAD/$item" ]; then
    run cp -a "$PAYLOAD/$item" "$DEST/"
    say "   → $item"
  fi
done
for f in env.sh bootstrap.sh connect-adb.sh ensure-vsd.sh 恢复.sh; do
  [ -f "$PAYLOAD/$f" ] && run cp -a "$PAYLOAD/$f" "$DEST/$f"
done
run chmod +x "$DEST/connect-adb.sh" "$DEST/bootstrap.sh" "$DEST/ensure-vsd.sh" 2>/dev/null || true
# 鉴权 token（命令守护用）
if [ -s "$DEST/.shd-token" ]; then
  say "   · .shd-token 已存在，保留"
else
  if command -v openssl >/dev/null 2>&1; then
    run sh -c "openssl rand -hex 16 > '$DEST/.shd-token'"
  else
    run sh -c "head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n' > '$DEST/.shd-token'"
  fi
  say "   · 已生成 .shd-token"
fi

# ---------------------------------------------------------------- 完
hr
if [ "$DRY" = "1" ]; then
  say "✅ 干跑结束（什么都没改）"; hr; exit 0
fi
say "✅ 安装完成"; hr; say ""
say "接下来 3 步（详见《快速开始.md》）："; say ""
say "  ① 连 adb（手机先开「无线调试」）："
say "       sh $DEST/connect-adb.sh"
say "     要配对就先在手机上：设置 → 开发者选项 → 无线调试 → 使用配对码配对设备，"
say "     然后：sh $DEST/connect-adb.sh pair <端口> <6位码>"; say ""
say "  ② 起守护 + 装输入法："
say "       sh $DEST/vscreen-daemon/start.sh"
say "       sh $DEST/shell-daemon/start.sh"
say "       sh $DEST/clip-app/install.sh          # 装完去 设置→输入法 启用「DSH 输入」"; say ""
say "  ③ 填配置（必改：群名 / 你的昵称 / API Key）："
say "       $DATA/config.yaml"
say "     然后先干跑观察："
say "       cd $APP && bash bot.sh start --dry-run"; say ""
say "  以后手机/DSH 重启过，跑这一个就够：  sh $DEST/恢复.sh"
say ""
say "  升级（保留数据）：把新包解开，再跑一次  sh install.sh"
say "  回滚：停机器人 → mv $BOT $BOT.failed → 从 $DEST/backups/ 里拷回 → 起机器人"
say "        （详见《排障手册.md》「升级 / 回滚」一节）"
