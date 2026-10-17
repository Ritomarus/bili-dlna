#!/bin/sh
# 启动 bili-dlna。参数会原样传给程序，例如：./run.sh --host 192.168.1.10 -v
cd "$(dirname "$0")" || exit 1
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "未找到 ffmpeg，请先安装：brew install ffmpeg" >&2
  exit 1
fi
exec python3 -m bilidlna "$@"
