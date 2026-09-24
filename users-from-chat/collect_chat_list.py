#!/usr/bin/env python3
"""Collect recent authors from a Markdown list of Telegram chats into the registry."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from pathlib import Path

from telethon import TelegramClient

from collect_chat_users import (
    DEFAULT_REGISTRY_URL,
    collect_users,
    cutoff_from_months,
    ensure_authorized,
    import_into_registry,
    resolve_account,
    resolve_chat,
)

URL_PATTERN = re.compile(r"https?://t\.me/[^\s)>\]]+", re.IGNORECASE)
BASE_DIR = Path(__file__).resolve().parent


def read_chat_references(path: Path | str) -> list[str]:
    path = Path(path)
    if not path.is_file():
        raise SystemExit(f"Chat list does not exist: {path}")
    references = []
    seen = set()
    for match in URL_PATTERN.finditer(path.read_text(encoding="utf-8")):
        reference = match.group(0).rstrip(".,;:!?}")
        key = reference.casefold()
        if key not in seen:
            seen.add(key)
            references.append(reference)
    if not references:
        raise SystemExit("No https://t.me/... links found in the chat list.")
    return references


def chat_name(chat, fallback: str) -> str:
    return (getattr(chat, "title", None) or getattr(chat, "username", None) or fallback).strip()


def write_summary(path: Path, summary: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect recent authors from every Telegram chat link in a Markdown file.")
    parser.add_argument("--list", required=True, type=Path, help="Markdown/text file containing https://t.me/... links.")
    parser.add_argument("--account", default="chat", help="Telegram account name. Default: chat.")
    parser.add_argument("--months", type=int, default=6, help="How many recent 30-day months to scan. Default: 6.")
    parser.add_argument("--max-flood-wait", type=int, default=300, help="Wait automatically only up to this many seconds. Default: 300.")
    parser.add_argument("--registry-url", default=DEFAULT_REGISTRY_URL, help="Username registry base URL.")
    parser.add_argument(
        "--summary",
        type=Path,
        default=BASE_DIR / "telegram_chat_batch_summary.json",
        help="Where to save the non-sensitive batch summary.",
    )
    return parser


async def main() -> None:
    args = build_parser().parse_args()
    admin_key = os.getenv("REGISTRY_ADMIN_KEY", "")
    if not admin_key:
        raise SystemExit("Set REGISTRY_ADMIN_KEY before starting the batch collector.")
    references = read_chat_references(args.list)
    account = resolve_account(args.account)
    cutoff = cutoff_from_months(args.months)
    client = TelegramClient(str(account.session_path.with_suffix("")), account.api_id, account.api_hash)
    summary: list[dict] = []

    print(f"Chats found: {len(references)}")
    print(f"Cutoff: {cutoff.isoformat()}")
    await client.connect()
    try:
        await ensure_authorized(client, account)
        for index, reference in enumerate(references, start=1):
            print(f"\n[BATCH {index}/{len(references)}] {reference}")
            entry = {"reference": reference}
            try:
                chat = await resolve_chat(client, reference, args.max_flood_wait)
                label = chat_name(chat, reference)
                rows, messages_seen = await collect_users(
                    client,
                    chat,
                    cutoff,
                    None,
                    False,
                    args.max_flood_wait,
                )
                imported, missing_data, existing = import_into_registry(rows, args.registry_url, admin_key, label)
                entry.update(
                    {
                        "status": "ok",
                        "chat": label,
                        "messages_seen": messages_seen,
                        "unique_users": len(rows),
                        "imported": imported,
                        "skipped_existing": existing,
                        "skipped_missing_data": missing_data,
                    }
                )
                print(
                    f"[BATCH] {label}: users={len(rows)}, imported={imported}, "
                    f"existing={existing}, missing_data={missing_data}"
                )
            except SystemExit as exc:
                entry.update({"status": "skipped", "reason": str(exc)})
                print(f"[BATCH][SKIP] {exc}")
            except Exception as exc:  # Continue with other chats after an unexpected per-chat failure.
                entry.update({"status": "error", "reason": f"{type(exc).__name__}: {exc}"})
                print(f"[BATCH][ERROR] {entry['reason']}")
            summary.append(entry)
            write_summary(args.summary, summary)
    finally:
        await client.disconnect()

    succeeded = sum(item["status"] == "ok" for item in summary)
    skipped = sum(item["status"] == "skipped" for item in summary)
    errors = sum(item["status"] == "error" for item in summary)
    imported = sum(int(item.get("imported", 0)) for item in summary)
    print(f"\nFinished. chats_ok={succeeded}, skipped={skipped}, errors={errors}, imported={imported}")
    print(f"Summary: {args.summary.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
