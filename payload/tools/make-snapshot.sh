#!/bin/sh
# make-snapshot.sh —— 给「虚拟屏设置 + 微信机器人 + 逆水寒日课脚本」打一份完整存档
#
#   用法: sh /sdcard/dsh/tools/make-snapshot.sh [存档目录]
#   产出: /sdcard/dsh/backups/fullsnap-<YYYYMMDD-HHMM>/
#           dsh-root/          与 /sdcard/dsh 同构的子树（cp -a 覆盖回去即可）
#           skills/            两个技能的 SKILL.md（/root/.dsh/skills 是容器内路径，会丢）
#           存档时的运行状态.txt  虚拟屏 displayId/分辨率、机器人 PID、桥接信息
#           README-存档说明.md
#           MANIFEST.txt       全部文件清单（tar -tv 输出）
#           MD5SUMS.txt        模型/jar 等大文件的校验值
#           restore-存档.sh    从本存档还原（默认演练，--apply 才真写）
#         以及 backups/fullsnap-<时间戳>.tar.gz  同样内容的单文件包
#
#   ⚠️ 只读原文件，不修改任何现有内容。
#   ⚠️ 存档是**快照**：微信机器人可能正在写日志/状态，目录副本与压缩包之间
#      允许有极细微差别。要「绝对一致」就先停机器人再跑本脚本。
set -e
D=${DSH_ROOT:-/sdcard/dsh}
TS=$(date +%Y%m%d-%H%M)
OUT=${1:-$D/backups/fullsnap-$TS}
[ -e "$OUT" ] && { echo "✗ 目标已存在：$OUT（换一个名字）"; exit 1; }
mkdir -p "$OUT/dsh-root" "$OUT/skills"

echo "=== [1/4] 复制三块内容到 $OUT/dsh-root ==="
cd "$D"
for x in wechat-bot tools vscreen-daemon shell-daemon vscreen-src hud \
         env.sh 恢复.sh bootstrap.sh connect-adb.sh ensure-vsd.sh \
         restore-agents.sh refresh-agents-local.sh README-重建说明.md \
         AGENTS.md AGENTS.local.md; do
  if [ -e "$D/$x" ]; then
    cp -a "$x" "$OUT/dsh-root/"
    printf '  ✓ %s\n' "$x"
  else
    printf '  · 跳过（不存在）%s\n' "$x"
  fi
done

echo "=== [2/4] 复制技能文档 ==="
for s in nishuihan-rike wechat-bot; do
  if [ -d "/root/.dsh/skills/$s" ]; then
    cp -a "/root/.dsh/skills/$s" "$OUT/skills/"
    printf '  ✓ skills/%s (%s)\n' "$s" "$(du -sh "$OUT/skills/$s" | cut -f1)"
  else
    printf '  · 找不到 /root/.dsh/skills/%s\n' "$s"
  fi
done

echo "=== [3/4] 采集当前运行状态 ==="
{
  echo "# 存档时的运行状态"
  echo
  echo "存档时间：$(date '+%Y-%m-%d %H:%M:%S %Z')"
  echo "容器：$(uname -srm)"
  echo
  echo "## 虚拟屏（在跑的 displayId 与分辨率 —— 每次重起都会变，恢复后别照抄）"
  node "$D/tools/vscreen" list 2>&1 || echo "（vscreen list 取不到：守护没在跑？）"
  echo
  echo "## 磁盘"
  df -h /sdcard 2>&1 | tail -2
} > "$OUT/存档时的运行状态.txt" 2>&1
{
  echo
  echo "## 微信机器人"
  bash "$D/wechat-bot/app/bot.sh" status 2>&1 || echo "（bot.sh status 取不到）"
  echo
  echo "## 虚拟屏守护源码里的规格 SPEC（改它要重编译）"
  grep -n 'static final int\[\]\[\] SPEC' "$D/vscreen-daemon/src/dsh/VsDaemon.java" 2>&1 || true
  echo "## 端口"
  grep -n 'export VSD_PORT\|export SHD_PORT' "$D/env.sh" 2>&1 || true
} >> "$OUT/存档时的运行状态.txt" 2>&1
echo "  已写 存档时的运行状态.txt"

echo "=== [4/4] 清单 / 校验 / 说明 / 打包 ==="
tar -cf - --exclude=./MANIFEST.txt --exclude=./MD5SUMS.txt -C "$OUT" . 2>/dev/null \
  | tar -tvf - > /tmp/manifest.$$ 2>/dev/null || true
mv /tmp/manifest.$$ "$OUT/MANIFEST.txt"
echo "  文件条目：$(wc -l < "$OUT/MANIFEST.txt")"

{
  echo "# 关键大文件 MD5（校验用；存档没坏就对得上）"
  md5sum "$OUT/dsh-root/wechat-bot/app/models/"*.onnx 2>/dev/null || true
  md5sum "$OUT/dsh-root/vscreen-src/"*.jar 2>/dev/null || true
  md5sum "$OUT/dsh-root/tools/rike.sh" "$OUT/dsh-root/tools/rike-enter.sh" \
         "$OUT/dsh-root/wechat-bot/app/wxbridge.py" "$OUT/dsh-root/wechat-bot/app/botctl.py" \
         "$OUT/dsh-root/wechat-bot/data/config.yaml" "$OUT/dsh-root/wechat-bot/data/rules.yaml" 2>/dev/null || true
} > "$OUT/MD5SUMS.txt"

cat > "$OUT/restore-存档.sh" <<'EOS'
#!/bin/sh
# 从本存档还原到 /sdcard/dsh —— 默认只演练，加 --apply 才真写
#   改动前的现有文件会先挪到 backups/pre-restore-<时间戳>/（可回退）
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
D=${DSH_ROOT:-/sdcard/dsh}
if [ "$1" != "--apply" ]; then
  echo "[演练] 下面这些项会被本存档覆盖（只列出，不动手）："
  ls -1 "$HERE/dsh-root"
  echo
  echo "确认无误后执行： sh $HERE/restore-存档.sh --apply"
  exit 0
fi
BACK="$D/backups/pre-restore-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACK"
for x in "$HERE"/dsh-root/*; do
  n=$(basename "$x")
  if [ -e "$D/$n" ]; then mv "$D/$n" "$BACK/$n"; echo "  旧 $n → $BACK/"; fi
done
cp -a "$HERE"/dsh-root/. "$D/"
echo "✅ 已还原到 $D"
echo "   技能文件请手动拷回容器内： mkdir -p /root/.dsh/skills && cp -a $HERE/skills/. /root/.dsh/skills/"
echo "   然后重起： sh $D/恢复.sh"
EOS
chmod 755 "$OUT/restore-存档.sh"

cat > "$OUT/README-存档说明.md" <<EOF
# 存档：虚拟屏设置 + 微信机器人 + 逆水寒日课脚本

- **存档时间**：$(date '+%Y-%m-%d %H:%M:%S %Z')
- **存档目录**：\`$OUT\`
- **单文件包**：\`$OUT.tar.gz\`
- **口径**：完整备份（连 OCR 模型、表情包、美图缓存、历史备份一起存）

## 一、这个存档里有什么

| 目录 | 内容 |
|---|---|
| \`dsh-root/vscreen-daemon/\` | 受信任虚拟屏守护（源码 \`src/dsh/VsDaemon.java\` + 编译好的 \`bin/classes.dex\` + start/stop）★ **两块屏的分辨率/密度/帧率就写在这里的 \`SPEC\`** |
| \`dsh-root/shell-daemon/\` | 命令端点守护（:18252，输入注入/取帧走它） |
| \`dsh-root/vscreen-src/\` | 编译用的 \`android.jar\` / \`d8.jar\`（38M，bootstrap 会从这里还原到容器内） |
| \`dsh-root/tools/\` | 全部工具脚本：\`vscreen\`（虚拟屏客户端）、\`shd.sh\`、**逆水寒日课整套 \`rike*.sh / rike-*.cjs / rike-tmpl / rike-fixtures\`**、OCR（\`ocr*.sh/py\`、\`ocr-daemon\`）、\`fix-vscreen-ports.sh\`（端口补丁）等 |
| \`dsh-root/wechat-auto/\` | 微信机器人整套：\`wxbridge.py\`、\`wxauto/\`、\`botctl.py\`、\`bot.sh\`、\`config.yaml\`（人设/参数）、\`rules.yaml\`、\`personas/\`、\`models/\`（OCR 模型 97M）、\`表情包/\`、\`美图缓存/\`、\`历史备份/\`、\`SKILL.md\`、\`README.md\`、\`ARCHITECTURE.md\`、\`recover.sh\`、\`battery_guard.sh\` 等 |
| \`dsh-root/hud/\` | 浮窗（附带存的，不属于三大块，但恢复环境时用得到） |
| \`dsh-root/*.sh\` | 公共入口：\`bootstrap.sh\`（容器重建后复原）、\`恢复.sh\`（重启后一把梭）、\`connect-adb.sh\`、\`ensure-vsd.sh\`、\`restore-agents.sh\`、\`env.sh\`（端口/路径）、两份 \`AGENTS*.md\` |
| \`skills/\` | 技能说明书：\`nishuihan-rike/SKILL.md\`（逆水寒日课，含全部实测坐标与坑）、\`wechat-bot/SKILL.md\` |
| \`存档时的运行状态.txt\` | 存档当下的虚拟屏 displayId/分辨率、机器人 PID、桥接自检、SPEC 与端口原始行 |
| \`MANIFEST.txt\` | 全部文件条目（含大小） |
| \`MD5SUMS.txt\` | 模型/jar/主脚本的校验值 |

## 二、没存什么

- 容器根文件系统里的东西（\`/usr/local/bin\` 入口、apt 装的 adb/java/tesseract、时区修正）——这些由 \`bootstrap.sh\` + \`恢复.sh\` 负责重建；
- \`/sdcard/dsh/backups/\`（存档自身）、\`tmp/\`、\`logs/\`、\`inbox/\`、\`debug/\` 等运行垃圾；
- DSH 本体与 profile 配置（\`/root/.dsh/profiles/\`）。

## 三、怎么恢复

\`\`\`sh
# 1) 先演练（只列会覆盖哪些项，不动手）
sh "$OUT/restore-存档.sh"

# 2) 确认后真还原（现有同名项会先挪到 backups/pre-restore-<时间戳>/）
sh "$OUT/restore-存档.sh" --apply

# 3) 技能文件拷回容器内
mkdir -p /root/.dsh/skills && cp -a "$OUT/skills/." /root/.dsh/skills/

# 4) 重起守护 + 机器人 + adb
sh /sdcard/dsh/恢复.sh
\`\`\`

⚠️ **恢复后 displayId 会变**（守护重起就重排），任何地方都别写死屏号 ——
\`存档时的运行状态.txt\` 里的 displayId 只是"当时的样子"。

## 四、存档验证过什么

- 目录树与 \`tar.gz\` 都在；
- 关键大文件 MD5 记在 \`MD5SUMS.txt\`（解包后可比对）；
- 用 \`tar -tzf\` 能完整列出包内条目（见 MANIFEST.txt）。
EOF

echo "  打包 tar.gz（约 1-2 分钟，模型文件难压缩）…"
tar -cf - --exclude=./MANIFEST.txt --exclude=./MD5SUMS.txt --exclude="$(basename "$OUT").tar.gz" \
    -C "$(dirname "$OUT")" "$(basename "$OUT")" 2>/dev/null | gzip -1 > "$OUT.tar.gz"

echo
echo "=== 完成 ==="
echo "目录：$OUT   $(du -sh "$OUT" | cut -f1)"
echo "单包：$OUT.tar.gz   $(du -sh "$OUT.tar.gz" | cut -f1)"
