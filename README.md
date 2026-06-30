# PM Assist (飞书智能项目管理助手)

![Version](https://img.shields.io/badge/version-v1.3.4-blue)
![Python](https://img.shields.io/badge/python-3.12%2B-green)
![Platform](https://img.shields.io/badge/platform-Feishu%20%7C%20Lark-cyan)
![Framework](https://img.shields.io/badge/framework-FastAPI-orange)
![Database](https://img.shields.io/badge/database-SQLite-lightgrey)

`pm-assist` 是专为**东软睿驰自动驾驶团队**打造的内部飞书项目管理 Bot。它能够无缝融入飞书群聊与单聊，通过自然语言交互，帮助项目经理（PM）及团队成员自动化地整理项目纪要、追踪风险与问题、管理待办事项及里程碑。

---

## 🎯 项目定位

项目管理的核心价值在于**将散落在会议、聊天记录、口头沟通中的碎片化信息，转化为结构化、可追踪、能驱动行动的记录**。

传统的管理方式耗时费力，而 `pm-assist` 旨在**自动化信息提取、结构化记录、以及风险追踪**，让 PM 能够把精力集中在决策和沟通本身。其核心目标是“越用越好”，成为项目经理的智能副驾驶。

---

## 🚀 核心特性

1. **自然语言对话与主动信息提取**
   * 在飞书群里 @Bot 或单聊中直接使用自然语言描述会议要点或转发客户反馈。
   * AI 会智能识别出其中的风险、待办、里程碑，并弹出结构化的**确认卡片**，一键确认即可落库。

2. **多项目隔离与智能上下文注入**
   * 针对不同项目进行数据隔离。系统会根据当前会话绑定的项目，自动在 AI 对话中注入四层知识库（预设假设、组织架构、风险列表、计划与待办等），确保 AI 能够基于最新的真实项目上下文进行精准回答。

3. **引用消息支持 (v1.3.3+)**
   * 支持飞书**引用消息**。当 PM 引用他人的消息并 @Bot 时，AI 会自动读取被引用消息的内容、发送人、发送时间，结合 PM 的追问/指令进行深度分析与记录。

4. **AI 定时/手动洗盘 (Nightly Review)**
   * **自动执行模式 (`direct`)** 与 **建议报告模式 (`report_only`)** 自由切换。
   * 扫描全量 facts 和待办，生成建议关闭、归档、合并的建议卡片与数据健康报告，并在每日早上 09:00 推送早报卡片。

5. **AI 项目状态汇报 (Status Report)**
   * 一键生成格式规范的结构化项目状态报告，自动汇总当前的核心风险、里程碑进度与本周待办，开会汇报的完美草稿。

6. **Web 可视化管理后台**
   * 挂载于 `/admin`，基于**飞书 OAuth** 进行登录与权限隔离。
   * 支持可视化的知识库管理、待办编辑、用户审批与角色配置、运行参数在线热改（AI模型、超时限制、洗盘参数等）。

---

## 🛠️ 四层数据架构

`pm-assist` 采用 SQLite 维护了四层递进的数据架构：

* **Layer 0：预设假设 (Assumptions)**
  * 部门/项目公认的背景知识（如“雅迪项目是...”、“客户接口人是...”），对话时自动注入，无需用户反复提及。
* **Layer 1：组织结构 (Org Units)**
  * 缓存团队人员、客户组织与角色关系，@mention 某人时自动缓存姓名与 Feishu open_id。
* **Layer 2：项目事实 (Facts)**
  * 涵盖风险（Risk）、问题（Issue）、阻塞（Blocker）、依赖（Dependency）、决策（Decision）和里程碑（Milestone）。
* **Layer 3：待办事项 (Todos)**
  * 可关联到具体的 Facts（追溯源头风险）或里程碑，实现任务的闭环管理。

---

## 📁 目录结构

```text
pm-assist/
├── main.py             # FastAPI 主入口、Webhook 路由、飞书卡片回调处理
├── ai_client.py        # AI 对话、智能洗盘(Nightly Review)与风险分解逻辑
├── feishu.py           # 飞书 API 封装与所有交互式卡片 Builder (Schema 2.0)
├── db.py               # SQLite 数据库 CRUD 封装与四层上下文组装
├── web_admin.py        # Web 后台 REST API 路由与权限校验
├── config.py           # 环境变量与配置参数加载
├── VERSION             # 当前软件版本号 (语义化：major.feature.patch)
├── CHANGELOG.md        # 详细的开发与发版历史记录
├── PM手册.md           # 面向 PM 用户的飞书命令速查与典型场景使用手册
├── CLAUDE.md           # 面向开发者的系统架构、接口规范与运维手册
├── static/
│   └── admin.html      # 单页管理后台 UI (纯 HTML/CSS/JS)
├── deploy/
│   ├── nginx.conf      # Nginx 反向代理配置模板
│   ├── pm-assist.service # Systemd 用户态服务配置文件
│   ├── setup.sh        # 服务器环境一键初始化脚本
│   └── start.sh        # 服务快捷启动/重启脚本
└── backups/            # 每日自动数据库备份目录 (保留最近 7 份)
```

---

## 🏁 快速上手

### 1. 本地开发环境配置

**前置依赖**：Python 3.12+

```bash
# 1. 克隆/进入项目目录
cd pm-assist

# 2. 创建并激活虚拟环境
python -m venv venv
# Windows 激活方式：
.\venv\Scripts\activate
# Linux/macOS 激活方式：
source venv/bin/activate

# 3. 安装依赖
pip install -r requirements.txt

# 4. 配置环境变量
cp .env.example .env
# 编辑 .env 文件，填入飞书 AppID/Secret、OpenRouter Key、管理员 OpenID 等
```

**运行服务**：
```bash
uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

---

### 2. 生产环境部署 (Ubuntu 24.04 示例)

项目提供了基于**用户态 systemd** 的一键部署流程，无需 root 权限即可平滑运行。

1. **配置 SSH 主机**：在本地 `~/.ssh/config` 中预设目标服务器别名为 `aliyun`。
2. **首次部署初始化**：
   ```bash
   # SSH 登录服务器
   ssh aliyun
   
   # 执行部署脚本进行环境初始化（需要临时 sudo 安装 python3-venv 和 nginx）
   bash ~/pm-assist/deploy/setup.sh
   ```
3. **日常代码更新与重启**：
   ```bash
   # 在本地通过 scp 上传更新的文件（注意：使用正斜杠路径避免 Windows 环境 scp 静默失败）
   # 之后在服务器端运行：
   systemctl --user restart pm-assist
   ```
4. **日志查看**：
   ```bash
   journalctl --user -u pm-assist -f
   # 或者是查看追加日志文件
   tail -f ~/pm-assist/logs/app.log
   ```

---

## 📖 相关文档链接

为了更深入地了解此项目，请参考以下专用文档：

* 📘 **[PM操作手册](file:///C:/Users/duyin/Desktop/pm-assist/PM%E6%89%8B%E5%86%8C.md)**：面向 PM 及团队成员，包含申请加入、日常场景（如何记笔记/风险/待办）、所有快捷菜单及群聊交互命令的速查。
* 🛠️ **[开发者指南 (CLAUDE.md)](file:///C:/Users/duyin/Desktop/pm-assist/CLAUDE.md)**：面向维护与开发者，包含飞书事件订阅（`im.message.receive_v1`等）、卡片回调响应协议、数据库详细 Schema 设计、AI 提示词注入顺序等底层逻辑。
* 📜 **[版本变更履历](file:///C:/Users/duyin/Desktop/pm-assist/CHANGELOG.md)**：追踪自项目启动以来的所有特性升级、Bug 修复和部署履历。

---

*如有任何问题或需要新增特性支持，请联系管理员：杜莹芳。*
