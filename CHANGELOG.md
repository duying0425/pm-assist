# pm-assist 变更历史

## 版本部署记录

| 版本 | 状态 | 主要变更 |
|------|------|---------|
| v0.7.6 | 已部署 | 飞书快捷菜单 + /risk 独立命令 + 移除 PRIMARY_ADMIN_OPEN_ID |
| v0.7.7 | 已部署 | /risk show + /todo show/update + approve/reject 通知幂等 + member 权限收敛 + TTL 延长至30分钟 + 帮助文本完整化 |
| v0.7.8 | 已部署 | 飞书消息全卡片化 + /schedule 里程碑命令 + 删除 _strip_md |
| v0.8.0 | 已部署 | 按项目早报卡片+PM推送、风险/待办列表可点击详情、AI标题加粗渲染修复、AI澄清问题卡片 |
| v0.8.1 | 已部署 | 飞书卡片全面升级 schema 2.0；"思考中"卡片原地更新修复：PATCH 端点从 /body 改为 /messages/{id}，注入 update_multi:true |
| v0.8.2 | 已部署 | 管理员 /join 直接绑定项目；无绑定时查全量跨项目上下文；AI 注入用户列表；sender_info 显示管理员项目 |
| v0.8.3 | 已部署 | 保留待确认状态；文字"保存/确认/跳过"可消费 pending；AI 建议命令转确认按钮后再写库 |
| v0.8.4 | 已部署 | 补全 AI 命令集含 risk owner/close/reopen；修复卡片发送失败静默问题，新增 400 响应日志与失败兜底提示 |
| v0.8.5 | 已部署 | 新增 AI 执行口吻防误导：无命令时禁止"已确认/已更新/已保存/已执行"语气，统一提示"仅建议未落库" |
| v0.9.0 | 已部署 | AI 建议整合到主回复：取消单独 extract_facts/extract_todo_intent 调用，AI 在回复中内嵌 ===SUGGESTIONS=== 块；统一建议确认卡片；旧 command_*/save_one/save_all/todo 卡片全部清理 |
| v0.9.1 | 本地完成 | 洗盘卡片修复+优化：合并/清洗建议统一进入 AI 建议卡片；修复快捷菜单 open_id 当 chat_id 发送失败 bug；洗盘报告章节标题改用 ## |
| v0.9.4 | 本地完成 | system prompt 修复：移除"系统会自动弹出确认卡片"误导语、禁止 AI 编造计次、明确 SUGGESTIONS 块触发条件；/clear 彻底清除所有 pending 类型 |
| v0.9.6 | 本地完成 | 提示词系统优化：_ROLE 新增"不应生成建议块"负面列表；_REVIEW_PROMPT_TPL 修复 retry prompt "十节" bug |
| v0.9.8 | 已部署 | 修复定时早报不发建议卡片：_morning_review_and_report 发完早报后调用 _broadcast_review_suggestions |
| v0.9.9 | 已部署 | 洗盘下放 PM 层级：/review run PM 和管理员均可用；早报 PM 卡片加入完整洗盘报告；新增 _broadcast_review_suggestions 多人广播 |
| v1.0.0 | 已部署 | 洗盘报告优化：六节纯自然语言+两节机器可读 JSON；direct 模式从 action_candidates JSON 执行；新增 view_morning_report 快捷菜单；AI 对话超时升至 90s |
| v1.0.1 | 已部署 | 里程碑列表详情按钮；风险/待办/里程碑详情卡片加后台编辑链接；admin_users 人员信息卡片按角色分级展示；/admin stats 加会话统计；修复群聊 @Bot 不响应问题 |
| v1.0.2 | 已部署 | Web 后台飞书 OAuth 认证（super_admin / pm 可访问）；DB 表补充 chat_bindings / user_chat_ids；会话统计 API |
| v1.0.3 | 已部署 | Web 后台完整 URL 路由：tab 切换/编辑框同步地址栏，支持浏览器前进/后退和深链直达 |
| v1.0.4 | 已部署 | /join 支持申请创建新项目：项目不存在时 pm 角色发起创建审批，管理员卡片批准后自动建项目并绑定；super_admin 直接创建无需审批 |
| v1.0.5 | 已部署 | 修复多项目上下文污染：决策/相关方按项目过滤；单聊跟人走（跳过 chat_bindings）；群聊首次 @Bot 自动绑定到 PM/管理员所在项目 |
| v1.0.6 | 已部署 | 修复单聊项目绑定：Feishu p2p 与群聊 chat_id 同为 oc_ 开头，改用 chat_type 字段区分；单聊彻底跳过 chat_bindings |
| v1.0.7 | 已部署 | Web 后台头部显示版本号（/api/version 接口 + header-version 展示）|
| v1.0.8 | 已部署 | 修复群聊 @Bot mention 未剥除导致命令解析错误；群聊切换项目绑定时自动清空对话历史并提示 |
| v1.0.9 | 已部署 | 修复卡片"全部保存"报错 -356：_card() 和 _resp() 的 config 补充 update_multi:true，满足 schema 2.0 卡片更新要求 |
| v1.0.10 | 已部署 | 优化 system prompt 数据边界说明：明确上下文才是事实，历史对话建议不代表已入库，消除 AI 上下文污染误判 |
| v1.0.11 | 已部署 | 修复定时早报只发管理员问题：sqlite3.Row 不支持 .get() 导致 PM 循环崩溃被吞；重构早报发送逻辑为每个项目一张卡，admin+NOTIFY+该项目PM收同一张 |
| v1.1.0 | 已部署 | 洗盘按项目隔离：每次洗盘只处理一个项目数据，报告按项目分开存储；admin 遍历所有活跃项目逐一洗，PM 只洗自己绑定项目 |
| v1.1.1 | 已部署 | Web 后台管理员接口加权限隔离：/api/ 路由校验飞书 OAuth session，非 super_admin 返回 403 |
| v1.2.0 | 已部署 | AI 项目状态汇报独立成命令（/status run + run_status 菜单）；早报卡片改用 AI 生成状态替代静态风险列表；洗盘→状态顺序执行；notify.py 删除内联至 main.py；deploy 脚本更新为用户态 systemd；.env.example 补全 NOTIFY_OPEN_IDS/SESSION_SECRET/ADMIN_REDIRECT_URI |
| v1.2.1 | 本地完成 | bug 修复：1) /status run 崩溃——list_users() 返回 sqlite3.Row 不支持 .get()，改为统一返回 dict；2) AI 洗盘新增 todo project 丢失——_apply_review_commands/_apply_review_action/_collect_review_suggestion_items 全链路补传 project 参数；3) 存量 project="默认" 的 todo 直接修正到"雅迪"；4) _enrich_suggestions 兜底从 "yadi" 改为 "" 避免写入不存在的项目名 |

## 已实现功能清单

1. **飞书 Bot 对话**：@Bot 发消息，结合四层知识上下文 + 对话历史用 AI 回答
2. **知识库管理**：`/admin list/add/update/enable/disable/delete`
3. **风险管理**：`/risk list/close/reopen/owner/add`（PM 和管理员均可用）
4. **统一信息管理**：`/admin fact list/show/update/archive/delete/add`
5. **预设假设管理**：`/admin assumption list/show/add/update/archive/delete`
6. **组织结构管理**：`/admin org list/add`
7. **快速记录**：`/note [内容]` 直接存入知识库
8. **AI 智能提取**：用户发消息后台自动提取，每个独立事项单独一条
9. **相似性去重**：提取时匹配已有条目，卡片上区分"新增"和"追加 #ID"
10. **交互卡片确认**：逐条确认，支持"全部保存"/"跳过"；卡片标题动态显示进度
11. **对话历史清除**：`/clear` 命令
12. **定时 AI 洗盘 + 早报推送**：每天 09:00 执行洗盘并立即推送（APScheduler 内置）；支持 report_only/direct 两种模式
13. **待办事项系统**：`/todo` 命令；支持关联 risk 追溯、挂载里程碑；AI 上下文带 todo 信息
14. **AI 分解 risk**：`/admin fact decompose [ID]` 自动将风险拆解为可执行 todo 列表
15. **时间戳上下文**：所有 facts 条目在 AI 上下文中带记录/更新日期
16. **AI 语言约束**：不再说"已记录/已保存"等误导性语言
17. **@mention 缓存**：消息中 @某人 时自动缓存姓名↔open_id 到 org_units 表
18. **版本管理**：`VERSION` 文件 + `/version` 命令
19. **注册与权限系统**：用户自主注册、管理员审批、三角色差异化 AI 上下文、说话人身份注入
20. **飞书 post 富文本消息支持**：同时处理 text 和 post 两种消息类型，解析 at/text/a 节点
21. **"思考中"占位消息**：AI 处理期间先发占位消息，回复就绪后原地 PATCH 更新，90 秒超时兜底
22. **待办意图自动提取**：对话中说"加个待办/提醒"时 AI 自动识别并弹出确认卡片
23. **Web 管理后台**：`https://pm.tmhcorps.cn/admin/`，可视化管理知识库/待办/用户/预设
24. **数据库索引优化**：facts/todos/conversations/processed_events 核心查询字段加索引
25. **AI 洗盘配置与手动执行**：Web 后台切换洗盘模式；飞书 `/review run` 立即洗盘
26. **AI 合并建议卡片确认**：洗盘报告合并候选生成飞书卡片，点击后执行合入并归档
27. **风险/待办清洗卡片确认**：洗盘风险关闭/归档/todo完成/取消建议生成飞书卡片
28. **注册审批卡片显示真实姓名**：调用飞书 contact API 查询申请人姓名
29. **项目绑定管理**：管理员修改用户项目绑定；用户 `/leave` 自助退出
30. **`/start` 新用户入口**：原 /register 改名
31. **飞书机器人快捷菜单支持**：处理 `application.bot.menu_v6` 事件
32. **`/risk` 独立命令**：从 `/admin risk` 下放至根级，PM 和管理员均可使用
33. **`/risk show`、`/todo show/update`**：详情查看和字段更新
34. **审批命令通知 + 幂等**：approve/reject 发飞书通知，防多管理员重复操作
35. **飞书消息统一卡片化**：所有回复走 send_reply() 统一出口；AI 回复 lark_md 卡片
36. **`/schedule` 里程碑命令**：member/PM/管理员均可用；逾期自动标注 ⚠️
37. **飞书卡片全面升级 schema 2.0**：所有卡片添加 schema:2.0，AI 回复支持完整 Markdown
38. **管理员项目绑定与全量 AI 上下文**：/join 对管理员直接生效；无绑定时查全量
39. **AI 建议整合到主回复**：取消独立 extract_facts/extract_todo_intent；AI 内嵌 SUGGESTIONS 块
40. **统一 AI 建议确认卡片**：按类型分组，支持详情查看/按条保存跳过/全部操作
41. **洗盘下放 PM 层级**：/review run PM 和管理员均可用；早报 PM 卡片含完整洗盘报告
42. **洗盘报告优化与模式分离**：六节纯自然语言+两节机器可读 JSON；report_only/direct 模式独立
43. **`view_morning_report` 快捷菜单**：查看最新早报，不触发 AI 调用，读 DB 缓存即时返回
44. **AI 澄清问题卡片**：AI 解析到 ===CLARIFY=== 块时弹出选项卡片，点击后继续作答
45. **Web 后台版本号展示**：`/api/version` 接口读取 VERSION 文件，头部实时显示当前版本号
46. **群聊 Bot @mention 正确剥除**：改用 BOT_OPEN_ID 比对替代失效的 is_bot 字段，@Bot 前置/后置均可正确解析命令
47. **切换项目绑定自动清空会话历史**：`/admin project bind` 成功后自动清除群聊对话历史，避免旧项目上下文污染
