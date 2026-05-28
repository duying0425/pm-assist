# pm-assist 项目上下文

## 项目定位
东软睿驰自动驾驶团队内部飞书PM助手Bot。帮助新人PM处理项目协调、客户沟通、风险管理等日常工作。通过飞书@Bot方式交互，目标"越用越好"。

## 用户信息
- 公司：东软睿驰（Neusoft RuiChi），自动驾驶团队
- 角色：PM，项目负责人，内部工具开发决策者
- 技术背景：有服务器运维能力，了解基本开发概念，非专业开发者
- 管理员：杜莹芳（`ou_d1ccad1071d7daf767337953ffeb317a`）、佟海鹏（open_id 待补充）
- 多管理员配置：在 `.env` 的 `ADMIN_OPEN_IDS` 中逗号分隔，如 `ou_xxx,ou_yyy`
- 协作风格：直接给可运行代码；说"你决定"时直接选最优方案执行，无需再问

## 部署信息
- 服务器：SSH 配置名 `aliyun`（本地 `~/.ssh/config` 中预设），用户 `duyingfang`，Ubuntu 24.04
- 项目目录：`~/pm-assist`（虚拟环境在 `~/pm-assist/venv`）
- 启动命令：`nohup venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000 >> logs/app.log 2>&1 &`
- 日志：`~/pm-assist/logs/app.log`
- 公网地址：`https://pm.tmhcorps.cn`（Cloudflare 代理 HTTP→HTTPS）
- nginx 配置：`/etc/nginx/conf.d/apps.conf`（与 chat.tmhcorps.cn 共用）
- 飞书 Webhook：`https://pm.tmhcorps.cn/webhook/feishu`
- .env 位置：`~/pm-assist/.env`（含 FEISHU_APP_ID/SECRET/TOKEN、OPENROUTER_API_KEY、ADMIN_OPEN_IDS、NOTIFY_OPEN_IDS）

## AI 调用规范
**必须用 OpenRouter 兼容接口，不得使用 anthropic 包。**
```python
from openai import AsyncOpenAI
client = AsyncOpenAI(base_url=config.OPENROUTER_BASE_URL, api_key=config.OPENROUTER_API_KEY)
```
用户有自建代理 `router.tmhcorps.cn`，config.py 中 `OPENROUTER_BASE_URL` 已配置。

## 服务管理规范
用 nohup 运行，不用 systemd（账号无 NOPASSWD sudo）。
- 重启：先 `ps aux | grep uvicorn` 找 PID，`kill PID`，再执行启动命令
- 重启后需手动启动（无开机自动恢复）
- 早报推送由 APScheduler 内置于 FastAPI 处理，**不要同时开 crontab 跑 notify.py**，否则主管理员会收到两份

## 版本管理
- 版本号存于 `VERSION` 文件（当前 `1.0.0`），语义化：`major.feature.patch`
- 飞书发 `/version` 可查询当前运行版本
- 每次部署前修改 `VERSION`，本地 `git tag vX.Y.Z && git push --tags`，scp 时一并上传

## 开发工作流程
1. 本地修改代码 + 更新 CLAUDE.md（版本号、功能描述、已知坑）
2. `git commit`（本地提交）
3. `scp` 推送到服务器，重启服务，飞书测试验证
4. 测试通过后 `git push` 推送到 GitHub

## 代码结构
```
pm-assist/
├── main.py          # FastAPI 主入口，Webhook 处理，管理员命令，卡片回调，/todo 命令
├── ai_client.py # AI 对话(chat) + 洗盘(nightly_review) + 分解(decompose_risk)
├── feishu.py        # 飞书 API：send_reply/send_reply_to_user（统一出口）、所有卡片 builder、卡片响应格式
├── db.py            # SQLite CRUD：四层数据（assumptions/org_units/facts/todos）+ 用户/项目
├── web_admin.py     # Web 管理后台 REST API（FastAPI Router，挂载于 /admin）
├── config.py        # 环境变量加载（从 .env 读取）
├── notify.py        # 消息推送：build_risk_section() + build_morning_report(review)
├── VERSION          # 版本号文件，格式 x.y.z
├── migrate_v2.py    # 一次性迁移脚本（已执行，勿重复执行）
├── seed.py          # 初始知识库数据（已执行，勿重复执行）
├── seed_yadi.py     # 雅迪项目初始数据（已执行，勿重复执行）
├── static/
│   └── admin.html   # Web 管理后台单页 UI（纯 HTML/CSS/JS，无外部依赖）
├── deploy/
│   ├── nginx.conf        # nginx 站点配置模板
│   ├── pm-assist.service # systemd 服务文件（备用，当前未启用）
│   ├── setup.sh          # 一键部署脚本
│   └── start.sh          # 启动脚本
└── logs/app.log     # 运行日志（服务器上）
```

## 数据库结构

### Layer 0：预设假设 `assumptions` 表

部门公认的背景知识，每次 AI 对话**自动注入**，无需用户提及。

```
id          INTEGER PRIMARY KEY
scope       TEXT    -- global|dept|project|client
scope_ref   TEXT    -- 当 scope=project/client 时填项目名或客户名
title       TEXT
body        TEXT
confidence  TEXT    -- universal（铁律）| common（通常）| assumed（推测）
source      TEXT    -- seed|manual
active      INTEGER -- 1=启用, 0=归档
created_at / updated_at
```

### Layer 1：组织结构 `org_units` 表

```
id          INTEGER PRIMARY KEY
type        TEXT    -- company|dept|team|role|client_org|person
name        TEXT
parent_id   INTEGER -- 父节点 ID，NULL 表示根节点
feishu_id   TEXT    -- 飞书 open_id 或 group_id（可选）
attributes  TEXT    -- JSON，存储额外属性（如 lead、domain）
active      INTEGER -- 1=启用, 0=停用
created_at  TEXT
```

`type=person` 由系统自动写入：飞书消息中 @某人 时，open_id ↔ 姓名自动缓存至此表。

### Layer 2：项目事项 `facts` 表

```
id          INTEGER PRIMARY KEY
type        TEXT    -- 子类型（见下）
dimension   TEXT    -- 一级维度（自动从 type 计算）
title       TEXT
body        TEXT    -- AI追加更新时末尾追加"[日期 更新] 内容"保留历史
status      TEXT    -- active（默认）| resolved | archived
priority    TEXT    -- high|medium|low
owner       TEXT
due_date    TEXT
project     TEXT    -- 默认 yadi
source      TEXT    -- seed|manual|ai
created_at / updated_at
```

**type → dimension 映射**：
| type | dimension |
|------|-----------|
| risk / issue / blocker / dependency | risk |
| milestone | schedule |
| decision / process | decision |
| team | resource |
| client / org | stakeholder |
| knowledge | scope |
| report | system（内部，不对外展示） |

**AI 上下文时间信息**：`fmt_risks` / `fmt_schedule` / `fmt_generic` 均输出 `记录:YYYY-MM-DD`，有更新时追加 `更新:YYYY-MM-DD`，AI 可据此判断信息时效性。

### Layer 3：待办事项 `todos` 表

```
id              INTEGER PRIMARY KEY
title           TEXT    NOT NULL
body            TEXT    -- 执行说明或备注
status          TEXT    -- open | done | cancelled
priority        TEXT    -- high | medium | low
owner           TEXT
due_date        TEXT
project         TEXT    -- 默认 yadi
source_fact_id  INTEGER -- 来自哪个 risk/issue/blocker（FK facts.id，可为空）
plan_id         INTEGER -- 挂到哪个里程碑（FK facts.id type=milestone，可为空）
source          TEXT    -- manual | ai
created_at / updated_at
```

**关联关系**：`source_fact_id` → 追溯源头风险；`plan_id` → 挂载到一级里程碑；两者均为空 → 独立待办。

**AI 上下文**：注入未完成 todo（最多30条）+ 近14天已完成（最多10条），均带时间和追溯信息。

### 用户与项目表

**users**
```
open_id / name / role(super_admin|pm|member) / project / status(pending|active|rejected|inactive)
```

**projects**
```
name / description / created_by / active / updated_at
```
默认种入「雅迪」项目；`init_db()` 自动处理 updated_at 列升级（幂等）。

### 其他表

**conversations**：`id / chat_id / role(user|assistant) / content / created_at`，按 chat_id 隔离，`/clear` 清空。

**pending_notes**（TTL 30 分钟）：`chat_id(PRIMARY KEY) / items_json / created_at`。同一张表用 key 前缀存四种 pending：AI建议/todo确认/洗盘合并/洗盘清洗。

**processed_events**：`event_id(PRIMARY KEY) / created_at`，飞书事件去重。

**system_settings**：`key / value / updated_at`，当前使用项：`nightly_review_mode`（`report_only` 默认 | `direct_cleanup`）。

`init_db()` 自动创建全部表和索引（幂等），含 facts/todos/conversations/processed_events 四个查询索引。

## 关键函数（db.py）

**四层上下文**：
- `get_full_context(project)` → 返回结构化 dict，供 AI 注入（dept_assumptions / project_assumptions / risks / schedule / decisions / references / todos）

**facts CRUD**：
- `add_fact(type_, title, body, ...)` → 新增（自动计算 dimension）
- `update_fact(id, **kwargs)` → 更新任意字段
- `append_to_fact(id, addition)` → body 末尾追加带时间戳更新
- `find_similar_fact(type_, content)` → 关键词重叠去重
- `list_facts(type_, dimension, status, project)` → 支持按 type 或 dimension 过滤

**todos CRUD**：
- `add_todo(title, body, priority, owner, due_date, project, source_fact_id, plan_id, source)` → 新增
- `get_todo(id)` / `update_todo(id, **kwargs)` / `list_todos(status, project, ...)` → 标准 CRUD
- `get_todos_for_context(project, open_limit, done_limit, done_days)` → AI 上下文格式化文本

**assumptions CRUD**：`add_assumption / update_assumption / list_assumptions`

**org_units CRUD**：`add_org_unit / list_org_units / upsert_person(open_id, name)`（@mention 自动缓存）

**洗盘相关**：
- `save_nightly_review(content)` / `get_latest_nightly_review()` → 存取 AI 洗盘报告
- `get_all_facts_for_review()` → 所有 active 非 report facts，供 AI 分析
- `get_setting(key, default)` / `set_setting(key, value)` → 系统配置读写

**洗盘边界**：
- 洗盘对象是 `facts` 表，不包含 `todos`、`assumptions`、`org_units`
- 报告结构：六节纯自然语言（建议归档/合并/状态更新/潜在风险/建议新增待办/数据健康总结）+ 两节机器可读 JSON（`===MERGE_CANDIDATES_JSON===` / `===ACTION_CANDIDATES_JSON===`）
- `report_only` 模式：发送报告 + 弹出确认卡片，人工操作，不自动执行任何数据变更
- `direct` 模式：从 `action_candidates` JSON 自动执行动作（close/archive/done/cancel），不发确认卡片
- 合法动作：`(risk,close)` / `(fact,archive)` / `(todo,done)` / `(todo,cancel)`；不执行 delete、新增、正文改写

## AI 上下文注入顺序（ai_client.py）

```
[静态] 角色定义（PM助手职责 + 语言约束：不说"已记录/已保存"）
[L0]   部门预设假设（铁律/通识，scope=dept/global）
[L1]   项目专属假设（scope=project）
[T]    待办事项（open todos + 近14天已完成，带追溯和时间）
[L2]   活跃风险与问题（带记录/更新时间）
[L3]   里程碑与计划（带目标日期和时间）
[L4]   决策记录（带更新时间）
[L5]   相关方与参考（带更新时间）
```

**角色差异**：`member` 仅注入角色定义+部门假设+里程碑/相关方（无 todos/risks/decisions）；`pm` 完整上下文；`super_admin` 完整上下文 + 数据库结构说明 + 注册用户列表。

**AI 分解函数**：`decompose_risk(fact)` → 调用 AI 将 risk 条目拆解为 2-6 条可执行 todo（JSON 格式）

**AI 建议格式**：AI 主回复末尾内嵌 `===SUGGESTIONS=== [...JSON...] ===END_SUGGESTIONS===` 块，主流程解析后弹出统一建议确认卡片。

## 角色与权限系统

### 角色概览
| 角色 | 来源 | 权限概述 |
|------|------|---------|
| `super_admin` | .env `ADMIN_OPEN_IDS` 自动注册，或管理员手动提升 | 全部功能 + /admin 系列 + AI 数据查询 |
| `pm` | 用户申请，管理员审批 | 完整 PM 工作模式（风险/待办/知识库/AI 提取卡片） |
| `member` | 用户申请，管理员审批 | 轻量 AI 对话（可查里程碑和组织信息；不注入 todos/risks，不触发提取卡片） |
| `pending` / `unknown` | 已申请未审批 / 从未发消息 | 仅 /start /join /help /version |

### 权限分级详表
| 功能 | unknown/pending | member | pm | super_admin |
|------|:-:|:-:|:-:|:-:|
| /start /join /help /version | ✓ | ✓ | ✓ | ✓ |
| @Bot AI 对话 | ✗ | ✓（轻量） | ✓（完整） | ✓（管理员） |
| /clear /leave | ✗ | ✓ | ✓ | ✓ |
| /risk /todo /note | ✗ | ✗ | ✓ | ✓ |
| AI 信息提取确认卡片 | ✗ | ✗ | ✓ | ✓ |
| /schedule 里程碑查看 | ✗ | ✓ | ✓ | ✓ |
| /review run（洗盘执行） | ✗ | ✗ | ✓ | ✓ |
| /admin 系列 | ✗ | ✗ | ✗ | ✓ |

### 注册流程
1. 用户发 `/start` 查看项目列表
2. 发 `/join [项目名] [pm|member]` 提交申请
3. 所有管理员（`ADMIN_OPEN_IDS`）收到飞书审批卡片
4. 点击「批准」→ 用户状态改为 active + 通知用户；点击「拒绝」→ 通知用户被拒

### AI 说话人注入
每次对话在 system prompt 注入当前用户身份，例：
- `管理员-杜莹芳（最高权限，可询问系统数据和数据库信息）`
- `项目经理PM-佟海鹏（雅迪项目）`
- `项目成员-李浩（雅迪项目）`

## 命令速查

### 所有人（含未注册）
```
/start                     查看可用项目并申请加入（新用户入口）
/join [项目名] [pm|member] 提交加入申请
/version                   查看当前版本号
/help                      显示使用说明
```

### 已注册用户（member / pm / super_admin，status=active）
```
@Bot [消息]    AI 对话（深度按角色不同）
/clear         清除当前会话历史
/leave         退出当前项目绑定（角色降为 member，账号保留）

# 里程碑查看（member/pm/super_admin 均可）
/schedule list           查看进行中的里程碑
/schedule list all       查看全部里程碑
/schedule show [ID]      查看里程碑详情（含关联待办）
```

### PM / 管理员可用
```
/note [内容]   快速记录笔记到知识库

# AI 洗盘
/review run                      按当前模式立即洗盘，发送给所有管理员和 PM
/review run report               临时按仅报告模式执行一次（弹卡片）
/review run direct               临时按直接执行模式执行一次（自动清洗）

# 风险管理
/risk list [open|all]
/risk show [ID]                                 完整正文 + 关联待办
/risk close [ID]  /risk reopen [ID]  /risk owner [ID] [姓名]
/risk add [type] [priority] [标题] | [描述]
  type: risk|issue|blocker|dependency  priority: high|medium|low

# 待办事项
/todo list [all | risk ID | plan ID]
/todo show [ID]
/todo [内容]                     新建独立待办
/todo [内容] risk [ID]           从 risk 分解新建待办
/todo [内容] plan [ID]           挂到里程碑新建待办
/todo update [ID] [字段] [值]    字段：title|body|priority|owner|due_date
/todo done [ID]  /todo cancel [ID]
```

### 管理员专用
```
/admin stats

# 统一信息管理
/admin fact list [type|all]
/admin fact show [ID]
/admin fact update [ID] [field] [值]   field: status|owner|priority|due_date|title|body
/admin fact archive [ID]  /admin fact delete [ID]
/admin fact add [type] [标题] | [正文]
/admin fact decompose [ID]             AI 分解 risk 为待办列表（卡片确认后入库）

# 用户管理
/admin user list  /admin user show [姓名/open_id]
/admin user role [open_id] [pm|member|super_admin]
/admin user project [open_id] [项目名|-]
/admin user approve [open_id]  /admin user reject [open_id]  /admin user remove [open_id]

# 项目管理
/admin project list  /admin project add [名称] | [描述]
/admin project close [ID]  /admin project open [ID]
/admin project bind [项目名]  /admin project unbind  /admin project bindings

# AI 洗盘模式配置（管理员专用）
/admin review status
/admin review mode report    设置为仅报告（默认）
/admin review mode direct    设置为直接执行

# 预设假设管理
/admin assumption list [dept|project|client]
/admin assumption show [ID]
/admin assumption add [scope] [confidence] [标题] | [正文]
  scope: dept|project/项目名|client|global  confidence: universal|common|assumed
/admin assumption update [ID] [field] [值]
/admin assumption archive [ID]  /admin assumption delete [ID]

# 组织结构管理
/admin org list [type?]
/admin org add [type] [名称] [父节点ID?]
  type: company|dept|team|role|client_org
```

**命令解析约定**：`fact add`、`fact update`、`assumption add`、`risk add` 等长文本参数由各子命令 handler 自行拼接，不受顶层 `split(None, 4)` 截断影响；带 `|` 时左侧为标题/名称，右侧为正文/描述。

## 权限与推送管理
```
ADMIN_OPEN_IDS=ou_d1ccad1071d7daf767337953ffeb317a,ou_佟海鹏的open_id
NOTIFY_OPEN_IDS=ou_其他需要收日报的人（非管理员也可收）
```
- `ADMIN_OPEN_IDS`：有权使用 `/admin` 命令；审批卡片发给所有管理员
- `NOTIFY_OPEN_IDS`：只收日报，无管理权限
- APScheduler 早报发送给 `ADMIN_OPEN_IDS | NOTIFY_OPEN_IDS` 全量
- 手动 `/review run` 发送给 `.env ADMIN_OPEN_IDS` + 数据库中 active 的 `super_admin` 和 `pm`
- 获取 open_id：让对方发一条消息，从 `logs/app.log` 找 `sender=ou_xxx`

## 飞书应用配置要点
- 事件订阅：`im.message.receive_v1` + `card.action.trigger` + `application.bot.menu_v6`（三个都必须订阅）
- 快捷菜单：在飞书开放平台 → 应用能力 → 机器人 → 快捷菜单中配置，event_key 须与下表完全一致
- 回调地址：`https://pm.tmhcorps.cn/webhook/feishu`
- 卡片回调响应格式：`{"toast":..., "card":{"type":"raw","data":{...}}}`（缺少此格式会报错200672）

### 快捷菜单事件键
| event_key | 建议菜单名 | 最低权限 |
|-----------|-----------|---------|
| `show_help` | 使用帮助 | 全员 |
| `show_version` | 查看版本 | 全员 |
| `clear_chat` | 清除对话 | active 用户 |
| `view_schedule` | 查看里程碑 | member+ |
| `view_todos` | 查看待办 | pm+ |
| `view_risks` | 查看风险 | pm+ |
| `run_review` | AI 洗盘 | pm+ |
| `view_morning_report` | 查看早报 | pm+ |
| `admin_users` | 人员信息 | member+ |

> 快捷菜单事件无 chat_id，`view_*` 使用发起人 open_id 作为 chat_id，项目按用户绑定解析（super_admin 无绑定时查全量）。

## 飞书卡片模板清单

所有 builder 在 `feishu.py`，callback 分发在 `main.py` 的 `_handle_card_trigger` / `_handle_card_callback`（两类路由处理相同 action，飞书新旧协议均兼容）。

**通用规范**：
- 卡片发送：`send_reply(chat_id, card_dict)` / `send_reply_to_user(open_id, card_dict)` 统一出口
- 卡片原地更新：`update_message_card(message_id, card)` → `PATCH /im/v1/messages/{id}`（须含 `update_multi:true`，勿用 `/body` 子路径）
- pending 存储：AI建议→`pending_commands`；洗盘合并→`pending_merges`；洗盘清洗→`pending_actions`；TTL=30分钟

**卡片一览**：

| # | 用途 | Builder | 触发方式 |
|---|------|---------|---------|
| 1 | 通用 Markdown 文本 / 占位 | `build_md_card(text, title, color)` `build_thinking_card()` | AI回复/命令响应/思考中占位 |
| 2 | AI 建议确认（列表） | `build_ai_suggestions_card(items, chat_id)` | 解析到 `===SUGGESTIONS===` 块 / decompose |
| 3 | AI 建议详情 | `build_suggestion_detail_card(item, chat_id, index)` | 卡片2点击"详情" |
| 4 | 注册审批 | `build_approval_card(open_id, name, role, project)` | 用户 /join 申请 |
| 5 | 洗盘合并建议 | `build_merge_confirm_card(merges, chat_id)` | 解析到 `===MERGE_CANDIDATES_JSON===` |
| 6 | 洗盘动作建议 | `build_action_confirm_card(actions, chat_id)` | 解析到 `===ACTION_CANDIDATES_JSON===` |
| 7 | Fact 详情（通用只读） | `build_fact_show_card(fact)` | `view_fact_detail` action |
| 8 | 风险列表 | `build_risk_list_card(rows)` | `/risk list` |
| 9 | 风险详情 | `build_risk_show_card(fact, open_todos)` | 卡片8点击"详情" / `/risk show` |
| 10 | 待办列表 | `build_todo_list_card(rows)` | `/todo list` |
| 11 | 待办详情 | `build_todo_show_card(todo, source_fact, plan_fact)` | 卡片10点击"详情" / `/todo show` |
| 12 | 里程碑列表 | `build_milestone_list_card(rows)` | `/schedule list` |
| 13 | 里程碑详情 | `build_milestone_show_card(fact, open_todos)` | `/schedule show` |
| 14 | AI 澄清问题 | `build_clarify_card(question, opts, chat_id, sender_open_id)` | 解析到 `===CLARIFY===` 块 |
| 15 | 早报 | `build_morning_report_card(project_name, risks, review_text, today)` | 定时09:00 / `/review run` |

**卡片交互 action 汇总**：

| action 值 | 执行逻辑 |
|-----------|---------|
| `suggestion_view_detail` | 原地切换为卡片3 |
| `suggestion_back_to_list` | 原地切换回卡片2 |
| `suggestion_save_one` / `suggestion_skip_one` | `_save_suggestion_item` / 标记 skipped，刷新卡片2 |
| `suggestion_save_all` / `suggestion_skip_all` | 批量操作，刷新卡片2 |
| `approve_user` / `reject_user` | `update_user()` + DM 通知申请人 |
| `merge_one` / `merge_all` | `_apply_merge_item`：`append_to_fact` + archive 被合入条目 |
| `skip_merges` | `clear_pending_merges` |
| `review_action_one` / `review_action_all` | `_apply_review_action`：risk.close / fact.archive / todo.done/cancel |
| `skip_review_actions` | `clear_pending_actions` |
| `view_risk_detail` | 原地更新为卡片9 |
| `view_todo_detail` | 原地更新为卡片11 |
| `view_fact_detail` | 原地更新为卡片7 |
| `clarify_option` | 原地占位 + 异步 `_clarify_and_respond` |

**卡片2分组**（kind/type 决定所在分组，无内容的组不显示）：
- ⚠️ 风险/问题：`kind=new_fact`，type∈{risk,issue,blocker,dependency}
- 📅 里程碑：`kind=new_fact`，type=milestone
- 📋 知识/决策/信息：`kind=new_fact`，其余 type
- ☐ 待办事项：`kind=new_todo`
- ✏️ 更新建议：`kind∈{update_fact,update_todo}`

## 已知坑
- SSH 登录用 `duyingfang` 而非 `root`
- `aliyun.tmhcorps.cn` DNS 须设为"仅DNS"（灰云），否则 SSH 被 Cloudflare 拦截
- 飞书卡片回调响应 body 必须包含 `card.type="raw"` 和 `data` 包装层
- APScheduler 的定时任务在服务重启后重新注册，若服务在 09:00 后重启，当天洗盘+早报会跳过（次日才补跑）
- 飞书更新卡片消息：必须用 `PATCH /im/v1/messages/{id}`（不带 `/body`），`/body` 子路径只支持 text/post，不支持 interactive；schema 2.0 卡片更新时 config 中须加 `"update_multi": true`
- **scp 注意**：本地路径必须用正斜杠 `/c/Users/...`，反斜杠在 bash 中会导致 scp 静默失败
- AI 洗盘 `direct` 模式自动执行，请先用 `/review run report` 观察建议质量再切换；`report_only` 为默认推荐模式

## 服务器当前状态
- **当前版本：v1.0.0（已部署）**
- Web 后台地址：`https://pm.tmhcorps.cn/admin/`（无需登录，内部工具）
- migrate_v2.py / seed.py / seed_yadi.py 均已执行（勿重复运行）
- `init_db()` 自动创建所有表、索引，并处理旧库升级（幂等），**无需手动迁移**
- 首次启动：自动创建 users/projects 表并种入「雅迪」项目；ADMIN_OPEN_IDS 用户首次发消息时自动注册为 super_admin
- notify.py 的 crontab 条目已删除（早报改由 APScheduler 统一发送）

> 历史版本部署记录见 `CHANGELOG.md`

## 待开发
- [ ] systemd 自动重启（当前重启服务器后需手动拉起）
- [ ] Web 后台登录认证（当前无认证，内部工具暂可接受）
- [ ] fact 正文重写：低质量描述生成结构化改写建议，建议走确认卡片，不直接自动覆盖
- [ ] 多人协作卡片同步：同一批清洗建议任意一人处理后同步刷新其他人卡片状态（当前使用率不高，暂缓）
