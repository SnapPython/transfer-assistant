from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import hmac
import json
import mimetypes
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from http.cookies import SimpleCookie
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
DEFAULT_MAX_UPLOAD_MB = 512
DEFAULT_MAX_TEXT_CHARS = 200_000
SESSION_COOKIE = "fta_session"
SESSION_MAX_AGE_SECONDS = 30 * 24 * 60 * 60


@dataclass(frozen=True)
class AppConfig:
    data_dir: Path
    token: str
    max_upload_bytes: int
    max_text_chars: int
    cors_origins: tuple[str, ...]
    totp_secret: str
    web_auth_mode: str

    @property
    def db_path(self) -> Path:
        return self.data_dir / "transfer_assistant.sqlite3"

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"

    @property
    def temp_dir(self) -> Path:
        return self.data_dir / "tmp"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sanitize_filename(value: str | None, fallback: str = "upload.bin") -> str:
    name = (value or "").replace("\\", "/")
    name = Path(name).name.strip().strip(".")
    cleaned = []
    for char in name:
        code = ord(char)
        if code < 32 or char in '<>:"/\\|?*':
            cleaned.append("_")
        else:
            cleaned.append(char)
    name = "".join(cleaned).strip()
    if not name:
        name = fallback
    return name[:180]


def make_title_from_text(text: str) -> str:
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if not first_line:
        return "Text note"
    return first_line[:80]


def format_item(row: sqlite3.Row, include_text: bool = False) -> dict[str, Any]:
    item = {
        "id": row["id"],
        "kind": row["kind"],
        "title": row["title"],
        "original_name": row["original_name"],
        "mime_type": row["mime_type"],
        "size_bytes": row["size_bytes"],
        "sha256": row["sha256"],
        "created_at": row["created_at"],
    }
    if row["kind"] == "text":
        text = row["text_content"] or ""
        if include_text:
            item["text_content"] = text
        item["text_preview"] = text[:400]
    return item


def json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def parse_bounded_int(value: str, default: int, minimum: int, maximum: int) -> int:
    if value == "":
        return default
    parsed = int(value)
    return min(max(parsed, minimum), maximum)


def content_disposition(filename: str) -> str:
    fallback = sanitize_filename(filename).encode("ascii", "ignore").decode("ascii") or "download"
    quoted = urllib.parse.quote(filename, safe="")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quoted}"


def session_signature(token: str, payload: str) -> str:
    return hmac.new(token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def make_session_value(token: str) -> str:
    timestamp = str(int(dt.datetime.now(dt.timezone.utc).timestamp()))
    nonce = base64.urlsafe_b64encode(os.urandom(18)).decode("ascii").rstrip("=")
    payload = f"{timestamp}.{nonce}"
    return f"{payload}.{session_signature(token, payload)}"


def verify_session_value(value: str, token: str, max_age_seconds: int = SESSION_MAX_AGE_SECONDS) -> bool:
    parts = value.split(".")
    if len(parts) != 3:
        return False
    timestamp_text, nonce, signature = parts
    if not timestamp_text.isdigit() or not nonce:
        return False
    payload = f"{timestamp_text}.{nonce}"
    expected = session_signature(token, payload)
    if not hmac.compare_digest(signature, expected):
        return False
    timestamp = int(timestamp_text)
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    return 0 <= now - timestamp <= max_age_seconds


def normalize_totp_secret(secret: str) -> str:
    return "".join(secret.upper().split()).rstrip("=")


def base32_decode_no_padding(value: str) -> bytes:
    normalized = normalize_totp_secret(value)
    padding = "=" * ((8 - len(normalized) % 8) % 8)
    return base64.b32decode(normalized + padding, casefold=True)


def generate_totp_secret(length: int = 20) -> str:
    return base64.b32encode(os.urandom(length)).decode("ascii").rstrip("=")


def hotp(secret: str, counter: int, digits: int = 6) -> str:
    key = base32_decode_no_padding(secret)
    msg = counter.to_bytes(8, "big")
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
    return str(value % (10**digits)).zfill(digits)


def verify_totp(secret: str, code: str, at_time: int | None = None, period: int = 30, window: int = 1, digits: int = 6) -> bool:
    clean_code = "".join(code.split())
    if not clean_code.isdigit() or len(clean_code) != digits:
        return False
    now = int(time.time() if at_time is None else at_time)
    counter = now // period
    for offset in range(-window, window + 1):
        if hmac.compare_digest(hotp(secret, counter + offset, digits), clean_code):
            return True
    return False


def validate_web_login(payload: dict[str, Any], config: AppConfig) -> bool:
    mode = config.web_auth_mode
    token = payload.get("token", "")
    code = payload.get("totp", "")
    token_ok = isinstance(token, str) and hmac.compare_digest(token.strip(), config.token)
    totp_ok = bool(config.totp_secret) and isinstance(code, str) and verify_totp(config.totp_secret, code)

    if mode == "totp":
        return totp_ok
    if mode == "token_totp":
        return token_ok and (not config.totp_secret or totp_ok)
    return token_ok


def init_storage(config: AppConfig) -> None:
    config.files_dir.mkdir(parents=True, exist_ok=True)
    config.temp_dir.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(config.db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS items (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL CHECK (kind IN ('file', 'text')),
                title TEXT NOT NULL,
                text_content TEXT,
                original_name TEXT,
                storage_name TEXT,
                mime_type TEXT,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                sha256 TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_created_at ON items(created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_kind ON items(kind)")
        conn.commit()


def connect_db(config: AppConfig) -> sqlite3.Connection:
    conn = sqlite3.connect(config.db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


class TransferHandler(BaseHTTPRequestHandler):
    server_version = "TransferAssistant/0.1"

    @property
    def config(self) -> AppConfig:
        return self.server.config  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - - [%s] %s\n" % (self.client_address[0], self.log_date_time_string(), fmt % args))

    def end_headers(self) -> None:
        origin = self.headers.get("Origin", "")
        if origin and origin in self.config.cors_origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Vary", "Origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; img-src 'self' blob: data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Filename, X-FTA-Token, X-FTA-Session")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path in {"/", "/index.html"} or path.startswith("/static/"):
            self.serve_static(path)
            return
        if path == "/healthz":
            self.send_json({"ok": True})
            return
        if not self.require_auth():
            return
        if path == "/api/items":
            self.handle_list_items(parsed)
            return
        if path.startswith("/api/items/"):
            parts = path.strip("/").split("/")
            if len(parts) == 3:
                self.handle_get_item(parts[2])
                return
            if len(parts) == 4 and parts[3] == "download":
                self.handle_download(parts[2])
                return
        self.send_error_json(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/login":
            self.handle_login()
            return
        if parsed.path == "/api/logout":
            self.handle_logout()
            return
        if not self.require_auth():
            return
        if parsed.path == "/api/text":
            self.handle_create_text()
            return
        if parsed.path == "/api/files":
            self.handle_upload_file(parsed)
            return
        self.send_error_json(HTTPStatus.NOT_FOUND, "Not found")

    def do_DELETE(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if not self.require_auth():
            return
        parts = parsed.path.strip("/").split("/")
        if len(parts) == 3 and parts[:2] == ["api", "items"]:
            self.handle_delete_item(parts[2])
            return
        self.send_error_json(HTTPStatus.NOT_FOUND, "Not found")

    def serve_static(self, path: str) -> None:
        if path in {"/", "/index.html"}:
            file_path = STATIC_DIR / "index.html"
        elif path == "/static/app.css":
            file_path = STATIC_DIR / "app.css"
        elif path == "/static/app.js":
            file_path = STATIC_DIR / "app.js"
        else:
            self.send_error_json(HTTPStatus.NOT_FOUND, "Not found")
            return

        try:
            body = file_path.read_bytes()
        except FileNotFoundError:
            self.send_error_json(HTTPStatus.NOT_FOUND, "Not found")
            return

        mime_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{mime_type}; charset=utf-8" if mime_type.startswith("text/") else mime_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def require_auth(self) -> bool:
        header = self.headers.get("Authorization", "")
        token = ""
        if header.lower().startswith("bearer "):
            token = header[7:].strip()
        if not token:
            token = self.headers.get("X-FTA-Token", "").strip()
        if hmac.compare_digest(token, self.config.token):
            return True
        session_token = self.headers.get("X-FTA-Session", "").strip()
        if session_token and verify_session_value(session_token, self.config.token):
            return True
        if self.has_valid_session_cookie():
            return True
        self.send_error_json(HTTPStatus.UNAUTHORIZED, "Invalid or missing token")
        return False

    def has_valid_session_cookie(self) -> bool:
        cookie_header = self.headers.get("Cookie", "")
        if not cookie_header:
            return False
        cookie = SimpleCookie()
        try:
            cookie.load(cookie_header)
        except Exception:
            return False
        morsel = cookie.get(SESSION_COOKIE)
        if morsel is None:
            return False
        return verify_session_value(morsel.value, self.config.token)

    def is_secure_request(self) -> bool:
        proto = self.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower()
        host = self.headers.get("Host", "").lower()
        return proto == "https" or host.endswith(".ts.net") or ".ts.net:" in host

    def send_session_cookie(self, value: str) -> None:
        cookie = f"{SESSION_COOKIE}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_MAX_AGE_SECONDS}"
        if self.is_secure_request():
            cookie += "; Secure"
        self.send_header("Set-Cookie", cookie)

    def clear_session_cookie(self) -> None:
        cookie = f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT"
        if self.is_secure_request():
            cookie += "; Secure"
        self.send_header("Set-Cookie", cookie)

    def read_json_body(self, max_bytes: int = 1_000_000) -> dict[str, Any] | None:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self.send_error_json(HTTPStatus.LENGTH_REQUIRED, "Content-Length is required")
            return None
        try:
            length = int(raw_length)
        except ValueError:
            self.send_error_json(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
            return None
        if length > max_bytes:
            self.send_error_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request body is too large")
            return None
        body = self.rfile.read(length)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_error_json(HTTPStatus.BAD_REQUEST, "Invalid JSON body")
            return None
        if not isinstance(payload, dict):
            self.send_error_json(HTTPStatus.BAD_REQUEST, "JSON body must be an object")
            return None
        return payload

    def send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status: HTTPStatus, message: str) -> None:
        self.send_json({"error": message}, status)

    def handle_login(self) -> None:
        payload = self.read_json_body(max_bytes=10_000)
        if payload is None:
            return
        if not validate_web_login(payload, self.config):
            self.send_error_json(HTTPStatus.UNAUTHORIZED, "Invalid login")
            return
        session_token = make_session_value(self.config.token)
        body = json_bytes({"ok": True, "session_token": session_token})
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_session_cookie(session_token)
        self.end_headers()
        self.wfile.write(body)

    def handle_logout(self) -> None:
        body = json_bytes({"ok": True})
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.clear_session_cookie()
        self.end_headers()
        self.wfile.write(body)

    def handle_list_items(self, parsed: urllib.parse.ParseResult) -> None:
        query = urllib.parse.parse_qs(parsed.query)
        try:
            limit = parse_bounded_int(query.get("limit", ["80"])[0], 80, 1, 200)
            offset = parse_bounded_int(query.get("offset", ["0"])[0], 0, 0, 1_000_000)
        except ValueError:
            self.send_error_json(HTTPStatus.BAD_REQUEST, "Invalid pagination parameter")
            return
        search = (query.get("q", [""])[0] or "").strip()

        params: list[Any] = []
        where = ""
        if search:
            like = f"%{search}%"
            where = "WHERE title LIKE ? OR original_name LIKE ? OR text_content LIKE ?"
            params.extend([like, like, like])

        with connect_db(self.config) as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM items
                {where}
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
        self.send_json({"items": [format_item(row) for row in rows], "limit": limit, "offset": offset})

    def handle_get_item(self, item_id: str) -> None:
        with connect_db(self.config) as conn:
            row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            self.send_error_json(HTTPStatus.NOT_FOUND, "Item not found")
            return
        self.send_json({"item": format_item(row, include_text=True)})

    def handle_create_text(self) -> None:
        payload = self.read_json_body()
        if payload is None:
            return
        text = payload.get("text", "")
        title = payload.get("title", "")
        if not isinstance(text, str) or not text.strip():
            self.send_error_json(HTTPStatus.BAD_REQUEST, "Text is required")
            return
        if len(text) > self.config.max_text_chars:
            self.send_error_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Text is too long")
            return
        if not isinstance(title, str) or not title.strip():
            title = make_title_from_text(text)
        else:
            title = title.strip()[:120]
        item_id = uuid.uuid4().hex
        created_at = utc_now()
        size_bytes = len(text.encode("utf-8"))
        sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with connect_db(self.config) as conn:
            conn.execute(
                """
                INSERT INTO items
                (id, kind, title, text_content, original_name, storage_name, mime_type, size_bytes, sha256, created_at)
                VALUES (?, 'text', ?, ?, NULL, NULL, 'text/plain; charset=utf-8', ?, ?, ?)
                """,
                (item_id, title, text, size_bytes, sha256, created_at),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        self.send_json({"item": format_item(row, include_text=True)}, HTTPStatus.CREATED)

    def handle_upload_file(self, parsed: urllib.parse.ParseResult) -> None:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self.send_error_json(HTTPStatus.LENGTH_REQUIRED, "Content-Length is required")
            return
        try:
            content_length = int(raw_length)
        except ValueError:
            self.send_error_json(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
            return
        if content_length < 1:
            self.send_error_json(HTTPStatus.BAD_REQUEST, "File body is empty")
            return
        if content_length > self.config.max_upload_bytes:
            self.send_error_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "File is too large")
            return

        query = urllib.parse.parse_qs(parsed.query)
        filename = query.get("name", [self.headers.get("X-Filename", "upload.bin")])[0]
        original_name = sanitize_filename(filename)
        mime_type = self.headers.get("Content-Type") or mimetypes.guess_type(original_name)[0] or "application/octet-stream"
        item_id = uuid.uuid4().hex
        suffix = Path(original_name).suffix[:32]
        storage_name = f"{item_id}{suffix}"
        final_path = self.config.files_dir / storage_name

        digest = hashlib.sha256()
        total = 0
        temp_fd, temp_name = tempfile.mkstemp(prefix=f"{item_id}-", suffix=".part", dir=self.config.temp_dir)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(temp_fd, "wb") as out:
                remaining = content_length
                while remaining > 0:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ConnectionError("Unexpected end of upload")
                    out.write(chunk)
                    digest.update(chunk)
                    total += len(chunk)
                    remaining -= len(chunk)
                    if total > self.config.max_upload_bytes:
                        raise ValueError("File is too large")
            shutil.move(str(temp_path), final_path)
        except ValueError as exc:
            temp_path.unlink(missing_ok=True)
            self.send_error_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, str(exc))
            return
        except Exception as exc:
            temp_path.unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        created_at = utc_now()
        with connect_db(self.config) as conn:
            conn.execute(
                """
                INSERT INTO items
                (id, kind, title, text_content, original_name, storage_name, mime_type, size_bytes, sha256, created_at)
                VALUES (?, 'file', ?, NULL, ?, ?, ?, ?, ?, ?)
                """,
                (item_id, original_name, original_name, storage_name, mime_type, total, digest.hexdigest(), created_at),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        self.send_json({"item": format_item(row)}, HTTPStatus.CREATED)

    def handle_download(self, item_id: str) -> None:
        with connect_db(self.config) as conn:
            row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            self.send_error_json(HTTPStatus.NOT_FOUND, "Item not found")
            return

        if row["kind"] == "text":
            filename = sanitize_filename(row["title"], "note") + ".txt"
            body = (row["text_content"] or "").encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition", content_disposition(filename))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        storage_name = row["storage_name"]
        if not storage_name:
            self.send_error_json(HTTPStatus.NOT_FOUND, "File is missing")
            return
        file_path = self.config.files_dir / storage_name
        if not file_path.exists():
            self.send_error_json(HTTPStatus.NOT_FOUND, "File is missing")
            return
        filename = row["original_name"] or row["title"] or "download"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", row["mime_type"] or "application/octet-stream")
        self.send_header("Content-Disposition", content_disposition(filename))
        self.send_header("Content-Length", str(file_path.stat().st_size))
        self.end_headers()
        with file_path.open("rb") as src:
            shutil.copyfileobj(src, self.wfile)

    def handle_delete_item(self, item_id: str) -> None:
        with connect_db(self.config) as conn:
            row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
            if row is None:
                self.send_error_json(HTTPStatus.NOT_FOUND, "Item not found")
                return
            conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
            conn.commit()

        if row["kind"] == "file" and row["storage_name"]:
            (self.config.files_dir / row["storage_name"]).unlink(missing_ok=True)
        self.send_json({"ok": True})


class TransferServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], config: AppConfig):
        super().__init__(server_address, TransferHandler)
        self.config = config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the self-hosted file transfer assistant.")
    parser.add_argument("--host", default=os.environ.get("FTA_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("FTA_PORT", "8787")))
    parser.add_argument("--data-dir", default=os.environ.get("FTA_DATA_DIR", "./data"))
    parser.add_argument("--token", default=os.environ.get("FTA_TOKEN"))
    parser.add_argument("--max-upload-mb", type=int, default=int(os.environ.get("FTA_MAX_UPLOAD_MB", DEFAULT_MAX_UPLOAD_MB)))
    parser.add_argument("--max-text-chars", type=int, default=int(os.environ.get("FTA_MAX_TEXT_CHARS", DEFAULT_MAX_TEXT_CHARS)))
    parser.add_argument("--cors-origins", default=os.environ.get("FTA_CORS_ORIGINS", ""))
    parser.add_argument("--totp-secret", default=os.environ.get("FTA_TOTP_SECRET", ""))
    parser.add_argument("--web-auth-mode", choices=("token", "token_totp", "totp"), default=os.environ.get("FTA_WEB_AUTH_MODE", "token_totp"))
    parser.add_argument("--generate-totp-secret", action="store_true", help="Print a Google Authenticator compatible TOTP secret and exit.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.generate_totp_secret:
        print(generate_totp_secret())
        return 0
    if not args.token:
        print("FTA_TOKEN is required. Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\"", file=sys.stderr)
        return 2
    config = AppConfig(
        data_dir=Path(args.data_dir).resolve(),
        token=args.token,
        max_upload_bytes=args.max_upload_mb * 1024 * 1024,
        max_text_chars=args.max_text_chars,
        cors_origins=tuple(origin.strip().rstrip("/") for origin in args.cors_origins.split(",") if origin.strip()),
        totp_secret=normalize_totp_secret(args.totp_secret),
        web_auth_mode=args.web_auth_mode,
    )
    if config.web_auth_mode == "totp" and not config.totp_secret:
        print("FTA_TOTP_SECRET is required when FTA_WEB_AUTH_MODE=totp.", file=sys.stderr)
        return 2
    init_storage(config)
    server = TransferServer((args.host, args.port), config)
    print(f"Transfer Assistant listening on http://{args.host}:{args.port}")
    print(f"Data directory: {config.data_dir}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
