"""
每日风险/问题通知脚本
由 crontab 调用：0 9 * * 1-5 cd ~/pm-assist && venv/bin/python notify.py >> logs/notify.log 2>&1
"""
import asyncio
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

import db
import feishu
from config import FEISHU_APP_ID, FEISHU_APP_SECRET

# 接收通知的用户列表（open_id）
NOTIFY_USERS = [
    "ou_6373ec8f094be9c36823255d75f9ef11",  # 佟海鹏
]

_TYPE_ZH = {"risk": "风险", "issue": "问题", "blocker": "阻塞项", "dependency": "依赖"}


def build_report() -> str:
    db.init_db()
    risks = db.list_risks(status="open")
    today = datetime.now().strftime("%m月%d日")

    if not risks:
        return f"📋 {today} 日报\n\n当前无未关闭风险/问题 ✅"

    high = [r for r in risks if r["priority"] == "high"]
    medium = [r for r in risks if r["priority"] == "medium"]
    low = [r for r in risks if r["priority"] == "low"]

    lines = [f"📋 {today} 风险与问题日报\n"]

    def fmt(r):
        typ = _TYPE_ZH.get(r["type"], r["type"])
        owner = f"（{r['owner']}）" if r["owner"] else ""
        due = f" ⏰{r['due_date']}" if r["due_date"] else ""
        return f"  R{r['id']} [{typ}] {r['title']}{owner}{due}"

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


async def main():
    report = build_report()
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] 发送日报：\n{report}\n")
    for uid in NOTIFY_USERS:
        await feishu.send_text_to_user(uid, report, FEISHU_APP_ID, FEISHU_APP_SECRET)
        print(f"  → 已发送至 {uid}")


if __name__ == "__main__":
    asyncio.run(main())
