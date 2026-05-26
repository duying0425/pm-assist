import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

import claude_client
import db
import feishu
import notify as _notify
from config import (
    ADMIN_OPEN_IDS,
    FEISHU_APP_ID,
    FEISHU_APP_SECRET,
    FEISHU_VERIFICATION_TOKEN,
    MAX_HISTORY,
    PRIMARY_ADMIN_OPEN_ID,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

_VERSION = (Path(__file__).parent / "VERSION").read_text().strip()

_TYPE_LABELS = {
    "risk": "风险", "issue": "问题", "blocker": "阻塞", "dependency": "依赖",
    "milestone": "里程碑", "decision": "决策", "team": "人员",
    "client": "客户", "org": "组织", "process": "流程", "knowledge": "知识",
}
_DIM_LABELS = {
    "risk": "风险维度", "schedule": "进度维度", "decision": "决策维度",
    "resource": "资源维度", "stakeholder": "相关方维度", "scope": "范围维度", "system": "系统",
}
_PRIO_LABELS = {"high": "高", "medium": "中", "low": "低"}
_STATUS_LABELS = {"active": "open", "resolved": "resolved", "archived": "archived"}
_CONF_LABELS = {"universal": "铁律", "common": "通常", "assumed": "推测"}
_SCOPE_LABELS = {"dept": "部门", "project": "项目", "client": "客户", "global": "全局"}


_scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")


async def _run_nightly_review():
    """凌晨0:30：AI分析所有facts，清洗报告存入DB。"""
    try:
        facts_text = db.get_all_facts_for_review()
        if not facts_text:
            log.info("nightly review: no active facts to review")
            return
        report = await claude_client.nightly_review(facts_text)
        db.save_nightly_review(report)
        log.info("nightly review saved to DB")
    except Exception:
        log.exception("nightly review error")


async def _send_morning_report():
    """早上9:00：发送风险日报 + AI洗盘报告给所有收件人。
    收件人 = ADMIN_OPEN_IDS | NOTIFY_OPEN_IDS（与 notify.py 独立脚本逻辑一致）。
    注意：如果同时开了 crontab 跑 notify.py，主管理员会收到两次，二选一即可。
    """
    from config import NOTIFY_OPEN_IDS
    try:
        recipients = ADMIN_OPEN_IDS | NOTIFY_OPEN_IDS
        if not recipients:
            log.warning("no recipients configured (ADMIN_OPEN_IDS and NOTIFY_OPEN_IDS both empty)")
            return
        review = db.get_latest_nightly_review()
        if not review:
            log.warning("morning report: no nightly review found (00:30 job may not have run yet)")
        report = _notify.build_morning_report(review)
        for uid in recipients:
            await feishu.send_text_to_user(uid, report, FEISHU_APP_ID, FEISHU_APP_SECRET)
        log.info("morning report sent to %d recipients: %s", len(recipients), recipients)
    except Exception:
        log.exception("morning report error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    log.info("DB initialized")
    _scheduler.add_job(_run_nightly_review, "cron", hour=0, minute=30, id="nightly_review")
    _scheduler.add_job(_send_morning_report, "cron", hour=9, minute=0, id="morning_report")
    _scheduler.start()
    log.info("Scheduler started: nightly_review@00:30, morning_report@09:00 (Asia/Shanghai)")
    yield
    _scheduler.shutdown()


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
        saved_count = int(value.get("saved_count", 0))
        saved, remaining = db.pop_pending_item(chat_id, index)
        if saved:
            _save_fact_item(saved)
            saved_count += 1
            if remaining:
                return feishu.card_one_saved_response(saved, remaining, chat_id, saved_count)
            return feishu.card_saved_response(saved_count)
        return feishu.card_skipped_response()

    pending = db.get_pending(chat_id)
    if action == "save_all" and pending:
        prev_saved = int(value.get("saved_count", 0))
        for item in pending:
            _save_fact_item(item)
        db.clear_pending(chat_id)
        return feishu.card_saved_response(prev_saved + len(pending))
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
            saved_count = int(value.get("saved_count", 0))
            saved, remaining = db.pop_pending_item(chat_id, index)
            if saved:
                _save_fact_item(saved)
                saved_count += 1
                log.info("saved item index=%d remaining=%d total_saved=%d", index, len(remaining), saved_count)
                if remaining:
                    return feishu.card_one_saved_response(saved, remaining, chat_id, saved_count)
                return feishu.card_saved_response(saved_count)
            return feishu.card_skipped_response()

        pending = db.get_pending(chat_id)
        if action == "save_all" and pending:
            prev_saved = int(value.get("saved_count", 0))
            for item in pending:
                _save_fact_item(item)
            db.clear_pending(chat_id)
            total = prev_saved + len(pending)
            log.info("saved_all: prev=%d new=%d total=%d", prev_saved, len(pending), total)
            return feishu.card_saved_response(total)
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
        key = mention.get("key", "")
        if not key:
            continue
        if mention.get("is_bot", False):
            text = text.replace(key, "").strip()
        else:
            open_id = mention.get("id", {}).get("open_id", "")
            name = mention.get("name", "")
            if open_id and name:
                db.upsert_person(open_id, name)
            text = text.replace(key, f"@{name}" if name else "").strip()

    log.info("text: %r", text)
    if not text:
        return

    if text == "/help":
        await feishu.send_text(chat_id, _help_text(), FEISHU_APP_ID, FEISHU_APP_SECRET)
        return

    if text == "/version":
        await feishu.send_text(chat_id, f"pm-assist v{_VERSION}", FEISHU_APP_ID, FEISHU_APP_SECRET)
        return

    if text.startswith("/admin") and sender_open_id in ADMIN_OPEN_IDS:
        if text.startswith("/admin fact decompose"):
            reply = await _handle_admin_fact_decompose(text)
        else:
            reply = _handle_admin(text)
        await feishu.send_text(chat_id, reply, FEISHU_APP_ID, FEISHU_APP_SECRET)
        return

    if text.startswith("/note "):
        note = text[6:].strip()
        if note:
            bid = db.add_fact("knowledge", f"笔记#{db.count_notes() + 1}", note, source="manual")
            await feishu.send_text(chat_id, f"✓ 已记录 (ID:{bid})", FEISHU_APP_ID, FEISHU_APP_SECRET)
        return

    if text.startswith("/todo"):
        reply = _handle_todo(text)
        await feishu.send_text(chat_id, reply, FEISHU_APP_ID, FEISHU_APP_SECRET)
        return

    if text == "/clear":
        db.clear_history(chat_id)
        db.clear_pending(chat_id)
        await feishu.send_text(chat_id, "对话历史已清除。", FEISHU_APP_ID, FEISHU_APP_SECRET)
        return

    db.clear_pending(chat_id)

    db.add_message(chat_id, "user", text)
    history = db.get_history(chat_id, MAX_HISTORY)
    context = db.get_full_context()
    reply = await claude_client.chat(history, context)
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


# ── Todo 命令（所有用户）────────────────────────────────────

def _fmt_todo_list(rows) -> str:
    if not rows:
        return "暂无待办事项"
    _PRIO = {"high": "高", "medium": "中", "low": "低"}
    _ICON = {"open": "[ ]", "done": "[x]", "cancelled": "[~]"}
    open_count = sum(1 for r in rows if r["status"] == "open")
    lines = [f"📋 待办（{open_count} 条进行中）\n"]
    for r in rows:
        r = dict(r)
        icon = _ICON.get(r["status"], "[ ]")
        p = _PRIO.get(r["priority"], "")
        prio_tag = f" [{p}]" if p and p != "中" else ""
        line = f"#T{r['id']} {icon}{prio_tag} {r['title']}"
        details = []
        if r["owner"]:    details.append(f"owner:{r['owner']}")
        if r["due_date"]: details.append(f"due:{r['due_date']}")
        details.append(f"创建:{r['created_at'][:10]}")
        if r["status"] == "done": details.append(f"完成:{r['updated_at'][:10]}")
        line += "\n   " + "  ".join(details)
        src = []
        if r["source_fact_id"]:
            fact = db.get_fact(r["source_fact_id"])
            if fact: src.append(f"← risk#{r['source_fact_id']}《{fact['title'][:20]}》")
        if r["plan_id"]:
            plan = db.get_fact(r["plan_id"])
            if plan: src.append(f"← milestone#{r['plan_id']}《{plan['title'][:20]}》")
        if src:
            line += "\n   " + "  ".join(src)
        lines.append(line)
    return "\n".join(lines)


def _todo_help() -> str:
    return (
        "待办事项命令：\n"
        "/todo list              查看进行中的待办\n"
        "/todo list all          查看全部（含已完成）\n"
        "/todo list risk [ID]    查看某风险关联的待办\n"
        "/todo list plan [ID]    查看某里程碑挂载的待办\n"
        "/todo [内容]             新建独立待办\n"
        "/todo [内容] risk [ID]   从 risk 分解新建\n"
        "/todo [内容] plan [ID]   挂到里程碑新建\n"
        "/todo done [ID]         标记完成\n"
        "/todo cancel [ID]       取消\n\n"
        "管理员分解命令：\n"
        "/admin fact decompose [ID]  AI 自动分解 risk 为待办列表"
    )


def _handle_todo(text: str) -> str:
    import re
    rest = text[len("/todo"):].strip()
    if not rest or rest.lower() == "help":
        return _todo_help()

    parts = rest.split()
    sub = parts[0].lower()

    if sub == "list":
        filter_type = parts[1].lower() if len(parts) > 1 else ""
        if filter_type == "all":
            rows = db.list_todos(status=None)
        elif filter_type in ("risk", "plan") and len(parts) > 2:
            try:
                bind_id = int(parts[2])
            except ValueError:
                return "ID 必须是数字"
            if filter_type == "risk":
                rows = db.list_todos(status=None, source_fact_id=bind_id)
            else:
                rows = db.list_todos(status=None, plan_id=bind_id)
        else:
            rows = db.list_todos(status="open")
        return _fmt_todo_list(rows)

    if sub == "done" and len(parts) >= 2 and parts[1].isdigit():
        tid = int(parts[1])
        todo = db.get_todo(tid)
        if not todo:
            return f"找不到待办 #T{tid}"
        db.update_todo(tid, status="done")
        return f"✅ #T{tid} 已完成：{todo['title']}"

    if sub == "cancel" and len(parts) >= 2 and parts[1].isdigit():
        tid = int(parts[1])
        todo = db.get_todo(tid)
        if not todo:
            return f"找不到待办 #T{tid}"
        db.update_todo(tid, status="cancelled")
        return f"↩ #T{tid} 已取消：{todo['title']}"

    # 新建待办：支持末尾 "risk N" 或 "plan N" 绑定
    content = rest
    source_fact_id = None
    plan_id = None
    m = re.search(r'\s+(risk|plan)\s+(\d+)\s*$', content, re.IGNORECASE)
    if m:
        btype = m.group(1).lower()
        bid   = int(m.group(2))
        content = content[:m.start()].strip()
        if btype == "risk":
            source_fact_id = bid
        else:
            plan_id = bid

    if not content:
        return "请输入待办内容"

    tid = db.add_todo(content, source_fact_id=source_fact_id, plan_id=plan_id)
    suffix = (f"（关联 risk#{source_fact_id}）" if source_fact_id
              else f"（挂载到 milestone#{plan_id}）" if plan_id
              else "")
    return f"✓ 已新增待办 #T{tid}：{content}{suffix}"


# ── 管理员 async 命令（需要 AI 调用）────────────────────────

async def _handle_admin_fact_decompose(text: str) -> str:
    parts = text.split(None, 4)
    if len(parts) < 4 or not parts[3].isdigit():
        return "用法：/admin fact decompose [ID]"
    fact_id = int(parts[3])
    fact = db.get_fact(fact_id)
    if not fact:
        return f"找不到 fact #{fact_id}"
    if fact["dimension"] != "risk":
        return f"#{fact_id} 不是风险类型（dimension={fact['dimension']}），仅支持分解 risk/issue/blocker/dependency"
    todos = await claude_client.decompose_risk(fact)
    if not todos:
        return "AI 未能分解出待办事项，请检查条目内容是否足够具体"
    saved: list[tuple[int, str]] = []
    for t in todos:
        tid = db.add_todo(
            t["title"],
            body=t.get("body", ""),
            priority=t.get("priority", "medium"),
            owner=t.get("owner", ""),
            source_fact_id=fact_id,
            source="ai",
        )
        saved.append((tid, t["title"]))
    lines = [f"已从 #{fact_id}《{fact['title']}》分解 {len(saved)} 条待办："]
    for tid, title in saved:
        lines.append(f"  #T{tid} {title}")
    return "\n".join(lines)


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

    if cmd == "assumption":
        return _handle_admin_assumption(parts[2:] if len(parts) > 2 else [])

    if cmd == "org":
        return _handle_admin_org(parts[2:] if len(parts) > 2 else [])

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


def _handle_admin_assumption(args: list[str]) -> str:
    sub = args[0].lower() if args else ""

    if sub == "list":
        scope_filter = args[1] if len(args) > 1 else None
        rows = db.list_assumptions(scope=scope_filter)
        if not rows:
            return "暂无预设假设"
        lines = []
        for r in rows:
            scope_tag = _SCOPE_LABELS.get(r["scope"], r["scope"])
            conf_tag  = _CONF_LABELS.get(r["confidence"], r["confidence"])
            ref = f"/{r['scope_ref']}" if r["scope_ref"] else ""
            lines.append(
                f"#{r['id']} [{scope_tag}{ref}·{conf_tag}] {r['title']}"
            )
        return "\n".join(lines)

    if sub == "show" and len(args) >= 2:
        try:
            aid = int(args[1])
        except ValueError:
            return "ID 必须是数字"
        row = db.get_assumption(aid)
        if not row:
            return f"找不到 #{aid}"
        scope_tag = _SCOPE_LABELS.get(row["scope"], row["scope"])
        conf_tag  = _CONF_LABELS.get(row["confidence"], row["confidence"])
        return (
            f"#{row['id']} [{scope_tag}·{conf_tag}] {row['title']}\n"
            f"范围参考：{row['scope_ref'] or '—'}\n"
            f"创建：{row['created_at'][:16]}  更新：{row['updated_at'][:16]}\n---\n"
            f"{row['body']}"
        )

    if sub == "add" and len(args) >= 3:
        # /admin assumption add [scope] [confidence] [标题] | [正文]
        # scope: dept|project|client|global
        # confidence: universal|common|assumed
        scope      = args[1] if args[1] in ("dept","project","client","global") else "dept"
        confidence = args[2] if args[2] in ("universal","common","assumed") else "common"
        rest = " ".join(args[3:])
        if "|" in rest:
            title, body = rest.split("|", 1)
            title, body = title.strip(), body.strip()
        else:
            title, body = rest.strip(), rest.strip()
        scope_ref = ""
        # 支持 project/雅迪 这种格式指定 scope_ref
        if "/" in scope:
            scope, scope_ref = scope.split("/", 1)
        aid = db.add_assumption(title, body, scope=scope, scope_ref=scope_ref, confidence=confidence)
        return f"✓ 已添加预设假设 #{aid} [{scope}·{confidence}] {title}"

    if sub == "update" and len(args) >= 4:
        try:
            aid = int(args[1])
        except ValueError:
            return "ID 必须是数字"
        field = args[2].lower()
        value = " ".join(args[3:])
        allowed = {"title", "body", "scope", "scope_ref", "confidence"}
        if field not in allowed:
            return f"可更新字段：{', '.join(sorted(allowed))}"
        db.update_assumption(aid, **{field: value})
        return f"✓ 已更新 #{aid}.{field}"

    if sub == "archive" and len(args) >= 2:
        try:
            aid = int(args[1])
        except ValueError:
            return "ID 必须是数字"
        db.update_assumption(aid, active=0)
        return f"✓ 已归档预设 #{aid}"

    if sub == "delete" and len(args) >= 2:
        try:
            aid = int(args[1])
        except ValueError:
            return "ID 必须是数字"
        db.delete_assumption(aid)
        return f"✓ 已删除预设 #{aid}"

    return (
        "assumption 命令（部门预设假设管理）：\n"
        "/admin assumption list [dept|project|client]  列出预设\n"
        "/admin assumption show [ID]                   查看详情\n"
        "/admin assumption add [scope] [confidence] [标题] | [正文]\n"
        "  scope: dept|project/项目名|client|global\n"
        "  confidence: universal（铁律）|common（通常）|assumed（推测）\n"
        "/admin assumption update [ID] [field] [值]\n"
        "/admin assumption archive [ID]\n"
        "/admin assumption delete [ID]"
    )


def _handle_admin_org(args: list[str]) -> str:
    sub = args[0].lower() if args else ""

    if sub == "list":
        type_filter = args[1] if len(args) > 1 else None
        rows = db.list_org_units(type_=type_filter)
        if not rows:
            return "暂无组织单元"
        lines = []
        for r in rows:
            indent = "  " if r["parent_id"] else ""
            lines.append(f"{indent}#{r['id']} [{r['type']}] {r['name']}")
        return "\n".join(lines)

    if sub == "add" and len(args) >= 3:
        # /admin org add [type] [name] [parent_id?]
        type_ = args[1]
        name  = args[2]
        parent_id = int(args[3]) if len(args) >= 4 else None
        oid = db.add_org_unit(type_, name, parent_id=parent_id)
        return f"✓ 已添加组织单元 #{oid} [{type_}] {name}"

    return (
        "org 命令（组织结构管理）：\n"
        "/admin org list [type?]               列出组织单元\n"
        "/admin org add [type] [名称] [父ID?]  新增\n"
        "  type: company|dept|team|role|client_org"
    )


def _help_text() -> str:
    return """PM助手使用说明

所有人可用：
  @Bot [消息]              AI对话（结合知识库、风险和待办上下文）
  /note [内容]             快速记录一条笔记
  /todo list               查看进行中的待办
  /todo list all           查看全部待办（含已完成）
  /todo list risk [ID]     查看某风险关联的待办
  /todo list plan [ID]     查看某里程碑挂载的待办
  /todo [内容]              新建独立待办
  /todo [内容] risk [ID]   从 risk 分解新建待办
  /todo [内容] plan [ID]   挂到里程碑新建待办
  /todo done [ID]          标记待办完成
  /todo cancel [ID]        取消待办
  /clear                   清除当前会话历史
  /version                 查看当前版本号
  /help                    显示本说明

管理员 — 风险管理：
  /admin risk list [open|all]
  /admin risk close/reopen [ID]
  /admin risk owner [ID] [姓名]
  /admin risk add [type] [priority] [标题] | [描述]
    type: risk|issue|blocker|dependency

管理员 — 统一信息管理：
  /admin fact list [type] [active|all]
  /admin fact show [ID]
  /admin fact update [ID] [field] [值]
  /admin fact archive/delete [ID]
  /admin fact add [type] [标题] | [正文]
  /admin fact decompose [ID]   AI 分解 risk 为待办列表

管理员 — 预设假设（部门公认背景知识）：
  /admin assumption list [dept|project|client]
  /admin assumption show [ID]
  /admin assumption add [scope] [confidence] [标题] | [正文]
  /admin assumption update/archive/delete [ID]

管理员 — 组织结构：
  /admin org list
  /admin org add [type] [名称] [父ID?]"""


def _admin_help() -> str:
    return (
        "管理员命令：\n"
        "/admin risk list/close/reopen/owner/add\n"
        "/admin fact list/show/update/archive/delete/add\n"
        "/admin fact decompose [ID]   AI 分解 risk 为待办\n"
        "/admin assumption list/show/add/update/archive/delete\n"
        "/admin org list/add\n\n"
        "所有人可用：/todo list/done/cancel/[新建内容]"
    )
