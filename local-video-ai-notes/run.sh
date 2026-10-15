#!/usr/bin/env bash
# video-notes 启动器 (Linux / macOS)
# 用法: 把视频/文件夹拖到此脚本，或 ./run.sh /路径/视频.mp4
set -e
cd "$(dirname "$0")"

# 把自带的 ffmpeg 加进 PATH（tools/ffmpeg/bin）
if [ -d "tools/ffmpeg/bin" ]; then
  export PATH="$PWD/tools/ffmpeg/bin:$PATH"
fi

# 激活 venv（若存在）
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

# 清洗输入：去首尾引号 / file:// 前缀（拖拽或粘贴路径时常见）
IN="$1"
IN="${IN#\"}"; IN="${IN%\"}"
IN="${IN#\'}"; IN="${IN%\'}"
case "$IN" in
  file:///*) IN="${IN#file://}" ;;
  file://*)  IN="${IN#file://}" ;;
esac

if [ -z "$IN" ]; then
  echo "用法: ./run.sh <视频文件或文件夹>"
  echo "示例: ./run.sh /home/user/课程/第1讲.mp4"
  echo "      ./run.sh /home/user/课程合集   (递归，按源路径镜像到 output/)"
  exit 1
fi

if [ ! -e "$IN" ]; then
  echo "文件/文件夹不存在: $IN"
  exit 1
fi

echo "[*] 输入: $IN"
python make_notes.py "$IN" "${@:2}"
