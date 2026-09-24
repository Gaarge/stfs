#!/usr/bin/env python3
"""Verify Telegram usernames in a lead workbook and resolve fallback phones.

The source workbook is never modified.  A new workbook preserves every source
column and appends the extracted Telegram candidates and their resolution.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path

from openpyxl import Workbook, load_workbook
from telethon import TelegramClient, errors, functions, types


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_API_ID = "34825825"
DEFAULT_API_HASH = "60176f7ad0bcd77e63d4a64ca8d50a38"
USERNAME_RE = re.compile(r"(?<![A-Za-z0-9_])@([A-Za-z][A-Za-z0-9_]{4,31})(?![A-Za-z0-9_])")
TG_LINK_RE = re.compile(r"(?:https?://)?(?:t|telegram)\.me/([A-Za-z][A-Za-z0-9_]{4,31})(?:[/?#]|$)", re.IGNORECASE)
PHONE_TOKEN_RE = re.compile(r"(?<!\d)(?:\+|00)?(?:\d[\s().-]*){7,16}\d(?!\d)")


@dataclass
class AccountConfig:
    api_id: int
    api_hash: str
    session_path: Path


def load_env_files() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    for path in (BASE_DIR / ".env", BASE_DIR.parent / ".env", Path.cwd() / ".env"):
        if path.exists():
            load_dotenv(path, override=False)


def account_from_env(account: str) -> AccountConfig:
    prefix = re.sub(r"[^A-Za-z0-9]+", "_", account).strip("_").upper() or "PHONE_LOOKUP"

    def setting(name: str) -> str:
        return os.getenv(f"TG_{prefix}_{name}") or os.getenv(f"TELEGRAM_{prefix}_{name}") or os.getenv(f"TG_{name}") or os.getenv(f"TELEGRAM_{name}") or ""

    api_id = int(setting("API_ID") or DEFAULT_API_ID)
    api_hash = setting("API_HASH") or DEFAULT_API_HASH
    session = setting("SESSION") or f"sessions/{account}.session"
    session_path = Path(session)
    if not session_path.is_absolute():
        session_path = BASE_DIR / session_path
    return AccountConfig(api_id, api_hash, session_path)


def unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def normalize_phone(candidate: str) -> str | None:
    """Normalize explicit international and Russian 8/7-prefixed numbers only."""
    compact = re.sub(r"[^\d+]", "", candidate)
    digits = re.sub(r"\D", "", compact)
    if compact.startswith("00"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if not (8 <= len(digits) <= 15 and (candidate.strip().startswith(("+", "00")) or (len(digits) == 11 and digits.startswith("7")))):
        return None
    return "+" + digits


def extract_candidates(value: object) -> tuple[list[str], list[str]]:
    text = "" if value is None else str(value)
    usernames = [match.group(1).lower() for match in USERNAME_RE.finditer(text)]
    usernames.extend(match.group(1).lower() for match in TG_LINK_RE.finditer(text))
    phones = [normalized for match in PHONE_TOKEN_RE.finditer(text) if (normalized := normalize_phone(match.group(0)))]
    return unique(usernames), unique(phones)


async def verify_username(client: TelegramClient, username: str) -> tuple[str, types.User | None]:
    try:
        entity = await client.get_entity(username)
    except (errors.UsernameInvalidError, errors.UsernameNotOccupiedError, ValueError):
        return "not_found", None
    except errors.FloodWaitError as exc:
        raise SystemExit(f"Telegram rate-limited username verification; wait {exc.seconds} seconds and rerun.") from exc
    if not isinstance(entity, types.User):
        return "not_a_user", None
    return "found", entity


async def resolve_phone_batch(
    client: TelegramClient, requests: list[tuple[int, int, str]]
) -> tuple[dict[tuple[int, int], tuple[str, types.User | None]], list[types.User]]:
    """Resolve up to 25 fallback numbers in a single Telegram contact import."""
    contacts = [
        types.InputPhoneContact(client_id=client_id, phone=phone, first_name="Contact", last_name="")
        for client_id, (_, _, phone) in enumerate(requests, start=1)
    ]
    while True:
        try:
            response = await client(functions.contacts.ImportContactsRequest(contacts=contacts))
            break
        except errors.FloodWaitError as exc:
            print(f"Telegram asked to wait {exc.seconds}s before the next phone batch; waiting once.", flush=True)
            await asyncio.sleep(exc.seconds + 1)

    imported = {item.client_id: item.user_id for item in response.imported}
    users = {user.id: user for user in response.users if isinstance(user, types.User)}
    results: dict[tuple[int, int], tuple[str, types.User | None]] = {}
    newly_imported: list[types.User] = []
    for client_id, (row_number, phone_index, _) in enumerate(requests, start=1):
        user_id = imported.get(client_id)
        user = users.get(user_id) if user_id else None
        results[(row_number, phone_index)] = ("found" if user else "not_found", user)
        if user:
            newly_imported.append(user)
    return results, newly_imported


async def delete_new_contacts(client: TelegramClient, users: list[types.User]) -> None:
    inputs = [types.InputUser(user.id, user.access_hash) for user in users if user.access_hash]
    if not inputs:
        return
    try:
        await client(functions.contacts.DeleteContactsRequest(id=inputs))
    except errors.FloodWaitError as exc:
        print(f"Warning: could not remove temporary imported contacts now; Telegram asked to wait {exc.seconds}s.", flush=True)


def result_values(
    usernames: list[str], phones: list[str], username_checks: list[str], phone_checks: list[str], user: types.User | None, source: str
) -> list[str]:
    return [
        ", ".join(f"@{name}" for name in usernames),
        ", ".join(phones),
        "; ".join(username_checks),
        "; ".join(phone_checks),
        source if user else "",
        "found" if user else "not_found",
        str(user.id) if user else "",
        str(user.access_hash or "") if user else "",
        user.username or "" if user else "",
        user.first_name or "" if user else "",
        user.last_name or "" if user else "",
    ]


async def process(source: Path, output: Path, account: str, delay: float) -> None:
    book = load_workbook(source, read_only=True, data_only=False)
    if "Лиды" not in book.sheetnames:
        raise SystemExit("Workbook has no sheet named 'Лиды'.")
    sheet = book["Лиды"]
    header_row = next(sheet.iter_rows(min_row=1, max_row=1, values_only=True))
    headers = list(header_row)
    try:
        contact_index = headers.index("Контакт")
    except ValueError as exc:
        raise SystemExit("Sheet 'Лиды' has no 'Контакт' column.") from exc

    result_book = Workbook()
    result_sheet = result_book.active
    result_sheet.title = "Проверенные лиды"
    added_headers = [
        "Строка источника", "Извлечённые username", "Извлечённые телефоны", "Проверка username",
        "Проверка телефона", "Источник найденного аккаунта", "Статус Telegram", "Telegram user_id",
        "Telegram access_hash", "Telegram username", "Telegram имя", "Telegram фамилия",
    ]
    result_sheet.append(headers + added_headers)

    config = account_from_env(account)
    client = TelegramClient(str(config.session_path.with_suffix("")), config.api_id, config.api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise SystemExit(f"No saved Telegram session at {config.session_path}. Log in with resolve_phone_contacts.py --account {account} --login-only --qr first.")

    try:
        states = []
        for row_number, source_values in enumerate(sheet.iter_rows(min_row=2, values_only=True), start=2):
            values = list(source_values)
            usernames, phones = extract_candidates(values[contact_index])
            states.append({
                "row_number": row_number, "values": values, "usernames": usernames, "phones": phones,
                "username_checks": [], "phone_checks": [], "resolved_user": None, "source_kind": "",
            })

        for state in states:
            for username in state["usernames"]:
                status, user = await verify_username(client, username)
                state["username_checks"].append(f"@{username}: {status}")
                if status == "found" and not state["resolved_user"]:
                    state["resolved_user"], state["source_kind"] = user, "username"
                if delay:
                    await asyncio.sleep(delay)
            if (state["row_number"] - 1) % 25 == 0:
                print(f"Checked usernames through {state['row_number'] - 1} lead rows.", flush=True)

        phone_requests = [
            (state["row_number"], phone_index, phone)
            for state in states if not state["resolved_user"]
            for phone_index, phone in enumerate(state["phones"], start=1)
        ]
        states_by_row = {state["row_number"]: state for state in states}
        for batch_start in range(0, len(phone_requests), 25):
            batch = phone_requests[batch_start : batch_start + 25]
            resolutions, imported_users = await resolve_phone_batch(client, batch)
            for row_number, phone_index, phone in batch:
                status, user = resolutions[(row_number, phone_index)]
                state = states_by_row[row_number]
                state["phone_checks"].append(f"{phone}: {status}")
                if status == "found" and not state["resolved_user"]:
                    state["resolved_user"], state["source_kind"] = user, "phone"
            await delete_new_contacts(client, imported_users)
            print(f"Resolved phone batch {batch_start // 25 + 1}: {len(batch)} numbers.", flush=True)
            if delay and batch_start + 25 < len(phone_requests):
                await asyncio.sleep(max(delay, 3.0))

        for state in states:
            result_sheet.append(
                state["values"] + [state["row_number"]] + result_values(
                    state["usernames"], state["phones"], state["username_checks"], state["phone_checks"],
                    state["resolved_user"], state["source_kind"],
                )
            )
    finally:
        await client.disconnect()

    for column in result_sheet.columns:
        letter = column[0].column_letter
        result_sheet.column_dimensions[letter].width = min(max(12, max(len(str(cell.value or "")) for cell in column) + 2), 50)
    result_sheet.freeze_panes = "A2"
    output.parent.mkdir(parents=True, exist_ok=True)
    result_book.save(output)
    print(f"Saved {output}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Verify Telegram contacts in the Repetam lead workbook.")
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, default=BASE_DIR / "repetam_online_school_leads_telegram_checked.xlsx")
    parser.add_argument("--account", default="main18")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between Telegram lookups; default: 1")
    parser.add_argument("--i-have-consent", action="store_true", help="required confirmation that you may process these contacts")
    args = parser.parse_args()
    if not args.i_have_consent:
        raise SystemExit("Refusing to process contacts without --i-have-consent.")
    if args.delay < 0:
        raise SystemExit("--delay cannot be negative.")
    if not args.input.is_file():
        raise SystemExit(f"Input file not found: {args.input}")
    load_env_files()
    await process(args.input, args.output, args.account, args.delay)


if __name__ == "__main__":
    asyncio.run(main())
