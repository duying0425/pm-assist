from pathlib import Path

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

import db

router = APIRouter(prefix="/admin")

_HTML = Path(__file__).parent / "static" / "admin.html"


@router.get("/", response_class=HTMLResponse)
def admin_index():
    return _HTML.read_text(encoding="utf-8")


# ── Stats / Projects ───────────────────────────────────────

@router.get("/api/stats")
def api_stats():
    return db.get_system_stats()


@router.get("/api/projects")
def api_list_projects():
    return [dict(r) for r in db.list_projects(active_only=False)]


@router.post("/api/projects")
def api_create_project(data: dict):
    try:
        pid = db.add_project(data["name"], data.get("description", ""))
        return {"id": pid}
    except Exception:
        return {"error": "项目名已存在"}


# ── Facts ──────────────────────────────────────────────────

@router.get("/api/facts")
def api_list_facts(
    fact_type: str = Query(default="", alias="type"),
    status: str = Query(default="active"),
    project: str = Query(default=""),
):
    rows = db.list_facts(
        type_=fact_type or None,
        status=status or None,
        project=project or None,
    )
    return [dict(r) for r in rows]


@router.post("/api/facts")
def api_create_fact(data: dict):
    fid = db.add_fact(
        data["type"], data["title"], data.get("body", ""),
        status=data.get("status", "active"),
        priority=data.get("priority", ""),
        owner=data.get("owner", ""),
        due_date=data.get("due_date", ""),
        project=data.get("project", "默认"),
        source="manual",
    )
    return {"id": fid}


@router.patch("/api/facts/{fact_id}")
def api_update_fact(fact_id: int, data: dict):
    db.update_fact(fact_id, **data)
    return {"ok": True}


@router.delete("/api/facts/{fact_id}")
def api_archive_fact(fact_id: int):
    db.update_fact(fact_id, status="archived")
    return {"ok": True}


# ── Todos ──────────────────────────────────────────────────

@router.get("/api/todos")
def api_list_todos(
    status: str = Query(default="open"),
    project: str = Query(default=""),
):
    rows = db.list_todos(status=status or None, project=project or None)
    return [dict(r) for r in rows]


@router.post("/api/todos")
def api_create_todo(data: dict):
    tid = db.add_todo(
        data["title"],
        body=data.get("body", ""),
        priority=data.get("priority", "medium"),
        owner=data.get("owner", ""),
        due_date=data.get("due_date", ""),
        project=data.get("project", "默认"),
        source="manual",
    )
    return {"id": tid}


@router.patch("/api/todos/{todo_id}")
def api_update_todo(todo_id: int, data: dict):
    db.update_todo(todo_id, **data)
    return {"ok": True}


# ── Users ──────────────────────────────────────────────────

@router.get("/api/users")
def api_list_users(
    role: str = Query(default=""),
    status: str = Query(default=""),
):
    rows = db.list_users(role=role or None, status=status or None)
    return [dict(r) for r in rows]


@router.patch("/api/users/{open_id}")
def api_update_user(open_id: str, data: dict):
    db.update_user(open_id, **data)
    return {"ok": True}


# ── Assumptions ────────────────────────────────────────────

@router.get("/api/assumptions")
def api_list_assumptions(
    scope: str = Query(default=""),
    active_only: bool = Query(default=True),
):
    rows = db.list_assumptions(scope=scope or None, active_only=active_only)
    return [dict(r) for r in rows]


@router.post("/api/assumptions")
def api_create_assumption(data: dict):
    aid = db.add_assumption(
        data["title"], data.get("body", ""),
        scope=data.get("scope", "dept"),
        scope_ref=data.get("scope_ref", ""),
        confidence=data.get("confidence", "common"),
    )
    return {"id": aid}


@router.patch("/api/assumptions/{assumption_id}")
def api_update_assumption(assumption_id: int, data: dict):
    db.update_assumption(assumption_id, **data)
    return {"ok": True}


@router.delete("/api/assumptions/{assumption_id}")
def api_archive_assumption(assumption_id: int):
    db.update_assumption(assumption_id, active=0)
    return {"ok": True}
