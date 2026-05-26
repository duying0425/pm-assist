from __future__ import annotations

import re
import sqlite3
from datetime import datetime

import json as _json
import time as _time

DB_PATH = "pm_assist.db"

# sub_type → dimension mapping (universal PM taxonomy)
TYPE_TO_DIMENSION = {
    "risk":        "risk",
    "issue":       "risk",
    "blocker":     "risk",
    "dependency":  "risk",
    "milestone":   "schedule",
    "decision":    "decision",
    "process":     "decision",
    "team":        "resource",
    "client":      "stakeholder",
    "org":         "stakeholder",
    "knowledge":   "scope",
    "report":      "system",
}

_CATEGORY_TYPE_MAP = {
    "org": "org",
    "process": "process",
    "项目框架": "process",
    "工作流程": "process",
    "customer": "client",
    "risk": "knowledge",
}
_NOTE_PREFIX_TYPE = {
    "[风险]": "risk",
    "[里程碑]": "milestone",
    "[决策]": "decision",
    "[人员]": "team",
    "[客户信息]": "client",
}
_RISK_STATUS_IN  = {"open": "active", "closed": "resolved", "resolved": "resolved"}
_RISK_STATUS_OUT = {"active": "open", "resolved": "resolved", "archived": "archived"}

ACTIONABLE_TYPES = {"risk", "issue", "blocker", "dependency", "milestone", "decision"}
KNOWLEDGE_TYPES  = {"org", "process", "client", "knowledge", "team"}

PENDING_TTL = 1800

_CONFIDENCE_LABEL = {"universal": "铁律", "common": "通常", "assumed": "推测"}
_SCOPE_LABEL       = {"dept": "部门", "project": "项目", "client": "客户", "global": "全局"}


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
                dimension   TEXT    NOT NULL DEFAULT '',
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
            CREATE TABLE IF NOT EXISTS assumptions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                scope       TEXT    NOT NULL DEFAULT 'dept',
                scope_ref   TEXT    NOT NULL DEFAULT '',
                title       TEXT    NOT NULL,
                body        TEXT    NOT NULL,
                confidence  TEXT    NOT NULL DEFAULT 'common',
                source      TEXT    NOT NULL DEFAULT 'manual',
                active      INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
                updated_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS org_units (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                type        TEXT    NOT NULL,
                name        TEXT    NOT NULL,
                parent_id   INTEGER DEFAULT NULL,
                feishu_id   TEXT    NOT NULL DEFAULT '',
                attributes  TEXT    NOT NULL DEFAULT '{}',
                active      INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
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
            CREATE TABLE IF NOT EXISTS chat_bindings (
                chat_id    TEXT    PRIMARY KEY,
                project    TEXT    NOT NULL,
                created_at TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS todos (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                title           TEXT    NOT NULL,
                body            TEXT    NOT NULL DEFAULT '',
                status          TEXT    NOT NULL DEFAULT 'open',
                priority        TEXT    NOT NULL DEFAULT 'medium',
                owner           TEXT    NOT NULL DEFAULT '',
                due_date        TEXT    NOT NULL DEFAULT '',
                project         TEXT    NOT NULL DEFAULT 'yadi',
                source_fact_id  INTEGER DEFAULT NULL,
                plan_id         INTEGER DEFAULT NULL,
                source          TEXT    NOT NULL DEFAULT 'manual',
                created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
                updated_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS users (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                open_id     TEXT    UNIQUE NOT NULL,
                name        TEXT    NOT NULL DEFAULT '',
                role        TEXT    NOT NULL DEFAULT 'pending',
                project     TEXT    NOT NULL DEFAULT '',
                status      TEXT    NOT NULL DEFAULT 'pending',
                created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
                updated_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS projects (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT    UNIQUE NOT NULL,
                description TEXT    NOT NULL DEFAULT '',
                created_by  TEXT    NOT NULL DEFAULT '',
                active      INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
                updated_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS system_settings (
                key         TEXT    PRIMARY KEY,
                value       TEXT    NOT NULL DEFAULT '',
                updated_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
            );
            CREATE INDEX IF NOT EXISTS idx_facts_status_project    ON facts(status, project);
            CREATE INDEX IF NOT EXISTS idx_todos_status_project     ON todos(status, project);
            CREATE INDEX IF NOT EXISTS idx_conversations_chat_id    ON conversations(chat_id);
            CREATE INDEX IF NOT EXISTS idx_processed_events_id      ON processed_events(event_id);
        """)
        # upgrade: add dimension column if coming from old schema
        try:
            conn.execute("ALTER TABLE facts ADD COLUMN dimension TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
        # upgrade: add updated_at to projects if coming from old schema
        try:
            conn.execute("ALTER TABLE projects ADD COLUMN updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))")
        except Exception:
            pass
        _migrate_legacy(conn)
        _migrate_dimension(conn)
        _seed_initial_project(conn)
        _migrate_project_names(conn)


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


def _migrate_dimension(conn):
    """为已有 facts 补填 dimension 字段，幂等。"""
    for type_, dim in TYPE_TO_DIMENSION.items():
        conn.execute(
            "UPDATE facts SET dimension=? WHERE type=? AND (dimension IS NULL OR dimension='')",
            (dim, type_),
        )


def _migrate_project_names(conn):
    """将 facts/todos 中英文 project='yadi' 统一改为中文名 '雅迪'，幂等。"""
    conn.execute("UPDATE facts SET project='雅迪' WHERE project='yadi'")
    conn.execute("UPDATE todos SET project='雅迪' WHERE project='yadi'")


def _seed_initial_project(conn):
    """确保系统预置项目存在，幂等。"""
    existing = {r[0] for r in conn.execute("SELECT name FROM projects").fetchall()}
    if "默认" not in existing:
        conn.execute(
            "INSERT INTO projects(name, description, created_by) VALUES(?,?,?)",
            ("默认", "系统默认项目", "system"),
        )
    if "雅迪" not in existing:
        conn.execute(
            "INSERT INTO projects(name, description, created_by) VALUES(?,?,?)",
            ("雅迪", "雅迪自动驾驶量产项目", "system"),
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
             project: str = "默认", source: str = "manual") -> int:
    dimension = TYPE_TO_DIMENSION.get(type_, "scope")
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO facts(type,dimension,title,body,status,priority,owner,due_date,project,source)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (type_, dimension, title, body, status, priority, owner, due_date, project, source),
        )
        return cur.lastrowid


def get_fact(fact_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM facts WHERE id=?", (fact_id,)).fetchone()
    return dict(row) if row else None


def update_fact(fact_id: int, **kwargs):
    allowed = {"type", "dimension", "title", "body", "status", "priority", "owner", "due_date"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if "type" in fields and "dimension" not in fields:
        fields["dimension"] = TYPE_TO_DIMENSION.get(fields["type"], "scope")
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


def list_facts(type_: str | None = None, dimension: str | None = None,
               status: str | None = "active", project: str | None = None) -> list:
    clauses, params = ["type != 'report'"], []
    if type_:
        clauses.append("type=?")
        params.append(type_)
    if dimension:
        clauses.append("dimension=?")
        params.append(dimension)
    if status:
        clauses.append("status=?")
        params.append(status)
    if project:
        clauses.append("project=?")
        params.append(project)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_conn() as conn:
        return conn.execute(
            f"SELECT * FROM facts {where} ORDER BY dimension, type, id", params
        ).fetchall()


def find_similar_fact(type_: str, content: str, threshold: int = 2) -> dict | None:
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


# ── 预设假设 CRUD ──────────────────────────────────────────

def add_assumption(title: str, body: str, scope: str = "dept", scope_ref: str = "",
                   confidence: str = "common", source: str = "manual") -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO assumptions(scope,scope_ref,title,body,confidence,source)"
            " VALUES(?,?,?,?,?,?)",
            (scope, scope_ref, title, body, confidence, source),
        )
        return cur.lastrowid


def get_assumption(assumption_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM assumptions WHERE id=?", (assumption_id,)).fetchone()
    return dict(row) if row else None


def update_assumption(assumption_id: int, **kwargs):
    allowed = {"title", "body", "scope", "scope_ref", "confidence", "active"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values())
    with get_conn() as conn:
        conn.execute(
            f"UPDATE assumptions SET {sets}, updated_at=datetime('now','localtime') WHERE id=?",
            (*vals, assumption_id),
        )


def delete_assumption(assumption_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM assumptions WHERE id=?", (assumption_id,))


def list_assumptions(scope: str | None = None, scope_ref: str | None = None,
                     active_only: bool = True) -> list:
    clauses, params = [], []
    if active_only:
        clauses.append("active=1")
    if scope:
        clauses.append("scope=?")
        params.append(scope)
    if scope_ref is not None:
        clauses.append("scope_ref=?")
        params.append(scope_ref)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    order = "ORDER BY CASE confidence WHEN 'universal' THEN 0 WHEN 'common' THEN 1 ELSE 2 END, id"
    with get_conn() as conn:
        return conn.execute(f"SELECT * FROM assumptions {where} {order}", params).fetchall()


# ── 组织单元 CRUD ──────────────────────────────────────────

def add_org_unit(type_: str, name: str, parent_id: int | None = None,
                 feishu_id: str = "", attributes: dict | None = None) -> int:
    attrs = _json.dumps(attributes or {}, ensure_ascii=False)
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO org_units(type,name,parent_id,feishu_id,attributes) VALUES(?,?,?,?,?)",
            (type_, name, parent_id, feishu_id, attrs),
        )
        return cur.lastrowid


def list_org_units(type_: str | None = None, active_only: bool = True) -> list:
    clauses, params = [], []
    if active_only:
        clauses.append("active=1")
    if type_:
        clauses.append("type=?")
        params.append(type_)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_conn() as conn:
        return conn.execute(f"SELECT * FROM org_units {where} ORDER BY parent_id, id", params).fetchall()


def upsert_person(open_id: str, name: str):
    """缓存飞书用户 open_id ↔ 姓名映射到 org_units（type='person'）。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, name FROM org_units WHERE feishu_id=? AND type='person'", (open_id,)
        ).fetchone()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if row:
            if row["name"] != name:
                conn.execute("UPDATE org_units SET name=? WHERE id=?", (name, row["id"]))
        else:
            conn.execute(
                "INSERT INTO org_units(type,name,feishu_id,created_at) VALUES(?,?,?,?)",
                ("person", name, open_id, now),
            )


# ── Todos CRUD ────────────────────────────────────────────

def add_todo(title: str, body: str = "", priority: str = "medium", owner: str = "",
             due_date: str = "", project: str = "默认",
             source_fact_id: int | None = None, plan_id: int | None = None,
             source: str = "manual") -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO todos(title,body,priority,owner,due_date,project,source_fact_id,plan_id,source)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (title, body, priority, owner, due_date, project, source_fact_id, plan_id, source),
        )
        return cur.lastrowid


def get_todo(todo_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()
    return dict(row) if row else None


def update_todo(todo_id: int, **kwargs):
    allowed = {"title", "body", "status", "priority", "owner", "due_date"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values())
    with get_conn() as conn:
        conn.execute(
            f"UPDATE todos SET {sets}, updated_at=datetime('now','localtime') WHERE id=?",
            (*vals, todo_id),
        )


def list_todos(status: str | None = "open", project: str | None = None,
               source_fact_id: int | None = None, plan_id: int | None = None) -> list:
    clauses, params = [], []
    if status:
        clauses.append("status=?")
        params.append(status)
    if project:
        clauses.append("project=?")
        params.append(project)
    if source_fact_id is not None:
        clauses.append("source_fact_id=?")
        params.append(source_fact_id)
    if plan_id is not None:
        clauses.append("plan_id=?")
        params.append(plan_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_conn() as conn:
        return conn.execute(
            f"SELECT * FROM todos {where}"
            " ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, id",
            params,
        ).fetchall()


def get_todos_for_context(project: str = "默认",
                          open_limit: int = 30,
                          done_limit: int = 10,
                          done_days: int = 14) -> str:
    """返回待办事项文本，供 AI 上下文注入。包含 open 条目和近期完成条目，均带时间信息。"""
    from datetime import datetime as _dt, timedelta as _td
    _PRIO = {"high": "高", "medium": "中", "low": "低"}
    _RISK_ZH = {"risk": "风险", "issue": "问题", "blocker": "阻塞", "dependency": "依赖"}
    cutoff = (_dt.now() - _td(days=done_days)).strftime("%Y-%m-%d")

    with get_conn() as conn:
        open_rows = conn.execute(
            "SELECT * FROM todos WHERE status='open' AND project=?"
            " ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, id"
            " LIMIT ?",
            (project, open_limit),
        ).fetchall()
        done_rows = conn.execute(
            "SELECT * FROM todos WHERE status='done' AND project=? AND updated_at >= ?"
            " ORDER BY updated_at DESC LIMIT ?",
            (project, cutoff, done_limit),
        ).fetchall()

    if not open_rows and not done_rows:
        return ""

    # 批量拉取关联 fact 标题
    fact_ids = set()
    for r in open_rows:
        if r["source_fact_id"]: fact_ids.add(r["source_fact_id"])
        if r["plan_id"]:        fact_ids.add(r["plan_id"])
    fact_map: dict[int, dict] = {}
    if fact_ids:
        with get_conn() as conn:
            placeholders = ",".join("?" * len(fact_ids))
            for row in conn.execute(
                f"SELECT id, type, title FROM facts WHERE id IN ({placeholders})",
                list(fact_ids),
            ).fetchall():
                fact_map[row["id"]] = dict(row)

    # 分组
    risk_groups: dict[int, list] = {}
    plan_groups: dict[int, list] = {}
    standalone: list = []
    for r in open_rows:
        r = dict(r)
        if r["source_fact_id"]:
            risk_groups.setdefault(r["source_fact_id"], []).append(r)
        elif r["plan_id"]:
            plan_groups.setdefault(r["plan_id"], []).append(r)
        else:
            standalone.append(r)

    def _fmt(r: dict) -> str:
        p = _PRIO.get(r["priority"], "")
        line = f"- [ ] #T{r['id']} {r['title']}"
        if p and p != "中": line += f" [{p}]"
        if r["owner"]:    line += f"  owner:{r['owner']}"
        if r["due_date"]: line += f"  due:{r['due_date']}"
        line += f"  创建:{r['created_at'][:10]}"
        return line

    lines: list[str] = []

    for fid, items in risk_groups.items():
        fact = fact_map.get(fid, {})
        ft_zh = _RISK_ZH.get(fact.get("type", ""), "事项")
        lines.append(f"**来自{ft_zh} #{fid}（{fact.get('title','?')[:25]}）**")
        lines.extend(_fmt(r) for r in items)

    for pid, items in plan_groups.items():
        fact = fact_map.get(pid, {})
        lines.append(f"**挂载到里程碑 #{pid}（{fact.get('title','?')[:25]}）**")
        lines.extend(_fmt(r) for r in items)

    if standalone:
        lines.append("**独立待办**")
        lines.extend(_fmt(r) for r in standalone)

    if done_rows:
        lines.append(f"**近期完成（{done_days}天内）**")
        for r in done_rows:
            r = dict(r)
            line = f"- [x] #T{r['id']} {r['title']}"
            if r["owner"]: line += f"  owner:{r['owner']}"
            line += f"  完成:{r['updated_at'][:10]}"
            lines.append(line)

    return "\n".join(lines)


# ── AI 上下文拼装（三层结构）─────────────────────────────────

def get_full_context(project: str = "默认") -> dict:
    """返回结构化上下文供 AI 使用。
    Layer 0: 部门预设假设（总是注入）
    Layer 1: 项目级假设
    Layer 2+: facts 按维度分组
    """
    _PRIO = {"high": "高", "medium": "中", "low": "低"}
    _TYPE_ZH = {
        "risk": "风险", "issue": "问题", "blocker": "阻塞", "dependency": "依赖",
        "milestone": "里程碑", "decision": "决策", "process": "流程",
        "team": "人员", "client": "客户", "org": "组织", "knowledge": "知识",
    }

    with get_conn() as conn:
        # Layer 0: 部门通用假设
        dept_rows = conn.execute(
            "SELECT title, body, confidence FROM assumptions"
            " WHERE active=1 AND scope IN ('dept','global')"
            " ORDER BY CASE confidence WHEN 'universal' THEN 0 WHEN 'common' THEN 1 ELSE 2 END, id"
        ).fetchall()

        # Layer 1: 项目专属假设
        proj_rows = conn.execute(
            "SELECT title, body, confidence FROM assumptions"
            " WHERE active=1 AND scope='project' AND scope_ref=?"
            " ORDER BY id", (project,)
        ).fetchall()

        # Layer 2: 风险（dimension=risk）
        risk_rows = conn.execute(
            "SELECT id, type, title, body, owner, priority, due_date, created_at, updated_at FROM facts"
            " WHERE dimension='risk' AND status='active' AND project=?"
            " ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, id",
            (project,)
        ).fetchall()

        # Layer 3: 进度（dimension=schedule）
        schedule_rows = conn.execute(
            "SELECT id, type, title, body, due_date, created_at, updated_at FROM facts"
            " WHERE dimension='schedule' AND status='active' AND project=?"
            " ORDER BY due_date, id", (project,)
        ).fetchall()

        # Layer 4: 决策（dimension=decision）
        decision_rows = conn.execute(
            "SELECT id, title, body, created_at, updated_at FROM facts"
            " WHERE dimension='decision' AND status='active'"
            " ORDER BY id"
        ).fetchall()

        # Layer 5: 相关方 + 资源（stakeholder/resource/scope）
        ref_rows = conn.execute(
            "SELECT id, type, title, body, updated_at FROM facts"
            " WHERE dimension IN ('stakeholder','resource','scope') AND status='active'"
            " ORDER BY dimension, id"
        ).fetchall()

    def fmt_assumption(rows):
        if not rows:
            return ""
        lines = []
        for r in rows:
            tag = _CONFIDENCE_LABEL.get(r["confidence"], r["confidence"])
            lines.append(f"[{tag}] {r['title']}：{r['body']}")
        return "\n".join(lines)

    def fmt_risks(rows):
        if not rows:
            return ""
        lines = []
        for r in rows:
            t = _TYPE_ZH.get(r["type"], r["type"])
            p = _PRIO.get(r["priority"], r["priority"])
            line = f"[{t}·{p}] #{r['id']} {r['title']}：{r['body']}"
            if r["owner"]:    line += f"（负责人：{r['owner']}）"
            if r["due_date"]: line += f"（截止：{r['due_date']}）"
            line += f"（记录:{r['created_at'][:10]}"
            if r["updated_at"][:10] != r["created_at"][:10]:
                line += f" 更新:{r['updated_at'][:10]}"
            line += "）"
            lines.append(line)
        return "\n".join(lines)

    def fmt_schedule(rows):
        if not rows:
            return ""
        lines = []
        for r in rows:
            t = _TYPE_ZH.get(r["type"], r["type"])
            line = f"[{t}] #{r['id']} {r['title']}"
            if r["due_date"]: line += f"（目标:{r['due_date']}）"
            line += f"（记录:{r['created_at'][:10]}"
            if r["updated_at"][:10] != r["created_at"][:10]:
                line += f" 更新:{r['updated_at'][:10]}"
            line += "）"
            if r["body"] and r["body"] != r["title"]: line += f"：{r['body'][:80]}"
            lines.append(line)
        return "\n".join(lines)

    def fmt_generic(rows):
        if not rows:
            return ""
        parts = []
        for r in rows:
            updated = r["updated_at"][:10] if r["updated_at"] else ""
            header = f"【{r['title']}】" + (f"（更新:{updated}）" if updated else "")
            parts.append(f"{header}\n{r['body']}")
        return "\n\n".join(parts)

    return {
        "dept_assumptions":    fmt_assumption(dept_rows),
        "project_assumptions": fmt_assumption(proj_rows),
        "risks":               fmt_risks(risk_rows),
        "schedule":            fmt_schedule(schedule_rows),
        "decisions":           fmt_generic(decision_rows),
        "references":          fmt_generic(ref_rows),
        "todos":               get_todos_for_context(project),
    }


# ── 旧版兼容：知识库 / 风险文本（供 nightly review 等使用）───

def get_knowledge_text() -> str:
    ctx = get_full_context()
    sections = []
    if ctx["dept_assumptions"]:
        sections.append("=== 部门预设 ===\n" + ctx["dept_assumptions"])
    if ctx["project_assumptions"]:
        sections.append("=== 项目背景 ===\n" + ctx["project_assumptions"])
    if ctx["decisions"]:
        sections.append("=== 决策记录 ===\n" + ctx["decisions"])
    if ctx["references"]:
        sections.append("=== 参考信息 ===\n" + ctx["references"])
    return "\n\n".join(sections)


def get_risks_text(project: str = "默认") -> str:
    return get_full_context(project)["risks"]


def get_all_facts_for_review() -> str:
    _TYPE_ZH = {
        "risk": "风险", "issue": "问题", "blocker": "阻塞", "dependency": "依赖",
        "milestone": "里程碑", "decision": "决策", "team": "人员",
        "client": "客户", "org": "组织", "process": "流程", "knowledge": "知识",
    }
    _PRIO_ZH = {"high": "高", "medium": "中", "low": "低"}
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, type, dimension, title, body, priority, owner, due_date, project, updated_at"
            " FROM facts WHERE status='active' AND type != 'report' ORDER BY dimension, type, id"
        ).fetchall()
    if not rows:
        return ""
    lines = []
    for r in rows:
        type_label = _TYPE_ZH.get(r["type"], r["type"])
        prio  = f" 优先级:{_PRIO_ZH.get(r['priority'], r['priority'])}" if r["priority"] else ""
        owner = f" 负责人:{r['owner']}" if r["owner"] else ""
        due   = f" 截止:{r['due_date']}" if r["due_date"] else ""
        project = f" 项目:{r['project']}" if r["project"] else ""
        updated = r["updated_at"][:10] if r["updated_at"] else "未知"
        lines.append(f"#{r['id']} [{type_label}/{r['dimension']}]{project}{prio}{owner}{due} 最后更新:{updated}")
        lines.append(f"  标题: {r['title']}")
        lines.append(f"  正文: {r['body'][:200]}")
        lines.append("")
    return "\n".join(lines)


# ── 旧版兼容：knowledge_blocks 接口 ───────────────────────

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
            " updated_at FROM facts ORDER BY dimension, type, id"
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


# ── 旧版兼容：risks 接口 ───────────────────────────────────

def add_risk(type_: str, title: str, description: str,
             owner: str = "", priority: str = "medium",
             due_date: str = "", project: str = "默认") -> int:
    return add_fact(type_, title, description,
                    priority=priority, owner=owner, due_date=due_date,
                    project=project, source="manual")


def list_risks(status: str | None = None, project: str | None = None) -> list:
    db_status = _RISK_STATUS_IN.get(status, status) if status else None
    with get_conn() as conn:
        base = (
            "SELECT id, type, title, body AS description, owner, priority,"
            " status, due_date, project, created_at, updated_at"
            " FROM facts WHERE dimension='risk'"
        )
        clauses, params = [], []
        if project is not None:
            clauses.append("project=?")
            params.append(project)
        if db_status:
            clauses.append("status=?")
            params.append(db_status)
        where = (" AND " + " AND ".join(clauses)) if clauses else ""
        order = " ORDER BY id" if db_status else " ORDER BY status, id"
        rows = conn.execute(base + where + order, params).fetchall()
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


_TODO_PENDING_PREFIX = "todo|"
_MERGE_PENDING_PREFIX = "merge|"
_ACTION_PENDING_PREFIX = "action|"


def save_pending_todos(chat_id: str, todos: list[dict]):
    save_pending(_TODO_PENDING_PREFIX + chat_id, todos)


def get_pending_todos(chat_id: str) -> list[dict]:
    return get_pending(_TODO_PENDING_PREFIX + chat_id)


def pop_pending_todo(chat_id: str, index: int) -> tuple[dict | None, list[dict]]:
    return pop_pending_item(_TODO_PENDING_PREFIX + chat_id, index)


def clear_pending_todos(chat_id: str):
    clear_pending(_TODO_PENDING_PREFIX + chat_id)


def save_pending_merges(chat_id: str, merges: list[dict]):
    save_pending(_MERGE_PENDING_PREFIX + chat_id, merges)


def get_pending_merges(chat_id: str) -> list[dict]:
    return get_pending(_MERGE_PENDING_PREFIX + chat_id)


def pop_pending_merge(chat_id: str, index: int) -> tuple[dict | None, list[dict]]:
    return pop_pending_item(_MERGE_PENDING_PREFIX + chat_id, index)


def clear_pending_merges(chat_id: str):
    clear_pending(_MERGE_PENDING_PREFIX + chat_id)


def save_pending_actions(chat_id: str, actions: list[dict]):
    save_pending(_ACTION_PENDING_PREFIX + chat_id, actions)


def get_pending_actions(chat_id: str) -> list[dict]:
    return get_pending(_ACTION_PENDING_PREFIX + chat_id)


def pop_pending_action(chat_id: str, index: int) -> tuple[dict | None, list[dict]]:
    return pop_pending_item(_ACTION_PENDING_PREFIX + chat_id, index)


def clear_pending_actions(chat_id: str):
    clear_pending(_ACTION_PENDING_PREFIX + chat_id)


def save_nightly_review(content: str) -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO facts(type,dimension,title,body,status,project,source)"
            " VALUES(?,?,?,?,?,?,?)",
            ("report", "system", f"AI数据洗盘 {today}", content, "active", "system", "ai"),
        )
        return cur.lastrowid


def get_latest_nightly_review() -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT body FROM facts WHERE type='report' AND project='system'"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return row["body"] if row else None


def get_setting(key: str, default: str = "") -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM system_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO system_settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now','localtime')",
            (key, value),
        )


# ── 群聊项目绑定 ──────────────────────────────────────────

def get_chat_binding(chat_id: str) -> str | None:
    """返回群聊绑定的项目名，未绑定返回 None。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT project FROM chat_bindings WHERE chat_id=?", (chat_id,)
        ).fetchone()
    return row["project"] if row else None


def set_chat_binding(chat_id: str, project: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO chat_bindings(chat_id, project) VALUES(?,?)",
            (chat_id, project),
        )


def delete_chat_binding(chat_id: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM chat_bindings WHERE chat_id=?", (chat_id,))


def list_chat_bindings() -> list:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM chat_bindings ORDER BY chat_id").fetchall()


# ── 用户管理 CRUD ─────────────────────────────────────────

def get_user(open_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE open_id=?", (open_id,)).fetchone()
    return dict(row) if row else None


def upsert_user(open_id: str, name: str = "", role: str = "pending",
                project: str = "", status: str = "pending") -> int:
    """插入或更新用户（以 open_id 为唯一键）。"""
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM users WHERE open_id=?", (open_id,)).fetchone()
        if row:
            conn.execute(
                "UPDATE users SET name=?, role=?, project=?, status=?,"
                " updated_at=datetime('now','localtime') WHERE open_id=?",
                (name, role, project, status, open_id),
            )
            return row["id"]
        cur = conn.execute(
            "INSERT INTO users(open_id, name, role, project, status) VALUES(?,?,?,?,?)",
            (open_id, name, role, project, status),
        )
        return cur.lastrowid


def update_user(open_id: str, **kwargs):
    allowed = {"name", "role", "project", "status"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values())
    with get_conn() as conn:
        conn.execute(
            f"UPDATE users SET {sets}, updated_at=datetime('now','localtime') WHERE open_id=?",
            (*vals, open_id),
        )


def list_users(role: str | None = None, status: str | None = None) -> list:
    clauses, params = [], []
    if role:
        clauses.append("role=?")
        params.append(role)
    if status:
        clauses.append("status=?")
        params.append(status)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_conn() as conn:
        return conn.execute(
            f"SELECT * FROM users {where} ORDER BY role, name", params
        ).fetchall()


def delete_user(open_id: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM users WHERE open_id=?", (open_id,))


# ── 项目管理 CRUD ─────────────────────────────────────────

def get_project_by_name(name: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM projects WHERE name=?", (name,)).fetchone()
    return dict(row) if row else None


def get_project(project_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    return dict(row) if row else None


def add_project(name: str, description: str = "", created_by: str = "") -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO projects(name, description, created_by) VALUES(?,?,?)",
            (name, description, created_by),
        )
        return cur.lastrowid


def list_projects(active_only: bool = True) -> list:
    with get_conn() as conn:
        if active_only:
            return conn.execute(
                "SELECT * FROM projects WHERE active=1 ORDER BY id"
            ).fetchall()
        return conn.execute("SELECT * FROM projects ORDER BY id").fetchall()


def update_project(project_id: int, **kwargs):
    allowed = {"name", "description", "active"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values())
    with get_conn() as conn:
        conn.execute(
            f"UPDATE projects SET {sets}, updated_at=datetime('now','localtime') WHERE id=?",
            (*vals, project_id),
        )


# ── 系统统计 ──────────────────────────────────────────────

def get_system_stats() -> dict:
    with get_conn() as conn:
        user_rows = conn.execute("SELECT role, status, COUNT(*) as cnt FROM users GROUP BY role, status").fetchall()
        project_count = conn.execute("SELECT COUNT(*) FROM projects WHERE active=1").fetchone()[0]
        fact_rows = conn.execute(
            "SELECT type, COUNT(*) as cnt FROM facts WHERE status='active' AND type!='report' GROUP BY type"
        ).fetchall()
        todo_rows = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM todos GROUP BY status"
        ).fetchall()
        review_row = conn.execute(
            "SELECT created_at FROM facts WHERE type='report' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return {
        "users": [dict(r) for r in user_rows],
        "project_count": project_count,
        "facts": [dict(r) for r in fact_rows],
        "todos": [dict(r) for r in todo_rows],
        "last_review": review_row["created_at"][:10] if review_row else "无",
        "review_mode": get_setting("nightly_review_mode", "report_only"),
    }


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
