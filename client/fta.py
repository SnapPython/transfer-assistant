#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import http.client
import json
import mimetypes
import os
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def env_or_prompt(name: str, prompt: str, secret: bool = False) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    if secret:
        return getpass.getpass(prompt).strip()
    return input(prompt).strip()


def normalize_url(url: str) -> str:
    return url.rstrip("/")


def auth_headers(token: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = dict(extra or {})
    headers["Authorization"] = f"Bearer {token}"
    headers["X-FTA-Token"] = token
    return headers


def request_json(method: str, base_url: str, token: str, path: str, payload: Any | None = None) -> Any:
    data = None
    headers = auth_headers(token)
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(f"{base_url}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"HTTP {exc.code}: {detail}") from exc
    if not body:
        return None
    return json.loads(body.decode("utf-8"))


def upload_file(base_url: str, token: str, file_path: Path, remote_name: str | None = None) -> Any:
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in {"http", "https"}:
        raise SystemExit("FTA_URL must start with http:// or https://")
    conn_class = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    port = parsed.port
    host = parsed.hostname or ""
    conn = conn_class(host, port=port, timeout=120)
    name = remote_name or file_path.name
    query = urllib.parse.urlencode({"name": name})
    prefix = parsed.path.rstrip("/")
    upload_path = f"{prefix}/api/files?{query}" if prefix else f"/api/files?{query}"
    size = file_path.stat().st_size
    mime_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    conn.putrequest("POST", upload_path)
    conn.putheader("Authorization", f"Bearer {token}")
    conn.putheader("X-FTA-Token", token)
    conn.putheader("Content-Type", mime_type)
    conn.putheader("Content-Length", str(size))
    conn.endheaders()
    with file_path.open("rb") as src:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            conn.send(chunk)
    response = conn.getresponse()
    body = response.read().decode("utf-8", "replace")
    conn.close()
    if response.status >= 400:
        raise SystemExit(f"HTTP {response.status}: {body}")
    return json.loads(body)


def download(base_url: str, token: str, item_id: str, output: Path) -> Path:
    detail = request_json("GET", base_url, token, f"/api/items/{item_id}")
    item = detail["item"]
    if output.exists() and output.is_dir():
        filename = item.get("original_name") or f"{item.get('title') or item_id}.txt"
        target = output / filename
    else:
        target = output
    req = urllib.request.Request(f"{base_url}/api/items/{item_id}/download", headers=auth_headers(token))
    try:
        with urllib.request.urlopen(req, timeout=120) as response, target.open("wb") as out:
            shutil.copyfileobj(response, out)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"HTTP {exc.code}: {detail}") from exc
    return target


def print_items(items: list[dict[str, Any]]) -> None:
    if not items:
        print("No items")
        return
    for item in items:
        title = item.get("title") or item.get("original_name") or ""
        created = item.get("created_at", "")
        size = item.get("size_bytes", 0)
        print(f"{item['id']}  {created}  {item['kind']:<4}  {size:>10}  {title}")


def read_text_arg(args: argparse.Namespace) -> str:
    if args.file:
        return Path(args.file).read_text(encoding="utf-8")
    if args.text:
        return " ".join(args.text)
    return sys.stdin.read()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CLI for the self-hosted file transfer assistant.")
    parser.add_argument("--url", default=os.environ.get("FTA_URL", ""), help="Service URL, or FTA_URL")
    parser.add_argument("--token", default=os.environ.get("FTA_TOKEN", ""), help="Access token, or FTA_TOKEN")
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser("list", help="List recent items")
    list_parser.add_argument("--search", "-q", default="")
    list_parser.add_argument("--limit", type=int, default=50)

    text_parser = sub.add_parser("text", help="Send text")
    text_parser.add_argument("text", nargs="*")
    text_parser.add_argument("--title", default="")
    text_parser.add_argument("--file", "-f")

    upload_parser = sub.add_parser("upload", help="Upload a file")
    upload_parser.add_argument("path")
    upload_parser.add_argument("--name")

    cat_parser = sub.add_parser("cat", help="Print a text item")
    cat_parser.add_argument("id")

    download_parser = sub.add_parser("download", help="Download an item")
    download_parser.add_argument("id")
    download_parser.add_argument("--output", "-o", default=".")

    delete_parser = sub.add_parser("delete", help="Delete an item")
    delete_parser.add_argument("id")
    delete_parser.add_argument("--yes", "-y", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    base_url = normalize_url(args.url or env_or_prompt("FTA_URL", "URL: "))
    token = args.token or env_or_prompt("FTA_TOKEN", "Token: ", secret=True)

    if args.command == "list":
        query = urllib.parse.urlencode({"limit": args.limit, "q": args.search})
        payload = request_json("GET", base_url, token, f"/api/items?{query}")
        print_items(payload["items"])
    elif args.command == "text":
        text = read_text_arg(args)
        payload = request_json("POST", base_url, token, "/api/text", {"title": args.title, "text": text})
        print(payload["item"]["id"])
    elif args.command == "upload":
        path = Path(args.path)
        if not path.is_file():
            raise SystemExit(f"Not a file: {path}")
        payload = upload_file(base_url, token, path, args.name)
        print(payload["item"]["id"])
    elif args.command == "cat":
        payload = request_json("GET", base_url, token, f"/api/items/{args.id}")
        item = payload["item"]
        if item["kind"] != "text":
            raise SystemExit("Item is not text")
        print(item.get("text_content", ""), end="")
    elif args.command == "download":
        target = download(base_url, token, args.id, Path(args.output))
        print(target)
    elif args.command == "delete":
        if not args.yes:
            answer = input(f"Delete {args.id}? [y/N] ").strip().lower()
            if answer != "y":
                return 1
        request_json("DELETE", base_url, token, f"/api/items/{args.id}")
        print("deleted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
