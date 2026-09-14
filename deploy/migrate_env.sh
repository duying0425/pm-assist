#!/bin/bash
set -e
cd ~/pm-assist.new

# 修复 SESSION_SECRE 拼写错误（缺 T）——修复后管理员登录态可跨重启保持
sed -i 's/^SESSION_SECRE=/SESSION_SECRET=/' .env

# 生成密钥并追加 HUB 配置（密钥不回显）
HUB_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
CL_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")

# Hub 接入注册与全局 scope 写入 hub_config.json（热加载，改动免重启）
cp hub_config.json.example hub_config.json
python3 - "$CL_SECRET" <<'PYEOF'
import json, sys
secret = sys.argv[1]
with open("hub_config.json", encoding="utf-8") as f:
    cfg = json.load(f)
cfg["clients"][0]["secret"] = secret
with open("hub_config.json", "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
PYEOF

cat >> .env <<EOF

# ===== 认证跳板 Hub（2026-09-11 部署）=====
HUB_SECRET=${HUB_SECRET}
EOF

echo "ENV_UPDATED"
grep -E '^(SESSION_SECRET|HUB_SECRET)=' .env | sed 's/=.*/=<set>/'
