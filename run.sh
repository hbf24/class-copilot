#!/usr/bin/env bash
# Class Copilot - Linux / macOS 启动脚本
set -e
cd "$(dirname "$0")"

if [ ! -f config.json ]; then
  echo "首次运行: 已复制 config.example.json -> config.json, 请填入 API Key 后重新运行。"
  cp config.example.json config.json
  ( sleep 2; python3 -m webbrowser "http://127.0.0.1:8321" || true ) &
fi

PORT="${CC_PORT:-8321}"
( sleep 2; python3 -m webbrowser "http://127.0.0.1:${PORT}" || true ) &
exec python3 app.py
