#!/bin/sh
# build.sh —— 编译命令端点守护
set -e
. "$(dirname "$0")/../env.sh"
cd "$(dirname "$0")"
[ -f "$AJ" ] || { echo "缺 android.jar: $AJ"; exit 1; }
[ -f "$D8_JAR" ] || { echo "缺 d8.jar: $D8_JAR"; exit 1; }
rm -rf obj bin; mkdir -p obj bin
javac -nowarn -source 8 -target 8 -bootclasspath "$AJ" -cp "$AJ" -d obj src/dsh/ShellDaemon.java
java -cp "$D8_JAR" com.android.tools.r8.D8 --release --min-api 21 --lib "$AJ" \
  --output bin $(find obj -name '*.class') 2>&1 | grep -viE '^warning|desugar' || true
[ -f bin/classes.dex ] || { echo "D8 未产出 classes.dex"; exit 1; }
echo "构建完成：$PWD/bin/classes.dex（$(stat -c%s bin/classes.dex) 字节）"
