# pm-assist 项目上下文

## 项目定位
东软睿驰自动驾驶团队内部飞书PM助手Bot。帮助新人PM处理项目协调、客户沟通、风险管理等日常工作。通过飞书@Bot方式交互，目标"越用越好"。

## 用户信息
- 公司：东软睿驰（Neusoft RuiChi），自动驾驶团队
- 角色：PM，项目负责人，内部工具开发决策者
- 技术背景：有服务器运维能力，了解基本开发概念，非专业开发者
- 飞书管理员 open_id：`ou_d1ccad1071d7daf767337953ffeb317a`
- 协作风格：直接给可运行代码；说"你决定"时直接选最优方案执行，无需再问

## 部署信息
- 服务器：`aliyun.tmhcorps.cn`，用户 `duyingfang`，Ubuntu 24.04
- 项目目录：`~/pm-assist`（虚拟环境在 `~/pm-assist/venv`）
- 启动命令：`nohup venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000 >> logs/app.log 2>&1 &`
- 日志：`~/pm-assist/logs/app.log`
- 公网地址：`https://pm.tmhcorps.cn`（Cloudflare 代理 HTTP→HTTPS）
- nginx 配置：`/etc/nginx/conf.d/apps.conf`（与 chat.tmhcorps.cn 共用）
- 飞书 Webhook：`https://pm.tmhcorps.cn/webhook/feishu`
- .env 位置：`~/pm-assist/.env`（含 FEISHU_APP_ID/SECRET/TOKEN、OPENROUTER_API_KEY、ADMIN_OPEN_IDS）

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
├── db.py            # SQLite CRUD：知识块、对话历史、事件去重、待确认笔记
├── config.py        # 环境变量加载（从 .env 读取）
├── notify.py        # 消息推送相关
├── seed.py          # 初始知识库数据（已执行，勿重复执行）
├── seed_yadi.py     # 扩展知识库数据
├── deploy/
│   ├── nginx.conf       # nginx 站点配置模板
│   ├── pm-assist.service # systemd 服务文件（备用，当前未启用）
│   ├── setup.sh         # 一键部署脚本
│   └── start.sh         # 启动脚本
└── logs/app.log     # 运行日志（服务器上）
```

## 数据库（SQLite: pm_assist.db，在服务器上）
- `knowledge_blocks`：通用知识库，category/title/content/enabled
- `risks`：结构化风险/问题表，字段：id/type(risk|issue|blocker|dependency)/title/description/owner/priority(high|medium|low)/status(open|closed|resolved)/due_date/project/created_at/updated_at
- `conversations`：对话历史，按 chat_id 隔离
- `pending_notes`：待确认笔记，TTL 10 分钟，json 存 items 列表，支持逐条 pop
- `processed_events`：事件去重

## 关键函数（db.py）
- `get_knowledge_text()` → 拼装知识块给 AI
- `get_risks_text()` → 拼装未关闭风险给 AI
- `add_risk(type_, title, desc, owner, priority, due_date)` → 新增风险
- `update_risk(id, **kwargs)` → 更新任意字段
- `pop_pending_item(chat_id, index)` → 弹出单条待确认项

## 已实现功能
1. 飞书 Bot 对话：@Bot 发消息，结合知识库+风险清单+对话历史用 AI 回答
2. 知识库管理：管理员 `/admin list/add/update/enable/disable/delete`
3. 风险/问题管理：`/admin risk list [open|all]` / `close [ID]` / `owner [ID] [姓名]`
4. 快速记录：`/note [内容]` 直接存入知识库
5. AI 智能提取：用户发消息后台自动提取风险/里程碑/决策/人员/客户信息
6. 交互卡片逐条确认：提取信息后发飞书卡片，每条有"保存"按钮+"全部保存"/"跳过"
7. 对话历史清除：`/clear` 命令

## 飞书应用配置要点
- 事件订阅：`im.message.receive_v1` + `card.action.trigger`（两个都必须订阅）
- 回调地址：`https://pm.tmhcorps.cn/webhook/feishu`
- 卡片回调响应格式：`{"toast":..., "card":{"type":"raw","data":{...}}}`（缺少此格式会报错200672）

## 已知坑
- SSH 登录用 `duyingfang` 而非 `root`
- `aliyun.tmhcorps.cn` DNS 须设为"仅DNS"（灰云），否则 SSH 被 Cloudflare 拦截
- 飞书卡片回调响应 body 必须包含 `card.type="raw"` 和 `data` 包装层

## 待开发
- [ ] systemd 自动重启（当前重启服务器后需手动拉起）
- [ ] 定时任务：每日/每周风险提醒推送
- [ ] 项目上下文感知（群组绑定项目，自动注入项目信息）
- [ ] 多项目支持
- [ ] 知识库 Web 管理后台
