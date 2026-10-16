#!/bin/sh
# build.sh —— 构建「DSH 浮窗」App（复用 openwith 的免下载工具链）
set -e
cd "$(dirname "$0")"

AJ=${AJ:-/sdcard/dsh/vscreen-src/android.jar}
D8_JAR=${D8_JAR:-/sdcard/dsh/vscreen-src/d8.jar}
KS=${KS:-$PWD/dsh-hud.keystore}
KSPASS=dshhud
ALIAS=dshhud

[ -f "$AJ" ] || { echo "缺 android.jar: $AJ"; exit 1; }
[ -f "$D8_JAR" ] || { echo "缺 d8.jar: $D8_JAR"; exit 1; }

rm -rf obj bin
mkdir -p obj bin

echo "[1/6] aapt2 link"
aapt2 link -I "$AJ" --manifest AndroidManifest.xml \
  --min-sdk-version 21 --target-sdk-version 22 -o bin/base.apk

echo "[2/6] javac"
javac -nowarn -encoding UTF-8 -source 8 -target 8 -bootclasspath "$AJ" -cp "$AJ" \
  -d obj $(find src -name '*.java')

echo "[3/6] d8"
java -cp "$D8_JAR" com.android.tools.r8.D8 --release --min-api 21 --lib "$AJ" \
  --output bin $(find obj -name '*.class') 2>&1 | grep -viE '^warning|desugar' || true
[ -f bin/classes.dex ] || { echo "D8 未产出 classes.dex"; exit 1; }

echo "[4/6] 打包 + 对齐"
( cd bin && aapt add base.apk classes.dex >/dev/null )
zipalign -f -p 4 bin/base.apk bin/aligned.apk

echo "[5/6] 签名"
if [ ! -f "$KS" ]; then
  keytool -genkeypair -keystore "$KS" -alias "$ALIAS" \
    -keyalg RSA -keysize 2048 -validity 10000 \
    -storepass "$KSPASS" -keypass "$KSPASS" \
    -dname "CN=DSH Hud, O=local, C=CN" >/dev/null 2>&1
fi
apksigner sign --ks "$KS" --ks-key-alias "$ALIAS" \
  --ks-pass pass:"$KSPASS" --key-pass pass:"$KSPASS" \
  --out bin/dsh-hud.apk bin/aligned.apk

echo "[6/6] 校验"
apksigner verify --verbose bin/dsh-hud.apk | head -5
echo "产出：$PWD/bin/dsh-hud.apk（$(stat -c%s bin/dsh-hud.apk) 字节）"
