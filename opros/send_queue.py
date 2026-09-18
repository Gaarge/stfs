#!/usr/bin/env python3
"""Send a prepared Telegram message to recipients claimed from the registry.

The registry is the only source of recipients.  It atomically marks a record
as used before returning it, so two running clients cannot receive the same
Telegram account.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

try:
    from telethon import TelegramClient, errors, types
except ImportError as exc:
    raise SystemExit(
        "Install dependencies first: python -m pip install telethon python-dotenv"
    ) from exc


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_MESSAGE_FILE = BASE_DIR / "message.txt"
DEFAULT_CLAIMS_FILE = BASE_DIR / "registry_claims.jsonl"
DEFAULT_SENT_FILE = BASE_DIR / "registry_sent.jsonl"
DEFAULT_ERRORS_FILE = BASE_DIR / "registry_send_errors.jsonl"
DEFAULT_RECOVERY_FILE = BASE_DIR / "telegram_recovery.jsonl"
DEFAULT_REGISTRY_URL = "https://lbam.tech/username-registry"
DEFAULT_VIDEO_FILE = Path("/home/garg/Загрузки/промо_итог.mp4")
DEFAULT_VIDEO_THUMBNAIL = BASE_DIR.parent / "prev.jpg"
VIDEO_CACHE_DIR = BASE_DIR / "media-cache"
PROMO_TEMPLATE_CACHE_DIR = VIDEO_CACHE_DIR / "promo-templates"
DEFAULT_API_ID = "34825825"
DEFAULT_API_HASH = "60176f7ad0bcd77e63d4a64ca8d50a38"
MAX_VIDEO_CAPTION_LENGTH = 1024
SAME_RECIPIENT_RETRY_WAIT_SECONDS = 2
NEXT_RECIPIENT_COOLDOWN_SECONDS = 120
MAX_UNRECOVERED_FAILURE_CYCLES = 2
SENDER_NAME_PATTERN = re.compile(r"^sender([1-9][0-9]*)$", re.IGNORECASE)


class RegistryRequestError(RuntimeError):
    """A response from the recipient registry could not be used."""


class SendStageError(RuntimeError):
    """A Telegram operation failed after identifying whether text or video failed."""

    def __init__(self, stage: str, original: Exception) -> None:
        self.stage = stage
        self.original = original
        super().__init__(f"{stage}: {type(original).__name__}: {original}")


@dataclass(frozen=True)
class AccountConfig:
    account: str
    api_id: int
    api_hash: str
    phone: str
    session_path: Path


@dataclass(frozen=True)
class Lead:
    username: str | None
    user_id: int
    access_hash: int
    chat: str


@dataclass(frozen=True)
class PromoVideo:
    path: Path
    duration_seconds: int
    width: int
    height: int
    thumbnail: Path


@dataclass(frozen=True)
class PromoTemplate:
    """Existing Telegram media uploaded once by one sending account."""

    media: Any
    message_id: int
    fingerprint: str


@dataclass
class WorkerResult:
    account: str
    sent: int = 0
    permanently_failed: int = 0
    recovered: int = 0
    claimed: int = 0
    exhausted: bool = False
    disabled: bool = False
    registry_error: str | None = None


def load_env_files() -> list[Path]:
    """Load the closest useful .env files without overwriting shell variables."""
    if load_dotenv is None:
        return []

    loaded: list[Path] = []
    for candidate in (BASE_DIR / ".env", BASE_DIR.parent / ".env", Path.cwd() / ".env"):
        candidate = candidate.resolve()
        if candidate.exists() and candidate not in loaded:
            load_dotenv(candidate, override=False)
            loaded.append(candidate)
    return loaded


LOADED_ENV_FILES = load_env_files()


def normalize_account_name(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", value.strip()).strip("_").upper()
    return normalized or "SENDER"


def account_env(account: str, name: str) -> str | None:
    prefix = normalize_account_name(account)
    candidates = (
        f"TG_{prefix}_{name}",
        f"TELEGRAM_{prefix}_{name}",
        f"TG_{name}",
        f"TELEGRAM_{name}",
    )
    for candidate in candidates:
        value = os.getenv(candidate)
        if value:
            return value.strip()
    return None


def resolve_account(account: str) -> AccountConfig:
    api_id_raw = account_env(account, "API_ID") or DEFAULT_API_ID
    api_hash = account_env(account, "API_HASH") or DEFAULT_API_HASH
    phone = account_env(account, "PHONE") or ""
    session_raw = account_env(account, "SESSION")
    session_path = (
        Path(session_raw).expanduser()
        if session_raw
        else BASE_DIR / "sessions" / account
    )
    session_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        api_id = int(api_id_raw)
    except ValueError as exc:
        raise SystemExit("TG_API_ID must be an integer.") from exc

    if not api_hash:
        raise SystemExit("TG_API_HASH is not configured.")
    return AccountConfig(account, api_id, api_hash, phone, session_path)


async def login_with_qr(client: TelegramClient) -> None:
    qr_login = await client.qr_login()
    print("\nScan this QR code in Telegram (Settings → Devices → Link Desktop Device):\n")
    try:
        subprocess.run(
            ["qrencode", "-t", "ANSIUTF8", "-m", "1", "-o", "-", qr_login.url],
            check=True,
        )
    except FileNotFoundError:
        print("qrencode is not installed; use the Telegram link below instead.")
    except subprocess.CalledProcessError:
        print("Could not render a terminal QR code; use the Telegram link below instead.")
    print("\nTelegram login link (backup):")
    print(qr_login.url)
    print("Waiting up to 2 minutes for confirmation…")
    try:
        await qr_login.wait(timeout=120)
    except errors.SessionPasswordNeededError:
        password = getpass("Telegram two-factor password: ")
        await client.sign_in(password=password)
    except asyncio.TimeoutError as exc:
        raise SystemExit("QR login timed out. Run the command again for a new QR link.") from exc


async def ensure_authorized(client: TelegramClient, config: AccountConfig) -> None:
    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Telegram authorized as {getattr(me, 'username', None) or getattr(me, 'id', 'account')}.")
        return

    method = input("Telegram login method ([q]r / [p]hone): ").strip().lower() or "q"
    if method.startswith("q"):
        await login_with_qr(client)
        return

    phone = config.phone or input("Telegram phone number in international format: ").strip()
    if not phone:
        raise SystemExit("A phone number is required for phone login.")
    sent = await client.send_code_request(phone)
    code = input("Telegram login code: ").strip()
    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
    except errors.SessionPasswordNeededError:
        password = getpass("Telegram two-factor password: ")
        await client.sign_in(password=password)


def load_message(path: Path) -> str:
    path = path.expanduser().resolve()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    message = path.read_text(encoding="utf-8").strip()
    if not message:
        raise SystemExit(
            f"Message is empty. Put the prepared text into {path}; no registry record was claimed."
        )
    if len(message) > MAX_VIDEO_CAPTION_LENGTH:
        raise SystemExit(
            f"Message has {len(message)} characters, but a Telegram video caption may contain at most "
            f"{MAX_VIDEO_CAPTION_LENGTH}; no registry record was claimed."
        )
    return message


def load_video(path: Path, thumbnail_override: Path | None = None) -> PromoVideo:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise SystemExit(f"Promo video was not found: {path}; no registry record was claimed.")
    if path.stat().st_size == 0:
        raise SystemExit(f"Promo video is empty: {path}; no registry record was claimed.")
    try:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries",
                "stream=codec_type,width,height,duration:format=duration",
                "-of", "json", str(path),
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        details = json.loads(probe.stdout)
    except FileNotFoundError as exc:
        raise SystemExit("ffprobe is required to prepare the Telegram video preview.") from exc
    except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Could not read video metadata from {path}; no registry record was claimed.") from exc

    stream = next((item for item in details.get("streams", []) if item.get("codec_type") == "video"), None)
    if not stream:
        raise SystemExit(f"Promo file has no video stream: {path}; no registry record was claimed.")
    try:
        width = int(stream["width"])
        height = int(stream["height"])
        duration = float(details.get("format", {}).get("duration") or stream.get("duration"))
    except (KeyError, TypeError, ValueError) as exc:
        raise SystemExit(f"Promo video metadata is incomplete: {path}; no registry record was claimed.") from exc
    if width < 1 or height < 1 or not math.isfinite(duration) or duration <= 0:
        raise SystemExit(f"Promo video metadata is invalid: {path}; no registry record was claimed.")

    if thumbnail_override is not None:
        thumbnail = thumbnail_override.expanduser().resolve()
        if not thumbnail.is_file() or thumbnail.stat().st_size == 0:
            raise SystemExit(f"Video thumbnail was not found or is empty: {thumbnail}; no registry record was claimed.")
        return PromoVideo(path, math.ceil(duration), width, height, thumbnail)

    fingerprint = hashlib.sha256(
        f"{path}:{path.stat().st_size}:{path.stat().st_mtime_ns}".encode("utf-8")
    ).hexdigest()[:16]
    thumbnail = VIDEO_CACHE_DIR / f"{path.stem}-{fingerprint}.jpg"
    if not thumbnail.is_file() or thumbnail.stat().st_size == 0:
        VIDEO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = min(3.0, duration / 2)
        try:
            thumbnail_result = subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-n", "-ss", f"{timestamp:.3f}",
                    "-i", str(path), "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "3", str(thumbnail),
                ],
                check=False,
                text=True,
                capture_output=True,
            )
        except FileNotFoundError as exc:
            raise SystemExit("ffmpeg is required to create the Telegram video preview.") from exc
        if thumbnail_result.returncode and (not thumbnail.is_file() or thumbnail.stat().st_size == 0):
            raise SystemExit(f"Could not create video preview: {thumbnail_result.stderr.strip()}")
    return PromoVideo(path, math.ceil(duration), width, height, thumbnail)


def registry_request(registry_url: str, api_key: str, size: int) -> dict[str, Any]:
    url = registry_url.rstrip("/") + "/v1/claim-next"
    body = json.dumps({"n": size}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-API-Key": api_key,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8")
        except OSError:
            detail = ""
        raise RegistryRequestError(f"Registry returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RegistryRequestError(f"Cannot reach registry: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise RegistryRequestError("Registry returned invalid JSON.") from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise RegistryRequestError("Registry response has an unexpected format.")
    return payload


def parse_lead(record: dict[str, Any]) -> Lead:
    try:
        username_raw = record.get("username")
        username = str(username_raw).strip() if username_raw is not None else None
        username = username or None
        return Lead(
            username=username,
            user_id=int(record["user_id"]),
            access_hash=int(record["access_hash"]),
            chat=str(record.get("chat") or ""),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RegistryRequestError(f"Registry returned a malformed recipient: {record!r}") from exc


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def promo_template_fingerprint(video: PromoVideo) -> str:
    """Change the template whenever either the MP4 or its supplied cover changes."""
    return hashlib.sha256(
        f"video:{file_digest(video.path)}|thumbnail:{file_digest(video.thumbnail)}".encode("utf-8")
    ).hexdigest()


def promo_template_cache_path(config: AccountConfig) -> Path:
    return PROMO_TEMPLATE_CACHE_DIR / f"{config.account.casefold()}.json"


def read_template_message_id(cache_path: Path, fingerprint: str) -> int | None:
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        if payload.get("fingerprint") != fingerprint:
            return None
        message_id = int(payload["message_id"])
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        return None
    return message_id if message_id > 0 else None


def write_template_message_id(cache_path: Path, config: AccountConfig, fingerprint: str, message_id: int) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = cache_path.with_suffix(".tmp")
    temporary_path.write_text(
        json.dumps(
            {
                "account": config.account,
                "fingerprint": fingerprint,
                "message_id": message_id,
                "uploaded_at": utc_now(),
            },
            ensure_ascii=False,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(cache_path)


async def upload_video(client: TelegramClient, peer: Any, video: PromoVideo, caption: str) -> Any:
    """Upload the MP4 once, with its metadata and permanent cover."""
    attributes = [
        types.DocumentAttributeVideo(
            duration=video.duration_seconds,
            w=video.width,
            h=video.height,
            supports_streaming=True,
        )
    ]
    return await client.send_file(
        peer,
        video.path,
        attributes=attributes,
        thumb=video.thumbnail,
        caption=caption,
        mime_type="video/mp4",
        force_document=False,
        supports_streaming=True,
    )


async def prepare_promo_template(
    client: TelegramClient,
    config: AccountConfig,
    video: PromoVideo,
    cache_path: Path | None = None,
) -> PromoTemplate:
    """Return this account's reusable Telegram video, uploading only when needed."""
    fingerprint = promo_template_fingerprint(video)
    cache_path = cache_path or promo_template_cache_path(config)
    cached_message_id = read_template_message_id(cache_path, fingerprint)
    if cached_message_id is not None:
        try:
            cached_message = await client.get_messages("me", ids=cached_message_id)
            if cached_message and getattr(cached_message, "media", None):
                print(f"[{config.account}] Reusing cached Telegram promo video (Saved Messages #{cached_message_id}).")
                return PromoTemplate(cached_message.media, cached_message_id, fingerprint)
        except Exception as exc:
            print(f"[{config.account}] Cached promo video is unavailable, uploading it again: {exception_text(exc)}")

    print(f"[{config.account}] Uploading promo video once to Saved Messages; later sends will reuse it.")
    uploaded_message = await upload_video(client, "me", video, "")
    media = getattr(uploaded_message, "media", None)
    message_id = getattr(uploaded_message, "id", None)
    if not media or not isinstance(message_id, int) or message_id <= 0:
        raise RuntimeError("Telegram did not return media after uploading the promo template to Saved Messages.")
    write_template_message_id(cache_path, config, fingerprint, message_id)
    print(f"[{config.account}] Promo video cached in Saved Messages (#{message_id}).")
    return PromoTemplate(media, message_id, fingerprint)


async def send_template_video(client: TelegramClient, peer: Any, template: PromoTemplate, caption: str) -> None:
    """Send existing Telegram media without uploading the local MP4 again."""
    await client.send_file(
        peer,
        template.media,
        caption=caption,
        force_document=False,
        supports_streaming=True,
    )


async def send_one(client: TelegramClient, lead: Lead, message: str, template: PromoTemplate) -> None:
    """Send one video message with the prepared text as its caption."""
    peer: Any = types.InputPeerUser(lead.user_id, lead.access_hash)
    try:
        await send_template_video(client, peer, template, message)
    except (errors.PeerIdInvalidError, errors.InputUserDeactivatedError, ValueError) as exc:
        if not lead.username:
            raise SendStageError("video_with_caption", exc) from exc
        peer = lead.username
        try:
            await send_template_video(client, peer, template, message)
        except Exception as fallback_exc:
            raise SendStageError("video_with_caption", fallback_exc) from fallback_exc
    except Exception as exc:
        raise SendStageError("video_with_caption", exc) from exc


async def send_test_recipient(client: TelegramClient, username: str, message: str, template: PromoTemplate) -> None:
    """Send only to an explicit test username; never contacts the registry."""
    peer = username
    try:
        await send_template_video(client, peer, template, message)
    except Exception as exc:
        raise SendStageError("video_with_caption", exc) from exc


def exception_text(exc: Exception) -> str:
    if isinstance(exc, SendStageError):
        return f"{exc.stage}: {exception_text(exc.original)}"
    if isinstance(exc, errors.FloodWaitError):
        return f"Flood wait: Telegram requires a pause of {exc.seconds} seconds."
    if isinstance(exc, errors.PeerFloodError):
        return "Peer flood: Telegram temporarily restricted new outgoing messages."
    return f"{type(exc).__name__}: {exc}"


def normalize_test_username(value: str) -> str:
    username = value.strip()
    if username.startswith("@"):
        username = username[1:]
    if not username or len(username) > 128 or any(character.isspace() for character in username):
        raise SystemExit("--test-username must be one Telegram username, for example @example.")
    return username


def requested_account_names(args: argparse.Namespace) -> list[str]:
    """Return unique senderN names, where N is any positive integer."""
    names: list[str] = []
    if args.account:
        names.append(args.account.strip())
    for value in args.accounts:
        names.extend(part.strip() for part in value.split(","))
    names = [name for name in names if name]
    if not names:
        raise SystemExit("Specify at least one Telegram account: --account sender1 or --accounts sender1,sender2.")

    seen: set[str] = set()
    unique: list[str] = []
    for name in names:
        match = SENDER_NAME_PATTERN.fullmatch(name)
        if not match:
            raise SystemExit(
                f"Invalid Telegram account {name!r}. Name every sending account senderN, where N is a positive integer "
                "(for example sender1)."
            )
        key = name.casefold()
        if key in seen:
            raise SystemExit(f"Telegram account {name!r} was listed more than once.")
        seen.add(key)
        unique.append(name)
    return unique


def send_error_payload(exc: Exception) -> tuple[str, str]:
    stage = exc.stage if isinstance(exc, SendStageError) else "unknown"
    return stage, exception_text(exc)


async def notify_spam_bot(
    config: AccountConfig,
    client: TelegramClient,
    record: dict[str, Any],
    recovery_path: Path,
) -> bool:
    """Ask Telegram's SpamBot about the current account's restriction state."""
    append_jsonl(
        recovery_path,
        {"at": utc_now(), "event": "spam_bot_start_requested", "account": config.account, "record": record},
    )
    try:
        await client.send_message("SpamBot", "/start")
    except Exception as exc:
        append_jsonl(
            recovery_path,
            {
                "at": utc_now(), "event": "spam_bot_start_failed", "account": config.account,
                "record": record, "error": exception_text(exc),
            },
        )
        print(f"[{config.account}] Could not send /start to @SpamBot: {exception_text(exc)}")
        return False
    append_jsonl(
        recovery_path,
        {"at": utc_now(), "event": "spam_bot_start_sent", "account": config.account, "record": record},
    )
    print(
        f"[{config.account}] Sent /start to @SpamBot; retrying the same recipient "
        f"in {SAME_RECIPIENT_RETRY_WAIT_SECONDS} seconds."
    )
    return True


async def recover_after_telegram_error(
    config: AccountConfig,
    client: TelegramClient,
    record: dict[str, Any],
    lead: Lead,
    message: str,
    template: PromoTemplate,
    consecutive_unrecovered_cycles: int,
    sent_path: Path,
    errors_path: Path,
    recovery_path: Path,
    retry_wait_seconds: float = SAME_RECIPIENT_RETRY_WAIT_SECONDS,
    next_recipient_wait_seconds: float = NEXT_RECIPIENT_COOLDOWN_SECONDS,
) -> tuple[bool, bool, int]:
    """Contact SpamBot, retry in two seconds, then cool down before the next lead."""
    cycle = consecutive_unrecovered_cycles + 1
    spam_bot_succeeded = await notify_spam_bot(config, client, record, recovery_path)
    if not spam_bot_succeeded:
        disabled = cycle >= MAX_UNRECOVERED_FAILURE_CYCLES
        if disabled:
            append_jsonl(
                recovery_path,
                {
                    "at": utc_now(), "event": "account_disabled", "account": config.account, "record": record,
                    "reason": "two_unrecovered_spam_bot_start_failures", "cycles": cycle,
                },
            )
            print(f"[{config.account}] Disabled after {cycle} unrecovered Telegram error cycles.")
            return False, True, cycle
        append_jsonl(
            recovery_path,
            {
                "at": utc_now(), "event": "next_recipient_cooldown_started", "account": config.account,
                "record": record, "cycle": cycle, "seconds": next_recipient_wait_seconds,
                "reason": "spam_bot_start_failed",
            },
        )
        await asyncio.sleep(next_recipient_wait_seconds)
        return False, False, cycle

    append_jsonl(
        recovery_path,
        {
            "at": utc_now(), "event": "same_recipient_retry_wait_started", "account": config.account,
            "record": record, "cycle": cycle, "seconds": retry_wait_seconds,
        },
    )
    await asyncio.sleep(retry_wait_seconds)
    try:
        await send_one(client, lead, message, template)
    except Exception as retry_exc:
        stage, error = send_error_payload(retry_exc)
        next_cycles = cycle
        append_jsonl(
            errors_path,
            {
                "at": utc_now(), "account": config.account, "record": record, "attempt": "retry",
                "cycle": cycle, "stage": stage, "error": error,
            },
        )
        append_jsonl(
            recovery_path,
            {
                "at": utc_now(), "event": "retry_failed", "account": config.account, "record": record,
                "cycle": cycle, "stage": stage, "error": error,
            },
        )
        disabled = next_cycles >= MAX_UNRECOVERED_FAILURE_CYCLES
        if disabled:
            append_jsonl(
                recovery_path,
                {
                    "at": utc_now(), "event": "account_disabled", "account": config.account, "record": record,
                    "reason": "two_unrecovered_telegram_error_cycles", "cycles": next_cycles,
                },
            )
            print(f"[{config.account}] Disabled after {next_cycles} unrecovered Telegram error cycles.")
        else:
            append_jsonl(
                recovery_path,
                {
                    "at": utc_now(), "event": "next_recipient_cooldown_started", "account": config.account,
                    "record": record, "cycle": cycle, "seconds": next_recipient_wait_seconds,
                    "reason": "same_recipient_retry_failed",
                },
            )
            print(
                f"[{config.account}] Retry failed; this recipient was logged. "
                f"Waiting {next_recipient_wait_seconds} seconds before the next registry record."
            )
            await asyncio.sleep(next_recipient_wait_seconds)
        return False, disabled, next_cycles

    append_jsonl(
        sent_path,
        {
            "sent_at": utc_now(), "account": config.account, "record": record, "attempt": "retry",
            "stages": ["video_with_caption"],
        },
    )
    append_jsonl(
        recovery_path,
        {"at": utc_now(), "event": "retry_succeeded", "account": config.account, "record": record, "cycle": cycle},
    )
    print(f"[{config.account}] Retry succeeded for {lead.username or lead.user_id}.")
    return True, False, 0


async def run_account_worker(
    config: AccountConfig,
    client: TelegramClient,
    registry_url: str,
    api_key: str,
    message: str,
    template: PromoTemplate,
    delay: float,
    max_per_run: int | None,
    claims_path: Path,
    sent_path: Path,
    errors_path: Path,
    recovery_path: Path,
) -> WorkerResult:
    """Claim one record at a time and keep this account independent from others."""
    result = WorkerResult(account=config.account)
    unrecovered_cycles = 0
    while max_per_run is None or result.claimed < max_per_run:
        try:
            payload = await asyncio.to_thread(registry_request, registry_url, api_key, 1)
        except RegistryRequestError as exc:
            result.registry_error = str(exc)
            append_jsonl(
                recovery_path,
                {"at": utc_now(), "event": "registry_error", "account": config.account, "error": str(exc)},
            )
            print(f"[{config.account}] Registry error: {exc}")
            return result

        records = payload["items"]
        if not records:
            result.exhausted = True
            append_jsonl(recovery_path, {"at": utc_now(), "event": "registry_exhausted", "account": config.account})
            print(f"[{config.account}] The registry has no unused recipients left.")
            return result

        record = records[0]
        try:
            lead = parse_lead(record)
        except RegistryRequestError as exc:
            result.permanently_failed += 1
            append_jsonl(
                errors_path,
                {"at": utc_now(), "account": config.account, "record": record, "attempt": "claim", "stage": "malformed_record", "error": str(exc)},
            )
            continue

        result.claimed += 1
        append_jsonl(
            claims_path,
            {"claimed_at": utc_now(), "account": config.account, "queue_index": result.claimed, "record": record},
        )
        try:
            await send_one(client, lead, message, template)
        except Exception as first_exc:
            stage, error = send_error_payload(first_exc)
            append_jsonl(
                recovery_path,
                {
                    "at": utc_now(), "event": "first_send_failed", "account": config.account, "record": record,
                    "cycle": unrecovered_cycles + 1, "stage": stage, "error": error,
                },
            )
            print(f"[{config.account}] Telegram error for {lead.username or lead.user_id}: {error}")
            sent, disabled, unrecovered_cycles = await recover_after_telegram_error(
                config, client, record, lead, message, template, unrecovered_cycles,
                sent_path, errors_path, recovery_path,
            )
            if sent:
                result.sent += 1
                result.recovered += 1
            else:
                result.permanently_failed += 1
            if disabled:
                result.disabled = True
                return result
            if not sent:
                # The 120-second recovery cooldown has already completed.
                # Claim the next VPS record immediately, without adding the
                # normal between-send delay a second time.
                continue
        else:
            # A normal successful send starts a fresh error sequence. A later
            # Telegram error must not be treated as a continuation of an old,
            # already recovered failure.
            unrecovered_cycles = 0
            result.sent += 1
            append_jsonl(
                sent_path,
                {
                    "sent_at": utc_now(), "account": config.account, "queue_index": result.claimed,
                    "record": record, "attempt": "first", "stages": ["video_with_caption"],
                },
            )
            print(f"[{config.account}][{result.claimed}] Sent to {lead.username or lead.user_id}.")

        if delay and (max_per_run is None or result.claimed < max_per_run):
            await asyncio.sleep(delay)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Claim recipients from username-registry and send one prepared Telegram message."
    )
    parser.add_argument(
        "--registry-url",
        default=os.getenv("REGISTRY_URL", DEFAULT_REGISTRY_URL),
        help="Registry base URL (default: REGISTRY_URL or lbam.tech endpoint).",
    )
    parser.add_argument("--max-per-run", type=int, help="Optional safety cap of records per account; omit for continuous work until the registry is empty.")
    parser.add_argument("--account", help="One account name in the senderN format, for example sender1.")
    parser.add_argument(
        "--accounts",
        action="append",
        default=[],
        metavar="NAME[,NAME...]",
        help="One or more senderN accounts; may be passed repeatedly. Example: --accounts sender1,sender2,sender3.",
    )
    parser.add_argument("--message-file", type=Path, default=DEFAULT_MESSAGE_FILE, help="Prepared message file.")
    parser.add_argument(
        "--video-file",
        type=Path,
        default=Path(os.getenv("PROMO_VIDEO_FILE", str(DEFAULT_VIDEO_FILE))),
        help="MP4 sent as one message with the prepared text as caption.",
    )
    parser.add_argument(
        "--video-thumbnail",
        type=Path,
        default=Path(os.getenv("PROMO_VIDEO_THUMBNAIL", str(DEFAULT_VIDEO_THUMBNAIL))),
        help="JPG/PNG cover shown by Telegram before download (default: PROMO_VIDEO_THUMBNAIL or ../prev.jpg).",
    )
    parser.add_argument(
        "--test-username",
        metavar="USERNAME",
        help="Send one video-with-caption message only to this username; does not claim a registry record or require REGISTRY_API_KEY.",
    )
    parser.add_argument("--claims-file", type=Path, default=DEFAULT_CLAIMS_FILE, help="Audit log for claimed records.")
    parser.add_argument("--sent-file", type=Path, default=DEFAULT_SENT_FILE, help="Audit log for successful sends.")
    parser.add_argument("--errors-file", type=Path, default=DEFAULT_ERRORS_FILE, help="Audit log for send errors.")
    parser.add_argument("--recovery-file", type=Path, default=DEFAULT_RECOVERY_FILE, help="Detailed Telegram recovery log JSONL.")
    parser.add_argument("--delay", type=float, default=float(os.getenv("SEND_DELAY_SECONDS", "60")), help="Pause in seconds between sends (default: 60).")
    parser.add_argument("--yes", action="store_true", help="Send immediately after the claim; skip confirmation.")
    parser.add_argument("--dry-run", action="store_true", help="Validate the message and configuration without claiming or sending.")
    return parser


async def run(args: argparse.Namespace) -> int:
    account_names = requested_account_names(args)
    test_username = normalize_test_username(args.test_username) if args.test_username else None
    if test_username and len(account_names) != 1:
        raise SystemExit("--test-username requires exactly one Telegram account.")
    if args.max_per_run is not None and args.max_per_run < 1:
        raise SystemExit("--max-per-run must be positive.")
    if args.delay < 0:
        raise SystemExit("--delay cannot be negative.")

    message = load_message(args.message_file)
    video = load_video(args.video_file, args.video_thumbnail)
    print(
        f"Prepared message: {len(message)} characters. "
        f"Video: {video.path} ({video.duration_seconds}s, {video.width}x{video.height}); "
        f"preview: {video.thumbnail}."
    )
    print(f"Telegram accounts ({len(account_names)}): {', '.join(account_names)}.")
    print("Mode: continuous one-by-one claims until the registry is empty or all selected accounts are disabled.")
    if args.max_per_run is not None:
        print(f"Safety cap: {args.max_per_run} claimed record(s) per account.")
    if args.dry_run:
        print("Dry run: no registry record was claimed and no Telegram video-with-caption was sent.")
        return 0

    api_key = os.getenv("REGISTRY_API_KEY", "").strip()
    if not test_username and not api_key:
        raise SystemExit("REGISTRY_API_KEY is not set. Put it in the shell or a local .env file.")

    configs = [resolve_account(account_name) for account_name in account_names]
    clients: list[tuple[AccountConfig, TelegramClient]] = []
    try:
        for config in configs:
            client = TelegramClient(str(config.session_path), config.api_id, config.api_hash)
            await client.connect()
            clients.append((config, client))
            print(f"Authorizing Telegram account {config.account}…")
            await ensure_authorized(client, config)

        if test_username:
            config, client = clients[0]
            print(f"TEST MODE: registry is not contacted; one video-with-caption will be sent only to @{test_username}.")
            if not args.yes:
                confirmation = input(f"Send the prepared video with text caption only to @{test_username}? [y/N]: ").strip().lower()
                if confirmation not in {"y", "yes", "д", "да"}:
                    print("Cancelled. The registry was not contacted.")
                    return 0
            record = {"username": test_username, "source": "test_username"}
            try:
                template = await prepare_promo_template(client, config, video)
                await send_test_recipient(client, test_username, message, template)
            except Exception as exc:
                stage = exc.stage if isinstance(exc, SendStageError) else "unknown"
                append_jsonl(
                    args.errors_file,
                    {"at": utc_now(), "account": config.account, "test_mode": True, "record": record, "stage": stage, "error": exception_text(exc)},
                )
                print(f"[test][{config.account}] Failed for @{test_username}: {exception_text(exc)}")
                return 1
            append_jsonl(
                args.sent_file,
                {"sent_at": utc_now(), "account": config.account, "test_mode": True, "record": record, "stages": ["video_with_caption"]},
            )
            print(f"[test][{config.account}] Video with text caption sent to @{test_username}.")
            return 0

        if not args.yes:
            confirmation = input("Start continuous sending? Each account will claim one record at a time. [y/N]: ").strip().lower()
            if confirmation not in {"y", "yes", "д", "да"}:
                print("Cancelled. The registry was not contacted.")
                return 0

        templates: list[tuple[AccountConfig, TelegramClient, PromoTemplate]] = []
        for config, client in clients:
            try:
                template = await prepare_promo_template(client, config, video)
            except Exception as exc:
                print(f"[{config.account}] Could not prepare reusable promo video: {exception_text(exc)}")
                return 1
            templates.append((config, client, template))

        results = await asyncio.gather(
            *(
                run_account_worker(
                    config, client, args.registry_url, api_key, message, template, args.delay, args.max_per_run,
                    args.claims_file, args.sent_file, args.errors_file, args.recovery_file,
                )
                for config, client, template in templates
            )
        )
        sent = sum(result.sent for result in results)
        failed = sum(result.permanently_failed for result in results)
        recovered = sum(result.recovered for result in results)
        claimed = sum(result.claimed for result in results)
        per_account = "; ".join(
            f"{result.account}: sent={result.sent}, recovered={result.recovered}, failed={result.permanently_failed}, "
            f"claimed={result.claimed}, status={'disabled' if result.disabled else 'empty' if result.exhausted else 'registry_error' if result.registry_error else 'cap_reached'}"
            for result in results
        )
        print(f"Finished. Sent: {sent}; recovered after retry: {recovered}; failed: {failed}; claimed: {claimed}.")
        print(f"Per account: {per_account}")
        return 1 if any(result.registry_error for result in results) or all(result.disabled for result in results) else 0
    finally:
        await asyncio.gather(*(client.disconnect() for _, client in clients), return_exceptions=True)


def main() -> int:
    return asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
