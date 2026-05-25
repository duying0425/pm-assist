#!/bin/bash
# 在阿里云服务器上执行一次，完成环境初始化
set -e

echo "=== 安装依赖 ==="
apt-get update -y
apt-get install -y python3 python3-pip python3-venv nginx

echo "=== 创建应用目录 ==="
mkdir -p /opt/pm-assist
cd /opt/pm-assist

echo "=== 创建虚拟环境 ==="
python3 -m venv venv
source venv/bin/activate

echo "=== 安装 Python 依赖 ==="
pip install -r requirements.txt

echo "=== 配置 Nginx ==="
cp deploy/nginx.conf /etc/nginx/sites-available/pm-assist
ln -sf /etc/nginx/sites-available/pm-assist /etc/nginx/sites-enabled/pm-assist
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

echo "=== 注册系统服务 ==="
cp deploy/pm-assist.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable pm-assist

echo ""
echo "=== 完成！下一步 ==="
echo "1. 编辑 /opt/pm-assist/.env 填入你的 Key"
echo "2. python seed.py  （初始化知识库）"
echo "3. systemctl start pm-assist"
echo "4. systemctl status pm-assist  （验证运行状态）"
