#!/bin/sh
# env.sh —— 所有脚本的公共环境（被 source，不单独执行）
# 关键：jar 与源码都放在 /sdcard，容器重建后仍在
export HOME=/root
export PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/node-v22.23.2-linux-arm64/bin
export DSH_ROOT=${DSH_ROOT:-/sdcard/dsh}

# 工具 jar：优先用持久化副本，回退到容器内 SDK 路径
AJ=${AJ:-}
if [ -z "$AJ" ]; then
  if [ -f "$DSH_ROOT/vscreen-src/android.jar" ]; then AJ="$DSH_ROOT/vscreen-src/android.jar"
  else AJ=/usr/lib/android-sdk/platforms/android-23/android.jar; fi
fi
D8_JAR=${D8_JAR:-}
if [ -z "$D8_JAR" ]; then
  if [ -f "$DSH_ROOT/vscreen-src/d8.jar" ]; then D8_JAR="$DSH_ROOT/vscreen-src/d8.jar"
  else D8_JAR=/usr/lib/android-sdk/build-tools/34.0.0/lib/d8.jar; fi
fi
export AJ D8_JAR

# 端口
# 2026-10-03：18240/18242 在本机被"失效的端口绑定"劫持 —— TCP 连得上但永远不回数据，
# 守护的 accept 队列一路堆积（ss 里 Recv-Q 涨到十几），谁连谁挂。
# 换到 18250/18252 后立刻恢复正常。这两个必须 export，否则 python/node 子进程
# 读不到，会退回客户端里写死的旧端口，又踩回同一个坑。
export VSD_PORT=${VSD_PORT:-18250}   # 受信任虚拟屏守护
export SHD_PORT=${SHD_PORT:-18252}   # 命令端点守护
PHONE_PORT=${PHONE_PORT:-3099} # 宿主无障碍桥

# 字形先验 OCR 纠错（★2026-10-13）：RapidOCR 对游戏自绘 UI 会把**形近字**稳定读错 ——
# 「已领」→ 巴领/己领/已邻、「点击右侧图标定位」→ 志击丰侧图标定位。
# 纠错器（tools/rike-ocr-fix.py + rike-wordlist.txt）用**游戏正文字体的字形 IoU**
# 把这些观测串吸回游戏文案词表；实测 11 张历史帧：修对 10 处、误纠 0、行数不变。
# 默认开着；要临时关掉：RIKE_OCR_FIX=0 sh tools/rike.sh ...
export RIKE_OCR_FIX=${RIKE_OCR_FIX:-1}

# adb 串号（无线调试连接后写入）
SERIAL=$(cat "$DSH_ROOT/.adb-serial" 2>/dev/null || true)
export SERIAL
