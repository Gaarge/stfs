#!/usr/bin/env python3
"""Collect only publicly advertised Telegram contacts for verified channels.

This script never enumerates channel subscribers or tries to discover hidden
owners. It reads public Telegram previews, extracts contacts explicitly shown
in channel descriptions/recent posts, then resolves those public usernames
with the already-authorized user session when possible.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.error import HTTPError

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output"
WORK = ROOT / "work"
CHECKPOINTS = ROOT / "checkpoints"
INPUT = OUT / "all_valid_channels.txt"
PREVIEWS = WORK / "public_contact_previews.jsonl"
RESOLVED = WORK / "resolved_public_contacts.json"
STATE = WORK / "public_contacts_state.csv"
CONTACTS_CSV = OUT / "channel_contacts.csv"
UNIQUE_CSV = OUT / "unique_public_contacts.csv"
NO_CONTACT_CSV = OUT / "channels_without_public_contact.csv"
UNAVAILABLE_CSV = OUT / "channels_unavailable.csv"
LINKS_CSV = OUT / "channel_external_links.csv"
OWNER_CSV = OUT / "owner_candidates.csv"
AUTHOR_CSV = OUT / "author_candidates.csv"
MANAGER_CSV = OUT / "manager_contacts.csv"
STATUS = ROOT / "CONTACTS_STATUS.md"

USERNAME_RE = re.compile(r"(?<![A-Za-z0-9_])@([A-Za-z][A-Za-z0-9_]{3,31})")
TME_RE = re.compile(r"(?:https?://)?t\.me/([A-Za-z][A-Za-z0-9_]{3,31})(?:[/?#][^\s<]*)?", re.I)
OWNER_RE = re.compile(r"(?:автор|основател|владелец|веду\s+(?:канал|блог)|мой\s+канал|личный\s+блог|создател)", re.I)
AUTHOR_RE = re.compile(r"(?:автор|преподавател|репетитор|учител|основател)", re.I)
MANAGER_RE = re.compile(r"(?:по\s+вопросам|запис(?:ь|аться)|менеджер|реклам|сотрудничеств|связ(?:ь|аться)|написать|продюсер|админ)", re.I)
BOT_RE = re.compile(r"(?:_bot|бот$)", re.I)
SKIP_HANDLES = {"s", "joinchat", "addstickers", "share", "proxy", "login", "iv", "c"}

CONTACT_FIELDS = [
    "channel_url", "channel_username", "channel_title", "contact_username", "contact_url",
    "contact_role", "confidence", "source", "source_url", "evidence", "post_date", "checked_at",
]
UNIQUE_FIELDS = [
    "contact_username", "contact_url", "entity_type", "is_bot", "telegram_user_id", "access_hash",
    "first_name", "last_name", "channels_count", "roles", "confidence", "evidence", "checked_at",
]
NO_FIELDS = ["channel_url", "channel_username", "channel_title", "reason", "checked_at"]
LINK_FIELDS = ["channel_url", "channel_username", "channel_title", "external_url", "source", "checked_at"]
STATE_FIELDS = ["channel_username", "channel_url", "status", "contacts_found", "reason", "checked_at"]


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def clean(value: str | None, limit: int = 1000) -> str:
    value = unescape(value or "")
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit]


def username_from_url(url: str) -> str:
    match = re.search(r"t\.me/([A-Za-z][A-Za-z0-9_]{3,31})", url, re.I)
    return match.group(1).lower() if match else ""


def extract_handles(text: str, html_links: list[str], own_username: str) -> list[tuple[str, int, str]]:
    found: dict[str, tuple[int, str]] = {}
    for match in USERNAME_RE.finditer(text):
        handle = match.group(1).lower()
        if handle not in SKIP_HANDLES and handle != own_username.lower():
            found.setdefault(handle, (match.start(), clean(text[max(0, match.start() - 100):match.end() + 100], 260)))
    for href in html_links:
        handle = username_from_url(href)
        if handle and handle not in SKIP_HANDLES and handle != own_username.lower():
            pos = text.lower().find(handle.lower())
            found.setdefault(handle, (max(pos, 0), clean(text[max(0, pos - 100):pos + len(handle) + 100], 260)))
    return [(handle, position, evidence) for handle, (position, evidence) in found.items()]


def role_for(context: str, handle: str) -> tuple[str, int]:
    if OWNER_RE.search(context):
        return "OWNER_CANDIDATE", 7
    if AUTHOR_RE.search(context):
        return "AUTHOR_CANDIDATE", 5
    if MANAGER_RE.search(context):
        return "MANAGER", 4
    if BOT_RE.search(handle):
        return "PUBLIC_BOT_CONTACT", 3
    return "PUBLIC_CONTACT", 2


def fetch_preview(username: str) -> dict:
    url = f"https://t.me/s/{username}"
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8", "ignore")
            soup = BeautifulSoup(body, "html.parser")
            info = soup.select_one(".tgme_channel_info")
            counters = [clean(x.get_text(" ", strip=True)).lower() for x in soup.select(".tgme_channel_info_counter .counter_type")]
            if not info or "subscribers" not in counters:
                return {"ok": False, "reason": "not a public broadcast channel", "username": username}
            title_node = soup.select_one(".tgme_channel_info_header_title") or soup.select_one('meta[property="og:title"]')
            about_node = soup.select_one(".tgme_channel_info_description") or soup.select_one('meta[property="og:description"]')
            title = clean(title_node.get("content", "") if title_node and title_node.name == "meta" else title_node.get_text(" ", strip=True) if title_node else "", 300)
            about = clean(about_node.get_text(" ", strip=True) if about_node and about_node.name != "meta" else about_node.get("content", "") if about_node else "", 2500)
            about_links = [a.get("href", "") for a in (about_node.select("a") if about_node else []) if a.get("href")]
            posts = []
            external_links: list[str] = []
            for wrap in soup.select(".tgme_widget_message_wrap"):
                if wrap.select_one(".tgme_widget_message_forwarded_from"):
                    continue
                text_node = wrap.select_one(".tgme_widget_message_text")
                date_node = wrap.select_one("time[datetime]")
                message_text = clean(text_node.get_text(" ", strip=True) if text_node else "", 1800)
                if not message_text or not date_node:
                    continue
                post_date = date_node.get("datetime", "")
                data_post = wrap.select_one("[data-post]")
                post_path = data_post.get("data-post", "") if data_post else ""
                post_url = f"https://t.me/{post_path}" if post_path else url
                links = [a.get("href", "") for a in wrap.select("a[href]") if a.get("href")]
                external_links.extend(links)
                posts.append({"text": message_text, "date": post_date, "url": post_url, "links": links})
                if len(posts) >= 30:
                    break
            return {
                "ok": True, "username": username, "title": title, "about": about,
                "about_links": about_links, "posts": posts, "external_links": external_links,
                "preview_url": url,
            }
        except HTTPError as exc:
            if exc.code == 429:
                time.sleep(min(45, 4 * (attempt + 1)))
                continue
            return {"ok": False, "reason": f"preview HTTP {exc.code}", "username": username}
        except Exception as exc:
            if attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            return {"ok": False, "reason": f"preview error: {type(exc).__name__}: {exc}", "username": username}
    return {"ok": False, "reason": "preview rate-limited after retries", "username": username}


def extract_contacts(preview: dict, checked_at: str) -> tuple[list[dict], list[dict]]:
    username = preview["username"]
    channel_url = f"https://t.me/{username}"
    title = preview.get("title", "")
    rows: list[dict] = []
    links: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    sources = [("description", preview.get("about", ""), preview.get("about_links", []), channel_url, "")]
    for post in preview.get("posts", []):
        sources.append(("recent_post", post.get("text", ""), post.get("links", []), post.get("url", channel_url), post.get("date", "")))
    for source, text, html_links, source_url, post_date in sources:
        for href in html_links:
            absolute = urllib.parse.urljoin("https://t.me/", href)
            if absolute.startswith("http") and "t.me/" not in absolute and not absolute.startswith("tg://"):
                links.append({"channel_url": channel_url, "channel_username": username, "channel_title": title, "external_url": absolute, "source": source, "checked_at": checked_at})
        for handle, position, nearby in extract_handles(text, html_links, username):
            context = f"{text[max(0, position - 150):position + 150]} {nearby}"
            role, role_score = role_for(context, handle)
            base = 7 if source == "description" else 2
            score = base + role_score
            if any(word in context.lower() for word in ["запись", "по вопросам", "сотрудничество", "реклама", "автор", "основатель"]):
                score += 2
            key = (handle, source, source_url)
            if key in seen:
                continue
            seen.add(key)
            if score < 5:
                continue
            confidence = "HIGH" if source == "description" and role in {"OWNER_CANDIDATE", "AUTHOR_CANDIDATE", "MANAGER"} else "MEDIUM"
            rows.append({
                "channel_url": channel_url,
                "channel_username": username,
                "channel_title": title,
                "contact_username": handle,
                "contact_url": f"https://t.me/{handle}",
                "contact_role": role,
                "confidence": confidence,
                "source": source,
                "source_url": source_url,
                "evidence": clean(nearby, 500),
                "post_date": post_date,
                "checked_at": checked_at,
                "_score": score,
            })
    # Keep a useful bounded set per channel, preferring explicit description contacts.
    rows.sort(key=lambda r: (-r["_score"], r["contact_username"]))
    return rows[:10], links


def read_channels() -> list[str]:
    channels = []
    for line in INPUT.read_text(encoding="utf-8").splitlines():
        username = username_from_url(line.strip())
        if username:
            channels.append(username)
    return sorted(set(channels))


def write_preview_state(previews: dict[str, dict]) -> None:
    with PREVIEWS.open("w", encoding="utf-8") as f:
        for username in sorted(previews):
            f.write(json.dumps(previews[username], ensure_ascii=False) + "\n")


def read_preview_state() -> dict[str, dict]:
    if not PREVIEWS.exists():
        return {}
    state = {}
    for line in PREVIEWS.read_text(encoding="utf-8").splitlines():
        if line.strip():
            item = json.loads(line)
            state[item["username"]] = item
    return state


async def resolve_contacts(rows: list[dict]) -> dict[str, dict]:
    # Import only in the environment that has Telethon installed.
    from telethon import TelegramClient
    from telethon.errors import RPCError
    from telethon.tl.types import Channel, Chat, User
    import sys
    sys.path.insert(0, str(ROOT / ".work"))
    import research

    unique = sorted({r["contact_username"] for r in rows})
    resolved = {}
    if RESOLVED.exists():
        resolved = json.loads(RESOLVED.read_text(encoding="utf-8"))
    pending = [u for u in unique if u not in resolved]
    if not pending:
        return resolved
    client = TelegramClient(research.SESSION, research.API_ID, research.API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        for username in pending:
            resolved[username] = {"entity_type": "UNRESOLVED", "error": "session is not authorized"}
        RESOLVED.write_text(json.dumps(resolved, ensure_ascii=False, indent=2), encoding="utf-8")
        await client.disconnect()
        return resolved
    for index, username in enumerate(pending, start=1):
        try:
            entity = await client.get_entity(username)
            if isinstance(entity, User):
                resolved[username] = {
                    "entity_type": "USER", "is_bot": bool(entity.bot), "telegram_user_id": str(entity.id),
                    "access_hash": str(entity.access_hash or ""), "first_name": entity.first_name or "",
                    "last_name": entity.last_name or "", "username": entity.username or username,
                }
            elif isinstance(entity, Channel):
                resolved[username] = {"entity_type": "CHANNEL", "is_bot": False, "telegram_user_id": str(entity.id), "username": entity.username or username, "title": entity.title or ""}
            elif isinstance(entity, Chat):
                resolved[username] = {"entity_type": "CHAT", "is_bot": False, "telegram_user_id": str(entity.id), "title": entity.title or ""}
            else:
                resolved[username] = {"entity_type": type(entity).__name__, "username": username}
        except Exception as exc:
            resolved[username] = {"entity_type": "UNRESOLVED", "error": f"{type(exc).__name__}: {exc}"}
        if index % 25 == 0:
            RESOLVED.write_text(json.dumps(resolved, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[RESOLVE {index}/{len(pending)}]", flush=True)
        await asyncio.sleep(0.15)
    RESOLVED.write_text(json.dumps(resolved, ensure_ascii=False, indent=2), encoding="utf-8")
    await client.disconnect()
    return resolved


def write_outputs(channels: list[str], previews: dict[str, dict], resolved: dict[str, dict], checked_at: str) -> None:
    contact_rows = []
    no_contact = []
    link_rows = []
    state_rows = []
    for username in channels:
        preview = previews.get(username, {"ok": False, "reason": "not checked"})
        if not preview.get("ok"):
            state_rows.append({"channel_username": username, "channel_url": f"https://t.me/{username}", "status": "ERROR", "contacts_found": 0, "reason": preview.get("reason", "preview unavailable"), "checked_at": checked_at})
            continue
        contacts, links = extract_contacts(preview, checked_at)
        contact_rows.extend(contacts)
        link_rows.extend(links)
        if not contacts:
            no_contact.append({"channel_url": f"https://t.me/{username}", "channel_username": username, "channel_title": preview.get("title", ""), "reason": "no sufficiently explicit public contact found in description/recent posts", "checked_at": checked_at})
        state_rows.append({"channel_username": username, "channel_url": f"https://t.me/{username}", "status": "CONTACTS_FOUND" if contacts else "NO_PUBLIC_CONTACT", "contacts_found": len(contacts), "reason": "" if contacts else "no explicit public contact", "checked_at": checked_at})

    for row in contact_rows:
        row.pop("_score", None)
    contact_rows.sort(key=lambda r: (r["channel_username"], r["contact_username"], r["source"]))
    write_csv(CONTACTS_CSV, contact_rows, CONTACT_FIELDS)
    write_csv(OWNER_CSV, [r for r in contact_rows if r["contact_role"] == "OWNER_CANDIDATE"], CONTACT_FIELDS)
    write_csv(AUTHOR_CSV, [r for r in contact_rows if r["contact_role"] == "AUTHOR_CANDIDATE"], CONTACT_FIELDS)
    write_csv(MANAGER_CSV, [r for r in contact_rows if r["contact_role"] == "MANAGER"], CONTACT_FIELDS)
    write_csv(NO_CONTACT_CSV, no_contact, NO_FIELDS)
    write_csv(UNAVAILABLE_CSV, [r for r in state_rows if r["status"] == "ERROR"], STATE_FIELDS)
    unique_links = {tuple(x.get(field, "") for field in LINK_FIELDS): x for x in link_rows}
    write_csv(LINKS_CSV, [unique_links[key] for key in sorted(unique_links)], LINK_FIELDS)
    write_csv(STATE, sorted(state_rows, key=lambda r: r["channel_username"]), STATE_FIELDS)

    grouped: dict[str, list[dict]] = {}
    for row in contact_rows:
        grouped.setdefault(row["contact_username"], []).append(row)
    unique_rows = []
    for username, items in sorted(grouped.items()):
        best = sorted(items, key=lambda r: (r["confidence"] != "HIGH", r["source"] != "description", r["contact_role"]))[0]
        entity = resolved.get(username, {})
        unique_rows.append({
            "contact_username": username,
            "contact_url": f"https://t.me/{username}",
            "entity_type": entity.get("entity_type", "UNRESOLVED"),
            "is_bot": entity.get("is_bot", ""),
            "telegram_user_id": entity.get("telegram_user_id", ""),
            "access_hash": entity.get("access_hash", ""),
            "first_name": entity.get("first_name", ""),
            "last_name": entity.get("last_name", ""),
            "channels_count": len({x["channel_username"] for x in items}),
            "roles": "; ".join(sorted({x["contact_role"] for x in items})),
            "confidence": best["confidence"],
            "evidence": best["evidence"],
            "checked_at": checked_at,
        })
    write_csv(UNIQUE_CSV, unique_rows, UNIQUE_FIELDS)
    os.chmod(UNIQUE_CSV, 0o600)

    for old in CHECKPOINTS.glob("contacts_batch_*.csv"):
        old.unlink()
    channel_rows = []
    for username in channels:
        channel_rows.extend([r for r in contact_rows if r["channel_username"] == username])
        if len(channel_rows) >= 100:
            pass
    for start in range(0, len(channels), 100):
        batch_channels = set(channels[start:start + 100])
        batch_rows = [r for r in contact_rows if r["channel_username"] in batch_channels]
        write_csv(CHECKPOINTS / f"contacts_batch_{start // 100 + 1:03d}.csv", batch_rows, CONTACT_FIELDS)

    user_count = sum(1 for x in unique_rows if x["entity_type"] == "USER")
    owner_count = sum(1 for x in contact_rows if x["contact_role"] == "OWNER_CANDIDATE")
    manager_count = sum(1 for x in contact_rows if x["contact_role"] == "MANAGER")
    lines = [
        "# Public channel contacts status", "", f"Updated: {checked_at}",
        f"Channels in input: {len(channels)}", f"Channels checked: {sum(1 for x in state_rows if x['status'] != 'ERROR')}",
        f"Channels with public contacts: {sum(1 for x in state_rows if x['status'] == 'CONTACTS_FOUND')}",
        f"Channels without sufficiently explicit public contact: {len(no_contact)}",
        f"Contact rows: {len(contact_rows)}", f"Unique public handles: {len(unique_rows)}", f"Resolved Telegram user accounts: {user_count}",
        f"OWNER_CANDIDATE rows: {owner_count}", f"MANAGER rows: {manager_count}",
        "", "The result contains public contacts only. OWNER_CANDIDATE means the channel text explicitly associates the handle with the author/founder/owner; it is not a claim about a hidden owner.",
        "The collector does not enumerate subscribers, scrape hidden administrators, or infer private identities.", "",
        "Files: output/owner_candidates.csv, output/author_candidates.csv, output/manager_contacts.csv, output/channel_contacts.csv, output/unique_public_contacts.csv, output/channels_without_public_contact.csv, output/channels_unavailable.csv, output/channel_external_links.csv.",
    ]
    STATUS.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-resolve", action="store_true", help="do not resolve public handles through Telegram API")
    args = parser.parse_args()
    channels = read_channels()
    checked_at = datetime.now(timezone.utc).isoformat()
    previews = read_preview_state()
    pending = [u for u in channels if u not in previews]
    print(f"[PREVIEW] total={len(channels)} already_checked={len(previews)} remaining={len(pending)}", flush=True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fetch_preview, username): username for username in pending}
        for index, future in enumerate(as_completed(futures), start=1):
            username = futures[future]
            previews[username] = future.result()
            if index % 50 == 0:
                write_preview_state(previews)
                print(f"[PREVIEW {index}/{len(pending)}]", flush=True)
    write_preview_state(previews)
    provisional_rows = []
    for username in channels:
        if previews.get(username, {}).get("ok"):
            provisional_rows.extend(extract_contacts(previews[username], checked_at)[0])
    if args.no_resolve:
        resolved = json.loads(RESOLVED.read_text(encoding="utf-8")) if RESOLVED.exists() else {}
    else:
        resolved = await resolve_contacts(provisional_rows)
    write_outputs(channels, previews, resolved, checked_at)
    print(f"[DONE] channels={len(channels)} contact_rows={len(provisional_rows)} unique_handles={len({r['contact_username'] for r in provisional_rows})}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
