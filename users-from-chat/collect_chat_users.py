import argparse
import asyncio
import csv
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from getpass import getpass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

try:
    from telethon import TelegramClient, errors, types
except ImportError as exc:
    raise SystemExit(
        "Missing dependency. Install requirements first:\n"
        "  python -m pip install -r requirements.txt"
    ) from exc


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CHAT = os.getenv("TARGET_CHAT", "https://t.me/freelead")
DEFAULT_OUTPUT = BASE_DIR / "telegram_chat_users.csv"
DEFAULT_API_ID = "34825825"
DEFAULT_API_HASH = "60176f7ad0bcd77e63d4a64ca8d50a38"


def load_env_files() -> list[Path]:
    if not load_dotenv:
        return []

    candidates = [
        BASE_DIR / ".env",
        BASE_DIR.parent / ".env",
        BASE_DIR.parent.parent / ".env",
        Path.cwd() / ".env",
    ]
    loaded = []
    seen = set()

    for path in candidates:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.exists() and load_dotenv(resolved, override=False):
            loaded.append(resolved)

    return loaded


LOADED_ENV_FILES = load_env_files()


def error_classes(*names):
    return tuple(cls for cls in (getattr(errors, name, None) for name in names) if cls)


ACCESS_ERRORS = error_classes(
    "ChannelPrivateError",
    "ChatAdminRequiredError",
    "UserBannedInChannelError",
    "UserNotParticipantError",
)
CHAT_RESOLVE_ERRORS = error_classes(
    "UsernameInvalidError",
    "UsernameNotOccupiedError",
    "PeerIdInvalidError",
)
AUTH_ERRORS = error_classes(
    "AuthKeyInvalidError",
    "AuthKeyUnregisteredError",
    "PhoneNumberBannedError",
    "UserDeactivatedBanError",
    "UserDeactivatedError",
)


def describe_telegram_error(exc: Exception) -> str:
    if isinstance(exc, getattr(errors, "FloodWaitError", ())):
        return f"Telegram rate limit: нужно подождать {exc.seconds} секунд."
    if isinstance(exc, ACCESS_ERRORS):
        return "Нет доступа к чату: аккаунт не состоит в чате, забанен, чат приватный или нужна роль администратора."
    if isinstance(exc, CHAT_RESOLVE_ERRORS):
        return "Не удалось найти чат. Проверь ссылку/username и что аккаунт уже вступил в этот чат."
    if isinstance(exc, AUTH_ERRORS):
        return "Проблема с Telegram-сессией или аккаунтом: сессия недействительна, аккаунт забанен или деактивирован."
    if isinstance(exc, getattr(errors, "RPCError", ())):
        return f"Telegram RPC error: {type(exc).__name__}: {exc}"
    if isinstance(exc, ValueError):
        return f"Не удалось распознать чат или entity: {exc}"
    return f"{type(exc).__name__}: {exc}"


async def maybe_wait_flood(exc: Exception, max_flood_wait: int, action: str) -> bool:
    seconds = int(getattr(exc, "seconds", 0) or 0)
    print(f"[TG][FLOOD WAIT] Telegram ограничил действие: {action}. Нужно ждать {seconds} секунд.")

    if max_flood_wait > 0 and seconds <= max_flood_wait:
        print(f"[TG][WAIT] Жду {seconds} секунд и продолжу.")
        await asyncio.sleep(seconds)
        return True

    print("[TG][STOP] Не жду автоматически. Можно увеличить --max-flood-wait, если хочешь ждать такие лимиты.")
    return False


async def resolve_chat(client: TelegramClient, chat_ref: str, max_flood_wait: int):
    while True:
        try:
            chat = await client.get_entity(chat_ref)
            title = getattr(chat, "title", None) or getattr(chat, "username", None) or chat_ref
            print(f"[TG] Chat resolved: {title}")
            return chat
        except errors.FloodWaitError as exc:
            if await maybe_wait_flood(exc, max_flood_wait, "открытие чата"):
                continue
            raise SystemExit(f"[TG][STOP] {describe_telegram_error(exc)}") from exc
        except ACCESS_ERRORS as exc:
            raise SystemExit(f"[TG][NO ACCESS] {describe_telegram_error(exc)}") from exc
        except CHAT_RESOLVE_ERRORS as exc:
            raise SystemExit(f"[TG][NOT FOUND] {describe_telegram_error(exc)}") from exc
        except AUTH_ERRORS as exc:
            raise SystemExit(f"[TG][AUTH] {describe_telegram_error(exc)}") from exc
        except ValueError as exc:
            raise SystemExit(f"[TG][NOT FOUND] {describe_telegram_error(exc)}") from exc
        except errors.RPCError as exc:
            raise SystemExit(f"[TG][ERROR] {describe_telegram_error(exc)}") from exc


async def get_sender_safely(message, max_flood_wait: int):
    while True:
        try:
            return await message.get_sender()
        except errors.FloodWaitError as exc:
            if await maybe_wait_flood(exc, max_flood_wait, "получение автора сообщения"):
                continue
            raise
        except errors.RPCError as exc:
            print(f"[TG][WARN] Не смог получить автора сообщения #{message.id}: {describe_telegram_error(exc)}")
            return None


@dataclass
class AccountConfig:
    account: str
    api_id: int
    api_hash: str
    phone: str
    session_path: Path


def normalize_account_name(account: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", account.strip())
    return cleaned or "default"


def env_for_account(account: str, key: str) -> str:
    prefix = normalize_account_name(account).upper()
    candidates = [
        f"TG_{prefix}_{key}",
        f"TELEGRAM_{prefix}_{key}",
    ]
    if account.lower() in {"chat", "comments", "search", "sender"}:
        candidates.extend(
            [
                f"TELEGRAM_{account.upper()}_{key}",
                f"TG_{account.upper()}_{key}",
            ]
        )
    candidates.extend([f"TG_{key}", f"TELEGRAM_{key}"])
    for name in candidates:
        value = os.getenv(name)
        if value:
            return value
    return ""


def session_file_exists(path: Path) -> bool:
    return path.exists() or path.with_suffix(".session").exists()


def resolve_session_path(session: str, account: str) -> Path:
    if session:
        session_path = Path(session)
        if session_path.is_absolute():
            return session_path

        candidates = [
            BASE_DIR / session_path,
            BASE_DIR.parent / session_path,
            BASE_DIR.parent / "opros" / session_path,
            BASE_DIR.parent / "users-from-coments" / session_path,
            Path.cwd() / session_path,
        ]
        for candidate in candidates:
            if session_file_exists(candidate):
                return candidate
        return BASE_DIR / session_path

    return BASE_DIR / "sessions" / f"{normalize_account_name(account)}.session"


def resolve_account(account: str) -> AccountConfig:
    api_id_raw = env_for_account(account, "API_ID") or DEFAULT_API_ID
    api_hash = env_for_account(account, "API_HASH") or DEFAULT_API_HASH
    phone = env_for_account(account, "PHONE")
    session = env_for_account(account, "SESSION") or env_for_account(account, "SESSION_NAME")

    try:
        api_id = int(api_id_raw)
    except ValueError as exc:
        raise SystemExit("Telegram API_ID must be a number.") from exc

    session_path = resolve_session_path(session, account)
    session_path.parent.mkdir(parents=True, exist_ok=True)
    return AccountConfig(account, api_id, api_hash, phone, session_path)


def print_terminal_qr(url: str) -> None:
    qrencode = shutil.which("qrencode")
    if qrencode:
        subprocess.run([qrencode, "-t", "ANSIUTF8", url], check=False)
        return
    print("QR generator is not installed. Open this link from an already logged-in Telegram app:")
    print(url)


async def login_with_qr(client: TelegramClient) -> None:
    print("Starting QR login.")
    print("Open Telegram: Settings -> Devices -> Link Desktop Device.")
    qr_login = await client.qr_login()
    print_terminal_qr(qr_login.url)
    try:
        user = await qr_login.wait(timeout=120)
    except asyncio.TimeoutError as exc:
        raise SystemExit("QR login timed out.") from exc
    except errors.SessionPasswordNeededError:
        password = getpass("Enter Telegram 2FA password: ")
        await client.sign_in(password=password)
        user = await client.get_me()
    print(f"Logged in as @{user.username or user.id}")


async def ensure_authorized(client: TelegramClient, account: AccountConfig) -> None:
    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"[TG] Account '{account.account}' authorized as @{me.username or me.id}")
        return

    method = input("Type 'qr' for QR login, or press Enter for phone-code login: ").strip().lower()
    if method == "qr":
        await login_with_qr(client)
        return

    if not account.phone:
        account.phone = input(f"Enter Telegram phone for account '{account.account}' (+79991234567): ").strip()
    if not account.phone:
        raise SystemExit("Telegram phone is empty.")

    try:
        sent = await client.send_code_request(account.phone)
    except errors.PhoneNumberBannedError as exc:
        raise SystemExit("Telegram says this phone number is banned.") from exc
    except errors.PhoneNumberInvalidError as exc:
        raise SystemExit("Telegram says this phone number is invalid.") from exc
    except errors.FloodWaitError as exc:
        raise SystemExit(f"Telegram rate-limited login. Wait {exc.seconds} seconds.") from exc

    print(f"Code requested. Delivery type: {type(sent.type).__name__}")
    code = input("Enter Telegram login code: ").strip().replace(" ", "")
    try:
        await client.sign_in(phone=account.phone, code=code, phone_code_hash=sent.phone_code_hash)
    except errors.SessionPasswordNeededError:
        password = getpass("Enter Telegram 2FA password: ")
        await client.sign_in(password=password)


def normalize_username(value: str | None) -> str:
    username = (value or "").strip()
    return username[1:] if username.startswith("@") else username


def cutoff_from_months(months: int) -> datetime:
    days = max(months, 0) * 30
    return datetime.now(timezone.utc) - timedelta(days=days)


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def add_user(seen_users: dict[int, dict], seen_usernames: set[str], sender: types.User, message) -> bool:
    if sender.id in seen_users:
        current = seen_users[sender.id]
        if sender.access_hash and not current.get("access_hash"):
            current["access_hash"] = sender.access_hash
        if sender.username and not current.get("username"):
            current["username"] = sender.username
        return False

    username = normalize_username(sender.username)
    username_key = username.lower()
    if username_key and username_key in seen_usernames:
        return False

    message_date = as_utc(message.date)
    row = {
        "user_id": sender.id,
        "access_hash": sender.access_hash or "",
        "username": username,
        "first_name": sender.first_name or "",
        "last_name": sender.last_name or "",
        "is_bot": bool(sender.bot),
        "message_id": message.id,
        "message_date": message_date.isoformat() if message_date else "",
    }
    seen_users[sender.id] = row
    if username_key:
        seen_usernames.add(username_key)
    return True


async def collect_users(
    client: TelegramClient,
    chat,
    cutoff: datetime,
    limit: int | None,
    include_bots: bool,
    max_flood_wait: int,
) -> tuple[list[dict], int]:
    seen_users: dict[int, dict] = {}
    seen_usernames: set[str] = set()
    messages_seen = 0
    offset_id = 0

    while True:
        try:
            async for message in client.iter_messages(chat, offset_id=offset_id):
                messages_seen += 1
                offset_id = message.id
                message_date = as_utc(message.date)
                if message_date and message_date < cutoff:
                    print(f"[STOP] Reached message older than cutoff: {message_date.isoformat()}")
                    rows = sorted(seen_users.values(), key=lambda row: row["message_date"], reverse=True)
                    return rows, messages_seen

                sender = await get_sender_safely(message, max_flood_wait)
                if not isinstance(sender, types.User):
                    continue
                if sender.bot and not include_bots:
                    continue

                added = add_user(seen_users, seen_usernames, sender, message)
                if added and len(seen_users) % 25 == 0:
                    print(f"[PROGRESS] Messages checked: {messages_seen}; unique users: {len(seen_users)}")

                if limit is not None and len(seen_users) >= limit:
                    print(f"[STOP] Limit reached: {limit}")
                    rows = sorted(seen_users.values(), key=lambda row: row["message_date"], reverse=True)
                    return rows, messages_seen

            rows = sorted(seen_users.values(), key=lambda row: row["message_date"], reverse=True)
            return rows, messages_seen
        except errors.FloodWaitError as exc:
            if await maybe_wait_flood(exc, max_flood_wait, "чтение истории чата"):
                continue
            raise SystemExit(f"[TG][STOP] {describe_telegram_error(exc)}") from exc
        except ACCESS_ERRORS as exc:
            raise SystemExit(f"[TG][NO ACCESS] {describe_telegram_error(exc)}") from exc
        except AUTH_ERRORS as exc:
            raise SystemExit(f"[TG][AUTH] {describe_telegram_error(exc)}") from exc
        except errors.RPCError as exc:
            raise SystemExit(f"[TG][ERROR] {describe_telegram_error(exc)}") from exc


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "user_id",
        "access_hash",
        "username",
        "first_name",
        "last_name",
        "is_bot",
        "message_id",
        "message_date",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect unique Telegram users who wrote in a chat.")
    parser.add_argument("--account", default="chat", help="Telegram account name. Example: chat, main.")
    parser.add_argument("--chat", default=DEFAULT_CHAT, help="Chat username/link/id. Default: https://t.me/freelead")
    parser.add_argument("--months", type=int, default=6, help="How many recent months to scan. Default: 6.")
    parser.add_argument("--limit", type=int, default=None, help="Stop after N unique users. Example: --limit 20")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Output CSV path.")
    parser.add_argument("--include-bots", action="store_true", help="Include bot accounts.")
    parser.add_argument("--max-flood-wait", type=int, default=0, help="Wait and continue if Telegram FloodWait is at most N seconds. Default: 0 means stop immediately.")
    return parser


async def main() -> None:
    args = build_parser().parse_args()
    account = resolve_account(args.account)
    client = TelegramClient(str(account.session_path.with_suffix("")), account.api_id, account.api_hash)
    cutoff = cutoff_from_months(args.months)

    print(f"Chat: {args.chat}")
    print(f"Account: {account.account}")
    print(f"Session: {account.session_path}")
    print(f"Loaded .env files: {', '.join(str(path) for path in LOADED_ENV_FILES) or '-'}")
    print(f"Login phone from env: {account.phone or '-'}")
    print(f"Cutoff: {cutoff.isoformat()}")
    print(f"Limit: {args.limit or '-'}")
    print(f"Max FloodWait auto-wait: {args.max_flood_wait}s")

    await client.connect()
    try:
        await ensure_authorized(client, account)
        chat = await resolve_chat(client, args.chat, args.max_flood_wait)
        rows, messages_seen = await collect_users(
            client,
            chat,
            cutoff,
            args.limit,
            args.include_bots,
            args.max_flood_wait,
        )
    finally:
        await client.disconnect()

    output_path = Path(args.output)
    write_csv(output_path, rows)

    print(f"Finished. Messages checked: {messages_seen}; unique users saved: {len(rows)}")
    print(f"Saved to: {output_path.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
