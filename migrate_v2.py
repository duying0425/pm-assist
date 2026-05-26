"""
一次性迁移脚本 v2：
1. 创建新表（assumptions, org_units），为 facts 补 dimension 字段
2. 植入组织结构（东软睿驰 AD 部门）
3. 植入部门预设假设（从系统 prompt 里提炼的公认知识）
4. 将现有 process/knowledge 类型的 facts 扫描一遍，标记可能应升级为 assumption 的条目

服务器上运行：
  cd ~/pm-assist && venv/bin/python migrate_v2.py
"""

import sqlite3
import json
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "pm_assist.db"

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

# ── 部门预设假设种子数据 ──────────────────────────────────────
# scope: dept（全部门公认）| project（项目专属）
# confidence: universal（铁律）| common（通常）| assumed（推测）

ASSUMPTION_SEEDS = [
    # ─── 铁律（universal）───
    {
        "scope": "dept", "scope_ref": "", "confidence": "universal",
        "title": "PM角色边界",
        "body": "PM不直接管理工程师，职责是协调各团队、推进节点、管理风险和对外沟通。遇到技术决策找架构师，遇到资源排期找团队负责人。",
    },
    {
        "scope": "dept", "scope_ref": "", "confidence": "universal",
        "title": "产品定位",
        "body": "当前业务为定点后智驾解决方案开发，非平台类产品。每个项目面向特定OEM客户交付，方案高度定制化，不可直接复用到其他客户项目。",
    },
    {
        "scope": "dept", "scope_ref": "", "confidence": "universal",
        "title": "ASPICE流程执行原则",
        "body": "项目遵循ASPICE流程精神指导，但根据团队规模和成本约束灵活裁剪，非严格按标准执行全套活动。裁剪决策需由PM记录并与客户对齐。",
    },
    # ─── 通常（common）───
    {
        "scope": "dept", "scope_ref": "", "confidence": "common",
        "title": "OEM客户决策周期",
        "body": "OEM客户内部签字确认流程通常需要1-2周。范围变更、交付物确认、合同补充需提前预留此周期，不能按开发完成时间倒推。",
    },
    {
        "scope": "dept", "scope_ref": "", "confidence": "common",
        "title": "书面确认原则",
        "body": "重要决策、范围变更、接口定义须有书面记录（邮件或飞书消息）。口头确认风险高，客户沟通后需主动发确认邮件收口。",
    },
    {
        "scope": "dept", "scope_ref": "", "confidence": "common",
        "title": "外部依赖响应周期",
        "body": "硬件验证、工具链获取、第三方SDK提供等存在外部供应商依赖，响应周期通常1-2周。相关里程碑需在节点前2周触发跟进动作。",
    },
    {
        "scope": "dept", "scope_ref": "", "confidence": "common",
        "title": "团队资源排期",
        "body": "核心专家（如架构师、算法骨干）通常同时承担多个项目。需要其介入的节点须提前1-2周沟通排期，而非临时拉人。",
    },
    {
        "scope": "dept", "scope_ref": "", "confidence": "common",
        "title": "团队分工与协作范式",
        "body": "团队包括：领导组、售前、架构师、PM、产品设计与定义、感知、规划控制、基础软件开发、测试、传感器评价与管理、环境实施。"
                "PM协调上述内部团队和外部总包/二级供应商，跨团队依赖需显式拉齐，不能假设默认对齐。",
    },
    {
        "scope": "dept", "scope_ref": "", "confidence": "common",
        "title": "硬件验证约束",
        "body": "硬件验证必须在实验室物理环境下进行，不支持远程执行。需提前预约实验室资源和硬件到位时间。",
    },
    # ─── 推测（assumed）───
    {
        "scope": "dept", "scope_ref": "", "confidence": "assumed",
        "title": "SW交付物提前冻结",
        "body": "软件交付物（SW deliverables）通常需要先于OEM硬件集成窗口2周冻结，以留出集成准备和内部验证时间。具体节点以项目计划为准。",
    },
    # ─── 雅迪项目专属 ───
    {
        "scope": "project", "scope_ref": "yadi", "confidence": "common",
        "title": "雅迪变更确认要求",
        "body": "雅迪项目的所有范围变更需要三方书面确认：雅迪客户方 + 东软睿驰 + 总包（如有）。缺少任一方确认均视为未生效。",
    },
    {
        "scope": "project", "scope_ref": "yadi", "confidence": "common",
        "title": "雅迪项目定位",
        "body": "雅迪是当前主要OEM客户，属于定点后开发阶段。项目处于方案开发和集成验证周期内，对进度和质量敏感度高。",
    },
]

# ── 组织结构种子数据 ────────────────────────────────────────

def seed_org_units(conn):
    existing = conn.execute("SELECT COUNT(*) FROM org_units").fetchone()[0]
    if existing > 0:
        print(f"  org_units 已有 {existing} 条，跳过植入")
        return 0

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 根节点
    conn.execute(
        "INSERT INTO org_units(type,name,parent_id,created_at) VALUES(?,?,NULL,?)",
        ("company", "东软睿驰", now),
    )
    company_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    conn.execute(
        "INSERT INTO org_units(type,name,parent_id,created_at) VALUES(?,?,?,?)",
        ("dept", "自动驾驶事业部", company_id, now),
    )
    dept_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    teams = [
        "领导组", "售前团队", "架构师团队", "PM团队",
        "产品设计与定义团队", "感知团队", "规划控制团队",
        "基础软件开发团队", "测试团队", "传感器评价与管理团队", "环境实施团队",
    ]
    for team in teams:
        conn.execute(
            "INSERT INTO org_units(type,name,parent_id,created_at) VALUES(?,?,?,?)",
            ("team", team, dept_id, now),
        )

    # 客户侧
    conn.execute(
        "INSERT INTO org_units(type,name,parent_id,created_at) VALUES(?,?,NULL,?)",
        ("client_org", "雅迪", now),
    )

    count = 1 + 1 + len(teams) + 1
    print(f"  植入 org_units: {count} 条")
    return count


def seed_assumptions(conn):
    existing = conn.execute("SELECT COUNT(*) FROM assumptions").fetchone()[0]
    if existing > 0:
        print(f"  assumptions 已有 {existing} 条，跳过植入")
        return 0

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for a in ASSUMPTION_SEEDS:
        conn.execute(
            "INSERT INTO assumptions(scope,scope_ref,title,body,confidence,source,active,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (a["scope"], a["scope_ref"], a["title"], a["body"],
             a["confidence"], "seed", 1, now, now),
        )
    count = len(ASSUMPTION_SEEDS)
    print(f"  植入 assumptions: {count} 条")
    return count


def add_dimension_column(conn):
    try:
        conn.execute("ALTER TABLE facts ADD COLUMN dimension TEXT NOT NULL DEFAULT ''")
        print("  facts.dimension 列已添加")
    except Exception:
        print("  facts.dimension 列已存在，跳过")


def migrate_dimension(conn):
    total = 0
    for type_, dim in TYPE_TO_DIMENSION.items():
        cur = conn.execute(
            "UPDATE facts SET dimension=? WHERE type=? AND (dimension IS NULL OR dimension='')",
            (dim, type_),
        )
        total += cur.rowcount
    print(f"  补填 dimension 字段: {total} 条 facts")
    return total


def scan_promotable_facts(conn):
    """扫描 process/knowledge 类型中适合升级为 assumption 的条目，仅打印建议，不自动操作。"""
    rows = conn.execute(
        "SELECT id, type, title, body FROM facts"
        " WHERE status='active' AND type IN ('process','knowledge')"
        " ORDER BY type, id"
    ).fetchall()
    if not rows:
        print("\n  无 process/knowledge 类型的 active 条目需要检查")
        return
    print(f"\n  以下 {len(rows)} 条 process/knowledge 条目建议人工审查是否升级为 assumption：")
    for r in rows:
        print(f"  #{r['id']} [{r['type']}] {r['title']}")
        print(f"    正文前80字：{r['body'][:80]}")
        print(f"    建议命令：/admin assumption add dept common {r['title']} | {r['body'][:60]}...")
        print(f"    确认升级后执行：/admin fact archive {r['id']}")
        print()


def main():
    if not DB_PATH.exists():
        print(f"数据库不存在：{DB_PATH}")
        print("请先启动一次服务让 init_db() 创建数据库，再运行此脚本")
        return

    print(f"迁移目标：{DB_PATH}")
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # 1. 确保新表存在
    conn.executescript("""
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
    """)
    print("新表已就绪")

    # 2. dimension 列
    add_dimension_column(conn)

    # 3. 补填 dimension
    migrate_dimension(conn)

    # 4. 植入组织结构
    seed_org_units(conn)

    # 5. 植入假设
    seed_assumptions(conn)

    conn.commit()

    # 6. 扫描可升级条目（只打印建议，不自动操作）
    scan_promotable_facts(conn)

    conn.close()

    print("\n迁移完成。")
    print("建议重启服务：kill $(pgrep -f uvicorn) && nohup venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000 >> logs/app.log 2>&1 &")


if __name__ == "__main__":
    main()
