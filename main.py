from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

import ai_client
import db
import feishu
import notify as _notify
from web_admin import router as admin_router
from config import (
    ADMIN_OPEN_IDS,
    FEISHU_APP_ID,
    FEISHU_APP_SECRET,
    FEISHU_VERIFICATION_TOKEN,
    MAX_HISTORY,
)

_ROLE_ZH = {"super_admin": "管理员", "pm": "项目经理PM", "member": "项目成员", "pending": "待审批"}

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
_REVIEW_MODE_KEY = "nightly_review_mode"
_REVIEW_MODE_REPORT = "report_only"
_REVIEW_MODE_DIRECT = "direct_cleanup"
_REVIEW_MODE_ALIASES = {
    "report": _REVIEW_MODE_REPORT,
    "report_only": _REVIEW_MODE_REPORT,
    "only_report": _REVIEW_MODE_REPORT,
    "仅报告": _REVIEW_MODE_REPORT,
    "direct": _REVIEW_MODE_DIRECT,
    "direct_cleanup": _REVIEW_MODE_DIRECT,
    "cleanup": _REVIEW_MODE_DIRECT,
    "直接清洗": _REVIEW_MODE_DIRECT,
}


def _review_mode_label(mode: str) -> str:
    return "直接清洗" if mode == _REVIEW_MODE_DIRECT else "仅报告"


def _review_run_suffix(mode: str, merge_count: int, action_count: int) -> str:
    """构建洗盘完成提示的后缀，按模式区分是自动执行还是待确认。"""
    parts = []
    if merge_count:
        parts.append(f"{merge_count} 组合并建议")
    if action_count:
        parts.append(f"{action_count} 项清洗建议")
    if not parts:
        return ""
    joined = "、".join(parts)
    if mode == _REVIEW_MODE_DIRECT:
        return f" 已自动执行 {joined}。"
    return f" 另发现 {joined}，请在卡片中逐条确认。"


def _get_review_mode() -> str:
    mode = db.get_setting(_REVIEW_MODE_KEY, _REVIEW_MODE_REPORT)
    return mode if mode in {_REVIEW_MODE_REPORT, _REVIEW_MODE_DIRECT} else _REVIEW_MODE_REPORT


def _normalize_review_mode(value: str) -> str | None:
    return _REVIEW_MODE_ALIASES.get(value.strip().lower())


def _apply_review_commands(report: str) -> list[str]:
    """direct 模式：自动执行全部合并建议和清洗建议。"""
    results: list[str] = []

    for item in _extract_merge_candidates(report):
        keep_id = item.get("keep_id")
        merge_ids = item.get("merge_ids", [])
        if not keep_id or not merge_ids:
            continue
        try:
            _apply_merge_item(item)
            results.append(f"已合并 {', '.join(f'#{m}' for m in merge_ids)} → #{keep_id}")
        except Exception as e:
            results.append(f"合并失败 #{keep_id}：{e}")

    for item in _extract_action_candidates(report):
        kind   = item.get("kind", "")
        action = item.get("action", "")
        fid    = item.get("id")
        title  = item.get("title", f"#{fid}")
        if kind == "new_todo" and action == "add":
            try:
                db.add_todo(
                    item.get("title", ""),
                    body=item.get("body", ""),
                    priority=item.get("priority", "") or "medium",
                    owner=item.get("owner", ""),
                    due_date=item.get("due_date", ""),
                    source="ai",
                )
                results.append(f"已新增待办：{title}")
            except Exception as e:
                results.append(f"新增待办失败：{e}")
            continue
        if not fid:
            continue
        if kind == "fact" and action == "archive":
            fact = db.get_fact(fid)
            if not fact:
                results.append(f"跳过：找不到 fact #{fid}")
                continue
            db.update_fact(fid, status="archived")
            results.append(f"已归档 #{fid}：{title}")
        elif kind == "risk" and action == "close":
            fact = db.get_fact(fid)
            if not fact:
                results.append(f"跳过：找不到 risk #{fid}")
                continue
            db.update_fact(fid, status="resolved")
            results.append(f"已关闭风险 #{fid}：{title}")
        elif kind == "todo" and action in ("done", "cancel"):
            todo = db.get_todo(fid)
            if not todo:
                results.append(f"跳过：找不到 todo #{fid}")
                continue
            new_status = "done" if action == "done" else "cancelled"
            db.update_todo(fid, status=new_status)
            results.append(f"已{action} todo #{fid}：{title}")

    if not results:
        results.append("未发现可自动执行的动作。")
    return results


def _extract_merge_candidates(report: str) -> list[dict]:
    start_marker = "===MERGE_CANDIDATES_JSON==="
    end_marker = "===END_MERGE_CANDIDATES_JSON==="
    start = report.find(start_marker)
    end = report.find(end_marker)
    if start < 0 or end < 0 or end <= start:
        return []
    raw = report[start + len(start_marker):end].strip()
    try:
        payload = json.loads(raw)
    except Exception:
        log.warning("failed to parse merge candidates JSON")
        return []

    candidates = []
    for item in payload.get("merge_candidates", []):
        try:
            keep_id = int(item.get("keep_id"))
            merge_ids = [int(mid) for mid in item.get("merge_ids", []) if int(mid) != keep_id]
        except Exception:
            continue
        if not merge_ids:
            continue
        keep_fact = db.get_fact(keep_id)
        if not keep_fact:
            continue
        valid_merge_ids = [mid for mid in merge_ids if db.get_fact(mid)]
        if not valid_merge_ids:
            continue
        candidates.append({
            "keep_id": keep_id,
            "merge_ids": valid_merge_ids,
            "reason": str(item.get("reason", ""))[:300],
            "append_text": str(item.get("append_text", ""))[:1000],
        })
    return candidates[:10]


def _extract_json_section(report: str, start_marker: str, end_marker: str) -> dict:
    start = report.find(start_marker)
    end = report.find(end_marker)
    if start < 0 or end < 0 or end <= start:
        return {}
    raw = report[start + len(start_marker):end].strip()
    try:
        return json.loads(raw)
    except Exception:
        log.warning("failed to parse JSON section %s", start_marker)
        return {}


def _extract_action_candidates(report: str) -> list[dict]:
    payload = _extract_json_section(
        report,
        "===ACTION_CANDIDATES_JSON===",
        "===END_ACTION_CANDIDATES_JSON===",
    )
    candidates = []
    for item in payload.get("action_candidates", []):
        kind = str(item.get("kind", "")).lower()
        action = str(item.get("action", "")).lower()

        if kind == "new_todo" and action == "add":
            title = str(item.get("title", "")).strip()
            if not title:
                continue
            candidates.append({
                "kind": "new_todo",
                "id": None,
                "action": "add",
                "title": title[:80],
                "body": str(item.get("body", "")),
                "priority": str(item.get("priority", "medium")),
                "owner": str(item.get("owner", "")),
                "due_date": str(item.get("due_date", "")),
                "reason": str(item.get("reason", ""))[:300],
                "type_label": "",
            })
            continue

        try:
            item_id = int(item.get("id"))
        except Exception:
            continue
        if (kind, action) not in {
            ("risk", "close"),
            ("fact", "archive"),
            ("todo", "done"),
            ("todo", "cancel"),
        }:
            continue

        title = ""
        type_label = ""
        if kind in ("risk", "fact"):
            fact = db.get_fact(item_id)
            if not fact:
                continue
            if kind == "risk" and fact.get("dimension") != "risk":
                continue
            title = fact.get("title", "")
            type_label = _TYPE_LABELS.get(fact.get("type", ""), fact.get("type", ""))
        else:
            todo = db.get_todo(item_id)
            if not todo:
                continue
            title = todo.get("title", "")
            type_label = "待办"

        candidates.append({
            "kind": kind,
            "id": item_id,
            "action": action,
            "title": title[:80],
            "reason": str(item.get("reason", ""))[:300],
            "type_label": type_label,
        })
    return candidates[:10]


def _extract_clarify(text: str) -> dict | None:
    """从 AI 回复中解析 ===CLARIFY=== 块，返回 {q, opts} 或 None。"""
    start = text.find("===CLARIFY===")
    end   = text.find("===END_CLARIFY===")
    if start < 0 or end < 0 or end <= start:
        return None
    raw = text[start + len("===CLARIFY==="):end].strip()
    try:
        return json.loads(raw)
    except Exception:
        log.warning("failed to parse CLARIFY JSON: %r", raw[:200])
        return None


def _strip_clarify(text: str) -> str:
    """从 AI 回复中剔除 ===CLARIFY=== 块。"""
    start = text.find("===CLARIFY===")
    end   = text.find("===END_CLARIFY===")
    if start >= 0 and end > start:
        text = (text[:start] + text[end + len("===END_CLARIFY==="):]).strip()
    return text


_EXECUTION_CLAIM_RE = re.compile(r"(已确认|已更新|已保存|已执行|已记录|我会记住|已稳定记录|系统中已|已在系统|已存档|已收录)")


def _sanitize_ai_execution_claims(text: str, will_send_card: bool = False) -> str:
    """Prevent AI from claiming DB changes were already executed in chat mode."""
    has_claim = bool(_EXECUTION_CLAIM_RE.search(text))
    has_command = any(
        line.strip().startswith(("/todo", "/risk", "/admin"))
        for line in text.splitlines()
    )
    if not has_claim or has_command:
        return text
    if will_send_card:
        return (
            "**提示：** AI 检测到可保存的建议，已弹出确认卡片，请按需确认入库。\n\n"
            + text
        )
    return (
        "**提示：** AI 提及了可记录的信息，但本次未生成确认卡片。如需保存，请用 `/note`、`/risk add` 或 `/todo` 命令手动记录。\n\n"
        + text
    )


def _strip_merge_candidates_json(report: str) -> str:
    # 剥除机器可读 JSON 块
    for start_marker, end_marker in (
        ("===MERGE_CANDIDATES_JSON===", "===END_MERGE_CANDIDATES_JSON==="),
        ("===ACTION_CANDIDATES_JSON===", "===END_ACTION_CANDIDATES_JSON==="),
    ):
        start = report.find(start_marker)
        end = report.find(end_marker)
        if start >= 0 and end > start:
            report = (report[:start] + report[end + len(end_marker):]).strip()
    # 剥除"机器可读"章节标题行（避免空标题残留）
    report = re.sub(r"##\s+[一-十\d]+、?机器可读[^\n]*\n?", "", report)
    return report.strip()


async def _build_and_save_review(mode: str | None = None) -> str | None:
    facts_text = db.get_all_facts_for_review()
    if not facts_text:
        log.info("nightly review: no active facts to review")
        return None

    effective_mode = mode or _get_review_mode()
    report = await ai_client.nightly_review(facts_text)
    if effective_mode == _REVIEW_MODE_DIRECT:
        results = _apply_review_commands(report)
        report = (
            f"{report}\n\n"
            f"=== 直接清洗执行结果 ===\n"
            + "\n".join(f"- {r}" for r in results)
        )
    db.save_nightly_review(_strip_merge_candidates_json(report))
    log.info("nightly review saved to DB mode=%s", effective_mode)
    return report


def _review_recipients_admins_pm() -> set[str]:
    recipients = set(ADMIN_OPEN_IDS)
    for role in ("super_admin", "pm"):
        for user in db.list_users(role=role, status="active"):
            if user["open_id"]:
                recipients.add(user["open_id"])
    return recipients


async def _send_review_to_admins_pm(report: str):
    recipients = _review_recipients_admins_pm()
    for uid in recipients:
        await feishu.send_reply_to_user(uid, report, FEISHU_APP_ID, FEISHU_APP_SECRET)
    log.info("manual review sent to %d admins/PMs: %s", len(recipients), recipients)


def _collect_review_suggestion_items(report: str) -> list[dict]:
    """将洗盘报告中的合并/清洗候选转为 AI 建议卡片 item 格式。"""
    items: list[dict] = []
    for c in _extract_merge_candidates(report):
        merge_ids = c.get("merge_ids", [])
        items.append({
            "kind":        "merge_fact",
            "keep_id":     c.get("keep_id"),
            "merge_ids":   merge_ids,
            "reason":      c.get("reason", ""),
            "append_text": c.get("append_text", ""),
            "title":       f"合并到 #{c.get('keep_id')}",
        })
    for c in _extract_action_candidates(report):
        items.append({
            "kind":     "review_action",
            "sub_kind": c.get("kind"),    # "risk" / "fact" / "todo" / "new_todo"
            "action":   c.get("action"),  # "close" / "archive" / "done" / "cancel" / "add"
            "id":       c.get("id"),
            "title":    c.get("title", ""),
            "body":     c.get("body", ""),
            "priority": c.get("priority", ""),
            "owner":    c.get("owner", ""),
            "due_date": c.get("due_date", ""),
            "reason":   c.get("reason", ""),
        })
    return items


async def _send_review_suggestions_card(chat_id: str, report: str):
    """将洗盘的合并+清洗建议合并为一张 AI 建议确认卡片发送给单个用户/群聊。"""
    if not chat_id:
        return
    items = _collect_review_suggestion_items(report)
    if not items:
        return
    db.save_pending_commands(chat_id, items)
    card = feishu.build_ai_suggestions_card(items, chat_id)
    if chat_id.startswith("ou_"):
        await feishu.send_reply_to_user(chat_id, card, FEISHU_APP_ID, FEISHU_APP_SECRET)
    else:
        await feishu.send_reply(chat_id, card, FEISHU_APP_ID, FEISHU_APP_SECRET)
    log.info("sent %d review suggestion items to %s", len(items), chat_id)


async def _broadcast_review_suggestions(open_ids: set[str], report: str):
    """向多个用户各自发送独立的洗盘建议确认卡片（每人一份 pending_commands）。"""
    items = _collect_review_suggestion_items(report)
    if not items:
        return
    for uid in open_ids:
        if not uid:
            continue
        db.save_pending_commands(uid, items)
        card = feishu.build_ai_suggestions_card(items, uid)
        await feishu.send_reply_to_user(uid, card, FEISHU_APP_ID, FEISHU_APP_SECRET)
    log.info("broadcast %d review suggestion items to %d users", len(items), len(open_ids))


async def _morning_review_and_report():
    """每天09:00：先执行AI洗盘，再发送风险日报 + 洗盘报告给所有收件人。
    - 管理员/NOTIFY：发全项目综合卡片（含AI洗盘摘要）+ 建议确认卡片
    - PM用户：发其所属项目的专属卡片（含AI洗盘摘要）+ 建议确认卡片
    注意：如果同时开了 crontab 跑 notify.py，主管理员会收到两次，二选一即可。
    """
    from config import NOTIFY_OPEN_IDS
    try:
        report = await _build_and_save_review()
        review_text = _strip_merge_candidates_json(report) if report else None

        # 按项目生成卡片（PM 卡片也带洗盘摘要）
        project_cards = _notify.get_morning_cards(review_text)

        # 管理员 + NOTIFY 接全量卡片
        admin_recipients = ADMIN_OPEN_IDS | NOTIFY_OPEN_IDS
        if not admin_recipients:
            log.warning("no recipients configured (ADMIN_OPEN_IDS and NOTIFY_OPEN_IDS both empty)")
        for uid in admin_recipients:
            await feishu.send_reply_to_user(uid, project_cards[None], FEISHU_APP_ID, FEISHU_APP_SECRET)

        # PM 用户：收自己项目的卡片（不重复发给已在 admin_recipients 的人）
        pm_open_ids: set[str] = set()
        pm_users = db.list_users(role="pm", status="active")
        for user in pm_users:
            uid = user.get("open_id", "")
            proj = user.get("project", "")
            if not uid or uid in admin_recipients:
                continue
            pm_open_ids.add(uid)
            card = project_cards.get(proj, project_cards[None])
            await feishu.send_reply_to_user(uid, card, FEISHU_APP_ID, FEISHU_APP_SECRET)

        log.info("morning report sent: %d admins/notify + %d PMs",
                 len(admin_recipients), len(pm_users))

        # report_only：广播建议确认卡片；direct：action 已自动执行，不发卡片
        if report and _get_review_mode() != _REVIEW_MODE_DIRECT:
            await _broadcast_review_suggestions(ADMIN_OPEN_IDS | pm_open_ids, report)
    except Exception:
        log.exception("morning review and report error")


def _do_db_backup():
    backup_dir = Path("backups")
    backup_dir.mkdir(exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    dest = backup_dir / f"pm_assist_{today}.db"
    if dest.exists():
        return
    src = sqlite3.connect(db.DB_PATH)
    bak = sqlite3.connect(str(dest))
    src.backup(bak)
    bak.close()
    src.close()
    kept = sorted(backup_dir.glob("pm_assist_*.db"))
    for old in kept[:-7]:
        old.unlink()
    log.info("DB backup: %s", dest.name)


async def _backup_db():
    try:
        await asyncio.to_thread(_do_db_backup)
    except Exception:
        log.exception("DB backup error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    log.info("DB initialized")
    _scheduler.add_job(_morning_review_and_report, "cron", hour=9, minute=0, id="morning_report")
    _scheduler.add_job(_backup_db, "cron", hour=3, minute=0, id="db_backup")
    _scheduler.start()
    log.info("Scheduler started: morning_review@09:00, db_backup@03:00 (Asia/Shanghai)")
    yield
    _scheduler.shutdown()


app = FastAPI(lifespan=lifespan)
app.include_router(admin_router)


# ── Webhook 入口 ──────────────────────────────────────────

@app.post("/webhook/feishu")
async def feishu_webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.json()
    log.info("webhook raw keys=%s event_type=%s", list(body.keys()), body.get("header", {}).get("event_type", body.get("type", "?")))

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

    if event_type == "application.bot.menu_v6":
        background_tasks.add_task(_handle_bot_menu, body.get("event", {}))
        return {"ok": True}

    return {"ok": True}


# ── 卡片回调 ──────────────────────────────────────────────

def _save_fact_item(item: dict):
    """统一保存逻辑：新增或追加更新到已有条目。"""
    action = item.get("action", "new")
    project = item.get("project", "默认")
    if action == "update" and item.get("fact_id"):
        db.append_to_fact(item["fact_id"], item["content"])
    else:
        label = _TYPE_LABELS.get(item["type"], item["type"])
        db.add_fact(
            item["type"],
            f"[{label}] {item['content'][:20]}",
            item["content"],
            source="ai",
            project=project,
        )


async def _handle_card_callback(body: dict) -> dict:
    value = body.get("action", {}).get("value", {})
    action = value.get("action")
    chat_id = value.get("chat_id", "")
    log.info("card callback action=%s chat_id=%s", action, chat_id)

    # ── 合并确认卡片 ──
    if action == "merge_one":
        return _card_merge_one(value, chat_id)
    if action == "merge_all":
        return _card_merge_all(value, chat_id)
    if action == "skip_merges":
        db.clear_pending_merges(chat_id)
        return feishu.card_merge_skipped_response()

    # ── 风险/待办处理确认卡片 ──
    if action == "review_action_one":
        return _card_review_action_one(value, chat_id)
    if action == "review_action_all":
        return _card_review_action_all(value, chat_id)
    if action == "skip_review_actions":
        db.clear_pending_actions(chat_id)
        return feishu.card_action_skipped_response()

    # ── AI 建议确认卡片 ──
    if action == "suggestion_save_one":
        return _card_suggestion_save_one(value, chat_id)
    if action == "suggestion_skip_one":
        return _card_suggestion_skip_one(value, chat_id)
    if action == "suggestion_save_all":
        return _card_suggestion_save_all(value, chat_id)
    if action == "suggestion_skip_all":
        return _card_suggestion_skip_all(value, chat_id)
    if action == "suggestion_view_detail":
        index = int(value.get("index", -1))
        items = db.get_pending_commands(chat_id)
        if index < 0 or index >= len(items):
            return feishu.card_skipped_response()
        return {
            "toast": {"type": "info", "content": "已打开详情"},
            "card": {"type": "raw", "data": feishu.build_suggestion_detail_card(items[index], chat_id, index)},
        }
    if action == "suggestion_back_to_list":
        return {
            "toast": {"type": "info", "content": "已返回建议清单"},
            "card": _card_suggestions_update(chat_id),
        }

    return feishu.card_skipped_response()


def _save_todo_item(t: dict):
    db.add_todo(
        t["title"],
        body=t.get("body", ""),
        priority=t.get("priority", "medium"),
        owner=t.get("owner", ""),
        due_date=t.get("due_date", ""),
        project=t.get("project", "默认"),
        source_fact_id=t.get("source_fact_id"),
        plan_id=t.get("plan_id"),
        source="ai",
    )



def _apply_merge_item(item: dict):
    keep_id = int(item["keep_id"])
    merge_ids = [int(mid) for mid in item.get("merge_ids", [])]
    append_text = item.get("append_text", "").strip()
    reason = item.get("reason", "").strip()
    source = ", ".join(f"#{mid}" for mid in merge_ids)
    addition_parts = [f"合并自 {source}"]
    if reason:
        addition_parts.append(f"原因：{reason}")
    if append_text:
        addition_parts.append(f"补充：{append_text}")
    db.append_to_fact(keep_id, "；".join(addition_parts))
    for mid in merge_ids:
        if mid != keep_id:
            db.update_fact(mid, status="archived")


def _card_merge_one(value: dict, chat_id: str) -> dict:
    index = int(value.get("index", -1))
    saved_count = int(value.get("saved_count", 0))
    saved, remaining = db.pop_pending_merge(chat_id, index)
    if saved:
        _apply_merge_item(saved)
        saved_count += 1
        if remaining:
            return feishu.card_merge_one_saved_response(saved, remaining, chat_id, saved_count)
        return feishu.card_merge_saved_response(saved_count)
    return feishu.card_merge_skipped_response()


def _card_merge_all(value: dict, chat_id: str) -> dict:
    prev_saved = int(value.get("saved_count", 0))
    pending = db.get_pending_merges(chat_id)
    if pending:
        for item in pending:
            _apply_merge_item(item)
        db.clear_pending_merges(chat_id)
        return feishu.card_merge_saved_response(prev_saved + len(pending))
    db.clear_pending_merges(chat_id)
    return feishu.card_merge_skipped_response()


def _apply_review_action(item: dict):
    kind = item.get("kind")
    action = item.get("action")
    if kind == "new_todo" and action == "add":
        db.add_todo(
            item.get("title", ""),
            body=item.get("body", ""),
            priority=item.get("priority", "") or "medium",
            owner=item.get("owner", ""),
            due_date=item.get("due_date", ""),
            source="ai",
        )
        return
    item_id = int(item["id"])
    if kind == "risk" and action == "close":
        db.update_risk(item_id, status="closed")
    elif kind == "fact" and action == "archive":
        db.update_fact(item_id, status="archived")
    elif kind == "todo" and action == "done":
        db.update_todo(item_id, status="done")
    elif kind == "todo" and action == "cancel":
        db.update_todo(item_id, status="cancelled")
    else:
        raise ValueError(f"unsupported review action: {kind}.{action}")


def _card_review_action_one(value: dict, chat_id: str) -> dict:
    index = int(value.get("index", -1))
    saved_count = int(value.get("saved_count", 0))
    saved, remaining = db.pop_pending_action(chat_id, index)
    if saved:
        _apply_review_action(saved)
        saved_count += 1
        if remaining:
            return feishu.card_action_one_saved_response(saved, remaining, chat_id, saved_count)
        return feishu.card_action_saved_response(saved_count)
    return feishu.card_action_skipped_response()


def _card_review_action_all(value: dict, chat_id: str) -> dict:
    prev_saved = int(value.get("saved_count", 0))
    pending = db.get_pending_actions(chat_id)
    if pending:
        for item in pending:
            _apply_review_action(item)
        db.clear_pending_actions(chat_id)
        return feishu.card_action_saved_response(prev_saved + len(pending))
    db.clear_pending_actions(chat_id)
    return feishu.card_action_skipped_response()


def _extract_command_candidates(text: str, sender_open_id: str,
                                project: str | None) -> list[dict]:
    # Legacy command extraction — kept for potential future use but no longer called in main flow.
    return []
    candidates: list[dict] = []
    seen: set[str] = set()
    user_role = (db.get_user(sender_open_id) or {}).get("role", "")
    allowed = (
        re.compile(r"^/admin\s+fact\s+update\s+\d+\s+(status|owner|priority|due_date|title|body)\s+.+$", re.I),
        re.compile(r"^/admin\s+fact\s+archive\s+\d+\s*$", re.I),
        re.compile(r"^/admin\s+fact\s+add\s+(risk|issue|milestone|decision|team|client|knowledge|process|org)\s+.+$", re.I),
        re.compile(r"^/risk\s+add\s+(risk|issue|blocker|dependency)\s+(high|medium|low)\s+.+$", re.I),
        re.compile(r"^/risk\s+owner\s+\d+\s+.+$", re.I),
        re.compile(r"^/risk\s+(close|reopen)\s+\d+\s*$", re.I),
        re.compile(r"^/todo\s+update\s+\d+\s+(title|body|priority|owner|due_date)\s+.+$", re.I),
        re.compile(r"^/todo\s+(done|cancel)\s+\d+\s*$", re.I),
        re.compile(r"^/todo\s+(?!list\b|show\b|help\b).+", re.I),
    )
    def describe_command(cmd: str) -> str:
        m = re.match(r"^/risk\s+owner\s+(\d+)\s+(.+)$", cmd, re.I)
        if m:
            rid = int(m.group(1))
            new_owner = m.group(2).strip()
            fact = db.get_fact(rid)
            if fact:
                old_owner = fact.get("owner") or "未设置"
                return f"目标：风险 #{rid}《{fact.get('title','')}》；负责人：{old_owner} -> {new_owner}"
            return f"目标：风险 #{rid}（未找到详情）"

        m = re.match(r"^/risk\s+(close|reopen)\s+(\d+)$", cmd, re.I)
        if m:
            action = m.group(1).lower()
            rid = int(m.group(2))
            fact = db.get_fact(rid)
            if fact:
                status = fact.get("status") or "unknown"
                target = "closed" if action == "close" else "open"
                return f"目标：风险 #{rid}《{fact.get('title','')}》；状态：{status} -> {target}"
            return f"目标：风险 #{rid}（未找到详情）"

        m = re.match(r"^/admin\s+fact\s+update\s+(\d+)\s+([a-z_]+)\s+(.+)$", cmd, re.I)
        if m:
            fid = int(m.group(1))
            field = m.group(2).lower()
            new_val = m.group(3).strip()
            fact = db.get_fact(fid)
            if fact:
                old_val = fact.get(field) if field in fact.keys() else ""
                old_show = old_val if old_val not in (None, "") else "未设置"
                return f"目标：#{fid}《{fact.get('title','')}》；{field}：{old_show} -> {new_val}"
            return f"目标：事实 #{fid}（未找到详情）"

        m = re.match(r"^/admin\s+fact\s+archive\s+(\d+)$", cmd, re.I)
        if m:
            fid = int(m.group(1))
            fact = db.get_fact(fid)
            if fact:
                return f"目标：#{fid}《{fact.get('title','')}》；状态 -> archived"
            return f"目标：事实 #{fid}（未找到详情）"

        m = re.match(r"^/todo\s+update\s+(\d+)\s+([a-z_]+)\s+(.+)$", cmd, re.I)
        if m:
            tid = int(m.group(1))
            field = m.group(2).lower()
            new_val = m.group(3).strip()
            todo = db.get_todo(tid)
            if todo:
                old_val = todo.get(field) if field in todo.keys() else ""
                old_show = old_val if old_val not in (None, "") else "未设置"
                return f"目标：#T{tid}《{todo.get('title','')}》；{field}：{old_show} -> {new_val}"
            return f"目标：待办 #T{tid}（未找到详情）"

        m = re.match(r"^/todo\s+(done|cancel)\s+(\d+)$", cmd, re.I)
        if m:
            action = m.group(1).lower()
            tid = int(m.group(2))
            todo = db.get_todo(tid)
            if todo:
                status = "done" if action == "done" else "cancelled"
                return f"目标：#T{tid}《{todo.get('title','')}》；状态 -> {status}"
            return f"目标：待办 #T{tid}（未找到详情）"

        m = re.match(r"^/todo\s+(.+?)\s+(risk|plan)\s+(\d+)\s*$", cmd, re.I)
        if m:
            bind_type = m.group(2).lower()
            bind_id = int(m.group(3))
            if bind_type == "risk":
                fact = db.get_fact(bind_id)
                if fact:
                    return f"关联：风险 #{bind_id}《{fact.get('title','')}》"
            if bind_type == "plan":
                fact = db.get_fact(bind_id)
                if fact:
                    return f"关联：里程碑 #{bind_id}《{fact.get('title','')}》"
            return f"关联：{bind_type} #{bind_id}"

        # Generic fallback: extract first numeric ID and try to resolve entity title.
        nums = re.findall(r"\b\d+\b", cmd)
        if nums:
            item_id = int(nums[0])
            if cmd.startswith("/todo"):
                todo = db.get_todo(item_id)
                if todo:
                    return f"目标：#T{item_id}《{todo.get('title','')}》"
            fact = db.get_fact(item_id)
            if fact:
                return f"目标：#{item_id}《{fact.get('title','')}》"
            return f"目标：#{item_id}（未找到详情）"

        return ""

    def target_of_command(cmd: str) -> tuple[str, int] | tuple[None, None]:
        m = re.match(r"^/risk\s+(owner|close|reopen)\s+(\d+)\b", cmd, re.I)
        if m:
            return "risk", int(m.group(2))
        m = re.match(r"^/admin\s+fact\s+(update|archive)\s+(\d+)\b", cmd, re.I)
        if m:
            return "fact", int(m.group(2))
        m = re.match(r"^/todo\s+(update|done|cancel)\s+(\d+)\b", cmd, re.I)
        if m:
            return "todo", int(m.group(2))
        return None, None

    for line in text.splitlines():
        cmd = line.strip().strip("`")
        cmd = re.sub(r"^[-*•]\s+", "", cmd)
        cmd = re.sub(r"^\d+[.)、]\s+", "", cmd)
        cmd = cmd.strip().strip("`")
        if cmd.startswith("[AUTO]"):
            cmd = cmd[len("[AUTO]"):].strip()
        if not cmd.startswith(("/admin", "/risk", "/todo")):
            continue
        if cmd.startswith("/admin") and user_role != "super_admin":
            continue
        if not any(pattern.match(cmd) for pattern in allowed):
            continue
        if cmd in seen:
            continue
        seen.add(cmd)
        if cmd.startswith("/admin fact update"):
            title = "更新知识库字段"
        elif cmd.startswith("/admin fact archive"):
            title = "归档知识库条目"
        elif cmd.startswith("/admin fact add"):
            title = "新增知识库条目"
        elif cmd.startswith("/risk add"):
            title = "新增风险"
        elif cmd.startswith("/risk owner"):
            title = "更新风险负责人"
        elif re.match(r"^/risk\s+(close|reopen)\b", cmd, re.I):
            title = "更新风险状态"
        elif cmd.startswith("/todo update"):
            title = "更新待办"
        elif re.match(r"^/todo\s+(done|cancel)\b", cmd, re.I):
            title = "处理待办"
        else:
            title = "新增待办"
        target_kind, target_id = target_of_command(cmd)
        target_meta: dict = {}
        if target_kind == "risk" and target_id:
            fact = db.get_fact(int(target_id))
            if fact:
                target_meta = {
                    "id": int(fact.get("id", 0) or 0),
                    "title": fact.get("title", ""),
                    "type": fact.get("type", ""),
                    "priority": fact.get("priority", ""),
                    "owner": fact.get("owner", ""),
                    "due_date": fact.get("due_date", ""),
                }
        command_type = "other"
        if cmd.startswith("/risk"):
            command_type = "risk"
        elif cmd.startswith("/todo"):
            command_type = "todo"
        elif cmd.startswith("/admin fact"):
            command_type = "fact"
        candidates.append({
            "command": cmd,
            "title": title,
            "description": describe_command(cmd),
            "target_kind": target_kind or "",
            "target_id": int(target_id) if target_id else 0,
            "target_meta": target_meta,
            "command_type": command_type,
            "status": "pending",
            "sender_open_id": sender_open_id,
            "project": project,
        })
    return candidates[:10]


def _execute_confirmed_command(item: dict) -> str:
    cmd = item.get("command", "").strip()
    project = item.get("project")
    sender_open_id = item.get("sender_open_id", "")
    if cmd.startswith("/admin"):
        return _handle_admin(cmd, sender_open_id, project, "")
    if cmd.startswith("/risk"):
        args = cmd.split(None, 1)[1].split() if len(cmd.split(None, 1)) > 1 else []
        return _handle_admin_risk(args, project=project)
    if cmd.startswith("/todo"):
        return _handle_todo(cmd, project=project)
    raise ValueError(f"unsupported command: {cmd}")


def _get_command_list(chat_id: str) -> list[dict]:
    return db.get_pending_commands(chat_id)


def _save_command_list(chat_id: str, items: list[dict]):
    db.save_pending_commands(chat_id, items)


def _count_processed_commands(items: list[dict]) -> int:
    return sum(1 for x in items if x.get("status") in ("saved", "skipped"))


def _build_command_preview(item: dict) -> str:
    if item.get("suggestion_kind") == "fact":
        fact_item = item.get("fact_item", {}) or {}
        ftype = fact_item.get("type", "knowledge")
        content = fact_item.get("content", "")
        action = fact_item.get("action", "new")
        if action == "update":
            fid = fact_item.get("fact_id")
            ftitle = fact_item.get("fact_title", "")
            return (
                f"**建议类型**\n知识库更新\n\n"
                f"**目标**\n#{fid} {ftitle}\n\n"
                f"**变更点**\n追加信息：{content[:300]}"
            )
        return (
            f"**建议类型**\n知识库新增\n\n"
            f"**分类**\n{ftype}\n\n"
            f"**变更点**\n{content[:300]}"
        )
    cmd = item.get("command", "").strip()
    lines = [f"**建议命令**\n`{cmd}`"]
    lines.append(f"\n**说明**\n{item.get('description') or '—'}")
    m = re.match(r"^/risk\s+owner\s+(\d+)\s+(.+)$", cmd, re.I)
    if m:
        rid = int(m.group(1))
        new_owner = m.group(2).strip()
        fact = db.get_fact(rid)
        if fact:
            lines.append(
                f"\n**更新后预览（风险 #{rid}）**\n"
                f"标题：{fact.get('title','')}\n"
                f"负责人：~~{fact.get('owner') or '未设置'}~~ **{new_owner}**"
            )
        return "\n".join(lines)
    m = re.match(r"^/admin\s+fact\s+update\s+(\d+)\s+([a-z_]+)\s+(.+)$", cmd, re.I)
    if m:
        fid = int(m.group(1)); field = m.group(2).lower(); new_val = m.group(3).strip()
        fact = db.get_fact(fid)
        if fact:
            old = fact.get(field) if field in fact.keys() else ""
            lines.append(
                f"\n**更新后预览（条目 #{fid}）**\n"
                f"标题：{fact.get('title','')}\n"
                f"{field}：~~{old or '未设置'}~~ **{new_val}**"
            )
        return "\n".join(lines)
    m = re.match(r"^/todo\s+update\s+(\d+)\s+([a-z_]+)\s+(.+)$", cmd, re.I)
    if m:
        tid = int(m.group(1)); field = m.group(2).lower(); new_val = m.group(3).strip()
        todo = db.get_todo(tid)
        if todo:
            old = todo.get(field) if field in todo.keys() else ""
            lines.append(
                f"\n**更新后预览（待办 #T{tid}）**\n"
                f"标题：{todo.get('title','')}\n"
                f"{field}：~~{old or '未设置'}~~ **{new_val}**"
            )
        return "\n".join(lines)
    return "\n".join(lines + ["\n**更新后预览**\n请按命令执行结果为准。"])


# ── AI 建议（===SUGGESTIONS===）解析与保存 ──────────────────────

def _extract_suggestions(text: str) -> list[dict] | None:
    """从 AI 回复中解析 ===SUGGESTIONS=== 块，返回 items 或 None。"""
    start = text.find("===SUGGESTIONS===")
    end   = text.find("===END_SUGGESTIONS===")
    if start < 0 or end < 0 or end <= start:
        return None
    raw = text[start + len("===SUGGESTIONS==="):end].strip()
    try:
        data = json.loads(raw)
        items = data.get("items", [])
        return items if isinstance(items, list) else None
    except Exception:
        log.warning("failed to parse SUGGESTIONS JSON: %r", raw[:200])
        return None


def _strip_suggestions(text: str) -> str:
    """从 AI 回复中剔除 ===SUGGESTIONS=== 块。"""
    start = text.find("===SUGGESTIONS===")
    end   = text.find("===END_SUGGESTIONS===")
    if start >= 0 and end > start:
        text = (text[:start] + text[end + len("===END_SUGGESTIONS==="):]).strip()
    return text


def _enrich_suggestions(items: list[dict], project: str | None) -> list[dict]:
    """为建议条目补充 DB 查询信息（update 类型补充现有值和实体标题）。"""
    _ALLOWED_UPDATE_FIELDS = {"owner", "priority", "due_date", "status"}
    _VALID_STATUS = {"resolved", "active", "archived"}
    _VALID_PRIO   = {"high", "medium", "low"}
    _VALID_KINDS  = {"new_fact", "new_todo", "update_fact", "update_todo"}
    _VALID_TYPES  = {"risk", "issue", "blocker", "dependency", "milestone",
                     "decision", "knowledge", "team", "client", "process", "org"}

    enriched = []
    for raw in items:
        item = dict(raw)
        kind = item.get("kind", "")
        if kind not in _VALID_KINDS:
            continue

        item["project"] = project or "yadi"
        item.setdefault("status", "pending")

        if kind == "new_fact":
            if item.get("type") not in _VALID_TYPES:
                item["type"] = "knowledge"
            if item.get("priority") not in _VALID_PRIO:
                item["priority"] = ""

        elif kind == "new_todo":
            if item.get("priority") not in _VALID_PRIO:
                item["priority"] = "medium"

        elif kind in ("update_fact", "update_todo"):
            field = item.get("field", "")
            if field not in _ALLOWED_UPDATE_FIELDS:
                continue
            value = str(item.get("value", ""))
            if field == "status" and value not in _VALID_STATUS:
                continue
            if field == "priority" and value not in _VALID_PRIO:
                continue
            # Enrich with current entity info
            eid = item.get("id")
            if eid:
                if kind == "update_fact":
                    fact = db.get_fact(int(eid))
                    if fact:
                        item["entity_title"] = fact.get("title", "")
                        item["old_value"] = fact.get(field) or "—"
                    else:
                        continue  # skip non-existent entity
                else:
                    todo = db.get_todo(int(eid))
                    if todo:
                        item["entity_title"] = todo.get("title", "")
                        item["old_value"] = todo.get(field) or "—"
                    else:
                        continue

        enriched.append(item)
    return enriched[:10]


def _save_suggestion_item(item: dict) -> bool:
    """将一条 AI 建议写入数据库，返回是否成功。"""
    kind    = item.get("kind", "")
    project = item.get("project", "yadi")
    try:
        if kind == "new_fact":
            ftype = item.get("type", "knowledge")
            title = item.get("title", "") or item.get("body", "")[:20] or "未命名"
            db.add_fact(
                ftype, title, item.get("body", ""),
                priority=item.get("priority", ""),
                owner=item.get("owner", ""),
                due_date=item.get("due_date", ""),
                source="ai",
                project=project,
            )
        elif kind == "new_todo":
            db.add_todo(
                item.get("title", ""),
                body=item.get("body", ""),
                priority=item.get("priority", "medium"),
                owner=item.get("owner", ""),
                due_date=item.get("due_date", ""),
                project=project,
                source_fact_id=item.get("source_fact_id"),
                plan_id=item.get("plan_id"),
                source="ai",
            )
        elif kind == "update_fact":
            fid   = int(item.get("id", 0))
            field = item.get("field", "")
            value = item.get("value", "")
            if fid and field:
                db.update_fact(fid, **{field: value})
        elif kind == "update_todo":
            tid   = int(item.get("id", 0))
            field = item.get("field", "")
            value = item.get("value", "")
            if tid and field:
                db.update_todo(tid, **{field: value})
        elif kind == "merge_fact":
            _apply_merge_item({
                "keep_id":     item.get("keep_id"),
                "merge_ids":   item.get("merge_ids", []),
                "append_text": item.get("append_text", ""),
                "reason":      item.get("reason", ""),
            })
        elif kind == "review_action":
            _apply_review_action({
                "kind":     item.get("sub_kind"),
                "action":   item.get("action"),
                "id":       item.get("id"),
                "title":    item.get("title", ""),
                "body":     item.get("body", ""),
                "priority": item.get("priority", ""),
                "owner":    item.get("owner", ""),
                "due_date": item.get("due_date", ""),
            })
        return True
    except Exception:
        log.exception("_save_suggestion_item error kind=%s", kind)
        return False


def _card_suggestions_update(chat_id: str) -> dict:
    """返回刷新后的 AI 建议卡片 response dict。"""
    items = db.get_pending_commands(chat_id)
    return {"type": "raw", "data": feishu.build_ai_suggestions_card(items, chat_id)}


def _card_suggestion_save_one(value: dict, chat_id: str) -> dict:
    index = int(value.get("index", -1))
    items = db.get_pending_commands(chat_id)
    if index < 0 or index >= len(items):
        return feishu.card_skipped_response()
    item = items[index]
    if item.get("status") in ("saved", "skipped"):
        return {"toast": {"type": "info", "content": "已处理"},
                "card": _card_suggestions_update(chat_id)}
    _save_suggestion_item(item)
    item["status"] = "saved"
    items[index] = item
    db.save_pending_commands(chat_id, items)
    return {
        "toast": {"type": "success", "content": f"已保存：{item.get('title', '')[:20]}"},
        "card": {"type": "raw", "data": feishu.build_ai_suggestions_card(items, chat_id)},
    }


def _card_suggestion_skip_one(value: dict, chat_id: str) -> dict:
    index = int(value.get("index", -1))
    items = db.get_pending_commands(chat_id)
    if index < 0 or index >= len(items):
        return feishu.card_skipped_response()
    item = items[index]
    if item.get("status") in ("saved", "skipped"):
        return {"toast": {"type": "info", "content": "已处理"},
                "card": _card_suggestions_update(chat_id)}
    item["status"] = "skipped"
    items[index] = item
    db.save_pending_commands(chat_id, items)
    return {
        "toast": {"type": "info", "content": f"已跳过：{item.get('title', '')[:20]}"},
        "card": {"type": "raw", "data": feishu.build_ai_suggestions_card(items, chat_id)},
    }


def _card_suggestion_save_all(value: dict, chat_id: str) -> dict:
    items = db.get_pending_commands(chat_id)
    n_saved = 0
    for i, item in enumerate(items):
        if item.get("status") not in ("saved", "skipped"):
            _save_suggestion_item(item)
            item["status"] = "saved"
            items[i] = item
            n_saved += 1
    db.save_pending_commands(chat_id, items)
    return {
        "toast": {"type": "success", "content": f"已全部保存（{n_saved} 项）"},
        "card": {"type": "raw", "data": feishu.build_ai_suggestions_card(items, chat_id)},
    }


def _card_suggestion_skip_all(value: dict, chat_id: str) -> dict:
    items = db.get_pending_commands(chat_id)
    n_skipped = 0
    for i, item in enumerate(items):
        if item.get("status") not in ("saved", "skipped"):
            item["status"] = "skipped"
            items[i] = item
            n_skipped += 1
    db.save_pending_commands(chat_id, items)
    return {
        "toast": {"type": "info", "content": f"已全部跳过（{n_skipped} 项）"},
        "card": {"type": "raw", "data": feishu.build_ai_suggestions_card(items, chat_id)},
    }


async def _send_ai_suggestions_card(chat_id: str, suggestions: list[dict]):
    """发送 AI 建议确认卡片（使用 pending_commands 存储）。"""
    db.save_pending_commands(chat_id, suggestions)
    card = feishu.build_ai_suggestions_card(suggestions, chat_id)
    await feishu.send_reply(chat_id, card, FEISHU_APP_ID, FEISHU_APP_SECRET)
    log.info("sent %d AI suggestions to chat_id=%s", len(suggestions), chat_id)


async def _handle_card_trigger(event: dict) -> dict:
    try:
        value = event.get("action", {}).get("value", {})
        action = value.get("action")
        chat_id = value.get("chat_id", "")
        log.info("card trigger action=%s chat_id=%s", action, chat_id)

        # ── 注册审批 ──
        if action == "approve_user":
            open_id = value.get("open_id", "")
            name    = value.get("name", "")
            role    = value.get("role", "member")
            project = value.get("project", "")
            existing = db.get_user(open_id)
            if existing and existing.get("status") == "active":
                return {
                    "toast": {"type": "info", "content": "已由其他管理员审批通过"},
                    "card": {"type": "raw", "data": {
                        "schema": "2.0",
                        "config": {"enable_forward": False},
                        "body": {"elements": [{"tag": "markdown",
                                               "content": f"✅ {name} 已审批通过（重复操作已忽略）"}]},
                    }},
                }
            db.update_user(open_id, role=role, project=project, status="active")
            role_zh = _ROLE_ZH.get(role, role)
            await feishu.send_reply_to_user(
                open_id,
                f"🎉 你的注册申请已通过！\n角色：{role_zh}\n项目：{project}\n\n"
                f"现在可以直接 @Bot 与我对话了，发 /help 查看可用命令。",
                FEISHU_APP_ID, FEISHU_APP_SECRET,
            )
            log.info("approved user %s name=%s role=%s project=%s", open_id, name, role, project)
            return feishu.card_approved_response(name, role, project)

        if action == "reject_user":
            open_id = value.get("open_id", "")
            name    = value.get("name", "")
            existing = db.get_user(open_id)
            if existing and existing.get("status") in ("rejected", "active"):
                return {
                    "toast": {"type": "info", "content": "该申请已处理"},
                    "card": {"type": "raw", "data": {
                        "schema": "2.0",
                        "config": {"enable_forward": False},
                        "body": {"elements": [{"tag": "markdown",
                                               "content": "该申请已处理（重复操作已忽略）"}]},
                    }},
                }
            db.update_user(open_id, status="rejected")
            await feishu.send_reply_to_user(
                open_id,
                "很抱歉，你的注册申请已被拒绝。如有疑问请联系管理员。",
                FEISHU_APP_ID, FEISHU_APP_SECRET,
            )
            log.info("rejected user %s name=%s", open_id, name)
            return feishu.card_rejected_response(name)

        # ── 合并确认卡片 ──
        if action == "merge_one":
            return _card_merge_one(value, chat_id)
        if action == "merge_all":
            return _card_merge_all(value, chat_id)
        if action == "skip_merges":
            db.clear_pending_merges(chat_id)
            return feishu.card_merge_skipped_response()

        # ── 风险/待办处理确认卡片 ──
        if action == "review_action_one":
            return _card_review_action_one(value, chat_id)
        if action == "review_action_all":
            return _card_review_action_all(value, chat_id)
        if action == "skip_review_actions":
            db.clear_pending_actions(chat_id)
            return feishu.card_action_skipped_response()

        if action == "clarify_option":
            option_text  = value.get("text", "")
            chat_id      = value.get("chat_id", "")
            sender_oid   = value.get("sender_open_id", "")
            if not option_text or not chat_id:
                return feishu.card_skipped_response()
            db.clear_pending_clarify(chat_id)
            db.add_message(chat_id, "user", option_text)
            user = db.get_user(sender_oid) or {}
            project = _resolve_project(chat_id, user)
            asyncio.create_task(_clarify_and_respond(
                chat_id, sender_oid, user, project, option_text
            ))
            return {
                "toast": {"type": "info", "content": f"已选择：{option_text[:20]}"},
                "card": {"type": "raw", "data": feishu.build_md_card("⏳ 正在整合信息，稍候...")},
            }

        # ── 风险/待办详情查看 ──
        if action == "view_risk_detail":
            rid = int(value.get("id", 0))
            fact = db.get_fact(rid)
            if not fact or fact.get("dimension") != "risk":
                return {"toast": {"type": "error", "content": "找不到该条目"},
                        "card": {"type": "raw", "data": feishu.build_md_card("找不到该条目")}}
            open_todos = db.list_todos(status="open", source_fact_id=rid)
            detail = feishu.build_risk_show_card(dict(fact), [dict(t) for t in open_todos])
            return {"toast": {"type": "info", "content": f"已加载 #{rid} 详情"},
                    "card": {"type": "raw", "data": detail}}

        if action == "view_todo_detail":
            tid = int(value.get("id", 0))
            todo = db.get_todo(tid)
            if not todo:
                return {"toast": {"type": "error", "content": "找不到该待办"},
                        "card": {"type": "raw", "data": feishu.build_md_card("找不到该待办")}}
            todo = dict(todo)
            src = db.get_fact(todo["source_fact_id"]) if todo.get("source_fact_id") else None
            plan = db.get_fact(todo["plan_id"]) if todo.get("plan_id") else None
            detail = feishu.build_todo_show_card(todo, dict(src) if src else None, dict(plan) if plan else None)
            return {"toast": {"type": "info", "content": f"已加载 #T{tid} 详情"},
                    "card": {"type": "raw", "data": detail}}

        if action == "view_fact_detail":
            fid = int(value.get("id", 0))
            fact = db.get_fact(fid)
            if not fact:
                return {"toast": {"type": "error", "content": "找不到该条目"},
                        "card": {"type": "raw", "data": feishu.build_md_card("找不到该条目")}}
            detail = feishu.build_fact_show_card(dict(fact))
            return {"toast": {"type": "info", "content": f"已加载 #{fid} 详情"},
                    "card": {"type": "raw", "data": detail}}

        # ── AI 建议确认卡片 ──
        if action == "suggestion_save_one":
            return _card_suggestion_save_one(value, chat_id)
        if action == "suggestion_skip_one":
            return _card_suggestion_skip_one(value, chat_id)
        if action == "suggestion_save_all":
            return _card_suggestion_save_all(value, chat_id)
        if action == "suggestion_skip_all":
            return _card_suggestion_skip_all(value, chat_id)
        if action == "suggestion_view_detail":
            index = int(value.get("index", -1))
            items = db.get_pending_commands(chat_id)
            if index < 0 or index >= len(items):
                return feishu.card_skipped_response()
            return {
                "toast": {"type": "info", "content": "已打开详情"},
                "card": {"type": "raw", "data": feishu.build_suggestion_detail_card(items[index], chat_id, index)},
            }
        if action == "suggestion_back_to_list":
            return {
                "toast": {"type": "info", "content": "已返回建议清单"},
                "card": _card_suggestions_update(chat_id),
            }

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


async def _handle_bot_menu(event: dict):
    """处理机器人快捷菜单点击事件（application.bot.menu_v6）。"""
    try:
        open_id = event.get("operator", {}).get("operator_id", {}).get("open_id", "")
        event_key = event.get("event_key", "")
        if not open_id or not event_key:
            return

        log.info("bot_menu event_key=%s open_id=%s", event_key, open_id)

        async def send(content):
            await feishu.send_reply_to_user(open_id, content, FEISHU_APP_ID, FEISHU_APP_SECRET)

        user = _get_or_init_user(open_id, "")
        user_role = user.get("role", "unknown")
        user_status = user.get("status", "unknown")

        # 全员可用
        if event_key == "show_help":
            await send(_help_text(user_role))
            return
        if event_key == "show_version":
            await send(f"pm-assist v{_VERSION}")
            return

        # 需要已注册且 active
        if user_role in ("unknown", "pending") or user_status != "active":
            status_msgs = {
                "pending": "你的注册申请正在等待管理员审批，批准后即可使用全部功能。",
                "rejected": "你的注册申请已被拒绝，如有疑问请联系管理员。",
            }
            await send(status_msgs.get(user_status, "你尚未注册，请发送 /start 开始注册。"))
            return

        # open_id 作为 chat_id（菜单事件无 chat_id，group binding 不适用，回落到用户绑定项目）
        chat_id = open_id
        project = _resolve_project(chat_id, user)

        if event_key == "clear_chat":
            db.clear_history(chat_id)
            db.clear_pending(chat_id)
            db.clear_pending_todos(chat_id)
            db.clear_pending_commands(chat_id)
            await send("对话历史已清除。")
            return

        # 查看类菜单：按角色分级访问
        if event_key in ("view_todos", "view_risks", "view_schedule"):
            # super_admin 无项目绑定时查全量；其他角色按绑定项目过滤
            query_project = project if user.get("project") else None
            if event_key == "view_schedule":
                # member/pm/super_admin 均可查看里程碑；super_admin 无绑定时 query_project=None 查全量
                await send(_handle_schedule([], project=query_project))
            else:
                # view_todos / view_risks：仅 pm 和 super_admin
                if user_role not in ("pm", "super_admin"):
                    await send("此功能仅限项目经理PM和管理员使用。")
                    return
                if event_key == "view_todos":
                    await send(_handle_todo("/todo list", project=query_project))
                else:
                    await send(_handle_admin_risk(["list", "open"], project=query_project))
            return

        # PM + 管理员：查看早报
        if event_key == "view_morning_report":
            if user_role not in ("pm", "super_admin"):
                await send("此功能仅限项目经理PM和管理员使用。")
                return
            review = db.get_latest_nightly_review()
            review_text = _strip_merge_candidates_json(review) if review else None
            today = datetime.now().strftime("%m月%d日")
            query_project = project if user.get("project") else None
            risks = db.list_risks(status="open", project=query_project)
            card = feishu.build_morning_report_card(query_project or "", risks, review_text, today)
            await send(card)
            return

        # PM + 管理员：AI 洗盘
        if event_key == "run_review":
            if user_role not in ("pm", "super_admin"):
                await send("此功能仅限项目经理PM和管理员使用。")
                return
            await send("开始 AI 洗盘，完成后会发送报告。")
            result = await _handle_review_run("/review run")
            await send(result)
            return

        # 人员信息查询：member/pm/super_admin 均可，按角色展示不同内容
        if event_key == "admin_users":
            my_user = db.get_user(open_id)
            if user_role == "super_admin":
                all_users = [dict(u) for u in db.list_users()]
                card = feishu.build_user_info_card(my_user or {}, all_users=all_users)
            elif user_role == "pm":
                my_project = (my_user or {}).get("project", "")
                if my_project:
                    members = [dict(u) for u in db.list_users()
                               if u.get("project") == my_project and u.get("open_id") != open_id]
                else:
                    members = []
                card = feishu.build_user_info_card(my_user or {}, members=members)
            else:
                card = feishu.build_user_info_card(my_user or {})
            await send(card)
            return

        # 管理员专用
        if user_role != "super_admin":
            await send("无权限：此操作仅限管理员使用。")
            return

    except Exception:
        log.exception("_handle_bot_menu error event_key=%s", event.get("event_key", ""))


def _resolve_project(chat_id: str, user: dict) -> str | None:
    """确定本次对话的项目：群聊绑定 > 用户绑定 > 管理员返回 None（全量）> 默认。"""
    binding = db.get_chat_binding(chat_id)
    if binding:
        return binding
    p = user.get("project", "")
    if p:
        return p
    if user.get("role") == "super_admin":
        return None  # admin without binding sees cross-project data
    return "默认"


def _get_or_init_user(open_id: str, name: str) -> dict:
    """获取用户信息；若是 .env 里配置的 admin 则自动注册为 super_admin。"""
    user = db.get_user(open_id)
    if user:
        # 同步姓名（飞书姓名可能变动）
        if name and user.get("name") != name:
            db.update_user(open_id, name=name)
            user["name"] = name
        return user
    # 首次出现：env 中的 admin 直接注册为 super_admin（active）
    if open_id in ADMIN_OPEN_IDS:
        db.upsert_user(open_id, name=name, role="super_admin", status="active")
        return db.get_user(open_id) or {"open_id": open_id, "name": name,
                                         "role": "super_admin", "project": "", "status": "active"}
    return {"open_id": open_id, "name": name, "role": "unknown",
            "project": "", "status": "unknown"}


def _sender_info(user: dict) -> str:
    """生成注入 AI 的说话人描述文字。"""
    role = user.get("role", "unknown")
    name = user.get("name", "未知用户")
    project = user.get("project", "")
    proj_tag = f"（{project}项目）" if project else ""
    if role == "super_admin":
        proj_tag = f"，当前关注项目：{project}" if project else "，全局视图（未绑定项目）"
        return f"管理员-{name}（最高权限，可询问系统数据和数据库信息{proj_tag}）"
    if role == "pm":
        return f"项目经理PM-{name}{proj_tag}"
    if role == "member":
        return f"项目成员-{name}{proj_tag}"
    return f"{name}（未注册用户）"


_CONFIRM_SAVE_TEXTS = {
    "保存", "确认", "确认保存", "全部保存", "保存全部", "同意", "可以", "执行", "全部执行",
    "更新", "确认更新", "保存吧", "存", "存一下",
}
_CONFIRM_SKIP_TEXTS = {"跳过", "取消", "不保存", "不用保存", "忽略", "算了"}


def _normalized_confirm_text(text: str) -> str:
    return re.sub(r"[\s。.!！?？]+", "", text.strip().lower())


def _has_pending_confirmation(chat_id: str) -> bool:
    return any((
        db.get_pending(chat_id),
        db.get_pending_todos(chat_id),
        db.get_pending_commands(chat_id),
        db.get_pending_merges(chat_id),
        db.get_pending_actions(chat_id),
    ))


def _clear_all_pending_confirmations(chat_id: str):
    db.clear_pending(chat_id)
    db.clear_pending_todos(chat_id)
    db.clear_pending_commands(chat_id)
    db.clear_pending_merges(chat_id)
    db.clear_pending_actions(chat_id)


async def _handle_text_confirmation(chat_id: str, text: str) -> bool:
    normalized = _normalized_confirm_text(text)
    if normalized not in _CONFIRM_SAVE_TEXTS and normalized not in _CONFIRM_SKIP_TEXTS:
        return False

    if normalized in _CONFIRM_SKIP_TEXTS:
        if _has_pending_confirmation(chat_id):
            _clear_all_pending_confirmations(chat_id)
            await feishu.send_reply(chat_id, "已跳过当前待确认项。", FEISHU_APP_ID, FEISHU_APP_SECRET)
        else:
            await feishu.send_reply(chat_id, "当前没有待确认项。", FEISHU_APP_ID, FEISHU_APP_SECRET)
        return True

    fact_items = db.get_pending(chat_id)
    todo_items = db.get_pending_todos(chat_id)
    command_items = db.get_pending_commands(chat_id)
    merge_items = db.get_pending_merges(chat_id)
    action_items = db.get_pending_actions(chat_id)

    if not any((fact_items, todo_items, command_items, merge_items, action_items)):
        await feishu.send_reply(chat_id, "当前没有待确认项；需要保存时请点击确认卡片，或先发具体内容。", FEISHU_APP_ID, FEISHU_APP_SECRET)
        return True

    saved_parts: list[str] = []
    for item in fact_items:
        _save_fact_item(item)
    if fact_items:
        db.clear_pending(chat_id)
        saved_parts.append(f"知识库 {len(fact_items)} 条")

    for item in todo_items:
        _save_todo_item(item)
    if todo_items:
        db.clear_pending_todos(chat_id)
        saved_parts.append(f"待办 {len(todo_items)} 条")

    saved_suggestions = [i for i in command_items if i.get("status") not in ("saved", "skipped")]
    for item in saved_suggestions:
        _save_suggestion_item(item)
    if saved_suggestions:
        db.clear_pending_commands(chat_id)
        saved_parts.append(f"AI 建议 {len(saved_suggestions)} 项")

    for item in merge_items:
        _apply_merge_item(item)
    if merge_items:
        db.clear_pending_merges(chat_id)
        saved_parts.append(f"合并 {len(merge_items)} 组")

    for item in action_items:
        _apply_review_action(item)
    if action_items:
        db.clear_pending_actions(chat_id)
        saved_parts.append(f"处理建议 {len(action_items)} 项")

    await feishu.send_reply(chat_id, "已确认：" + "、".join(saved_parts), FEISHU_APP_ID, FEISHU_APP_SECRET)
    return True


async def _handle_message(event: dict):
    message = event.get("message", {})
    sender = event.get("sender", {})

    msg_type = message.get("message_type", "")
    chat_id = message.get("chat_id", "")
    sender_open_id = sender.get("sender_id", {}).get("open_id", "")

    log.info("chat_id=%s sender=%s msg_type=%s", chat_id, sender_open_id, msg_type)

    if msg_type not in ("text", "post"):
        return

    raw = json.loads(message.get("content", "{}"))

    if msg_type == "post":
        # 飞书 post 消息：直接是 {"title":..., "content":[...]}，无语言包装层
        # content 外层为段落数组，内层为行内节点；段落间用 \n 分隔保留排版
        post_body = raw.get("zh_cn") or raw.get("en_us") or raw
        para_texts = []
        for paragraph in post_body.get("content", []):
            inline = []
            for node in paragraph:
                tag = node.get("tag", "")
                if tag == "text":
                    inline.append(node.get("text", ""))
                elif tag == "at":
                    uid  = node.get("user_id", "")
                    uname = node.get("user_name", "")
                    if uid and uname:
                        db.upsert_person(uid, uname)
                    if not node.get("is_bot", False):
                        inline.append(f"@{uname}" if uname else "")
                elif tag == "a":
                    inline.append(node.get("text", ""))
                # img 等其他 tag 跳过
            para_texts.append("".join(inline))
        text = "\n".join(para_texts).strip()
        # 去掉 post 消息开头的 @Bot（飞书群里 @Bot 会在富文本首节点）
        # 注：post 消息的 mentions 字段与 text 消息相同
        for mention in message.get("mentions", []):
            if mention.get("is_bot", False):
                key = mention.get("key", "")
                if key:
                    text = text.replace(key, "").strip()
            else:
                open_id = mention.get("id", {}).get("open_id", "")
                name = mention.get("name", "")
                if open_id and name:
                    db.upsert_person(open_id, name)
    else:
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

    # 通过飞书消息中的发送者信息获取姓名
    sender_name = sender.get("sender_id", {}).get("name", "") or ""

    log.info("text: %r", text)
    if not text:
        return

    # 获取/初始化用户信息
    user = _get_or_init_user(sender_open_id, sender_name)
    user_role = user.get("role", "unknown")
    user_status = user.get("status", "unknown")

    # ── 所有人可用命令 ──
    if text == "/help":
        await feishu.send_reply(chat_id, _help_text(user_role), FEISHU_APP_ID, FEISHU_APP_SECRET)
        return

    if text == "/version":
        await feishu.send_reply(chat_id, f"pm-assist v{_VERSION}", FEISHU_APP_ID, FEISHU_APP_SECRET)
        return

    if text == "/start":
        await feishu.send_reply(chat_id, _register_text(), FEISHU_APP_ID, FEISHU_APP_SECRET)
        return

    if text.startswith("/join"):
        reply = await _handle_join(text, sender_open_id, user)
        await feishu.send_reply(chat_id, reply, FEISHU_APP_ID, FEISHU_APP_SECRET)
        return

    if text == "/leave":
        reply = _handle_leave(sender_open_id, user)
        await feishu.send_reply(chat_id, reply, FEISHU_APP_ID, FEISHU_APP_SECRET)
        return

    # ── 未注册/待审批用户：仅允许 /start /join /help /version ──
    if user_role in ("unknown", "pending") or user_status not in ("active",):
        if user_status == "pending":
            msg = "你的注册申请正在等待管理员审批，批准后即可使用全部功能。"
        elif user_status == "rejected":
            msg = "你的注册申请已被拒绝，如有疑问请联系管理员。"
        else:
            msg = "你尚未注册，请发送 /start 开始注册。"
        await feishu.send_reply(chat_id, msg, FEISHU_APP_ID, FEISHU_APP_SECRET)
        return

    # 确定本次对话所属项目
    project = _resolve_project(chat_id, user)

    # ── 管理员专用命令 ──
    if user_role in ("pm", "super_admin") and await _handle_text_confirmation(chat_id, text):
        return

    if text.startswith("/admin"):
        if user_role != "super_admin":
            await feishu.send_reply(chat_id, "无权限：/admin 命令仅限管理员使用。",
                                   FEISHU_APP_ID, FEISHU_APP_SECRET)
            return
        if text.startswith("/admin fact decompose"):
            reply = await _handle_admin_fact_decompose(text, chat_id=chat_id)
        elif text.startswith("/admin review run"):
            await feishu.send_reply(chat_id, "开始 AI 洗盘，完成后会发送报告。", FEISHU_APP_ID, FEISHU_APP_SECRET)
            reply = await _handle_admin_review_run(text, chat_id=chat_id)
        elif text.startswith("/admin user approve ") or text.startswith("/admin user reject "):
            reply = await _handle_admin_user_approve_reject(text)
        else:
            reply = _handle_admin(text, sender_open_id, project, chat_id)
        await feishu.send_reply(chat_id, reply, FEISHU_APP_ID, FEISHU_APP_SECRET)
        return

    # ── member / PM / 管理员：里程碑查看 ──
    if text.startswith("/schedule"):
        schedule_args = text.split(None, 1)[1].split() if len(text.split(None, 1)) > 1 else []
        reply = _handle_schedule(schedule_args, project=project)
        await feishu.send_reply(chat_id, reply, FEISHU_APP_ID, FEISHU_APP_SECRET)
        return

    # ── PM / 管理员：风险管理 ──
    if text.startswith("/risk"):
        if user_role not in ("pm", "super_admin"):
            await feishu.send_reply(chat_id, "此命令仅限项目经理PM和管理员使用。",
                                   FEISHU_APP_ID, FEISHU_APP_SECRET)
            return
        risk_args = text.split(None, 1)[1].split() if len(text.split(None, 1)) > 1 else []
        reply = _handle_admin_risk(risk_args, project=project)
        await feishu.send_reply(chat_id, reply, FEISHU_APP_ID, FEISHU_APP_SECRET)
        return

    # ── PM / 管理员：AI 洗盘 ──
    if text.startswith("/review"):
        if user_role not in ("pm", "super_admin"):
            await feishu.send_reply(chat_id, "此命令仅限项目经理PM和管理员使用。",
                                   FEISHU_APP_ID, FEISHU_APP_SECRET)
            return
        parts = text.split()
        sub = parts[1].lower() if len(parts) > 1 else ""
        if sub == "run":
            await feishu.send_reply(chat_id, "开始 AI 洗盘，完成后会发送报告。",
                                   FEISHU_APP_ID, FEISHU_APP_SECRET)
            reply = await _handle_review_run(text)
        else:
            reply = (
                "洗盘命令：\n"
                "/review run             立即洗盘，报告和建议卡片发送给所有管理员和 PM\n"
                "/review run report      按仅报告模式执行一次\n"
                "/review run direct      按直接清洗模式执行一次\n\n"
                "（洗盘模式设置请联系管理员使用 /admin review mode）"
            )
        await feishu.send_reply(chat_id, reply, FEISHU_APP_ID, FEISHU_APP_SECRET)
        return

    # ── PM / 管理员专用命令 ──
    if text.startswith("/note ") or text.startswith("/todo"):
        if user_role not in ("pm", "super_admin"):
            await feishu.send_reply(chat_id, "此命令仅限项目经理PM和管理员使用。",
                                   FEISHU_APP_ID, FEISHU_APP_SECRET)
            return
        if text.startswith("/note "):
            note = text[6:].strip()
            if note:
                bid = db.add_fact("knowledge", f"笔记#{db.count_notes() + 1}",
                                  note, source="manual", project=project)
                await feishu.send_reply(chat_id, f"✓ 已记录 (ID:{bid})", FEISHU_APP_ID, FEISHU_APP_SECRET)
            return
        if text.startswith("/todo"):
            reply = _handle_todo(text, project=project)
            await feishu.send_reply(chat_id, reply, FEISHU_APP_ID, FEISHU_APP_SECRET)
            return

    if text == "/clear":
        db.clear_history(chat_id)
        _clear_all_pending_confirmations(chat_id)
        db.clear_pending_clarify(chat_id)
        await feishu.send_reply(chat_id, "对话历史已清除。", FEISHU_APP_ID, FEISHU_APP_SECRET)
        return

    # ── AI 对话（member/pm/super_admin 均可，上下文深度不同）──

    # 若有待处理澄清问题，把上下文拼入用户消息，然后清除
    pending_clarify = db.get_pending_clarify(chat_id)
    if pending_clarify:
        db.clear_pending_clarify(chat_id)
        clarify_ctx = pending_clarify.get("context", "")
        if clarify_ctx:
            text = f"[补充信息：{text}]（关于之前的问题：{clarify_ctx}）"

    db.add_message(chat_id, "user", text)
    history = db.get_history(chat_id, MAX_HISTORY)
    context = db.get_full_context(project)
    if user_role == "super_admin":
        context["users"] = db.get_users_summary()
    sender_info_str = _sender_info(user)

    # 先发"思考中"卡片占位，拿到 message_id 以便后续原地更新
    msg_id = ""
    try:
        msg_id = await feishu.send_card_return_id(
            chat_id, feishu.build_thinking_card(), FEISHU_APP_ID, FEISHU_APP_SECRET
        )
    except Exception:
        log.warning("failed to send thinking indicator for chat_id=%s", chat_id)

    try:
        reply = await asyncio.wait_for(
            ai_client.chat(history, context,
                               sender_info=sender_info_str, role=user_role),
            timeout=90.0,
        )
    except asyncio.TimeoutError:
        reply = "AI 响应超时，请稍后重试。"
        log.warning("AI chat timeout chat_id=%s", chat_id)
    except Exception:
        reply = "AI 响应出错，请稍后重试。"
        log.exception("AI chat error chat_id=%s", chat_id)

    # 检查 AI 是否附带了澄清问题和建议
    clarify_data = _extract_clarify(reply)
    clean_reply  = _strip_clarify(reply)
    suggestions  = _extract_suggestions(clean_reply)
    has_block = "===SUGGESTIONS===" in reply
    log.info("suggestions_found=%s has_sugg_block=%s n_items=%s role=%s reply_tail=%r",
             bool(suggestions), has_block,
             len(suggestions) if suggestions else 0,
             user_role, reply[-300:])
    if not has_block:
        log.info("AI did not include SUGGESTIONS block")
    elif suggestions is None:
        log.warning("SUGGESTIONS block found but JSON parse failed: %r",
                    reply[reply.find("===SUGGESTIONS==="):reply.find("===SUGGESTIONS===")+300])
    clean_reply  = _strip_suggestions(clean_reply)
    # 先存原始干净回复到历史（避免 sanitize 文本污染对话上下文）
    db.add_message(chat_id, "assistant", clean_reply)
    # 仅在展示给用户时才加"已执行"误导性措辞警告
    will_send_card = bool(suggestions) and user_role in ("pm", "super_admin")
    display_reply = _sanitize_ai_execution_claims(clean_reply, will_send_card=will_send_card)

    # 用实际回复卡片原地更新占位消息
    reply_card = feishu.build_md_card(display_reply)
    if msg_id:
        updated = await feishu.update_message_card(msg_id, reply_card, FEISHU_APP_ID, FEISHU_APP_SECRET)
        if not updated:
            log.warning("update card failed, sending new card chat_id=%s msg_id=%s", chat_id, msg_id)
            await feishu.send_reply(chat_id, reply_card, FEISHU_APP_ID, FEISHU_APP_SECRET)
    else:
        await feishu.send_reply(chat_id, reply_card, FEISHU_APP_ID, FEISHU_APP_SECRET)

    # 发送 AI 建议确认卡片（PM / 管理员）
    if suggestions and user_role in ("pm", "super_admin"):
        enriched = _enrich_suggestions(suggestions, project)
        log.info("enriched suggestions count=%d raw=%d", len(enriched), len(suggestions))
        if enriched:
            await _send_ai_suggestions_card(chat_id, enriched)

    # 如果 AI 附带了澄清问题，额外发送问题卡片并记录待处理状态
    if clarify_data and user_role in ("pm", "super_admin"):
        question = clarify_data.get("q", "")
        opts     = clarify_data.get("opts", [])
        if question:
            db.save_pending_clarify(chat_id, question, context=text)
            clarify_card = feishu.build_clarify_card(
                question, opts, chat_id, sender_open_id=sender_open_id
            )
            await feishu.send_reply(chat_id, clarify_card, FEISHU_APP_ID, FEISHU_APP_SECRET)
            log.info("clarify card sent chat_id=%s question=%r", chat_id, question[:60])


def _register_text() -> str:
    projects = db.list_projects(active_only=True)
    if projects:
        proj_lines = "\n".join(
            f"  {i+1}. {p['name']}" + (f"（{p['description']}）" if p["description"] else "")
            for i, p in enumerate(projects)
        )
    else:
        proj_lines = "  （暂无项目，请联系管理员创建）"
    return (
        "📋 pm-assist 注册系统\n\n"
        f"可加入的项目：\n{proj_lines}\n\n"
        "注册步骤：\n"
        "1. 发送 /join [项目名] [pm|member] 申请加入\n"
        "   例：/join 雅迪 pm\n"
        "2. 等待管理员审批（审批后会收到通知）\n"
        "3. 批准后即可使用完整功能\n\n"
        "角色说明：\n"
        "- pm：项目经理，可使用完整PM工作功能（风险管理、待办、AI辅助等）\n"
        "- member：普通成员，可与AI对话咨询团队/项目问题"
    )


async def _handle_join(text: str, sender_open_id: str, user: dict) -> str:
    """处理 /join [项目] [pm|member] 申请注册。"""
    parts = text.split()
    if len(parts) < 3:
        return "用法：/join [项目名] [pm|member]\n例：/join 雅迪 pm\n\n发 /start 查看可用项目列表。"

    project_name = parts[1]
    role_req = parts[2].lower()
    if role_req not in ("pm", "member"):
        return "角色只能是 pm 或 member\n例：/join 雅迪 pm"

    # 验证项目存在
    project = db.get_project_by_name(project_name)
    if not project or not project.get("active"):
        projects = db.list_projects(active_only=True)
        names = "、".join(p["name"] for p in projects) if projects else "（暂无）"
        return f"找不到项目「{project_name}」。\n当前可用项目：{names}\n\n发 /start 查看详情。"

    # 已是 super_admin：直接绑定项目，无需审批
    if user.get("role") == "super_admin":
        db.update_user(sender_open_id, project=project_name)
        return f"✓ 已将你的项目绑定改为「{project_name}」，之后的 AI 对话将使用该项目上下文。\n（管理员可随时用 /join [项目名] 切换项目，或 /admin user project [open_id] - 清除绑定）"

    # 已经是 active 用户
    if user.get("status") == "active" and user.get("project"):
        role_zh = _ROLE_ZH.get(user.get("role", ""), user.get("role", ""))
        return (f"你已注册为「{role_zh}」，绑定项目「{user.get('project', '')}」。\n"
                "如需变更角色或项目，请联系管理员。")

    # 已有 pending 申请
    if user.get("status") == "pending":
        return "你已有一条待审批的申请，请等待管理员处理。"

    # 获取发送者姓名（从 org_units 缓存，或调飞书 API 查询，最后 fallback 到 open_id 前缀）
    name = user.get("name", "")
    if not name:
        name = await feishu.get_user_name(sender_open_id, FEISHU_APP_ID, FEISHU_APP_SECRET)
    if not name:
        name = sender_open_id[:8]

    # 写入 pending 状态
    db.upsert_user(sender_open_id, name=name, role=role_req,
                   project=project_name, status="pending")

    # 发审批卡片给所有管理员
    try:
        card = feishu.build_approval_card(sender_open_id, name, role_req, project_name)
        for admin_id in ADMIN_OPEN_IDS:
            await feishu.send_card_to_user(admin_id, card, FEISHU_APP_ID, FEISHU_APP_SECRET)
        log.info("approval card sent to %d admins for %s role=%s project=%s",
                 len(ADMIN_OPEN_IDS), sender_open_id, role_req, project_name)
    except Exception:
        log.exception("failed to send approval card")

    role_zh = _ROLE_ZH.get(role_req, role_req)
    return (f"✅ 申请已提交！\n角色：{role_zh}\n项目：{project_name}\n\n"
            "等待管理员审批，批准后会收到通知。")


def _handle_leave(sender_open_id: str, user: dict) -> str:
    """处理 /leave：用户退出当前项目绑定。"""
    role = user.get("role", "unknown")
    if role == "super_admin":
        return "管理员无需退出项目，项目绑定不适用于管理员角色。"
    if role == "unknown" or user.get("status") != "active":
        return "你尚未加入任何项目，无需退出。"
    project = user.get("project", "")
    if not project:
        return "你当前没有绑定任何项目。"
    db.update_user(sender_open_id, project="", role="member")
    name = user.get("name", sender_open_id[:8])
    return (f"✅ 已退出项目「{project}」。\n"
            f"角色已变更为普通成员，AI 对话仍可继续使用。\n"
            f"如需加入新项目，发 /start 查看可用项目列表。")


async def _clarify_and_respond(chat_id: str, sender_oid: str, user: dict,
                               project: str, user_text: str):
    """用户回答了澄清问题后，重新调用 AI 并发送结果。"""
    try:
        history = db.get_history(chat_id, MAX_HISTORY)
        context = db.get_full_context(project)
        if user.get("role") == "super_admin":
            context["users"] = db.get_users_summary()
        sender_info = _sender_info(user)
        role = user.get("role", "pm")
        reply = await asyncio.wait_for(
            ai_client.chat(history, context, sender_info=sender_info, role=role),
            timeout=90.0,
        )
        reply = _strip_clarify(reply)
        reply = _sanitize_ai_execution_claims(reply)
        db.add_message(chat_id, "assistant", reply)
        await feishu.send_reply(chat_id, feishu.build_md_card(reply), FEISHU_APP_ID, FEISHU_APP_SECRET)
    except Exception:
        log.exception("_clarify_and_respond error chat_id=%s", chat_id)


async def _extract_and_card(chat_id: str, text: str, project: str = "默认"):
    return  # Replaced by ===SUGGESTIONS=== flow in AI response
    try:
        items = await ai_client.extract_facts(text)
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
                    "project": project,
                })
            else:
                enriched.append({**item, "action": "new", "project": project})
        existing = db.get_pending_commands(chat_id)
        fact_suggestions = []
        for item in enriched[:10]:
            title = "更新知识库条目" if item.get("action") == "update" else "新增知识库条目"
            desc = (f"目标：#{item.get('fact_id')}《{item.get('fact_title','')}》"
                    if item.get("action") == "update"
                    else f"类型：{item.get('type','knowledge')}")
            fact_suggestions.append({
                "suggestion_kind": "fact",
                "title": title,
                "description": desc,
                "command_type": "fact",
                "status": "pending",
                "fact_item": item,
            })
        merged = existing + fact_suggestions
        db.save_pending_commands(chat_id, merged)
        # Avoid duplicate cards: if command suggestions already produced a card in this turn,
        # only merge data into pending list and do not send a second card here.
        if existing:
            return
        sent = await feishu.send_command_confirm_card(chat_id, merged, FEISHU_APP_ID, FEISHU_APP_SECRET)
        if not sent:
            await feishu.send_reply(
                chat_id,
                "检测到可保存的知识条目，但确认卡片发送失败。请回复“保存”直接入库，或稍后重试。",
                FEISHU_APP_ID, FEISHU_APP_SECRET,
            )
    except Exception:
        log.exception("extract_and_card error")


async def _extract_todos_and_card(chat_id: str, text: str, project: str = "默认"):
    return  # Replaced by ===SUGGESTIONS=== flow in AI response
    try:
        todos = await ai_client.extract_todo_intent(text)
        if not todos:
            return
        for t in todos:
            t["project"] = project
        db.save_pending_todos(chat_id, todos)
        sent = await feishu.send_todo_confirm_card(chat_id, todos, FEISHU_APP_ID, FEISHU_APP_SECRET)
        if not sent:
            await feishu.send_reply(
                chat_id,
                "检测到待办建议，但确认卡片发送失败。请回复“保存”直接入库，或稍后重试。",
                FEISHU_APP_ID, FEISHU_APP_SECRET,
            )
    except Exception:
        log.exception("extract_todos_and_card error")


# ── Todo 命令（所有用户）────────────────────────────────────

def _todo_help() -> str:
    return (
        "待办事项命令：\n"
        "/todo list                        查看进行中的待办\n"
        "/todo list all                    查看全部（含已完成）\n"
        "/todo list risk [ID]              查看某风险关联的待办\n"
        "/todo list plan [ID]              查看某里程碑挂载的待办\n"
        "/todo show [ID]                   查看待办详情\n"
        "/todo [内容]                       新建独立待办\n"
        "/todo [内容] risk [ID]             从 risk 分解新建\n"
        "/todo [内容] plan [ID]             挂到里程碑新建\n"
        "/todo update [ID] [字段] [值]      更新待办字段\n"
        "  字段：title|body|priority|owner|due_date\n"
        "  priority: high|medium|low\n"
        "/todo done [ID]                   标记完成\n"
        "/todo cancel [ID]                 取消\n\n"
        "管理员分解命令：\n"
        "/admin fact decompose [ID]  AI 自动分解 risk 为待办列表"
    )


def _handle_todo(text: str, project: str | None = "默认") -> str:
    import re
    rest = text[len("/todo"):].strip()
    if not rest or rest.lower() == "help":
        return _todo_help()

    parts = rest.split()
    sub = parts[0].lower()

    if sub == "list":
        filter_type = parts[1].lower() if len(parts) > 1 else ""
        if filter_type == "all":
            rows = db.list_todos(status=None, project=project)
        elif filter_type in ("risk", "plan") and len(parts) > 2:
            try:
                bind_id = int(parts[2])
            except ValueError:
                return "ID 必须是数字"
            if filter_type == "risk":
                rows = db.list_todos(status=None, source_fact_id=bind_id, project=project)
            else:
                rows = db.list_todos(status=None, plan_id=bind_id, project=project)
        else:
            rows = db.list_todos(status="open", project=project)
        return feishu.build_todo_list_card([dict(r) for r in rows])

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

    if sub == "show" and len(parts) >= 2:
        if not parts[1].isdigit():
            return "ID 必须是数字"
        tid = int(parts[1])
        todo = db.get_todo(tid)
        if not todo:
            return f"找不到待办 #T{tid}"
        todo = dict(todo)
        source_fact = db.get_fact(todo["source_fact_id"]) if todo.get("source_fact_id") else None
        plan_fact = db.get_fact(todo["plan_id"]) if todo.get("plan_id") else None
        return feishu.build_todo_show_card(
            todo,
            dict(source_fact) if source_fact else None,
            dict(plan_fact) if plan_fact else None,
        )

    if sub == "update" and len(parts) >= 3:
        if not parts[1].isdigit():
            return "ID 必须是数字"
        tid = int(parts[1])
        todo = db.get_todo(tid)
        if not todo:
            return f"找不到待办 #T{tid}"
        field = parts[2].lower()
        allowed_fields = {"title", "body", "priority", "owner", "due_date"}
        if field not in allowed_fields:
            return f"可更新字段：title|body|priority|owner|due_date"
        value = " ".join(parts[3:]) if len(parts) > 3 else ""
        if not value:
            return "请提供要更新的值"
        if field == "priority" and value not in ("high", "medium", "low"):
            return "priority 只能是 high|medium|low"
        db.update_todo(tid, **{field: value})
        return f"✓ 已更新 #T{tid}.{field} = {value}"

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

    tid = db.add_todo(content, source_fact_id=source_fact_id, plan_id=plan_id, project=project)
    suffix = (f"（关联 risk#{source_fact_id}）" if source_fact_id
              else f"（挂载到 milestone#{plan_id}）" if plan_id
              else "")
    return f"✓ 已新增待办 #T{tid}：{content}{suffix}"


# ── 管理员 async 命令（需要 AI 调用）────────────────────────

async def _handle_admin_fact_decompose(text: str, chat_id: str = "") -> str:
    parts = text.split(None, 4)
    if len(parts) < 4 or not parts[3].isdigit():
        return "用法：/admin fact decompose [ID]"
    fact_id = int(parts[3])
    fact = db.get_fact(fact_id)
    if not fact:
        return f"找不到 fact #{fact_id}"
    if fact["dimension"] != "risk":
        return f"#{fact_id} 不是风险类型（dimension={fact['dimension']}），仅支持分解 risk/issue/blocker/dependency"
    todos = await ai_client.decompose_risk(fact)
    if not todos:
        return "AI 未能分解出待办事项，请检查条目内容是否足够具体"
    # 标记来源 fact，以便确认后写入时保留追溯关系
    project = fact.get("project", "默认")
    for t in todos:
        t["source_fact_id"] = fact_id
        t["project"] = project
    if chat_id:
        suggestion_items = [
            {"kind": "new_todo", "status": "pending", "project": project, **t}
            for t in todos
        ]
        await _send_ai_suggestions_card(chat_id, suggestion_items)
        return f"已为 #{fact_id}《{fact['title'][:20]}》生成 {len(todos)} 条待办建议，请在卡片中确认。"
    # 无 chat_id 时直接入库（兜底，正常不会走到这里）
    saved: list[tuple[int, str]] = []
    for t in todos:
        tid = db.add_todo(
            t["title"],
            body=t.get("body", ""),
            priority=t.get("priority", "medium"),
            owner=t.get("owner", ""),
            source_fact_id=fact_id,
            source="ai",
            project=project,
        )
        saved.append((tid, t["title"]))
    lines = [f"已从 #{fact_id}《{fact['title']}》分解 {len(saved)} 条待办："]
    for tid, title in saved:
        lines.append(f"  #T{tid} {title}")
    return "\n".join(lines)


def _strip_action_command_lines(text: str) -> str:
    """Hide raw action commands from AI visible reply; keep them for card extraction only."""
    kept: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        normalized = re.sub(r"^[-*\d\.\)\s]+", "", s)
        if normalized.startswith("[AUTO]"):
            normalized = normalized[len("[AUTO]"):].strip()
        if normalized.startswith(("/todo", "/risk", "/admin")):
            continue
        kept.append(line)
    out = "\n".join(kept).strip()
    return out or "我已整理出待处理建议，请在确认卡片中逐条处理。"


async def _run_review_and_broadcast(mode: str | None = None) -> tuple[str | None, int, int]:
    """执行洗盘并广播报告。
    report_only：发报告文字 + 建议确认卡片；direct：自动执行 + 仅发报告文字。
    返回 (report, merge_count, action_count)。
    """
    effective_mode = mode or _get_review_mode()
    report = await _build_and_save_review(effective_mode)
    if not report:
        return None, 0, 0
    await _send_review_to_admins_pm(_strip_merge_candidates_json(report))
    review_items = _collect_review_suggestion_items(report)
    merge_count  = sum(1 for x in review_items if x["kind"] == "merge_fact")
    action_count = sum(1 for x in review_items if x["kind"] == "review_action")
    if effective_mode != _REVIEW_MODE_DIRECT:
        # report_only：全卡片确认，不自动执行
        await _broadcast_review_suggestions(_review_recipients_admins_pm(), report)
    return report, merge_count, action_count


async def _handle_review_run(text: str) -> str:
    """PM/管理员均可用：/review run [report|direct]"""
    mode_args = text.split()[2:]  # 跳过 /review run
    mode = _get_review_mode()
    if mode_args:
        requested = _normalize_review_mode(mode_args[0])
        if not requested:
            return "模式只能是 report/仅报告 或 direct/直接清洗"
        mode = requested

    report, merge_count, action_count = await _run_review_and_broadcast(mode)
    if not report:
        return "当前没有 active 项目信息条目，未生成洗盘报告。"

    suffix = _review_run_suffix(mode, merge_count, action_count)
    return f"✓ 已完成 AI 洗盘（{_review_mode_label(mode)}），报告已发送给所有管理员和 PM。{suffix}"


async def _handle_admin_review_run(text: str, chat_id: str = "") -> str:
    # text 形如 "/admin review run" 或 "/admin review run report"（保留兼容）
    mode_args = text.split()[3:]  # 跳过 /admin review run
    mode = _get_review_mode()
    if mode_args:
        requested = _normalize_review_mode(mode_args[0])
        if not requested:
            return "模式只能是 report/仅报告 或 direct/直接清洗"
        mode = requested

    report, merge_count, action_count = await _run_review_and_broadcast(mode)
    if not report:
        return "当前没有 active 项目信息条目，未生成洗盘报告。"

    suffix = _review_run_suffix(mode, merge_count, action_count)
    return f"✓ 已完成手动 AI 洗盘（{_review_mode_label(mode)}），报告已发送给所有管理员和 PM。{suffix}"


async def _handle_admin_user_approve_reject(text: str) -> str:
    """处理 /admin user approve/reject [open_id]，含飞书通知。"""
    parts = text.split()
    if len(parts) < 4:
        return "用法：/admin user approve [open_id] 或 /admin user reject [open_id]"
    sub = parts[2]  # approve or reject
    open_id = parts[3]
    user = db.get_user(open_id)
    if not user:
        return f"找不到用户 {open_id[:16]}…"
    name = user.get("name", open_id[:16])

    if sub == "approve":
        if user.get("status") == "active":
            return f"{name} 已是激活状态，无需重复审批。"
        role = user.get("role", "member")
        project = user.get("project", "")
        db.update_user(open_id, status="active")
        role_zh = _ROLE_ZH.get(role, role)
        try:
            await feishu.send_reply_to_user(
                open_id,
                f"🎉 你的注册申请已通过！\n角色：{role_zh}\n项目：{project}\n\n"
                f"现在可以直接 @Bot 与我对话了，发 /help 查看可用命令。",
                FEISHU_APP_ID, FEISHU_APP_SECRET,
            )
        except Exception:
            log.exception("failed to notify user %s on approve", open_id)
        log.info("admin approved user %s name=%s role=%s project=%s", open_id, name, role, project)
        return f"✓ 已批准 {name} [{role_zh}·{project}]，已发送通知"

    else:  # reject
        if user.get("status") == "rejected":
            return f"{name} 的申请已是拒绝状态。"
        db.update_user(open_id, status="rejected")
        try:
            await feishu.send_reply_to_user(
                open_id,
                "很抱歉，你的注册申请已被拒绝。如有疑问请联系管理员。",
                FEISHU_APP_ID, FEISHU_APP_SECRET,
            )
        except Exception:
            log.exception("failed to notify user %s on reject", open_id)
        log.info("admin rejected user %s name=%s", open_id, name)
        return f"✓ 已拒绝 {name} 的申请，已发送通知"


def _handle_admin_review(args: list[str]) -> str:
    sub = args[0].lower() if args else ""
    current = _get_review_mode()

    if sub in ("", "status"):
        return (
            f"当前 AI 洗盘模式：{_review_mode_label(current)}\n"
            "可用命令：\n"
            "/admin review mode report     设置为仅报告\n"
            "/admin review mode direct     设置为直接清洗\n"
            "/admin review run             立即洗盘并发送给管理员和 PM\n"
            "/admin review run report      按仅报告模式立即执行一次\n"
            "/admin review run direct      按直接清洗模式立即执行一次"
        )

    if sub == "mode":
        if len(args) < 2:
            return f"当前 AI 洗盘模式：{_review_mode_label(current)}"
        mode = _normalize_review_mode(args[1])
        if not mode:
            return "模式只能是 report/仅报告 或 direct/直接清洗"
        db.set_setting(_REVIEW_MODE_KEY, mode)
        return f"✓ 已设置 AI 洗盘模式：{_review_mode_label(mode)}"

    return (
        "review 命令：\n"
        "/admin review status              查看洗盘模式\n"
        "/admin review mode report         仅生成报告，不改数据\n"
        "/admin review mode direct         根据白名单命令直接清洗\n"
        "/admin review run [report|direct] 立即洗盘并发送给管理员和 PM"
    )


# ── 管理员命令 ────────────────────────────────────────────

def _handle_admin(text: str, sender_open_id: str = "",
                  project: str = "默认", chat_id: str = "") -> str:
    parts = text.split(None, 4)
    cmd = parts[1].lower() if len(parts) > 1 else ""
    admin_args = text.split()[2:] if len(parts) > 2 else []

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

    if cmd == "fact":
        return _handle_admin_fact(admin_args, project=project)

    if cmd == "assumption":
        return _handle_admin_assumption(admin_args)

    if cmd == "org":
        return _handle_admin_org(admin_args)

    if cmd == "user":
        return _handle_admin_user(admin_args)

    if cmd == "project":
        return _handle_admin_project(admin_args, sender_open_id, chat_id)

    if cmd == "review":
        return _handle_admin_review(admin_args)

    if cmd == "stats":
        return _handle_admin_stats()

    return _admin_help()


def _handle_schedule(args: list[str], project: str | None = "默认") -> str | dict:
    sub = args[0].lower() if args else "list"

    if sub == "list":
        filter_all = len(args) > 1 and args[1].lower() == "all"
        rows = db.list_facts(type_="milestone",
                             status=None if filter_all else "active",
                             project=project)
        return feishu.build_milestone_list_card([dict(r) for r in rows])

    if sub == "show" and len(args) >= 2:
        try:
            fid = int(args[1])
        except ValueError:
            return "ID 必须是数字"
        fact = db.get_fact(fid)
        if not fact or fact.get("type") != "milestone":
            return f"找不到里程碑 #{fid}"
        open_todos = db.list_todos(status="open", plan_id=fid)
        return feishu.build_milestone_show_card(dict(fact), [dict(t) for t in open_todos])

    return (
        "里程碑命令：\n"
        "/schedule list           查看进行中的里程碑\n"
        "/schedule list all       查看全部里程碑\n"
        "/schedule show [ID]      查看里程碑详情（含关联待办）"
    )


def _handle_admin_risk(args: list[str], project: str = "默认") -> str:
    sub = args[0].lower() if args else ""

    if sub == "list":
        filter_status = args[1] if len(args) > 1 else "open"
        rows = db.list_risks(status=None if filter_status == "all" else filter_status,
                             project=project)
        return feishu.build_risk_list_card([dict(r) for r in rows], filter_status)

    if sub == "show" and len(args) >= 2:
        try:
            rid = int(args[1])
        except ValueError:
            return "ID 必须是数字"
        fact = db.get_fact(rid)
        if not fact or fact.get("dimension") != "risk":
            return f"找不到风险 #{rid}"
        open_todos = db.list_todos(status="open", source_fact_id=rid)
        return feishu.build_risk_show_card(dict(fact), [dict(t) for t in open_todos])

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
        # /risk add [type] [priority] [title] | [description]
        type_ = args[1] if args[1] in ("risk", "issue", "blocker", "dependency") else "risk"
        priority = args[2] if args[2] in ("high", "medium", "low") else "medium"
        rest = " ".join(args[3:])
        if "|" in rest:
            title, desc = rest.split("|", 1)
            title, desc = title.strip(), desc.strip()
        else:
            title, desc = rest.strip(), rest.strip()
        rid = db.add_risk(type_, title, desc, priority=priority, project=project)
        return f"✓ 已新增 #{rid} [{type_}·{priority}] {title}（{project}）"

    return (
        "风险命令：\n"
        "/risk list [open|all]              查看风险列表\n"
        "/risk show [ID]                    查看风险详情（含正文和关联待办）\n"
        "/risk close [ID]                   关闭风险\n"
        "/risk reopen [ID]                  重新打开\n"
        "/risk owner [ID] [姓名]            设置负责人\n"
        "/risk add [type] [priority] [标题] | [描述]  新增风险\n"
        "  type: risk|issue|blocker|dependency\n"
        "  priority: high|medium|low"
    )


def _handle_admin_fact(args: list[str], project: str = "默认") -> str:
    sub = args[0].lower() if args else ""

    if sub == "list":
        type_filter = args[1] if len(args) > 1 and args[1] != "all" else None
        status_filter = args[2] if len(args) > 2 else "active"
        if len(args) > 1 and args[1] == "all":
            status_filter = None
        # admin list 默认显示所有项目，加 project 参数时过滤
        rows = db.list_facts(type_=type_filter, status=status_filter)
        if not rows:
            return "无匹配条目"
        # 判断是否存在多个项目的数据
        projects_in_data = {r["project"] for r in rows if r["project"]}
        multi_project = len(projects_in_data) > 1
        lines = []
        for r in rows:
            label = _TYPE_LABELS.get(r["type"], r["type"])
            status = _STATUS_LABELS.get(r["status"], r["status"])
            prio = f"·{_PRIO_LABELS[r['priority']]}" if r["priority"] in _PRIO_LABELS else ""
            owner = f"（{r['owner']}）" if r["owner"] else ""
            date = r["updated_at"][:10]
            proj_tag = f"[{r['project']}]" if multi_project and r["project"] else ""
            lines.append(f"#{r['id']} {proj_tag}[{label}{prio}·{status}] {r['title']}{owner} [{date}]")
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
        fid = db.add_fact(type_, title, body, source="manual", project=project)
        return f"✓ 已新增 #{fid} [{type_}] {title}（{project}）"

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
        "  type: risk|issue|milestone|decision|team|client|knowledge|process|org\n"
        "/admin fact decompose [ID]                     AI 分解 risk 为待办列表"
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
        scope_token = args[1]
        scope_ref = ""
        if "/" in scope_token:
            scope_token, scope_ref = scope_token.split("/", 1)
        scope      = scope_token if scope_token in ("dept","project","client","global") else "dept"
        confidence = args[2] if args[2] in ("universal","common","assumed") else "common"
        rest = " ".join(args[3:])
        if not rest.strip():
            return "用法：/admin assumption add [scope] [confidence] [标题] | [正文]\n例如：/admin assumption add dept common 会议纪要规则 | 所有关键决策必须记录 owner 和截止时间"
        if "|" in rest:
            title, body = rest.split("|", 1)
            title, body = title.strip(), body.strip()
        else:
            title, body = rest.strip(), rest.strip()
        # 支持 project/雅迪 这种格式指定 scope_ref
        if not title or not body:
            return "标题和正文不能为空。格式：/admin assumption add [scope] [confidence] [标题] | [正文]"
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


def _handle_admin_user(args: list[str]) -> str:
    sub = args[0].lower() if args else ""

    if sub == "list":
        rows = db.list_users()
        if not rows:
            return "暂无注册用户"
        lines = []
        for r in rows:
            status_tag = {"active": "✓", "pending": "⏳", "rejected": "✗", "inactive": "—"}.get(
                r["status"], r["status"])
            role_zh = _ROLE_ZH.get(r["role"], r["role"])
            proj = f"/{r['project']}" if r["project"] else ""
            lines.append(
                f"{status_tag} {r['name'] or '(未知)'} [{role_zh}{proj}]"
                f"  {r['open_id'][:16]}…  加入:{r['created_at'][:10]}"
            )
        return f"用户列表（共 {len(rows)} 人）：\n" + "\n".join(lines)

    if sub == "show" and len(args) >= 2:
        keyword = " ".join(args[1:])
        # 按 open_id 或 name 模糊查找
        rows = db.list_users()
        matches = [r for r in rows if keyword in r["open_id"] or keyword in (r["name"] or "")]
        if not matches:
            return f"找不到用户：{keyword}"
        lines = []
        for r in matches:
            lines.append(
                f"姓名：{r['name'] or '(未知)'}\n"
                f"open_id：{r['open_id']}\n"
                f"角色：{_ROLE_ZH.get(r['role'], r['role'])}\n"
                f"项目：{r['project'] or '—'}\n"
                f"状态：{r['status']}\n"
                f"注册：{r['created_at'][:16]}"
            )
        return "\n---\n".join(lines)

    if sub == "role" and len(args) >= 3:
        open_id = args[1]
        new_role = args[2].lower()
        if new_role not in ("pm", "member", "super_admin"):
            return "角色只能是 pm | member | super_admin"
        db.update_user(open_id, role=new_role)
        return f"✓ 已将 {open_id[:16]}… 的角色改为 {new_role}"

    if sub == "remove" and len(args) >= 2:
        open_id = args[1]
        user = db.get_user(open_id)
        if not user:
            return f"找不到用户 {open_id[:16]}…"
        db.delete_user(open_id)
        return f"✓ 已删除用户 {user.get('name', open_id[:16])}"

    if sub == "project" and len(args) >= 3:
        open_id = args[1]
        new_project = args[2]
        user = db.get_user(open_id)
        if not user:
            return f"找不到用户 {open_id[:16]}…"
        name = user.get("name", open_id[:16])
        if new_project == "-":
            db.update_user(open_id, project="")
            return f"✓ 已清除 {name} 的项目绑定"
        proj = db.get_project_by_name(new_project)
        if not proj or not proj.get("active"):
            projects = db.list_projects(active_only=True)
            names = "、".join(p["name"] for p in projects) if projects else "（暂无）"
            return f"找不到项目「{new_project}」，当前可用：{names}"
        db.update_user(open_id, project=new_project)
        return f"✓ 已将 {name} 的项目绑定改为「{new_project}」"

    return (
        "用户管理命令：\n"
        "/admin user list                                     列出所有用户\n"
        "/admin user show [姓名/open_id]                      查看用户详情\n"
        "/admin user role [open_id] [pm|member|super_admin]   修改角色\n"
        "/admin user project [open_id] [项目名|-]              修改或清除项目绑定\n"
        "/admin user approve [open_id]                        手动批准申请（含通知）\n"
        "/admin user reject [open_id]                         拒绝申请（含通知）\n"
        "/admin user remove [open_id]                         删除用户"
    )


def _handle_admin_project(args: list[str], sender_open_id: str = "", chat_id: str = "") -> str:
    sub = args[0].lower() if args else ""

    if sub == "list":
        rows = db.list_projects(active_only=False)
        if not rows:
            return "暂无项目"
        bindings = {b["project"]: b["chat_id"] for b in db.list_chat_bindings()}
        lines = []
        for r in rows:
            status = "✓" if r["active"] else "✗"
            bound = f"（已绑群聊）" if r["name"] in bindings else ""
            desc = f"（{r['description']}）" if r["description"] else ""
            lines.append(f"#{r['id']} {status} {r['name']}{desc}{bound}")
        return "\n".join(lines)

    if sub == "add" and len(args) >= 2:
        rest = " ".join(args[1:])
        if "|" in rest:
            name, desc = rest.split("|", 1)
            name, desc = name.strip(), desc.strip()
        else:
            name, desc = rest.strip(), ""
        try:
            pid = db.add_project(name, desc, created_by=sender_open_id)
            return f"✓ 已创建项目 #{pid}「{name}」"
        except Exception:
            return f"创建失败（项目名「{name}」可能已存在）"

    if sub == "close" and len(args) >= 2:
        try:
            pid = int(args[1])
        except ValueError:
            return "ID 必须是数字"
        db.update_project(pid, active=0)
        return f"✓ 项目 #{pid} 已关闭"

    if sub == "open" and len(args) >= 2:
        try:
            pid = int(args[1])
        except ValueError:
            return "ID 必须是数字"
        db.update_project(pid, active=1)
        return f"✓ 项目 #{pid} 已重新开启"

    if sub == "bind" and len(args) >= 2:
        # /admin project bind [项目名]  — 绑定当前 chat 到项目
        proj_name = " ".join(args[1:])
        proj = db.get_project_by_name(proj_name)
        if not proj or not proj["active"]:
            projs = db.list_projects(active_only=True)
            names = "、".join(p["name"] for p in projs)
            return f"找不到项目「{proj_name}」，当前项目：{names}"
        if not chat_id:
            return "无法获取当前群聊 ID，请在群聊中使用此命令"
        db.set_chat_binding(chat_id, proj_name)
        return f"✓ 当前群聊已绑定到项目「{proj_name}」"

    if sub == "unbind":
        if not chat_id:
            return "无法获取当前群聊 ID"
        db.delete_chat_binding(chat_id)
        return "✓ 当前群聊的项目绑定已解除"

    if sub == "bindings":
        rows = db.list_chat_bindings()
        if not rows:
            return "暂无群聊绑定"
        return "\n".join(f"{r['chat_id']} → {r['project']}" for r in rows)

    return (
        "项目管理命令：\n"
        "/admin project list                  列出所有项目\n"
        "/admin project add [名称] | [描述]   创建项目\n"
        "/admin project close [ID]            关闭项目\n"
        "/admin project open [ID]             重新开启项目\n"
        "/admin project bind [项目名]         将当前群聊绑定到项目\n"
        "/admin project unbind                解除当前群聊绑定\n"
        "/admin project bindings              查看所有群聊绑定"
    )


def _handle_admin_stats() -> str:
    stats = db.get_system_stats()
    # 用户统计
    user_summary: dict[str, dict] = {}
    for r in stats["users"]:
        role = r["role"]
        status = r["status"]
        if role not in user_summary:
            user_summary[role] = {}
        user_summary[role][status] = r["cnt"]
    user_lines = []
    for role in ("super_admin", "pm", "member", "pending"):
        if role in user_summary:
            statuses = user_summary[role]
            active = statuses.get("active", 0)
            pending = statuses.get("pending", 0)
            role_zh = _ROLE_ZH.get(role, role)
            s = f"  {role_zh}：{active} 人"
            if pending:
                s += f"（{pending} 待审批）"
            user_lines.append(s)
    # facts 统计
    fact_summary = {r["type"]: r["cnt"] for r in stats["facts"]}
    risk_cnt = sum(fact_summary.get(t, 0) for t in ("risk", "issue", "blocker", "dependency"))
    # todos 统计
    todo_summary = {r["status"]: r["cnt"] for r in stats["todos"]}
    lines = [
        "=== 系统统计 ===",
        f"项目数：{stats['project_count']} 个（活跃）",
        "用户：",
        *user_lines,
        f"知识库（active）：{sum(fact_summary.values())} 条",
        f"  风险/问题：{risk_cnt} | 里程碑：{fact_summary.get('milestone', 0)}"
        f" | 决策：{fact_summary.get('decision', 0)} | 知识：{fact_summary.get('knowledge', 0)}",
        f"待办：open {todo_summary.get('open', 0)} | done {todo_summary.get('done', 0)}"
        f" | cancelled {todo_summary.get('cancelled', 0)}",
        f"最近洗盘：{stats['last_review']}",
    ]
    return "\n".join(lines)


def _help_text(role: str = "unknown") -> str:
    common = (
        "pm-assist 使用说明\n\n"
        "所有人可用：\n"
        "  /start                   查看可用项目并申请加入\n"
        "  /join [项目] [pm|member] 申请加入项目\n"
        "  /version                 查看版本号\n"
        "  /help                    显示本说明\n"
    )
    if role in ("unknown", "pending"):
        return common + "\n（注册并审批通过后可使用更多功能）"

    registered_section = (
        "\n已注册用户可用：\n"
        "  @Bot [消息]              AI对话（里程碑、组织信息等）\n"
        "  /leave                   退出当前项目绑定\n"
        "  /clear                   清除当前会话历史\n"
    )

    if role == "member":
        return (
            common + registered_section +
            "\n（你的角色为项目成员，可通过 @Bot 咨询里程碑进展和团队组织信息；"
            "\n 风险、待办等PM工作功能需申请 pm 角色）"
        )

    pm_section = (
        "\nPM / 管理员可用：\n"
        "  /note [内容]             快速记录笔记到知识库\n"
        "  /risk list [open|all]    查看风险/问题列表\n"
        "  /risk show [ID]          查看风险详情（含正文和关联待办）\n"
        "  /risk close/reopen [ID]  关闭/重开风险\n"
        "  /risk owner [ID] [姓名]  设置负责人\n"
        "  /risk add [type] [priority] [标题] | [描述]  新增风险\n"
        "    type: risk|issue|blocker|dependency  priority: high|medium|low\n"
        "  /todo list               查看进行中的待办\n"
        "  /todo list all           查看全部待办（含已完成）\n"
        "  /todo list risk/plan [ID] 查看关联待办\n"
        "  /todo show [ID]          查看待办详情\n"
        "  /todo [内容]              新建独立待办\n"
        "  /todo [内容] risk/plan [ID]  关联新建待办\n"
        "  /todo update [ID] [字段] [值]  更新字段\n"
        "    字段：title|body|priority|owner|due_date\n"
        "  /todo done/cancel [ID]   标记完成/取消\n"
        "  /review run              立即 AI 洗盘，报告和建议卡片发给所有管理员和PM\n"
        "  /review run report|direct  指定本次洗盘模式\n"
    )

    if role == "pm":
        return common + registered_section + pm_section

    # super_admin
    admin_section = (
        "\n管理员专用：\n"
        "  /admin stats\n"
        "  /admin user list/show/role/project/approve/reject/remove\n"
        "  /admin project list/add/close/open/bind/unbind/bindings\n"
        "  /admin fact list/show/update/archive/delete/add/decompose\n"
        "  /admin review status/mode  查看/设置洗盘模式（run 已移至 /review）\n"
        "  /admin assumption list/show/add/update/archive/delete\n"
        "  /admin org list/add\n"
    )
    return common + registered_section + pm_section + admin_section


def _admin_help() -> str:
    return (
        "管理员命令：\n"
        "/admin stats\n"
        "/admin user list/show/role/project/approve/reject/remove\n"
        "/admin project list/add/close/open/bind/unbind/bindings\n"
        "/admin fact list/show/update/archive/delete/add/decompose\n"
        "/admin review status/mode  查看/设置洗盘模式\n"
        "/admin assumption list/show/add/update/archive/delete\n"
        "/admin org list/add\n\n"
        "PM/管理员：/risk list/show/close/reopen/owner/add\n"
        "           /todo list/show/update/done/cancel/[内容]\n"
        "           /review run [report|direct]  立即洗盘\n"
        "           /note [内容]"
    )
