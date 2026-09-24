#!/usr/bin/env python3
import os
import re
import csv
import asyncio
from collections import defaultdict
from pathlib import Path

from telethon import TelegramClient, errors
from telethon.tl.types import User

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]

SEEDS_FILE = Path(os.environ.get("SEEDS_FILE", "seed_chats_new.txt"))
EXISTING_FILE = Path(os.environ.get("EXISTING_FILE", "existing_usernames.txt"))
OUTPUT_FILE = Path(os.environ.get("OUTPUT_FILE", "repetam_public_tg_leads.csv"))

MAX_MESSAGES_PER_CHAT = int(os.environ.get("MAX_MESSAGES_PER_CHAT", "20000"))
TRY_PARTICIPANTS = os.environ.get("TRY_PARTICIPANTS", "1") == "1"
INCLUDE_LOW_CONFIDENCE = os.environ.get("INCLUDE_LOW_CONFIDENCE", "0") == "1"

TUTOR_RE = re.compile(
    r"(репетитор|преподавател|учител|педагог|готовлю|подготовк.{0,8}(?:егэ|огэ|впр)|"
    r"занятия|уроки|набираю ученик|математик|русск|английск|физик|хими|биолог|"
    r"информатик|обществозн|истори|литератур|географ|немецк|французск|китайск)",
    re.I,
)

SCHOOL_RE = re.compile(
    r"(онлайн.?школ|школа|основател|владелец|руководител|директор|администратор|"
    r"методист|академическ|продюсер|edtech|ваканси|ищем преподавател|"
    r"требуется преподавател|наша команда|наш курс|набор преподавател)",
    re.I,
)

def load_existing():
    if not EXISTING_FILE.exists():
        return set()
    out = set()
    for line in EXISTING_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
        m = re.search(r"@?([A-Za-z0-9_]{5,})", line)
        if m:
            out.add(m.group(1).lower())
    return out

def load_seeds():
    seeds = []
    for raw in SEEDS_FILE.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        kind, ref = raw.split("|", 1)
        seeds.append((kind.strip(), ref.strip()))
    return seeds

def score_candidate(kind, texts):
    joined = "\n".join(texts[-8:])
    tutor_hits = len(TUTOR_RE.findall(joined))
    school_hits = len(SCHOOL_RE.findall(joined))

    if kind == "pure_tutor":
        return "tutor", max(0.85, min(0.99, 0.85 + tutor_hits * 0.02))
    if kind == "tutor_market":
        if tutor_hits:
            return "tutor", min(0.98, 0.72 + tutor_hits * 0.05)
        return "unknown", 0.35
    if kind == "school_admin":
        if school_hits:
            return "school_admin_or_staff", min(0.98, 0.70 + school_hits * 0.05)
        if tutor_hits:
            return "tutor_or_teacher", min(0.85, 0.55 + tutor_hits * 0.04)
        return "unknown", 0.30
    return "unknown", 0.25

async def safe_sleep_on_flood(exc):
    seconds = int(getattr(exc, "seconds", 10))
    print(f"[FloodWait] sleep {seconds}s")
    await asyncio.sleep(seconds + 1)

async def main():
    existing = load_existing()
    seeds = load_seeds()

    # username -> record
    found = {}
    source_texts = defaultdict(lambda: defaultdict(list))
    source_counts = defaultdict(lambda: defaultdict(int))

    client = TelegramClient("repetam_leads_session", API_ID, API_HASH)
    await client.start()

    for idx, (kind, ref) in enumerate(seeds, 1):
        print(f"[{idx}/{len(seeds)}] {kind}: @{ref}")
        try:
            entity = await client.get_entity(ref)
        except Exception as e:
            print(f"  resolve failed: {e}")
            continue

        # 1) Active authors from recent/history messages.
        while True:
            try:
                async for msg in client.iter_messages(entity, limit=MAX_MESSAGES_PER_CHAT):
                    try:
                        sender = await msg.get_sender()
                    except Exception:
                        sender = None
                    if not isinstance(sender, User):
                        continue
                    if sender.bot or not sender.username:
                        continue

                    u = sender.username.lower()
                    if u in existing:
                        continue

                    rec = found.setdefault(u, {
                        "telegram_username": "@" + sender.username,
                        "user_id": sender.id,
                        "first_name": sender.first_name or "",
                        "last_name": sender.last_name or "",
                        "sources": set(),
                    })
                    rec["sources"].add("@" + ref)

                    text = (msg.raw_text or "").strip()
                    if text:
                        source_texts[u][kind].append(text[:1000])
                        source_counts[u][kind] += 1
                break
            except errors.FloodWaitError as e:
                await safe_sleep_on_flood(e)
            except Exception as e:
                print(f"  messages failed: {e}")
                break

        # 2) Visible member list where Telegram allows it.
        if TRY_PARTICIPANTS:
            while True:
                try:
                    async for user in client.iter_participants(entity):
                        if not isinstance(user, User) or user.bot or not user.username:
                            continue
                        u = user.username.lower()
                        if u in existing:
                            continue
                        rec = found.setdefault(u, {
                            "telegram_username": "@" + user.username,
                            "user_id": user.id,
                            "first_name": user.first_name or "",
                            "last_name": user.last_name or "",
                            "sources": set(),
                        })
                        rec["sources"].add("@" + ref)
                        # Participant-only candidates from pure tutor groups
                        # are useful even if they never wrote a message.
                        if kind == "pure_tutor" and not source_texts[u][kind]:
                            source_texts[u][kind].append("участник профессионального чата репетиторов")
                    break
                except errors.FloodWaitError as e:
                    await safe_sleep_on_flood(e)
                except Exception as e:
                    print(f"  participants unavailable/limited: {e}")
                    break

        await asyncio.sleep(1.5)

    rows = []
    for u, rec in found.items():
        best_label = "unknown"
        best_conf = 0.0
        all_samples = []
        total_msgs = 0

        kinds_present = set(source_texts[u].keys())
        if not kinds_present:
            kinds_present = {"unknown"}

        for kind in kinds_present:
            texts = source_texts[u][kind]
            label, conf = score_candidate(kind, texts)
            if conf > best_conf:
                best_label, best_conf = label, conf
            total_msgs += source_counts[u][kind]
            all_samples.extend(texts[-2:])

        if not INCLUDE_LOW_CONFIDENCE and best_conf < 0.60:
            continue

        sample = " | ".join(x.replace("\n", " ")[:350] for x in all_samples[-3:])
        rows.append({
            "telegram_username": rec["telegram_username"],
            "user_id": rec["user_id"],
            "first_name": rec["first_name"],
            "last_name": rec["last_name"],
            "lead_type": best_label,
            "confidence": round(best_conf, 2),
            "sources": ";".join(sorted(rec["sources"])),
            "messages_seen": total_msgs,
            "sample_public_text": sample,
        })

    rows.sort(key=lambda r: (-r["confidence"], -r["messages_seen"], r["telegram_username"].lower()))

    with OUTPUT_FILE.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "telegram_username", "user_id", "first_name", "last_name",
            "lead_type", "confidence", "sources", "messages_seen",
            "sample_public_text"
        ])
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nSaved {len(rows)} candidates -> {OUTPUT_FILE}")
    print("Put previously collected usernames into existing_usernames.txt and rerun to deduplicate.")

    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
