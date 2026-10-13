#!/bin/sh
# timer.sh —— 日课流程「步骤计时器」（副屏模式）
#   timer.sh start                开始新一轮计时
#   timer.sh mark "步骤名"         记录一步（打印该步耗时 + 累计）
#   timer.sh report               本轮汇总（按耗时降序，>=60s 标 ⚠️）
#   timer.sh save "标签"           把本轮存进历史（标签如：玉米鹿/醉梦辰/清月辞）
#   timer.sh history              历史累计（每步：平均/最大/次数，按平均降序）
. "$(dirname "$0")/../env.sh"
STATE=/tmp/rike-timer.state
LOG=/tmp/rike-timer.log
HIST="$DSH_ROOT/rike-timing.tsv"
SLOW=${SLOW_THRESHOLD:-60}

case "$1" in
  start)
    now=$(date +%s); : > "$LOG"
    printf 'START=%s\nLAST=%s\n' "$now" "$now" > "$STATE"
    echo "  ⏱  计时开始 $(date +%H:%M:%S)"
    ;;
  mark)
    name=$2; [ -z "$name" ] && { echo '用法: timer.sh mark "步骤名"'; exit 1; }
    now=$(date +%s)
    if [ -f "$STATE" ]; then . "$STATE"; else START=$now; LAST=$now; fi
    d=$((now - LAST)); tot=$((now - START))
    printf '%s\t%s\n' "$name" "$d" >> "$LOG"
    printf '  ⏱  %-26s %4ss   (累计 %ss)\n' "$name" "$d" "$tot"
    printf 'START=%s\nLAST=%s\n' "$START" "$now" > "$STATE"
    ;;
  report)
    [ -f "$LOG" ] || { echo "  还没有记录（先 start + mark）"; exit 0; }
    . "$STATE" 2>/dev/null
    now=$(date +%s); tot=$((now - START))
    echo "  ── 本轮各步耗时（降序）──"
    sort -t"$(printf '\t')" -k2 -nr "$LOG" | while IFS="$(printf '\t')" read -r n d; do
      flag=""; [ "$d" -ge "$SLOW" ] && flag="  ⚠️偏慢"
      printf '  %5ss  %s%s\n' "$d" "$n" "$flag"
    done
    printf '  ── 合计 %ss（%s分%s秒）──\n' "$tot" "$((tot/60))" "$((tot%60))"
    ;;
  save)
    tag=${2:-未命名}; [ -f "$LOG" ] || exit 0
    while IFS="$(printf '\t')" read -r n d; do
      printf '%s\t%s\t%s\t%s\n' "$(date +%F)" "$tag" "$n" "$d" >> "$HIST"
    done < "$LOG"
    echo "  💾 已存历史 → $HIST"
    ;;
  history)
    [ -f "$HIST" ] || { echo "  暂无历史"; exit 0; }
    echo "  ── 历史累计（按平均耗时降序）──"
    awk -F'\t' '{c[$3]++; s[$3]+=$4; if($4>m[$3])m[$3]=$4} END{for(k in c) printf "%d|%s|%d|%d\n", s[k]/c[k], k, m[k], c[k]}' "$HIST" \
      | sort -t'|' -nr \
      | while IFS='|' read -r avg name max cnt; do
          printf '  平均 %5ss   最大 %5ss   ×%s   %s\n' "$avg" "$max" "$cnt" "$name"
        done
    ;;
  *) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//' ;;
esac
