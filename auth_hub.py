"""认证跳板 Hub：兄弟应用复用本应用的飞书凭证完成 OAuth，APP_SECRET 不出本服务。

由 hub_main.py 独立进程运行（127.0.0.1:8002），与 pm-assist 业务进程（8000）隔离，
业务发版重启不影响其他应用的认证。对外 URL 恒为 https://pm.tmhcorps.cn/hub/...
（nginx location /hub/ 转发），飞书侧回调配置永不变化。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, StrictBool

import feishu
from config import (
    ADMIN_OPEN_IDS, FEISHU_APP_ID, FEISHU_APP_SECRET, HUB_ADMIN_API_KEY,
    HUB_CALLBACK_URI, HUB_SECRET, HUB_TOKEN_DB_PATH,
)
from hub_tokens import HubTokenStore

log = logging.getLogger("auth_hub")
router = APIRouter(prefix="/hub")

_FEISHU_AUTHORIZE = "https://open.feishu.cn/open-apis/authen/v1/authorize"
_FEISHU_TOKEN_V2 = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
_FEISHU_USER_INFO = "https://open.feishu.cn/open-apis/authen/v1/user_info"

_STATE_TTL = 600      # 授权跳转 state 有效期（秒）
_AUTH_CODE_TTL = 300  # 一次性 auth_code 有效期（秒）

# hub 配置文件：全局授权 scope + client 注册表，每次请求现读，改动免重启
_HUB_CONFIG_FILE = Path(__file__).with_name("hub_config.json")

# 一次性 auth_code -> 换发结果，用后即删；单进程部署故存内存即可
_auth_codes: dict[str, dict] = {}
_token_store: HubTokenStore | None = None
_token_locks: dict[tuple[str, str], asyncio.Lock] = {}
_REFRESH_MARGIN = 300


def token_store() -> HubTokenStore:
    global _token_store
    if _token_store is None:
        _token_store = HubTokenStore(HUB_TOKEN_DB_PATH)
    return _token_store


def _token_lock(client_id: str, open_id: str) -> asyncio.Lock:
    # 部署为一个 uvicorn 进程；所有换发/续期路由共用此锁。
    return _token_locks.setdefault((client_id, open_id), asyncio.Lock())


def _authorize_url(client_id: str) -> str:
    return HUB_CALLBACK_URI.rsplit("/", 1)[0] + "/authorize?" + urllib.parse.urlencode({
        "client_id": client_id,
    })


def _authorization_required(client_id: str) -> HTTPException:
    return HTTPException(status_code=409, detail={
        "code": "authorization_required",
        "message": "请在接入应用中重新登录飞书，授权成功后即可取 token。",
        "authorize_url": _authorize_url(client_id),
    })


def _legacy_token_response(grant: dict) -> dict:
    now = time.time()
    return {
        "access_token": grant["access_token"],
        "refresh_token": grant["refresh_token"],
        "expires_in": max(0, int(grant["expires_at"] - now)),
        "refresh_token_expires_in": max(0, int(grant["refresh_expires_at"] - now)),
    }


async def _request_refresh(client_id: str, refresh_token: str) -> dict:
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(_FEISHU_TOKEN_V2, json={
                "grant_type": "refresh_token",
                "client_id": FEISHU_APP_ID,
                "client_secret": FEISHU_APP_SECRET,
                "refresh_token": refresh_token,
            }, timeout=10)
        d = r.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=502, detail="飞书刷新服务暂时不可用") from None
    if r.status_code != 200 or d.get("code") != 0:
        log.warning("refresh failed client=%s code=%s", client_id, d.get("code"))
        # 原客户端依赖 20073 等飞书错误码提示重新登录。
        raise HTTPException(status_code=400, detail={"code": d.get("code"),
                                                    "msg": "飞书拒绝刷新凭证"})
    if not d.get("access_token") or not d.get("refresh_token") or d.get("expires_in", 0) <= 0:
        raise HTTPException(status_code=502, detail="飞书返回的凭证不完整")
    return d


def _refreshed_grant(previous: dict, data: dict) -> dict:
    now = time.time()
    return {
        "open_id": previous["open_id"], "name": previous["name"],
        "access_token": data["access_token"], "refresh_token": data["refresh_token"],
        "expires_at": now + data["expires_in"],
        "refresh_expires_at": now + data.get("refresh_token_expires_in", 0),
    }


async def _managed_token(client_id: str, open_id: str, force_refresh: bool = False,
                         presented_refresh: str | None = None) -> tuple[dict, bool]:
    async with _token_lock(client_id, open_id):
        grant = token_store().get(client_id, open_id)
        if not grant:
            raise _authorization_required(client_id)
        # 旧客户端拿着轮换前的刷新凭证时，返回 Hub 当前凭证，避免重复使用一次性凭证。
        force = force_refresh or (presented_refresh is not None and hmac.compare_digest(
            presented_refresh.encode(), grant["refresh_token"].encode()
        ))
        if not force and grant["expires_at"] > time.time() + _REFRESH_MARGIN:
            return grant, False
        if not grant["refresh_token"] or grant["refresh_expires_at"] <= time.time():
            raise _authorization_required(client_id)
        try:
            data = await _request_refresh(client_id, grant["refresh_token"])
        except HTTPException as exc:
            if isinstance(exc.detail, dict) and exc.detail.get("code") == 20073:
                token_store().invalidate_refresh(client_id, open_id)
                raise _authorization_required(client_id) from None
            raise
        saved = token_store().save(client_id, _refreshed_grant(grant, data))
        log.info("managed token refreshed client=%s open_id=%s", client_id, open_id)
        return saved, True


def _require_admin(authorization: str):
    if not HUB_ADMIN_API_KEY or not ADMIN_OPEN_IDS:
        raise HTTPException(status_code=503, detail="后台 token API 尚未配置")
    scheme, _, key = authorization.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        key.encode(), HUB_ADMIN_API_KEY.encode()
    ):
        raise HTTPException(status_code=401, detail="invalid admin API key")


def _admin_accounts() -> list[dict]:
    registered = _clients(load_hub_config())
    return [a for a in token_store().list_accounts()
            if a["client_id"] in registered and a["open_id"] in ADMIN_OPEN_IDS]


class UserTokenRequest(BaseModel):
    client_id: str | None = None
    open_id: str | None = None
    force_refresh: StrictBool = False


def load_hub_config() -> dict:
    try:
        cfg = json.loads(_HUB_CONFIG_FILE.read_text(encoding="utf-8"))
        if not isinstance(cfg.get("scopes"), list) or not cfg["scopes"]:
            raise ValueError("scopes 必须为非空数组")
        if not isinstance(cfg.get("clients"), list) or not cfg["clients"]:
            raise ValueError("clients 必须为非空数组")
    except (OSError, ValueError) as e:
        log.error("hub_config.json 加载失败: %s", e)
        raise HTTPException(status_code=500, detail="hub 配置加载失败，请检查 hub_config.json")
    return cfg


def _clients(cfg: dict) -> dict:
    return {c["id"]: c for c in cfg["clients"]}


# ── client 凭证校验 ──────────────────────────────────────────

def _require_client(data: dict) -> dict:
    c = _clients(load_hub_config()).get(str(data.get("client_id", "")))
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
    cfg = load_hub_config()
    c = _clients(cfg).get(client_id)
    if not c:
        raise HTTPException(status_code=404, detail="未注册的接入应用")
    params = urllib.parse.urlencode({
        "client_id": FEISHU_APP_ID,
        "redirect_uri": HUB_CALLBACK_URI,
        "scope": " ".join(cfg["scopes"]),
        "state": _make_state(client_id, state),
    })
    return RedirectResponse(f"{_FEISHU_AUTHORIZE}?{params}")


@router.get("/callback")
async def hub_callback(code: str = "", state: str = "", error: str = ""):
    parsed = _read_state(state)
    if error or not code or not parsed:
        return HTMLResponse("<h3>授权失败，请关闭后重试</h3>", status_code=400)
    c = _clients(load_hub_config()).get(parsed["client_id"])
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

    received_at = time.time()

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
    if r2.status_code != 200 or not open_id:
        return HTMLResponse("<h3>获取用户身份失败，请重试</h3>", status_code=502)

    _purge_expired()
    auth_code = secrets.token_urlsafe(32)
    _auth_codes[auth_code] = {
        "client_id": c["id"],
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "expires_at": received_at + data.get("expires_in", 0),
        "refresh_expires_at": received_at + data.get("refresh_token_expires_in", 0),
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
async def api_token(data: dict):
    c = _require_client(data)
    _purge_expired()
    auth_code = str(data.get("auth_code", ""))
    entry = _auth_codes.get(auth_code)
    if not entry or entry["client_id"] != c["id"]:
        raise HTTPException(status_code=400, detail="auth_code 无效或已过期")
    async with _token_lock(c["id"], entry["open_id"]):
        if _auth_codes.get(auth_code) is not entry or entry["exp"] <= time.time():
            raise HTTPException(status_code=400, detail="auth_code 无效或已过期")
        saved = token_store().save(c["id"], entry)
        del _auth_codes[auth_code]
    log.info("token delivered client=%s open_id=%s", c["id"], entry["open_id"])
    return {
        **_legacy_token_response(saved),
        "open_id": entry["open_id"],
        "name": entry["name"],
    }


@router.post("/api/refresh")
async def api_refresh(data: dict):
    c = _require_client(data)
    refresh_token = str(data.get("refresh_token", ""))
    if not refresh_token:
        raise HTTPException(status_code=400, detail="refresh_token 不能为空")
    # 同一旧凭证的首次迁移也串行处理，避免向飞书重复提交一次性 refresh_token。
    legacy_key = "refresh:" + hashlib.sha256(refresh_token.encode()).hexdigest()
    async with _token_lock(c["id"], legacy_key):
        grant = token_store().find_refresh(c["id"], refresh_token)
        if grant:
            try:
                current, _ = await _managed_token(c["id"], grant["open_id"],
                                                   presented_refresh=refresh_token)
            except HTTPException as exc:
                if exc.status_code == 409:
                    raise HTTPException(status_code=400, detail={
                        "code": 20073, "msg": "授权已失效，请重新登录飞书",
                    }) from None
                raise
            return _legacy_token_response(current)

        # 尚未进入 Hub 持久化的旧客户端：保留原刷新接口，成功后尝试登记用户身份。
        d = await _request_refresh(c["id"], refresh_token)
        received_at = time.time()
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(_FEISHU_USER_INFO,
                                     headers={"Authorization": "Bearer " + d["access_token"]},
                                     timeout=10)
            info = r.json().get("data", {})
            if r.status_code == 200 and info.get("open_id"):
                async with _token_lock(c["id"], info["open_id"]):
                    token_store().save(c["id"], {
                        "open_id": info["open_id"], "name": info.get("name", ""),
                        "access_token": d["access_token"], "refresh_token": d["refresh_token"],
                        "expires_at": received_at + d["expires_in"],
                        "refresh_expires_at": received_at + d.get("refresh_token_expires_in", 0),
                    }, previous_refresh_token=refresh_token)
        except (httpx.HTTPError, ValueError):
            # 上游已经轮换凭证，即使身份查询失败也必须把新凭证交还原客户端。
            log.warning("legacy token identity unavailable client=%s", c["id"])
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


# ── 后台一键获取用户 token（独立管理 API Key）────────────────

@router.post("/api/user-token/accounts")
async def api_user_token_accounts(authorization: str = Header(default="")):
    _require_admin(authorization)
    now = time.time()
    return {"accounts": [{
        "client_id": a["client_id"], "open_id": a["open_id"], "name": a["name"],
        "expires_at": datetime.fromtimestamp(a["expires_at"], timezone.utc).isoformat(),
        "refresh_available": a["refresh_expires_at"] > now,
    } for a in _admin_accounts()]}


@router.post("/api/user-token")
async def api_user_token(data: UserTokenRequest, authorization: str = Header(default="")):
    _require_admin(authorization)
    registered = _clients(load_hub_config())
    if data.open_id and data.open_id not in ADMIN_OPEN_IDS:
        raise HTTPException(status_code=403, detail="仅允许获取已配置管理员的授权 token")
    if data.client_id and data.client_id not in registered:
        raise HTTPException(status_code=404, detail="未注册的接入应用")
    accounts = [a for a in _admin_accounts()
                if (not data.client_id or a["client_id"] == data.client_id)
                and (not data.open_id or a["open_id"] == data.open_id)]
    if not accounts:
        client_id = data.client_id or next(iter(registered))
        raise _authorization_required(client_id)
    if len(accounts) != 1:
        raise HTTPException(status_code=409, detail={
            "code": "account_required", "message": "存在多个授权，请指定 client_id 和 open_id。",
            "accounts": [{k: a[k] for k in ("client_id", "open_id", "name")} for a in accounts],
        })
    account = accounts[0]
    grant, refreshed = await _managed_token(account["client_id"], account["open_id"],
                                            force_refresh=data.force_refresh)
    log.info("admin user token delivered client=%s open_id=%s refreshed=%s",
             account["client_id"], account["open_id"], refreshed)
    return {
        "user_access_token": grant["access_token"], "token_type": "Bearer",
        "expires_in": max(0, int(grant["expires_at"] - time.time())),
        "expires_at": datetime.fromtimestamp(grant["expires_at"], timezone.utc).isoformat(),
        "client_id": grant["client_id"], "open_id": grant["open_id"], "name": grant["name"],
        "refreshed": refreshed,
    }
