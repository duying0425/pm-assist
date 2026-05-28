from __future__ import annotations

"""
每日早报模块。
可作为独立脚本由 crontab 调用：
  0 9 * * 1-5 cd ~/pm-assist && venv/bin/python notify.py >> logs/notify.log 2>&1
也可由 main.py 的 APScheduler 直接调用（推荐，已内置）。
"""
import asyncio
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

import db
import feishu
from config import ADMIN_OPEN_IDS, NOTIFY_OPEN_IDS, FEISHU_APP_ID, FEISHU_APP_SECRET

_TYPE_ZH = {"risk": "风险", "issue": "问题", "blocker": "阻塞项", "dependency": "依赖"}


def build_risk_section(project: str | None = None) -> str:
    """纯文本风险摘要（兼容旧接口，早报改用 build_morning_report_card）。"""
    db.init_db()
    risks = db.list_risks(status="open", project=project)
    today = datetime.now().strftime("%m月%d日")
    label = f"【{project}】" if project else ""

    if not risks:
        return f"📋 {today} {label}日报\n\n当前无未关闭风险/问题 ✅"

    high   = [r for r in risks if r["priority"] == "high"]
    medium = [r for r in risks if r["priority"] == "medium"]
    low    = [r for r in risks if r["priority"] == "low"]

    lines = [f"📋 {today} {label}风险与问题日报\n"]

    def fmt(r):
        typ   = _TYPE_ZH.get(r["type"], r["type"])
        owner = f"（{r['owner']}）" if r["owner"] else ""
        due   = f" ⏰{r['due_date']}" if r["due_date"] else ""
        return f"  #{r['id']} [{typ}] {r['title']}{owner}{due}"

    if high:
        lines.append(f"🔴 高优先级 · {len(high)} 条")
        lines.extend(fmt(r) for r in high)
    if medium:
        lines.append(f"\n🟡 中优先级 · {len(medium)} 条")
        lines.extend(fmt(r) for r in medium)
    if low:
        lines.append(f"\n⚪ 低优先级 · {len(low)} 条")
        lines.extend(fmt(r) for r in low)

    lines.append(f"\n共 {len(risks)} 条待处理 | 发送 /admin risk list 查看详情")
    return "\n".join(lines)


def build_morning_report(review: str | None = None) -> str:
    """组合风险摘要 + AI洗盘决策报告（兼容旧接口）。"""
    parts = [build_risk_section()]
    if review:
        divider = "─" * 24
        parts.append(f"\n\n🤖 AI数据洗盘·决策报告\n{divider}\n{review}")
    return "".join(parts)


def get_morning_cards(review_text: str | None = None) -> dict[str | None, dict]:
    """返回各项目的早报卡片 dict，key=project_name（None=全项目）。
    全项目卡片给管理员/notify用户；各项目卡片给对应 PM 用户。
    """
    today = datetime.now().strftime("%m月%d日")
    projects = db.list_projects(active_only=True)
    cards: dict[str | None, dict] = {}

    # 全项目卡片（admin/notify 收件人）
    all_risks = db.list_risks(status="open")
    cards[None] = feishu.build_morning_report_card("", all_risks, review_text, today)

    # 每个项目单独卡片（供 PM 接收，也包含洗盘摘要）
    for proj in projects:
        name = proj["name"]
        proj_risks = db.list_risks(status="open", project=name)
        cards[name] = feishu.build_morning_report_card(name, proj_risks, review_text, today)

    return cards


async def main():
    review = db.get_latest_nightly_review()
    report = build_morning_report(review)
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] 发送日报：\n{report}\n")
    recipients = NOTIFY_OPEN_IDS | ADMIN_OPEN_IDS
    if not recipients:
        print("  ⚠ NOTIFY_OPEN_IDS 和 ADMIN_OPEN_IDS 均未配置，无人接收")
        return
    for uid in recipients:
        await feishu.send_text_to_user(uid, report, FEISHU_APP_ID, FEISHU_APP_SECRET)
        print(f"  → 已发送至 {uid}")


if __name__ == "__main__":
    asyncio.run(main())
