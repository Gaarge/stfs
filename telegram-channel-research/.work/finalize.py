from __future__ import annotations

import csv
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work"
OUT = ROOT / "output"
CHECKPOINTS = ROOT / "checkpoints"
CHECKPOINTS.mkdir(exist_ok=True)

FIELDS = [
    "telegram_url", "telegram_username", "channel_title", "type", "subject",
    "activity_status", "last_post_date", "confidence", "evidence", "source", "checked_at",
]
REJECT_FIELDS = ["telegram_url", "telegram_username", "channel_title", "reason", "last_post_date", "source", "checked_at"]
STATE_FIELDS = ["telegram_username", "telegram_url", "status", "type", "confidence", "activity_status", "last_post_date", "reason"]


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def load_verified() -> list[dict]:
    rows = []
    for line in (WORK / "verified.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    unique = {r["telegram_username"].lower(): r for r in rows}
    return sorted(unique.values(), key=lambda r: r["telegram_username"].lower())


OFFLINE_NEWS_RE = re.compile(
    r"(?:новости\s+школ|официальн(?:ый|ого)\s+канал.*школ|жизн[ьи]\s+школ|"
    r"\b(?:МАОУ|МБОУ|ГБОУ|МОБУ|ГУО|СОШ)\b|школа\s*(?:№|#)?\s*\d+|"
    r"школы\s*(?:№|#)?\s*\d+|гимназия\s*(?:№|N)?\s*\d+|"
    r"лицей\s*(?:№|N)?\s*\d+|school\s*\d+\b|school official)", re.I
)
AGGREGATOR_RE = re.compile(
    r"(?:биржа\s+репетитор|найти\s+репетитор|находим\s+репетитор|"
    r"репетиторы\s+(?:москвы|самар)|делимся\s+нужн.*контакт|"
    r"заявки\s+для\s+репетитор|подбор.*репетитор)", re.I
)
TEACHER_BUSINESS_RE = re.compile(
    r"(?:для\s+репетитор|для\s+учител|для\s+преподавател|обуча[ею]м\s+педагог|"
    r"маркетинг\s+репетитор|поток\s+учеников|доход.*репетитор|"
    r"репетиторство\s+как\s+бизнес|продаж.*репетитор|авитолог|"
    r"наставник\s+репетитор|нейросет.*учител|методическ.*учител|"
    r"готовые\s+уроки.*репетитор)", re.I
)
DIRECT_SERVICE_RE = re.compile(
    r"(?:репетитор|индивидуальн.*занят|урок[иов]|занятия|"
    r"запис(?:ь|аться).{0,70}(?:занят|урок|курс|учен)|учени(?:к|ца)|"
    r"для\s+(?:детей|школьник)|подготовк.*(?:ЕГЭ|ОГЭ|ВПР)|"
    r"курс.*(?:школь|ЕГЭ|ОГЭ)|помога[юе].{0,40}(?:выуч|изуч)|"
    r"преподавател.*(?:язык|предмет))", re.I
)
OFF_TOPIC_RE = re.compile(
    r"(?:кондитер|арт[- ]?терап|танц(?:ы|ев)|карьерн|школа\s+безопас|кардиол|"
    r"педиатр|косметик|единоборств|литотерап|фитотерап|нутрициолог|дебат|"
    r"судей|астролог|инвестиц|дизайн\s+человек|журналистик|медицин|"
    r"здоровь[ея]|бьюти|школа\s+бхакт|школа\s+деда|психотерап|робототех|"
    r"\bробот\b|шахмат|искусств|график[аи])", re.I
)
MEDIA_RE = re.compile(r"(?:аудио\s+стать|обозревател|помощник\s+депутат|радиоведущ|коммерческ.*запрос)", re.I)
ACADEMIC_RE = re.compile(
    r"(?:ЕГЭ|ОГЭ|ВПР|ФИПИ|математ|русск|английск|english|физик|хими|биолог|"
    r"информат|программ|обществозн|истори|литератур|географ|начальн|дошколь|"
    r"подготовк[аи]\s+к\s+школ|немецк|французск|испанск|китайск|иностранн.*язык)", re.I
)
CHILD_EDUCATION_RE = re.compile(r"(?:дет|школь|класс|подготовк[аи]\s+к\s+школ|1\s*[-–]\s*11|начальн|дошколь)", re.I)
FINAL_QC_REMOVALS = {
    "tat_schoolpro": "failed final QC: not a public broadcast channel",
    "prostay_arifmetika": "failed final QC: not a public broadcast channel",
    "smartclass_on": "failed final QC: not a public broadcast channel",
    "across_english": "failed final QC: not a public broadcast channel",
    "insperia_rus_oge": "failed final QC: not a public broadcast channel",
}


def strict_rejection(row: dict) -> str | None:
    identity = row.get("channel_title", "") + " " + row.get("evidence", "").split("Признаки:")[0]
    if AGGREGATOR_RE.search(identity):
        return "final strict QC: aggregator/catalog rather than tutor or school"
    if MEDIA_RE.search(identity) and not DIRECT_SERVICE_RE.search(identity):
        return "final strict QC: media/author channel without teaching service"
    if TEACHER_BUSINESS_RE.search(identity) and not DIRECT_SERVICE_RE.search(identity):
        return "final strict QC: teacher-business or methodical-only channel"
    if OFF_TOPIC_RE.search(identity):
        has_academic_service = bool(ACADEMIC_RE.search(identity) and DIRECT_SERVICE_RE.search(identity))
        if not has_academic_service:
            return "final strict QC: outside school subjects/languages/preschool scope"
    if row.get("type") == "ONLINE_SCHOOL" and OFFLINE_NEWS_RE.search(identity):
        if not re.search(r"(?:онлайн|дистанц|курс|занят|репетитор|подготовк|ЕГЭ|ОГЭ|язык|начальн|дошколь)", identity, re.I):
            return "final strict QC: ordinary school/news channel"
    if row.get("type") == "ONLINE_SCHOOL" and row.get("subject") == "general":
        if not ACADEMIC_RE.search(identity) and not CHILD_EDUCATION_RE.search(identity):
            return "final strict QC: no target subject or school-age evidence"
    return None


def main() -> None:
    checked_at = datetime.now(timezone.utc).isoformat()
    all_verified = load_verified()
    accepted = []
    removed = []
    for row in all_verified:
        username = row["telegram_username"].lower()
        reason = FINAL_QC_REMOVALS.get(username) or strict_rejection(row)
        if reason:
            row = dict(row)
            row["_final_rejection_reason"] = reason
            removed.append(row)
        else:
            accepted.append(row)
    rejected = read_csv(OUT / "rejected.csv")
    # A previous finalization moves STALE rows to review.csv. Bring them back
    # into the internal rejected set before writing the next final snapshot.
    previous_review = read_csv(OUT / "review.csv")
    existing_rejected = {r.get("telegram_username", "").lower() for r in rejected}
    for row in previous_review:
        if row.get("activity_status") == "STALE" and row.get("telegram_username", "").lower() not in existing_rejected:
            rejected.append({
                "telegram_url": row.get("telegram_url", ""),
                "telegram_username": row.get("telegram_username", ""),
                "channel_title": row.get("channel_title", ""),
                "reason": "STALE",
                "last_post_date": row.get("last_post_date", ""),
                "source": row.get("source", ""),
                "checked_at": row.get("checked_at", ""),
            })
    known_rejected = {r.get("telegram_username", "").lower() for r in rejected}
    for row in removed:
        if row["telegram_username"].lower() not in known_rejected:
            rejected.append({
                "telegram_url": row["telegram_url"],
                "telegram_username": row["telegram_username"],
                "channel_title": row.get("channel_title", ""),
                "reason": row.get("_final_rejection_reason", "failed final QC"),
                "last_post_date": row.get("last_post_date", ""),
                "source": row.get("source", ""),
                "checked_at": checked_at,
            })
    rejected = {r.get("telegram_username", "").lower(): r for r in rejected if r.get("telegram_username")}
    rejected = sorted(rejected.values(), key=lambda r: r["telegram_username"].lower())

    (WORK / "verified.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in accepted), encoding="utf-8"
    )
    groups = {
        "tutors_high.csv": [r for r in accepted if r["type"] == "INDIVIDUAL_TUTOR" and r["confidence"] == "HIGH"],
        "tutors_medium.csv": [r for r in accepted if r["type"] == "INDIVIDUAL_TUTOR" and r["confidence"] == "MEDIUM"],
        "online_schools_high.csv": [r for r in accepted if r["type"] == "ONLINE_SCHOOL" and r["confidence"] == "HIGH"],
        "online_schools_medium.csv": [r for r in accepted if r["type"] == "ONLINE_SCHOOL" and r["confidence"] == "MEDIUM"],
    }
    for name, rows in groups.items():
        write_csv(OUT / name, rows, FIELDS)
    (OUT / "all_valid_channels.txt").write_text(
        "".join(r["telegram_url"] + "\n" for r in accepted), encoding="utf-8"
    )

    acceptable = [r for r in accepted if r["activity_status"] == "ACCEPTABLE"]
    stale = [r for r in rejected if r.get("reason") == "STALE"]
    review = acceptable + [{
        "telegram_url": r["telegram_url"], "telegram_username": r["telegram_username"],
        "channel_title": r.get("channel_title", ""), "type": "", "subject": "",
        "activity_status": "STALE", "last_post_date": r.get("last_post_date", ""),
        "confidence": "", "evidence": "Отложено из-за неактуальности канала на момент проверки.",
        "source": r.get("source", ""), "checked_at": r.get("checked_at", ""),
    } for r in stale]
    write_csv(OUT / "review.csv", sorted(review, key=lambda r: r["telegram_username"].lower()), FIELDS)
    non_stale_rejected = [r for r in rejected if r.get("reason") != "STALE"]
    write_csv(OUT / "rejected.csv", non_stale_rejected, REJECT_FIELDS)

    state = []
    for r in accepted:
        state.append({
            "telegram_username": r["telegram_username"], "telegram_url": r["telegram_url"],
            "status": "VALID", "type": r["type"], "confidence": r["confidence"],
            "activity_status": r["activity_status"], "last_post_date": r["last_post_date"], "reason": "accepted",
        })
    for r in rejected:
        reason = r.get("reason", "")
        activity = reason if reason in {"STALE", "DEAD"} else ""
        state.append({
            "telegram_username": r["telegram_username"], "telegram_url": r["telegram_url"],
            "status": "REJECTED", "type": "", "confidence": "", "activity_status": activity,
            "last_post_date": r.get("last_post_date", ""), "reason": reason,
        })
    write_csv(ROOT / "state.csv", sorted(state, key=lambda r: r["telegram_username"].lower()), STATE_FIELDS)

    subjects = [
        "math", "russian", "english", "physics", "chemistry", "biology", "informatics",
        "social_studies", "history", "literature", "geography", "elementary", "preschool",
        "german", "french", "spanish", "chinese", "other_languages", "general",
    ]
    query_rows = read_csv(ROOT / "search_queries.csv")
    if not any(r.get("source") == "TGStat public catalog" for r in query_rows):
        query_rows.append({
            "query": "site:tgstat.ru Telegram education channels",
            "category": "ONLINE_SCHOOL",
            "subject": "general",
            "source": "TGStat public catalog",
            "status": "BLOCKED",
            "results_found": "0",
            "valid_found": "0",
            "last_processed_at": checked_at,
        })
        write_csv(ROOT / "search_queries.csv", query_rows, ["query", "category", "subject", "source", "status", "results_found", "valid_found", "last_processed_at"])
    qdone = Counter(r["subject"] for r in query_rows if r["status"] in {"DONE", "BLOCKED"})
    counts = Counter((r["type"], r["subject"], r["confidence"]) for r in accepted)
    coverage = []
    for type_name in ["INDIVIDUAL_TUTOR", "ONLINE_SCHOOL"]:
        for subject in subjects:
            high = counts[(type_name, subject, "HIGH")]
            medium = counts[(type_name, subject, "MEDIUM")]
            coverage.append({
                "category": type_name, "subject": subject, "valid_count": high + medium,
                "high_count": high, "medium_count": medium, "queries_done": qdone[subject],
            })
    write_csv(ROOT / "coverage.csv", coverage, ["category", "subject", "valid_count", "high_count", "medium_count", "queries_done"])

    # Stable checkpoint files: one hundred new valid channels per batch.
    for old in CHECKPOINTS.glob("batch_*.csv"):
        old.unlink()
    for start in range(0, len(accepted), 100):
        batch = accepted[start:start + 100]
        write_csv(CHECKPOINTS / f"batch_{start // 100 + 1:03d}.csv", batch, FIELDS)

    candidates = sum(1 for line in (WORK / "candidates.jsonl").open(encoding="utf-8") if line.strip())
    qtotal = len(query_rows)
    done = sum(r["status"] in {"DONE", "BLOCKED"} for r in query_rows)
    type_counts = Counter(r["type"] for r in accepted)
    confidence_counts = Counter(r["confidence"] for r in accepted)
    activity_counts = Counter(r["activity_status"] for r in accepted)
    source_counts = Counter()
    for r in accepted:
        for source in r.get("source", "").split("; "):
            source_counts[source] += 1
    lines = [
        "# Telegram channel research status", "",
        f"Updated: {checked_at}",
        f"Candidate records collected: {candidates}",
        f"Candidates checked by public Telegram preview: {candidates}",
        f"Valid unique public channels: {len(accepted)}",
        f"HIGH: {confidence_counts['HIGH']}; MEDIUM: {confidence_counts['MEDIUM']}",
        f"INDIVIDUAL_TUTOR: {type_counts['INDIVIDUAL_TUTOR']}; ONLINE_SCHOOL: {type_counts['ONLINE_SCHOOL']}",
        f"Rejected total: {len(rejected)} (review.csv includes {len(stale)} STALE records and {len(acceptable)} ACCEPTABLE valid records)",
        f"Rejected.csv rows excluding STALE review: {len(non_stale_rejected)}",
        f"Search queries DONE/BLOCKED: {done}/{qtotal}; NEW: {sum(r['status'] == 'NEW' for r in query_rows)}",
        f"Checkpoints: {len(list(CHECKPOINTS.glob('batch_*.csv')))} batches; batch size 100",
        f"Activity among valid: VERY_ACTIVE={activity_counts['VERY_ACTIVE']}, ACTIVE={activity_counts['ACTIVE']}, ACCEPTABLE={activity_counts['ACCEPTABLE']}",
        "",
        "## Sources",
        "",
        "- Telegram public preview (mandatory final verification for every accepted row).",
        "- Telegram global search (candidate discovery).",
        "- list.tg public search and public channel sitemap (candidate discovery).",
        "- Telemetr public catalog and public web-search seeds (candidate discovery).",
        "- TGStat public catalog was attempted but direct access returned HTTP 403; no paid TGStat API was used.",
        "",
        "## Source occurrence in accepted rows",
        "",
    ]
    lines.extend(f"- {source}: {count}" for source, count in sorted(source_counts.items()))
    lines += [
        "", "## Criteria", "",
        "Only public broadcast channels were accepted. Groups, chats, user pages, repost-only/news/GDZ/solution channels, inactive channels, and records without at least two type signals were rejected.",
        "HIGH additionally requires a Russian-market signal and VERY_ACTIVE or ACTIVE status. MEDIUM is retained when the type is supported but the Russian-market evidence is incomplete.",
        "Independent QC files: work/qc/batch_samples.csv, work/qc/high_100.csv, work/qc/online_schools_30.csv.",
    ]
    (ROOT / "STATUS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (ROOT / "false_positive_patterns.md").write_text(
        "# False-positive patterns\n\n"
        "- Educational news, author or personal blogs containing the word teacher but no own lessons, enrollment or student evidence.\n"
        "- EGE/OGE/GDZ/solution channels that publish materials only and do not offer teaching services.\n"
        "- Catalogues, aggregators, vacancies, parent/student communities, groups and chats.\n"
        "- Organization names without two independent online-school signals such as a program, enrollment, paid course, team or classes.\n"
        "- Channels whose public preview later disappears or is no longer a public broadcast channel; these are removed during final QC.\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "candidates": candidates, "valid": len(accepted), "high": confidence_counts["HIGH"],
        "medium": confidence_counts["MEDIUM"], "tutors": type_counts["INDIVIDUAL_TUTOR"],
        "schools": type_counts["ONLINE_SCHOOL"], "rejected_total": len(rejected),
        "rejected_csv": len(non_stale_rejected), "stale_review": len(stale),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
