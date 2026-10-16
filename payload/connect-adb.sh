#!/bin/sh
# connect-adb.sh —— 连接本机「无线调试」
#
#   用法:
#     sh connect-adb.sh                      自动找端口并连接（已配对过）
#     sh connect-adb.sh <端口>               直连指定端口（会在所有候选 IP 上试）
#     sh connect-adb.sh <IP:端口>            直连指定地址
#     sh connect-adb.sh pair <配对端口> <6位配对码>      首次 / 重新配对
#     sh connect-adb.sh pair <IP:配对端口> <6位配对码>
#
#   配对端口与配对码在手机上：设置 → 开发者选项 → 无线调试 → 使用配对码配对设备
. "$(dirname "$0")/env.sh"
export HOME=/root

# ================================================================ ★2026-10-12 抖动修复（P1）
# ① **全局串行化**：看护 / 屏幕看护 / 机器人自动修 / 面板按钮会同时调这个脚本。并发时
#    一个进程刚 connect 的端口会被另一个判"不通"再扫再断 —— 抖动自我维持（10-12 19:10
#    日志里两条同秒重连 + 逐行交错就是现场）。自动调用拿不到锁直接跳过；手动调用最多等。
LOCK=/tmp/adb-connect.lock
exec 9>"$LOCK"
if [ "${ADB_CONNECT_AUTO:-0}" = "1" ]; then
  flock -n 9 || { echo "  ⏭ 另一个重连正在进行（flock）→ 本次跳过"; exit 0; }
else
  flock -w 180 9 || { echo "  ❌ 等锁超时（另一个重连卡住了？）"; exit 1; }
fi

# ② **最小重连间隔**：只在自动调用时生效（手动/面板按钮不受限，保证"点了就有反应"）。
if [ "${ADB_CONNECT_AUTO:-0}" = "1" ] && [ -z "$1" ]; then
  _now=$(date +%s)
  _last=$(cat /tmp/adb-last-connect 2>/dev/null | tr -dc 0-9)
  [ -n "$_last" ] || _last=0
  _cd=${ADB_CONNECT_COOLDOWN:-60}
  if [ $(( _now - _last )) -lt "$_cd" ]; then
    echo "  ⏭ 距上次重连仅 $(( _now - _last ))s（<${_cd}s 冷却）→ 本次跳过"
    exit 0
  fi
fi
date +%s > /tmp/adb-last-connect     # 先占位：从现在起就算"刚试过"，防调用者挤在一起

adb start-server >/dev/null 2>&1

# ================================================================ ★2026-10-14 候选主机（WLAN 兜底）
# 病根（2026-10-14 实测）：手机重启后无线调试要**重新配对**，配对之后 adbd
# **只在 WLAN IP（实测 192.168.1.3）上监听**，不再听 127.0.0.1。
# 而本脚本以前处处硬编码 127.0.0.1 —— 扫描、试连、配对全部打空，开机自愈必然失败：
# adb 连不上 → 两个守护（:18250 / :18252 都必须 adb push + app_process 拉起）全灭
# → 机器人进程还活着，但它「瞎」了（连报「读不到画面」，群里不理）。
# 现在：候选主机 = 上次成功的 IP → 127.0.0.1 → hostname -I 的其它 IPv4（192.168.* 优先）。
_last_host=$(sed -n 's/^\(.*\):[0-9]\{1,5\}$/\1/p' "$DSH_ROOT/.adb-serial" 2>/dev/null | head -1)
_last_port=$(sed -n 's/.*:\([0-9]\{1,5\}\)$/\1/p' "$DSH_ROOT/.adb-serial" 2>/dev/null | head -1)
_wlan_all=$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]{1,3}(\.[0-9]{1,3}){3}$' | grep -v '^127\.')
_wlan_pref=$(printf '%s\n' "$_wlan_all" | grep '^192\.168\.')
_wlan_rest=$(printf '%s\n' "$_wlan_all" | grep -v '^192\.168\.')
CAND_HOSTS=$(printf '%s\n' "$_last_host" "127.0.0.1" "$_wlan_pref" "$_wlan_rest" | awk 'NF && !seen[$0]++')
echo "  候选主机: $(printf '%s' "$CAND_HOSTS" | tr '\n' ' ')"

# ★2026-10-04：先把历史上残留的 offline transport 清干净。
# adb 会为每个 offline 项不停重连（实测日志刷到 280MB），而且会去骚扰
# 守护端口 18250/18252 和 Web 端口 3080 —— 整条机器人链路都会因此变慢、
# node 客户端抓帧超时、面板恒报「虚拟屏守护挂了」。
adb devices 2>/dev/null | awk '/offline/{print $1}' | while read -r _a; do
  [ -n "$_a" ] && adb disconnect "$_a" >/dev/null 2>&1
done

# 「自己人」端口：对它们 adb connect 只会制造 offline 僵尸
_SELF_PORTS=" 5037 3080 3099 8765 8790 7890 ${VSD_PORT:-18250} ${SHD_PORT:-18252} "
_is_self_port() { case "$_SELF_PORTS" in *" $1 "*) return 0 ;; esac; return 1; }

# 试连一个 host:port；**只有这个串号真的变成 device** 才算成功（原判据，别改）
try_connect() {
  _h=$1; _p=$2
  adb connect "$_h:$_p" >/dev/null 2>&1
  if adb devices 2>/dev/null | awk -v s="$_h:$_p" '$1==s && $2=="device"{f=1} END{exit !f}'; then
    SER="$_h:$_p"
    echo "$SER" > "$DSH_ROOT/.adb-serial"
    date +%s > /tmp/adb-last-connect
    echo "  ✅ 已连接并记下串号: $SER"
    adb -s "$SER" shell "id" 2>&1 | head -1 | sed 's/^/  uid: /'
    return 0
  fi
  adb disconnect "$_h:$_p" >/dev/null 2>&1
  return 1
}

# 扫「候选主机 × 端口」。端口下限默认 20000：无线调试端口由系统在 ephemeral 段
# （32768~60999）里选，实测 33801 / 37117 / 45415 全在这段 —— 从 1024 扫纯属浪费。
scan_ports() {
  HOSTS=$(printf '%s' "$1" | tr '\n' ' ') SCAN_LO="${ADB_SCAN_LO:-20000}" node -e '
const net=require("net");
const hosts=(process.env.HOSTS||"127.0.0.1").split(/\s+/).filter(Boolean);
const lo=parseInt(process.env.SCAN_LO||"20000",10);
const targets=[];
for(const h of hosts)for(let p=lo;p<=65535;p++)targets.push([h,p]);
let open=[],done=0;
const CONC=400;let i=0;
function next(){
  while(i<targets.length){
    const t=targets[i++];
    const s=net.connect({host:t[0],port:t[1]});
    let settled=false;
    s.setTimeout(700);
    s.on("connect",()=>{open.push(t[0]+":"+t[1]);s.destroy()});
    s.on("timeout",()=>{s.destroy()});
    s.on("error",()=>{});
    s.on("close",()=>{ if(!settled){settled=true; if(++done===targets.length){console.log(open.filter(x=>!x.endsWith(":5037")).join(" "))} else next();} });
    return;
  }
}
for(let k=0;k<CONC;k++)next();
if(targets.length===0)console.log("");
'
}

case "$1" in
  pair)
    PP="$2"; CODE="$3"
    [ -n "$PP" ] && [ -n "$CODE" ] || { echo "用法: sh connect-adb.sh pair <配对端口|IP:配对端口> <6位码>"; exit 2; }
    # ★2026-10-14：配对口同样只监听 WLAN IP（实测 127.0.0.1 配不通）。
    #   给了「IP:端口」就直用；只给端口则以 WLAN IP 优先，失败再退 127.0.0.1。
    case "$PP" in
      *:*) _pair_list="$PP" ;;
      *)   _wlan1=$(printf '%s\n' "$_wlan_pref" "$_wlan_rest" | awk 'NF' | head -1)
           [ -n "$_wlan1" ] || _wlan1=127.0.0.1
           _pair_list="$_wlan1:$PP 127.0.0.1:$PP" ;;
    esac
    for _t in $_pair_list; do
      echo "  配对 $_t ..."
      _out=$(adb pair "$_t" "$CODE" 2>&1)
      printf '%s\n' "$_out" | sed 's/^/  /'
      case "$_out" in *Successfully*) break ;; esac
    done
    ;;
  ""|--scan)
    echo "  先试上次成功的地址，再扫候选主机（端口下限 ${ADB_SCAN_LO:-20000}）..."
    ;;
  *)
    case "$1" in
      *:*) _cands="$1" ;;
      *)   _cands=$(printf '%s\n' "$CAND_HOSTS" | sed "s|\$|:$1|") ;;
    esac
    for _t in $_cands; do
      echo "  直连 $_t"
      adb connect "$_t" 2>&1 | sed 's/^/  /'
      if adb devices 2>/dev/null | awk -v s="$_t" '$1==s && $2=="device"{f=1} END{exit !f}'; then
        SER="$_t"; echo "$SER" > "$DSH_ROOT/.adb-serial"; date +%s > /tmp/adb-last-connect
        echo "  ✅ 已记下串号: $SER"
        exit 0
      fi
      adb disconnect "$_t" >/dev/null 2>&1
    done
    exit 1
    ;;
esac

# ★2026-10-12：全段扫描前先试「上次成功的端口 + 传统 5555」——
#   无线调试端口有时可以连用很久（37821 曾用两天），先试这两个能把重连从几分钟降到 1 秒。
# ★2026-10-14：这一轮**对每个候选主机都试**（WLAN IP 优先），不再只认 127.0.0.1。
for _h in $CAND_HOSTS; do
  for _p in ${_last_port:+$_last_port} 5555; do
    [ -n "$_p" ] || continue
    _is_self_port "$_p" && continue
    echo "  先试上次/常用端口 $_h:$_p"
    try_connect "$_h" "$_p" && exit 0
  done
done

OPEN=$(scan_ports "$CAND_HOSTS")
echo "  开放端口: ${OPEN:-（无）}"
[ -n "$OPEN" ] || { echo "  ❌ 没扫到端口 —— 请先在手机上打开「无线调试」"; exit 1; }

for _hp in $OPEN; do
  _p=${_hp##*:}
  _is_self_port "$_p" && { echo "  ⏭ 跳过 $_hp（自己人端口，不必试）"; continue; }
  echo "  尝试 adb connect $_hp"
  adb connect "$_hp" 2>&1 | sed 's/^/    /'
  if ! adb devices 2>/dev/null | awk '/device$/{print $1}' | grep -Fxq "$_hp"; then
    adb disconnect "$_hp" >/dev/null 2>&1
    continue
  fi
  SER="$_hp"
  echo "$SER" > "$DSH_ROOT/.adb-serial"
  date +%s > /tmp/adb-last-connect
  echo "  ✅ 已连接并记下串号: $SER"
  adb -s "$SER" shell "id" 2>&1 | head -1 | sed 's/^/  uid: /'
  exit 0
done
echo "  ❌ 端口都在，但 adb 未连上 —— 可能需要先配对：sh connect-adb.sh pair <端口> <6位码>"
exit 1
