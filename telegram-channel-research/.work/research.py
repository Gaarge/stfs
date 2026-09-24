#!/usr/bin/env python3
"""Local research runner for verified public Telegram education channels.

This is a working file, not the deliverable: the deliverable is the CSV/TXT/MD
data in the parent directory. It uses only public channel metadata/posts and
never enumerates group participants.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from urllib.error import HTTPError
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from telethon import TelegramClient, functions
from telethon.errors import FloodWaitError, RPCError
from telethon.tl.types import PeerChannel


ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work"
OUT = ROOT / "output"
CHECKPOINTS = ROOT / "checkpoints"
QUERIES_CSV = ROOT / "search_queries.csv"
STATE_CSV = ROOT / "state.csv"
COVERAGE_CSV = ROOT / "coverage.csv"
STATUS_MD = ROOT / "STATUS.md"
FALSE_POSITIVE_MD = ROOT / "false_positive_patterns.md"
CANDIDATES_JSONL = WORK / "candidates.jsonl"
VERIFIED_JSONL = WORK / "verified.jsonl"
QUALITY_CSV = WORK / "quality_control.csv"

API_ID = 34825825
API_HASH = "60176f7ad0bcd77e63d4a64ca8d50a38"
SESSION = str(Path(__file__).resolve().parents[2] / "users-from-chat" / "sessions" / "chat")
NOW = datetime.now(timezone.utc)

SUBJECTS = {
    "math": "математика",
    "russian": "русский язык",
    "english": "английский язык",
    "physics": "физика",
    "chemistry": "химия",
    "biology": "биология",
    "informatics": "информатика",
    "social_studies": "обществознание",
    "history": "история",
    "literature": "литература",
    "geography": "география",
    "elementary": "начальная школа",
    "preschool": "подготовка к школе",
    "german": "немецкий язык",
    "french": "французский язык",
    "spanish": "испанский язык",
    "chinese": "китайский язык",
    "other_languages": "иностранные языки",
}

INDIVIDUAL_FORMS = [
    "репетитор {subject}",
    "преподаватель {subject}",
    "ЕГЭ {subject}",
    "ОГЭ {subject}",
    "подготовка ЕГЭ {subject}",
    "подготовка ОГЭ {subject}",
    "уроки {subject}",
    "занятия {subject}",
    "набор учеников {subject}",
    "онлайн занятия {subject}",
]

GENERAL_QUERIES = [
    ("INDIVIDUAL_TUTOR", "general", "репетитор"),
    ("INDIVIDUAL_TUTOR", "general", "частный преподаватель"),
    ("INDIVIDUAL_TUTOR", "general", "преподаватель онлайн"),
    ("INDIVIDUAL_TUTOR", "general", "подготовка ЕГЭ"),
    ("INDIVIDUAL_TUTOR", "general", "подготовка ОГЭ"),
]

SCHOOL_QUERIES = [
    "онлайн школа",
    "онлайн-школа",
    "репетиторский центр",
    "образовательный центр",
    "центр подготовки ЕГЭ",
    "центр подготовки ОГЭ",
    "школа ЕГЭ",
    "школа ОГЭ",
    "языковая онлайн школа",
]

SUBJECT_SYNONYMS = {
    "math": ["матан", "профильная математика", "базовая математика", "математика егэ", "математика огэ"],
    "russian": ["русский егэ", "русский огэ", "русский язык егэ", "русский язык огэ", "грамотность"],
    "english": ["английский егэ", "английский огэ", "английский для школьников", "english егэ", "english уроки"],
    "physics": ["физика егэ", "физика огэ", "физика для школьников", "физика уроки"],
    "chemistry": ["химия егэ", "химия огэ", "химия для школьников", "химия уроки"],
    "biology": ["биология егэ", "биология огэ", "биология для школьников", "биология уроки"],
    "informatics": ["информатика егэ", "информатика огэ", "информатика уроки", "программирование школьники"],
    "social_studies": ["общество егэ", "обществознание егэ", "обществознание огэ", "общество уроки"],
    "history": ["история егэ", "история огэ", "история уроки", "история школьники"],
    "literature": ["литература егэ", "литература огэ", "литература уроки", "литра егэ"],
    "geography": ["география егэ", "география огэ", "география уроки", "география школьники"],
    "elementary": ["началка", "начальная школа уроки", "учитель начальных классов", "начальная школа занятия"],
    "preschool": ["дошкольное развитие", "подготовка к школе занятия", "подготовка первоклассника", "логопед подготовка к школе"],
    "german": ["немецкий егэ", "немецкий для школьников", "немецкий уроки", "немецкий занятия"],
    "french": ["французский для школьников", "французский уроки", "французский занятия", "французский егэ"],
    "spanish": ["испанский для школьников", "испанский уроки", "испанский занятия", "испанский егэ"],
    "chinese": ["китайский для школьников", "китайский уроки", "китайский занятия", "китайский егэ"],
    "other_languages": ["языки для школьников", "иностранный язык уроки", "языковые занятия", "английский немецкий репетитор"],
}

REGIONAL_QUERIES = [
    "репетитор Москва", "репетитор Санкт-Петербург", "репетитор Екатеринбург", "репетитор Казань",
    "репетитор Новосибирск", "репетитор Краснодар", "репетитор Самара", "репетитор Ростов",
    "онлайн репетитор Россия", "курсы ЕГЭ Россия", "подготовка школьников онлайн", "занятия для школьников онлайн",
]

EXCLUSION_RE = re.compile(
    r"(?:гдз|решебник|слив(?:ы|ов)?|шпаргал|ответы\s+(?:егэ|огэ)|мемы|мемчик|"
    r"новости\s+образован|образовательн(?:ые|ых)\s+новост|студент(?:ы|ов)|"
    r"ваканс|каталог|агрегатор|методич(?:ка|ки)\s+для\s+учител|"
    r"продвижен.*репетитор|репетитор(?:ам|ов)\s+.*продаж|подбор.*репетитор)",
    re.I,
)

ACADEMIC_RE = re.compile(
    r"(?:\bЕГЭ\b|\bОГЭ\b|\bВПР\b|ФИПИ|школьн|\b\d{1,2}\s*класс|"
    r"математ|матан|русск|английск|english|физик|хими|биолог|информат|"
    r"обществозн|\bобщество\b|истори|литератур|географ|начальн|дошколь|"
    r"подготовк[аи]\s+к\s+школ|иностранн(?:ый|ых)\s+язык|немецк|французск|"
    r"испанск|китайск)",
    re.I,
)
TECHNICAL_RE = re.compile(
    r"^(?:канал\s+переехал|мы\s+переехали|техническ(?:ий|ое)\s+сообщени|"
    r"закреплённое\s+сообщение|подписывайтесь\s+на\s+наш\s+канал)\b",
    re.I,
)
LINK_ONLY_RE = re.compile(r"^(?:https?://\S+\s*)+$", re.I)

RU_RE = re.compile(
    r"(?:\bЕГЭ\b|\bОГЭ\b|\bВПР\b|ФИПИ|руб(?:\.|лей|ля)?|₽|Россия|российск|"
    r"школьн(?:ая|ой|ую)\s+программ|Москв|Петербург|Новосибирск|Екатеринбург|"
    r"Казань|Нижний\s+Новгород|Ростов-на-Дону|Самара|Омск|Воронеж|Краснодар|"
    r"[\w.-]+\.(?:ru|рф)\b)",
    re.I,
)

TYPE_RULES = {
    "INDIVIDUAL_TUTOR": {
        "personal": re.compile(r"(?:репетитор|репетиторство|частн(?:ый|ая)\s+преподав|преподавател[ья]|учитель|наставник)", re.I),
        "service": re.compile(r"(?:индивидуальн(?:ые|ых)\s+занят|занятия|урок[иов]|запис(?:ь|аться)|набор\s+учен|ученики|подготовк[аи].{0,30}(?:ЕГЭ|ОГЭ|ВПР)|курс[аы]|тариф|стоимост|оплат)", re.I),
        "results": re.compile(r"(?:результат(?:ы|ов)\s+(?:моих|учеников)|учени(?:к|ца).{0,30}(?:сдал|поступил|балл)|мои\s+учени)", re.I),
    },
    "ONLINE_SCHOOL": {
        "organization": re.compile(r"(?:онлайн[- ]?школ|\bшкол[аы]\b|гимнази|лице[йя]|образовательн(?:ый|ого)\s+центр|репетиторск(?:ий|ого)\s+центр|академи[яи]|образовательн(?:ый|ого)\s+проект)", re.I),
        "program": re.compile(r"(?:курс[аы]|обучени[ея]|занятия|урок[иов]|подготовк[аи].{0,30}(?:ЕГЭ|ОГЭ|ВПР)|учебн(?:ая|ую)\s+программ)", re.I),
        "enrollment": re.compile(r"(?:запис(?:ь|аться)|набор\s+учен|тариф|стоимост|оплат|групп[аы]|пробн(?:ый|ое)\s+занят)", re.I),
        "team": re.compile(r"(?:команд[аы]|преподавател[ья]|куратор[аы]|эксперт[ыа]|несколько\s+преподав)", re.I),
    },
}


def clean_text(value: str | None, limit: int = 500) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit]


def make_queries() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen = set()
    for type_name, subject, query in GENERAL_QUERIES:
        key = (query, type_name, subject)
        if key not in seen:
            rows.append({"query": query, "category": type_name, "subject": subject, "source": "Telegram global search", "status": "NEW", "results_found": "0", "valid_found": "0", "last_processed_at": ""})
            seen.add(key)
    for subject, label in SUBJECTS.items():
        for form in INDIVIDUAL_FORMS:
            query = form.format(subject=label)
            key = (query, "INDIVIDUAL_TUTOR", subject)
            if key not in seen:
                rows.append({"query": query, "category": "INDIVIDUAL_TUTOR", "subject": subject, "source": "Telegram global search", "status": "NEW", "results_found": "0", "valid_found": "0", "last_processed_at": ""})
                seen.add(key)
    for query in SCHOOL_QUERIES:
        key = (query, "ONLINE_SCHOOL", "general")
        rows.append({"query": query, "category": "ONLINE_SCHOOL", "subject": "general", "source": "Telegram global search", "status": "NEW", "results_found": "0", "valid_found": "0", "last_processed_at": ""})
        seen.add(key)
        for subject, label in SUBJECTS.items():
            q = f"{query} {label}"
            key = (q, "ONLINE_SCHOOL", subject)
            if key not in seen:
                rows.append({"query": q, "category": "ONLINE_SCHOOL", "subject": subject, "source": "Telegram global search", "status": "NEW", "results_found": "0", "valid_found": "0", "last_processed_at": ""})
                seen.add(key)
    for subject, terms in SUBJECT_SYNONYMS.items():
        for term in terms:
            for suffix, category in (("репетитор", "INDIVIDUAL_TUTOR"), ("преподаватель", "INDIVIDUAL_TUTOR"), ("занятия", "INDIVIDUAL_TUTOR"), ("онлайн школа", "ONLINE_SCHOOL")):
                query = f"{term} {suffix}"
                key = (query, category, subject)
                if key not in seen:
                    rows.append({"query": query, "category": category, "subject": subject, "source": "list.tg search", "status": "NEW", "results_found": "0", "valid_found": "0", "last_processed_at": ""})
                    seen.add(key)
    for query in REGIONAL_QUERIES:
        key = (query, "INDIVIDUAL_TUTOR", "general")
        if key not in seen:
            rows.append({"query": query, "category": "INDIVIDUAL_TUTOR", "subject": "general", "source": "list.tg search", "status": "NEW", "results_found": "0", "valid_found": "0", "last_processed_at": ""})
            seen.add(key)
    return rows


def save_query_rows(rows: list[dict[str, str]]) -> None:
    QUERIES_CSV.parent.mkdir(parents=True, exist_ok=True)
    fields = ["query", "category", "subject", "source", "status", "results_found", "valid_found", "last_processed_at"]
    with QUERIES_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def load_query_rows() -> list[dict[str, str]]:
    if not QUERIES_CSV.exists():
        rows = make_queries()
        save_query_rows(rows)
        return rows
    with QUERIES_CSV.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    known = {(r["query"], r["category"], r["subject"]) for r in rows}
    for row in make_queries():
        if (row["query"], row["category"], row["subject"]) not in known:
            rows.append(row)
    save_query_rows(rows)
    return rows


def load_candidates() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if not CANDIDATES_JSONL.exists():
        return result
    for line in CANDIDATES_JSONL.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        username = str(item.get("username", "")).lower().lstrip("@")
        if username:
            result[username] = item
    return result


def save_candidates(candidates: dict[str, dict[str, Any]]) -> None:
    CANDIDATES_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with CANDIDATES_JSONL.open("w", encoding="utf-8") as f:
        for username in sorted(candidates):
            f.write(json.dumps(candidates[username], ensure_ascii=False) + "\n")


def add_candidate(candidates: dict[str, dict[str, Any]], username: str, title: str, source: str, source_url: str, query: str, category: str, subject: str, peer_id: int | None = None) -> None:
    username = username.lower().lstrip("@")
    if not re.fullmatch(r"[a-zA-Z0-9_]{4,32}", username):
        return
    item = candidates.setdefault(username, {
        "username": username,
        "title": clean_text(title, 250),
        "peer_id": peer_id,
        "sources": [],
        "source_urls": [],
        "queries": [],
        "categories": [],
        "subjects": [],
    })
    if title and not item.get("title"):
        item["title"] = clean_text(title, 250)
    if peer_id:
        item["peer_id"] = peer_id
    for key, value in (("sources", source), ("source_urls", source_url), ("queries", query), ("categories", category), ("subjects", subject)):
        if value and value not in item[key]:
            item[key].append(value)


def telemetr_candidates() -> list[dict[str, str]]:
    """Get the public first page of two free Telemetr catalog views.

    Telemetr is candidate discovery only; acceptance still requires Telegram
    verification below.
    """
    found: list[dict[str, str]] = []
    for slug in ("ege", "education"):
        url = f"https://telemetr.me/catalog/tag/{slug}/"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            page = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")
            ids = list(dict.fromkeys(re.findall(r'<tr data-id="(\d+)"', page)))[:10]
            for peer_id in ids:
                analytics = f"https://telemetr.me/analytics/?name={peer_id}"
                req2 = urllib.request.Request(analytics, headers={"User-Agent": "Mozilla/5.0"})
                detail = urllib.request.urlopen(req2, timeout=30).read().decode("utf-8", "ignore")
                match = re.search(r"канал\s+@(\w+)", detail, re.I)
                title_match = re.search(r'<title>Telegram-канал\s+&quot;([^<]+?)&quot;', detail, re.I)
                if match:
                    found.append({"username": match.group(1), "title": html.unescape(title_match.group(1)) if title_match else "", "source_url": analytics})
        except Exception as exc:
            print(f"[Telemetr] {slug}: {type(exc).__name__}: {exc}", file=sys.stderr)
    return found


WEB_SEEDS = [
    ("mal_tutor", "https://t.me/mal_tutor", "Web search"),
    ("pro100rep", "https://t.me/s/pro100rep", "Web search"),
    ("stopointerschool", "https://t.me/s/stopointerschool", "Web search"),
    ("socegeanastasia", "https://t.me/s/socegeanastasia", "Web search"),
    ("tanya_shibitova", "https://t.me/tanya_shibitova", "Web search"),
    ("twoyenglish", "https://t.me/twoyenglish", "Web search"),
    ("english_withtutor", "https://t.me/english_withtutor", "Web search"),
    ("galinaenglishclub", "https://t.me/galinaenglishclub", "Web search"),
]


def fetch_listtg_search(query: str) -> list[dict[str, str]]:
    url = "https://list.tg/search?q=" + urllib.parse.quote(query)
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8", "ignore")
        soup = BeautifulSoup(body, "html.parser")
        result = []
        seen = set()
        for link in soup.select('a[href^="/channel/"]'):
            match = re.fullmatch(r"/channel/([A-Za-z0-9_]{4,32})/?", link.get("href", ""))
            if not match or match.group(1).lower() in seen:
                continue
            username = match.group(1).lower()
            seen.add(username)
            title_node = link.select_one(".ci-name")
            result.append({"username": username, "title": clean_text(title_node.get_text(" ", strip=True) if title_node else link.get_text(" ", strip=True), 250), "source_url": url})
        return result
    except Exception as exc:
        print(f"[list.tg] {query}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return []


def listtg_candidates(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    queries = sorted({row["query"] for row in rows})
    found: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch_listtg_search, query): query for query in queries}
        for number, future in enumerate(as_completed(futures), start=1):
            query = futures[future]
            for item in future.result():
                item["query"] = query
                found.append(item)
            if number % 50 == 0:
                print(f"[list.tg {number}/{len(queries)}] discovered={len(found)}", flush=True)
    return found


async def harvest() -> None:
    rows = load_query_rows()
    candidates = load_candidates()
    for seed in telemetr_candidates():
        add_candidate(candidates, seed["username"], seed.get("title", ""), "Telemetr", seed["source_url"], "catalog", "DISCOVERY", "general")
    for username, url, source in WEB_SEEDS:
        add_candidate(candidates, username, "", source, url, "web seed", "DISCOVERY", "general")
    listtg_rows = [row for row in rows if row.get("source") == "list.tg search" and row.get("status") != "DONE"]
    listtg = listtg_candidates(listtg_rows) if listtg_rows else []
    row_by_query = {row["query"]: row for row in rows}
    discovered_by_query = Counter(item["query"] for item in listtg)
    for seed in listtg:
        row = row_by_query.get(seed["query"], {})
        add_candidate(candidates, seed["username"], seed.get("title", ""), "list.tg search", seed["source_url"], seed["query"], row.get("category", "DISCOVERY"), row.get("subject", "general"))
    for row in listtg_rows:
        row["status"] = "DONE"
        row["results_found"] = str(discovered_by_query[row["query"]])
        row["valid_found"] = str(discovered_by_query[row["query"]])
        row["last_processed_at"] = datetime.now(timezone.utc).isoformat()
    save_query_rows(rows)
    save_candidates(candidates)

    client = TelegramClient(SESSION, API_ID, API_HASH, flood_sleep_threshold=0, request_retries=1, connection_retries=1)
    await client.start()
    try:
        total = len(rows)
        for index, row in enumerate(rows, start=1):
            if row.get("status") == "DONE" or row.get("source") != "Telegram global search":
                continue
            row["status"] = "IN_PROGRESS"
            save_query_rows(rows)
            try:
                result = await client(functions.contacts.SearchRequest(q=row["query"], limit=100))
                channels = [x for x in result.chats if getattr(x, "broadcast", False) and getattr(x, "username", None)]
                new_count = 0
                for entity in channels:
                    before = entity.username.lower() in candidates
                    add_candidate(candidates, entity.username, entity.title, "Telegram global search", f"https://t.me/{entity.username}", row["query"], row["category"], row["subject"], entity.id)
                    if not before:
                        new_count += 1
                row["results_found"] = str(len(channels))
                row["valid_found"] = str(new_count)
                row["status"] = "DONE"
                row["last_processed_at"] = datetime.now(timezone.utc).isoformat()
                save_candidates(candidates)
                save_query_rows(rows)
                print(f"[HARVEST {index}/{total}] {row['query']}: channels={len(channels)} new={new_count} total_candidates={len(candidates)}", flush=True)
            except FloodWaitError as exc:
                row["status"] = "BLOCKED"
                row["last_processed_at"] = datetime.now(timezone.utc).isoformat()
                save_query_rows(rows)
                print(f"[BLOCKED] {row['query']}: FloodWait {exc.seconds}s", flush=True)
                await asyncio.sleep(min(exc.seconds, 120))
            except Exception as exc:
                row["status"] = "BLOCKED"
                row["last_processed_at"] = datetime.now(timezone.utc).isoformat()
                save_query_rows(rows)
                print(f"[BLOCKED] {row['query']}: {type(exc).__name__}: {exc}", flush=True)
            await asyncio.sleep(0.15)
    finally:
        save_candidates(candidates)
        await client.disconnect()
    print(f"[HARVEST DONE] candidates={len(candidates)}")


def substantive(message: Any) -> bool:
    text = clean_text(getattr(message, "message", ""), 1000)
    if not text or len(text) < 20 or LINK_ONLY_RE.fullmatch(text) or TECHNICAL_RE.search(text):
        return False
    if getattr(message, "fwd_from", None) is not None:
        return False
    return True


def activity_for(date_value: datetime | None) -> str:
    if not date_value:
        return "DEAD"
    days = max(0, (NOW - date_value.astimezone(timezone.utc)).days)
    if days <= 14:
        return "VERY_ACTIVE"
    if days <= 30:
        return "ACTIVE"
    if days <= 60:
        return "ACCEPTABLE"
    if days <= 90:
        return "STALE"
    return "DEAD"


def classify(title: str, about: str, samples: list[str], preferred: list[str]) -> tuple[str | None, int, list[str], bool]:
    text = clean_text(" ".join([title, about, *samples]), 12000)
    identity = clean_text(" ".join([title, about]), 3000)
    if EXCLUSION_RE.search(identity):
        return None, 0, ["exclusion pattern"], bool(RU_RE.search(text))
    if not ACADEMIC_RE.search(text):
        return None, 0, ["no school subject or language evidence"], bool(RU_RE.search(text))
    ru = bool(RU_RE.search(text))
    individual = TYPE_RULES["INDIVIDUAL_TUTOR"]
    school = TYPE_RULES["ONLINE_SCHOOL"]
    imatches = [name for name, pattern in individual.items() if pattern.search(text)]
    smatches = [name for name, pattern in school.items() if pattern.search(text)]
    org = bool(school["organization"].search(title + " " + about))
    school_ok = org and len(smatches) >= 2
    person_ok = bool(individual["personal"].search(title + " " + about)) and len(imatches) >= 2
    # A personal channel that sells its own lessons is an individual tutor;
    # an explicit school/center/team wins when the organization evidence is clear.
    if school_ok and ("team" in smatches or "enrollment" in smatches or "program" in smatches):
        return "ONLINE_SCHOOL", len(smatches), smatches, ru
    if person_ok:
        return "INDIVIDUAL_TUTOR", len(imatches), imatches, ru
    if school_ok:
        return "ONLINE_SCHOOL", len(smatches), smatches, ru
    return None, max(len(imatches), len(smatches)), imatches + smatches, ru


def make_evidence(title: str, about: str, samples: list[str], matched: list[str], activity: str, last_date: datetime | None) -> str:
    parts = []
    if about:
        parts.append(f"Описание: {clean_text(about, 220)}")
    if matched:
        parts.append("Признаки: " + ", ".join(matched))
    if samples:
        parts.append(f"Пост: {clean_text(samples[0], 220)}")
    date_text = last_date.astimezone(timezone.utc).date().isoformat() if last_date else "нет"
    parts.append(f"Последний содержательный пост: {date_text}; активность: {activity}.")
    return " ".join(parts)[:900]


def infer_subject(text: str, preferred: list[str]) -> str:
    for subject in preferred:
        if subject not in {"general", "DISCOVERY"}:
            return subject
    patterns = [
        ("math", r"математ|матан"), ("russian", r"русск|грамотн"), ("english", r"английск|english"),
        ("physics", r"физик"), ("chemistry", r"хими"), ("biology", r"биолог"),
        ("informatics", r"информат|программирован"), ("social_studies", r"обществозн|общество"),
        ("history", r"истори"), ("literature", r"литератур"), ("geography", r"географ"),
        ("elementary", r"начальн|началк"), ("preschool", r"дошколь|подготовк[аи] к школ"),
        ("german", r"немецк"), ("french", r"французск"), ("spanish", r"испанск"),
        ("chinese", r"китайск"), ("other_languages", r"иностранн(?:ый|ых) язык|языков"),
    ]
    for subject, pattern in patterns:
        if re.search(pattern, text, re.I):
            return subject
    return "general"


def fetch_public_preview(username: str) -> dict[str, Any]:
    """Fetch and parse the public Telegram preview without Telegram API calls."""
    url = f"https://t.me/s/{username}"
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            body = response.read().decode("utf-8", "ignore")
        soup = BeautifulSoup(body, "html.parser")
        info = soup.select_one(".tgme_channel_info")
        counters = [clean_text(x.get_text(" ", strip=True)).lower() for x in soup.select(".tgme_channel_info_counter .counter_type")]
        if not info or "subscribers" not in counters:
            return {"ok": False, "reason": "not a public broadcast channel"}
        title_node = soup.select_one(".tgme_channel_info_header_title") or soup.select_one('meta[property="og:title"]')
        title = title_node.get("content", "") if title_node.name == "meta" else title_node.get_text(" ", strip=True)
        about_node = soup.select_one(".tgme_channel_info_description") or soup.select_one('meta[property="og:description"]')
        about = about_node.get("content", "") if about_node.name == "meta" else about_node.get_text(" ", strip=True)
        posts = []
        for wrap in soup.select(".tgme_widget_message_wrap"):
            if wrap.select_one(".tgme_widget_message_forwarded_from"):
                continue
            text_node = wrap.select_one(".tgme_widget_message_text")
            date_node = wrap.select_one("time[datetime]")
            text = clean_text(text_node.get_text(" ", strip=True) if text_node else "", 1000)
            if not text or len(text) < 20 or LINK_ONLY_RE.fullmatch(text) or TECHNICAL_RE.search(text):
                continue
            if not date_node:
                continue
            try:
                post_date = datetime.fromisoformat(date_node.get("datetime", "").replace("Z", "+00:00"))
            except ValueError:
                continue
            posts.append({"text": text, "date": post_date})
            if len(posts) >= 5:
                break
        return {"ok": True, "title": clean_text(title, 300), "about": clean_text(about, 1500), "posts": posts, "preview_url": url}
    except HTTPError as exc:
        if exc.code == 429:
            retry_after = exc.headers.get("Retry-After", "8") if exc.headers else "8"
            try:
                retry_after = max(3, min(int(retry_after), 60))
            except ValueError:
                retry_after = 8
            return {"ok": None, "rate_limited": True, "retry_after": retry_after, "reason": "Telegram preview HTTP 429"}
        return {"ok": False, "reason": f"preview HTTP {exc.code}: {exc.reason}"}
    except Exception as exc:
        return {"ok": False, "reason": f"preview error: {type(exc).__name__}: {exc}"}


def fetch_public_preview_retry(username: str) -> dict[str, Any]:
    for attempt in range(3):
        result = fetch_public_preview(username)
        if not result.get("rate_limited"):
            return result
        delay = result.get("retry_after", 8) * (attempt + 1)
        time.sleep(delay)
    return {"ok": False, "reason": "Telegram preview rate-limited after 3 retries"}


async def verify() -> None:
    candidates = load_candidates()
    if not candidates:
        print("[VERIFY] no candidates; run harvest first", file=sys.stderr)
        return
    checked_at = datetime.now(timezone.utc).isoformat()
    accepted: list[dict[str, Any]] = read_verified()
    rejected: list[dict[str, Any]] = []
    previous_rejected_path = OUT / "rejected.csv"
    if previous_rejected_path.exists():
        with previous_rejected_path.open(newline="", encoding="utf-8") as f:
            rejected.extend(csv.DictReader(f))
    known = {x.get("telegram_username", "").lower() for x in accepted + rejected if x.get("telegram_username")}
    ordered = [(username, candidate) for username, candidate in sorted(candidates.items()) if username not in known]
    print(f"[VERIFY] resume: already_checked={len(known)} remaining={len(ordered)}", flush=True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fetch_public_preview_retry, username): (username, candidate) for username, candidate in ordered}
        for number, future in enumerate(as_completed(futures), start=1):
            username, candidate = futures[future]
            try:
                preview = future.result()
            except Exception as exc:
                preview = {"ok": False, "reason": f"preview worker error: {type(exc).__name__}: {exc}"}
            base = {"telegram_url": f"https://t.me/{username}", "telegram_username": username, "channel_title": preview.get("title", candidate.get("title", "")), "source": "; ".join(candidate.get("sources", [])), "checked_at": checked_at}
            if not preview.get("ok"):
                base["reason"] = preview.get("reason", "preview unavailable")
                rejected.append(base)
            else:
                posts = preview.get("posts", [])
                if not posts:
                    base["reason"] = "no substantive public posts"
                    rejected.append(base)
                else:
                    last_date = posts[0]["date"]
                    activity = activity_for(last_date)
                    if activity in {"STALE", "DEAD"}:
                        base["reason"] = activity
                        base["last_post_date"] = last_date.isoformat()
                        rejected.append(base)
                    else:
                        samples = [x["text"] for x in posts]
                        type_name, score, matched, ru = classify(preview.get("title", ""), preview.get("about", ""), samples, candidate.get("subjects", []))
                        if not type_name:
                            base["reason"] = "insufficient type evidence: " + ", ".join(matched)
                            base["last_post_date"] = last_date.isoformat()
                            rejected.append(base)
                        else:
                            subject = infer_subject(" ".join([preview.get("title", ""), preview.get("about", ""), *samples]), candidate.get("subjects", []))
                            confidence = "HIGH" if score >= 2 and ru and activity in {"VERY_ACTIVE", "ACTIVE"} else "MEDIUM"
                            source_names = list(candidate.get("sources", []))
                            if "Telegram public preview" not in source_names:
                                source_names.append("Telegram public preview")
                            accepted.append({
                                "telegram_url": f"https://t.me/{username}",
                                "telegram_username": username,
                                "channel_title": preview.get("title", candidate.get("title", "")),
                                "type": type_name,
                                "subject": subject,
                                "activity_status": activity,
                                "last_post_date": last_date.isoformat(),
                                "confidence": confidence,
                                "evidence": make_evidence(preview.get("title", ""), preview.get("about", ""), samples, matched, activity, last_date),
                                "source": "; ".join(source_names),
                                "source_urls": "; ".join(dict.fromkeys(candidate.get("source_urls", []) + [preview.get("preview_url", f"https://t.me/s/{username}")])),
                                "checked_at": checked_at,
                            })
            if number % 50 == 0:
                print(f"[VERIFY {number}/{len(ordered)}] total_valid={len(accepted)} new_rejected={number - (len(accepted) - len(read_verified()))}", flush=True)
            if number % 100 == 0:
                write_outputs(accepted, rejected)
    with VERIFIED_JSONL.open("w", encoding="utf-8") as f:
        for item in accepted:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    write_outputs(accepted, rejected)
    print(f"[VERIFY DONE] checked={len(candidates)} accepted={len(accepted)} rejected={len(rejected)}")


def read_verified() -> list[dict[str, Any]]:
    if not VERIFIED_JSONL.exists():
        return []
    return [json.loads(line) for line in VERIFIED_JSONL.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_outputs(accepted: list[dict[str, Any]], rejected: list[dict[str, Any]]) -> None:
    fields = ["telegram_url", "telegram_username", "channel_title", "type", "subject", "activity_status", "last_post_date", "confidence", "evidence", "source", "checked_at"]
    accepted = sorted({x["telegram_username"]: x for x in accepted}.values(), key=lambda x: x["telegram_username"])
    groups = {
        "tutors_high.csv": [x for x in accepted if x["type"] == "INDIVIDUAL_TUTOR" and x["confidence"] == "HIGH"],
        "tutors_medium.csv": [x for x in accepted if x["type"] == "INDIVIDUAL_TUTOR" and x["confidence"] == "MEDIUM"],
        "online_schools_high.csv": [x for x in accepted if x["type"] == "ONLINE_SCHOOL" and x["confidence"] == "HIGH"],
        "online_schools_medium.csv": [x for x in accepted if x["type"] == "ONLINE_SCHOOL" and x["confidence"] == "MEDIUM"],
    }
    for name, rows in groups.items():
        write_csv(OUT / name, rows, fields)
    (OUT / "all_valid_channels.txt").write_text("\n".join(x["telegram_url"] for x in accepted) + ("\n" if accepted else ""), encoding="utf-8")
    review = [x for x in accepted if x["activity_status"] == "ACCEPTABLE"]
    write_csv(OUT / "review.csv", review, fields)
    reject_fields = ["telegram_url", "telegram_username", "channel_title", "reason", "last_post_date", "source", "checked_at"]
    write_csv(OUT / "rejected.csv", rejected, reject_fields)
    state_rows = []
    for item in accepted:
        state_rows.append({"telegram_username": item["telegram_username"], "telegram_url": item["telegram_url"], "status": "VALID", "type": item["type"], "confidence": item["confidence"], "activity_status": item["activity_status"], "last_post_date": item["last_post_date"], "reason": "accepted"})
    for item in rejected:
        state_rows.append({"telegram_username": item["telegram_username"], "telegram_url": item["telegram_url"], "status": "REJECTED", "type": "", "confidence": "", "activity_status": "", "last_post_date": item.get("last_post_date", ""), "reason": item.get("reason", "")})
    write_csv(STATE_CSV, state_rows, ["telegram_username", "telegram_url", "status", "type", "confidence", "activity_status", "last_post_date", "reason"])
    write_coverage(accepted)
    write_status(accepted, rejected)


def write_coverage(accepted: list[dict[str, Any]]) -> None:
    counter: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for x in accepted:
        counter[(x["type"], x["subject"])][x["confidence"]] += 1
    rows = []
    query_rows = load_query_rows()
    done_by_subject = Counter(r["subject"] for r in query_rows if r["status"] in {"DONE", "BLOCKED"})
    for (type_name, subject), counts in sorted(counter.items()):
        rows.append({"category": type_name, "subject": subject, "valid_count": counts["HIGH"] + counts["MEDIUM"], "high_count": counts["HIGH"], "medium_count": counts["MEDIUM"], "queries_done": done_by_subject[subject]})
    write_csv(COVERAGE_CSV, rows, ["category", "subject", "valid_count", "high_count", "medium_count", "queries_done"])


def write_status(accepted: list[dict[str, Any]], rejected: list[dict[str, Any]]) -> None:
    qrows = load_query_rows()
    counts = Counter(x["confidence"] for x in accepted)
    types = Counter(x["type"] for x in accepted)
    activity = Counter(x["activity_status"] for x in accepted)
    done = sum(r["status"] in {"DONE", "BLOCKED"} for r in qrows)
    valid = len(accepted)
    batch = (valid // 100) + (1 if valid % 100 else 0)
    lines = [
        "# Telegram channel research status", "",
        f"Updated: {datetime.now(timezone.utc).isoformat()}",
        f"Valid unique channels: {valid}",
        f"HIGH: {counts['HIGH']}; MEDIUM: {counts['MEDIUM']}",
        f"INDIVIDUAL_TUTOR: {types['INDIVIDUAL_TUTOR']}; ONLINE_SCHOOL: {types['ONLINE_SCHOOL']}",
        f"Rejected: {len(rejected)}", f"Queries DONE/BLOCKED: {done}/{len(qrows)}", f"Current batch: {batch}",
        f"Activity: VERY_ACTIVE={activity['VERY_ACTIVE']}, ACTIVE={activity['ACTIVE']}, ACCEPTABLE={activity['ACCEPTABLE']}", "",
        "Sources used: Telegram global search, Telegram public preview, Telemetr public catalog, web-search seeds.",
        "The candidate catalog is not treated as evidence; accepted rows have a public Telegram verification URL and concrete evidence.", "",
        "Next: continue NEW/BLOCKED query variants, then run independent QC on HIGH samples and refresh the output files.", "",
    ]
    STATUS_MD.write_text("\n".join(lines), encoding="utf-8")
    if not FALSE_POSITIVE_MD.exists():
        FALSE_POSITIVE_MD.write_text("# False-positive patterns\n\n- Reject channels with only free materials and no evidence of offered teaching services.\n- Reject catalogs, groups, chats, participant lists, news, and answer/solution channels.\n", encoding="utf-8")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["harvest", "verify", "all"])
    args = parser.parse_args()
    if args.phase in {"harvest", "all"}:
        await harvest()
    if args.phase in {"verify", "all"}:
        await verify()


if __name__ == "__main__":
    asyncio.run(main())
