#!/bin/bash
set -e
cd ~/pm-assist.new

# 修复 SESSION_SECRE 拼写错误（缺 T）——修复后管理员登录态可跨重启保持
sed -i 's/^SESSION_SECRE=/SESSION_SECRET=/' .env

# 生成密钥并追加 HUB 配置（密钥不回显）
HUB_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
CL_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")

cat >> .env <<EOF

# ===== 认证跳板 Hub（2026-09-11 部署）=====
HUB_SECRET=${HUB_SECRET}
HUB_CLIENTS=[{"id":"chatlogger","secret":"${CL_SECRET}","redirect_uri":"https://chatlogger.tmhcorps.cn/auth/callback","scopes":"im:message:readonly im:message.group_msg:get_as_user bitable:app im:chat:readonly offline_access docx:document:readonly wiki:wiki:readonly drive:drive:readonly"}]
EOF

echo "ENV_UPDATED"
grep -E '^(SESSION_SECRET|HUB_SECRET|HUB_CLIENTS)=' .env | sed 's/=.*/=<set>/'
