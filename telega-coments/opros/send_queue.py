import argparse
import asyncio
import csv
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

try:
    from telethon import TelegramClient, errors, functions, types
except ImportError as exc:
    raise SystemExit(
        "Missing dependency. Install requirements first:\n"
        "  python -m pip install -r requirements.txt"
    ) from exc

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError as exc:
    raise SystemExit(
        "Missing dependency. Install requirements first:\n"
        "  python -m pip install -r requirements.txt"
    ) from exc


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_QUEUE = BASE_DIR / "leads_queue.txt"
DEFAULT_PROCESSED = BASE_DIR / "leads_queue_processed.txt"
DEFAULT_MESSAGE_FILE = BASE_DIR / "message.txt"
DEFAULT_ERRORS_FILE = BASE_DIR / "send_errors.jsonl"
DEFAULT_API_ID = "34825825"
DEFAULT_API_HASH = "60176f7ad0bcd77e63d4a64ca8d50a38"
DEFAULT_OPENROUTER_MODEL = "google/gemini-3-flash-preview"
DEFAULT_TELEGRAM_RETRY_SLEEP_SECONDS = 4 * 60


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

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")


class TelegramSendRetryFailed(RuntimeError):
    def __init__(self, original: Exception) -> None:
        super().__init__(str(original))
        self.original = original


class RecipientNotResolved(RuntimeError):
    def __init__(self, original: Exception) -> None:
        super().__init__(str(original))
        self.original = original


@dataclass
class AccountConfig:
    account: str
    api_id: int
    api_hash: str
    phone: str
    session_path: Path


@dataclass
class Lead:
    line_no: int
    raw: str
    user_id: int | None = None
    access_hash: int | None = None
    username: str = ""
    phone: str = ""
    site: str = ""
    excel_row: int | None = None


def normalize_account_name(account: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", account.strip())
    return cleaned or "default"


def env_for_account(account: str, key: str) -> str:
    prefix = normalize_account_name(account).upper()
    candidates = [
        f"TG_{prefix}_{key}",
        f"TELEGRAM_{prefix}_{key}",
    ]
    if account.lower() in {"sender", "search", "comments"}:
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


def normalize_cell(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_username(value: str) -> str:
    username = normalize_cell(value)
    return username[1:] if username.startswith("@") else username


def safe_int(value) -> int | None:
    value = normalize_cell(value).replace("'", "")
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def normalize_phone(value: str) -> str:
    phone = normalize_cell(value)
    if not phone:
        return ""
    digits = re.sub(r"\D", "", phone)
    if not digits:
        return ""
    if len(digits) == 11 and digits.startswith("8"):
        return "+7" + digits[1:]
    if len(digits) == 11 and digits.startswith("7"):
        return "+" + digits
    if len(digits) == 10:
        return "+7" + digits
    if phone.startswith("+"):
        return "+" + digits
    return "+" + digits


def normalize_url(raw_url: str) -> str:
    url = normalize_cell(raw_url)
    if not url:
        return ""
    if not re.match(r"^https?://", url, flags=re.IGNORECASE):
        url = "https://" + url
    return url


def looks_like_phone(value: str) -> bool:
    return bool(re.fullmatch(r"\+?\d[\d\s\-()]{8,20}", normalize_cell(value)))


def fetch_site_text(url: str, max_chars: int = 8000) -> str:
    normalized_url = normalize_url(url)
    if not normalized_url:
        return ""

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; SiteAnalyzer/1.0)",
    }

    print(f"[SITE] Загружаю сайт: {normalized_url}")
    response = requests.get(normalized_url, headers=headers, timeout=15)
    response.raise_for_status()
    print(f"[SITE] HTTP статус: {response.status_code}")
    print(f"[SITE] Content-Type: {response.headers.get('Content-Type', 'не указан')}")

    if response.apparent_encoding:
        response.encoding = response.apparent_encoding

    soup = BeautifulSoup(response.text, "html.parser")

    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    title = soup.title.string.strip() if soup.title and soup.title.string else ""

    meta_description = ""
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        meta_description = meta["content"].strip()

    text = soup.get_text(separator=" ")
    text = re.sub(r"\s+", " ", text).strip()

    result = f"""
URL: {normalized_url}
Title: {title}
Meta description: {meta_description}
Page text: {text}
""".strip()

    print(f"[SITE] Title: {title!r}")
    print(f"[SITE] Meta description: {meta_description!r}")
    print(f"[SITE] Длина очищенного текста до обрезки: {len(result)} символов")
    print(f"[SITE] В нейросеть уйдёт максимум: {max_chars} символов")

    return result[:max_chars]


def generate_pitch(site_text: str = "", model: str = DEFAULT_OPENROUTER_MODEL) -> str:
    prompt = f"""
Ты пишешь короткое рекламное сообщение потенциальному клиенту от веб-разработчика.

Ситуация:

* Мы не знаем точно, есть ли у клиента сайт.
* Сообщение должно одинаково хорошо подходить и тем, у кого сайта пока нет, и тем, у кого сайт уже есть.
* Не пиши фразы вроде: "я нашёл ваш сайт", "увидел ваш сайт", "наткнулся на ваш сайт", "у вас нет сайта", "ваш сайт плохой".
* Не утверждай то, чего мы точно не знаем.
* Можно использовать нейтральную формулировку: "если сайта пока нет — можно сделать его с нуля, если сайт уже есть — можно обновить его и доработать".

Стиль:

* Пиши просто, по-человечески.
* Без пафоса.
* Без слов: "статусный", "экспертиза", "упаковать", "презентабельный", "визуальная форма", "достойный", "серьезные клиенты".
* Не используй канцелярит.
* Не пиши слишком вежливо и корпоративно.
* Тон: прямой, уверенный, спокойный.
* Максимум 6-7 предложений.
* Ни слова не говори про цену.
* Обязательно поздоровайся.
* Прощаться не надо.

Что нужно сказать:

1. Меня зовут Владислав.
2. Я уже несколько лет разрабатываю сайты.
3. Также занимаюсь SEO-анализом — то есть слежу за тем, чтобы сайт могли увидеть как можно больше целевых клиентов.
4. Скажи, что я могу сделать сайт с нуля или обновить уже существующий.
5. Сайт должен быть красивым, понятным, современным и удобным для людей.
6. Хороший сайт помогает бизнесу выглядеть понятнее, вызывает больше доверия и помогает получать больше заявок.
7. Напиши уверенно, но честно: "сайт будет сделан так, чтобы он чаще попадал на первые страницы поиска".
8. Объясни, что чем чаще сайт появляется на первых страницах поиска, тем больше людей его видят, а значит у бизнеса может быть больше клиентов.
9. Упомяни, что я разрабатывал сайты для учебных заведений, адвокатских контор и интернет-магазинов.
10. Напиши, что если нужно, могу прислать свои работы.
11. Напиши, что работаю по договору.
12. Не дави на человека и не пугай его потерей клиентов.

Данные о бизнесе клиента, если они есть, чтобы аккуратно подстроить сообщение под сферу:
{site_text}

Выдай только готовое сообщение клиенту, без пояснений.
""".strip()

    if not OPENROUTER_API_KEY:
        raise RuntimeError(
            "Не найден OPENROUTER_API_KEY. Добавь его в .env или экспортируй переменную окружения."
        )

    print("[AI] Отправляю запрос в OpenRouter.")
    print(f"[AI] Модель: {model}")
    print(f"[AI] Длина промта: {len(prompt)} символов")

    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://example.com",
            "X-Title": "Site Pitch Generator",
        },
        json={
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
            "temperature": 0.7,
            "max_tokens": 300,
        },
        timeout=60,
    )

    print(f"[AI] HTTP статус OpenRouter: {response.status_code}")

    if response.status_code != 200:
        raise RuntimeError(f"OpenRouter error {response.status_code}: {response.text}")

    data = response.json()
    message = data["choices"][0]["message"]["content"].strip()
    print(f"[AI] Ответ получен. Длина сообщения: {len(message)} символов")
    return message


def build_site_fallback_text(lead: "Lead") -> str:
    return f"""
URL: {normalize_url(lead.site)}
Title:
Meta description:
Page text: Информации со страницы мало. Напиши сообщение в общем стиле: сайт выглядит слабым, его можно сделать понятнее, современнее и лучше подготовить под поиск.
""".strip()


def build_openrouter_message(lead: "Lead", max_site_chars: int, model: str) -> str:
    site_text = ""

    if lead.site:
        try:
            site_text = fetch_site_text(lead.site, max_chars=max_site_chars)
            print("[OK] Сайт успешно загружен.")
            print(f"[SITE] Длина текста сайта для нейросети: {len(site_text)} символов")
        except requests.RequestException as exc:
            print("[WARNING] Сайт не удалось загрузить, но лид НЕ будет пропущен.")
            print(f"          Строка очереди: {lead.line_no}")
            print(f"          Сайт: {lead.site}")
            print(f"          Тип ошибки: {type(exc).__name__}")
            print("          В нейросеть НЕ передаю текст ошибки, чтобы она не писала клиенту про недоступность сайта.")
            site_text = build_site_fallback_text(lead)
    else:
        print("[SITE][WARN] В строке очереди нет сайта. Сгенерирую общее сообщение без данных о бизнесе.")

    return generate_pitch(site_text, model=model)


def parse_json_lead(data: dict, line_no: int, raw: str) -> Lead:
    return Lead(
        line_no=line_no,
        raw=raw,
        user_id=safe_int(data.get("user_id") or data.get("telegram_user_id")),
        access_hash=safe_int(data.get("access_hash") or data.get("telegram_access_hash")),
        username=normalize_username(data.get("username") or data.get("telegram_username") or ""),
        phone=normalize_phone(data.get("phone") or data.get("telephone") or ""),
        site=normalize_cell(data.get("site")),
        excel_row=safe_int(data.get("excel_row")),
    )


def parse_csv_lead(parts: list[str], line_no: int, raw: str) -> Lead | None:
    cleaned = [normalize_cell(part) for part in parts if normalize_cell(part)]
    if len(cleaned) < 2:
        return None

    phone = ""
    if cleaned and looks_like_phone(cleaned[-1]):
        phone = normalize_phone(cleaned.pop())

    user_id = safe_int(cleaned[0]) if cleaned else None
    access_hash = safe_int(cleaned[1]) if len(cleaned) > 1 else None
    username = normalize_username(cleaned[2]) if len(cleaned) > 2 else ""

    return Lead(
        line_no=line_no,
        raw=raw,
        user_id=user_id,
        access_hash=access_hash,
        username=username,
        phone=phone,
    )


def parse_queue_line(line: str, line_no: int) -> Lead | None:
    raw = line.strip()
    if not raw or raw.startswith("#"):
        return None

    if raw.startswith("{"):
        try:
            return parse_json_lead(json.loads(raw), line_no, raw)
        except json.JSONDecodeError:
            print(f"[QUEUE][WARN] Bad JSON at line {line_no}, skipping.")
            return None

    try:
        parts = next(csv.reader([raw]))
    except csv.Error:
        parts = []
    if len(parts) <= 1:
        if "|" in raw:
            parts = raw.split("|")
        elif ";" in raw:
            parts = raw.split(";")

    return parse_csv_lead(parts, line_no, raw)


def read_queue(path: Path) -> list[Lead]:
    if not path.exists():
        path.touch()
        return []

    leads = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            lead = parse_queue_line(line, line_no)
            if lead:
                leads.append(lead)
    return leads


def lead_processed_keys(lead: Lead) -> set[str]:
    if lead.phone:
        return {f"phone:{lead.phone}"}

    keys = set()
    if lead.user_id:
        keys.add(f"user_id:{lead.user_id}")
    if lead.access_hash:
        keys.add(f"access_hash:{lead.access_hash}")
    if lead.username:
        keys.add(f"username:{lead.username.lower()}")
    return keys


def processed_keys_from_line(line: str) -> set[str]:
    line = line.strip()
    if not line:
        return set()

    keys = {line}
    if line.startswith("{"):
        try:
            lead = parse_json_lead(json.loads(line), 0, line)
        except json.JSONDecodeError:
            return keys
        keys.update(lead_processed_keys(lead))
        return keys

    parts = line.split("|")
    if len(parts) >= 2 and looks_like_phone(parts[1]):
        keys.add(f"phone:{normalize_phone(parts[1])}")

    csv_lead = parse_queue_line(line, 0)
    if csv_lead:
        keys.update(lead_processed_keys(csv_lead))
    return keys


def load_processed_keys(path: Path) -> set[str]:
    if not path.exists():
        path.touch()
        return set()

    keys = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        keys.update(processed_keys_from_line(line))
    return keys


def append_processed(path: Path, lead: Lead, message: str) -> None:
    record = {
        "status": "sent",
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "queue_line": lead.line_no,
        "user_id": lead.user_id,
        "access_hash": lead.access_hash,
        "username": lead.username or None,
        "phone": lead.phone or None,
        "site": lead.site or None,
        "excel_row": lead.excel_row,
        "message_len": len(message),
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()


def append_error(path: Path, lead: Lead, exc: Exception) -> None:
    record = {
        "error_at": datetime.now(timezone.utc).isoformat(),
        "queue_line": lead.line_no,
        "user_id": lead.user_id,
        "access_hash": lead.access_hash,
        "username": lead.username or None,
        "phone": lead.phone or None,
        "site": lead.site or None,
        "excel_row": lead.excel_row,
        "error_type": type(exc).__name__,
        "error": str(exc),
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()


def error_classes(*names):
    return tuple(cls for cls in (getattr(errors, name, None) for name in names) if cls)


CRITICAL_ERRORS = error_classes(
    "FloodWaitError",
    "PeerFloodError",
    "PhoneNumberBannedError",
    "UserDeactivatedBanError",
    "UserDeactivatedError",
    "AuthKeyInvalidError",
    "AuthKeyUnregisteredError",
)
RECIPIENT_ERRORS = error_classes(
    "UserPrivacyRestrictedError",
    "UserIsBlockedError",
    "InputUserDeactivatedError",
    "UsernameInvalidError",
    "UsernameNotOccupiedError",
    "PeerIdInvalidError",
)


def describe_telegram_error(exc: Exception) -> str:
    if isinstance(exc, getattr(errors, "FloodWaitError", ())):
        return f"Telegram flood wait. Wait {exc.seconds} seconds."
    if isinstance(exc, getattr(errors, "PeerFloodError", ())):
        return "Telegram limited this account for too many outgoing actions."
    if isinstance(exc, error_classes("PhoneNumberBannedError", "UserDeactivatedBanError", "UserDeactivatedError")):
        return "Telegram account/phone looks banned or deactivated."
    if isinstance(exc, RECIPIENT_ERRORS):
        return "Recipient cannot be messaged or resolved."
    return f"Telegram error: {type(exc).__name__}: {exc}"


def describe_error(exc: Exception) -> str:
    if isinstance(exc, TelegramSendRetryFailed):
        return describe_telegram_error(exc.original)
    if isinstance(exc, RecipientNotResolved):
        return f"Recipient cannot be resolved: {type(exc.original).__name__}: {exc.original}"
    if isinstance(exc, requests.RequestException):
        return f"HTTP request error: {type(exc).__name__}: {exc}"
    if isinstance(exc, RuntimeError) and "OpenRouter" in str(exc):
        return str(exc)
    return describe_telegram_error(exc)


def load_static_message(args) -> str:
    if args.message:
        return args.message.strip()

    message_path = Path(args.message_file)
    if not message_path.exists():
        message_path.touch()
    message = message_path.read_text(encoding="utf-8").strip()
    if not message:
        raise SystemExit(f"Static message is empty. Fill {message_path} or pass --message.")
    return message


def is_recipient_resolution_error(exc: Exception) -> bool:
    if isinstance(exc, RECIPIENT_ERRORS):
        return True
    return isinstance(exc, ValueError) and "entity" in str(exc).lower()


async def try_send_to_peer(client: TelegramClient, label: str, peer, message: str) -> None:
    print(f"[TG] Пробую отправить через {label}.")
    await client.send_message(peer, message)
    print(f"[TG] Отправлено через {label}.")


async def import_user_by_phone(client: TelegramClient, phone: str):
    if not phone:
        return None

    print(f"[TG] Пробую найти получателя с аккаунта-отправителя по телефону: {phone}")
    contact = types.InputPhoneContact(
        client_id=random.randrange(1, 10_000_000),
        phone=phone,
        first_name="Temporary",
        last_name="Contact",
    )
    result = await client(functions.contacts.ImportContactsRequest([contact]))
    if not result.users:
        print("[TG] По телефону пользователь не найден или скрыт настройками приватности.")
        return None
    user = result.users[0]
    print(f"[TG] Получатель найден по телефону: id={getattr(user, 'id', None)} username={getattr(user, 'username', None) or '-'}")
    return user


async def cleanup_imported_contact(client: TelegramClient, user) -> None:
    try:
        await client(functions.contacts.DeleteContactsRequest(id=[user]))
        print("[TG] Временный контакт удалён.")
    except Exception as exc:
        print(f"[TG][WARN] Не удалось удалить временный контакт: {type(exc).__name__}: {exc}")


async def send_one(client: TelegramClient, lead: Lead, message: str) -> None:
    attempts = []
    if lead.username:
        attempts.append((f"username @{lead.username}", lead.username))
    if lead.user_id and lead.access_hash:
        attempts.append(
            (
                "user_id/access_hash из очереди",
                types.InputPeerUser(user_id=int(lead.user_id), access_hash=int(lead.access_hash)),
            )
        )
    if lead.user_id:
        attempts.append(("user_id из очереди", int(lead.user_id)))

    last_exc: Exception | None = None
    for label, peer in attempts:
        try:
            await try_send_to_peer(client, label, peer, message)
            return
        except Exception as exc:
            if not is_recipient_resolution_error(exc):
                raise
            last_exc = exc
            print(f"[TG][WARN] Не получилось через {label}: {type(exc).__name__}: {exc}")

    imported_user = None
    if lead.phone:
        try:
            imported_user = await import_user_by_phone(client, lead.phone)
            if imported_user:
                await try_send_to_peer(client, "телефон, импортированный этим аккаунтом", imported_user, message)
                return
        except Exception as exc:
            if not is_recipient_resolution_error(exc):
                raise
            last_exc = exc
            print(f"[TG][WARN] Не получилось через телефон: {type(exc).__name__}: {exc}")
        finally:
            if imported_user is not None:
                await cleanup_imported_contact(client, imported_user)

    if last_exc is not None:
        raise RecipientNotResolved(last_exc)
    raise RecipientNotResolved(RuntimeError("Queue row has no user_id/access_hash/username/phone to send to."))


def resolve_clicker_script(script_arg: str = "") -> Path | None:
    candidates = []
    if script_arg:
        candidates.append(Path(script_arg))
    candidates.extend(
        [
            BASE_DIR / "start_clicker.sh",
            BASE_DIR.parent / "start_clicker.sh",
            BASE_DIR.parent.parent / "start_clicker.sh",
            BASE_DIR.parent.parent / "pipline" / "start_clicker.sh",
            Path.cwd() / "start_clicker.sh",
            Path.cwd() / "pipline" / "start_clicker.sh",
        ]
    )

    seen = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.exists():
            return resolved
    return None


def run_clicker_script(script_arg: str = "") -> None:
    script_path = resolve_clicker_script(script_arg)
    if script_path is None:
        print("[TG][WARN] start_clicker.sh не найден. Пауза и повторная попытка всё равно будут выполнены.")
        return

    print(f"[TG][SCRIPT] Запускаю: {script_path}")
    try:
        completed = subprocess.run(["bash", str(script_path)], check=False)
    except Exception as exc:
        print(f"[TG][WARN] Не удалось запустить start_clicker.sh: {type(exc).__name__}: {exc}")
        return

    if completed.returncode != 0:
        print(f"[TG][WARN] start_clicker.sh завершился с кодом {completed.returncode}.")


async def send_one_with_telegram_retry(client: TelegramClient, lead: Lead, message: str, args) -> None:
    try:
        await send_one(client, lead, message)
        return
    except RecipientNotResolved:
        raise
    except Exception as first_exc:
        print(f"[TG][ERROR] Ошибка отправки Telegram: {describe_telegram_error(first_exc)}")

    run_clicker_script(args.clicker_script)

    retry_sleep = args.telegram_retry_sleep
    if retry_sleep > 0:
        print(f"[TG][PAUSE] Жду {retry_sleep:.1f} секунд, потом повторю отправку этому же получателю.")
        await asyncio.sleep(retry_sleep)

    try:
        print("[TG][RETRY] Повторная попытка отправки того же сообщения тому же получателю.")
        await send_one(client, lead, message)
        print("[TG][RETRY] Повторная отправка прошла успешно, продолжаю очередь.")
    except RecipientNotResolved as second_exc:
        print(f"[TG][SKIP] Повторная попытка не смогла найти получателя: {describe_error(second_exc)}")
        raise
    except Exception as second_exc:
        print(f"[TG][STOP] Повторная отправка тоже упала: {describe_telegram_error(second_exc)}")
        raise TelegramSendRetryFailed(second_exc) from second_exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate OpenRouter messages and send them to Telegram leads from queue.")
    parser.add_argument("--queue", default=str(DEFAULT_QUEUE), help="Queue file path.")
    parser.add_argument("--processed", default=str(DEFAULT_PROCESSED), help="Processed state file.")
    parser.add_argument("--errors", default=str(DEFAULT_ERRORS_FILE), help="Error log JSONL.")
    parser.add_argument("--message-file", default=str(DEFAULT_MESSAGE_FILE), help="Static message text file for --use-static-message.")
    parser.add_argument("--message", default="", help="Static message text from command line for --use-static-message.")
    parser.add_argument("--use-static-message", action="store_true", help="Send --message or --message-file instead of generating through OpenRouter.")
    parser.add_argument("--openrouter-model", default=os.getenv("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL), help="OpenRouter model.")
    parser.add_argument("--max-site-chars", type=int, default=8000, help="Max site text chars to send to OpenRouter.")
    parser.add_argument("--account", default="sender", help="Telegram account name. Example: sender, main.")
    parser.add_argument("--delay", type=float, default=float(os.getenv("SEND_DELAY_SECONDS", "60")), help="Delay after successful send.")
    parser.add_argument("--error-sleep", type=float, default=float(os.getenv("SEND_ERROR_SLEEP_SECONDS", "240")), help="Sleep after non-critical Telegram error.")
    parser.add_argument("--telegram-retry-sleep", type=float, default=float(os.getenv("TELEGRAM_RETRY_SLEEP_SECONDS", str(DEFAULT_TELEGRAM_RETRY_SLEEP_SECONDS))), help="Sleep before retrying failed Telegram send.")
    parser.add_argument("--clicker-script", default=os.getenv("START_CLICKER_SCRIPT", ""), help="Path to start_clicker.sh. Auto-detected if empty.")
    parser.add_argument("--max-per-run", type=int, default=None, help="Max messages to send this run.")
    parser.add_argument("--dry-run", action="store_true", help="Do not send and do not write processed file.")
    parser.add_argument("--yes", action="store_true", help="Do not ask before each send.")
    return parser


async def main() -> None:
    args = build_parser().parse_args()
    queue_path = Path(args.queue)
    processed_path = Path(args.processed)
    errors_path = Path(args.errors)
    static_message = load_static_message(args) if args.use_static_message else None

    account = resolve_account(args.account)
    client = TelegramClient(str(account.session_path.with_suffix("")), account.api_id, account.api_hash)

    leads = read_queue(queue_path)
    processed_keys = load_processed_keys(processed_path)
    if not args.use_static_message and not OPENROUTER_API_KEY:
        raise SystemExit("OPENROUTER_API_KEY is empty. Add it to .env or pass --use-static-message.")

    print(f"Queue: {queue_path.resolve()}")
    print(f"Processed: {processed_path.resolve()}")
    print(f"Account: {account.account}")
    print(f"Session: {account.session_path}")
    print(f"Loaded .env files: {', '.join(str(path) for path in LOADED_ENV_FILES) or '-'}")
    print(f"Login phone from env: {account.phone or '-'}")
    print(f"Leads loaded: {len(leads)}")
    print(f"Processed keys loaded: {len(processed_keys)}")
    print(f"Dry-run: {'yes' if args.dry_run else 'no'}")
    print(f"Message source: {'static message' if static_message is not None else 'OpenRouter'}")
    if static_message is None:
        print(f"OpenRouter model: {args.openrouter_model}")
    print(f"Telegram retry sleep: {args.telegram_retry_sleep:.1f}s")
    print(f"Clicker script: {resolve_clicker_script(args.clicker_script) or '-'}")

    sent = 0
    skipped = 0
    errors_count = 0
    processed_this_run = 0

    client_connected = False
    if not args.dry_run:
        await client.connect()
        client_connected = True
        await ensure_authorized(client, account)
    else:
        print("[DRY-RUN] Telegram connection is skipped.")

    try:
        for lead in leads:
            keys = lead_processed_keys(lead)
            if not keys:
                print(f"[{lead.line_no}] SKIP: no user_id/access_hash/username/phone keys.")
                skipped += 1
                continue
            if not keys.isdisjoint(processed_keys):
                print(f"[{lead.line_no}] SKIP: already processed by {', '.join(sorted(keys))}")
                skipped += 1
                continue
            if args.max_per_run is not None and processed_this_run >= args.max_per_run:
                print(f"Max per run reached: {args.max_per_run}")
                break

            title = (
                f"[{lead.line_no}] user_id={lead.user_id or '-'} "
                f"access_hash={lead.access_hash or '-'} "
                f"username={lead.username or '-'} "
                f"phone={lead.phone or '-'} "
                f"site={lead.site or '-'}"
            )
            print("\n" + title)

            if static_message is None and not args.yes:
                answer = input("Generate OpenRouter message? Type 'generate' to continue, anything else to skip: ").strip().lower()
                if answer not in {"generate", "g", "yes", "y", "да", "send"}:
                    print("Skipped before OpenRouter request.")
                    skipped += 1
                    continue

            try:
                message = static_message or build_openrouter_message(
                    lead,
                    max_site_chars=args.max_site_chars,
                    model=args.openrouter_model,
                )
                print("\n" + "#" * 90)
                print("# MESSAGE")
                print("#" * 90)
                print(message)

                if args.dry_run:
                    print("[DRY-RUN] Would send message.")
                    processed_this_run += 1
                else:
                    if not args.yes:
                        answer = input("Send this message? Type 'send' to send, anything else to skip: ").strip().lower()
                        if answer != "send":
                            print("Skipped by user.")
                            skipped += 1
                            continue

                    await send_one_with_telegram_retry(client, lead, message, args)
                    append_processed(processed_path, lead, message)
                    processed_keys.update(keys)
                    sent += 1
                    processed_this_run += 1
                    print("Sent and marked as processed.")

                if args.delay > 0 and not args.dry_run:
                    print(f"Delay {args.delay:.1f}s...")
                    await asyncio.sleep(args.delay)
            except Exception as exc:
                errors_count += 1
                logged_exc = exc.original if isinstance(exc, (TelegramSendRetryFailed, RecipientNotResolved)) else exc
                append_error(errors_path, lead, logged_exc)
                print(f"[ERROR] {describe_error(exc)}")

                if isinstance(exc, RecipientNotResolved):
                    skipped += 1
                    print("[SKIP] Получателя не удалось найти ни одним способом. Иду дальше без start_clicker.sh.")
                    continue

                if isinstance(exc, TelegramSendRetryFailed):
                    print("[STOP] Повторная отправка после start_clicker.sh тоже не получилась. Завершаю скрипт.")
                    break

                if isinstance(exc, CRITICAL_ERRORS):
                    if isinstance(exc, getattr(errors, "FloodWaitError", ())):
                        print(f"[TG][STOP] FloodWait seconds: {exc.seconds}")
                    print("[TG][STOP] Critical Telegram error. Increase delay or switch account.")
                    break

                if args.error_sleep > 0:
                    print(f"Sleeping after error: {args.error_sleep:.1f}s")
                    await asyncio.sleep(args.error_sleep)
    finally:
        if client_connected:
            await client.disconnect()

    print("\nDone.")
    print(f"Sent: {sent}")
    print(f"Skipped: {skipped}")
    print(f"Errors: {errors_count}")


if __name__ == "__main__":
    asyncio.run(main())
