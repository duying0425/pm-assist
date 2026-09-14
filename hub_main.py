"""认证跳板 Hub 独立入口：只挂载 /hub 路由，跑在 127.0.0.1:8002。

启动：uvicorn hub_main:app --host 127.0.0.1 --port 8002（见 deploy/pm-hub.service）
业务进程（main.py:8000）发版重启不影响本进程。
"""
import logging

from fastapi import FastAPI

import auth_hub

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")

try:
    auth_hub.load_hub_config()
except Exception as e:
    raise SystemExit(f"hub_config.json 校验失败: {e}")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(auth_hub.router)
