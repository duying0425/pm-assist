from __future__ import annotations

import json
import re as _re
import time
import httpx

_token_cache: dict = {"value": None, "expires_at": 0}

FEISHU_BASE = "https://open.feishu.cn/open-apis"


def _strip_md(text: str) -> str:
    """Remove markdown syntax that Feishu plain-text messages don't render."""
    # Code blocks first (avoid inner-block conflicts)
    text = _re.sub(r'```[\w]*\n?(.*?)```', r'\1', text, flags=_re.DOTALL)
    # Headers: ## Title → Title
    text = _re.sub(r'^#{1,6}\s+', '', text, flags=_re.MULTILINE)
    # Bold: **text** or __text__
    text = _re.sub(r'\*\*([^*\n]+)\*\*', r'\1', text)
    text = _re.sub(r'__([^_\n]+)__', r'\1', text)
    # Italic: *text* or _text_ (guard against list bullets and underscores in identifiers)
    text = _re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)', r'\1', text)
    # Inline code: `code`
    text = _re.sub(r'`([^`\n]+)`', r'\1', text)
    return text

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
    text = _strip_md(text)
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
    text = _strip_md(text)
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
                    "elements": [{
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**{i + 1}. [{label}]** {display}\n{note}",
                        },
                    }],
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
    return {
        "config": {"wide_screen_mode": True, "enable_forward": False},
        "header": {
            "title": {"tag": "plain_text", "content": title_text},
            "template": header_color,
        },
        "elements": elements,
    }


def card_saved_response(count: int) -> dict:
    return {
        "toast": {"type": "success", "content": f"已保存 {count} 条"},
        "card": {
            "type": "raw",
            "data": {
                "config": {"enable_forward": False},
                "elements": [{
                    "tag": "div",
                    "text": {"tag": "lark_md",
                             "content": f"✅ 已保存 {count} 条，后续回答将参考。"},
                }],
            },
        },
    }


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
    return {
        "toast": {"type": "info", "content": "已跳过"},
        "card": {
            "type": "raw",
            "data": {
                "config": {"enable_forward": False},
                "elements": [{
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": "⏭ 已跳过。"},
                }],
            },
        },
    }


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


def build_approval_card(open_id: str, name: str, role: str, project: str) -> dict:
    """构建用户注册审批卡片（发给管理员）。"""
    _ROLE_ZH = {"pm": "项目经理 PM", "member": "普通成员"}
    role_zh = _ROLE_ZH.get(role, role)
    return {
        "config": {"wide_screen_mode": False, "enable_forward": False},
        "header": {
            "title": {"tag": "plain_text", "content": "📋 新用户注册申请"},
            "template": "blue",
        },
        "elements": [
            {
                "tag": "div",
                "fields": [
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**申请人**\n{name}"}},
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**申请角色**\n{role_zh}"}},
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**申请项目**\n{project}"}},
                    {"is_short": True, "text": {"tag": "lark_md",
                                                "content": f"**open_id**\n`{open_id}`"}},
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
        ],
    }


def card_approved_response(name: str, role: str, project: str) -> dict:
    _ROLE_ZH = {"pm": "项目经理 PM", "member": "普通成员"}
    return {
        "toast": {"type": "success", "content": f"已批准 {name}"},
        "card": {
            "type": "raw",
            "data": {
                "config": {"enable_forward": False},
                "elements": [{
                    "tag": "div",
                    "text": {"tag": "lark_md",
                             "content": f"✅ 已批准 **{name}** 以「{_ROLE_ZH.get(role, role)}」身份加入「{project}」"},
                }],
            },
        },
    }


def card_rejected_response(name: str) -> dict:
    return {
        "toast": {"type": "info", "content": f"已拒绝 {name}"},
        "card": {
            "type": "raw",
            "data": {
                "config": {"enable_forward": False},
                "elements": [{
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": f"❌ 已拒绝 **{name}** 的申请"},
                }],
            },
        },
    }


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
    text = _strip_md(text[:4000])
    token = await get_tenant_token(app_id, app_secret)
    async with httpx.AsyncClient() as client:
        resp = await client.patch(
            f"{FEISHU_BASE}/im/v1/messages/{message_id}/body",
            headers={"Authorization": f"Bearer {token}"},
            json={"content": json.dumps({"text": text})},
        )
        return resp.status_code == 200


# ── Todo 确认卡片 ──────────────────────────────────────────

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
                    "elements": [{
                        "tag": "div",
                        "text": {"tag": "lark_md",
                                 "content": f"**{i + 1}. {title}**\n{meta}"},
                    }],
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
    return {
        "config": {"wide_screen_mode": True, "enable_forward": False},
        "header": {
            "title": {"tag": "plain_text", "content": title_text},
            "template": "blue" if saved_count == 0 else "green",
        },
        "elements": elements,
    }


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
    return {
        "toast": {"type": "success", "content": f"已新增 {count} 条待办"},
        "card": {
            "type": "raw",
            "data": {
                "config": {"enable_forward": False},
                "elements": [{
                    "tag": "div",
                    "text": {"tag": "lark_md",
                             "content": f"✅ 已新增 {count} 条待办。用 /todo list 查看。"},
                }],
            },
        },
    }


def card_todo_one_saved_response(title: str, remaining: list[dict],
                                  chat_id: str, saved_count: int) -> dict:
    updated = build_todo_confirm_card(remaining, chat_id, saved_count)
    return {
        "toast": {"type": "success", "content": f"已新增：{title[:20]}"},
        "card": {"type": "raw", "data": updated},
    }


def card_todo_skipped_response() -> dict:
    return {
        "toast": {"type": "info", "content": "已跳过"},
        "card": {
            "type": "raw",
            "data": {
                "config": {"enable_forward": False},
                "elements": [{
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": "⏭ 已跳过待办建议。"},
                }],
            },
        },
    }


# ── Merge 确认卡片 ─────────────────────────────────────────

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
                    "elements": [{
                        "tag": "div",
                        "text": {"tag": "lark_md",
                                 "content": (
                                     f"**{i + 1}. 合并到 #{keep_id}**\n"
                                     f"合入：{merge_text}\n"
                                     f"原因：{reason}\n"
                                     f"追加：{append_preview}"
                                 )},
                    }],
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
    return {
        "config": {"wide_screen_mode": True, "enable_forward": False},
        "header": {
            "title": {"tag": "plain_text", "content": title_text},
            "template": "blue" if saved_count == 0 else "green",
        },
        "elements": elements,
    }


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
    return {
        "toast": {"type": "success", "content": f"已合并 {count} 组信息"},
        "card": {
            "type": "raw",
            "data": {
                "config": {"enable_forward": False},
                "elements": [{
                    "tag": "div",
                    "text": {"tag": "lark_md",
                             "content": f"✅ 已合并 {count} 组信息。"},
                }],
            },
        },
    }


def card_merge_one_saved_response(item: dict, remaining: list[dict],
                                  chat_id: str, saved_count: int) -> dict:
    updated = build_merge_confirm_card(remaining, chat_id, saved_count)
    return {
        "toast": {"type": "success", "content": f"已合并到 #{item.get('keep_id')}"},
        "card": {"type": "raw", "data": updated},
    }


def card_merge_skipped_response() -> dict:
    return {
        "toast": {"type": "info", "content": "已跳过"},
        "card": {
            "type": "raw",
            "data": {
                "config": {"enable_forward": False},
                "elements": [{
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": "⏭ 已跳过合并建议。"},
                }],
            },
        },
    }


# ── Risk/Todo 清洗动作确认卡片 ──────────────────────────────

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

        elements.append({
            "tag": "column_set",
            "flex_mode": "none",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 5,
                    "elements": [{
                        "tag": "div",
                        "text": {"tag": "lark_md",
                                 "content": (
                                     f"**{i + 1}. {label} {prefix}{item_id}** {title}\n"
                                     f"原因：{reason}"
                                 )},
                    }],
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
    return {
        "config": {"wide_screen_mode": True, "enable_forward": False},
        "header": {
            "title": {"tag": "plain_text", "content": title_text},
            "template": "blue" if saved_count == 0 else "green",
        },
        "elements": elements,
    }


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
    return {
        "toast": {"type": "success", "content": f"已处理 {count} 项"},
        "card": {
            "type": "raw",
            "data": {
                "config": {"enable_forward": False},
                "elements": [{
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": f"✅ 已处理 {count} 项风险/待办。"},
                }],
            },
        },
    }


def card_action_one_saved_response(item: dict, remaining: list[dict],
                                   chat_id: str, saved_count: int) -> dict:
    updated = build_action_confirm_card(remaining, chat_id, saved_count)
    return {
        "toast": {"type": "success", "content": f"已处理：{item.get('title', '')[:20]}"},
        "card": {"type": "raw", "data": updated},
    }


def card_action_skipped_response() -> dict:
    return {
        "toast": {"type": "info", "content": "已跳过"},
        "card": {
            "type": "raw",
            "data": {
                "config": {"enable_forward": False},
                "elements": [{
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": "⏭ 已跳过风险/待办处理建议。"},
                }],
            },
        },
    }


def _split(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts = []
    while text:
        parts.append(text[:limit])
        text = text[limit:]
    return parts
