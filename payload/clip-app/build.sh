#!/bin/sh
# build.sh —— 构建 DSH 剪贴板 + 无键盘输入法小工具（复用 openwith 那套工具链）
set -e
cd "$(dirname "$0")"

AJ=${AJ:-/sdcard/dsh/vscreen-src/android.jar}
D8_JAR=${D8_JAR:-/sdcard/dsh/vscreen-src/d8.jar}
KS=${KS:-$PWD/dsh-clip.keystore}
KSPASS=dshclip
ALIAS=dshclip

[ -f "$AJ" ] || { echo "缺 android.jar: $AJ"; exit 1; }
[ -f "$D8_JAR" ] || { echo "缺 d8.jar: $D8_JAR"; exit 1; }

rm -rf obj bin
mkdir -p obj bin

echo "[1/7] aapt2 compile 资源"
aapt2 compile --dir res -o bin/res.zip

echo "[2/7] aapt2 link"
aapt2 link -I "$AJ" --manifest AndroidManifest.xml \
  --min-sdk-version 21 --target-sdk-version 29 \
  -o bin/base.apk bin/res.zip

echo "[3/7] javac"
javac -nowarn -source 8 -target 8 -bootclasspath "$AJ" -cp "$AJ" \
  -d obj $(find src -name '*.java')

echo "[4/7] d8"
java -cp "$D8_JAR" com.android.tools.r8.D8 --release --min-api 21 --lib "$AJ" \
  --output bin $(find obj -name '*.class') 2>&1 | grep -viE '^warning|desugar' || true
[ -f bin/classes.dex ] || { echo "D8 未产出 classes.dex"; exit 1; }

echo "[5/7] 打包 + 对齐"
( cd bin && aapt add base.apk classes.dex >/dev/null )
zipalign -f -p 4 bin/base.apk bin/aligned.apk

echo "[6/7] 签名"
if [ ! -f "$KS" ]; then
  keytool -genkeypair -keystore "$KS" -alias "$ALIAS" \
    -keyalg RSA -keysize 2048 -validity 10000 \
    -storepass "$KSPASS" -keypass "$KSPASS" \
    -dname "CN=DSH Clip, O=local, C=CN" >/dev/null 2>&1
fi
apksigner sign --ks "$KS" --ks-key-alias "$ALIAS" \
  --ks-pass pass:"$KSPASS" --key-pass pass:"$KSPASS" \
  --out bin/dsh-clip.apk bin/aligned.apk

echo "[7/7] 校验"
apksigner verify --verbose bin/dsh-clip.apk | head -4
echo "产出：$PWD/bin/dsh-clip.apk（$(stat -c%s bin/dsh-clip.apk) 字节）"
