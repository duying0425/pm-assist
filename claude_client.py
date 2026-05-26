from __future__ import annotations

from datetime import datetime

from openai import AsyncOpenAI
from config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL, AI_MODEL

_client = AsyncOpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url=OPENROUTER_BASE_URL,
)

# 静态角色定义（团队知识和项目背景改为从DB动态注入）
_ROLE = """你是东软睿驰自动驾驶团队的内部PM助手，专门帮助PM（尤其是新人PM）处理日常项目管理工作。

你的职责：
- 指导PM如何协调各团队、推进项目节点
- 提供客户沟通话术建议
- 帮助识别、跟踪和管理风险
- 梳理工作优先级和跨团队分工
- 解答公司流程和规范相关问题
- 帮助新人PM避免内耗和常见失误

回答风格：简洁实用，直接给可操作建议；多步骤时给清单；判断该找谁时明确说人/团队名称。
回答格式：使用纯文本，不要使用Markdown符号（如##标题、**加粗**、*斜体*、`代码`等），列表用数字序号或短横线加空格即可。

重要约束：
- 当用户提供事实类信息（人员分工、里程碑节点、风险、决策等），不要说"已记录"、"已保存"、"我会记住"等暗示已持久化的话，系统会自动提示用户确认是否保存到知识库
- 信息提取和保存由系统后台处理，你的职责是理解并给出建议，不是替代存储动作"""

# 管理员额外权限说明（注入 system prompt）
_ADMIN_EXTRA = """
---
## 【管理员权限说明】
当前用户为系统管理员，你可以帮助其了解和分析系统内部数据。
数据库主要表结构：
- users: 用户注册（open_id, name, role[super_admin/pm/member/pending], project, status[active/pending/inactive]）
- projects: 项目列表（name, description, active）
- facts: 项目事项（type, dimension, title, body, status, priority, owner, due_date）
- todos: 待办事项（title, status, priority, owner, source_fact_id, plan_id）
- assumptions: 预设背景知识（scope, title, body, confidence[universal/common/assumed]）
- org_units: 组织结构（type, name, parent_id, feishu_id）
- conversations: 对话历史（chat_id, role, content）
当前 AI 上下文已注入全部 active 条目详情，管理员也可使用 /admin 系列命令查询实际数据。"""

# 成员版角色说明（轻量，不涉及PM专属工作流）
_ROLE_MEMBER = """你是东软睿驰自动驾驶团队的内部助手。你可以回答关于团队、项目背景、工作流程的问题，提供一般性建议。
回答风格：简洁友好，聚焦团队和项目背景知识，不涉及PM内部工作细节。"""

_CONTEXT_TEMPLATE = """{role}

{sender_section}{dept_section}{project_section}{todos_section}{risk_section}{schedule_section}{decision_section}{ref_section}"""


def _build_system(context: dict, sender_info: str = "", role: str = "pm") -> str:
    def section(title: str, content: str) -> str:
        if not content:
            return ""
        return f"\n---\n## {title}\n{content}\n"

    if role == "member":
        base_role = _ROLE_MEMBER
        todos_section    = ""
        risk_section     = ""
        decision_section = ""
    elif role == "super_admin":
        base_role    = _ROLE + _ADMIN_EXTRA
        todos_section    = section("待办事项（带追溯和时间信息）", context.get("todos", ""))
        risk_section     = section("当前活跃风险与问题", context.get("risks") or "（暂无已登记风险）")
        decision_section = section("关键决策记录", context.get("decisions", ""))
    else:  # pm
        base_role    = _ROLE
        todos_section    = section("待办事项（带追溯和时间信息）", context.get("todos", ""))
        risk_section     = section("当前活跃风险与问题", context.get("risks") or "（暂无已登记风险）")
        decision_section = section("关键决策记录", context.get("decisions", ""))

    sender_section   = section("当前对话用户", sender_info) if sender_info else ""
    dept_section     = section("部门预设（团队公认背景知识）", context.get("dept_assumptions", ""))
    project_section  = section("当前项目背景", context.get("project_assumptions", ""))
    schedule_section = section("里程碑与计划节点", context.get("schedule", ""))
    ref_section      = section("相关方与参考信息", context.get("references", ""))

    return _CONTEXT_TEMPLATE.format(
        role=base_role,
        sender_section=sender_section,
        dept_section=dept_section,
        project_section=project_section,
        todos_section=todos_section,
        risk_section=risk_section,
        schedule_section=schedule_section,
        decision_section=decision_section,
        ref_section=ref_section,
    )


async def chat(history: list[dict], context: dict,
               sender_info: str = "", role: str = "pm") -> str:
    system = _build_system(context, sender_info=sender_info, role=role)
    response = await _client.chat.completions.create(
        model=AI_MODEL,
        max_tokens=4000,
        messages=[{"role": "system", "content": system}] + history,
    )
    return response.choices[0].message.content


_EXTRACT_PROMPT = """你是一个信息提取助手。分析下面这段项目相关的消息，提取值得存入知识库的关键信息。

只提取以下类型（忽略闲聊、问候、无实质内容的话）：
- risk: 风险或问题（影响项目进度/质量的事项）
- milestone: 里程碑、时间节点、计划安排
- decision: 重要决定或达成的结论
- team: 人员分工、联系人、职责
- client: 客户相关信息（需求、态度、要求）

拆分规则（重要）：
- 每个独立事项必须单独一条，发现几个就输出几条，绝不合并
- 同一条消息中的多个风险、多个里程碑节点、多个人员各自独立
- 每条 content 只描述一件事，不超过60字

示例：
消息："张工说BSP SDK还没验证好，测试环境也没搭，预计5月底完成集成测试"
输出：{"has_facts": true, "items": [
  {"type": "risk", "content": "BSP SDK验证未完成，可能影响后续进度"},
  {"type": "risk", "content": "测试环境尚未搭建"},
  {"type": "milestone", "content": "集成测试预计5月底完成"}
]}

返回格式（仅返回JSON，不要其他内容）：
无内容时：{"has_facts": false}
有内容时：{"has_facts": true, "items": [{"type": "risk", "content": "..."}]}

用户消息：
"""


_REVIEW_PROMPT_TPL = """你是项目数据管家。今天是 {today}，请分析以下项目信息库的所有条目，给出清洗建议报告。

重点检查：
1. **疑似重复**：内容或标题高度相似的条目，建议其中一条归档
2. **疑似过期**：最后更新超过30天且看起来已完成或不再相关的条目
3. **高优先级但无负责人**：高优先级条目缺少 owner 字段
4. **超期未关闭**：截止日期已过但仍为 active 的条目
5. **优先级合理性**：结合内容判断优先级是否准确

输出格式要求：
- 每类问题独立一节，发现几条写几条，无问题可跳过该节
- 每条建议附上可直接执行的 /admin fact 命令（如 /admin fact archive 3）
- 命令必须单独一行，不要在命令后追加解释文字，便于系统直接执行
- 末尾给出整体数据健康评分：优 / 良 / 待改善，并说明理由（一句话）

项目信息库（共 {count} 条 active 条目）：
{facts_text}
"""


async def nightly_review(facts_text: str) -> str:
    count = facts_text.count("\n  标题:")
    today = datetime.now().strftime("%Y-%m-%d")
    prompt = _REVIEW_PROMPT_TPL.format(today=today, count=count, facts_text=facts_text)
    try:
        response = await _client.chat.completions.create(
            model=AI_MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"AI洗盘分析失败：{e}"


async def extract_facts(text: str) -> list[dict]:
    try:
        response = await _client.chat.completions.create(
            model=AI_MODEL,
            max_tokens=800,
            messages=[{"role": "user", "content": _EXTRACT_PROMPT + text}],
        )
        import json
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()
        result = json.loads(raw)
        if result.get("has_facts"):
            return result.get("items", [])
    except Exception:
        pass
    return []


_DECOMPOSE_PROMPT = """你是项目管理专家。将以下风险/问题分解为2-6条具体可执行的待办事项。

要求：
- 每条必须是一个具体行动（动词开头），不是风险描述的重复
- priority 只能是 high / medium / low
- owner 留空，除非原始信息有明确提及具体姓名
- body 写执行说明或注意事项，无特殊要求可留空

返回格式（仅 JSON，不要其他内容）：
{"todos": [{"title": "...", "body": "...", "priority": "medium", "owner": ""}]}

风险/问题：
"""


async def decompose_risk(fact: dict) -> list[dict]:
    """用 AI 将一个 risk/issue/blocker 条目分解为可执行 todo 列表。"""
    import json
    _PRIO_ZH = {"high": "高", "medium": "中", "low": "低"}
    fact_text = (
        f"#{fact['id']} [{fact['type']}] {fact['title']}\n"
        f"优先级：{_PRIO_ZH.get(fact.get('priority', ''), '—')}\n"
        f"负责人：{fact.get('owner', '') or '—'}\n"
        f"正文：{fact['body']}"
    )
    try:
        response = await _client.chat.completions.create(
            model=AI_MODEL,
            max_tokens=1000,
            messages=[{"role": "user", "content": _DECOMPOSE_PROMPT + fact_text}],
        )
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()
        result = json.loads(raw)
        return result.get("todos", [])
    except Exception:
        return []


_TODO_INTENT_PROMPT_TPL = """分析以下用户消息，判断是否有明确的"创建待办事项"意图。

只在用户明确要求建立/创建/新增待办、任务、提醒时提取，询问或讨论性质的消息不要提取。

提取字段：
- title: 待办标题（动词开头，简洁）
- due_date: 截止日期（YYYY-MM-DD格式），没有则留空字符串
- priority: high/medium/low，默认medium
- owner: 负责人姓名，没明确提到则留空

今天是 {today}，遇到"下周X"、"明天"、"本周五"等相对日期请换算为绝对日期。

返回格式（仅JSON，不要其他内容）：
无意图：{{"has_todos": false}}
有意图：{{"has_todos": true, "todos": [{{"title": "...", "due_date": "", "priority": "medium", "owner": ""}}]}}

用户消息：
"""


async def extract_todo_intent(text: str) -> list[dict]:
    """从用户消息中提取明确的待办创建意图，无意图则返回空列表。"""
    import json
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    prompt = _TODO_INTENT_PROMPT_TPL.format(today=today) + text
    try:
        response = await _client.chat.completions.create(
            model=AI_MODEL,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()
        result = json.loads(raw)
        if result.get("has_todos"):
            return result.get("todos", [])
    except Exception:
        pass
    return []
