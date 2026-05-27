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
- 服务器：SSH 配置名 `aliyun`（本地 `~/.ssh/config` 中预设，不同电脑同名指向 Cloudflare Tunnel），用户 `duyingfang`，Ubuntu 24.04
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
- 版本号存于 `VERSION` 文件（当前 `0.9.0`），语义化：`major.feature.patch`
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
├── claude_client.py # AI 对话(chat) + 信息提取(extract_facts) + 洗盘(nightly_review) + 分解(decompose_risk)
├── feishu.py        # 飞书 API：send_reply/send_reply_to_user（统一出口）、lark_md 卡片 builder、交互卡片、卡片响应格式
├── db.py            # SQLite CRUD：四层数据（assumptions/org_units/facts/todos）
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

当前共 20 条（3 铁律 + 10 通常dept + 5 雅迪项目专属）：
- PM角色边界、产品定位、ASPICE裁剪原则（铁律）
- OEM决策周期、书面确认原则、外部依赖节奏、团队协作范式、硬件验证约束（通常）
- 风险管理规则、Kickoff检查清单、需求变更管控规则、跨团队协作规则、问题处理分类流程（通常，从旧知识库迁入）
- 雅迪三方确认要求、雅迪项目定位、BSP由华阳负责、团队角色分工、六大管理领域（项目专属）

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

已植入：东软睿驰（company）→ 自动驾驶事业部（dept）→ 11 个团队（team）+ 雅迪（client_org），共 14 条。
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

**迁移说明**：`init_db()` 自动处理旧数据升级（添加 dimension 列并补填），幂等。

### Layer 3：待办事项 `todos` 表

可从 risk 分解（保留追溯）、挂到里程碑、或独立创建。

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

**关联关系**：
- `source_fact_id` → 追溯源头风险（risk 保持 active，通过 todo 跟踪推进）
- `plan_id` → 挂载到一级里程碑
- 两者均为空 → 独立待办

**AI 上下文**：注入未完成 todo（最多30条）+ 近14天已完成（最多10条），均带时间和追溯信息。

### 其他表

**conversations**（对话历史）
```
id / chat_id / role（user|assistant）/ content / created_at
```
按 chat_id 隔离，`/clear` 命令清空当前 chat_id 的记录。

**pending_notes**（待确认笔记，TTL 30 分钟）
```
chat_id（PRIMARY KEY）/ items_json / created_at（unix timestamp）
```
items_json 是数组，每项含：`type / content / action(new|update) / fact_id / fact_title / saved_count`
同一张表（key 加前缀）存放四种 pending：知识库确认、todo 确认、洗盘合并建议、洗盘清洗建议；统一 TTL=1800s（`db.PENDING_TTL`）。

**processed_events**（飞书事件去重）
```
event_id（PRIMARY KEY）/ created_at
```

**system_settings**（系统配置，v0.7.1 新增）
```
key / value / updated_at
```
当前使用项：`nightly_review_mode`，取值 `report_only`（仅报告，默认）或 `direct_cleanup`（直接清洗）。

## 关键函数（db.py）

**四层上下文**：
- `get_full_context(project)` → 返回结构化 dict，供 AI 注入
  - `dept_assumptions`：部门铁律/通识
  - `project_assumptions`：项目专属假设
  - `risks`：活跃风险（带记录/更新时间）
  - `schedule`：里程碑与节点（带时间）
  - `decisions`：决策记录（带更新时间）
  - `references`：相关方与参考信息
  - `todos`：待办事项（open + 近期完成，带追溯和时间）

**facts CRUD**：
- `add_fact(type_, title, body, ...)` → 新增（自动计算 dimension）
- `update_fact(id, **kwargs)` → 更新任意字段
- `append_to_fact(id, addition)` → body 末尾追加带时间戳更新
- `find_similar_fact(type_, content)` → 关键词重叠去重
- `list_facts(type_, dimension, status, project)` → 支持按 type 或 dimension 过滤

**todos CRUD**：
- `add_todo(title, body, priority, owner, due_date, project, source_fact_id, plan_id, source)` → 新增
- `get_todo(id)` → 查单条
- `update_todo(id, **kwargs)` → 更新（status/priority/owner/due_date 等）
- `list_todos(status, project, source_fact_id, plan_id)` → 过滤查询
- `get_todos_for_context(project, open_limit, done_limit, done_days)` → AI 上下文格式化文本

**assumptions CRUD**：
- `add_assumption(title, body, scope, scope_ref, confidence)` → 新增预设
- `update_assumption(id, **kwargs)` → 更新
- `list_assumptions(scope, scope_ref, active_only)` → 列出

**org_units CRUD**：
- `add_org_unit(type_, name, parent_id)` → 新增组织单元
- `list_org_units(type_)` → 列出
- `upsert_person(open_id, name)` → 自动缓存 @mention 人员信息

**洗盘相关**：
- `save_nightly_review(content)` → 存入 AI 洗盘报告
- `get_latest_nightly_review()` → 取最新洗盘报告
- `get_all_facts_for_review()` → 所有 active 非 report 的 facts 条目，供 AI 分析；输入包含 project，避免跨项目误判重复
- `get_setting(key, default)` / `set_setting(key, value)` → 读取/保存系统配置

**洗盘边界**：
- 当前洗盘对象是 `facts` 表，不包含 `todos`、`assumptions`、`org_units`
- `todos` 不是 fact；todo 可通过 `source_fact_id` 关联 risk/issue/blocker，但不会被当前洗盘直接处理
- 洗盘目标已升级为“项目数据提炼”：从杂乱 facts 中识别有用状态、可归档信息、风险候选、待办建议和描述质量问题
- 报告结构：可归档信息 / 可合并信息 / 当前状态更新建议 / 风险候选 / 待办建议 / 描述质量改写建议 / 低风险字段补全 / 数据健康评分
- `direct_cleanup` 只执行带 `[AUTO]` 前缀的低风险白名单命令：`/admin fact archive [ID]` 和 `/admin fact update [ID] status|owner|priority|due_date [值]`
- 不执行 AI 生成的 delete、新增 risk、新增 todo、合并、title/body 改写或其他非白名单命令；priority/status 会做合法值校验
- 正文改写、风险候选、待办建议属于高风险语义动作，只在报告里给人工确认建议，不自动保存
- 合并建议通过机器可读 `merge_candidates` 解析后发送飞书确认卡片；点击合并后执行 `append_to_fact(keep_id, ...)` 并归档被合入条目，不硬删除
- 风险/待办处理建议通过机器可读 `action_candidates` 解析后发送飞书确认卡片；点击后可关闭 risk、归档 fact、完成/取消 todo

## AI 上下文注入顺序（claude_client.py）

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

**AI 分解函数**：`decompose_risk(fact)` → 调用 AI 将 risk 条目拆解为 2-6 条可执行 todo（JSON 格式）

## 角色与权限系统（v0.6.0 新增）

### 角色概览
| 角色 | 来源 | 权限概述 |
|------|------|---------|
| `super_admin` | .env `ADMIN_OPEN_IDS` 自动注册，或管理员手动提升 | 全部功能 + /admin 系列 + AI 数据查询 |
| `pm` | 用户申请，管理员审批 | 完整 PM 工作模式（风险/待办/知识库/AI 提取卡片） |
| `member` | 用户申请，管理员审批 | 轻量 AI 对话（可查里程碑和组织信息；不注入 todos/risks，不触发提取卡片，不能使用 /risk /todo /note） |
| `pending` | 已申请未审批 | 仅 /start /join /help /version |
| `unknown` | 从未发过消息 | 同 pending |

### 权限分级详表
| 功能 | unknown/pending | member | pm | super_admin |
|------|:-:|:-:|:-:|:-:|
| /start /join /help /version | ✓ | ✓ | ✓ | ✓ |
| @Bot AI 对话 | ✗ | ✓（轻量上下文） | ✓（完整上下文） | ✓（管理员上下文） |
| /clear /leave | ✗ | ✓ | ✓ | ✓ |
| /risk（查看/管理） | ✗ | ✗ | ✓ | ✓ |
| /todo（查看/管理） | ✗ | ✗ | ✓ | ✓ |
| /note | ✗ | ✗ | ✓ | ✓ |
| AI 信息提取确认卡片 | ✗ | ✗ | ✓ | ✓ |
| 快捷菜单-里程碑 | ✗ | ✓ | ✓ | ✓ |
| 快捷菜单-待办/风险 | ✗ | ✗ | ✓ | ✓ |
| /admin 系列 | ✗ | ✗ | ✗ | ✓ |
| 快捷菜单-洗盘/用户 | ✗ | ✗ | ✗ | ✓ |

**AI 上下文差异**（`claude_client.py`）：
- `member`：仅注入角色定义 + 部门假设 + 里程碑/相关方（无 todos/risks/decisions）
- `pm`：完整上下文（含 todos + risks + decisions + 里程碑）
- `super_admin`：完整上下文 + 数据库结构说明

### 注册流程
1. 用户发 `/start` 查看项目列表
2. 发 `/join [项目名] [pm|member]` 提交申请
3. 所有管理员（`ADMIN_OPEN_IDS`）收到飞书审批卡片
4. 点击「批准」→ 用户状态改为 active + 通知用户；点击「拒绝」→ 通知用户被拒

### 数据库新增表
- **users**：`open_id/name/role/project/status(pending|active|rejected|inactive)`
- **projects**：`name/description/created_by/active/updated_at`；默认种入「雅迪」项目
- `projects.updated_at` 在 v0.7.0 补加，`init_db()` 会自动 ALTER TABLE 升级旧库

### 数据库索引（v0.7.0 新增）
`init_db()` 自动创建以下索引（幂等，重启即生效）：
- `idx_facts_status_project`：`facts(status, project)`
- `idx_todos_status_project`：`todos(status, project)`
- `idx_conversations_chat_id`：`conversations(chat_id)`
- `idx_processed_events_id`：`processed_events(event_id)`

### AI 说话人注入
每次对话在 system prompt 注入当前用户身份，例：
- `管理员-杜莹芳（最高权限，可询问系统数据和数据库信息）`
- `项目经理PM-佟海鹏（雅迪项目）`
- `项目成员-李浩（雅迪项目）`

管理员 AI 上下文额外注入数据库各表结构说明，方便询问系统内部数据。

### 飞书卡片回调新增 actions
- `approve_user`：批准注册申请
- `reject_user`：拒绝注册申请

## 已实现功能
1. **飞书 Bot 对话**：@Bot 发消息，结合四层知识上下文 + 对话历史用 AI 回答
2. **知识库管理**：`/admin list/add/update/enable/disable/delete`（兼容旧接口）
3. **风险管理**：`/risk list/close/reopen/owner/add`（PM 和管理员均可用）
4. **统一信息管理**：`/admin fact list/show/update/archive/delete/add`
5. **预设假设管理**：`/admin assumption list/show/add/update/archive/delete`
6. **组织结构管理**：`/admin org list/add`
7. **快速记录**：`/note [内容]` 直接存入知识库
8. **AI 智能提取**：用户发消息后台自动提取，每个独立事项单独一条
9. **相似性去重**：提取时匹配已有条目，卡片上区分"新增"和"追加 #ID"
10. **交互卡片确认**：逐条确认，支持"全部保存"/"跳过"；卡片标题动态显示进度；计数正确累加
11. **对话历史清除**：`/clear` 命令
12. **定时 AI 洗盘 + 早报推送**：每天 09:00 执行洗盘并立即推送给 ADMIN_OPEN_IDS | NOTIFY_OPEN_IDS（APScheduler 内置）；支持 `report_only/direct_cleanup` 两种模式
13. **待办事项系统**：`/todo` 命令新建/查询/完成/取消；支持关联 risk 追溯、挂载里程碑；AI 上下文带 todo 信息
14. **AI 分解 risk**：`/admin fact decompose [ID]` 自动将风险拆解为可执行 todo 列表
15. **时间戳上下文**：所有 facts 条目在 AI 上下文中带记录/更新日期，AI 可判断信息时效
16. **AI 语言约束**：不再说"已记录/已保存"等误导性语言，保存动作由用户通过卡片确认
17. **@mention 缓存**：消息中 @某人 时自动缓存姓名↔open_id 到 org_units 表，文本保留 @姓名 传给 AI
18. **版本管理**：`VERSION` 文件 + `/version` 命令
19. **注册与权限系统**：用户自主注册、管理员审批、三角色差异化 AI 上下文、说话人身份注入（v0.6.0）
20. **飞书 post 富文本消息支持**：同时处理 `text` 和 `post` 两种消息类型，解析 at/text/a 节点，剔除 @Bot 节点后正常进入对话流程；段落间保留 `\n` 换行，编号列表等排版结构完整传给 AI（v0.6.1）
21. **"思考中"占位消息**：AI 处理期间先发占位消息，回复就绪后原地 PATCH 更新，避免消息跳动；加 60 秒超时兜底（v0.6.3）
22. **AI 纯文本输出**：系统提示约束 AI 不输出 Markdown 符号；`feishu._strip_md()` 二次兜底剥除残留格式（v0.6.3）
23. **待办意图自动提取**：用户对话中明确说"加个待办/提醒"时，AI 自动识别并弹出确认卡片，支持逐条或全部新增（v0.6.3）
24. **`/admin fact decompose` 卡片确认**：AI 分解 risk 后不再直接写库，改为弹出待办确认卡片，用户可按需选择保存（v0.6.3）
25. **Web 管理后台**：浏览器访问 `https://pm.tmhcorps.cn/admin/`，可视化管理知识库/待办/用户/预设，无需登录（内部工具）（v0.7.0）
26. **数据库索引优化**：`facts/todos/conversations/processed_events` 核心查询字段加索引，`projects` 表补加 `updated_at`（v0.7.0）
27. **AI 洗盘配置与手动执行**：Web 后台可切换洗盘模式；飞书 `/admin review run` 可立即洗盘并发送给管理员和 PM（v0.7.1）
28. **AI 合并建议卡片确认**：洗盘报告中的合并候选会生成飞书卡片，管理员点击后执行合入并归档重复条目（v0.7.1）
29. **风险/待办清洗卡片确认**：洗盘报告中的风险关闭、信息归档、todo 完成/取消建议会生成飞书卡片，点击后执行（v0.7.1）
30. **洗盘卡片类型标签**：清洗建议卡片每条显示 `[风险]`/`[里程碑]`/`[待办]` 等类型标签，不再靠 action label 猜条目类型（v0.7.3）
31. **管理员命令各级提示完善**：`/admin fact` 不完整时提示中补充 `decompose` 子命令（v0.7.3）
32. **注册审批卡片显示真实姓名**：调用飞书 contact API 查询申请人姓名，不再显示 open_id 前缀（v0.7.4）
33. **项目绑定管理**：管理员可用 `/admin user project [open_id] [项目名|-]` 修改或清除用户项目绑定；用户可用 `/leave` 自助退出当前项目（v0.7.4）
34. **`/register` 改名为 `/start`**：新用户入口命令更符合 Bot 惯例，语义更准确（v0.7.5）
35. **Web 后台操作按钮列对齐修复**：知识库/待办/用户/预设各表的编辑归档按钮列正确对齐（v0.7.5）
36. **飞书机器人快捷菜单支持**：处理 `application.bot.menu_v6` 事件，支持查看待办/风险/里程碑、清除对话、AI 洗盘、用户列表等菜单操作（v0.7.6）
37. **`/risk` 独立命令**：风险管理从 `/admin risk` 下放至根级 `/risk`，PM 和管理员均可使用；`/admin` 命令严格限管理员；`db.list_risks` 支持 `project=None` 全量查询（v0.7.6）
38. **移除 `PRIMARY_ADMIN_OPEN_ID`**：审批卡片统一发给所有 `ADMIN_OPEN_IDS`，配置简化（v0.7.6）
39. **`/risk show [ID]`**：PM 和管理员可查看风险完整正文及关联进行中待办（v0.7.7）
40. **`/todo show [ID]` / `/todo update [ID] [字段] [值]`**：待办详情查看和字段更新（v0.7.7）
41. **审批命令通知 + 幂等**：`/admin user approve/reject` 发送飞书通知；卡片审批加幂等检查，防止多管理员重复操作（v0.7.7）
42. **member 权限收敛**：member 角色仅支持 @Bot 查询里程碑和组织信息 + 菜单里程碑查看；/risk、/todo、待办/风险菜单均不开放（v0.7.7）
43. **pending_notes TTL 延长至 30 分钟**：合并建议/清洗建议卡片有充裕的确认窗口（v0.7.7）
44. **帮助文本/命令帮助完整化**：按角色分区展示可用命令，`_admin_help()` 补齐全部子命令（v0.7.7）
45. **飞书消息统一卡片化**：删除 `_strip_md()`，所有回复走 `send_reply()`/`send_reply_to_user()` 统一出口；AI 回复改为 lark_md 卡片保留 markdown 格式；`/risk list/show`、`/todo list/show` 改为结构化卡片（带颜色 header、优先级图标）（v0.7.8）
46. **`/schedule` 里程碑命令**：独立命令 `/schedule list [all]`、`/schedule show [ID]`，member/PM/管理员均可用；快捷菜单"查看里程碑"同步换卡片；逾期自动标注 ⚠️（v0.7.8）
47. **快捷菜单项目查询逻辑统一**：有项目绑定查指定项目，无绑定查全量（对所有角色一致）；修复 PM 无绑定时按钮显示空数据的问题（v0.7.8）
48. **飞书卡片全面升级 schema 2.0**：所有卡片 builder 添加 `"schema":"2.0"` 并将 `elements` 包进 `body`；AI 回复/文本展示改用 `{"tag":"markdown"}` 元素，支持完整 Markdown（`##` 标题、`|表格|`、代码块）；column 内部保留 `lark_md` 文本；系统提示同步解除 `##` 和表格禁令（v0.8.1）
49. **"思考中"卡片原地更新修复**：卡片更新改用正确端点 `PATCH /im/v1/messages/{id}`（原 `/body` 子路径仅支持 text/post，不支持 interactive）；schema 2.0 更新时注入 `update_multi: True`；"思考中"占位卡片可被 AI 回复卡片原地替换（v0.8.1）
50. **管理员项目绑定与全量 AI 上下文**：`/join [项目名]` 对管理员直接生效（无需审批）；管理员无项目绑定时 `_resolve_project` 返回 `None`，`get_full_context(None)` 查全量（所有项目的 facts/todos 均注入，带项目标注）；`_sender_info` 显示管理员当前关注项目；AI 上下文新增"系统注册用户"章节（`db.get_users_summary()`），AI 可回答"谁在哪个项目、什么角色"（v0.8.2）
51. **AI 建议整合到主回复**：取消独立 `extract_facts` / `extract_todo_intent` 额外 AI 调用；AI 在主回复末尾内嵌 `===SUGGESTIONS=== JSON ===END_SUGGESTIONS===` 块，主流程解析后弹出统一建议确认卡片（v0.9.0）
52. **统一 AI 建议确认卡片**：`build_ai_suggestions_card` 按类型分组（风险/里程碑/知识/待办/更新建议），每项附"详情"按钮；`build_suggestion_detail_card` 展示完整信息 + 保存/跳过/返回操作；支持全部保存、全部跳过、全部处理完时显示汇总；`/admin fact decompose` 生成的待办也改走此卡片（v0.9.0）

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
@Bot [消息]    AI 对话（深度按角色不同，见权限分级表）
/clear         清除当前会话历史
/leave         退出当前项目绑定（角色降为 member，账号保留）

# 里程碑查看（member/pm/super_admin 均可）
/schedule list           查看进行中的里程碑（卡片）
/schedule list all       查看全部里程碑
/schedule show [ID]      查看里程碑详情（含关联待办，卡片）
```

### PM / 管理员可用
```
/note [内容]   快速记录笔记到知识库

# 风险管理
/risk list [open|all]
/risk show [ID]                                 完整正文 + 关联待办
/risk close [ID]
/risk reopen [ID]
/risk owner [ID] [姓名]
/risk add [type] [priority] [标题] | [描述]
  type: risk|issue|blocker|dependency  priority: high|medium|low

# 待办事项
/todo list                      查看进行中的待办
/todo list all                  查看全部待办（含已完成/已取消）
/todo list risk [ID]            查看某风险关联的待办
/todo list plan [ID]            查看某里程碑挂载的待办
/todo show [ID]                 查看待办详情（含关联风险/里程碑和备注正文）
/todo [内容]                     新建独立待办
/todo [内容] risk [ID]           从 risk 分解新建待办（保留追溯）
/todo [内容] plan [ID]           挂到里程碑新建待办
/todo update [ID] [字段] [值]    更新待办字段
  字段：title|body|priority|owner|due_date  priority: high|medium|low
/todo done [ID]                 标记待办完成
/todo cancel [ID]               取消待办
```

### 管理员专用
```
/admin stats

# 统一信息管理
/admin fact list                       列出所有 active 条目
/admin fact list risk                  按 type 过滤
/admin fact list all                   含 archived/resolved
/admin fact show [ID]                  完整正文（含历史更新）
/admin fact update [ID] [field] [值]   更新字段
  field: status|owner|priority|due_date|title|body
  status: open|resolved|archived
/admin fact archive [ID]               归档（软删除）
/admin fact delete [ID]                硬删除
/admin fact add [type] [标题] | [正文] 新增
/admin fact decompose [ID]             AI 分解 risk 为待办列表（卡片确认后入库）

# 用户管理
/admin user list                           列出所有用户
/admin user show [姓名/open_id]            查看用户详情
/admin user role [open_id] [pm|member|super_admin]  修改角色
/admin user project [open_id] [项目名|-]   修改或清除项目绑定（- 表示清除）
/admin user approve [open_id]              手动批准申请（含飞书通知，幂等）
/admin user reject [open_id]              拒绝申请（含飞书通知，幂等）
/admin user remove [open_id]              删除用户

# 项目管理
/admin project list                  列出所有项目
/admin project add [名称] | [描述]   创建项目
/admin project close [ID]            关闭项目
/admin project open [ID]             重新开启
/admin project bind [项目名]         将当前群聊绑定到项目
/admin project unbind                解除当前群聊绑定
/admin project bindings              查看所有群聊绑定

# AI 洗盘
/admin review status                   查看当前洗盘模式
/admin review mode report              设置为仅报告（默认，不改数据）
/admin review mode direct              设置为直接清洗（执行白名单命令）
/admin review run                      按当前模式立即洗盘，发送给管理员和 PM
/admin review run report               临时按仅报告模式执行一次
/admin review run direct               临时按直接清洗模式执行一次

# 预设假设管理（部门公认背景知识）
/admin assumption list [dept|project|client]
/admin assumption show [ID]
/admin assumption add [scope] [confidence] [标题] | [正文]
  scope: dept|project/项目名|client|global
  confidence: universal（铁律）|common（通常）|assumed（推测）
/admin assumption update [ID] [field] [值]
/admin assumption archive [ID]
/admin assumption delete [ID]

# 组织结构管理
/admin org list [type?]
/admin org add [type] [名称] [父节点ID?]
  type: company|dept|team|role|client_org
```

**命令解析约定**：`fact add`、`fact update`、`assumption add`、`risk add` 等长文本参数由各子命令 handler 自行拼接，不受顶层 `split(None, 4)` 截断影响；带 `|` 时左侧为标题/名称，右侧为正文/描述。没有 `|` 时，add 类命令通常把同一段文本同时作为标题和正文。

## 权限与推送管理
无独立用户表，通过 `.env` 配置：
```
ADMIN_OPEN_IDS=ou_d1ccad1071d7daf767337953ffeb317a,ou_佟海鹏的open_id
NOTIFY_OPEN_IDS=ou_其他需要收日报的人（非管理员也可收）
```
- `ADMIN_OPEN_IDS`：有权使用 `/admin` 命令；审批卡片发给所有管理员
- `NOTIFY_OPEN_IDS`：只收日报，无管理权限
- APScheduler 早报发送给 `ADMIN_OPEN_IDS | NOTIFY_OPEN_IDS` 全量
- 手动 `/admin review run` 发送给 `.env ADMIN_OPEN_IDS` + 数据库中 active 的 `super_admin` 和 `pm`
- **不要同时启用 crontab 跑 notify.py**，否则主管理员收到两份
- 获取 open_id：让对方发一条消息，从 `logs/app.log` 找 `sender=ou_xxx`

## 飞书应用配置要点
- 事件订阅：`im.message.receive_v1` + `card.action.trigger` + `application.bot.menu_v6`（三个都必须订阅）
- 快捷菜单：在飞书开放平台 → 应用能力 → 机器人 → 快捷菜单 中配置，event_key 须与下表完全一致
- 回调地址：`https://pm.tmhcorps.cn/webhook/feishu`
- 卡片回调响应格式：`{"toast":..., "card":{"type":"raw","data":{...}}}`（缺少此格式会报错200672）

### 快捷菜单事件键（application.bot.menu_v6）
由 `_handle_bot_menu` 处理，event_key 必须与飞书后台配置完全一致。

| event_key | 建议菜单名 | 最低权限 | 功能说明 |
|-----------|-----------|---------|---------|
| `show_help` | 使用帮助 | 全员（含未注册） | 展示 /help 内容（按角色显示不同版本） |
| `show_version` | 查看版本 | 全员（含未注册） | 显示当前运行版本号 |
| `clear_chat` | 清除对话 | active 已注册用户 | 清除当前对话历史及 pending 确认项 |
| `view_schedule` | 查看里程碑 | member / pm / super_admin | 列出当前项目 active 里程碑 |
| `view_todos` | 查看待办 | pm / super_admin | 列出进行中待办（按项目过滤） |
| `view_risks` | 查看风险 | pm / super_admin | 列出 open 风险/问题（按项目过滤） |
| `run_review` | AI 洗盘 | super_admin | 立即执行洗盘（按当前模式），完成后推送报告 |
| `admin_users` | 用户列表 | super_admin | 列出所有注册用户 |

> 注意：快捷菜单事件无 chat_id，`view_*` 使用发起人的 open_id 作为 chat_id，项目按用户绑定解析（super_admin 无项目绑定时查全量）。

## 飞书卡片模板清单

所有卡片 builder 和响应函数均在 `feishu.py`，card callback 分发在 `main.py` 的 `_handle_card_callback` / `_handle_card_trigger`。

### 通用说明
- **两类回调路由**（两者处理的 action 完全相同，飞书新旧协议均兼容）：
  - `card.action.trigger`（飞书新协议）→ `_handle_card_trigger`
  - `_handle_card_callback`（旧协议）：body 含 `action` + `open_message_id` 字段
- **回调响应格式**：`{"toast":{"type":"success/info/error","content":"..."}, "card":{"type":"raw","data":{...card body...}}}`
- **卡片发送**：`send_reply(chat_id, card_dict, ...)` / `send_reply_to_user(open_id, card_dict, ...)` 接受 dict 直接发 interactive；短文本走 text；长文本自动包成 `build_md_card`
- **卡片原地更新**：`update_message_card(message_id, card, ...)` → `PATCH /im/v1/messages/{id}`，config 自动注入 `update_multi:true`
- **pending 存储**：AI 建议用 `pending_commands`；洗盘合并用 `pending_merges`；洗盘清洗用 `pending_actions`；均 TTL=30 分钟

---

### 卡片 1：通用 lark_md 卡片
**Builder**: `build_md_card(text, title=None, color="blue")`
**特殊变体**: `build_thinking_card()` → 无标题，内容"⏳ 思考中..."，用于 AI 处理期间的占位消息（PATCH 替换）
**用途**: AI 对话回复、错误提示、占位消息、各命令纯文本响应
**结构**: 可选 header（title+color） + 单个 `{"tag":"markdown"}` 元素，支持完整 Markdown（##标题、表格、代码块）
**无交互按钮**

---

### 卡片 2：AI 建议确认卡片（v0.9.0 统一入口）
**Builder**: `build_ai_suggestions_card(items, chat_id)`
**发送**: `_send_ai_suggestions_card(chat_id, suggestions)` → 先 `save_pending_commands`，再 `send_reply`
**触发时机**: AI 主回复中解析到 `===SUGGESTIONS===` 块，或 `/admin fact decompose` AI 分解风险后
**Header**: "💡 AI 建议（N 项）" → "💡 AI 建议（已处理 P/N）" → "✅ AI 建议处理完毕（保存 S · 跳过 K）"（blue→green）
**分组展示**（5 组，无内容的组不显示）:
- ⚠️ 风险 / 问题：kind=new_fact，type∈{risk,issue,blocker,dependency}
- 📅 里程碑：kind=new_fact，type=milestone
- 📋 知识 / 决策 / 信息：kind=new_fact，其余 type
- ☐ 待办事项：kind=new_todo
- ✏️ 更新建议：kind∈{update_fact,update_todo}
**每行内容**: `[类型] 标题 ✅/⏭\n优先级 负责人 截止`；update 类型显示 `字段：旧值→新值`
**每行按钮**: "详情"（default）→ `{action:"suggestion_view_detail", chat_id, index}`
**底部按钮**: "全部保存"（primary）、"全部跳过"（danger）
**Action 值**（两类路由均支持）:
- `suggestion_view_detail` → 原地切换为卡片 3 详情
- `suggestion_back_to_list` → 原地切换回本卡片
- `suggestion_save_one` / `suggestion_skip_one` → `{chat_id, index}`
- `suggestion_save_all` / `suggestion_skip_all` → `{chat_id}`
**执行逻辑**: `_save_suggestion_item(item)` → new_fact→`add_fact`；new_todo→`add_todo`；update_fact→`update_fact`；update_todo→`update_todo`
**文字确认兼容**: 发"保存/确认"等关键词也会执行所有 pending suggestions

---

### 卡片 3：AI 建议详情卡片
**Builder**: `build_suggestion_detail_card(item, chat_id, index)`
**触发方式**: 卡片 2 点击"详情" → `suggestion_view_detail` → 原地更新
**Header**: "新增 [类型] 标题"（blue）/ "新增待办：标题"（blue）/ "更新 #ID 标题"（yellow）
**内容**: 完整元信息（操作类型/类型/优先级/负责人/截止/正文 或 字段旧→新值/原因）
**按钮**（未处理时）: "返回清单"（default）、"保存"（primary）、"跳过"（danger）
**按钮**（已处理时）: "返回清单"（default）、"已保存"或"已跳过"（default，仅提示）
**Action 值**: `suggestion_save_one` / `suggestion_skip_one` / `suggestion_back_to_list`

---

### 卡片 4：注册审批卡片
**Builder**: `build_approval_card(open_id, name, role, project)`
**Sender**: `send_card_to_user(open_id, card, ...)` 向所有 ADMIN_OPEN_IDS 逐一发送
**Header**: "📋 新用户注册申请"（blue）
**Fields（两列）**: 申请人、申请角色、申请项目、open_id
**按钮**: "✅ 批准"（primary）、"❌ 拒绝"（danger）
**Action 值（card.action.trigger）**:
- 批准: `{action:"approve_user", open_id, name, role, project}`
- 拒绝: `{action:"reject_user", open_id, name}`
**幂等处理**: 已 active 用户批准时返回 info toast；已处理申请拒绝时同样提示
**响应函数**: `card_approved_response(name, role, project)`、`card_rejected_response(name)`
**审批后副作用**: 向申请人发 DM 通知（`send_reply_to_user`）

---

### 卡片 5：AI 合并建议确认卡片
**Builder**: `build_merge_confirm_card(merges, chat_id, saved_count=0)`
**Sender**: `send_merge_confirm_card(chat_id, merges, app_id, app_secret)`
**触发时机**: AI 洗盘报告中解析到 `===MERGE_CANDIDATES_JSON===` 块
**Header**: "🔀 建议合并 N 组信息" → "✅ 已合并 N 组，还剩 N 组"（blue→green）
**每行内容**: `N. 合并到 #keep_id\n合入：#id1,#id2\n原因：xxx\n追加：预览文字`
**每行按钮**: "合并"（primary）
**底部按钮**: "✓ 全部合并"（primary）、"✗ 跳过"（danger）
**Action 值**:
- `merge_one`：`{chat_id, index, saved_count}` → `pop_pending_merge` + `_apply_merge_item`
- `merge_all`：`{chat_id, saved_count}` → 批量执行
- `skip_merges`：`{chat_id}` → `clear_pending_merges`
**执行逻辑**: `_apply_merge_item` → `append_to_fact(keep_id, ...)` + 被合入条目 status→archived

---

### 卡片 6：风险/待办清洗动作确认卡片
**Builder**: `build_action_confirm_card(actions, chat_id, saved_count=0)`
**Sender**: `send_action_confirm_card(chat_id, actions, app_id, app_secret)`
**触发时机**: AI 洗盘报告中解析到 `===ACTION_CANDIDATES_JSON===` 块
**Header**: "⚙️ 建议处理 N 项风险/待办" → "✅ 已处理 N 项，还剩 N 项"（blue→green）
**每行内容**: `N. [操作标签] [类型标签]#ID 标题\n原因：xxx`
**支持 kind+action 组合**: `(risk,close)`→关闭风险、`(fact,archive)`→归档信息、`(todo,done)`→完成待办、`(todo,cancel)`→取消待办
**每行按钮**: 操作标签前4字（primary）
**底部按钮**: "✓ 全部处理"（primary）、"✗ 跳过"（danger）
**Action 值**:
- `review_action_one`：`{chat_id, index, saved_count}` → `pop_pending_action` + `_apply_review_action`
- `review_action_all`：`{chat_id, saved_count}`
- `skip_review_actions`：`{chat_id}` → `clear_pending_actions`
**执行逻辑**: `_apply_review_action` → risk.close→`update_risk(status="closed")`；fact.archive→`update_fact(status="archived")`；todo.done/cancel→`update_todo(status=...)`

---

### 卡片 7：通用 Fact 详情卡片
**Builder**: `build_fact_show_card(fact)`
**触发方式**: `view_fact_detail` action → `{action:"view_fact_detail", id}` → 原地更新
**Header**: "#{id} {title}"（blue）
**内容**: 类型、状态、优先级、负责人、截止、项目、创建/更新时间 + 正文（lark_md）
**用途**: 通用 fact 详情查看，不限于风险（risk 专属走卡片 9）
**无操作按钮**（只读）

---

### 卡片 8：风险列表卡片
**Builder**: `build_risk_list_card(rows, status_filter="open")`
**Header**: "⚠️ 风险列表（进行中/全部 · N 条）"（red）
**每行内容**: `**#ID 标题**\n🔴 [类型·优先级]  负责人  截止`
**每行按钮**: "详情"（default）→ `{action:"view_risk_detail", id}`
**点击详情**: 原地更新为卡片 9 风险详情

---

### 卡片 9：风险详情卡片
**Builder**: `build_risk_show_card(fact, open_todos)`
**Header**: "#{id} {title}"（red）
**内容**: 类型/优先级（带图标）/负责人/截止/记录更新时间 + 正文（lark_md）
**关联待办区**（有时）: `关联待办（N 条进行中）\n- 🔴 #T{id} 标题（负责人）`
**触发方式**: 风险列表点击"详情" → `view_risk_detail` → 原地更新；`/risk show [ID]` 直接发送

---

### 卡片 10：待办列表卡片
**Builder**: `build_todo_list_card(rows)`
**Header**: "📋 待办列表（进行中 N / 共 N 条）"（blue）
**每行内容**: `{状态图标} {优先级图标} **#T{id} 标题**\n负责人 截止 创建 ← risk/milestone来源`
**状态图标**: ☐ open / ☑ done / ☒ cancelled
**每行按钮**: "详情"（default）→ `{action:"view_todo_detail", id}`
**点击详情**: 原地更新为卡片 11 待办详情

---

### 卡片 11：待办详情卡片
**Builder**: `build_todo_show_card(todo, source_fact, plan_fact)`
**Header**: "#T{id} {title}"（blue）
**内容**: 状态/优先级（带图标）/负责人/截止/创建更新时间（精确到分钟）；可选：关联风险、挂载里程碑；可选正文（lark_md）
**触发方式**: 待办列表点击"详情" → `view_todo_detail` → 原地更新；`/todo show [ID]` 直接发送
**无操作按钮**（只读）

---

### 卡片 12：里程碑列表卡片
**Builder**: `build_milestone_list_card(rows)`
**Header**: "📅 里程碑（进行中 N / 共 N 个）"（green）
**每行内容**: `✅/🔄 **#{id} 标题**\n⚠️逾期/截止日期  负责人`；逾期自动标注 ⚠️
**无交互按钮**（纯展示）；`/schedule show [ID]` 单独查看详情

---

### 卡片 13：里程碑详情卡片
**Builder**: `build_milestone_show_card(fact, open_todos)`
**Header**: "📅 #{id} {title}"（green，逾期时用 yellow）
**内容**: 状态/截止（逾期提醒）/负责人/记录更新时间；可选正文（lark_md）；可选关联待办列表
**触发方式**: `/schedule show [ID]` 直接发送
**无交互按钮**（只读）

---

### 卡片 14：AI 澄清问题卡片
**Builder**: `build_clarify_card(question, opts, chat_id, sender_open_id="")`
**触发时机**: AI 主回复中解析到 `===CLARIFY===` 块
**Header**: "需要确认一些信息"（yellow）
**内容**: `❓ 问题` + 最多4个选项按钮 + "💬 也可以直接发送文字回复"
**每个选项**: `{action:"clarify_option", text, chat_id, sender_open_id}`
**点击后**: 原地更新为"⏳ 正在整合信息，稍候..."，`asyncio.create_task(_clarify_and_respond(...))` 异步继续作答

---

### 卡片 15：早报卡片
**Builder**: `build_morning_report_card(project_name, risks, review_text, today)`
**Header**: "📋 {today} {project_name}早报" / "📋 {today} 综合早报"（blue）
**内容**:
- 无风险时: "✅ 当前无待处理风险/问题"
- 有风险时: 按 高/中/低 分组列出，格式 `#{id} [类型] 标题（负责人）⏰截止`，末尾显示总数
- 可选 AI 洗盘摘要（≤800字，超出截断提示"完整报告已存入系统"）
**发送场景**: APScheduler 09:00 自动触发 / `/admin review run` 手动触发
**按项目分发**: 全项目综合卡片→管理员+NOTIFY_OPEN_IDS；各项目单独卡片→对应 PM（不重复发给管理员）
**无交互按钮**（纯展示）

---

### 辅助常量（feishu.py）
```python
_PRIO_ICON  = {"high":"🔴", "medium":"🟡", "low":"🟢"}
_PRIO_ZH2   = {"high":"高", "medium":"中", "low":"低"}
_TYPE_TAG   = {"risk":"风险","issue":"问题","blocker":"阻塞","dependency":"依赖","milestone":"里程碑",...}
_TYPE_LABELS = {same keys}  # 用于 AI 建议卡片类型标签
_ACTION_LABELS = {("risk","close"):"关闭风险", ("fact","archive"):"归档信息",...}
_SUGGESTION_SECTIONS = [("risk_fact","⚠️ 风险/问题"),("schedule_fact","📅 里程碑"),...]  # 建议卡片分组
```

### 卡片交互汇总表

| action 值 | 触发卡片 | 路由 | 执行逻辑 |
|-----------|---------|------|---------|
| `suggestion_view_detail` | AI 建议列表 | 两类均可 | 原地切换为详情卡片 3 |
| `suggestion_back_to_list` | AI 建议详情 | 两类均可 | 原地切换回列表卡片 2 |
| `suggestion_save_one` | AI 建议详情/列表 | 两类均可 | `_save_suggestion_item` + 刷新卡片 2 |
| `suggestion_skip_one` | AI 建议详情/列表 | 两类均可 | 标记 skipped + 刷新卡片 2 |
| `suggestion_save_all` | AI 建议列表 | 两类均可 | 批量 `_save_suggestion_item` + 刷新卡片 2 |
| `suggestion_skip_all` | AI 建议列表 | 两类均可 | 全标 skipped + 刷新卡片 2 |
| `approve_user` | 注册审批卡片 | card_trigger | `update_user(active)` + DM 通知申请人 |
| `reject_user` | 注册审批卡片 | card_trigger | `update_user(rejected)` + DM 通知申请人 |
| `merge_one` | 合并建议卡片 | 两类均可 | `_apply_merge_item` + 刷新卡片 5 |
| `merge_all` | 合并建议卡片 | 两类均可 | 批量合并 |
| `skip_merges` | 合并建议卡片 | 两类均可 | `clear_pending_merges` |
| `review_action_one` | 清洗动作卡片 | 两类均可 | `_apply_review_action` + 刷新卡片 6 |
| `review_action_all` | 清洗动作卡片 | 两类均可 | 批量执行 |
| `skip_review_actions` | 清洗动作卡片 | 两类均可 | `clear_pending_actions` |
| `view_risk_detail` | 风险列表卡片 | card_trigger | 原地更新为卡片 9 |
| `view_todo_detail` | 待办列表卡片 | card_trigger | 原地更新为卡片 11 |
| `view_fact_detail` | （通用） | card_trigger | 原地更新为卡片 7 |
| `clarify_option` | 澄清问题卡片 | card_trigger | 原地更新占位 + 异步 AI 继续作答 |

## 已知坑
- SSH 登录用 `duyingfang` 而非 `root`
- `aliyun.tmhcorps.cn` DNS 须设为"仅DNS"（灰云），否则 SSH 被 Cloudflare 拦截
- 飞书卡片回调响应 body 必须包含 `card.type="raw"` 和 `data` 包装层
- APScheduler 的定时任务在服务重启后重新注册，若服务在 09:00 后重启，当天洗盘+早报会跳过（次日才补跑）
- AI 洗盘 `direct_cleanup` 只执行报告中带 `[AUTO]` 前缀的低风险 facts 命令；请先用 `/admin review run report` 观察建议质量；目前不会直接清洗 todos
- Web 后台概览页可切换洗盘模式，但没有单独登录认证，仍按内部工具处理
- 飞书更新卡片消息：必须用 `PATCH /im/v1/messages/{id}`（不带 `/body`），`/body` 子路径只支持 text/post，不支持 interactive；schema 2.0 卡片更新时 config 中须加 `"update_multi": true`

## 服务器当前状态
- v0.7.6 已部署（飞书快捷菜单 + /risk 独立命令 + 移除 PRIMARY_ADMIN_OPEN_ID）
- v0.7.7 已部署（/risk show + /todo show/update + approve/reject 通知幂等 + member 权限收敛 + TTL 延长至30分钟 + 帮助文本完整化）
- v0.7.8 已部署（飞书消息全卡片化 + /schedule 里程碑命令 + 删除 _strip_md）
- v0.8.0 已部署（按项目早报卡片+PM推送、风险/待办列表可点击详情、AI标题加粗渲染修复、AI澄清问题卡片）
- v0.8.1 已部署（飞书卡片全面升级 schema 2.0；"思考中"卡片原地更新修复：PATCH 端点从 /body 改为正确的 /messages/{id}，注入 update_multi:true）
- v0.8.2 已部署（管理员 /join 直接绑定项目；无绑定时查全量跨项目上下文；AI 注入用户列表；sender_info 显示管理员项目）
- v0.8.3 已部署（保留待确认状态；文字“保存/确认/跳过”可消费 pending；AI 建议命令转确认按钮后再写库）
- v0.8.4 已部署（补全 AI 命令集含 risk owner/close/reopen；修复卡片发送失败静默问题，新增 400 响应日志与失败兜底提示）
- v0.8.5 已部署（新增 AI 执行口吻防误导：无命令时禁止”已确认/已更新/已保存/已执行”语气，统一提示”仅建议未落库”）
- v0.9.0 已部署并验证（AI 建议整合到主回复：取消单独 extract_facts/extract_todo_intent 调用，AI 在回复中内嵌 ===SUGGESTIONS=== 块；统一建议确认卡片 build_ai_suggestions_card，支持按类型分组/详情查看/按条保存跳过/全部保存跳过；旧 command_*/save_one/save_all/todo 卡片全部清理；修复 AI 建议块未触发：提示词重构移至末尾并加具体示例，历史记录存 clean 版本避免污染）
- **scp 注意**：本地路径必须用正斜杠 `/c/Users/...`，反斜杠在 bash 中会导致 scp 静默失败
- Web 后台地址：`https://pm.tmhcorps.cn/admin/`（无需登录，内部工具）
- migrate_v2.py 已执行（DB已迁移，勿重复运行）
- todos/users/projects/system_settings 表由 `init_db()` 自动创建，无需手动迁移
- v0.7.0 起 `init_db()` 自动补加 projects.updated_at 列和4条索引（幂等）；v0.7.1 起自动创建 system_settings
- notify.py 的 crontab 条目已删除（早报改由 APScheduler 统一发送）
- **部署后首次启动**：`init_db()` 自动创建 users/projects 表并种入「雅迪」项目；.env 中的 ADMIN_OPEN_IDS 用户首次发消息时自动注册为 super_admin

## 待开发
- [x] 飞书机器人快捷菜单（`application.bot.menu_v6`，v0.7.6）
- [x] 飞书消息全卡片化：send_reply 统一出口，AI 回复 lark_md 卡片，/risk /todo /schedule 结构化卡片（v0.7.8）
- [x] /schedule 里程碑命令（member/pm/super_admin 均可，v0.7.8）
- [ ] 佟海鹏 open_id 添加到 .env ADMIN_OPEN_IDS（让他在飞书发一条消息看日志）
- [ ] systemd 自动重启（当前重启服务器后需手动拉起）
- [ ] Web 后台登录认证（当前无认证，内部工具暂可接受）
- [ ] 多项目支持完善（Web 后台已支持筛选，飞书侧已有群聊绑定）
- [ ] todo 洗盘：超期、长期 open、缺 owner、源风险已关闭但 todo 仍 open
- [ ] fact 正文重写：低质量描述生成结构化改写建议，建议走确认卡片，不直接自动覆盖
- [ ] 多人协作卡片同步：同一批清洗建议发给 admin/项目 PM，任意一人处理后同步刷新其他人的卡片状态（当前使用率不高，暂缓）
- [x] 知识库 Web 管理后台（v0.7.0，`https://pm.tmhcorps.cn/admin/`）
- [x] AI 洗盘模式配置与手动执行（v0.7.1）
- [x] AI 合并建议卡片确认（v0.7.1）
- [x] 风险/待办清洗卡片确认（v0.7.1）
- [x] 群组绑定项目（`/admin project bind`，代码已全部实现）
- [x] 待办事项系统（todos 表 + /todo 命令 + AI 分解 + 上下文注入）
- [x] 时间戳注入 AI 上下文
- [x] AI 语言约束（不说"已记录"）
- [x] @mention 姓名缓存
- [x] VERSION 版本文件
- [x] 用户注册与角色管理（super_admin/pm/member，飞书卡片审批，说话人身份注入 AI）
