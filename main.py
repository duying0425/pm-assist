import asyncio
import json
import logging
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

import claude_client
import db
import feishu
from config import (
    ADMIN_OPEN_IDS,
    FEISHU_APP_ID,
    FEISHU_APP_SECRET,
    FEISHU_VERIFICATION_TOKEN,
    MAX_HISTORY,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

_TYPE_LABELS = {
    "risk": "风险",
    "milestone": "里程碑",
    "decision": "决策",
    "team": "人员",
    "client": "客户信息",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    log.info("DB initialized")
    yield


app = FastAPI(lifespan=lifespan)


# ── Webhook 入口 ──────────────────────────────────────────

@app.post("/webhook/feishu")
async def feishu_webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.json()

    # 飞书 URL 验证
    if body.get("type") == "url_verification":
        if body.get("token") != FEISHU_VERIFICATION_TOKEN:
            return JSONResponse(status_code=401, content={"error": "invalid token"})
        log.info("URL verification ok")
        return {"challenge": body["challenge"]}

    # 卡片按钮回调（消息卡片请求网址触发，body 里有 action + open_message_id）
    if "action" in body and "open_message_id" in body:
        if body.get("token") != FEISHU_VERIFICATION_TOKEN:
            return JSONResponse(status_code=401, content={"error": "invalid token"})
        return await _handle_card_callback(body)

    # 普通事件
    header = body.get("header", {})
    if header.get("token") != FEISHU_VERIFICATION_TOKEN:
        log.warning("invalid token")
        return JSONResponse(status_code=401, content={"error": "invalid token"})

    event_type = header.get("event_type", "")
    event_id = header.get("event_id", "")
    log.info("event_type=%s event_id=%s", event_type, event_id)

    if event_id:
        if db.is_processed(event_id):
            return {"ok": True}
        db.mark_processed(event_id)

    if event_type == "im.message.receive_v1":
        background_tasks.add_task(handle_message, body.get("event", {}))
        return {"ok": True}

    # 卡片按钮点击（card.action.trigger，需在3秒内同步返回卡片更新）
    if event_type == "card.action.trigger":
        return await _handle_card_trigger(body.get("event", {}))

    return {"ok": True}


# ── 卡片按钮回调（消息卡片请求网址触发，支持返回卡片更新） ──

async def _handle_card_callback(body: dict) -> dict:
    value = body.get("action", {}).get("value", {})
    action = value.get("action")
    chat_id = value.get("chat_id", "")
    log.info("card callback action=%s chat_id=%s", action, chat_id)

    if action == "save_one":
        index = int(value.get("index", -1))
        saved, remaining = db.pop_pending_item(chat_id, index)
        if saved:
            label = _TYPE_LABELS.get(saved["type"], saved["type"])
            db.add_block("note", f"[{label}] {saved['content'][:20]}", saved["content"])
            if remaining:
                return feishu.card_one_saved_response(saved, remaining, chat_id)
            return feishu.card_saved_response(1)
        return feishu.card_skipped_response()

    pending = db.get_pending(chat_id)
    if action == "save_all" and pending:
        for item in pending:
            label = _TYPE_LABELS.get(item["type"], item["type"])
            db.add_block("note", f"[{label}] {item['content'][:20]}", item["content"])
        db.clear_pending(chat_id)
        return feishu.card_saved_response(len(pending))
    db.clear_pending(chat_id)
    return feishu.card_skipped_response()


async def _handle_card_trigger(event: dict) -> dict:
    try:
        value = event.get("action", {}).get("value", {})
        action = value.get("action")
        chat_id = value.get("chat_id", "")
        log.info("card trigger action=%s chat_id=%s", action, chat_id)

        if action == "save_one":
            index = int(value.get("index", -1))
            saved, remaining = db.pop_pending_item(chat_id, index)
            if saved:
                label = _TYPE_LABELS.get(saved["type"], saved["type"])
                db.add_block("note", f"[{label}] {saved['content'][:20]}", saved["content"])
                log.info("saved item index=%d remaining=%d", index, len(remaining))
                if remaining:
                    return feishu.card_one_saved_response(saved, remaining, chat_id)
                return feishu.card_saved_response(1)
            return feishu.card_skipped_response()

        pending = db.get_pending(chat_id)
        if action == "save_all" and pending:
            for item in pending:
                label = _TYPE_LABELS.get(item["type"], item["type"])
                db.add_block("note", f"[{label}] {item['content'][:20]}", item["content"])
            db.clear_pending(chat_id)
            log.info("saved %d items", len(pending))
            return feishu.card_saved_response(len(pending))
        db.clear_pending(chat_id)
        return feishu.card_skipped_response()
    except Exception:
        log.exception("handle_card_trigger error")
        return feishu.card_skipped_response()


# ── 消息处理 ──────────────────────────────────────────────

async def handle_message(event: dict):
    try:
        await _handle_message(event)
    except Exception:
        log.exception("handle_message error")


async def _handle_message(event: dict):
    message = event.get("message", {})
    sender = event.get("sender", {})

    msg_type = message.get("message_type", "")
    chat_id = message.get("chat_id", "")
    sender_open_id = sender.get("sender_id", {}).get("open_id", "")

    log.info("chat_id=%s sender=%s msg_type=%s", chat_id, sender_open_id, msg_type)

    if msg_type != "text":
        return

    raw = json.loads(message.get("content", "{}"))
    text = raw.get("text", "").strip()

    for mention in message.get("mentions", []):
        text = text.replace(mention.get("key", ""), "").strip()

    log.info("text: %r", text)
    if not text:
        return

    # ── /help ────────────────────────────────────────────
    if text == "/help":
        await feishu.send_text(chat_id, _help_text(), FEISHU_APP_ID, FEISHU_APP_SECRET)
        return

    # ── 管理员命令 ───────────────────────────────────────
    if text.startswith("/admin") and sender_open_id in ADMIN_OPEN_IDS:
        reply = _handle_admin(text)
        await feishu.send_text(chat_id, reply, FEISHU_APP_ID, FEISHU_APP_SECRET)
        return

    # ── /note 直接存储 ───────────────────────────────────
    if text.startswith("/note "):
        note = text[6:].strip()
        if note:
            bid = db.add_block("note", f"笔记#{db.count_notes() + 1}", note)
            await feishu.send_text(chat_id, f"✓ 已记录 (ID:{bid})", FEISHU_APP_ID, FEISHU_APP_SECRET)
        return

    # ── /clear ───────────────────────────────────────────
    if text == "/clear":
        db.clear_history(chat_id)
        db.clear_pending(chat_id)
        await feishu.send_text(chat_id, "对话历史已清除。", FEISHU_APP_ID, FEISHU_APP_SECRET)
        return

    # 有新话题时清掉旧 pending（用户已忽略上次卡片）
    db.clear_pending(chat_id)

    # ── 正常 AI 对话 ──────────────────────────────────────
    db.add_message(chat_id, "user", text)
    history = db.get_history(chat_id, MAX_HISTORY)
    knowledge = db.get_knowledge_text()

    risks = db.get_risks_text()
    reply = await claude_client.chat(history, knowledge, risks)
    db.add_message(chat_id, "assistant", reply)
    await feishu.send_text(chat_id, reply, FEISHU_APP_ID, FEISHU_APP_SECRET)

    # 后台提取关键信息，不阻塞主回复
    asyncio.create_task(_extract_and_card(chat_id, text))


async def _extract_and_card(chat_id: str, text: str):
    try:
        items = await claude_client.extract_facts(text)
        if not items:
            return
        db.save_pending(chat_id, items)
        await feishu.send_confirm_card(chat_id, items, FEISHU_APP_ID, FEISHU_APP_SECRET)
    except Exception:
        log.exception("extract_and_card error")


# ── 管理员命令 ────────────────────────────────────────────

def _handle_admin(text: str) -> str:
    parts = text.split(None, 4)
    cmd = parts[1].lower() if len(parts) > 1 else ""

    if cmd == "list":
        blocks = db.list_blocks()
        if not blocks:
            return "暂无知识块"
        lines = [
            f"ID:{b['id']} [{b['category']}] {b['title']} {'✓' if b['enabled'] else '✗'}"
            for b in blocks
        ]
        return "\n".join(lines)

    if cmd == "add" and len(parts) >= 5:
        _, _, category, title, content = text.split(None, 4)
        bid = db.add_block(category, title, content)
        return f"✓ 已添加知识块 ID:{bid}"

    if cmd == "update" and len(parts) >= 4:
        try:
            bid = int(parts[2])
        except ValueError:
            return "ID 必须是数字"
        content = parts[3] if len(parts) > 3 else ""
        db.update_block(bid, content)
        return f"✓ 已更新知识块 ID:{bid}"

    if cmd == "disable" and len(parts) >= 3:
        try:
            bid = int(parts[2])
        except ValueError:
            return "ID 必须是数字"
        db.toggle_block(bid, False)
        return f"✓ 已禁用知识块 ID:{bid}"

    if cmd == "enable" and len(parts) >= 3:
        try:
            bid = int(parts[2])
        except ValueError:
            return "ID 必须是数字"
        db.toggle_block(bid, True)
        return f"✓ 已启用知识块 ID:{bid}"

    if cmd == "delete" and len(parts) >= 3:
        try:
            bid = int(parts[2])
        except ValueError:
            return "ID 必须是数字"
        db.delete_block(bid)
        return f"✓ 已删除知识块 ID:{bid}"

    if cmd == "risk":
        return _handle_admin_risk(parts[2:] if len(parts) > 2 else [])

    return _admin_help()


def _handle_admin_risk(args: list[str]) -> str:
    sub = args[0].lower() if args else ""

    if sub == "list":
        filter_status = args[1] if len(args) > 1 else "open"
        if filter_status == "all":
            rows = db.list_risks(status=None)
        else:
            rows = db.list_risks(status=filter_status)
        if not rows:
            return f"无{filter_status}状态的风险/问题"
        _PRIO = {"high": "高", "medium": "中", "low": "低"}
        _TYPE = {"risk": "风险", "issue": "问题", "blocker": "阻塞", "dependency": "依赖"}
        lines = [
            f"R{r['id']} [{_TYPE.get(r['type'], r['type'])}·{_PRIO.get(r['priority'], r['priority'])}·{r['status']}] "
            f"{r['title']}" + (f"（{r['owner']}）" if r['owner'] else "")
            for r in rows
        ]
        return "\n".join(lines)

    if sub == "close" and len(args) >= 2:
        try:
            rid = int(args[1])
        except ValueError:
            return "ID 必须是数字"
        db.update_risk(rid, status="closed")
        return f"✓ 已关闭风险 R{rid}"

    if sub == "reopen" and len(args) >= 2:
        try:
            rid = int(args[1])
        except ValueError:
            return "ID 必须是数字"
        db.update_risk(rid, status="open")
        return f"✓ 已重新打开 R{rid}"

    if sub == "owner" and len(args) >= 3:
        try:
            rid = int(args[1])
        except ValueError:
            return "ID 必须是数字"
        owner = " ".join(args[2:])
        db.update_risk(rid, owner=owner)
        return f"✓ 已设置 R{rid} 负责人为 {owner}"

    return ("风险管理命令：\n"
            "/admin risk list [open|all]   列出风险（默认只看open）\n"
            "/admin risk close [ID]        关闭风险\n"
            "/admin risk reopen [ID]       重新打开\n"
            "/admin risk owner [ID] [姓名]  指定负责人")


def _help_text() -> str:
    return """📖 PM助手使用说明

所有人可用：
  @Bot [消息]   AI对话（结合知识库和风险清单）
  /note [内容]  快速记录一条笔记
  /clear        清除当前会话历史
  /help         显示本说明

管理员命令 — 知识库：
  /admin list                         列出所有知识块
  /admin add [分类] [标题] [内容]      新增知识块
  /admin update [ID] [新内容]          更新内容
  /admin enable/disable/delete [ID]

管理员命令 — 风险与问题：
  /admin risk list             查看未关闭风险（默认）
  /admin risk list all         查看全部（含已关闭）
  /admin risk close [ID]       关闭风险
  /admin risk reopen [ID]      重新打开
  /admin risk owner [ID] [姓名] 指定负责人

风险ID格式：R1、R2… 填数字即可，如 /admin risk close 3"""


def _admin_help() -> str:
    return """管理员命令：
/admin list                        列出所有知识块
/admin add [分类] [标题] [内容]     添加知识块
/admin update [ID] [新内容]         更新知识块内容
/admin enable [ID]                  启用知识块
/admin disable [ID]                 禁用知识块
/admin delete [ID]                  删除知识块
/admin risk list [open|all]         列出风险/问题
/admin risk close [ID]              关闭风险
/admin risk owner [ID] [姓名]       指定负责人

所有人可用：
/note [内容]    直接存入一条记录
/clear          清除当前会话历史"""
