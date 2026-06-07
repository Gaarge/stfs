import csv
import asyncio
import argparse
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from getpass import getpass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

from telethon import TelegramClient, errors, types

BASE_DIR = Path(__file__).resolve().parent

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

channel_username = os.getenv("TARGET_CHANNEL", "portnyaginlive")

OUTPUT_FILE = BASE_DIR / "telegram_comment_users.csv"
POST_LIMIT = int(os.getenv("POST_LIMIT", "0")) or None
COMMENT_LIMIT_PER_POST = int(os.getenv("COMMENT_LIMIT_PER_POST", "0")) or None

client = None


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
    if account.lower() in {"comments", "search", "sender"}:
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


def resolve_account(account: str) -> AccountConfig:
    api_id_raw = env_for_account(account, "API_ID") or DEFAULT_API_ID
    api_hash = env_for_account(account, "API_HASH") or DEFAULT_API_HASH
    phone = env_for_account(account, "PHONE")
    session = env_for_account(account, "SESSION") or env_for_account(account, "SESSION_NAME")

    try:
        api_id = int(api_id_raw)
    except ValueError as exc:
        raise SystemExit("Telegram API_ID must be a number.") from exc

    if session:
        session_path = Path(session)
        if not session_path.is_absolute():
            session_path = BASE_DIR / session_path
    else:
        session_path = BASE_DIR / "sessions" / f"{normalize_account_name(account)}.session"

    session_path.parent.mkdir(parents=True, exist_ok=True)
    return AccountConfig(account, api_id, api_hash, phone, session_path)


def describe_code_type(code_type):
    type_name = type(code_type).__name__
    descriptions = {
        "CodeTypeCall": "следующая попытка может быть звонком",
        "CodeTypeFlashCall": "следующая попытка может быть flash-call",
        "CodeTypeFragmentSms": "следующая попытка может быть Fragment SMS",
        "CodeTypeMissedCall": "следующая попытка может быть пропущенным звонком",
        "CodeTypeSms": "следующая попытка может быть SMS",
        "SentCodeTypeApp": "код отправлен в приложение Telegram на уже авторизованном устройстве",
        "SentCodeTypeSms": "код отправлен по SMS",
        "SentCodeTypeCall": "код придет звонком",
        "SentCodeTypeFlashCall": "Telegram проверит номер через flash-call",
        "SentCodeTypeMissedCall": "код придет через пропущенный звонок",
        "SentCodeTypeEmailCode": "код отправлен на email",
        "SentCodeTypeFirebaseSms": "код отправлен через Firebase SMS",
        "SentCodeTypeFragmentSms": "код отправлен через Fragment SMS",
        "SentCodeTypeSmsPhrase": "код отправлен по SMS фразой",
        "SentCodeTypeSmsWord": "код отправлен по SMS словом",
    }

    description = descriptions.get(type_name, type_name)
    length = getattr(code_type, "length", None)
    if length:
        description += f" ({length} символов)"
    email_pattern = getattr(code_type, "email_pattern", None)
    if email_pattern:
        description += f" ({email_pattern})"
    return description


def print_code_request(sent):
    print("Code requested.")
    print("Delivery:", describe_code_type(sent.type))
    if getattr(sent, "next_type", None):
        print("Next available delivery method:", describe_code_type(sent.next_type))
    if getattr(sent, "timeout", None):
        print(f"If the code does not arrive, wait about {sent.timeout} seconds before requesting it again.")
    print("If there is no code, press Enter without typing a code to request the next/repeated delivery method.")


def print_terminal_qr(url):
    qrencode = shutil.which("qrencode")
    if qrencode:
        subprocess.run([qrencode, "-t", "ANSIUTF8", url], check=False)
        return

    print("QR generator is not installed, open this link from an already logged-in Telegram app:")
    print(url)


async def login_with_qr():
    print("Starting QR login.")
    print("Open Telegram on your phone: Settings -> Devices -> Link Desktop Device.")
    print("Then scan the QR below while this script is waiting.")

    qr_login = await client.qr_login()
    print_terminal_qr(qr_login.url)

    try:
        user = await qr_login.wait(timeout=120)
    except asyncio.TimeoutError as exc:
        raise SystemExit("QR login timed out. Run the script again and choose QR login one more time.") from exc
    except errors.SessionPasswordNeededError:
        password = getpass("Enter Telegram 2FA password: ")
        await client.sign_in(password=password)
        user = await client.get_me()

    print(f"Logged in as @{user.username or user.id}")


async def ensure_authorized(account):
    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Account '{account.account}' already logged in as @{me.username or me.id}")
        return

    login_method = input("Type 'qr' for QR login, or press Enter for phone-code login: ").strip().lower()
    if login_method == "qr":
        await login_with_qr()
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
        raise SystemExit("Telegram says this phone number is invalid. Use international format, for example +79991234567.") from exc
    except errors.FloodWaitError as exc:
        raise SystemExit(f"Telegram rate-limited login attempts. Wait {exc.seconds} seconds and try again.") from exc

    print_code_request(sent)

    attempts = 0
    resend_attempts = 0
    while attempts < 3:
        code = input("Enter Telegram login code: ").strip().replace(" ", "")
        if not code:
            if resend_attempts >= 3:
                raise SystemExit("No code entered after several resend attempts. Stop and try again later.")
            resend_attempts += 1
            use_qr = input("Still no code? Type 'qr' to switch to QR login, or press Enter to request another delivery attempt: ").strip().lower()
            if use_qr == "qr":
                await login_with_qr()
                return
            print("Requesting another delivery attempt from Telegram...")
            try:
                sent = await client.send_code_request(account.phone)
            except errors.FloodWaitError as exc:
                raise SystemExit(f"Telegram rate-limited login attempts. Wait {exc.seconds} seconds and try again.") from exc
            except errors.PhoneCodeExpiredError as exc:
                raise SystemExit("The previous code request expired. Run the script again and request a new code.") from exc
            print_code_request(sent)
            continue

        try:
            await client.sign_in(phone=account.phone, code=code, phone_code_hash=sent.phone_code_hash)
            me = await client.get_me()
            print(f"Logged in as @{me.username or me.id}")
            return
        except errors.SessionPasswordNeededError:
            password = getpass("Enter Telegram 2FA password: ")
            await client.sign_in(password=password)
            me = await client.get_me()
            print(f"Logged in as @{me.username or me.id}")
            return
        except errors.PhoneCodeInvalidError:
            print(f"Invalid code. Attempts left: {2 - attempts}")
        except errors.PhoneCodeExpiredError as exc:
            raise SystemExit("The code expired. Run the script again and request a new code.") from exc
        attempts += 1

    raise SystemExit("Too many invalid code attempts.")


def add_sender(seen_users, sender):
    if not isinstance(sender, types.User):
        return

    current = seen_users.get(sender.id, {})
    seen_users[sender.id] = {
        "user_id": sender.id,
        "access_hash": sender.access_hash or current.get("access_hash") or "",
        "username": sender.username or current.get("username") or "",
    }


def build_parser():
    parser = argparse.ArgumentParser(description="Collect Telegram users from comments under channel posts.")
    parser.add_argument("--account", default="comments", help="Telegram account name. Example: comments, main.")
    parser.add_argument("--channel", default=channel_username, help="Public channel username.")
    parser.add_argument("--post-limit", type=int, default=POST_LIMIT, help="Max channel posts to scan.")
    parser.add_argument("--comment-limit-per-post", type=int, default=COMMENT_LIMIT_PER_POST, help="Max comments per post.")
    parser.add_argument("--output", default=str(OUTPUT_FILE), help="Output CSV path.")
    return parser


async def main():
    global client

    args = build_parser().parse_args()
    account = resolve_account(args.account)
    client = TelegramClient(str(account.session_path.with_suffix("")), account.api_id, account.api_hash)

    seen_users = {}

    await client.connect()
    try:
        await ensure_authorized(account)

        channel = await client.get_entity(args.channel)
        posts_seen = 0
        threads_seen = 0
        comments_seen = 0

        print(f"Reading comments from @{args.channel}")
        print(f"Account: {account.account}")
        print(f"Session: {account.session_path}")
        print(f"Loaded .env files: {', '.join(str(path) for path in LOADED_ENV_FILES) or '-'}")
        async for post in client.iter_messages(channel, limit=args.post_limit):
            posts_seen += 1
            reply_count = getattr(getattr(post, "replies", None), "replies", 0) or 0
            if reply_count == 0:
                continue

            threads_seen += 1
            try:
                async for comment in client.iter_messages(
                    channel,
                    reply_to=post.id,
                    limit=args.comment_limit_per_post,
                ):
                    comments_seen += 1
                    sender = await comment.get_sender()
                    add_sender(seen_users, sender)
            except errors.RPCError as exc:
                print(f"Cannot read comments for post {post.id}: {type(exc).__name__}: {exc}")

            if posts_seen % 100 == 0:
                print(
                    f"Posts checked: {posts_seen}; "
                    f"comment threads: {threads_seen}; "
                    f"comments checked: {comments_seen}; "
                    f"unique users: {len(seen_users)}"
                )

        print(
            f"Finished. Posts checked: {posts_seen}; "
            f"comment threads: {threads_seen}; "
            f"comments checked: {comments_seen}"
        )
    finally:
        await client.disconnect()

    output_path = Path(args.output)
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["user_id", "access_hash", "username"]
        )
        writer.writeheader()
        writer.writerows(sorted(seen_users.values(), key=lambda row: row["user_id"]))

    print(f"Saved {len(seen_users)} users to {output_path}")

if __name__ == "__main__":
    asyncio.run(main())
