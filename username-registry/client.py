#!/usr/bin/env python3
"""Command-line client for the username registry service."""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from pathlib import Path


def request(url: str, endpoint: str, payload: dict, key_name: str, key: str) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    http_request = urllib.request.Request(f"{url.rstrip('/')}{endpoint}", data=body, headers={"Content-Type": "application/json", key_name: key}, method="POST")
    try:
        with urllib.request.urlopen(http_request, timeout=20) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read()).get("message", str(exc))
        except (json.JSONDecodeError, UnicodeDecodeError):
            message = str(exc)
        raise SystemExit(f"Request failed ({exc.code}): {message}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Could not reach the service: {exc.reason}") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description="Username registry client")
    parser.add_argument("--url", default=os.getenv("REGISTRY_URL"), required=not os.getenv("REGISTRY_URL"))
    sub = parser.add_subparsers(dest="command", required=True)
    claim = sub.add_parser("claim", help="claim one unused username")
    claim.add_argument("username")
    take = sub.add_parser("take", help="atomically take the next unused record(s) in queue order")
    take.add_argument("n", nargs="?", type=int, default=1, help="number of records to take (1..10000; default: 1)")
    add = sub.add_parser("add", help="add an unused Telegram user (admin only)")
    add.add_argument("username")
    add.add_argument("user_id")
    add.add_argument("access_hash")
    add.add_argument("chat")
    import_csv = sub.add_parser("import-csv", help="import Telegram users from CSV (admin only)")
    import_csv.add_argument("path", type=Path)
    import_csv.add_argument("--chat", help="Chat to use when CSV has no chat column")
    import_csv.add_argument("--ignore-existing", action="store_true", help="Import only new records and report existing duplicates")
    args = parser.parse_args()
    if args.command == "claim":
        key = os.getenv("REGISTRY_API_KEY")
        if not key:
            raise SystemExit("Set REGISTRY_API_KEY")
        print(json.dumps(request(args.url, "/v1/claim", {"username": args.username}, "X-API-Key", key), ensure_ascii=False))
        return
    if args.command == "take":
        key = os.getenv("REGISTRY_API_KEY")
        if not key:
            raise SystemExit("Set REGISTRY_API_KEY")
        print(json.dumps(request(args.url, "/v1/claim-next", {"n": args.n}, "X-API-Key", key), ensure_ascii=False))
        return
    key = os.getenv("REGISTRY_ADMIN_KEY")
    if not key:
        raise SystemExit("Set REGISTRY_ADMIN_KEY")
    if args.command == "add":
        username = None if args.username == "-" else args.username
        result = request(
            args.url,
            "/v1/records",
            {"username": username, "user_id": args.user_id, "access_hash": args.access_hash, "chat": args.chat},
            "X-Admin-Key",
            key,
        )
    else:
        import csv
        with args.path.open(newline="", encoding="utf-8-sig") as source:
            reader = csv.DictReader(source)
            required = {"username", "user_id", "access_hash"}
            if not reader.fieldnames or not required.issubset(reader.fieldnames):
                raise SystemExit("CSV must have username, user_id and access_hash headers")
            if "chat" not in reader.fieldnames and not args.chat:
                raise SystemExit("CSV has no chat header; pass --chat 'Chat name'")
            records = [
                {
                    "username": row.get("username"),
                    "user_id": row.get("user_id"),
                    "access_hash": row.get("access_hash"),
                    "chat": row.get("chat") or args.chat,
                }
                for row in reader
            ]
        result = request(args.url, "/v1/import", {"records": records, "ignore_existing": args.ignore_existing}, "X-Admin-Key", key)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
