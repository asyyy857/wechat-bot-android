#!/bin/sh
# inbox.sh —— 「用 DSH 打开」收件箱助手（含待处理状态管理）
#
#   inbox.sh              列出收件箱（新 → 旧，标注待处理/已处理）
#   inbox.sh status       总结：总数 / 待处理(new) / 已处理(ack) + 最新待处理及来源
#   inbox.sh new          列出尚未标记为已处理的文件（全路径）
#   inbox.sh ack <file>   把某文件标记为"已处理"（收 basename，也接受全路径）
#   inbox.sh info <file>  打印某文件的来源元信息（.meta/<file>.meta.json）
#   inbox.sh latest       打印最新文件名（全路径）
#   inbox.sh cat          打印最新文件内容（文本文件用）
#   inbox.sh clear        清空收件箱（含元信息与已处理标记）
#
# 说明：收件箱里的文件都是经「用 DSH 打开」这个中转 App 从微信递进来的；
# .meta/<file>.meta.json 由该 App 落盘时写入，记录来源/大小/到达时间。
INBOX=${INBOX:-/sdcard/dsh/inbox}
ACK="$INBOX/.acked"
mkdir -p "$INBOX" "$INBOX/.meta"

# 列出收件箱中"真正的文件"（排除 .meta 目录、.acked、其它点文件），新到旧
file_list() { ls -1t "$INBOX" 2>/dev/null | grep -v '^\.'; }

# 某文件是否已被标记为已处理
is_acked() { grep -qxF "$1" "$ACK" 2>/dev/null; }

# 打印某文件的来源元信息
print_info() {
  f=$(basename "$1")
  m="$INBOX/.meta/$f.meta.json"
  if [ -f "$m" ]; then cat "$m"; else echo "（无元信息，说明收件早于元信息功能或走了别的路径）"; fi
}

case "${1:-list}" in
  list)
    echo "收件箱: $INBOX"
    n=0
    for f in $(file_list); do
      n=$((n+1))
      m="待处理"
      if is_acked "$f"; then m="已处理"; fi
      printf "%5d  %-6s  %s (%s 字节)\n" "$n" "$m" "$f" "$(stat -c%s "$INBOX/$f" 2>/dev/null)"
    done
    [ "$n" -eq 0 ] && echo "（空）"
    ;;
  status)
    total=0; pend=0; donec=0
    for f in $(file_list); do
      total=$((total+1))
      if is_acked "$f"; then donec=$((donec+1)); else pend=$((pend+1)); fi
    done
    echo "收件箱: $INBOX"
    echo "总数=$total  待处理(new)=$pend  已处理(ack)=$donec"
    if [ "$pend" -eq 0 ]; then
      echo "没有待处理的文件。"
    else
      for f in $(file_list); do
        if ! is_acked "$f"; then
          echo "最新待处理: $f"
          print_info "$f"
          break
        fi
      done
    fi
    ;;
  new)
    for f in $(file_list); do
      if ! is_acked "$f"; then echo "$INBOX/$f"; fi
    done
    ;;
  ack)
    [ $# -ge 2 ] || { echo "用法: inbox.sh ack <文件名>"; exit 2; }
    f=$(basename "$2")
    if [ ! -e "$INBOX/$f" ]; then echo "文件不存在: $f"; exit 1; fi
    if is_acked "$f"; then echo "已处理过: $f"; exit 0; fi
    printf '%s\n' "$f" >> "$ACK"
    echo "已标记为已处理: $f"
    ;;
  info)
    [ $# -ge 2 ] || { echo "用法: inbox.sh info <文件名>"; exit 2; }
    print_info "$2"
    ;;
  latest) file_list | head -1 | sed "s|^|$INBOX/|" ;;
  cat)
    f=$(file_list | head -1)
    [ -n "$f" ] || { echo "收件箱是空的"; exit 1; }
    echo "--- $INBOX/$f"
    cat "$INBOX/$f"
    ;;
  clear)
    rm -rf "$INBOX"/*
    echo "已清空 $INBOX"
    ;;
  *) echo "用法: inbox.sh [list|status|new|ack|info|latest|cat|clear]"; exit 2 ;;
esac
