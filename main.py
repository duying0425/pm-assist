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
    "risk": "风险", "issue": "问题", "blocker": "阻塞", "dependency": "依赖",
    "milestone": "里程碑", "decision": "决策", "team": "人员",
    "client": "客户", "org": "组织", "process": "流程", "knowledge": "知识",
}
_PRIO_LABELS = {"high": "高", "medium": "中", "low": "低"}
_STATUS_LABELS = {"active": "open", "resolved": "resolved", "archived": "archived"}


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

    if body.get("type") == "url_verification":
        if body.get("token") != FEISHU_VERIFICATION_TOKEN:
            return JSONResponse(status_code=401, content={"error": "invalid token"})
        log.info("URL verification ok")
        return {"challenge": body["challenge"]}

    if "action" in body and "open_message_id" in body:
        if body.get("token") != FEISHU_VERIFICATION_TOKEN:
            return JSONResponse(status_code=401, content={"error": "invalid token"})
        return await _handle_card_callback(body)

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

    if event_type == "card.action.trigger":
        return await _handle_card_trigger(body.get("event", {}))

    return {"ok": True}


# ── 卡片回调 ──────────────────────────────────────────────

def _save_fact_item(item: dict):
    """统一保存逻辑：新增或追加更新到已有条目。"""
    action = item.get("action", "new")
    if action == "update" and item.get("fact_id"):
        db.append_to_fact(item["fact_id"], item["content"])
    else:
        label = _TYPE_LABELS.get(item["type"], item["type"])
        db.add_fact(
            item["type"],
            f"[{label}] {item['content'][:20]}",
            item["content"],
            source="ai",
        )


async def _handle_card_callback(body: dict) -> dict:
    value = body.get("action", {}).get("value", {})
    action = value.get("action")
    chat_id = value.get("chat_id", "")
    log.info("card callback action=%s chat_id=%s", action, chat_id)

    if action == "save_one":
        index = int(value.get("index", -1))
        saved, remaining = db.pop_pending_item(chat_id, index)
        if saved:
            _save_fact_item(saved)
            if remaining:
                return feishu.card_one_saved_response(saved, remaining, chat_id)
            return feishu.card_saved_response(1)
        return feishu.card_skipped_response()

    pending = db.get_pending(chat_id)
    if action == "save_all" and pending:
        for item in pending:
            _save_fact_item(item)
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
                _save_fact_item(saved)
                log.info("saved item index=%d remaining=%d", index, len(remaining))
                if remaining:
                    return feishu.card_one_saved_response(saved, remaining, chat_id)
                return feishu.card_saved_response(1)
            return feishu.card_skipped_response()

        pending = db.get_pending(chat_id)
        if action == "save_all" and pending:
            for item in pending:
                _save_fact_item(item)
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

    if text == "/help":
        await feishu.send_text(chat_id, _help_text(), FEISHU_APP_ID, FEISHU_APP_SECRET)
        return

    if text.startswith("/admin") and sender_open_id in ADMIN_OPEN_IDS:
        reply = _handle_admin(text)
        await feishu.send_text(chat_id, reply, FEISHU_APP_ID, FEISHU_APP_SECRET)
        return

    if text.startswith("/note "):
        note = text[6:].strip()
        if note:
            bid = db.add_fact("knowledge", f"笔记#{db.count_notes() + 1}", note, source="manual")
            await feishu.send_text(chat_id, f"✓ 已记录 (ID:{bid})", FEISHU_APP_ID, FEISHU_APP_SECRET)
        return

    if text == "/clear":
        db.clear_history(chat_id)
        db.clear_pending(chat_id)
        await feishu.send_text(chat_id, "对话历史已清除。", FEISHU_APP_ID, FEISHU_APP_SECRET)
        return

    db.clear_pending(chat_id)

    db.add_message(chat_id, "user", text)
    history = db.get_history(chat_id, MAX_HISTORY)
    knowledge = db.get_knowledge_text()
    risks = db.get_risks_text()
    reply = await claude_client.chat(history, knowledge, risks)
    db.add_message(chat_id, "assistant", reply)
    await feishu.send_text(chat_id, reply, FEISHU_APP_ID, FEISHU_APP_SECRET)

    asyncio.create_task(_extract_and_card(chat_id, text))


async def _extract_and_card(chat_id: str, text: str):
    try:
        items = await claude_client.extract_facts(text)
        if not items:
            return
        enriched = []
        for item in items:
            similar = db.find_similar_fact(item["type"], item["content"])
            if similar:
                enriched.append({
                    **item,
                    "action": "update",
                    "fact_id": similar["id"],
                    "fact_title": similar["title"],
                })
            else:
                enriched.append({**item, "action": "new"})
        db.save_pending(chat_id, enriched)
        await feishu.send_confirm_card(chat_id, enriched, FEISHU_APP_ID, FEISHU_APP_SECRET)
    except Exception:
        log.exception("extract_and_card error")


# ── 管理员命令 ────────────────────────────────────────────

def _handle_admin(text: str) -> str:
    parts = text.split(None, 4)
    cmd = parts[1].lower() if len(parts) > 1 else ""

    if cmd == "list":
        blocks = db.list_blocks()
        if not blocks:
            return "暂无条目"
        lines = [
            f"#{b['id']} [{b['category']}] {b['title']}"
            f" {'✓' if b['enabled'] else '✗'} ({b['updated_at'][:10]})"
            for b in blocks
        ]
        return "\n".join(lines)

    if cmd == "add" and len(parts) >= 5:
        _, _, category, title, content = text.split(None, 4)
        bid = db.add_block(category, title, content)
        return f"✓ 已添加 ID:{bid}"

    if cmd == "update" and len(parts) >= 4:
        try:
            bid = int(parts[2])
        except ValueError:
            return "ID 必须是数字"
        content = parts[3] if len(parts) > 3 else ""
        db.update_block(bid, content)
        return f"✓ 已更新 ID:{bid}"

    if cmd == "disable" and len(parts) >= 3:
        try:
            bid = int(parts[2])
        except ValueError:
            return "ID 必须是数字"
        db.toggle_block(bid, False)
        return f"✓ 已禁用 ID:{bid}"

    if cmd == "enable" and len(parts) >= 3:
        try:
            bid = int(parts[2])
        except ValueError:
            return "ID 必须是数字"
        db.toggle_block(bid, True)
        return f"✓ 已启用 ID:{bid}"

    if cmd == "delete" and len(parts) >= 3:
        try:
            bid = int(parts[2])
        except ValueError:
            return "ID 必须是数字"
        db.delete_block(bid)
        return f"✓ 已删除 ID:{bid}"

    if cmd == "risk":
        return _handle_admin_risk(parts[2:] if len(parts) > 2 else [])

    if cmd == "fact":
        return _handle_admin_fact(parts[2:] if len(parts) > 2 else [])

    return _admin_help()


def _handle_admin_risk(args: list[str]) -> str:
    sub = args[0].lower() if args else ""

    if sub == "list":
        filter_status = args[1] if len(args) > 1 else "open"
        rows = db.list_risks(status=None if filter_status == "all" else filter_status)
        if not rows:
            return f"无{filter_status}状态的风险/问题"
        lines = [
            f"#{r['id']} [{_TYPE_LABELS.get(r['type'], r['type'])}"
            f"·{_PRIO_LABELS.get(r['priority'], r['priority'])}"
            f"·{_STATUS_LABELS.get(r['status'], r['status'])}]"
            f" {r['title']}" + (f"（{r['owner']}）" if r['owner'] else "")
            for r in rows
        ]
        return "\n".join(lines)

    if sub == "close" and len(args) >= 2:
        try:
            rid = int(args[1])
        except ValueError:
            return "ID 必须是数字"
        db.update_risk(rid, status="closed")
        return f"✓ 已关闭 #{rid}"

    if sub == "reopen" and len(args) >= 2:
        try:
            rid = int(args[1])
        except ValueError:
            return "ID 必须是数字"
        db.update_risk(rid, status="open")
        return f"✓ 已重新打开 #{rid}"

    if sub == "owner" and len(args) >= 3:
        try:
            rid = int(args[1])
        except ValueError:
            return "ID 必须是数字"
        owner = " ".join(args[2:])
        db.update_risk(rid, owner=owner)
        return f"✓ 已设置 #{rid} 负责人为 {owner}"

    if sub == "add" and len(args) >= 4:
        # /admin risk add [type] [priority] [title] | [description]
        type_ = args[1] if args[1] in ("risk", "issue", "blocker", "dependency") else "risk"
        priority = args[2] if args[2] in ("high", "medium", "low") else "medium"
        rest = " ".join(args[3:])
        if "|" in rest:
            title, desc = rest.split("|", 1)
            title, desc = title.strip(), desc.strip()
        else:
            title, desc = rest.strip(), rest.strip()
        rid = db.add_risk(type_, title, desc, priority=priority)
        return f"✓ 已新增 #{rid} [{type_}·{priority}] {title}"

    return (
        "风险命令：\n"
        "/admin risk list [open|all]\n"
        "/admin risk close [ID]\n"
        "/admin risk reopen [ID]\n"
        "/admin risk owner [ID] [姓名]\n"
        "/admin risk add [type] [priority] [标题] | [描述]\n"
        "  type: risk|issue|blocker|dependency\n"
        "  priority: high|medium|low"
    )


def _handle_admin_fact(args: list[str]) -> str:
    sub = args[0].lower() if args else ""

    if sub == "list":
        type_filter = args[1] if len(args) > 1 and args[1] != "all" else None
        status_filter = args[2] if len(args) > 2 else "active"
        if len(args) > 1 and args[1] == "all":
            status_filter = None
        rows = db.list_facts(type_=type_filter, status=status_filter)
        if not rows:
            return "无匹配条目"
        lines = []
        for r in rows:
            label = _TYPE_LABELS.get(r["type"], r["type"])
            status = _STATUS_LABELS.get(r["status"], r["status"])
            prio = f"·{_PRIO_LABELS[r['priority']]}" if r["priority"] in _PRIO_LABELS else ""
            owner = f"（{r['owner']}）" if r["owner"] else ""
            date = r["updated_at"][:10]
            lines.append(f"#{r['id']} [{label}{prio}·{status}] {r['title']}{owner} [{date}]")
        return "\n".join(lines)

    if sub == "show" and len(args) >= 2:
        try:
            fid = int(args[1])
        except ValueError:
            return "ID 必须是数字"
        row = db.get_fact(fid)
        if not row:
            return f"找不到 #{fid}"
        label = _TYPE_LABELS.get(row["type"], row["type"])
        status = _STATUS_LABELS.get(row["status"], row["status"])
        lines = [
            f"#{row['id']} [{label}·{status}] {row['title']}",
            f"优先级：{_PRIO_LABELS.get(row['priority'], row['priority'] or '—')}",
            f"负责人：{row['owner'] or '—'}",
            f"截止：{row['due_date'] or '—'}",
            f"来源：{row['source']}",
            f"创建：{row['created_at'][:16]}  更新：{row['updated_at'][:16]}",
            "---",
            row["body"],
        ]
        return "\n".join(lines)

    if sub == "update" and len(args) >= 4:
        try:
            fid = int(args[1])
        except ValueError:
            return "ID 必须是数字"
        field = args[2].lower()
        value = " ".join(args[3:])
        allowed = {"status", "owner", "priority", "due_date", "title", "body"}
        if field not in allowed:
            return f"可更新字段：{', '.join(sorted(allowed))}"
        # 对 status 做用户友好映射
        if field == "status":
            value = {"open": "active", "closed": "resolved"}.get(value, value)
        db.update_fact(fid, **{field: value})
        return f"✓ 已更新 #{fid}.{field} = {value}"

    if sub == "archive" and len(args) >= 2:
        try:
            fid = int(args[1])
        except ValueError:
            return "ID 必须是数字"
        db.update_fact(fid, status="archived")
        return f"✓ 已归档 #{fid}"

    if sub == "delete" and len(args) >= 2:
        try:
            fid = int(args[1])
        except ValueError:
            return "ID 必须是数字"
        db.delete_fact(fid)
        return f"✓ 已删除 #{fid}"

    if sub == "add" and len(args) >= 3:
        type_ = args[1]
        rest = " ".join(args[2:])
        if "|" in rest:
            title, body = rest.split("|", 1)
            title, body = title.strip(), body.strip()
        else:
            title, body = rest.strip(), rest.strip()
        fid = db.add_fact(type_, title, body, source="manual")
        return f"✓ 已新增 #{fid} [{type_}] {title}"

    return (
        "fact 命令：\n"
        "/admin fact list [type] [active|archived|all]  列出条目\n"
        "/admin fact list all                           列出全部\n"
        "/admin fact show [ID]                          查看完整内容\n"
        "/admin fact update [ID] [field] [值]           更新字段\n"
        "  field: status|owner|priority|due_date|title|body\n"
        "  status: open|resolved|archived\n"
        "/admin fact archive [ID]                       归档\n"
        "/admin fact delete [ID]                        删除\n"
        "/admin fact add [type] [标题] | [正文]         新增\n"
        "  type: risk|issue|milestone|decision|team|client|knowledge|process|org"
    )


def _help_text() -> str:
    return """PM助手使用说明

所有人可用：
  @Bot [消息]   AI对话（结合知识库和风险清单）
  /note [内容]  快速记录一条笔记
  /clear        清除当前会话历史
  /help         显示本说明

管理员 — 知识库（旧接口）：
  /admin list
  /admin add [分类] [标题] [内容]
  /admin update/enable/disable/delete [ID]

管理员 — 风险（快捷）：
  /admin risk list [open|all]
  /admin risk close/reopen [ID]
  /admin risk owner [ID] [姓名]
  /admin risk add [type] [priority] [标题] | [描述]

管理员 — 统一信息管理：
  /admin fact list [type] [active|all]
  /admin fact show [ID]
  /admin fact update [ID] [field] [值]
  /admin fact archive/delete [ID]
  /admin fact add [type] [标题] | [正文]"""


def _admin_help() -> str:
    return (
        "管理员命令：\n"
        "/admin list/add/update/enable/disable/delete\n"
        "/admin risk list/close/reopen/owner/add\n"
        "/admin fact list/show/update/archive/delete/add"
    )
