# Hub 后台获取用户 Access Token

后台接口由 `aliyun-cf` 上的 `pm-hub` 提供，地址为 `https://pm.tmhcorps.cn`。
Hub 保存已授权用户的凭证，后台调用不需要 SSH，不需要新建 client，也不依赖 NAS 在线。

## 一次请求获取 token

```http
POST /hub/api/user-token
Authorization: Bearer <HUB_ADMIN_API_KEY>
Content-Type: application/json

{}
```

有唯一管理员授权时，空 JSON 对象即可取 token。有效期不足 5 分钟时自动续期，其他情况返回当前有效 token。
立即生成新 token：发送 `{"force_refresh":true}`。接口返回 `user_access_token`，供飞书 API 的 `Authorization: Bearer <user_access_token>` 请求头使用。

| 请求参数 | 类型 | 说明 |
|---|---|---|
| `client_id` | string，可选 | 授权来源，例如 `chatlogger` |
| `open_id` | string，可选 | 指定 `ADMIN_OPEN_IDS` 中的管理员账号 |
| `force_refresh` | boolean，可选 | 默认为 `false`；字符串不接受 |

响应结构示例（凭证和时间为占位示例）：

```json
{
  "user_access_token": "u-...",
  "token_type": "Bearer",
  "expires_in": 7199,
  "expires_at": "2026-10-09T10:00:00+00:00",
  "client_id": "chatlogger",
  "open_id": "ou_...",
  "name": "用户姓名",
  "refreshed": true
}
```

`expires_at` 为 UTC 的 ISO 8601 时间，`expires_in` 为剩余秒数。后台接口不下发 refresh_token。
所有 `/hub/api/` 响应均带 `Cache-Control: no-store` 和 `Pragma: no-cache`。

PowerShell 示例：

```powershell
$headers = @{ Authorization = "Bearer $env:HUB_ADMIN_API_KEY" }
$result = Invoke-RestMethod -Method Post `
  -Uri 'https://pm.tmhcorps.cn/hub/api/user-token' `
  -Headers $headers -ContentType 'application/json' -Body '{}'
$result.user_access_token
```

## 授权与账号选择

- 首次仍需要用户在接入应用中登录飞书，完成 OAuth 授权；后台 API 不能跳过用户授权。
- Hub 在 `/hub/api/token` 交付授权结果时保存凭证。旧客户端在 `/hub/api/refresh` 成功刷新后也会登记授权。
- 存量 `chatlogger` 凭证可在部署时从其数据库安全导入一次，无需用户重新登录。
- 后台只允许获取 `ADMIN_OPEN_IDS` 中的用户，且来源 client 必须仍在 `hub_config.json` 中注册。
- 多份授权时返回 `409 account_required`，根据账号信息指定 `client_id`、`open_id`。
- 账号元数据接口：`POST /hub/api/user-token/accounts`，使用相同后台 API Key，不返回任何 token。
- 授权失效返回 `409 authorization_required` 和授权入口；用户应在对应接入应用中重新登录。

## 错误响应

| HTTP 状态 | 含义 |
|---|---|
| 401 | 后台 API Key 缺少或错误 |
| 403 | 指定用户未配置为管理员 |
| 404 | client 已删除或未注册 |
| 409 | 需要重新授权或选择账号；看 `detail.code` |
| 422 | 参数类型错误 |
| 502 | 飞书请求暂时失败；保留现有凭证，可稍后重试 |
| 503 | API Key 或管理员列表未配置，后台功能禁用 |

## 配置与维护

`.env` 新增 `HUB_ADMIN_API_KEY=<随机长密钥>`；可选 `HUB_TOKEN_DB_PATH`，默认 `data/hub_tokens.sqlite3`。
生成 API Key：`python -c "import secrets; print(secrets.token_urlsafe(32))"`。
修改环境变量后执行 `systemctl --user restart pm-hub`；`hub_config.json` 仍热加载。

数据库权限为 `0600`，不进入 Git。凭证数据需随服务备份，使用 SQLite backup API，避免直接复制运行中的 WAL 数据库文件。
保持单个 uvicorn 进程，勿增加 `--workers`：授权码和续期锁在进程内；同一用户的取 token、客户端刷新和新授权交付串行执行。

后台续期后，原客户端拿着旧刷新凭证调用 `/hub/api/refresh` 时，Hub 根据按客户端隔离的凭证摘要返回当前凭证，避免重复消耗一次性刷新凭证；旧摘要保留至相应到期时间。
原 `/hub/api/token`、`/hub/api/refresh`、`/hub/api/tenant-token` 的参数和响应字段保持兼容。
日志记录 client、open_id 和结果，不记录 access_token、refresh_token 或 API Key。

测试：`python -m unittest discover -s tests -p 'test_*.py'`。覆盖鉴权、管理员范围、续期、并发、旧客户端兼容、授权码交付、重启保留和 Unix 文件权限。
