#!/bin/bash
# 在服务器上以普通用户身份执行一次，完成环境初始化
# 用法：ssh aliyun 后，bash ~/pm-assist/deploy/setup.sh
set -e

echo "=== 安装系统依赖（需要 sudo）==="
sudo apt-get update -y
sudo apt-get install -y python3 python3-pip python3-venv nginx

echo "=== 创建虚拟环境 ==="
cd ~/pm-assist
python3 -m venv venv
source venv/bin/activate

echo "=== 安装 Python 依赖 ==="
pip install -r requirements.txt

echo "=== 配置 Nginx ==="
sudo cp deploy/nginx.conf /etc/nginx/conf.d/pm-assist.conf
sudo nginx -t && sudo systemctl reload nginx

echo "=== 注册用户态 systemd 服务 ==="
mkdir -p ~/.config/systemd/user
cp deploy/pm-assist.service ~/.config/systemd/user/pm-assist.service
systemctl --user daemon-reload
systemctl --user enable pm-assist
loginctl enable-linger "$USER"   # 确保用户服务开机自启（无需登录）

echo ""
echo "=== 完成！下一步 ==="
echo "1. 编辑 ~/pm-assist/.env 填入各项 Key（参考 .env.example）"
echo "2. mkdir -p ~/pm-assist/logs"
echo "3. systemctl --user start pm-assist"
echo "4. systemctl --user status pm-assist  （验证运行状态）"
echo "5. journalctl --user -u pm-assist -f  （实时查看日志）"
