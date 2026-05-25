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
        max_tokens=1500,
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

返回格式（仅返回JSON，不要其他内容）：
无内容时：{"has_facts": false}
有内容时：{"has_facts": true, "items": [{"type": "risk", "content": "简洁一句话描述"}]}

用户消息：
"""


async def extract_facts(text: str) -> list[dict]:
    try:
        response = await _client.chat.completions.create(
            model=AI_MODEL,
            max_tokens=400,
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
