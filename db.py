import sqlite3

DB_PATH = "pm_assist.db"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS knowledge_blocks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                category    TEXT    NOT NULL,
                title       TEXT    NOT NULL,
                content     TEXT    NOT NULL,
                enabled     INTEGER DEFAULT 1,
                updated_at  TEXT    DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS conversations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id     TEXT    NOT NULL,
                role        TEXT    NOT NULL,
                content     TEXT    NOT NULL,
                created_at  TEXT    DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS processed_events (
                event_id    TEXT    PRIMARY KEY,
                created_at  TEXT    DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS pending_notes (
                chat_id     TEXT    PRIMARY KEY,
                items_json  TEXT    NOT NULL,
                created_at  INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS risks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                type        TEXT    NOT NULL DEFAULT 'risk',
                title       TEXT    NOT NULL,
                description TEXT    NOT NULL,
                owner       TEXT    DEFAULT '',
                priority    TEXT    DEFAULT 'medium',
                status      TEXT    DEFAULT 'open',
                due_date    TEXT    DEFAULT '',
                project     TEXT    DEFAULT 'yadi',
                created_at  TEXT    DEFAULT CURRENT_TIMESTAMP,
                updated_at  TEXT    DEFAULT CURRENT_TIMESTAMP
            );
        """)


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


# ── 知识块 ────────────────────────────────────────────────

def get_knowledge_text() -> str:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT title, content FROM knowledge_blocks WHERE enabled=1 ORDER BY category, id"
        ).fetchall()
    if not rows:
        return ""
    return "\n\n".join(f"【{r['title']}】\n{r['content']}" for r in rows)


def list_blocks() -> list:
    with get_conn() as conn:
        return conn.execute(
            "SELECT id, category, title, enabled FROM knowledge_blocks ORDER BY category, id"
        ).fetchall()


def add_block(category: str, title: str, content: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO knowledge_blocks(category, title, content) VALUES(?,?,?)",
            (category, title, content),
        )
        return cur.lastrowid


def update_block(block_id: int, content: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE knowledge_blocks SET content=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (content, block_id),
        )


def toggle_block(block_id: int, enabled: bool):
    with get_conn() as conn:
        conn.execute(
            "UPDATE knowledge_blocks SET enabled=? WHERE id=?",
            (1 if enabled else 0, block_id),
        )


def delete_block(block_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM knowledge_blocks WHERE id=?", (block_id,))


def count_notes() -> int:
    with get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM knowledge_blocks WHERE category='note'"
        ).fetchone()[0]


# ── 待确认笔记 ────────────────────────────────────────────

import json as _json
import time as _time

PENDING_TTL = 600  # 10分钟内未确认自动失效


def save_pending(chat_id: str, items: list[dict]):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO pending_notes(chat_id, items_json, created_at) VALUES(?,?,?)",
            (chat_id, _json.dumps(items, ensure_ascii=False), int(_time.time())),
        )


def get_pending(chat_id: str) -> list[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT items_json, created_at FROM pending_notes WHERE chat_id=?",
            (chat_id,),
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


# ── 风险与问题表 ──────────────────────────────────────────

_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def add_risk(type_: str, title: str, description: str,
             owner: str = "", priority: str = "medium", due_date: str = "", project: str = "yadi") -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO risks(type, title, description, owner, priority, due_date, project) VALUES(?,?,?,?,?,?,?)",
            (type_, title, description, owner, priority, due_date, project),
        )
        return cur.lastrowid


def list_risks(status: str | None = None, project: str = "yadi") -> list:
    with get_conn() as conn:
        if status:
            return conn.execute(
                "SELECT * FROM risks WHERE project=? AND status=? ORDER BY id",
                (project, status),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM risks WHERE project=? ORDER BY status, id",
            (project,),
        ).fetchall()


def update_risk(risk_id: int, **kwargs):
    allowed = {"status", "owner", "priority", "due_date", "description"}
    sets = ", ".join(f"{k}=?" for k in kwargs if k in allowed)
    vals = [v for k, v in kwargs.items() if k in allowed]
    if not sets:
        return
    with get_conn() as conn:
        conn.execute(
            f"UPDATE risks SET {sets}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (*vals, risk_id),
        )


def get_risks_text(project: str = "yadi") -> str:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT type, title, description, owner, priority, due_date, status "
            "FROM risks WHERE project=? AND status NOT IN ('resolved','closed') "
            "ORDER BY id",
            (project,),
        ).fetchall()
    if not rows:
        return ""
    _PRIO = {"high": "高", "medium": "中", "low": "低"}
    _TYPE = {"risk": "风险", "issue": "问题", "blocker": "阻塞项", "dependency": "依赖"}
    lines = []
    for r in rows:
        prio = _PRIO.get(r["priority"], r["priority"])
        typ = _TYPE.get(r["type"], r["type"])
        line = f"[{typ}·{prio}] {r['title']}：{r['description']}"
        if r["owner"]:
            line += f"（负责人：{r['owner']}）"
        if r["due_date"]:
            line += f"（截止：{r['due_date']}）"
        lines.append(line)
    return "\n".join(lines)


def pop_pending_item(chat_id: str, index: int) -> tuple[dict | None, list[dict]]:
    """弹出并返回指定索引的待确认条目，同时返回剩余列表。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT items_json, created_at FROM pending_notes WHERE chat_id=?",
            (chat_id,),
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
