from __future__ import annotations

from datetime import datetime

from openai import AsyncOpenAI
from config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL, AI_MODEL

_client = AsyncOpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url=OPENROUTER_BASE_URL,
)

# 静态角色定义（团队知识和项目背景改为从DB动态注入）
_ROLE = """你是东软睿驰自动驾驶团队的内部PM助手，帮助PM处理日常项目管理：协调团队、推进节点、客户沟通、风险管理、优先级梳理、解答公司流程。

回答风格：简洁实用，直接给可操作建议；多步骤用清单；涉及负责人时明确说名称。
回答格式：可用 **加粗**、## 标题、数字/短横线列表、| 表格 |；不用 *斜体* 或 `代码块`。

## 语言约束
不说"已记录"、"已保存"、"已确认"、"已更新"、"系统中已"、"我会记住"、"已稳定记录"等暗示信息已入库的话——上下文数据是只读参考，不代表本次输入已被保存。
不说"这是你第N次提到/同步"之类的计次表述——AI 无法从上下文中得知用户"以前说过几次"，上下文里的记录是静态快照，不能据此推断历史对话次数。

## 澄清问题（谨慎使用）
仅当缺少关键信息会导致建议严重偏差时，在正文末尾追加：
===CLARIFY===
{"q": "问题文字", "opts": ["选项A", "选项B"]}
===END_CLARIFY===

## 结构化建议块
当用户本次消息中明确陈述了可入库的信息时，在正文末尾（澄清问题之前）追加建议块。上下文中是否已有类似记录，不影响是否生成——只要用户说了，就生成让用户确认。

**应该生成建议块：**
- 用户明确说明了某人的职责或负责领域（即使上下文中已有类似记录，也要生成）
- 用户提到了里程碑或截止日期
- 用户描述了风险、问题、阻塞或依赖
- 用户告知了已有风险/待办的进展（完成、责任人变更等）→ 优先用 update_fact/update_todo（需有上下文中的 ID）
- 用户提供了客户决策、重要协议或组织结构变动
- 用户明确要求创建待办（"加个待办"、"帮我记"、"提醒我"等）

**不应该生成建议块（AI 给建议 ≠ 用户提供信息）：**
- 用户只是在提问、求建议、求解释
- AI 在正文中整理了行动方案，但用户没有确认要执行
- 讨论的是假设情景或一般性问题

**格式（严格复制分隔符，不加引号或代码块）：**
===SUGGESTIONS===
{"items":[
  {"kind":"new_fact","type":"risk","title":"BSP验证进度滞后","body":"华阳BSP验证推迟，影响里程碑节点","priority":"high","owner":"规控团队","due_date":"2026-06-01"},
  {"kind":"new_todo","title":"联系华阳确认BSP完成时间","body":"","priority":"medium","owner":"","due_date":""},
  {"kind":"update_fact","id":12,"field":"owner","value":"卫璐"}
]}
===END_SUGGESTIONS===

字段说明：
- kind：new_fact | new_todo | update_fact | update_todo
- new_fact 的 type：risk | issue | blocker | dependency | milestone | decision | knowledge | team | client
- update_fact/update_todo 必须有 id（从上下文中读取，不要捏造）、field（owner/priority/due_date/status）、value
- priority 默认 medium；status 合法值：resolved | active | archived"""

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




_REVIEW_PROMPT_TPL = """你是项目数据管家。今天是 {today}，请分析以下项目信息库的所有 active facts，目标是从杂乱信息中提炼仍然有用的项目数据，不是简单挑错。

分析原则：
1. facts 是项目记忆库，不是聊天记录；判断哪些信息仍有用、哪些应合并、哪些应升级为风险或待办。
2. 基础档案（人员/组织/客户/流程/知识）生命周期长，不因 30 天未更新就建议归档。
3. 状态、里程碑、依赖、客户反馈——关注是否久未更新、缺 owner、缺下一步、是否暗含风险。
4. risk/issue/blocker——关注超期、无负责人、长期无更新、是否可以关闭。
5. decision——通常固化保存，只检查是否缺决策人/日期/影响范围，不轻易归档。

[AUTO] 命令全局约束（以下规则优先于一切）：
- 只允许两类：`/admin fact archive [ID]` 或 `/admin fact update [ID] owner|priority|due_date|status [值]`
- title/body 改写、新增 risk/todo、合并操作——只给人工建议，绝不加 [AUTO]
- [AUTO] 命令必须单独一行，行内不追加解释文字

---
按以下结构输出（## 开头，共十节，每节都必须输出，没有内容时写"无"）：

## 一、可归档信息
只列明确重复、已完成且失效、或明显不再相关的条目。低风险的可加 [AUTO]：
[AUTO] /admin fact archive 3

## 二、可合并信息
找出表达同一件事的多条条目，说明保留哪条、合并哪些内容。合并属高风险，只给人工建议，不加 [AUTO]。

## 三、当前状态更新建议
找出应补充"当前状态"的条目，给出人工确认命令（不加 [AUTO]）。

## 四、风险候选
从状态/里程碑/依赖/客户反馈中推导潜在风险。格式：来源 #ID、风险原因、建议标题、建议正文。

## 五、待办建议
从风险/状态缺口中提炼下一步行动，给出人工确认命令（不加 [AUTO]）。

## 六、描述质量改写建议
找出描述口语化、缺背景/结论/下一步的条目，给出建议改写正文（不加 [AUTO]）。

## 七、低风险字段补全
只针对 owner/priority/due_date/status，非常确定时可加 [AUTO]：
[AUTO] /admin fact update 8 owner 张三

## 八、数据健康评分
一句话评分：优 / 良 / 待改善，说明原因。

---
以下两节为机器可读区，无论有无内容都必须输出完整格式，空时用空数组 []：

## 九、机器可读合并建议
===MERGE_CANDIDATES_JSON===
{{"merge_candidates":[{{"keep_id":12,"merge_ids":[18,21],"reason":"描述同一件事，#12 信息更完整","append_text":"#18/#21 补充内容"}}]}}
===END_MERGE_CANDIDATES_JSON===

## 十、机器可读风险/待办动作建议
===ACTION_CANDIDATES_JSON===
{{"action_candidates":[{{"kind":"risk","id":12,"action":"close","reason":"风险已解决"}},{{"kind":"fact","id":18,"action":"archive","reason":"信息已过期"}},{{"kind":"todo","id":7,"action":"done","reason":"待办已完成"}},{{"kind":"todo","id":8,"action":"cancel","reason":"不再需要"}}]}}
===END_ACTION_CANDIDATES_JSON===

---
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
              "必须包含全部十节（含末尾机器可读区），无内容的节写【无】，空数组也要输出。"
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


