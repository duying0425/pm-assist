import os
from dotenv import load_dotenv

load_dotenv()

FEISHU_APP_ID = os.environ["FEISHU_APP_ID"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET"]
FEISHU_VERIFICATION_TOKEN = os.environ["FEISHU_VERIFICATION_TOKEN"]

OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://router.tmhcorps.cn/v1")
AI_MODEL = os.getenv("AI_MODEL", "anthropic/claude-sonnet-4-5")

ADMIN_OPEN_IDS = set(filter(None, os.getenv("ADMIN_OPEN_IDS", "").split(",")))
NOTIFY_OPEN_IDS = set(filter(None, os.getenv("NOTIFY_OPEN_IDS", "").split(",")))
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "20"))

# 决策报告唯一接收人（默认取 ADMIN_OPEN_IDS 第一个，或单独配置）
PRIMARY_ADMIN_OPEN_ID = os.getenv(
    "PRIMARY_ADMIN_OPEN_ID",
    next(iter(sorted(ADMIN_OPEN_IDS)), ""),
)
