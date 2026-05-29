from __future__ import annotations

import base64
import hashlib
import hmac as _hmac
import json
import time
import urllib.parse
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import db
from config import ADMIN_REDIRECT_URI, AI_MODEL, FEISHU_APP_ID, FEISHU_APP_SECRET, MAX_HISTORY, SESSION_SECRET

router = APIRouter(prefix="/admin")

_HTML = Path(__file__).parent / "static" / "admin.html"
_SESSION_COOKIE = "pm_session"
_SESSION_TTL = 8 * 3600  # 8 小时

_FEISHU_AUTHORIZE  = "https://open.feishu.cn/open-apis/authen/v1/authorize"
_FEISHU_APP_TOKEN  = "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal"
_FEISHU_USER_TOKEN = "https://open.feishu.cn/open-apis/authen/v1/access_token"

# ── Session helpers ─────────────────────────────────────────


def _make_session(data: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")
    sig = _hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _read_session(token: str) -> dict | None:
    try:
        payload, sig = token.rsplit(".", 1)
        expected = _hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not _hmac.compare_digest(sig, expected):
            return None
        padded = payload + "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded))
        if data.get("exp", 0) < time.time():
            return None
        return data
    except Exception:
        return None


def _get_session(request: Request) -> dict | None:
    token = request.cookies.get(_SESSION_COOKIE, "")
    return _read_session(token) if token else None


def require_auth(request: Request) -> dict:
    """API dependency：无效 session 返回 401。"""
    session = _get_session(request)
    if not session:
        raise HTTPException(status_code=401, detail="未登录")
    return session


def require_super_admin(session: dict = Depends(require_auth)) -> dict:
    """API dependency：非 super_admin 返回 403。"""
    if session.get("role") != "super_admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return session


# ── Auth routes ─────────────────────────────────────────────


@router.get("/login", include_in_schema=False)
def admin_login():
    params = urllib.parse.urlencode({
        "app_id": FEISHU_APP_ID,
        "redirect_uri": ADMIN_REDIRECT_URI,
        "scope": "contact:user.base:readonly",
    })
    return RedirectResponse(f"{_FEISHU_AUTHORIZE}?{params}")


@router.get("/oauth/callback", include_in_schema=False)
async def admin_oauth_callback(code: str = "", error: str = ""):
    if error or not code:
        return HTMLResponse("<h3>授权失败，请关闭后重试</h3>", status_code=400)

    async with httpx.AsyncClient() as client:
        r1 = await client.post(
            _FEISHU_APP_TOKEN,
            json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
            timeout=10,
        )
        app_token = r1.json().get("app_access_token", "")
        if not app_token:
            return HTMLResponse("<h3>获取应用凭证失败，请重试</h3>", status_code=500)
        r2 = await client.post(
            _FEISHU_USER_TOKEN,
            headers={"Authorization": f"Bearer {app_token}"},
            json={"grant_type": "authorization_code", "code": code},
            timeout=10,
        )
    data = r2.json().get("data", {})
    open_id = data.get("open_id", "")
    name = data.get("name", "")

    if not open_id:
        return HTMLResponse("<h3>获取用户信息失败，请关闭后重试</h3>", status_code=400)

    user = db.get_user(open_id)
    role = (user or {}).get("role", "")
    status = (user or {}).get("status", "")
    if role not in ("super_admin", "pm") or status != "active":
        return HTMLResponse(
            f"<h3>无访问权限</h3><p>{name}（{open_id}）不是 admin 或 PM 角色，请联系管理员。</p>",
            status_code=403,
        )

    session_data = {"open_id": open_id, "name": name, "role": role, "exp": time.time() + _SESSION_TTL}
    token = _make_session(session_data)
    response = RedirectResponse("/admin/", status_code=302)
    response.set_cookie(_SESSION_COOKIE, token, max_age=_SESSION_TTL, httponly=True, samesite="lax")
    return response


@router.get("/logout", include_in_schema=False)
def admin_logout():
    response = RedirectResponse("/admin/login", status_code=302)
    response.delete_cookie(_SESSION_COOKIE)
    return response


# ── Pages ────────────────────────────────────────────────────


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def admin_index(request: Request):
    if not _get_session(request):
        return RedirectResponse("/admin/login", status_code=302)
    return _HTML.read_text(encoding="utf-8")


# ── API: whoami ──────────────────────────────────────────────


@router.get("/api/me")
def api_me(session: dict = Depends(require_auth)):
    return {"open_id": session["open_id"], "name": session["name"], "role": session["role"]}


@router.get("/api/version")
def api_version(session: dict = Depends(require_auth)):
    try:
        ver = (Path(__file__).parent / "VERSION").read_text().strip()
    except Exception:
        ver = "unknown"
    return {"version": ver}


# ── Stats ─────────────────────────────────────────────────────

@router.get("/api/stats")
def api_stats(session: dict = Depends(require_auth)):
    return db.get_system_stats()


# ── Settings ──────────────────────────────────────────────────

_SETTINGS_META: dict[str, dict] = {
    # ── AI 模型 ──────────────────────────────────────────────
    "ai_model": {
        "group": "AI 模型",
        "label": "模型 ID",
        "description": "OpenRouter 兼容模型 ID，例如 anthropic/claude-sonnet-4-5",
        "type": "text",
        "options": [],
        "default": AI_MODEL,
    },
    "max_history": {
        "group": "AI 模型",
        "label": "对话历史轮数",
        "description": "每次 AI 对话携带的最大历史消息条数（建议 10–50）",
        "type": "number",
        "options": [],
        "default": str(MAX_HISTORY),
    },
    "chat_max_tokens": {
        "group": "AI 模型",
        "label": "对话最大 Token",
        "description": "AI 对话单次回复的最大输出 token 数",
        "type": "number",
        "options": [],
        "default": "8000",
    },
    "chat_timeout": {
        "group": "AI 模型",
        "label": "对话超时（秒）",
        "description": "AI 对话请求的最长等待时间，超时后提示用户重试",
        "type": "number",
        "options": [],
        "default": "90",
    },
    # ── AI 洗盘 ──────────────────────────────────────────────
    "nightly_review_mode": {
        "group": "AI 洗盘",
        "label": "洗盘模式",
        "description": "report_only: 生成报告并弹确认卡片；direct_cleanup: 自动执行清洗动作",
        "type": "select",
        "options": [["report_only", "仅报告（推荐）"], ["direct_cleanup", "直接清洗"]],
        "default": "report_only",
    },
    "review_max_tokens": {
        "group": "AI 洗盘",
        "label": "洗盘最大 Token",
        "description": "AI 洗盘报告单次回复的最大输出 token 数（建议 ≥ 8000）",
        "type": "number",
        "options": [],
        "default": "16000",
    },
    "review_timeout": {
        "group": "AI 洗盘",
        "label": "洗盘超时（秒）",
        "description": "AI 洗盘请求的最长等待时间（建议 ≥ 120）",
        "type": "number",
        "options": [],
        "default": "180",
    },
    # ── AI 上下文 ─────────────────────────────────────────────
    "todo_open_limit": {
        "group": "AI 上下文",
        "label": "注入待办上限（进行中）",
        "description": "每次注入 AI 上下文的进行中待办最大条数",
        "type": "number",
        "options": [],
        "default": "30",
    },
    "todo_done_limit": {
        "group": "AI 上下文",
        "label": "注入待办上限（已完成）",
        "description": "每次注入 AI 上下文的已完成待办最大条数",
        "type": "number",
        "options": [],
        "default": "10",
    },
    "todo_done_days": {
        "group": "AI 上下文",
        "label": "已完成待办时间窗口（天）",
        "description": "只将近 N 天内完成的待办注入 AI 上下文",
        "type": "number",
        "options": [],
        "default": "14",
    },
    # ── 系统 ─────────────────────────────────────────────────
    "pending_ttl": {
        "group": "系统",
        "label": "确认卡片有效期（秒）",
        "description": "AI 建议/洗盘确认卡片的过期时间，超时后点击操作无效",
        "type": "number",
        "options": [],
        "default": "1800",
    },
}


@router.get("/api/settings")
def api_list_settings(session: dict = Depends(require_super_admin)):
    result = []
    for key, meta in _SETTINGS_META.items():
        result.append({
            "key": key,
            "value": db.get_setting(key, meta["default"]),
            "group": meta.get("group", "其他"),
            "label": meta["label"],
            "description": meta["description"],
            "type": meta["type"],
            "options": meta["options"],
            "default": meta["default"],
        })
    return result


@router.patch("/api/settings/{key}")
def api_update_setting(key: str, data: dict, session: dict = Depends(require_super_admin)):
    if key not in _SETTINGS_META:
        raise HTTPException(status_code=404, detail=f"未知配置项: {key}")
    value = str(data.get("value", "")).strip()
    if not value:
        raise HTTPException(status_code=400, detail="值不能为空")
    meta = _SETTINGS_META[key]
    if meta["type"] == "select":
        valid = [o[0] for o in meta["options"]]
        if value not in valid:
            raise HTTPException(status_code=400, detail=f"无效值，允许: {valid}")
    if meta["type"] == "number":
        try:
            int(value)
        except ValueError:
            raise HTTPException(status_code=400, detail="必须为整数")
    db.set_setting(key, value)
    return {"ok": True, "key": key, "value": value}


# ── Projects ─────────────────────────────────────────────────

@router.get("/api/projects")
def api_list_projects(session: dict = Depends(require_auth)):
    return [dict(r) for r in db.list_projects(active_only=False)]


@router.post("/api/projects")
def api_create_project(data: dict, session: dict = Depends(require_super_admin)):
    try:
        pid = db.add_project(data["name"], data.get("description", ""))
        return {"id": pid}
    except Exception:
        return {"error": "项目名已存在"}


# ── Facts ────────────────────────────────────────────────────

@router.get("/api/facts")
def api_list_facts(
    fact_type: str = Query(default="", alias="type"),
    status: str = Query(default="active"),
    project: str = Query(default=""),
    session: dict = Depends(require_auth),
):
    rows = db.list_facts(
        type_=fact_type or None,
        status=status or None,
        project=project or None,
    )
    return [dict(r) for r in rows]


@router.post("/api/facts")
def api_create_fact(data: dict, session: dict = Depends(require_auth)):
    fid = db.add_fact(
        data["type"], data["title"], data.get("body", ""),
        status=data.get("status", "active"),
        priority=data.get("priority", ""),
        owner=data.get("owner", ""),
        due_date=data.get("due_date", ""),
        project=data.get("project", "默认"),
        source="manual",
    )
    return {"id": fid}


@router.patch("/api/facts/{fact_id}")
def api_update_fact(fact_id: int, data: dict, session: dict = Depends(require_auth)):
    db.update_fact(fact_id, **data)
    return {"ok": True}


@router.delete("/api/facts/{fact_id}")
def api_archive_fact(fact_id: int, session: dict = Depends(require_auth)):
    db.update_fact(fact_id, status="archived")
    return {"ok": True}


# ── Todos ─────────────────────────────────────────────────────

@router.get("/api/todos")
def api_list_todos(
    status: str = Query(default="open"),
    project: str = Query(default=""),
    session: dict = Depends(require_auth),
):
    rows = db.list_todos(status=status or None, project=project or None)
    return [dict(r) for r in rows]


@router.post("/api/todos")
def api_create_todo(data: dict, session: dict = Depends(require_auth)):
    tid = db.add_todo(
        data["title"],
        body=data.get("body", ""),
        priority=data.get("priority", "medium"),
        owner=data.get("owner", ""),
        due_date=data.get("due_date", ""),
        project=data.get("project", "默认"),
        source="manual",
    )
    return {"id": tid}


@router.patch("/api/todos/{todo_id}")
def api_update_todo(todo_id: int, data: dict, session: dict = Depends(require_auth)):
    db.update_todo(todo_id, **data)
    return {"ok": True}


# ── Users ─────────────────────────────────────────────────────

@router.get("/api/users")
def api_list_users(
    role: str = Query(default=""),
    status: str = Query(default=""),
    session: dict = Depends(require_auth),
):
    rows = db.list_users(role=role or None, status=status or None)
    conv_stats = db.get_user_conv_stats()
    result = []
    for r in rows:
        u = dict(r)
        stats = conv_stats.get(u["open_id"], {"chat_count": 0, "msg_count": 0})
        u["conv_chat_count"] = stats["chat_count"]
        u["conv_msg_count"] = stats["msg_count"]
        result.append(u)
    return result


@router.patch("/api/users/{open_id}")
def api_update_user(open_id: str, data: dict, session: dict = Depends(require_super_admin)):
    db.update_user(open_id, **data)
    return {"ok": True}


# ── Assumptions ───────────────────────────────────────────────

@router.get("/api/assumptions")
def api_list_assumptions(
    scope: str = Query(default=""),
    active_only: bool = Query(default=True),
    session: dict = Depends(require_auth),
):
    rows = db.list_assumptions(scope=scope or None, active_only=active_only)
    return [dict(r) for r in rows]


@router.post("/api/assumptions")
def api_create_assumption(data: dict, session: dict = Depends(require_auth)):
    aid = db.add_assumption(
        data["title"], data.get("body", ""),
        scope=data.get("scope", "dept"),
        scope_ref=data.get("scope_ref", ""),
        confidence=data.get("confidence", "common"),
    )
    return {"id": aid}


@router.patch("/api/assumptions/{assumption_id}")
def api_update_assumption(assumption_id: int, data: dict, session: dict = Depends(require_auth)):
    db.update_assumption(assumption_id, **data)
    return {"ok": True}


@router.delete("/api/assumptions/{assumption_id}")
def api_archive_assumption(assumption_id: int, session: dict = Depends(require_auth)):
    db.update_assumption(assumption_id, active=0)
    return {"ok": True}


# ── Logs ──────────────────────────────────────────────────────

_LOG = Path(__file__).parent / "logs" / "app.log"


@router.get("/api/logs")
def api_get_logs(lines: int = Query(default=100, le=500), session: dict = Depends(require_auth)):
    if not _LOG.exists():
        return {"lines": [], "total": 0, "size": 0, "exists": False}
    with _LOG.open(encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()
    tail = all_lines[-lines:]
    return {
        "lines": [ln.rstrip("\n") for ln in tail],
        "total": len(all_lines),
        "size": _LOG.stat().st_size,
        "exists": True,
    }
