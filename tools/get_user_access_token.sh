#!/usr/bin/env bash
# pm-assist Hub：在 Aliyun 服务器上一条命令获取飞书 user_access_token。
#
# 使用方法（以 duyingfang 用户运行）：
#   ~/get_user_access_token.sh                  获取可用 token，保存为 ~/user_access_token.json
#   ~/get_user_access_token.sh --force-refresh  立即更新 token 并保存
#   ~/get_user_access_token.sh --verify         获取后调用飞书 user_info，核验用户身份
#   ~/get_user_access_token.sh --print-token    只在标准输出打印 token，方便复制或其他脚本读取
#   ~/get_user_access_token.sh --help           查看所有选项，包括指定账号和输出文件
#
# 默认访问本机 pm-hub：http://127.0.0.1:8002/hub/api/user-token。
# 从 ~/pm-assist/.env 读取 HUB_ADMIN_API_KEY；脚本本身不存放密钥。
# 使用 ~/pm-assist/venv 中现有 Python 和 python-dotenv，无需另装依赖。
# 迁移项目目录时，可设置 PM_ASSIST_DIR=/新路径/pm-assist 后运行。
# 保存的 JSON 含 user_access_token、账号、到期时间，不含 refresh_token；文件权限为 0600。
# 默认只显示账号、到期时间和文件位置；--print-token 会输出完整凭证，勿放入公开日志。
# 有效期不足 5 分钟由 Hub 自动更新；--force-refresh 要求立即更新。
# 首次仍需在 chatlogger 等接入应用中登录飞书；刷新授权失效后也需重新登录。
# 多个授权账号时，用 --client-id 和 --open-id 选择；仅允许已配置管理员的授权。
# 更新或验证失败时不会覆盖之前保存的文件。
# 仓库维护源文件：~/pm-assist/tools/get_user_access_token.sh。
# 同步到用户目录：install -m 700 ~/pm-assist/tools/get_user_access_token.sh ~/get_user_access_token.sh

set -euo pipefail
project_dir="${PM_ASSIST_DIR:-$HOME/pm-assist}"
python_bin="$project_dir/venv/bin/python"
if [[ ! -x "$python_bin" ]]; then
    printf '未找到项目 Python 环境：%s\n请检查 PM_ASSIST_DIR 或 ~/pm-assist/venv。\n' "$python_bin" >&2
    exit 1
fi
export PM_ASSIST_DIR="$project_dir"
exec "$python_bin" - "$@" <<'PYTHON'
import argparse
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from dotenv import dotenv_values


def request_json(url, token, payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="GET" if payload is None else "POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if payload is None:
            raise RuntimeError(f"飞书身份验证失败（HTTP {exc.code}），请重新获取 token。") from None
        hints = {
            400: "飞书拒绝更新，请检查应用配置或重新登录飞书。",
            401: "请检查项目 .env 中的 HUB_ADMIN_API_KEY。",
            403: "该用户未配置为 Hub 管理员。",
            404: "接入应用未注册，请检查 --client-id。",
            409: "请重新登录飞书，或用 --client-id 和 --open-id 选择授权账号。",
            422: "请求参数有误，请查看 --help。",
            502: "飞书服务暂时不可用，请稍后重试。",
            503: "Hub 的后台 API Key 或管理员列表尚未配置。",
        }
        raise RuntimeError(f"Hub 请求失败（HTTP {exc.code}）：{hints.get(exc.code, '请检查 pm-hub 服务。')}") from None
    except (urllib.error.URLError, TimeoutError):
        service = "飞书" if payload is None else "本机 pm-hub"
        raise RuntimeError(f"无法连接{service}，请检查服务和网络后重试。") from None


def save_private_json(path, result):
    # 先写仅当前用户可读的临时文件，再原子替换，避免凭证文件写到一半。
    fd, temporary = tempfile.mkstemp(prefix=".user-token-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(
        prog="get_user_access_token.sh",
        description="从本机 pm-assist Hub 获取已授权管理员的飞书 user_access_token。",
        epilog="密钥读取：$PM_ASSIST_DIR/.env（默认 ~/pm-assist/.env）。首次或授权失效时，先在接入应用中登录飞书。",
    )
    parser.add_argument("--force-refresh", action="store_true", help="立即更新 token；默认获取当前有效 token")
    parser.add_argument("--verify", action="store_true", help="调用飞书 user_info 核验 token 和用户身份")
    parser.add_argument("--print-token", action="store_true", help="标准输出仅打印完整 token，仍保存 JSON 文件")
    parser.add_argument("--client-id", help="指定接入应用，例如 chatlogger")
    parser.add_argument("--open-id", help="指定已授权管理员的飞书 open_id")
    parser.add_argument("--output", type=Path, default=Path.home() / "user_access_token.json", help="JSON 保存位置，默认 ~/user_access_token.json")
    args = parser.parse_args()
    env_path = Path(os.environ["PM_ASSIST_DIR"]) / ".env"
    if not env_path.is_file():
        raise RuntimeError(f"未找到鉴权配置：{env_path}")
    api_key = dotenv_values(env_path).get("HUB_ADMIN_API_KEY")
    if not api_key:
        raise RuntimeError("项目 .env 未配置 HUB_ADMIN_API_KEY。")
    output_path = args.output.expanduser().absolute()
    if not output_path.parent.is_dir():
        raise RuntimeError(f"输出目录不存在：{output_path.parent}")
    payload = {"force_refresh": args.force_refresh}
    for field in ("client_id", "open_id"):
        value = getattr(args, field)
        if value:
            payload[field] = value
    result = request_json("http://127.0.0.1:8002/hub/api/user-token", api_key, payload)
    if not isinstance(result, dict) or not result.get("user_access_token") or result.get("expires_in", 0) <= 0:
        raise RuntimeError("Hub 未返回有效的 user_access_token，原文件已保留。")
    if args.verify:
        profile = request_json("https://open.feishu.cn/open-apis/authen/v1/user_info", result["user_access_token"])
        if not isinstance(profile, dict) or profile.get("code") != 0 or not isinstance(profile.get("data"), dict) or profile["data"].get("open_id") != result.get("open_id"):
            raise RuntimeError("飞书身份验证失败：token 不可用或用户不匹配，原文件已保留。")
        result["verified"] = True
    expires_at = datetime.fromisoformat(result["expires_at"]).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    save_private_json(output_path, result)
    if args.print_token:
        print(result["user_access_token"])
    else:
        print(f"账号：{result.get('name', result.get('open_id', ''))}")
        print(f"到期时间：{expires_at}")
        print(f"本次已更新：{'是' if result.get('refreshed') else '否'}")
        print(f"已保存：{output_path}（权限 0600）")
        if args.verify:
            print("飞书身份验证通过：这是该账号可用的 user_access_token。")


try:
    main()
except (RuntimeError, OSError, ValueError, KeyError, TypeError) as exc:
    # 不输出请求头、响应正文或 traceback，避免错误日志带出凭证。
    message = str(exc) if isinstance(exc, RuntimeError) else "读取配置、解析响应或保存文件失败，请检查网络、目录和权限。"
    print(f"失败：{message}", file=sys.stderr)
    sys.exit(1)
PYTHON
