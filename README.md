# 文件传输助手

一个可自托管的文件和短文本传输助手，适合放在 VPS 上。手机、电脑、平板用浏览器访问；Codex、终端和脚本通过同一套 HTTP API 或 `client/fta.py` 操作。

## 为什么建议放 VPS

放 VPS 更合适。你的电脑关机后，手机和平板仍然可以上传或取回文件；Codex 也能通过公网 HTTPS 地址直接访问这个服务。电脑本机只适合作为临时局域网传输点。

## 安全设计

- API 读写必须带 `Authorization: Bearer <FTA_TOKEN>`。
- Web 页面通过 `/api/login` 登录，服务端返回 `HttpOnly` session cookie；令牌不再长期保存到 `localStorage`。
- 设置 `FTA_WEB_AUTH_MODE=totp` 和 `FTA_TOTP_SECRET` 后，网页登录只需要 Google Authenticator 等应用生成的 6 位 TOTP 验证码。
- CLI/Codex 仍可使用 `Authorization: Bearer <FTA_TOKEN>` 或 `X-FTA-Token: <FTA_TOKEN>`。
- 上传文件名会清理，文件保存到数据目录，不会按用户传入路径写入。
- 上传大小默认限制为 512 MB，可用 `FTA_MAX_UPLOAD_MB` 调整。
- 删除记录时会删除 SQLite 记录和磁盘文件。
- 公网部署时必须放在 HTTPS 反向代理后面。

## 本地运行

```powershell
$env:FTA_TOKEN = "change-this-to-a-long-random-token"
python -m transfer_assistant.server --host 127.0.0.1 --port 8787 --data-dir .\.data
```

打开 `http://127.0.0.1:8787`，使用同一个令牌登录。

## VPS Docker 部署

```bash
cp .env.example .env
python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
# 把输出写入 .env 的 FTA_TOKEN
docker compose up -d --build
```

启用 Google Authenticator：

```bash
python -m transfer_assistant.server --generate-totp-secret
# 把输出写入 .env 的 FTA_TOTP_SECRET
```

在 Google Authenticator 中选择手动输入 setup key：

- Account name: `Transfer Assistant`
- Key: `.env` 里的 `FTA_TOTP_SECRET`
- Type: Time based

网页登录模式：

```bash
FTA_WEB_AUTH_MODE=token       # 只用登录令牌
FTA_WEB_AUTH_MODE=token_totp  # 登录令牌 + Google Authenticator
FTA_WEB_AUTH_MODE=totp        # 只用 Google Authenticator
```

生产环境当前推荐 `FTA_WEB_AUTH_MODE=totp`。CLI/Codex 继续使用独立的 `FTA_TOKEN`。

如果使用 Linux VPS 上的绑定目录保存数据，第一次启动前建议设置目录权限：

```bash
mkdir -p data
chown -R 1000:1000 data
```

推荐用 Caddy 提供 HTTPS：

```caddyfile
files.example.com {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8787
}
```

然后访问 `https://files.example.com`。

如果 VPS 已经接入 Tailscale，也可以不暴露公网端口：

```bash
docker compose up -d --build
tailscale serve --bg --yes 8787
```

然后在同一个 tailnet 内访问 `https://<machine>.<tailnet>.ts.net/`。如果客户端没有启用 Tailscale DNS，可以再加一个 tailnet-only HTTP 入口：

```bash
tailscale serve --bg --yes --http=8787 8787
```

如果要直接放到公网，可以用 Tailscale Funnel 提供公网 HTTPS：

```bash
tailscale funnel --bg --yes 8787
```

Funnel 开启后，公网访问地址通常是 `https://<machine>.<tailnet>.ts.net/`。任何人能打开页面，但必须使用 `FTA_TOKEN` 登录后才能读写文件和文本。

## GitHub Pages 前端

GitHub Pages 只能托管静态文件，不能运行上传 API 或保存文件。正式 Pages 方案是：

- `docs/`：GitHub Pages 静态前端。
- VPS：继续运行 API、认证和文件存储。
- `docs/static/config.js`：配置 Pages 前端连接的 VPS API 地址。

Pages 前端使用当前标签页内的登录令牌调用 API，不把令牌长期写入 `localStorage`。VPS 后端需要允许 Pages 来源：

```bash
FTA_CORS_ORIGINS=https://snappython.github.io
```

## Codex / 终端使用

PowerShell：

```powershell
$env:FTA_URL = "https://files.example.com"
$env:FTA_TOKEN = "your-long-token"
python .\client\fta.py text "一段要同步的文本"
python .\client\fta.py upload .\report.pdf
python .\client\fta.py list
python .\client\fta.py cat <item-id>
python .\client\fta.py download <item-id> --output .
python .\client\fta.py delete <item-id> --yes
```

Bash：

```bash
export FTA_URL=https://files.example.com
export FTA_TOKEN=your-long-token
python3 client/fta.py text "hello from codex"
python3 client/fta.py upload ./report.pdf
python3 client/fta.py list
```

## API

```bash
curl -H "Authorization: Bearer $FTA_TOKEN" "$FTA_URL/api/items"
curl -H "Authorization: Bearer $FTA_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text":"hello","title":"note"}' \
  "$FTA_URL/api/text"
curl -H "Authorization: Bearer $FTA_TOKEN" \
  -H "Content-Type: application/octet-stream" \
  --data-binary @report.pdf \
  "$FTA_URL/api/files?name=report.pdf"
```

## 备份

备份 `data/` 目录即可，里面包含 SQLite 元数据和上传文件。
