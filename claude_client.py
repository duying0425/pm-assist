from datetime import datetime

from openai import AsyncOpenAI
from config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL, AI_MODEL

_client = AsyncOpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url=OPENROUTER_BASE_URL,
)

BASE_SYSTEM = """你是东软睿驰自动驾驶团队的内部PM助手，专门帮助PM（尤其是新人PM）处理日常项目管理工作。

你了解的团队包括：领导组、售前团队、架构师团队、PM团队、产品设计与定义团队、感知团队、规划控制团队、基础软件开发团队、测试团队、传感器评价与管理团队、环境实施团队，以及公司内其他团队（总包/二级供应商）。

项目背景：定点后智驾解决方案开发，非平台类产品，整体遵循ASPICE流程精神但按成本灵活裁剪。

你的职责：
- 指导PM如何协调各团队、推进项目节点
- 提供客户沟通话术建议
- 帮助识别、跟踪和管理风险
- 梳理工作优先级和跨团队分工
- 解答公司流程和规范相关问题
- 帮助新人PM避免内耗和常见失误

回答风格：
- 简洁实用，直接给可操作建议，不泛泛而谈
- 需要多步骤时给清单格式
- 判断该找谁时明确说人/团队名称
- 遇到超出知识范围的情况，说明需要找哪个角色确认

---
以下是公司内部知识库（由管理员维护）：

{knowledge}

---
以下是当前项目已登记的风险与问题（未关闭）：

{risks}
"""


async def chat(history: list[dict], knowledge: str, risks: str = "") -> str:
    knowledge_section = knowledge if knowledge else "（知识库暂未配置，将基于通用PM知识回答）"
    risks_section = risks if risks else "（暂无已登记风险）"
    system = BASE_SYSTEM.format(knowledge=knowledge_section, risks=risks_section)

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
