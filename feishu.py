from __future__ import annotations

import json
import time
import httpx

_token_cache: dict = {"value": None, "expires_at": 0}

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


async def send_confirm_card(chat_id: str, items: list[dict], app_id: str, app_secret: str):
    token = await get_tenant_token(app_id, app_secret)
    card = _build_confirm_card(items, chat_id)
    async with httpx.AsyncClient() as client:
        await client.post(
            f"{FEISHU_BASE}/im/v1/messages",
            headers={"Authorization": f"Bearer {token}"},
            params={"receive_id_type": "chat_id"},
            json={"receive_id": chat_id, "msg_type": "interactive",
                  "content": json.dumps(card)},
        )


# ── 通用 lark_md / markdown 卡片 ──────────────────────────────

def build_md_card(text: str, title: str | None = None, color: str = "blue") -> dict:
    """把 markdown 文本包成飞书 interactive 卡片（schema 2.0，完整 Markdown 渲染）。"""
    elements = [_md(text)]
    return _card(elements, header=_header(title, color) if title else None)


def build_thinking_card() -> dict:
    return build_md_card("⏳ 思考中...")


# ── AI 信息提取确认卡片（知识库条目）──────────────────────────

def _build_confirm_card(items: list[dict], chat_id: str, saved_count: int = 0) -> dict:
    has_update = any(item.get("action") == "update" for item in items)
    elements = []
    for i, item in enumerate(items[:10]):
        label = _TYPE_LABELS.get(item["type"], item["type"])
        content = item["content"]
        display = content if len(content) <= 55 else content[:52] + "…"

        action = item.get("action", "new")
        if action == "update":
            fact_id = item.get("fact_id", "")
            fact_title = item.get("fact_title", "")
            short_title = fact_title[:18] + "…" if len(fact_title) > 18 else fact_title
            note = f"*→ 追加到 #{fact_id}《{short_title}》*"
            btn_text = f"追加 #{fact_id}"
            btn_type = "default"
        else:
            note = "*→ 新增条目*"
            btn_text = "新增"
            btn_type = "primary"

        elements.append({
            "tag": "column_set",
            "flex_mode": "none",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 5,
                    "elements": [_lark_div(f"**{i + 1}. [{label}]** {display}\n{note}")],
                },
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [{
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": btn_text},
                        "type": btn_type,
                        "value": {
                            "action": "save_one",
                            "chat_id": chat_id,
                            "index": i,
                            "saved_count": saved_count,
                        },
                    }],
                },
            ],
        })

    if saved_count > 0:
        title_text = f"💡 已保存 {saved_count} 条，还剩 {len(items)} 条"
        header_color = "green"
    else:
        title_text = "💡 发现可更新/记录信息" if has_update else "💡 发现可记录信息"
        header_color = "blue"

    elements.extend([
        {"tag": "hr"},
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "✓ 全部保存"},
                    "type": "primary",
                    "value": {"action": "save_all", "chat_id": chat_id, "saved_count": saved_count},
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "✗ 跳过"},
                    "type": "danger",
                    "value": {"action": "skip", "chat_id": chat_id},
                },
            ],
        },
    ])
    return _card(elements, header=_header(title_text, header_color), forward=False)


def card_saved_response(count: int) -> dict:
    return _resp("success", f"已保存 {count} 条",
                 [_md(f"✅ 已保存 {count} 条，后续回答将参考。")])


def card_one_saved_response(saved: dict, remaining: list[dict], chat_id: str,
                             saved_count: int = 1) -> dict:
    label = _TYPE_LABELS.get(saved["type"], saved["type"])
    action = saved.get("action", "new")
    verb = f"已追加到 #{saved.get('fact_id')}" if action == "update" else "已新增"
    updated = _build_confirm_card(remaining, chat_id, saved_count)
    return {
        "toast": {"type": "success", "content": f"{verb}：[{label}]"},
        "card": {"type": "raw", "data": updated},
    }


def card_skipped_response() -> dict:
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
        {
            "tag": "action",
            "actions": [
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
            ],
        },
    ]
    return _card(elements, header=_header("📋 新用户注册申请", "blue"), wide=False, forward=False)


def card_approved_response(name: str, role: str, project: str) -> dict:
    _ROLE_ZH = {"pm": "项目经理 PM", "member": "普通成员"}
    return _resp("success", f"已批准 {name}",
                 [_md(f"✅ 已批准 **{name}** 以「{_ROLE_ZH.get(role, role)}」身份加入「{project}」")])


def card_rejected_response(name: str) -> dict:
    return _resp("info", f"已拒绝 {name}",
                 [_md(f"❌ 已拒绝 **{name}** 的申请")])


# ── 待办确认卡片 ──────────────────────────────────────────────

_PRIO_ZH = {"high": "高", "medium": "中", "low": "低"}


def build_todo_confirm_card(todos: list[dict], chat_id: str, saved_count: int = 0) -> dict:
    elements = []
    for i, todo in enumerate(todos[:10]):
        title = todo.get("title", "")
        due = todo.get("due_date", "")
        priority = todo.get("priority", "medium")
        owner = todo.get("owner", "")

        meta_parts = [f"优先级：{_PRIO_ZH.get(priority, priority)}"]
        if due:
            meta_parts.append(f"截止：{due}")
        if owner:
            meta_parts.append(f"负责人：{owner}")
        meta = "  ".join(meta_parts)

        elements.append({
            "tag": "column_set",
            "flex_mode": "none",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 5,
                    "elements": [_lark_div(f"**{i + 1}. {title}**\n{meta}")],
                },
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [{
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "新增"},
                        "type": "primary",
                        "value": {
                            "action": "save_todo_one",
                            "chat_id": chat_id,
                            "index": i,
                            "saved_count": saved_count,
                        },
                    }],
                },
            ],
        })

    title_text = (f"✅ 已新增 {saved_count} 条，还剩 {len(todos)} 条"
                  if saved_count > 0
                  else f"📋 建议新增 {len(todos)} 条待办")
    elements.extend([
        {"tag": "hr"},
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "✓ 全部新增"},
                    "type": "primary",
                    "value": {"action": "save_todo_all", "chat_id": chat_id,
                              "saved_count": saved_count},
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "✗ 跳过"},
                    "type": "danger",
                    "value": {"action": "skip_todos", "chat_id": chat_id},
                },
            ],
        },
    ])
    header_color = "green" if saved_count > 0 else "blue"
    return _card(elements, header=_header(title_text, header_color), forward=False)


async def send_todo_confirm_card(chat_id: str, todos: list[dict],
                                  app_id: str, app_secret: str):
    token = await get_tenant_token(app_id, app_secret)
    card = build_todo_confirm_card(todos, chat_id)
    async with httpx.AsyncClient() as client:
        await client.post(
            f"{FEISHU_BASE}/im/v1/messages",
            headers={"Authorization": f"Bearer {token}"},
            params={"receive_id_type": "chat_id"},
            json={"receive_id": chat_id, "msg_type": "interactive",
                  "content": json.dumps(card)},
        )


def card_todo_saved_response(count: int) -> dict:
    return _resp("success", f"已新增 {count} 条待办",
                 [_md(f"✅ 已新增 {count} 条待办。用 /todo list 查看。")])


def card_todo_one_saved_response(title: str, remaining: list[dict],
                                  chat_id: str, saved_count: int) -> dict:
    updated = build_todo_confirm_card(remaining, chat_id, saved_count)
    return {
        "toast": {"type": "success", "content": f"已新增：{title[:20]}"},
        "card": {"type": "raw", "data": updated},
    }


def card_todo_skipped_response() -> dict:
    return _resp("info", "已跳过", [_md("⏭ 已跳过待办建议。")])


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
        {
            "tag": "action",
            "actions": [
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
            ],
        },
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
        {
            "tag": "action",
            "actions": [
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
            ],
        },
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

    return _card(elements,
                 header=_header(f"#{fact['id']} {fact['title']}", "red"),
                 forward=False)


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

    return _card(elements,
                 header=_header(f"#T{todo['id']} {todo['title']}", "blue"),
                 forward=False)


def build_milestone_list_card(rows: list) -> dict:
    """/schedule list 结构化卡片。"""
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

        elements.append(_md(
            f"{status_icon} **#{r['id']}  {r['title']}**\n"
            f"{due_part}{owner_part}".strip()
        ))
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
        elements.append({"tag": "action", "actions": btn_actions})
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
        summary = review_text[:800] + "…（完整报告已存入系统）" if len(review_text) > 800 else review_text
        elements.append(_md(f"**🤖 AI洗盘摘要**\n{summary}"))

    header_title = f"📋 {today} {project_name}早报" if project_name else f"📋 {today} 综合早报"
    return _card(elements, header=_header(header_title, "blue"), forward=False)
