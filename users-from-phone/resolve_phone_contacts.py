#!/usr/bin/env python3
"""Resolve consented phone contacts to Telegram delivery identifiers.

Telegram does not expose a public "phone number -> username" endpoint.  The
official client workflow is to import a contact, then inspect the user objects
Telegram returns.  This script follows that workflow and removes only contacts
that the current run actually imported (unless --keep-contacts is specified).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from getpass import getpass
from pathlib import Path
from typing import Iterable

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

from telethon import TelegramClient, errors, functions, types


BASE_DIR = Path(__file__).resolve().parent
PHONE_RE = re.compile(r"^\+[1-9]\d{6,14}$")
# Same Telegram application used by the existing collectors in this repository.
# Account-specific environment variables still override these values.
DEFAULT_API_ID = "34825825"
DEFAULT_API_HASH = "60176f7ad0bcd77e63d4a64ca8d50a38"


@dataclass
class AccountConfig:
    api_id: int
    api_hash: str
    phone: str
    session_path: Path


def load_env_files() -> None:
    if not load_dotenv:
        return
    for path in (BASE_DIR / ".env", BASE_DIR.parent / ".env", Path.cwd() / ".env"):
        if path.exists():
            load_dotenv(path, override=False)


def normalize_phone(value: str) -> str:
    """Accept only an international E.164-like number; never guess a country."""
    value = value.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if value.startswith("00"):
        value = "+" + value[2:]
    if not PHONE_RE.fullmatch(value):
        raise ValueError("must be in international format, e.g. +79991234567")
    return value


def account_from_env(account: str) -> AccountConfig:
    prefix = re.sub(r"[^A-Za-z0-9]+", "_", account).strip("_").upper() or "PHONE_LOOKUP"

    def setting(name: str) -> str:
        return os.getenv(f"TG_{prefix}_{name}") or os.getenv(f"TELEGRAM_{prefix}_{name}") or os.getenv(f"TG_{name}") or os.getenv(f"TELEGRAM_{name}") or ""

    try:
        api_id = int(setting("API_ID") or DEFAULT_API_ID)
    except ValueError as exc:
        raise SystemExit("Set TG_<ACCOUNT>_API_ID (or TG_API_ID) to your Telegram API ID.") from exc
    api_hash = setting("API_HASH") or DEFAULT_API_HASH
    if not api_hash:
        raise SystemExit("Set TG_<ACCOUNT>_API_HASH (or TG_API_HASH).")
    session = setting("SESSION") or f"sessions/{account}.session"
    session_path = Path(session)
    if not session_path.is_absolute():
        session_path = BASE_DIR / session_path
    session_path.parent.mkdir(parents=True, exist_ok=True)
    return AccountConfig(api_id, api_hash, setting("PHONE"), session_path)


def print_qr(url: str) -> None:
    if qrencode := shutil.which("qrencode"):
        subprocess.run([qrencode, "-t", "ANSIUTF8", url], check=False)
    else:
        print("Open this QR login URL on an authorized Telegram device:\n" + url)


async def authorize(client: TelegramClient, config: AccountConfig, force_qr: bool = False) -> None:
    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Authorized as @{me.username or me.id}")
        return

    method = "qr" if force_qr else input("Type 'qr' for QR login, or press Enter for phone-code login: ").strip().lower()
    if method == "qr":
        qr_login = await client.qr_login()
        print_qr(qr_login.url)
        try:
            await qr_login.wait(timeout=120)
        except errors.SessionPasswordNeededError:
            await client.sign_in(password=getpass("Telegram 2FA password: "))
        except asyncio.TimeoutError as exc:
            raise SystemExit("QR login timed out; run the command again.") from exc
        return

    phone = config.phone or input("Your Telegram login phone (+79991234567): ").strip()
    try:
        phone = normalize_phone(phone)
        sent = await client.send_code_request(phone)
        code = input("Telegram login code: ").strip().replace(" ", "")
        await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
    except errors.SessionPasswordNeededError:
        await client.sign_in(password=getpass("Telegram 2FA password: "))
    except errors.FloodWaitError as exc:
        raise SystemExit(f"Telegram rate-limited the login; wait {exc.seconds} seconds.") from exc


def read_contacts(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if not reader.fieldnames or "phone" not in reader.fieldnames:
            raise SystemExit("Input CSV must have a 'phone' column.")
        contacts: list[dict[str, str]] = []
        seen: set[str] = set()
        for line, row in enumerate(reader, start=2):
            raw_phone = row.get("phone") or ""
            try:
                phone = normalize_phone(raw_phone)
            except ValueError as exc:
                raise SystemExit(f"CSV line {line}: phone {raw_phone!r} {exc}.") from exc
            if phone not in seen:
                seen.add(phone)
                contacts.append({"phone": phone, "first_name": (row.get("first_name") or "").strip() or "Contact", "last_name": (row.get("last_name") or "").strip()})
    return contacts


def chunks(values: list[dict[str, str]], size: int) -> Iterable[list[dict[str, str]]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


async def delete_imported_contacts(client: TelegramClient, users: list[types.User]) -> None:
    """Delete contacts proven to have been newly imported in this run."""
    inputs = [types.InputUser(user.id, user.access_hash) for user in users if user.access_hash]
    for start in range(0, len(inputs), 100):
        await client(functions.contacts.DeleteContactsRequest(id=inputs[start : start + 100]))


async def resolve(client: TelegramClient, contacts: list[dict[str, str]], batch_size: int, delay: float, keep_contacts: bool) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    imported_users_to_delete: list[types.User] = []

    for batch_number, batch in enumerate(chunks(contacts, batch_size), start=1):
        telegram_contacts = [
            types.InputPhoneContact(client_id=index, phone=item["phone"], first_name=item["first_name"], last_name=item["last_name"])
            for index, item in enumerate(batch, start=1)
        ]
        try:
            response = await client(functions.contacts.ImportContactsRequest(contacts=telegram_contacts))
        except errors.FloodWaitError as exc:
            raise SystemExit(f"Telegram limited this account while importing batch {batch_number}; wait {exc.seconds} seconds before continuing.") from exc

        by_client_id = {item.client_id: item.user_id for item in response.imported}
        users = {user.id: user for user in response.users if isinstance(user, types.User)}
        newly_imported_ids = set(by_client_id.values())
        imported_users_to_delete.extend(users[user_id] for user_id in newly_imported_ids if user_id in users)

        for index, item in enumerate(batch, start=1):
            user = users.get(by_client_id.get(index, 0))
            results.append(
                {
                    "phone": item["phone"],
                    "found": "yes" if user else "no",
                    "user_id": str(user.id) if user else "",
                    "access_hash": str(user.access_hash or "") if user else "",
                    "username": user.username or "" if user else "",
                    "first_name": user.first_name or "" if user else "",
                    "last_name": user.last_name or "" if user else "",
                    "is_bot": str(bool(user.bot)).lower() if user else "",
                }
            )

        print(f"Processed batch {batch_number}: {len(batch)} numbers.")
        if delay and batch_number * batch_size < len(contacts):
            await asyncio.sleep(delay)

    if not keep_contacts and imported_users_to_delete:
        try:
            await delete_imported_contacts(client, imported_users_to_delete)
            print(f"Removed {len(imported_users_to_delete)} contacts imported by this run.")
        except errors.FloodWaitError as exc:
            print(f"Warning: results were saved, but cleanup was rate-limited. Wait {exc.seconds}s and remove the newly imported contacts manually.")
    return results


def write_results(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames = ["phone", "found", "user_id", "access_hash", "username", "first_name", "last_name", "is_bot"]
    with path.open("w", encoding="utf-8", newline="") as destination:
        writer = csv.DictWriter(destination, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve consented phone contacts to Telegram delivery identifiers.")
    parser.add_argument("input", type=Path, nargs="?", help="CSV with a required phone column in +E.164 format")
    parser.add_argument("--output", type=Path, default=BASE_DIR / "telegram_phone_contacts.csv")
    parser.add_argument("--account", default="phone_lookup", help="env/session account suffix; default: phone_lookup")
    parser.add_argument("--batch-size", type=int, default=25, choices=range(1, 101), metavar="1..100")
    parser.add_argument("--delay", type=float, default=3.0, help="seconds between batches; default: 3")
    parser.add_argument("--keep-contacts", action="store_true", help="do not remove contacts newly imported by this run")
    parser.add_argument("--i-have-consent", action="store_true", help="required confirmation that you may use every supplied number")
    parser.add_argument("--login-only", action="store_true", help="authorize and save the Telegram session, without reading contacts")
    parser.add_argument("--qr", action="store_true", help="use QR authorization immediately, without choosing a login method")
    args = parser.parse_args()
    if not args.login_only and not args.i_have_consent:
        raise SystemExit("Refusing to process contacts without --i-have-consent.")
    if args.delay < 0:
        raise SystemExit("--delay cannot be negative.")
    if args.qr and not args.login_only:
        raise SystemExit("--qr is only available with --login-only.")
    if not args.login_only and (not args.input or not args.input.is_file()):
        raise SystemExit(f"Input file not found: {args.input}")

    load_env_files()
    config = account_from_env(args.account)
    client = TelegramClient(str(config.session_path.with_suffix("")), config.api_id, config.api_hash)
    await client.connect()
    try:
        await authorize(client, config, force_qr=args.qr)
        if args.login_only:
            print(f"Session saved at {config.session_path}.")
            return
        contacts = read_contacts(args.input)
        if not contacts:
            raise SystemExit("Input CSV contains no contacts.")
        rows = await resolve(client, contacts, args.batch_size, args.delay, args.keep_contacts)
    finally:
        await client.disconnect()
    write_results(args.output, rows)
    print(f"Saved {len(rows)} rows to {args.output}. Found: {sum(row['found'] == 'yes' for row in rows)}.")


if __name__ == "__main__":
    asyncio.run(main())
