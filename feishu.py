from __future__ import annotations

import json
import time
import logging
import httpx

_token_cache: dict = {"value": None, "expires_at": 0}
log = logging.getLogger(__name__)

FEISHU_BASE = "https://open.feishu.cn/open-apis"

_TYPE_LABELS = {
    "risk": "风险",
    "issue": "问题",
    "blocker": "阻塞",
    "dependency": "依赖",
    "milestone": "里程碑",
    "decision": "决策",
    "team": "人员",
    "client": "客户",
    "org": "组织",
    "process": "流程",
    "knowledge": "知识",
}


# ── Schema 2.0 helpers ────────────────────────────────────────

def _md(content: str) -> dict:
    """顶层 markdown 元素，支持完整 Markdown（##标题、表格、代码块等）。"""
    return {"tag": "markdown", "content": content}


def _lark_div(content: str) -> dict:
    """column 内部专用：div + lark_md（有限 Markdown，支持加粗/换行）。"""
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def _button_row(buttons: list[dict]) -> dict:
    columns = []
    for btn in buttons:
        columns.append({
            "tag": "column",
            "width": "weighted",
            "weight": 1,
            "elements": [btn],
        })
    return {"tag": "column_set", "flex_mode": "none", "columns": columns}


def _card(elements: list, header: dict | None = None,
          wide: bool = True, forward: bool = True) -> dict:
    """构建 schema 2.0 卡片 dict。"""
    c: dict = {
        "schema": "2.0",
        "config": {"wide_screen_mode": wide, "enable_forward": forward},
        "body": {"elements": elements},
    }
    if header:
        c["header"] = header
    return c


def _header(title: str, color: str = "blue") -> dict:
    return {"title": {"tag": "plain_text", "content": title}, "template": color}


def _resp(toast_type: str, toast_content: str, elements: list,
          header: dict | None = None) -> dict:
    """构建卡片回调响应 dict（schema 2.0）。"""
    data: dict = {
        "schema": "2.0",
        "config": {"enable_forward": False},
        "body": {"elements": elements},
    }
    if header:
        data["header"] = header
    return {
        "toast": {"type": toast_type, "content": toast_content},
        "card": {"type": "raw", "data": data},
    }


# ── 基础 API 函数 ──────────────────────────────────────────────

async def get_tenant_token(app_id: str, app_secret: str) -> str:
    if _token_cache["value"] and time.time() < _token_cache["expires_at"]:
        return _token_cache["value"]
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
        )
        data = resp.json()
    _token_cache["value"] = data["tenant_access_token"]
    _token_cache["expires_at"] = time.time() + data["expire"] - 300
    return _token_cache["value"]


async def get_bot_open_id(app_id: str, app_secret: str) -> str:
    token = await get_tenant_token(app_id, app_secret)
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{FEISHU_BASE}/bot/v3/info",
            headers={"Authorization": f"Bearer {token}"},
        )
    data = resp.json()
    return data.get("bot", {}).get("open_id", "")


async def send_text_to_user(open_id: str, text: str, app_id: str, app_secret: str):
    token = await get_tenant_token(app_id, app_secret)
    async with httpx.AsyncClient() as client:
        for chunk in _split(text, 4000):
            await client.post(
                f"{FEISHU_BASE}/im/v1/messages",
                headers={"Authorization": f"Bearer {token}"},
                params={"receive_id_type": "open_id"},
                json={"receive_id": open_id, "msg_type": "text",
                      "content": json.dumps({"text": chunk})},
            )


async def send_text(chat_id: str, text: str, app_id: str, app_secret: str):
    token = await get_tenant_token(app_id, app_secret)
    async with httpx.AsyncClient() as client:
        for chunk in _split(text, 4000):
            await client.post(
                f"{FEISHU_BASE}/im/v1/messages",
                headers={"Authorization": f"Bearer {token}"},
                params={"receive_id_type": "chat_id"},
                json={"receive_id": chat_id, "msg_type": "text",
                      "content": json.dumps({"text": chunk})},
            )


async def send_card_to_user(open_id: str, card: dict, app_id: str, app_secret: str):
    """向指定用户发送交互卡片（直接消息）。"""
    token = await get_tenant_token(app_id, app_secret)
    async with httpx.AsyncClient() as client:
        await client.post(
            f"{FEISHU_BASE}/im/v1/messages",
            headers={"Authorization": f"Bearer {token}"},
            params={"receive_id_type": "open_id"},
            json={"receive_id": open_id, "msg_type": "interactive",
                  "content": json.dumps(card)},
        )


async def send_text_return_id(chat_id: str, text: str, app_id: str, app_secret: str) -> str:
    """Send a text message and return its message_id (empty string on failure)."""
    token = await get_tenant_token(app_id, app_secret)
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{FEISHU_BASE}/im/v1/messages",
            headers={"Authorization": f"Bearer {token}"},
            params={"receive_id_type": "chat_id"},
            json={"receive_id": chat_id, "msg_type": "text",
                  "content": json.dumps({"text": text})},
        )
        return resp.json().get("data", {}).get("message_id", "")


async def update_message_text(message_id: str, text: str, app_id: str, app_secret: str) -> bool:
    """Update an existing text message in-place (Feishu PATCH body API). Returns True on success."""
    if not message_id:
        return False
    token = await get_tenant_token(app_id, app_secret)
    async with httpx.AsyncClient() as client:
        resp = await client.patch(
            f"{FEISHU_BASE}/im/v1/messages/{message_id}/body",
            headers={"Authorization": f"Bearer {token}"},
            json={"content": json.dumps({"text": text[:4000]})},
        )
        return resp.status_code == 200


async def get_user_name(open_id: str, app_id: str, app_secret: str) -> str:
    """Fetch user display name from Feishu contact API by open_id."""
    try:
        token = await get_tenant_token(app_id, app_secret)
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{FEISHU_BASE}/contact/v3/users/{open_id}",
                headers={"Authorization": f"Bearer {token}"},
                params={"user_id_type": "open_id"},
            )
            return resp.json().get("data", {}).get("user", {}).get("name", "") or ""
    except Exception:
        return ""


def _split(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts = []
    while text:
        parts.append(text[:limit])
        text = text[limit:]
    return parts


# ── 统一发送入口 ──────────────────────────────────────────────

async def send_reply(chat_id: str, content: str | dict, app_id: str, app_secret: str):
    """统一回复：dict → interactive 卡片；短字符串 → text；长字符串/多行 → markdown 卡片。"""
    token = await get_tenant_token(app_id, app_secret)
    if isinstance(content, dict):
        msg_type, body = "interactive", json.dumps(content)
    elif len(content) < 60 and "\n" not in content:
        msg_type, body = "text", json.dumps({"text": content})
    else:
        msg_type, body = "interactive", json.dumps(build_md_card(content))
    async with httpx.AsyncClient() as client:
        await client.post(
            f"{FEISHU_BASE}/im/v1/messages",
            headers={"Authorization": f"Bearer {token}"},
            params={"receive_id_type": "chat_id"},
            json={"receive_id": chat_id, "msg_type": msg_type, "content": body},
        )


async def send_reply_to_user(open_id: str, content: str | dict, app_id: str, app_secret: str):
    """send_reply 的 DM 版（open_id）。"""
    token = await get_tenant_token(app_id, app_secret)
    if isinstance(content, dict):
        msg_type, body = "interactive", json.dumps(content)
    elif len(content) < 60 and "\n" not in content:
        msg_type, body = "text", json.dumps({"text": content})
    else:
        msg_type, body = "interactive", json.dumps(build_md_card(content))
    async with httpx.AsyncClient() as client:
        await client.post(
            f"{FEISHU_BASE}/im/v1/messages",
            headers={"Authorization": f"Bearer {token}"},
            params={"receive_id_type": "open_id"},
            json={"receive_id": open_id, "msg_type": msg_type, "content": body},
        )


async def send_card_return_id(chat_id: str, card: dict, app_id: str, app_secret: str) -> str:
    """发 interactive 卡片并返回 message_id（供后续 PATCH 更新）。"""
    token = await get_tenant_token(app_id, app_secret)
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{FEISHU_BASE}/im/v1/messages",
            headers={"Authorization": f"Bearer {token}"},
            params={"receive_id_type": "chat_id"},
            json={"receive_id": chat_id, "msg_type": "interactive",
                  "content": json.dumps(card)},
        )
        return resp.json().get("data", {}).get("message_id", "")


async def update_message_card(message_id: str, card: dict, app_id: str, app_secret: str) -> bool:
    """原地更新 interactive 卡片消息（PATCH /im/v1/messages/{id}，注意不带 /body）。
    schema 2.0 更新必须在 config 中注入 update_multi=true。"""
    if not message_id:
        return False
    update_card = {**card, "config": {**card.get("config", {}), "update_multi": True}}
    token = await get_tenant_token(app_id, app_secret)
    async with httpx.AsyncClient() as client:
        resp = await client.patch(
            f"{FEISHU_BASE}/im/v1/messages/{message_id}",
            headers={"Authorization": f"Bearer {token}"},
            json={"content": json.dumps(update_card)},
        )
        return resp.status_code == 200




# ── 通用 lark_md / markdown 卡片 ──────────────────────────────

def build_md_card(text: str, title: str | None = None, color: str = "blue") -> dict:
    """把 markdown 文本包成飞书 interactive 卡片（schema 2.0，完整 Markdown 渲染）。"""
    elements = [_md(text)]
    return _card(elements, header=_header(title, color) if title else None)


def build_thinking_card() -> dict:
    return build_md_card("⏳ 思考中...")


def card_skipped_response() -> dict:
    """通用跳过/错误响应（用于澄清回调等兜底场景）。"""
    return _resp("info", "已跳过", [_md("⏭ 已跳过。")])


# ── 注册审批卡片 ───────────────────────────────────────────────

def build_approval_card(open_id: str, name: str, role: str, project: str) -> dict:
    """构建用户注册审批卡片（发给管理员）。"""
    _ROLE_ZH = {"pm": "项目经理 PM", "member": "普通成员"}
    role_zh = _ROLE_ZH.get(role, role)
    elements = [
        {
            "tag": "div",
            "fields": [
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**申请人**\n{name}"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**申请角色**\n{role_zh}"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**申请项目**\n{project}"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**open_id**\n{open_id}"}},
            ],
        },
        {"tag": "hr"},
        _button_row([
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "✅ 批准"},
                "type": "primary",
                "value": {
                    "action": "approve_user",
                    "open_id": open_id,
                    "name": name,
                    "role": role,
                    "project": project,
                },
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "❌ 拒绝"},
                "type": "danger",
                "value": {
                    "action": "reject_user",
                    "open_id": open_id,
                    "name": name,
                },
            },
        ]),
    ]
    return _card(elements, header=_header("📋 新用户注册申请", "blue"), wide=False, forward=False)


def card_approved_response(name: str, role: str, project: str) -> dict:
    _ROLE_ZH = {"pm": "项目经理 PM", "member": "普通成员"}
    return _resp("success", f"已批准 {name}",
                 [_md(f"✅ 已批准 **{name}** 以「{_ROLE_ZH.get(role, role)}」身份加入「{project}」")])


def card_rejected_response(name: str) -> dict:
    return _resp("info", f"已拒绝 {name}",
                 [_md(f"❌ 已拒绝 **{name}** 的申请")])


# ── 创建项目审批卡片 ────────────────────────────────────────────

def build_project_request_card(open_id: str, name: str, proj_name: str, description: str) -> dict:
    """构建新建项目审批卡片（发给管理员）。"""
    elements = [
        {
            "tag": "div",
            "fields": [
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**申请人**\n{name}"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**项目名称**\n{proj_name}"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**项目描述**\n{description or '（无）'}"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**open_id**\n{open_id}"}},
            ],
        },
        {"tag": "hr"},
        _button_row([
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "✅ 批准创建"},
                "type": "primary",
                "value": {
                    "action": "approve_project",
                    "open_id": open_id,
                    "name": name,
                    "proj_name": proj_name,
                    "description": description,
                },
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "❌ 拒绝"},
                "type": "danger",
                "value": {
                    "action": "reject_project",
                    "open_id": open_id,
                    "name": name,
                    "proj_name": proj_name,
                },
            },
        ]),
    ]
    return _card(elements, header=_header("🆕 新建项目申请", "wathet"), wide=False, forward=False)


def card_project_approved_response(name: str, proj_name: str) -> dict:
    return _resp("success", f"已批准创建「{proj_name}」",
                 [_md(f"✅ 已批准 **{name}** 创建项目「{proj_name}」")])


def card_project_rejected_response(name: str, proj_name: str) -> dict:
    return _resp("info", f"已拒绝「{proj_name}」",
                 [_md(f"❌ 已拒绝 **{name}** 的项目创建申请「{proj_name}」")])


# ── Merge 确认卡片 ─────────────────────────────────────────────

def build_merge_confirm_card(merges: list[dict], chat_id: str,
                             saved_count: int = 0) -> dict:
    elements = []
    for i, item in enumerate(merges[:10]):
        keep_id = item.get("keep_id", "")
        merge_ids = item.get("merge_ids", [])
        reason = item.get("reason", "")
        append_text = item.get("append_text", "")
        append_preview = append_text if len(append_text) <= 70 else append_text[:67] + "…"
        merge_text = ", ".join(f"#{mid}" for mid in merge_ids)

        elements.append({
            "tag": "column_set",
            "flex_mode": "none",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 5,
                    "elements": [_lark_div(
                        f"**{i + 1}. 合并到 #{keep_id}**\n"
                        f"合入：{merge_text}\n"
                        f"原因：{reason}\n"
                        f"追加：{append_preview}"
                    )],
                },
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [{
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "合并"},
                        "type": "primary",
                        "value": {
                            "action": "merge_one",
                            "chat_id": chat_id,
                            "index": i,
                            "saved_count": saved_count,
                        },
                    }],
                },
            ],
        })

    title_text = (f"✅ 已合并 {saved_count} 组，还剩 {len(merges)} 组"
                  if saved_count > 0
                  else f"🔀 建议合并 {len(merges)} 组信息")
    elements.extend([
        {"tag": "hr"},
        _button_row([
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "✓ 全部合并"},
                "type": "primary",
                "value": {"action": "merge_all", "chat_id": chat_id,
                          "saved_count": saved_count},
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "✗ 跳过"},
                "type": "danger",
                "value": {"action": "skip_merges", "chat_id": chat_id},
            },
        ]),
    ])
    header_color = "green" if saved_count > 0 else "blue"
    return _card(elements, header=_header(title_text, header_color), forward=False)


async def send_merge_confirm_card(chat_id: str, merges: list[dict],
                                  app_id: str, app_secret: str):
    token = await get_tenant_token(app_id, app_secret)
    card = build_merge_confirm_card(merges, chat_id)
    async with httpx.AsyncClient() as client:
        await client.post(
            f"{FEISHU_BASE}/im/v1/messages",
            headers={"Authorization": f"Bearer {token}"},
            params={"receive_id_type": "chat_id"},
            json={"receive_id": chat_id, "msg_type": "interactive",
                  "content": json.dumps(card)},
        )


def card_merge_saved_response(count: int) -> dict:
    return _resp("success", f"已合并 {count} 组信息",
                 [_md(f"✅ 已合并 {count} 组信息。")])


def card_merge_one_saved_response(item: dict, remaining: list[dict],
                                  chat_id: str, saved_count: int) -> dict:
    updated = build_merge_confirm_card(remaining, chat_id, saved_count)
    return {
        "toast": {"type": "success", "content": f"已合并到 #{item.get('keep_id')}"},
        "card": {"type": "raw", "data": updated},
    }


def card_merge_skipped_response() -> dict:
    return _resp("info", "已跳过", [_md("⏭ 已跳过合并建议。")])


# ── Risk/Todo 清洗动作确认卡片 ────────────────────────────────

_ACTION_LABELS = {
    ("risk", "close"): "关闭风险",
    ("fact", "archive"): "归档信息",
    ("todo", "done"): "完成待办",
    ("todo", "cancel"): "取消待办",
}


def build_action_confirm_card(actions: list[dict], chat_id: str,
                              saved_count: int = 0) -> dict:
    elements = []
    for i, item in enumerate(actions[:10]):
        kind = item.get("kind", "")
        action = item.get("action", "")
        item_id = item.get("id", "")
        title = item.get("title", "")
        reason = item.get("reason", "")
        label = _ACTION_LABELS.get((kind, action), f"{kind}.{action}")
        prefix = "#T" if kind == "todo" else "#"
        type_label = item.get("type_label", "")
        type_tag = f"[{type_label}] " if type_label else ""

        elements.append({
            "tag": "column_set",
            "flex_mode": "none",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 5,
                    "elements": [_lark_div(
                        f"**{i + 1}. {label}** {type_tag}{prefix}{item_id} {title}\n"
                        f"原因：{reason}"
                    )],
                },
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [{
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": label[:4]},
                        "type": "primary",
                        "value": {
                            "action": "review_action_one",
                            "chat_id": chat_id,
                            "index": i,
                            "saved_count": saved_count,
                        },
                    }],
                },
            ],
        })

    title_text = (f"✅ 已处理 {saved_count} 项，还剩 {len(actions)} 项"
                  if saved_count > 0
                  else f"⚙️ 建议处理 {len(actions)} 项风险/待办")
    elements.extend([
        {"tag": "hr"},
        _button_row([
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "✓ 全部处理"},
                "type": "primary",
                "value": {"action": "review_action_all", "chat_id": chat_id,
                          "saved_count": saved_count},
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "✗ 跳过"},
                "type": "danger",
                "value": {"action": "skip_review_actions", "chat_id": chat_id},
            },
        ]),
    ])
    header_color = "green" if saved_count > 0 else "blue"
    return _card(elements, header=_header(title_text, header_color), forward=False)


async def send_action_confirm_card(chat_id: str, actions: list[dict],
                                   app_id: str, app_secret: str):
    token = await get_tenant_token(app_id, app_secret)
    card = build_action_confirm_card(actions, chat_id)
    async with httpx.AsyncClient() as client:
        await client.post(
            f"{FEISHU_BASE}/im/v1/messages",
            headers={"Authorization": f"Bearer {token}"},
            params={"receive_id_type": "chat_id"},
            json={"receive_id": chat_id, "msg_type": "interactive",
                  "content": json.dumps(card)},
        )


def card_action_saved_response(count: int) -> dict:
    return _resp("success", f"已处理 {count} 项",
                 [_md(f"✅ 已处理 {count} 项风险/待办。")])


def card_action_one_saved_response(item: dict, remaining: list[dict],
                                   chat_id: str, saved_count: int) -> dict:
    updated = build_action_confirm_card(remaining, chat_id, saved_count)
    return {
        "toast": {"type": "success", "content": f"已处理：{item.get('title', '')[:20]}"},
        "card": {"type": "raw", "data": updated},
    }


def card_action_skipped_response() -> dict:
    return _resp("info", "已跳过", [_md("⏭ 已跳过风险/待办处理建议。")])


# ── 结构化查询卡片 builder ────────────────────────────────────

_PRIO_ICON = {"high": "🔴", "medium": "🟡", "low": "🟢"}
_PRIO_ZH2 = {"high": "高", "medium": "中", "low": "低"}
_TYPE_TAG = {
    "risk": "风险", "issue": "问题", "blocker": "阻塞", "dependency": "依赖",
    "milestone": "里程碑", "decision": "决策", "knowledge": "知识",
}


# ── AI 建议确认卡片（统一入口）────────────────────────────────

_SUGGESTION_SECTIONS = [
    ("risk_fact",    "⚠️ 风险 / 问题"),
    ("schedule_fact","📅 里程碑"),
    ("other_fact",   "📋 知识 / 决策 / 信息"),
    ("todo",         "☐ 待办事项"),
    ("update",       "✏️ 更新建议"),
    ("merge",        "🔀 合并建议"),
    ("wash_action",  "⚙️ 清洗建议"),
]

_REVIEW_ACTION_LABELS = {
    "close": "关闭风险", "archive": "归档信息",
    "done": "完成待办", "cancel": "取消待办",
    "add": "新增待办",
}


def _suggestion_group(item: dict) -> str:
    kind = item.get("kind", "")
    if kind == "new_todo":
        return "todo"
    if kind in ("update_fact", "update_todo"):
        return "update"
    if kind == "merge_fact":
        return "merge"
    if kind == "review_action":
        return "wash_action"
    ftype = item.get("type", "knowledge")
    if ftype in ("risk", "issue", "blocker", "dependency"):
        return "risk_fact"
    if ftype == "milestone":
        return "schedule_fact"
    return "other_fact"


def _suggestion_row_text(item: dict) -> str:
    kind = item.get("kind", "")
    status = item.get("status", "pending")
    status_tag = " ✅" if status == "saved" else (" ⏭" if status == "skipped" else "")

    if kind == "new_fact":
        type_label = _TYPE_LABELS.get(item.get("type", ""), item.get("type", ""))
        prio_icon = _PRIO_ICON.get(item.get("priority", ""), "")
        prio_label = _PRIO_ZH2.get(item.get("priority", ""), "")
        parts = []
        if prio_label:
            parts.append(f"{prio_icon} {prio_label}")
        if item.get("owner"):
            parts.append(f"负责人：{item['owner']}")
        if item.get("due_date"):
            parts.append(f"截止：{item['due_date']}")
        meta = "  ".join(parts) if parts else "待填写"
        return f"**[{type_label}] {item.get('title', '')}**{status_tag}\n{meta}"

    if kind == "new_todo":
        prio_icon = _PRIO_ICON.get(item.get("priority", ""), "")
        prio_label = _PRIO_ZH2.get(item.get("priority", ""), "")
        parts = []
        if prio_label:
            parts.append(f"{prio_icon} {prio_label}")
        if item.get("owner"):
            parts.append(f"负责人：{item['owner']}")
        if item.get("due_date"):
            parts.append(f"截止：{item['due_date']}")
        meta = "  ".join(parts) if parts else "待填写"
        return f"**{item.get('title', '')}**{status_tag}\n{meta}"

    if kind == "merge_fact":
        keep_id = item.get("keep_id", "")
        merge_ids = item.get("merge_ids", [])
        from_str = "、".join(f"#{mid}" for mid in merge_ids)
        reason = (item.get("reason") or "")[:50]
        return f"**合并到 #{keep_id}**{status_tag}  合入 {from_str}\n{reason}"

    if kind == "review_action":
        sub_kind = item.get("sub_kind", "")
        action = item.get("action", "")
        label = _REVIEW_ACTION_LABELS.get(action, action)
        reason = (item.get("reason") or "")[:50]
        if sub_kind == "new_todo":
            owner = item.get("owner", "")
            due = item.get("due_date", "")
            meta = "  ".join(filter(None, [owner and f"负责：{owner}", due and f"截止：{due}"]))
            return f"**{label}**{status_tag} {item.get('title', '')}\n{meta or reason}"
        prefix = "#T" if sub_kind == "todo" else "#"
        return f"**{label}** {prefix}{item.get('id', '')}{status_tag}\n{reason}"

    # update_fact / update_todo
    prefix = "#T" if kind == "update_todo" else "#"
    entity_title = item.get("entity_title", "")
    field = item.get("field", "")
    old_v = item.get("old_value", "—")
    new_v = item.get("value", "")
    reason = item.get("reason", "")[:30]
    return (
        f"**{prefix}{item.get('id', '')} {entity_title}**{status_tag}\n"
        f"{field}：{old_v} → {new_v}  {reason}"
    )


def build_ai_suggestions_card(items: list[dict], chat_id: str) -> dict:
    """AI建议确认卡片：按类型分组，实时状态更新，支持逐条/批量保存跳过。"""
    groups: dict[str, list[tuple[int, dict]]] = {k: [] for k, _ in _SUGGESTION_SECTIONS}
    for i, item in enumerate(items[:10]):
        groups[_suggestion_group(item)].append((i, item))

    elements: list[dict] = []
    for group_key, group_title in _SUGGESTION_SECTIONS:
        group_items = groups[group_key]
        if not group_items:
            continue
        elements.append(_md(f"**{group_title}**"))
        for idx, item in group_items:
            elements.append({
                "tag": "column_set",
                "flex_mode": "none",
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 5,
                        "elements": [_lark_div(_suggestion_row_text(item))],
                    },
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "vertical_align": "center",
                        "elements": [{
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "详情"},
                            "type": "default",
                            "value": {"action": "suggestion_view_detail",
                                      "chat_id": chat_id, "index": idx},
                        }],
                    },
                ],
            })
        elements.append({"tag": "hr"})

    if elements and elements[-1]["tag"] == "hr":
        elements.pop()

    total = len(items)
    n_saved = sum(1 for x in items if x.get("status") == "saved")
    n_skipped = sum(1 for x in items if x.get("status") == "skipped")
    processed = n_saved + n_skipped

    if processed >= total and total > 0:
        title_text = f"✅ AI 建议处理完毕（保存 {n_saved} · 跳过 {n_skipped}）"
        header_color = "green"
        elements.append({"tag": "hr"})
        elements.append(_md(
            f"共处理 {total} 项 AI 建议：新增/更新 {n_saved} 项，跳过 {n_skipped} 项。"
        ))
    else:
        title_text = (
            f"💡 AI 建议（已处理 {processed}/{total}）" if processed > 0
            else f"💡 AI 建议（{total} 项）"
        )
        header_color = "green" if processed > 0 else "blue"
        elements.extend([
            {"tag": "hr"},
            _button_row([
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "全部保存"},
                    "type": "primary",
                    "value": {"action": "suggestion_save_all", "chat_id": chat_id},
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "全部跳过"},
                    "type": "danger",
                    "value": {"action": "suggestion_skip_all", "chat_id": chat_id},
                },
            ]),
        ])

    return _card(elements, header=_header(title_text, header_color), forward=False)


def build_suggestion_detail_card(item: dict, chat_id: str, index: int) -> dict:
    """AI建议详情卡片：显示完整信息，提供保存/跳过/返回操作。"""
    kind = item.get("kind", "")
    status = item.get("status", "pending")
    done = status in ("saved", "skipped")

    if kind == "new_fact":
        type_label = _TYPE_LABELS.get(item.get("type", ""), item.get("type", ""))
        prio_icon = _PRIO_ICON.get(item.get("priority", ""), "")
        prio_label = _PRIO_ZH2.get(item.get("priority", ""), "—")
        meta = (
            f"**建议操作**  新增\n"
            f"**类型**  {type_label}\n"
            f"**优先级**  {prio_icon} {prio_label}\n"
            f"**负责人**  {item.get('owner') or '—'}\n"
            f"**截止**  {item.get('due_date') or '—'}"
        )
        elements: list[dict] = [_md(meta)]
        if item.get("body"):
            elements.extend([{"tag": "hr"}, _md(item["body"])])
        header_text = f"新增 [{type_label}] {item.get('title', '')[:40]}"
        header_color = "blue"

    elif kind == "new_todo":
        prio_icon = _PRIO_ICON.get(item.get("priority", ""), "")
        prio_label = _PRIO_ZH2.get(item.get("priority", ""), "—")
        meta_lines = [
            f"**建议操作**  新增待办",
            f"**优先级**  {prio_icon} {prio_label}",
            f"**负责人**  {item.get('owner') or '—'}",
            f"**截止**  {item.get('due_date') or '—'}",
        ]
        if item.get("source_fact_id"):
            meta_lines.append(f"**关联风险**  #{item['source_fact_id']}")
        if item.get("plan_id"):
            meta_lines.append(f"**挂载里程碑**  #{item['plan_id']}")
        elements = [_md("\n".join(meta_lines))]
        if item.get("body"):
            elements.extend([{"tag": "hr"}, _md(item["body"])])
        header_text = f"新增待办：{item.get('title', '')[:40]}"
        header_color = "blue"

    elif kind == "merge_fact":
        keep_id = item.get("keep_id", "")
        merge_ids = item.get("merge_ids", [])
        from_str = "、".join(f"#{mid}" for mid in merge_ids)
        append_text = item.get("append_text", "")
        reason = item.get("reason", "")
        meta = (
            f"**建议操作**  合并信息\n"
            f"**保留条目**  #{keep_id}\n"
            f"**合入条目**  {from_str}\n"
            f"**原因**  {reason}"
        )
        elements: list[dict] = [_md(meta)]
        if append_text:
            elements.extend([{"tag": "hr"}, _md(f"**追加内容预览：**\n{append_text}")])
        header_text = f"合并到 #{keep_id}  ← {from_str}"
        header_color = "yellow"

    elif kind == "review_action":
        sub_kind = item.get("sub_kind", "")
        action = item.get("action", "")
        label = _REVIEW_ACTION_LABELS.get(action, action)
        reason = item.get("reason", "")
        title = item.get("title", "")
        if sub_kind == "new_todo":
            parts = [f"**建议操作**  {label}", f"**标题**  {title}"]
            if item.get("priority"):
                parts.append(f"**优先级**  {item['priority']}")
            if item.get("owner"):
                parts.append(f"**负责人**  {item['owner']}")
            if item.get("due_date"):
                parts.append(f"**截止**  {item['due_date']}")
            if item.get("body"):
                parts.append(f"**说明**  {item['body']}")
            parts.append(f"**原因**  {reason}")
            meta = "\n".join(parts)
            header_text = f"{label}：{title[:30]}"
            header_color = "green"
        else:
            prefix = "#T" if sub_kind == "todo" else "#"
            meta = (
                f"**建议操作**  {label}\n"
                f"**目标**  {prefix}{item.get('id', '')} {title}\n"
                f"**原因**  {reason}"
            )
            header_text = f"{label}：{prefix}{item.get('id', '')} {title[:30]}"
            header_color = "red" if action in ("close", "archive") else "blue"
        elements = [_md(meta)]

    else:  # update_fact / update_todo
        prefix = "#T" if kind == "update_todo" else "#"
        entity_title = item.get("entity_title", "")
        field = item.get("field", "")
        old_v = item.get("old_value", "—")
        new_v = item.get("value", "")
        reason = item.get("reason", "")
        meta = (
            f"**建议操作**  更新字段\n"
            f"**目标**  {prefix}{item.get('id', '')} {entity_title}\n"
            f"**字段**  {field}\n"
            f"**当前值**  {old_v}\n"
            f"**新值**  {new_v}\n"
            f"**原因**  {reason}"
        )
        elements = [_md(meta)]
        header_text = f"更新 {prefix}{item.get('id', '')} {entity_title[:30]}"
        header_color = "yellow"

    elements.append({"tag": "hr"})
    btns: list[dict] = [{
        "tag": "button",
        "text": {"tag": "plain_text", "content": "返回清单"},
        "type": "default",
        "value": {"action": "suggestion_back_to_list", "chat_id": chat_id},
    }]
    if not done:
        btns.extend([
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "保存"},
                "type": "primary",
                "value": {"action": "suggestion_save_one",
                          "chat_id": chat_id, "index": index},
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "跳过"},
                "type": "danger",
                "value": {"action": "suggestion_skip_one",
                          "chat_id": chat_id, "index": index},
            },
        ])
    else:
        btns.append({
            "tag": "button",
            "text": {"tag": "plain_text",
                     "content": "已保存" if status == "saved" else "已跳过"},
            "type": "default",
            "value": {"action": "suggestion_back_to_list", "chat_id": chat_id},
        })
    elements.append(_button_row(btns))

    return _card(elements, header=_header(header_text, header_color), forward=False)


def build_risk_list_card(rows: list, status_filter: str = "open") -> dict:
    """/risk list 结构化卡片，每条附带「详情」按钮。"""
    if not rows:
        return build_md_card(f"暂无{'open' if status_filter == 'open' else ''}风险/问题")

    elements = []
    for r in rows:
        icon = _PRIO_ICON.get(r.get("priority", ""), "")
        type_label = _TYPE_TAG.get(r["type"], r["type"])
        prio_label = _PRIO_ZH2.get(r.get("priority", ""), "")
        owner_part = f"  负责人：{r['owner']}" if r.get("owner") else ""
        due_part = f"  截止：{r['due_date']}" if r.get("due_date") else ""
        elements.append({
            "tag": "column_set",
            "flex_mode": "none",
            "horizontal_spacing": "8px",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 5,
                    "elements": [_lark_div(
                        f"**#{r['id']}  {r['title']}**\n"
                        f"{icon} [{type_label}·{prio_label}]{owner_part}{due_part}"
                    )],
                },
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "vertical_align": "center",
                    "elements": [{
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "详情"},
                        "type": "default",
                        "value": {"action": "view_risk_detail", "id": r["id"]},
                    }],
                },
            ],
        })
        elements.append({"tag": "hr"})

    if elements and elements[-1]["tag"] == "hr":
        elements.pop()

    label = "全部" if status_filter == "all" else "进行中"
    return _card(elements,
                 header=_header(f"⚠️ 风险列表（{label} · {len(rows)} 条）", "red"),
                 forward=False)


def build_risk_show_card(fact: dict, open_todos: list) -> dict:
    """/risk show 详情卡片。"""
    icon = _PRIO_ICON.get(fact.get("priority", ""), "")
    type_label = _TYPE_TAG.get(fact["type"], fact["type"])
    prio_label = _PRIO_ZH2.get(fact.get("priority", ""), "—")

    meta = (
        f"**类型**  {type_label}　　**优先级**  {icon} {prio_label}\n"
        f"**负责人**  {fact.get('owner') or '—'}　　**截止**  {fact.get('due_date') or '—'}\n"
        f"**记录**  {fact['created_at'][:10]}　　**更新**  {fact['updated_at'][:10]}"
    )
    elements = [
        _md(meta),
        {"tag": "hr"},
        _md(fact.get("body") or "（无正文）"),
    ]

    if open_todos:
        todo_lines = [f"**关联待办（{len(open_todos)} 条进行中）**"]
        for t in open_todos:
            p = _PRIO_ICON.get(t.get("priority", ""), "")
            owner_s = f"（{t['owner']}）" if t.get("owner") else ""
            todo_lines.append(f"- {p} #T{t['id']} {t['title']}{owner_s}")
        elements.append({"tag": "hr"})
        elements.append(_md("\n".join(todo_lines)))

    elements.append({"tag": "hr"})
    elements.append(_md(f"[🔗 在后台编辑](https://pm.tmhcorps.cn/admin/?tab=facts&edit={fact['id']})"))

    return _card(elements,
                 header=_header(f"#{fact['id']} {fact['title']}", "red"),
                 forward=False)


def build_fact_show_card(fact: dict) -> dict:
    """Generic fact detail card for command confirmation flow."""
    type_label = _TYPE_TAG.get(fact.get("type", ""), fact.get("type", "fact"))
    prio_label = _PRIO_ZH2.get(fact.get("priority", ""), fact.get("priority") or "--")
    meta_lines = [
        f"**类型**  {type_label}    **状态**  {fact.get('status') or 'active'}",
        f"**优先级**  {prio_label}    **负责人**  {fact.get('owner') or '--'}",
        f"**截止**  {fact.get('due_date') or '--'}    **项目**  {fact.get('project') or '--'}",
        f"**创建**  {fact.get('created_at', '')[:16]}    **更新**  {fact.get('updated_at', '')[:16]}",
    ]
    elements = [_md("\n".join(meta_lines)), {"tag": "hr"}, _md(fact.get("body") or "（无正文）")]
    return _card(elements, header=_header(f"#{fact['id']} {fact.get('title','')}", "blue"), forward=False)


def build_todo_list_card(rows: list) -> dict:
    """/todo list 结构化卡片，每条附带「详情」按钮。"""
    if not rows:
        return build_md_card("暂无待办事项")

    _STATUS_ICON = {"open": "☐", "done": "☑", "cancelled": "☒"}
    open_count = sum(1 for r in rows if r["status"] == "open")
    elements = []

    for r in rows:
        icon = _STATUS_ICON.get(r["status"], "☐")
        prio_icon = _PRIO_ICON.get(r.get("priority", ""), "")
        meta = []
        if r.get("owner"):
            meta.append(f"负责人：{r['owner']}")
        if r.get("due_date"):
            meta.append(f"截止：{r['due_date']}")
        meta.append(f"创建：{r['created_at'][:10]}")
        if r["status"] == "done":
            meta.append(f"完成：{r['updated_at'][:10]}")
        meta_str = "　".join(meta)

        src_parts = []
        if r.get("source_fact_id"):
            src_parts.append(f"← risk#{r['source_fact_id']}")
        if r.get("plan_id"):
            src_parts.append(f"← milestone#{r['plan_id']}")
        src_str = ("  " + "  ".join(src_parts)) if src_parts else ""

        elements.append({
            "tag": "column_set",
            "flex_mode": "none",
            "horizontal_spacing": "8px",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 5,
                    "elements": [_lark_div(
                        f"{icon} {prio_icon} **#T{r['id']}  {r['title']}**\n"
                        f"{meta_str}{src_str}"
                    )],
                },
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "vertical_align": "center",
                    "elements": [{
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "详情"},
                        "type": "default",
                        "value": {"action": "view_todo_detail", "id": r["id"]},
                    }],
                },
            ],
        })
        elements.append({"tag": "hr"})

    if elements and elements[-1]["tag"] == "hr":
        elements.pop()

    return _card(elements,
                 header=_header(f"📋 待办列表（进行中 {open_count} / 共 {len(rows)} 条）", "blue"),
                 forward=False)


def build_todo_show_card(todo: dict, source_fact: dict | None, plan_fact: dict | None) -> dict:
    """/todo show 详情卡片。"""
    _STATUS_ZH_TODO = {"open": "进行中", "done": "已完成", "cancelled": "已取消"}
    status_label = _STATUS_ZH_TODO.get(todo["status"], todo["status"])
    prio_icon = _PRIO_ICON.get(todo.get("priority", ""), "")
    prio_label = _PRIO_ZH2.get(todo.get("priority", ""), "—")

    meta_lines = [
        f"**状态**  {status_label}　　**优先级**  {prio_icon} {prio_label}",
        f"**负责人**  {todo.get('owner') or '—'}　　**截止**  {todo.get('due_date') or '—'}",
        f"**创建**  {todo['created_at'][:16]}　　**更新**  {todo['updated_at'][:16]}",
    ]
    if source_fact:
        meta_lines.append(f"**关联风险**  #{todo['source_fact_id']}《{source_fact['title'][:30]}》")
    if plan_fact:
        meta_lines.append(f"**挂载里程碑**  #{todo['plan_id']}《{plan_fact['title'][:30]}》")

    elements = [_md("\n".join(meta_lines))]
    if todo.get("body"):
        elements.append({"tag": "hr"})
        elements.append(_md(todo["body"]))

    elements.append({"tag": "hr"})
    elements.append(_md(f"[🔗 在后台编辑](https://pm.tmhcorps.cn/admin/?tab=todos&edit={todo['id']})"))

    return _card(elements,
                 header=_header(f"#T{todo['id']} {todo['title']}", "blue"),
                 forward=False)


def build_milestone_list_card(rows: list) -> dict:
    """/schedule list 结构化卡片，每条附带「详情」按钮。"""
    import datetime
    if not rows:
        return build_md_card("暂无里程碑")

    today = datetime.date.today().isoformat()
    elements = []
    for r in rows:
        due = r.get("due_date") or ""
        status = r.get("status", "active")
        status_icon = "✅" if status == "resolved" else "🔄"
        if due:
            overdue = status == "active" and due < today
            due_part = f"  ⚠️ 已逾期 {due}" if overdue else f"  📅 {due}"
        else:
            due_part = ""
        owner_part = f"  负责人：{r['owner']}" if r.get("owner") else ""

        elements.append({
            "tag": "column_set",
            "flex_mode": "none",
            "horizontal_spacing": "8px",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 5,
                    "elements": [_lark_div(
                        f"{status_icon} **#{r['id']}  {r['title']}**\n"
                        f"{due_part}{owner_part}".strip()
                    )],
                },
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "vertical_align": "center",
                    "elements": [{
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "详情"},
                        "type": "default",
                        "value": {"action": "view_milestone_detail", "id": r["id"]},
                    }],
                },
            ],
        })
        elements.append({"tag": "hr"})

    if elements and elements[-1]["tag"] == "hr":
        elements.pop()

    active_count = sum(1 for r in rows if r.get("status") == "active")
    return _card(elements,
                 header=_header(f"📅 里程碑（进行中 {active_count} / 共 {len(rows)} 个）", "green"),
                 forward=False)


def build_milestone_show_card(fact: dict, open_todos: list) -> dict:
    """/schedule show 详情卡片。"""
    import datetime
    today = datetime.date.today().isoformat()
    due = fact.get("due_date") or ""
    status = fact.get("status", "active")
    status_label = {"active": "进行中", "resolved": "已完成", "archived": "已归档"}.get(status, status)
    overdue = status == "active" and due and due < today

    meta = (
        f"**状态**  {status_label}　　**截止**  {'⚠️ ' if overdue else ''}{due or '—'}\n"
        f"**负责人**  {fact.get('owner') or '—'}\n"
        f"**记录**  {fact['created_at'][:10]}　　**更新**  {fact['updated_at'][:10]}"
    )
    elements = [_md(meta)]
    if fact.get("body"):
        elements.append({"tag": "hr"})
        elements.append(_md(fact["body"]))

    if open_todos:
        todo_lines = [f"**关联待办（{len(open_todos)} 条进行中）**"]
        for t in open_todos:
            p = _PRIO_ICON.get(t.get("priority", ""), "")
            owner_s = f"（{t['owner']}）" if t.get("owner") else ""
            todo_lines.append(f"- {p} #T{t['id']} {t['title']}{owner_s}")
        elements.append({"tag": "hr"})
        elements.append(_md("\n".join(todo_lines)))

    elements.append({"tag": "hr"})
    elements.append(_md(f"[🔗 在后台编辑](https://pm.tmhcorps.cn/admin/?tab=facts&edit={fact['id']})"))

    header_color = "yellow" if overdue else "green"
    return _card(elements,
                 header=_header(f"📅 #{fact['id']} {fact['title']}", header_color),
                 forward=False)


# ── AI 澄清问题卡片 ───────────────────────────────────────────

def build_clarify_card(question: str, opts: list[str], chat_id: str,
                       sender_open_id: str = "") -> dict:
    """AI 需要澄清时发送的问题卡片，含可选选项按钮。"""
    elements: list = [_md(f"**❓ {question}**")]
    if opts:
        elements.append({"tag": "hr"})
        btn_actions = []
        for opt in opts[:4]:
            btn_actions.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": opt},
                "type": "default",
                "value": {
                    "action": "clarify_option",
                    "text": opt,
                    "chat_id": chat_id,
                    "sender_open_id": sender_open_id,
                },
            })
        elements.append(_button_row(btn_actions))
    elements.append({"tag": "hr"})
    elements.append(_md("💬 也可以直接发送文字回复，我会根据你的回复继续作答。"))
    return _card(elements, header=_header("需要确认一些信息", "yellow"), forward=False)


# ── 早报卡片 ──────────────────────────────────────────────────

def build_morning_report_card(project_name: str, risks: list,
                               review_text: str | None, today: str) -> dict:
    """每日早报卡片：风险摘要 + 可选 AI 洗盘摘要。"""
    _TYPE_ZH = {"risk": "风险", "issue": "问题", "blocker": "阻塞项", "dependency": "依赖"}
    elements: list = []

    if risks:
        high   = [r for r in risks if r.get("priority") == "high"]
        medium = [r for r in risks if r.get("priority") == "medium"]
        low    = [r for r in risks if r.get("priority") == "low"]

        risk_lines: list[str] = []
        for group, icon, label in ((high, "🔴", "高"), (medium, "🟡", "中"), (low, "🟢", "低")):
            if not group:
                continue
            risk_lines.append(f"**{icon} {label}优先级 · {len(group)} 条**")
            for r in group:
                typ = _TYPE_ZH.get(r.get("type", ""), r.get("type", ""))
                owner = f"（{r['owner']}）" if r.get("owner") else ""
                due = f" ⏰{r['due_date']}" if r.get("due_date") else ""
                risk_lines.append(f"- #{r['id']} [{typ}] {r['title']}{owner}{due}")
        risk_lines.append(f"\n共 {len(risks)} 条待处理")
        elements.append(_md("\n".join(risk_lines)))
    else:
        elements.append(_md("✅ 当前无待处理风险/问题"))

    if review_text:
        elements.append({"tag": "hr"})
        elements.append(_md(f"**🤖 AI洗盘报告**\n{review_text}"))

    header_title = f"📋 {today} {project_name}早报" if project_name else f"📋 {today} 综合早报"
    return _card(elements, header=_header(header_title, "blue"), forward=False)


# ── 人员信息卡片 ──────────────────────────────────────────────

def build_user_info_card(user: dict,
                         members: list | None = None,
                         all_users: list | None = None) -> dict:
    """人员信息卡片，按角色展示不同内容。
    - all_users 不为 None → 管理员总览（分类）
    - members 不为 None   → PM 视图（自己 + 项目成员）
    - 否则                → 普通成员视图（仅自己）
    """
    _ROLE_ZH = {"super_admin": "管理员", "pm": "项目经理", "member": "普通成员"}
    _STATUS_ICON = {"active": "✓", "pending": "⏳", "rejected": "✗", "inactive": "—"}

    role = user.get("role", "member")
    name = user.get("name") or "(未知)"
    project = user.get("project") or "—"
    role_zh = _ROLE_ZH.get(role, role)
    joined = (user.get("created_at") or "")[:10]

    # ── 管理员总览 ────────────────────────────────────────────
    if all_users is not None:
        active_admins  = [u for u in all_users if u["role"] == "super_admin" and u["status"] == "active"]
        active_pms     = [u for u in all_users if u["role"] == "pm"          and u["status"] == "active"]
        active_members = [u for u in all_users if u["role"] == "member"      and u["status"] == "active"]
        pending_users  = [u for u in all_users if u["status"] == "pending"]
        inactive_users = [u for u in all_users if u["status"] in ("rejected", "inactive")]

        total_active = len(active_admins) + len(active_pms) + len(active_members)
        lines = [f"## 用户总览（在籍 {total_active} 人）\n"]

        if active_admins:
            lines.append(f"**管理员（{len(active_admins)} 人）**")
            for u in active_admins:
                proj_tag = f"（{u['project']}）" if u.get("project") else ""
                lines.append(f"- {u['name'] or '(未知)'}{proj_tag}")
            lines.append("")

        if active_pms:
            # 按项目分组
            pm_by_proj: dict[str, list] = {}
            for u in active_pms:
                k = u.get("project") or "—"
                pm_by_proj.setdefault(k, []).append(u)
            lines.append(f"**项目经理（{len(active_pms)} 人）**")
            for proj_name in sorted(pm_by_proj):
                names = "、".join(u["name"] or "(未知)" for u in pm_by_proj[proj_name])
                lines.append(f"- {proj_name}：{names}")
            lines.append("")

        if active_members:
            # 按项目分组
            mem_by_proj: dict[str, list] = {}
            for u in active_members:
                k = u.get("project") or "—"
                mem_by_proj.setdefault(k, []).append(u)
            lines.append(f"**普通成员（{len(active_members)} 人）**")
            for proj_name in sorted(mem_by_proj):
                names = "、".join(u["name"] or "(未知)" for u in mem_by_proj[proj_name])
                lines.append(f"- {proj_name}：{names}")
            lines.append("")

        if pending_users:
            lines.append(f"**待审批（{len(pending_users)} 人）**")
            for u in pending_users:
                req_role = _ROLE_ZH.get(u["role"], u["role"])
                proj_tag = f"/{u['project']}" if u.get("project") else ""
                lines.append(f"- ⏳ {u['name'] or '(未知)'}　申请：{req_role}{proj_tag}")
            lines.append("")

        if inactive_users:
            lines.append(f"**已停用/拒绝（{len(inactive_users)} 人）**")
            for u in inactive_users:
                icon = _STATUS_ICON.get(u["status"], u["status"])
                lines.append(f"- {icon} {u['name'] or '(未知)'}")

        return build_md_card("\n".join(lines), title="用户总览", color="purple")

    # ── PM 视图 ───────────────────────────────────────────────
    if members is not None:
        lines = [
            "## 我的信息",
            f"**姓名：** {name}　**角色：** {role_zh}　**项目：** {project}　**加入：** {joined}",
            "",
            f"## {project} · 项目成员",
        ]
        if not members:
            lines.append("（暂无其他成员）")
        else:
            adm  = [u for u in members if u["role"] == "super_admin" and u["status"] == "active"]
            pms  = [u for u in members if u["role"] == "pm"          and u["status"] == "active"]
            mems = [u for u in members if u["role"] == "member"      and u["status"] == "active"]
            pend = [u for u in members if u["status"] == "pending"]
            if adm:
                lines.append("**管理员：** " + "、".join(u["name"] or "(未知)" for u in adm))
            if pms:
                lines.append("**项目经理：** " + "、".join(u["name"] or "(未知)" for u in pms))
            if mems:
                lines.append("**成员：** " + "、".join(u["name"] or "(未知)" for u in mems))
            if pend:
                lines.append("**待审批：** " + "、".join(f"⏳{u['name'] or '(未知)'}" for u in pend))
        return build_md_card("\n".join(lines), title=f"{project} 项目", color="green")

    # ── 普通成员视图 ──────────────────────────────────────────
    lines = [
        "## 我的信息",
        f"**姓名：** {name}",
        f"**角色：** {role_zh}",
        f"**项目：** {project}",
        f"**加入：** {joined}",
    ]
    return build_md_card("\n".join(lines), title="个人信息", color="blue")
