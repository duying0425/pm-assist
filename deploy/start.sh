#!/bin/bash
# 启动 / 重启服务（无需 sudo）
cd ~/pm-assist
pkill -f "uvicorn main:app" 2>/dev/null || true
nohup venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000 > logs/app.log 2>&1 &
echo "Started PID $!"
