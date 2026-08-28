#!/usr/bin/env bash
# 启动后端服务（固定使用项目 venv 解释器，避免 Homebrew 升级 python 导致依赖丢失）
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "venv 不存在，正在创建..."
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip -q
  .venv/bin/pip install -r requirements.txt -r requirements-heavy.txt
fi

# 若端口已有服务在跑，先停掉（只杀监听进程，避免误杀 Obsidian 等客户端连接）
lsof -ti :8765 -sTCP:LISTEN | xargs kill 2>/dev/null || true
sleep 1

nohup .venv/bin/python -m backend > backend.log 2>&1 &
echo "PID=$!"
sleep 3
curl -s -m 5 http://127.0.0.1:8765/api/health && echo
