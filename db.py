import re
import sqlite3
from datetime import datetime

import json as _json
import time as _time

DB_PATH = "pm_assist.db"

# --- Migration mappings ---
_CATEGORY_TYPE_MAP = {
    "org": "org",
    "process": "process",
    "项目框架": "process",
    "工作流程": "process",
    "customer": "client",
    "risk": "knowledge",   # 风险管理规则是知识内容，不是风险条目
}
_NOTE_PREFIX_TYPE = {
    "[风险]": "risk",
    "[里程碑]": "milestone",
    "[决策]": "decision",
    "[人员]": "team",
    "[客户信息]": "client",
}
_RISK_STATUS_IN = {"open": "active", "closed": "resolved", "resolved": "resolved"}
_RISK_STATUS_OUT = {"active": "open", "resolved": "resolved", "archived": "archived"}

ACTIONABLE_TYPES = {"risk", "issue", "blocker", "dependency", "milestone", "decision"}
KNOWLEDGE_TYPES = {"org", "process", "client", "knowledge", "team"}

PENDING_TTL = 600


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS facts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                type        TEXT    NOT NULL,
                title       TEXT    NOT NULL,
                body        TEXT    NOT NULL,
                status      TEXT    NOT NULL DEFAULT 'active',
                priority    TEXT    NOT NULL DEFAULT '',
                owner       TEXT    NOT NULL DEFAULT '',
                due_date    TEXT    NOT NULL DEFAULT '',
                project     TEXT    NOT NULL DEFAULT 'yadi',
                source      TEXT    NOT NULL DEFAULT 'manual',
                created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
                updated_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS conversations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id     TEXT    NOT NULL,
                role        TEXT    NOT NULL,
                content     TEXT    NOT NULL,
                created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS processed_events (
                event_id    TEXT    PRIMARY KEY,
                created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS pending_notes (
                chat_id     TEXT    PRIMARY KEY,
                items_json  TEXT    NOT NULL,
                created_at  INTEGER NOT NULL
            );
        """)
        _migrate_legacy(conn)


def _migrate_legacy(conn):
    """从 knowledge_blocks + risks 一次性迁移到 facts，幂等。"""
    if conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] > 0:
        return

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    has_kb = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='knowledge_blocks'"
    ).fetchone()[0]
    has_risks = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='risks'"
    ).fetchone()[0]

    if has_kb:
        for r in conn.execute("SELECT * FROM knowledge_blocks").fetchall():
            cat = r["category"]
            if cat == "note":
                body = r["content"]
                fact_type = "knowledge"
                for prefix, t in _NOTE_PREFIX_TYPE.items():
                    if body.startswith(prefix):
                        fact_type = t
                        break
                source = "ai"
            else:
                fact_type = _CATEGORY_TYPE_MAP.get(cat, "knowledge")
                source = "seed"
            status = "active" if r["enabled"] else "archived"
            ts = r["updated_at"] or now
            conn.execute(
                "INSERT INTO facts(type,title,body,status,source,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (fact_type, r["title"], r["content"], status, source, ts, ts),
            )

    if has_risks:
        for r in conn.execute("SELECT * FROM risks").fetchall():
            status = _RISK_STATUS_IN.get(r["status"], "active")
            conn.execute(
                "INSERT INTO facts(type,title,body,status,priority,owner,due_date,project,"
                "source,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    r["type"], r["title"], r["description"],
                    status, r["priority"] or "", r["owner"] or "",
                    r["due_date"] or "", r["project"] or "yadi",
                    "seed", r["created_at"] or now, r["updated_at"] or now,
                ),
            )


# ── 事件去重 ───────────────────────────────────────────────

def is_processed(event_id: str) -> bool:
    with get_conn() as conn:
        return conn.execute(
            "SELECT 1 FROM processed_events WHERE event_id=?", (event_id,)
        ).fetchone() is not None


def mark_processed(event_id: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO processed_events(event_id) VALUES(?)", (event_id,)
        )


# ── 对话历史 ───────────────────────────────────────────────

def get_history(chat_id: str, limit: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT role, content FROM conversations WHERE chat_id=? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def add_message(chat_id: str, role: str, content: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO conversations(chat_id, role, content) VALUES(?,?,?)",
            (chat_id, role, content),
        )


def clear_history(chat_id: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM conversations WHERE chat_id=?", (chat_id,))


# ── facts 核心 CRUD ────────────────────────────────────────

def add_fact(type_: str, title: str, body: str, status: str = "active",
             priority: str = "", owner: str = "", due_date: str = "",
             project: str = "yadi", source: str = "manual") -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO facts(type,title,body,status,priority,owner,due_date,project,source)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (type_, title, body, status, priority, owner, due_date, project, source),
        )
        return cur.lastrowid


def get_fact(fact_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM facts WHERE id=?", (fact_id,)).fetchone()
    return dict(row) if row else None


def update_fact(fact_id: int, **kwargs):
    allowed = {"type", "title", "body", "status", "priority", "owner", "due_date"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values())
    with get_conn() as conn:
        conn.execute(
            f"UPDATE facts SET {sets}, updated_at=datetime('now','localtime') WHERE id=?",
            (*vals, fact_id),
        )


def append_to_fact(fact_id: int, addition: str):
    """在 body 末尾追加带时间戳的更新记录，保留变更历史。"""
    ts = datetime.now().strftime("%Y-%m-%d")
    with get_conn() as conn:
        row = conn.execute("SELECT body FROM facts WHERE id=?", (fact_id,)).fetchone()
        if not row:
            return
        new_body = row["body"] + f"\n[{ts} 更新] {addition}"
        conn.execute(
            "UPDATE facts SET body=?, updated_at=datetime('now','localtime') WHERE id=?",
            (new_body, fact_id),
        )


def delete_fact(fact_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM facts WHERE id=?", (fact_id,))


def list_facts(type_: str | None = None, status: str | None = "active",
               project: str | None = None) -> list:
    clauses, params = [], []
    if type_:
        clauses.append("type=?")
        params.append(type_)
    if status:
        clauses.append("status=?")
        params.append(status)
    if project:
        clauses.append("project=?")
        params.append(project)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_conn() as conn:
        return conn.execute(
            f"SELECT * FROM facts {where} ORDER BY type, id", params
        ).fetchall()


def find_similar_fact(type_: str, content: str, threshold: int = 2) -> dict | None:
    """按关键词重叠查找同类型的已有 active 条目。"""
    words = {t for t in re.split(r'[\s，。、；：！？「」【】（）\[\],.!?;:()\-\n]+', content) if len(t) >= 2}
    if not words:
        return None
    with get_conn() as conn:
        candidates = conn.execute(
            "SELECT id, type, title, body FROM facts WHERE status='active' AND type=?",
            (type_,),
        ).fetchall()
    best_score, best = 0, None
    for c in candidates:
        c_words = {
            t for t in re.split(r'[\s，。、；：！？「」【】（）\[\],.!?;:()\-\n]+',
                                 c["title"] + " " + c["body"])
            if len(t) >= 2
        }
        score = len(words & c_words)
        if score > best_score:
            best_score, best = score, dict(c)
    return best if best_score >= threshold else None


# ── AI 上下文拼装 ──────────────────────────────────────────

def get_knowledge_text() -> str:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT title, body FROM facts WHERE status='active'"
            " AND type NOT IN ('risk','issue','blocker','dependency')"
            " ORDER BY type, id"
        ).fetchall()
    if not rows:
        return ""
    return "\n\n".join(f"【{r['title']}】\n{r['body']}" for r in rows)


def get_risks_text(project: str = "yadi") -> str:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, type, title, body, owner, priority, due_date FROM facts"
            " WHERE project=? AND status='active'"
            " AND type IN ('risk','issue','blocker','dependency') ORDER BY id",
            (project,),
        ).fetchall()
    if not rows:
        return ""
    _PRIO = {"high": "高", "medium": "中", "low": "低"}
    _TYPE = {"risk": "风险", "issue": "问题", "blocker": "阻塞项", "dependency": "依赖"}
    lines = []
    for r in rows:
        line = f"[{_TYPE.get(r['type'], r['type'])}·{_PRIO.get(r['priority'], r['priority'])}]"
        line += f" #{r['id']} {r['title']}：{r['body']}"
        if r["owner"]:
            line += f"（负责人：{r['owner']}）"
        if r["due_date"]:
            line += f"（截止：{r['due_date']}）"
        lines.append(line)
    return "\n".join(lines)


# ── 向后兼容：knowledge_blocks 接口 ───────────────────────

def add_block(category: str, title: str, content: str) -> int:
    if category == "note":
        fact_type = "knowledge"
        for prefix, t in _NOTE_PREFIX_TYPE.items():
            if content.startswith(prefix):
                fact_type = t
                break
        source = "ai"
    else:
        fact_type = _CATEGORY_TYPE_MAP.get(category, "knowledge")
        source = "manual"
    return add_fact(fact_type, title, content, source=source)


def list_blocks() -> list:
    with get_conn() as conn:
        return conn.execute(
            "SELECT id, type AS category, title,"
            " CASE WHEN status='active' THEN 1 ELSE 0 END AS enabled,"
            " updated_at FROM facts ORDER BY type, id"
        ).fetchall()


def update_block(block_id: int, content: str):
    update_fact(block_id, body=content)


def toggle_block(block_id: int, enabled: bool):
    update_fact(block_id, status="active" if enabled else "archived")


def delete_block(block_id: int):
    delete_fact(block_id)


def count_notes() -> int:
    with get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM facts WHERE source='ai'"
        ).fetchone()[0]


# ── 向后兼容：risks 接口 ───────────────────────────────────

def add_risk(type_: str, title: str, description: str,
             owner: str = "", priority: str = "medium",
             due_date: str = "", project: str = "yadi") -> int:
    return add_fact(type_, title, description,
                    priority=priority, owner=owner, due_date=due_date,
                    project=project, source="manual")


def list_risks(status: str | None = None, project: str = "yadi") -> list:
    db_status = _RISK_STATUS_IN.get(status, status) if status else None
    with get_conn() as conn:
        base = (
            "SELECT id, type, title, body AS description, owner, priority,"
            " status, due_date, project, created_at, updated_at"
            " FROM facts WHERE project=?"
            " AND type IN ('risk','issue','blocker','dependency')"
        )
        if db_status:
            rows = conn.execute(base + " AND status=? ORDER BY id",
                                (project, db_status)).fetchall()
        else:
            rows = conn.execute(base + " ORDER BY status, id", (project,)).fetchall()
    # 将内部 status 映射回对外展示的 open/closed 术语
    result = []
    for r in rows:
        d = dict(r)
        d["status"] = _RISK_STATUS_OUT.get(d["status"], d["status"])
        result.append(d)
    return result


def update_risk(risk_id: int, **kwargs):
    if "status" in kwargs:
        kwargs["status"] = _RISK_STATUS_IN.get(kwargs["status"], kwargs["status"])
    if "description" in kwargs:
        kwargs["body"] = kwargs.pop("description")
    allowed = {"status", "owner", "priority", "due_date", "body", "title"}
    update_fact(risk_id, **{k: v for k, v in kwargs.items() if k in allowed})


# ── 待确认笔记 ────────────────────────────────────────────

def save_pending(chat_id: str, items: list[dict]):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO pending_notes(chat_id,items_json,created_at) VALUES(?,?,?)",
            (chat_id, _json.dumps(items, ensure_ascii=False), int(_time.time())),
        )


def get_pending(chat_id: str) -> list[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT items_json, created_at FROM pending_notes WHERE chat_id=?", (chat_id,)
        ).fetchone()
    if not row:
        return []
    if _time.time() - row["created_at"] > PENDING_TTL:
        clear_pending(chat_id)
        return []
    return _json.loads(row["items_json"])


def clear_pending(chat_id: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM pending_notes WHERE chat_id=?", (chat_id,))


def pop_pending_item(chat_id: str, index: int) -> tuple[dict | None, list[dict]]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT items_json, created_at FROM pending_notes WHERE chat_id=?", (chat_id,)
        ).fetchone()
        if not row or _time.time() - row["created_at"] > PENDING_TTL:
            conn.execute("DELETE FROM pending_notes WHERE chat_id=?", (chat_id,))
            return None, []
        items: list[dict] = _json.loads(row["items_json"])
        if index < 0 or index >= len(items):
            return None, items
        saved = items.pop(index)
        if items:
            conn.execute(
                "UPDATE pending_notes SET items_json=? WHERE chat_id=?",
                (_json.dumps(items, ensure_ascii=False), chat_id),
            )
        else:
            conn.execute("DELETE FROM pending_notes WHERE chat_id=?", (chat_id,))
        return saved, items
