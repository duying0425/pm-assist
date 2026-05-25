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

## 代码结构
```
pm-assist/
├── main.py          # FastAPI 主入口，Webhook 处理，管理员命令，卡片回调
├── claude_client.py # AI 对话(chat) + 关键信息提取(extract_facts)
├── feishu.py        # 飞书 API：发文本、发交互卡片、卡片响应格式
├── db.py            # SQLite CRUD：facts 表 + 所有业务逻辑
├── config.py        # 环境变量加载（从 .env 读取）
├── notify.py        # 消息推送相关
├── seed.py          # 初始知识库数据（已执行，勿重复执行）
├── seed_yadi.py     # 雅迪项目初始数据（已执行，勿重复执行）
├── deploy/
│   ├── nginx.conf        # nginx 站点配置模板
│   ├── pm-assist.service # systemd 服务文件（备用，当前未启用）
│   ├── setup.sh          # 一键部署脚本
│   └── start.sh          # 启动脚本
└── logs/app.log     # 运行日志（服务器上）
```

## 数据库（SQLite: pm_assist.db，在服务器上）

### 核心表：facts（统一信息表，替代原 knowledge_blocks + risks）

```
id          INTEGER PRIMARY KEY
type        TEXT    -- risk|issue|blocker|dependency|milestone|decision|team|client|knowledge|process|org
title       TEXT
body        TEXT    -- 正文；AI追加更新时末尾追加"[日期 更新] 内容"保留历史
status      TEXT    -- active（默认）| resolved | archived
priority    TEXT    -- high|medium|low（风险类用）
owner       TEXT    -- 负责人
due_date    TEXT    -- 截止日期
project     TEXT    -- 默认 yadi
source      TEXT    -- seed|manual|ai（信息来源）
created_at  TEXT    -- 本地时间，自动生成
updated_at  TEXT    -- 本地时间，每次更新自动刷新
```

**type 语义**：
- `risk / issue / blocker / dependency`：可追踪的项目风险与问题（进风险清单）
- `milestone / decision`：可追踪的里程碑与决策（进风险清单）
- `org / process / client / knowledge / team`：参考知识（进知识库）

**迁移说明**：首次 `init_db()` 自动将旧 `knowledge_blocks` + `risks` 迁移到 `facts`，幂等，旧表保留。

### 其他表
- `conversations`：对话历史，按 chat_id 隔离，字段 chat_id/role/content/created_at
- `pending_notes`：待确认笔记，TTL 10 分钟，items_json 含 action(new|update)/fact_id
- `processed_events`：事件去重

## 关键函数（db.py）
- `add_fact(type_, title, body, ...)` → 新增任意类型条目
- `update_fact(id, **kwargs)` → 更新任意字段（status/owner/priority/due_date/title/body）
- `append_to_fact(id, addition)` → 在 body 末尾追加带时间戳的更新（保留历史）
- `find_similar_fact(type_, content)` → 关键词重叠匹配，用于 AI 提取时去重
- `get_knowledge_text()` → 拼装非风险类 active 条目给 AI（知识库部分）
- `get_risks_text()` → 拼装 risk/issue/blocker/dependency active 条目给 AI
- `list_facts(type_, status, project)` → 按条件列出条目
- `pop_pending_item(chat_id, index)` → 弹出单条待确认项

## 已实现功能
1. **飞书 Bot 对话**：@Bot 发消息，结合知识库+风险清单+对话历史用 AI 回答
2. **知识库管理**：`/admin list/add/update/enable/disable/delete`（兼容旧接口）
3. **风险管理**：`/admin risk list/close/reopen/owner/add`
4. **统一信息管理**：`/admin fact list/show/update/archive/delete/add`（新接口）
5. **快速记录**：`/note [内容]` 直接存入知识库
6. **AI 智能提取**：用户发消息后台自动提取，每个独立事项单独一条（不合并）
7. **相似性去重**：提取时匹配已有条目，卡片上区分"新增"和"追加 #ID"两种操作
8. **交互卡片确认**：逐条确认，支持"全部保存"/"跳过"
9. **对话历史清除**：`/clear` 命令

## 管理员命令速查
```
# 查看所有风险（active=open）
/admin risk list
/admin risk list all

# 关闭/重开/设负责人
/admin risk close 3
/admin risk owner 3 张工
/admin risk add issue high 测试环境未搭建 | 具体描述

# 统一信息管理（更强）
/admin fact list                    # 列出所有 active 条目
/admin fact list risk               # 只看风险类
/admin fact list all                # 含 archived/resolved
/admin fact show 5                  # 看完整正文（含历史更新记录）
/admin fact update 5 status resolved
/admin fact update 5 owner 李工
/admin fact archive 5               # 归档（软删除）
/admin fact delete 5                # 硬删除
/admin fact add milestone 5月底完成集成测试 | 详细说明
```

## 权限与推送管理
无独立用户表，通过 `.env` 配置：
```
ADMIN_OPEN_IDS=ou_d1ccad1071d7daf767337953ffeb317a,ou_佟海鹏的open_id
NOTIFY_OPEN_IDS=ou_其他需要收日报的人（非管理员也可收）
```
- `ADMIN_OPEN_IDS`：有权使用 `/admin` 命令，且自动收日报
- `NOTIFY_OPEN_IDS`：只收日报，无管理权限
- notify.py 发送目标 = `ADMIN_OPEN_IDS | NOTIFY_OPEN_IDS` 并集
- 获取 open_id：让对方发一条消息，从 `logs/app.log` 找 `sender=ou_xxx`

## 飞书应用配置要点
- 事件订阅：`im.message.receive_v1` + `card.action.trigger`（两个都必须订阅）
- 回调地址：`https://pm.tmhcorps.cn/webhook/feishu`
- 卡片回调响应格式：`{"toast":..., "card":{"type":"raw","data":{...}}}`（缺少此格式会报错200672）

## 已知坑
- SSH 登录用 `duyingfang` 而非 `root`
- `aliyun.tmhcorps.cn` DNS 须设为"仅DNS"（灰云），否则 SSH 被 Cloudflare 拦截
- 飞书卡片回调响应 body 必须包含 `card.type="raw"` 和 `data` 包装层

## 待开发
- [ ] 佟海鹏 open_id 添加到 .env ADMIN_OPEN_IDS（让他在飞书发一条消息看日志）
- [ ] systemd 自动重启（当前重启服务器后需手动拉起）
- [ ] 定时任务：每日/每周风险提醒推送
- [ ] 项目上下文感知（群组绑定项目，自动注入项目信息）
- [ ] 多项目支持
- [ ] 知识库 Web 管理后台
