#!/bin/sh
# v2.0.0 路径解析（shell 版，与 wxauto/paths.py 同一口径）
#   source 这个文件后可用：WXBOT_APPDIR / WXBOT_DATADIR / WXBOT_LOGDIR
# 用法：WXBOT_APPDIR=$(cd "$(dirname "$0")" && pwd) 之前先确定脚本位置
WXBOT_APPDIR=${1:-${WXBOT_APPDIR:-$(cd "$(dirname "$0")" 2>/dev/null && pwd)}}
WXBOT_ROOT=$(dirname "$WXBOT_APPDIR")
if [ -n "${WXBOT_DATA_DIR:-}" ]; then
  WXBOT_DATADIR=$WXBOT_DATA_DIR
elif [ -d "$WXBOT_ROOT/data" ]; then
  WXBOT_DATADIR=$WXBOT_ROOT/data
else
  WXBOT_DATADIR=$WXBOT_APPDIR          # v1 混装布局兜底
fi
if [ -n "${WXBOT_LOG_DIR:-}" ]; then
  WXBOT_LOGDIR=$WXBOT_LOG_DIR
elif [ -d "$WXBOT_ROOT/logs" ]; then
  WXBOT_LOGDIR=$WXBOT_ROOT/logs
else
  WXBOT_LOGDIR=$WXBOT_APPDIR
fi
export WXBOT_APPDIR WXBOT_DATADIR WXBOT_LOGDIR
mkdir -p "$WXBOT_DATADIR" "$WXBOT_LOGDIR" 2>/dev/null || true
