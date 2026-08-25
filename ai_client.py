from __future__ import annotations

from datetime import datetime

from openai import AsyncOpenAI
import db as _db
from config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL, AI_MODEL


def _model() -> str:
    return _db.get_setting("ai_model", AI_MODEL)

def _chat_max_tokens() -> int:
    return int(_db.get_setting("chat_max_tokens", "8000"))

def _chat_timeout() -> float:
    return float(_db.get_setting("chat_timeout", "90"))

def _review_max_tokens() -> int:
    return int(_db.get_setting("review_max_tokens", "16000"))

def _review_timeout() -> float:
    return float(_db.get_setting("review_timeout", "180"))

_client = AsyncOpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url=OPENROUTER_BASE_URL,
)

# 静态角色定义（团队知识和项目背景改为从DB动态注入）
_ROLE = """你是东软睿驰自动驾驶团队的内部PM助手，帮助PM处理日常项目管理：协调团队、推进节点、客户沟通、风险管理、优先级梳理、解答公司流程。

回答风格：简洁实用，直接给可操作建议；多步骤用清单；涉及负责人时明确说名称。
回答格式：可用 **加粗**、## 标题、数字/短横线列表、| 表格 |、`代码块`；不用 *斜体*。

## 数据读写边界
上下文中的所有信息（风险、待办、里程碑等）才是真正事实，对话记录中出现的建议不代表用户已执行或已入库。不要用"已记录"、"已保存"等措辞。

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
- 正文中用了"你可以……"、"建议……"、"下一步……"等措辞——这是 AI 的建议，不是用户陈述的事实
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

_WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


def _today_str() -> str:
    now = datetime.now()
    return f"{now.strftime('%Y-%m-%d')}（{_WEEKDAYS[now.weekday()]}）"


# 成员版角色说明（轻量，不涉及PM专属工作流）
_ROLE_MEMBER = """你是东软睿驰自动驾驶团队的内部助手。你可以回答关于团队、项目背景、工作流程的问题，提供一般性建议。
回答风格：简洁友好，聚焦团队和项目背景知识，不涉及PM内部工作细节。"""

_CONTEXT_TEMPLATE = """{role}

{time_section}{sender_section}{dept_section}{project_section}{users_section}{todos_section}{risk_section}{schedule_section}{decision_section}{ref_section}"""


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

    time_section     = section("当前系统时间", f"今天是 {_today_str()}。所有日期推算、超期判断、待办建议 due_date 均以此基准日期为准。")
    sender_section   = section("当前对话用户", sender_info) if sender_info else ""
    dept_section     = section("部门预设（团队公认背景知识）", context.get("dept_assumptions", ""))
    project_section  = section("当前项目背景", context.get("project_assumptions", ""))
    schedule_section = section("里程碑与计划节点", context.get("schedule", ""))
    ref_section      = section("相关方与参考信息", context.get("references", ""))

    return _CONTEXT_TEMPLATE.format(
        role=base_role,
        time_section=time_section,
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
        model=_model(),
        max_tokens=_chat_max_tokens(),
        timeout=_chat_timeout(),
        messages=[{"role": "system", "content": system}] + history,
    )
    return response.choices[0].message.content




_REVIEW_PROMPT_TPL = """你是项目数据管家。今天是 {today}，请分析以下项目信息库的所有 active facts，从杂乱信息中提炼仍然有用的项目状态，帮助 PM 掌握全局。

分析原则：
1. facts 是项目记忆库；判断哪些仍有用、哪些可合并、哪些暗含风险或需要跟进待办。
2. 基础档案（人员/组织/客户/流程/知识）生命周期长，不因未更新就建议归档。
3. 状态/里程碑/依赖/客户反馈——关注久未更新、缺 owner、缺下一步行动。
4. risk/issue/blocker——关注超期、无负责人、长期无进展、是否可以关闭。
5. decision——固化保存，只在明确缺失关键字段时提示。

---
按以下结构输出（## 开头，共六节，每节都必须输出，没有内容时写"无"）：

## 一、建议归档或关闭
逐条说明：条目ID、标题、归档/关闭原因。只列明确重复、已完成且失效、或明显不再相关的条目。

## 二、建议合并
找出表达同一件事的多条条目，说明保留哪条（ID）、合入哪些（ID）、以及建议追加的补充内容。

## 三、状态与字段更新建议
找出缺 owner、缺截止、描述严重过时的条目，逐条说明建议更新的内容（自然语言，不写命令）。

## 四、潜在风险
从状态/里程碑/依赖/客户反馈中推导出尚未记录为风险的隐患，说明来源 #ID、风险原因和建议跟进方向。

## 五、建议新增待办
从风险/状态缺口中提炼下一步可执行行动，每条说明：行动内容、建议负责人、建议截止。

## 六、数据健康总结
2-3 句话：整体质量评估（优/良/待改善）、主要问题点、最需要 PM 关注的一件事。

---
以下两节为机器可读区，供系统自动处理，无论有无内容都必须输出完整格式，空时用空数组 []：

## 七、机器可读合并建议
===MERGE_CANDIDATES_JSON===
{{"merge_candidates":[{{"keep_id":12,"merge_ids":[18,21],"reason":"描述同一件事，#12 信息更完整","append_text":"#18/#21 补充内容"}}]}}
===END_MERGE_CANDIDATES_JSON===

## 八、机器可读动作建议
===ACTION_CANDIDATES_JSON===
{{"action_candidates":[{{"kind":"risk","id":12,"action":"close","reason":"风险已解决"}},{{"kind":"fact","id":18,"action":"archive","reason":"信息已过期"}},{{"kind":"todo","id":7,"action":"done","reason":"待办已完成"}},{{"kind":"todo","id":8,"action":"cancel","reason":"不再需要"}},{{"kind":"new_todo","action":"add","title":"联系华阳确认BSP完成时间","body":"","priority":"medium","owner":"规控团队","due_date":"2026-06-01","reason":"风险#12缺乏跟进行动"}}]}}
===END_ACTION_CANDIDATES_JSON===

---
项目信息库（共 {count} 条 active 条目）：
{facts_text}
"""


_PROJECT_STATUS_PROMPT_TPL = """你是项目管理助手。今天是 {today}，请基于以下项目数据，用 3-5 段自然语言汇报「{project}」项目的当前整体状态。

要求：
- 总长不超过 300 字，语言简洁直接，适合早报阅读
- 涵盖：进展与里程碑状态、主要风险与阻塞、需要关注的核心问题
- 客观陈述，有具体细节（引用条目名或人员名），不作空洞鼓励
- 仅输出状态汇报正文，不输出待办建议，不输出 JSON

---
项目数据（共 {count} 条活跃条目）：
{facts_text}
"""


async def generate_project_status(project: str) -> str:
    import db as _db
    facts_text = _db.get_all_facts_for_review(project)
    if not facts_text:
        return f"「{project}」项目暂无活跃数据。"
    count = facts_text.count("\n  标题:")
    today = _today_str()
    prompt = _PROJECT_STATUS_PROMPT_TPL.format(
        today=today, project=project, count=count, facts_text=facts_text
    )
    response = await _client.chat.completions.create(
        model=_model(),
        max_tokens=_chat_max_tokens(),
        timeout=_chat_timeout(),
        messages=[{"role": "user", "content": prompt}],
    )
    return (response.choices[0].message.content or "").strip()


async def nightly_review(facts_text: str) -> str:
    count = facts_text.count("\n  标题:")
    today = _today_str()
    prompt = _REVIEW_PROMPT_TPL.format(today=today, count=count, facts_text=facts_text)
    try:
        response = await _client.chat.completions.create(
            model=_model(),
            max_tokens=_review_max_tokens(),
            timeout=_review_timeout(),
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
            model=_model(),
            max_tokens=_review_max_tokens(),
            timeout=_review_timeout(),
            messages=[{"role": "user", "content": retry_prompt}],
        )
        content = response.choices[0].message.content or ""
        if content.strip():
            return content
        return "AI洗盘分析失败：模型返回空内容，请重试。"
    except Exception as e:
        return f"AI洗盘分析失败：{e}"




_DECOMPOSE_PROMPT_TPL = """你是项目管理专家。今天是 {today}，将以下风险/问题分解为2-6条具体可执行的待办事项。

要求：
- 每条必须是一个具体行动（动词开头），不是风险描述的重复
- priority 只能是 high / medium / low
- owner 留空，除非原始信息有明确提及具体姓名
- body 写执行说明或注意事项，无特殊要求可留空

返回格式（仅 JSON，不要其他内容）：
{{"todos": [{{"title": "...", "body": "...", "priority": "medium", "owner": ""}}]}}

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
    today = _today_str()
    prompt = _DECOMPOSE_PROMPT_TPL.format(today=today) + fact_text
    try:
        response = await _client.chat.completions.create(
            model=_model(),
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()
        result = json.loads(raw)
        return result.get("todos", [])
    except Exception:
        return []


