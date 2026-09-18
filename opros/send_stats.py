#!/usr/bin/env python3
"""Show cumulative successful Telegram deliveries by sending account."""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SENT_FILE = BASE_DIR / "registry_sent.jsonl"


@dataclass
class SenderStats:
    successful_events: int = 0
    recipients: set[str] | None = None
    test_events: int = 0
    test_recipients: set[str] | None = None

    def __post_init__(self) -> None:
        self.recipients = set() if self.recipients is None else self.recipients
        self.test_recipients = set() if self.test_recipients is None else self.test_recipients


def recipient_key(record: dict[str, Any]) -> str | None:
    """Build a stable identity so retries do not count the same person twice."""
    user_id = record.get("user_id")
    if user_id not in (None, ""):
        return f"id:{user_id}"
    username = str(record.get("username") or "").strip().lstrip("@").casefold()
    if username:
        return f"username:{username}"
    return None


def collect_stats(path: Path) -> tuple[dict[str, SenderStats], int]:
    stats: dict[str, SenderStats] = defaultdict(SenderStats)
    skipped_lines = 0
    if not path.is_file():
        return stats, skipped_lines

    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
            account = str(event["account"]).strip().casefold()
            record = event["record"]
            if not account or not isinstance(record, dict):
                raise ValueError("missing account or record")
            key = recipient_key(record)
            if key is None:
                raise ValueError("recipient has neither user_id nor username")
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            skipped_lines += 1
            continue

        item = stats[account]
        item.successful_events += 1
        item.recipients.add(key)
        if event.get("test_mode"):
            item.test_events += 1
            item.test_recipients.add(key)
    return stats, skipped_lines


def output_payload(stats: dict[str, SenderStats], skipped_lines: int) -> dict[str, Any]:
    def account_sort_key(account: str) -> tuple[int, int | str]:
        match = re.fullmatch(r"sender([1-9][0-9]*)", account, flags=re.IGNORECASE)
        return (0, int(match.group(1))) if match else (1, account)

    accounts = sorted(stats, key=account_sort_key)
    sender_data = {
        account: {
            "unique_recipients": len((item := stats.get(account, SenderStats())).recipients),
            "successful_send_events": item.successful_events,
            "test_unique_recipients": len(item.test_recipients),
            "test_send_events": item.test_events,
        }
        for account in accounts
    }
    return {
        "senders": sender_data,
        "total_unique_recipient_account_pairs": sum(item["unique_recipients"] for item in sender_data.values()),
        "skipped_malformed_lines": skipped_lines,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Count unique successful Telegram recipients for each sender from registry_sent.jsonl."
    )
    parser.add_argument("--sent-file", type=Path, default=DEFAULT_SENT_FILE)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args()

    sent_file = args.sent_file.expanduser().resolve()
    stats, skipped_lines = collect_stats(sent_file)
    payload = output_payload(stats, skipped_lines)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    print(f"Source: {sent_file}")
    for account, item in payload["senders"].items():
        print(
            f"{account}: {item['unique_recipients']} unique recipient(s); "
            f"{item['successful_send_events']} successful send event(s); "
            f"tests: {item['test_unique_recipients']} recipient(s), {item['test_send_events']} event(s)."
        )
    print(f"Total unique recipient/account pairs: {payload['total_unique_recipient_account_pairs']}")
    if skipped_lines:
        print(f"Skipped malformed log lines: {skipped_lines}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
