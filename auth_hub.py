"""认证跳板 Hub：兄弟应用复用本应用的飞书凭证完成 OAuth，APP_SECRET 不出本服务。

由 hub_main.py 独立进程运行（127.0.0.1:8001），与 pm-assist 业务进程（8000）隔离，
业务发版重启不影响其他应用的认证。对外 URL 恒为 https://pm.tmhcorps.cn/hub/...
（nginx location /hub/ 转发），飞书侧回调配置永不变化。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
import urllib.parse

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

import feishu
from config import FEISHU_APP_ID, FEISHU_APP_SECRET, HUB_CALLBACK_URI, HUB_CLIENTS, HUB_SECRET

log = logging.getLogger("auth_hub")
router = APIRouter(prefix="/hub")

_FEISHU_AUTHORIZE = "https://open.feishu.cn/open-apis/authen/v1/authorize"
_FEISHU_TOKEN_V2 = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
_FEISHU_USER_INFO = "https://open.feishu.cn/open-apis/authen/v1/user_info"

_STATE_TTL = 600      # 授权跳转 state 有效期（秒）
_AUTH_CODE_TTL = 300  # 一次性 auth_code 有效期（秒）

_clients: dict[str, dict] = {c["id"]: c for c in HUB_CLIENTS}
# 一次性 auth_code -> 换发结果，用后即删；单进程部署故存内存即可
_auth_codes: dict[str, dict] = {}


# ── client 凭证校验 ──────────────────────────────────────────

def _require_client(data: dict) -> dict:
    c = _clients.get(str(data.get("client_id", "")))
    if not c or not hmac.compare_digest(
        c["secret"].encode(), str(data.get("client_secret", "")).encode()
    ):
        raise HTTPException(status_code=401, detail="invalid client credentials")
    return c


# ── state 签名（无状态 HMAC，防伪造/防 CSRF）──────────────────

def _sign(payload: str) -> str:
    return hmac.new(HUB_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _make_state(client_id: str, client_state: str) -> str:
    # client_state 放最后，允许其自身包含任意字符
    payload = f"{client_id}:{int(time.time())}:{secrets.token_hex(8)}:{client_state}"
    return f"{payload}:{_sign(payload)}"


def _read_state(state: str) -> dict | None:
    try:
        client_id, ts, nonce, rest = state.split(":", 3)
        client_state, sig = rest.rsplit(":", 1)
        payload = f"{client_id}:{ts}:{nonce}:{client_state}"
    except ValueError:
        return None
    if not hmac.compare_digest(_sign(payload), sig):
        return None
    if int(ts) + _STATE_TTL < time.time():
        return None
    return {"client_id": client_id, "client_state": client_state}


# ── 授权入口 / 回调（浏览器 302 链路）────────────────────────

@router.get("/authorize")
def hub_authorize(client_id: str = "", state: str = ""):
    c = _clients.get(client_id)
    if not c:
        raise HTTPException(status_code=404, detail="未注册的接入应用")
    params = urllib.parse.urlencode({
        "client_id": FEISHU_APP_ID,
        "redirect_uri": HUB_CALLBACK_URI,
        "scope": c["scopes"],
        "state": _make_state(client_id, state),
    })
    return RedirectResponse(f"{_FEISHU_AUTHORIZE}?{params}")


@router.get("/callback")
async def hub_callback(code: str = "", state: str = "", error: str = ""):
    parsed = _read_state(state)
    if error or not code or not parsed:
        return HTMLResponse("<h3>授权失败，请关闭后重试</h3>", status_code=400)
    c = _clients.get(parsed["client_id"])
    if not c:
        return HTMLResponse("<h3>未注册的接入应用</h3>", status_code=400)

    async with httpx.AsyncClient() as client:
        r = await client.post(_FEISHU_TOKEN_V2, json={
            "grant_type": "authorization_code",
            "client_id": FEISHU_APP_ID,
            "client_secret": FEISHU_APP_SECRET,
            "code": code,
            "redirect_uri": HUB_CALLBACK_URI,
        }, timeout=10)
    data = r.json()
    if data.get("code") != 0:
        log.warning("token exchange failed client=%s code=%s msg=%s",
                    c["id"], data.get("code"), data.get("msg"))
        return HTMLResponse("<h3>换取凭证失败，请重试</h3>", status_code=500)

    open_id, name = "", ""
    async with httpx.AsyncClient() as client:
        r2 = await client.get(
            _FEISHU_USER_INFO,
            headers={"Authorization": f"Bearer {data['access_token']}"},
            timeout=10,
        )
    info = r2.json().get("data", {})
    open_id = info.get("open_id", "")
    name = info.get("name", "")

    auth_code = secrets.token_urlsafe(32)
    _auth_codes[auth_code] = {
        "client_id": c["id"],
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "expires_in": data.get("expires_in", 0),
        "refresh_expires_in": data.get("refresh_token_expires_in", 0),
        "open_id": open_id,
        "name": name,
        "exp": time.time() + _AUTH_CODE_TTL,
    }
    log.info("auth_code issued client=%s open_id=%s", c["id"], open_id)

    sep = "&" if "?" in c["redirect_uri"] else "?"
    params = {"auth_code": auth_code}
    if parsed["client_state"]:
        params["state"] = parsed["client_state"]
    return RedirectResponse(c["redirect_uri"] + sep + urllib.parse.urlencode(params))


# ── 换发接口（接入应用服务端 → hub）──────────────────────────

def _purge_expired():
    now = time.time()
    for k in [k for k, v in _auth_codes.items() if v["exp"] < now]:
        del _auth_codes[k]


@router.post("/api/token")
def api_token(data: dict):
    c = _require_client(data)
    _purge_expired()
    entry = _auth_codes.pop(str(data.get("auth_code", "")), None)
    if not entry or entry["client_id"] != c["id"]:
        raise HTTPException(status_code=400, detail="auth_code 无效或已过期")
    log.info("token delivered client=%s open_id=%s", c["id"], entry["open_id"])
    return {
        "access_token": entry["access_token"],
        "refresh_token": entry["refresh_token"],
        "expires_in": entry["expires_in"],
        "refresh_token_expires_in": entry["refresh_expires_in"],
        "open_id": entry["open_id"],
        "name": entry["name"],
    }


@router.post("/api/refresh")
async def api_refresh(data: dict):
    c = _require_client(data)
    refresh_token = str(data.get("refresh_token", ""))
    if not refresh_token:
        raise HTTPException(status_code=400, detail="refresh_token 不能为空")
    async with httpx.AsyncClient() as client:
        r = await client.post(_FEISHU_TOKEN_V2, json={
            "grant_type": "refresh_token",
            "client_id": FEISHU_APP_ID,
            "client_secret": FEISHU_APP_SECRET,
            "refresh_token": refresh_token,
        }, timeout=10)
    d = r.json()
    if d.get("code") != 0:
        log.warning("refresh failed client=%s code=%s msg=%s",
                    c["id"], d.get("code"), d.get("msg"))
        # 透传飞书错误码（如 20073），接入方可据此提示重新登录
        raise HTTPException(status_code=400,
                            detail={"code": d.get("code"), "msg": d.get("msg")})
    log.info("token refreshed client=%s", c["id"])
    return {
        "access_token": d["access_token"],
        "refresh_token": d["refresh_token"],
        "expires_in": d.get("expires_in", 0),
        "refresh_token_expires_in": d.get("refresh_token_expires_in", 0),
    }


@router.post("/api/tenant-token")
async def api_tenant_token(data: dict):
    c = _require_client(data)
    token = await feishu.get_tenant_token(FEISHU_APP_ID, FEISHU_APP_SECRET)
    log.info("tenant token delivered client=%s", c["id"])
    return {"tenant_access_token": token}
