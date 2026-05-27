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
回答格式：可以用**加粗**、## ### 标题、数字序号或短横线列表、| 表格 |（飞书卡片均支持渲染）；不要用*斜体*、`代码块`等格式。

重要约束：
- 当用户提供事实类信息时，不要说"已记录"、"已保存"、"已确认"、"我会记住"等暗示已持久化的话；也不要自行添加"提示：以下内容是建议"之类的文字——系统会自动弹出确认卡片。

澄清问题（谨慎使用）：
- 仅当缺少某个关键信息会导致建议严重偏差时，才在回答末尾附加澄清问题
- 一般性闲聊、已有足够信息的问题，不要附加澄清问题
- 格式：正文结束后换行，输出 ===CLARIFY=== 然后是JSON，再输出 ===END_CLARIFY===
- JSON格式：{"q": "问题文字", "opts": ["选项A", "选项B"]}（opts可省略）

---
## 【输出规则：结构化建议块】

下列情况下，你必须在正文最后追加建议块（在澄清问题之前）：
- 用户说明了某人/某团队的负责人、角色或职责
- 用户提到了里程碑、截止日期或计划节点
- 用户描述了一个风险、问题、阻塞或依赖
- 用户说明了某条已有风险/待办的进展（已完成、已解决、新增负责人等）
- 用户提供了决策、客户信息或团队信息

建议块格式（严格复制分隔符，不要加引号或代码块）：
===SUGGESTIONS===
{"items":[{"kind":"new_fact","type":"team","title":"规控团队负责人","body":"规控团队负责人为卫璐","priority":"","owner":"卫璐","due_date":""}]}
===END_SUGGESTIONS===

字段说明（仅供参考，按实际内容填写）：
- kind：new_fact | new_todo | update_fact | update_todo
- new_fact 的 type：risk | issue | blocker | dependency | milestone | decision | knowledge | team | client
- update_fact/update_todo 的 field：owner | priority | due_date | status
- status 合法值：resolved | active | archived；priority 合法值：high | medium | low
- 若知识库中已有同类条目且内容完全一致则不重复；若有 ID 可更新则用 update_fact；不要凭空捏造 ID"""

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

{sender_section}{dept_section}{project_section}{users_section}{todos_section}{risk_section}{schedule_section}{decision_section}{ref_section}"""


def _build_system(context: dict, sender_info: str = "", role: str = "pm") -> str:
    def section(title: str, content: str) -> str:
        if not content:
            return ""
        return f"\n---\n## {title}\n{content}\n"

    users_section = ""
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
        users_section    = section("系统注册用户（可回答谁在哪个项目）", context.get("users", ""))
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
        users_section=users_section,
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
        max_tokens=8000,
        messages=[{"role": "system", "content": system}] + history,
    )
    return response.choices[0].message.content




_REVIEW_PROMPT_TPL = """你是项目数据管家。今天是 {today}，请分析以下项目信息库的所有 active facts，目标不是简单挑错，而是从杂乱信息中提炼仍然有用的项目数据。

核心原则：
1. facts 不是原始聊天记录，而是项目记忆库；你要判断哪些信息仍有用、哪些应合并、哪些应升级为风险或待办。
2. 不同信息生命周期不同：人员/组织/客户/流程/知识等基础档案长期稳定，不要因为 30 天未更新就建议归档。
3. 项目状态、里程碑、依赖、客户反馈需要关注是否久未更新、缺下一步、缺 owner、是否暗含风险。
4. risk/issue/blocker 需要关注超期、无负责人、长期无更新、是否已有行动项、是否可以关闭。
5. decision 通常应固化保存，只检查是否缺少决策人、日期、影响范围，不轻易归档。

请严格按以下结构输出（每节用 ## 开头作为标题）：

## 一、可归档信息
- 只列明确重复、已完成且失效、或明显不再相关的信息。
- 低风险可自动归档的命令必须单独一行，并加 [AUTO] 前缀：
[AUTO] /admin fact archive 3

## 二、可合并信息
- 找出表达同一件事的多条信息，说明建议保留哪条、把哪些内容并入哪条。
- 合并属于高风险动作，只给建议，不要输出 [AUTO] 命令。
- 如果存在合并建议，必须同时在报告末尾的机器可读区写入 merge_candidates。

## 三、当前状态更新建议
- 找出应被更新为"当前状态"的条目，说明建议补充什么。
- 可以给人工确认命令，但不要加 [AUTO]，例如：
/admin fact update 12 body 新的状态描述

## 四、风险候选
- 从状态、里程碑、依赖、客户反馈中推导潜在风险。
- 格式：来源 #ID、风险原因、建议风险标题、建议正文。
- 只给人工确认命令，不要加 [AUTO]，例如：
/admin fact add risk 标题 | 正文

## 五、待办建议
- 从风险候选或状态缺口中提炼下一步行动。
- 只给人工确认命令，不要加 [AUTO]，例如：
/todo 联系华阳确认 BSP 验证完成时间 risk 12
- 如果已有 todo 可完成或取消，必须同时在报告末尾的机器可读区写入 action_candidates。

## 六、描述质量改写建议
- 找出描述太口语、缺背景、缺结论、缺下一步的信息。
- 给出"建议改写正文"，但不要加 [AUTO]，不要自动覆盖。

## 七、低风险字段补全
- 只针对 owner、priority、due_date、status 这类结构化字段。
- 如果非常确定，可以给 [AUTO] 命令，命令必须单独一行：
[AUTO] /admin fact update 8 owner 张三
[AUTO] /admin fact update 9 priority high
[AUTO] /admin fact update 10 due_date 2026-06-01
[AUTO] /admin fact update 11 status resolved

## 八、数据健康评分
- 给出整体评分：优 / 良 / 待改善，并用一句话说明原因。

## 九、机器可读合并建议
- 必须放在报告最后，格式严格如下；没有合并建议时 merge_candidates 为空数组：
===MERGE_CANDIDATES_JSON===
{{"merge_candidates":[{{"keep_id":12,"merge_ids":[18,21],"reason":"描述同一件事，#12 信息更完整","append_text":"#18/#21 补充的信息：华阳尚未给出明确完成时间，需持续跟进。"}}]}}
===END_MERGE_CANDIDATES_JSON===

## 十、机器可读风险/待办动作建议
- 必须紧跟合并建议之后；没有建议时 action_candidates 为空数组：
===ACTION_CANDIDATES_JSON===
{{"action_candidates":[{{"kind":"risk","id":12,"action":"close","reason":"风险已解决或已被新状态覆盖"}},{{"kind":"fact","id":18,"action":"archive","reason":"信息已过期"}},{{"kind":"todo","id":7,"action":"done","reason":"待办已完成"}},{{"kind":"todo","id":8,"action":"cancel","reason":"待办已不再需要"}}]}}
===END_ACTION_CANDIDATES_JSON===

命令约束：
- 只有低风险归档和字段补全可以使用 [AUTO] 前缀。
- [AUTO] 命令只允许 `/admin fact archive [ID]` 或 `/admin fact update [ID] owner|priority|due_date|status [值]`。
- title/body 改写、新增 risk、新增 todo、合并信息都必须人工确认，绝不能加 [AUTO]。
- 命令必须单独一行，不要在命令后追加解释文字，便于系统识别。

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
            max_tokens=16000,
            timeout=180,
            messages=[{"role": "user", "content": prompt}],
        )
        content = response.choices[0].message.content or ""
        if content.strip():
            return content

        retry_prompt = (
            prompt
            + "\n\n重要：上一次模型返回了空正文。请不要展开推理，直接输出最终报告；"
              "必须包含一到八节，如无合并建议也必须输出空的机器可读区。"
        )
        response = await _client.chat.completions.create(
            model=AI_MODEL,
            max_tokens=16000,
            timeout=180,
            messages=[{"role": "user", "content": retry_prompt}],
        )
        content = response.choices[0].message.content or ""
        if content.strip():
            return content
        return "AI洗盘分析失败：模型返回空内容，请重试。"
    except Exception as e:
        return f"AI洗盘分析失败：{e}"




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
            max_tokens=2000,
            messages=[{"role": "user", "content": _DECOMPOSE_PROMPT + fact_text}],
        )
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()
        result = json.loads(raw)
        return result.get("todos", [])
    except Exception:
        return []


