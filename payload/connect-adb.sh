#!/bin/sh
# connect-adb.sh —— 连接本机「无线调试」
#
#   用法:
#     sh connect-adb.sh                     扫描 127.0.0.1 端口并连接（已配对过）
#     sh connect-adb.sh pair <配对端口> <6位配对码>   首次配对（容器重建后通常需要）
#     sh connect-adb.sh <端口>               直连指定端口
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

# ★2026-10-04：先把历史上残留的 offline transport 清干净。
# adb 会为每个 offline 项不停重连（实测日志刷到 280MB），而且会去骚扰
# 守护端口 18250/18252 和 Web 端口 3080 —— 整条机器人链路都会因此变慢、
# node 客户端抓帧超时、面板恒报「虚拟屏守护挂了」。
adb devices 2>/dev/null | awk '/offline/{print $1}' | while read -r _a; do
  [ -n "$_a" ] && adb disconnect "$_a" >/dev/null 2>&1
done

scan_ports() {
  node -e '
const net=require("net");
const ports=[];
for(let p=1024;p<=65535;p++)ports.push(p);
let open=[],done=0;
const CONC=200;let i=0;
function next(){
  while(i<ports.length){
    const p=ports[i++];
    const s=net.connect({host:"127.0.0.1",port:p});
    let settled=false;
    s.setTimeout(700);
    s.on("connect",()=>{open.push(p);s.destroy()});
    s.on("timeout",()=>{s.destroy()});
    s.on("error",()=>{});
    s.on("close",()=>{ if(!settled){settled=true; if(++done===ports.length){console.log(open.filter(x=>x!==5037).sort((a,b)=>a-b).join(" "))} else next();} });
    return;
  }
}
for(let k=0;k<CONC;k++)next();
if(ports.length===0)console.log("");
'
}

case "$1" in
  pair)
    PP="$2"; CODE="$3"
    [ -n "$PP" ] && [ -n "$CODE" ] || { echo "用法: sh connect-adb.sh pair <配对端口> <6位码>"; exit 2; }
    echo "  配对 127.0.0.1:$PP ..."
    adb pair "127.0.0.1:$PP" "$CODE" 2>&1 | sed 's/^/  /'
    ;;
  ""|--scan)
    echo "  先试上次端口，再全段扫描 127.0.0.1（扫描约 10~20 秒）..."
    ;;
  *)
    echo "  直连 127.0.0.1:$1"
    adb connect "127.0.0.1:$1" 2>&1 | sed 's/^/  /'
    SER=$(adb devices | awk '/device$/{print $1; exit}')
    [ -n "$SER" ] && echo "$SER" > "$DSH_ROOT/.adb-serial" && date +%s > /tmp/adb-last-connect && echo "  ✅ 已记下串号: $SER"
    exit 0
    ;;
esac

# ★2026-10-12：全段扫描前先试「上次成功的端口 + 传统 5555」——
#   无线调试端口有时可以连用很久（37821 曾用两天），先试这两个能把重连从 10~20 秒降到 1 秒。
_last_port=$(sed -n 's/.*:\([0-9]\{1,5\}\)$/\1/p' "$DSH_ROOT/.adb-serial" 2>/dev/null | head -1)
for _p in ${_last_port:+$_last_port} 5555; do
  [ -n "$_p" ] || continue
  case " 5037 3080 3099 8765 8790 7890 ${VSD_PORT:-18250} ${SHD_PORT:-18252} " in
    *" $_p "*) continue ;;
  esac
  echo "  先试上次/常用端口 127.0.0.1:$_p"
  adb connect "127.0.0.1:$_p" >/dev/null 2>&1
  if adb devices 2>/dev/null | awk -v s="127.0.0.1:$_p" '$1==s && $2=="device"{f=1} END{exit !f}'; then
    SER="127.0.0.1:$_p"
    echo "$SER" > "$DSH_ROOT/.adb-serial"
    date +%s > /tmp/adb-last-connect
    echo "  ✅ 直接连上（免全段扫描）: $SER"
    adb -s "$SER" shell "id" 2>&1 | head -1 | sed 's/^/  uid: /'
    exit 0
  fi
  adb disconnect "127.0.0.1:$_p" >/dev/null 2>&1
done

OPEN=$(scan_ports)
echo "  开放端口: ${OPEN:-（无）}"
[ -n "$OPEN" ] || { echo "  ❌ 没扫到端口 —— 请先在手机上打开「无线调试」"; exit 1; }

for p in $OPEN; do
  # ★ 2026-10-05：跳过已知「自己人」端口 —— 对它们 adb connect 只会制造 offline 僵尸
  #   （实测 Web 3080 / 无障碍桥 3099 / 桥接 8765 / 面板 8790 / 两个守护 18250·18252
  #    都会被连成 offline），而僵尸在的时候后续 connect 更容易失败。
  case " 5037 3080 3099 8765 8790 7890 ${VSD_PORT:-18250} ${SHD_PORT:-18252} " in
    *" $p "*) echo "  ⏭ 跳过 127.0.0.1:$p（自己人端口，不必试）"; continue ;;
  esac
  echo "  尝试 adb connect 127.0.0.1:$p"
  adb connect "127.0.0.1:$p" 2>&1 | sed 's/^/    /'
  # ★2026-10-04：只有**这个端口**真的变成 device 才算连上。
  # 老写法「devices 里有任意 device 就算成功」在已有连接时永远为真；
  # 而连不上的端口会以 offline transport 的形式赖在 adb 里被反复重连，
  # 必须当场 disconnect。
  if ! adb devices 2>/dev/null | awk '/device$/{print $1}' | grep -qx "127.0.0.1:$p"; then
    adb disconnect "127.0.0.1:$p" >/dev/null 2>&1
    continue
  fi
  SER="127.0.0.1:$p"
  echo "$SER" > "$DSH_ROOT/.adb-serial"
  date +%s > /tmp/adb-last-connect
  echo "  ✅ 已连接并记下串号: $SER"
  adb -s "$SER" shell "id" 2>&1 | head -1 | sed 's/^/  uid: /'
  exit 0
done
echo "  ❌ 端口都在，但 adb 未连上 —— 可能需要先配对：sh connect-adb.sh pair <端口> <6位码>"
exit 1
