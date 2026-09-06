# Transfer Assistant

A self-hosted file and short-text transfer service for a VPS. Use a browser on
phones, computers and tablets, or the same HTTP API through `client/fta.py`.
The interface, status messages and date formatting use English. Uploaded files,
filenames and user text retain their original content and language.

## Why run it on a VPS

A VPS keeps uploads and downloads available when your computer is off. A local
computer is useful as a temporary LAN transfer endpoint.

## Security

- API reads and writes require `Authorization: Bearer <FTA_TOKEN>`.
- `/api/login` issues a web session valid for 30 days. The Pages client stores
  that limited session, not `FTA_TOKEN`.
- With `FTA_WEB_AUTH_MODE=totp` and `FTA_TOTP_SECRET`, web sign-in accepts a
  six-digit code from Google Authenticator or another TOTP app.
- Three consecutive failed sign-ins lock access for 15 minutes by default;
  configure `FTA_LOGIN_LOCK_FAILURES` and `FTA_LOGIN_LOCK_SECONDS` to change this.
- CLI clients can use a Bearer token or `X-FTA-Token: <FTA_TOKEN>`.
- Upload names are sanitized; client paths cannot choose filesystem locations.
- The default upload limit is 512 MB, configurable with `FTA_MAX_UPLOAD_MB`.
- Deleting an item removes its SQLite record and file.
- Public deployments require an HTTPS reverse proxy.

## Run locally

```powershell
$env:FTA_TOKEN = "change-this-to-a-long-random-token"
python -m transfer_assistant.server --host 127.0.0.1 --port 8787 --data-dir .\.data
```

Open `http://127.0.0.1:8787` and sign in with that token.

## VPS Docker deployment

For this stack, release application changes through `vps-stack` and its guarded
deployment workflow. The following commands describe standalone setup:

```bash
cp .env.example .env
python3 - <<'PYTHON'
import secrets
print(secrets.token_urlsafe(32))
PYTHON
# Store the generated value as FTA_TOKEN in .env; never commit it.
docker compose up -d --build
```

Generate an authenticator secret:

```bash
python -m transfer_assistant.server --generate-totp-secret
# Store the output as FTA_TOTP_SECRET in .env.
```

In the authenticator app, add a time-based account named `Transfer Assistant`
using that setup key. Choose a web authentication mode:

```bash
FTA_WEB_AUTH_MODE=token       # Access token only
FTA_WEB_AUTH_MODE=token_totp  # Token and authenticator code
FTA_WEB_AUTH_MODE=totp        # Authenticator code only
```

The production configuration uses TOTP for web access. CLI clients continue
using the separate `FTA_TOKEN`. The default lockout configuration is:

```bash
FTA_LOGIN_LOCK_FAILURES=3
FTA_LOGIN_LOCK_SECONDS=900
```

Prepare a Linux bind-mounted data directory before the first start:

```bash
mkdir -p data
chown -R 1000:1000 data
```

An example Caddy HTTPS entry:

```caddyfile
files.example.com {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8787
}
```

Then open `https://files.example.com`. A standalone deployment already using
Tailscale can expose a tailnet-only endpoint:

```bash
docker compose up -d --build
tailscale serve --bg --yes 8787
```

Open `https://<machine>.<tailnet>.ts.net/` from the tailnet. Where Tailscale DNS
is unavailable, a tailnet-only HTTP endpoint can be added:

```bash
tailscale serve --bg --yes --http=8787 8787
```

A standalone public endpoint can instead use Funnel:

```bash
tailscale funnel --bg --yes 8787
```

The public page remains protected by the configured web authentication method
for all file and text reads/writes. Do not change the managed stack's exposure
or authentication topology through these standalone examples.

## GitHub Pages client

Pages hosts static files, not the upload API or storage:

- `docs/` contains the Pages frontend.
- The VPS runs the API, authentication and file storage.
- `docs/static/config.js` sets the API address for the Pages client.

The client uses the limited web session and never writes `FTA_TOKEN` into
`localStorage`. The API must allow the Pages origin:

```bash
FTA_CORS_ORIGINS=https://snappython.github.io
```

## Terminal use

PowerShell:

```powershell
$env:FTA_URL = "https://files.example.com"
$env:FTA_TOKEN = "your-long-token"
python .\client\fta.py text "A note to sync"
python .\client\fta.py upload .\report.pdf
python .\client\fta.py list
python .\client\fta.py cat <item-id>
python .\client\fta.py download <item-id> --output .
python .\client\fta.py delete <item-id> --yes
```

Bash:

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

## Backup

Back up the `data/` directory, which holds SQLite metadata and uploaded files.
Use an application-consistent snapshot when the service is accepting writes.
