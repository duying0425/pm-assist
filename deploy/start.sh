#!/bin/bash
# 重启服务（systemd 用户态）
systemctl --user restart pm-assist
systemctl --user status pm-assist
