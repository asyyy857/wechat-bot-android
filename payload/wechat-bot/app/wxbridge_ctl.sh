#!/bin/bash
# wxbridge 控制脚本：start / stop / restart / status / log
# 存在的意义：pkill -f wxbridge.py 会把执行它的 shell 自己一起杀掉（命令行含同样字符串），
# 这里改用「精确匹配 python3 wxbridge.py 结尾」+ 排除自身，才敢自动化。
DIR=$(cd "$(dirname "$0")" && pwd)
. "$DIR/paths.sh" "$DIR"
export TZ=Asia/Shanghai        # 容器是 UTC，锁成北京时间（同 bot.sh 的说明）
LIB=${WX_OCRLIB:-/root/ocrlibs}
PIDF=/tmp/wxbridge.pid
PORT=${WX_BRIDGE_PORT:-8765}
# ★ 2026-10-10 评审修复（§7.3）：进程判据统一到 botpids.sh —— 这里的
#   `pgrep -f "python3 wxbridge\.py$"` 与真身 `python3 -u wxbridge.py` 对不上，
#   **精确匹配零命中**，只剩 pidfile 兜底；pidfile 一丢就会在旧桥接仍占着 8765 时
#   再起一个（新进程 bind 失败，而 /health 问到的是旧进程，静默失败）。
source "$DIR/botpids.sh"

stop() {
  if [ -f "$PIDF" ]; then
    kill "$(cat "$PIDF")" 2>/dev/null
    rm -f "$PIDF"
  fi
  local pid
  for pid in $(_bridge_pids); do
    kill "$pid" 2>/dev/null
  done
  sleep 1
}

start() {
  stop
  cd "$DIR" || return 1
  # 从 env.sh 拿 VSD_PORT/SHD_PORT（18240/18242 被劫持，已换成 18250/18252）。
  # 必须显式传下去：tools/vscreen 是 node 子进程，只认环境变量。
  . "$DIR/../env.sh" 2>/dev/null || true
  echo "===== $(date '+%F %T') wxbridge restart =====" >> "$WXBOT_LOGDIR/wxbridge.log"
  # ⚠️ 这里的续行极易踩坑：echo/日志那一步**必须**放在赋值链之前，
  #    否则 PYTHONPATH 会被赋给 echo，python 反而在干净环境里起来 —— 那样
  #    rapidocr_onnxruntime 导入不到，OCR 静默返回空（2026-10-03 亲自踩过）。
  #
  # ★ 2026-10-06 用户拍板「让他主动抢一下吧，我不知道我在占用着……让他弹我一下，
  #   我确认一下，让他抢一下」——所以默认**开启抢焦点**（旧默认是 0 不抢）：
  #   · WXBRIDGE_TAKE_FOCUS=1  物理屏占着输入焦点时，机器人自己去抢一次；
  #   · WX_GRAB_GRACE=3        ★ 2026-10-12 用户要求 2 → 3（原话：「机器人看见消息后
#                             弹浮悬窗说 2s 之后抢屏，把这个 2s 改成 3s」）：
#                             抢之前**先弹通知 + 浮窗预告 + 震动**，3 秒后就抢
  #                            ★ 2026-10-09 用户定：2 秒 + 震动（原来是 1 秒，
  #                              他嫌来不及反应；再早的 6 秒是他嫌等太久改小的）
  #                            （2026-10-07 用户嫌 6 秒太久 → 2；
  #                             2026-10-09 用户「提前通知我，然后直接抢」→ 1）
  #                            （他多半只是没意识到占着焦点）→ 见 wxbridge.py；
  #   · WX_USER_HANG_SECONDS=2 动手前等"用户手停"的上限：30 → 8 → **2**
#                            （2026-10-07 用户报「切屏后等了近 20 秒」：
#                             浮窗预告 + 2 秒宽限已经等过一轮了，这里是重复等待）
  #   · WX_IMAGE_HANG_SECONDS=3 ★ 2026-10-13 新增：**发图**专用的让位窗口。
  #                            发图原来一路静默硬闯（不弹预告、等满就抢），用户报
  #                            「发图片的时候机器人抢我键盘，怎么没有弹窗」。现在发图前
  #                            先弹浮窗预告 + 震动，给你 3 秒收手（2026-10-13 用户定：
  #                            8 → **3**）；到点你还在打字就
  #                            **这张图不发**（retryable，下一轮自动补）。想让发图更急/更缓
  #                            改这个值即可（发文字那条路仍用 WX_USER_HANG_SECONDS=2）。
  #   · WX_PHYS_WAIT_SECONDS=8  抢不了时"等你让位"的上限：60 → 15 → 30 → **8**
  #                            （2026-10-09 用户拍板「全改」：用户主动让位是概率事件，
  #                             吊 30 秒只是白等 —— 发不出去的这条下一轮 20 秒后还会
  #                             重试，缩短窗口几乎察觉不到，但发送流程不再被吊死）
  #   · WX_SEARCH_STUCK_SECS=45 ★ 2026-10-09（续13，用户问「微信卡在搜索页能自动
  #                             恢复吗」）：虚拟屏微信**停在搜索页**超过这么久，
  #                             看护就点一下返回箭头把它退出来（正常 /open 在搜索页
  #                             只待 10~20 秒）。这条**故意不看机器人在不在跑** ——
  #                             要救的正是"机器人启动切群失败、已经退出"那种（今天
  #                             12:14 的故障）。设 0 = 关掉。
  #   全部写成 ${VAR:-默认值}，想临时关掉抢：WXBRIDGE_TAKE_FOCUS=0 bash wxbridge_ctl.sh restart
  # ★ 2026-10-08 审查修复（M16）：桥接日志同样只增不减，启动前留最近 2000 行
  _wblog="$WXBOT_LOGDIR/wxbridge.log"
  if [ -f "$_wblog" ] && [ "$(wc -l < "$_wblog" 2>/dev/null || echo 0)" -gt 3000 ]; then
    tail -n 2000 "$_wblog" > "$_wblog.tmp" 2>/dev/null && mv "$_wblog.tmp" "$_wblog"
  fi
  # ★ 2026-10-10 晚用户最终口径：「**不管啥情况，2s 之后直接抢**」——
  #   wxbridge.py::preflight_yield 里原来叠了五道"不抢"分支（打字耐心 / 物理屏用微信 /
  #   限流闸门 / 没授权 / adb 断），每道弹一条不同的提示，他在真机上同时看到三条
  #   互相矛盾的提醒。已全部删除，收敛成「预告 → 等 GRAB_GRACE 秒 → 直接抢」。
  #   下面这两个变量**已废弃、不再参与任何判断**，留着只为万一要回退：
  #     WX_TYPING_PATIENCE（原 60 秒打字耐心）、WX_GRAB_MIN_GAP（原 20 秒限流）。
  #   「一条消息最多抢一次」由 wxbridge.py::send_text 的 _grab_tried 保证。
  #   ⚠️ 注释别插到下面那条续行命令中间 —— shell 会在注释处断行、把后续行当新命令。
  PYTHONPATH="$LIB" OMP_NUM_THREADS=2 \
    VSD_PORT="${VSD_PORT:-18250}" SHD_PORT="${SHD_PORT:-18252}" \
    WXBRIDGE_TAKE_FOCUS="${WXBRIDGE_TAKE_FOCUS:-1}" \
    WX_GRAB_GRACE="${WX_GRAB_GRACE:-3}" \
    WX_USER_HANG_SECONDS="${WX_USER_HANG_SECONDS:-2}" \
    WX_IMAGE_HANG_SECONDS="${WX_IMAGE_HANG_SECONDS:-3}" \
    WXBRIDGE_TIMING="${WXBRIDGE_TIMING:-1}" \
    WX_PHYS_WAIT_SECONDS="${WX_PHYS_WAIT_SECONDS:-8}" \
    WX_SEARCH_STUCK_SECS="${WX_SEARCH_STUCK_SECS:-45}" \
    nohup python3 -u wxbridge.py >> "$WXBOT_LOGDIR/wxbridge.log" 2>&1 9>&- &
  echo $! > "$PIDF"
  # ★★ 2026-10-10 评审修复（§7.3）：原来这两行是 `curl ...; echo` —— `echo` 恒成功，
  #   于是 start()（连带 restart）**永远返回 0**，bot.sh 里那句 `|| exit 1`
  #   是死代码：桥接根本没起来也照样报"启动成功"，然后机器人对着一个死桥接跑。
  #   现在以 /health 是否回 `"ok": true` 作为成功判据，失败返回 1。
  # ★★ 2026-10-10 晚**再修一次**（当天踩的第二个坑）：原来 `sleep 3` 之后**只查一次**
  #   —— 而 OCR 引擎（PP-OCRv5 server，84MB）加载通常要 5~10 秒，桥接才开始监听 8765。
  #   于是 `bot.sh:102` 的 `|| exit 1` 把"其实好好的桥接"判成失败，**机器人根本起不来**
  #   （当晚 22:07 就是这样：桥接 health 明明 ok，机器人却停在"没在跑"）。
  #   现在最多等 40 秒、每 2 秒重查一次，只有真起不来才返回 1。
  local _health="" _i=0
  while [ "$_i" -lt 20 ]; do
    _health=$(curl -s -m 4 "http://127.0.0.1:$PORT/health" 2>/dev/null)
    case "$_health" in
      *'"ok": true'*|*'"ok":true'*) echo "$_health"; return 0 ;;
    esac
    _i=$((_i + 1))
    sleep 2
  done
  echo "$_health"
  echo "   ⚠️ 等 40 秒桥接 /health 仍没回 ok —— 起没起来存疑（看 "$WXBOT_LOGDIR/wxbridge.log"）" >&2
  return 1
}

case "${1:-status}" in
  start)   source "$DIR/wxlock.sh" "wxbridge_ctl.sh start" || exit 1; start || exit 1 ;;
  stop)    stop; echo "stopped" ;;
  restart) source "$DIR/wxlock.sh" "wxbridge_ctl.sh restart" || exit 1; start || exit 1 ;;
  status)  curl -s -m 6 "http://127.0.0.1:$PORT/health"; echo ;;
  log)     tail -n "${2:-30}" "$WXBOT_LOGDIR/wxbridge.log" ;;
  *)       echo "用法: $0 {start|stop|restart|status|log [n]}" ;;
esac
