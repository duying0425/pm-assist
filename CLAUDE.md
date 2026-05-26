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
- 服务器：`aliyun.tmhcorps.cn`，用户 `duyingfang`，Ubuntu 24.04
- 项目目录：`~/pm-assist`（虚拟环境在 `~/pm-assist/venv`）
- 启动命令：`nohup venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000 >> logs/app.log 2>&1 &`
- 日志：`~/pm-assist/logs/app.log`
- 公网地址：`https://pm.tmhcorps.cn`（Cloudflare 代理 HTTP→HTTPS）
- nginx 配置：`/etc/nginx/conf.d/apps.conf`（与 chat.tmhcorps.cn 共用）
- 飞书 Webhook：`https://pm.tmhcorps.cn/webhook/feishu`
- .env 位置：`~/pm-assist/.env`（含 FEISHU_APP_ID/SECRET/TOKEN、OPENROUTER_API_KEY、ADMIN_OPEN_IDS、NOTIFY_OPEN_IDS、PRIMARY_ADMIN_OPEN_ID）

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

## 代码结构
```
pm-assist/
├── main.py          # FastAPI 主入口，Webhook 处理，管理员命令，卡片回调
├── claude_client.py # AI 对话(chat) + 关键信息提取(extract_facts) + 夜间洗盘(nightly_review)
├── feishu.py        # 飞书 API：发文本、发交互卡片、卡片响应格式
├── db.py            # SQLite CRUD：三层知识架构（assumptions/org_units/facts）
├── config.py        # 环境变量加载（从 .env 读取）
├── notify.py        # 消息推送：build_risk_section() + build_morning_report(review)
├── migrate_v2.py    # 一次性迁移脚本（已执行，勿重复执行）
├── seed.py          # 初始知识库数据（已执行，勿重复执行）
├── seed_yadi.py     # 雅迪项目初始数据（已执行，勿重复执行）
├── deploy/
│   ├── nginx.conf        # nginx 站点配置模板
│   ├── pm-assist.service # systemd 服务文件（备用，当前未启用）
│   ├── setup.sh          # 一键部署脚本
│   └── start.sh          # 启动脚本
└── logs/app.log     # 运行日志（服务器上）
```

## 知识架构（三层）

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
id / type（company|dept|team|role|client_org）/ name / parent_id / feishu_id / attributes(JSON)
```

已植入：东软睿驰 → 自动驾驶事业部 → 11 个团队 + 雅迪（client_org），共 15 条。

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

**迁移说明**：`init_db()` 自动处理旧数据升级（添加 dimension 列并补填），幂等。服务器首次部署新版本后运行 `venv/bin/python migrate_v2.py` 完成数据迁移。

### 其他表
- `conversations`：对话历史，按 chat_id 隔离
- `pending_notes`：待确认笔记，TTL 10 分钟，items_json 含 action(new|update)/fact_id/saved_count
- `processed_events`：事件去重

## 关键函数（db.py）

**三层上下文**：
- `get_full_context(project)` → 返回结构化 dict，供 AI 按优先级注入（替代旧的 get_knowledge_text + get_risks_text）
  - `dept_assumptions`：部门铁律/通识
  - `project_assumptions`：项目专属假设
  - `risks`：活跃风险与问题
  - `schedule`：里程碑与节点
  - `decisions`：决策记录
  - `references`：相关方与参考信息

**facts CRUD**：
- `add_fact(type_, title, body, ...)` → 新增（自动计算 dimension）
- `update_fact(id, **kwargs)` → 更新任意字段
- `append_to_fact(id, addition)` → body 末尾追加带时间戳更新
- `find_similar_fact(type_, content)` → 关键词重叠去重
- `list_facts(type_, dimension, status, project)` → 支持按 type 或 dimension 过滤

**assumptions CRUD**：
- `add_assumption(title, body, scope, scope_ref, confidence)` → 新增预设
- `update_assumption(id, **kwargs)` → 更新
- `list_assumptions(scope, scope_ref, active_only)` → 列出

**org_units CRUD**：
- `add_org_unit(type_, name, parent_id)` → 新增组织单元
- `list_org_units(type_)` → 列出

**洗盘相关**：
- `save_nightly_review(content)` → 存入 AI 洗盘报告
- `get_latest_nightly_review()` → 取最新洗盘报告
- `get_all_facts_for_review()` → 所有 active 非 report 条目，供 AI 分析

## AI 上下文注入顺序（claude_client.py）

```
[静态] 角色定义（PM助手职责）
[L0]   部门预设假设（铁律/通识，scope=dept/global）
[L1]   项目专属假设（scope=project）
[L2]   活跃风险与问题（dimension=risk）
[L3]   里程碑与计划（dimension=schedule）
[L4]   决策记录（dimension=decision）
[L5]   相关方与参考（dimension=stakeholder/resource/scope）
```

## 已实现功能
1. **飞书 Bot 对话**：@Bot 发消息，结合三层知识上下文 + 对话历史用 AI 回答
2. **知识库管理**：`/admin list/add/update/enable/disable/delete`（兼容旧接口）
3. **风险管理**：`/admin risk list/close/reopen/owner/add`
4. **统一信息管理**：`/admin fact list/show/update/archive/delete/add`
5. **预设假设管理**：`/admin assumption list/show/add/update/archive/delete`
6. **组织结构管理**：`/admin org list/add`
7. **快速记录**：`/note [内容]` 直接存入知识库
8. **AI 智能提取**：用户发消息后台自动提取，每个独立事项单独一条
9. **相似性去重**：提取时匹配已有条目，卡片上区分"新增"和"追加 #ID"
10. **交互卡片确认**：逐条确认，支持"全部保存"/"跳过"；卡片标题动态显示进度（已保存N条/还剩M条）；最终计数正确累加
11. **对话历史清除**：`/clear` 命令
12. **定时 AI 洗盘 + 早报推送**：凌晨 00:30 洗盘存 DB；09:00 推送给 ADMIN_OPEN_IDS | NOTIFY_OPEN_IDS（APScheduler 内置）

## 管理员命令速查
```
# 风险管理
/admin risk list [open|all]
/admin risk close/reopen [ID]
/admin risk owner [ID] [姓名]
/admin risk add [type] [priority] [标题] | [描述]
  type: risk|issue|blocker|dependency  priority: high|medium|low

# 统一信息管理
/admin fact list                       # 列出所有 active 条目
/admin fact list risk                  # 按 type 过滤
/admin fact list all                   # 含 archived/resolved
/admin fact show 5                     # 完整正文（含历史更新）
/admin fact update 5 status resolved
/admin fact update 5 owner 李工
/admin fact archive 5                  # 归档（软删除）
/admin fact delete 5                   # 硬删除
/admin fact add milestone 5月底完成集成测试 | 详细说明

# 预设假设管理（部门公认背景知识）
/admin assumption list                 # 所有假设
/admin assumption list dept            # 只看部门级
/admin assumption list project         # 只看项目级
/admin assumption show 3
/admin assumption add dept universal PM角色边界 | PM不直接管人...
/admin assumption add project/yadi common 雅迪变更确认 | 需三方书面确认
  scope: dept|project/项目名|client|global
  confidence: universal（铁律）|common（通常）|assumed（推测）
/admin assumption update 3 body 更新后的内容
/admin assumption archive 3

# 组织结构管理
/admin org list
/admin org add team 新团队名称 [父节点ID]
```

## 权限与推送管理
无独立用户表，通过 `.env` 配置：
```
ADMIN_OPEN_IDS=ou_d1ccad1071d7daf767337953ffeb317a,ou_佟海鹏的open_id
NOTIFY_OPEN_IDS=ou_其他需要收日报的人（非管理员也可收）
PRIMARY_ADMIN_OPEN_ID=ou_d1ccad1071d7daf767337953ffeb317a
```
- `ADMIN_OPEN_IDS`：有权使用 `/admin` 命令
- `NOTIFY_OPEN_IDS`：只收日报，无管理权限
- APScheduler 早报发送给 `ADMIN_OPEN_IDS | NOTIFY_OPEN_IDS` 全量
- **不要同时启用 crontab 跑 notify.py**，否则主管理员收到两份
- 获取 open_id：让对方发一条消息，从 `logs/app.log` 找 `sender=ou_xxx`

## 飞书应用配置要点
- 事件订阅：`im.message.receive_v1` + `card.action.trigger`（两个都必须订阅）
- 回调地址：`https://pm.tmhcorps.cn/webhook/feishu`
- 卡片回调响应格式：`{"toast":..., "card":{"type":"raw","data":{...}}}`（缺少此格式会报错200672）

## 已知坑
- SSH 登录用 `duyingfang` 而非 `root`
- `aliyun.tmhcorps.cn` DNS 须设为"仅DNS"（灰云），否则 SSH 被 Cloudflare 拦截
- 飞书卡片回调响应 body 必须包含 `card.type="raw"` 和 `data` 包装层
- APScheduler 的定时任务在服务重启后重新注册，若服务在 00:30 后重启，当天洗盘会跳过（次日才补跑）

## 服务器当前状态（已完成）
- v2架构已部署，服务运行中
- migrate_v2.py 已执行（DB已迁移，勿重复运行）
- notify.py 的 crontab 条目已删除（早报改由 APScheduler 统一发送）
- 旧 process/knowledge 知识条目已迁移为 assumptions 并归档

## 待开发
- [ ] 佟海鹏 open_id 添加到 .env ADMIN_OPEN_IDS（让他在飞书发一条消息看日志）
- [ ] systemd 自动重启（当前重启服务器后需手动拉起）
- [x] 定时任务：凌晨 AI 洗盘（00:30）+ 早报推送（09:00），APScheduler 内置于 FastAPI
- [x] 三层知识架构：assumptions（预设假设）+ org_units（组织结构）+ facts（项目事项）
- [x] 卡片逐条保存计数正确累加
- [x] 早报双发问题修复（APScheduler 统一发送，停用 crontab）
- [x] 旧 process/knowledge 条目审查并迁移为 assumptions
- [ ] 项目上下文感知（群组绑定项目，自动注入项目信息）
- [ ] 多项目支持
- [ ] 知识库 Web 管理后台
