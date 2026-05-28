import os
import secrets
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

# Web 管理后台 session 签名密钥。未配置时每次重启生成新密钥（重启后需重新登录）。
SESSION_SECRET = os.getenv("SESSION_SECRET") or secrets.token_hex(32)
# 飞书 OAuth 回调地址，需与飞书开放平台配置一致
ADMIN_REDIRECT_URI = os.getenv("ADMIN_REDIRECT_URI", "https://pm.tmhcorps.cn/admin/oauth/callback")
