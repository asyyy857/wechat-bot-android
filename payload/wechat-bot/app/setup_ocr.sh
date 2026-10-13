#!/bin/bash
# 恢复 OCR 运行时（RapidOCR + onnxruntime + opencv + numpy）。
#
# 为什么必须放在容器内（默认 /root/ocrlibs）：
#   /sdcard 是 FUSE 挂载，共享库**没法 mmap**，放在那里 import 会直接报
#   "failed to map segment from shared object"。所以持久副本只能以 tar 包形态躺在 /sdcard。
set -e
LIB=${WX_OCRLIB:-/root/ocrlibs}
TARBALL=/sdcard/dsh/ocrlibs.tar.gz

if [ -d "$LIB/rapidocr_onnxruntime" ] && [ -d "$LIB/numpy" ]; then
  echo "OCR 运行时已就绪：$LIB"
  exit 0
fi

if [ -f "$TARBALL" ]; then
  echo "从 $TARBALL 解包到 /root …"
  tar xzf "$TARBALL" -C "$(dirname "$LIB")"
else
  echo "没有备份包，从 pypi 安装到 $LIB …（需要联网）"
  python3 -m pip install --break-system-packages --target="$LIB" -q rapidocr-onnxruntime
fi

PYTHONPATH="$LIB" python3 -c "
from rapidocr_onnxruntime import RapidOCR
print('OCR 自检通过：RapidOCR 可用')
"
