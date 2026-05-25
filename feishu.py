import json
import time
import httpx

_token_cache: dict = {"value": None, "expires_at": 0}

FEISHU_BASE = "https://open.feishu.cn/open-apis"

_TYPE_LABELS = {
    "risk": "风险",
    "milestone": "里程碑",
    "decision": "决策",
    "team": "人员",
    "client": "客户信息",
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
    token = await get_tenant_token(app_id, app_secret)
    chunks = _split(text, 4000)
    async with httpx.AsyncClient() as client:
        for chunk in chunks:
            await client.post(
                f"{FEISHU_BASE}/im/v1/messages",
                headers={"Authorization": f"Bearer {token}"},
                params={"receive_id_type": "open_id"},
                json={
                    "receive_id": open_id,
                    "msg_type": "text",
                    "content": json.dumps({"text": chunk}),
                },
            )


async def send_text(chat_id: str, text: str, app_id: str, app_secret: str):
    token = await get_tenant_token(app_id, app_secret)
    chunks = _split(text, 4000)
    async with httpx.AsyncClient() as client:
        for chunk in chunks:
            await client.post(
                f"{FEISHU_BASE}/im/v1/messages",
                headers={"Authorization": f"Bearer {token}"},
                params={"receive_id_type": "chat_id"},
                json={
                    "receive_id": chat_id,
                    "msg_type": "text",
                    "content": json.dumps({"text": chunk}),
                },
            )


async def send_confirm_card(chat_id: str, items: list[dict], app_id: str, app_secret: str):
    token = await get_tenant_token(app_id, app_secret)
    card = _build_confirm_card(items, chat_id)
    async with httpx.AsyncClient() as client:
        await client.post(
            f"{FEISHU_BASE}/im/v1/messages",
            headers={"Authorization": f"Bearer {token}"},
            params={"receive_id_type": "chat_id"},
            json={
                "receive_id": chat_id,
                "msg_type": "interactive",
                "content": json.dumps(card),
            },
        )


def _build_confirm_card(items: list[dict], chat_id: str) -> dict:
    elements = []
    for i, item in enumerate(items[:10]):
        label = _TYPE_LABELS.get(item["type"], item["type"])
        display = item["content"] if len(item["content"]) <= 60 else item["content"][:57] + "…"
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
                        "text": {"tag": "lark_md", "content": f"**{i + 1}. [{label}]** {display}"},
                    }],
                },
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [{
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "保存"},
                        "type": "primary",
                        "value": {"action": "save_one", "chat_id": chat_id, "index": i},
                    }],
                },
            ],
        })
    elements.extend([
        {"tag": "hr"},
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "✓ 全部保存"},
                    "type": "primary",
                    "value": {"action": "save_all", "chat_id": chat_id},
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
            "title": {"tag": "plain_text", "content": "💡 发现可记录信息"},
            "template": "blue",
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
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"✅ 已保存 {count} 条信息，后续回答将参考。",
                        },
                    }
                ],
            },
        },
    }


def card_one_saved_response(saved: dict, remaining: list[dict], chat_id: str) -> dict:
    label = _TYPE_LABELS.get(saved["type"], saved["type"])
    updated = _build_confirm_card(remaining, chat_id)
    return {
        "toast": {"type": "success", "content": f"已保存：{label}"},
        "card": {"type": "raw", "data": updated},
    }


def card_skipped_response() -> dict:
    return {
        "toast": {"type": "info", "content": "已跳过"},
        "card": {
            "type": "raw",
            "data": {
                "config": {"enable_forward": False},
                "elements": [
                    {
                        "tag": "div",
                        "text": {"tag": "lark_md", "content": "⏭ 已跳过。"},
                    }
                ],
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
