from __future__ import annotations

import csv
import json
import random
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".work"))
import research  # noqa: E402

OUT = ROOT / "output"
WORK = ROOT / "work"
QC_DIR = WORK / "qc"
QC_DIR.mkdir(parents=True, exist_ok=True)

FIELDS = [
    "batch", "telegram_username", "telegram_url", "type", "confidence",
    "result", "reason", "checked_at",
]


def read_rows() -> list[dict[str, str]]:
    rows = []
    for name in [
        "tutors_high.csv", "tutors_medium.csv",
        "online_schools_high.csv", "online_schools_medium.csv",
    ]:
        with (OUT / name).open(encoding="utf-8", newline="") as f:
            rows.extend(csv.DictReader(f))
    return sorted(rows, key=lambda x: x["telegram_username"].lower())


def independent_check(row: dict[str, str], preview: dict) -> tuple[str, str]:
    if not preview.get("ok"):
        return "UNCERTAIN", preview.get("reason", "preview unavailable")
    posts = preview.get("posts", [])
    if not posts:
        return "INCORRECT", "no substantive public posts"
    activity = research.activity_for(posts[0]["date"])
    if activity in {"STALE", "DEAD"}:
        return "INCORRECT", f"activity is {activity}"
    title = preview.get("title", "")
    about = preview.get("about", "")
    samples = [p["text"] for p in posts]
    all_text = research.clean_text(" ".join([title, about, *samples]), 12000)
    identity = research.clean_text(" ".join([title, about]), 3000)
    if research.EXCLUSION_RE.search(identity):
        return "INCORRECT", "exclusion pattern in title/description"
    if not research.ACADEMIC_RE.search(all_text):
        return "INCORRECT", "no school subject/language or Russian exam evidence"
    individual = research.TYPE_RULES["INDIVIDUAL_TUTOR"]
    school = research.TYPE_RULES["ONLINE_SCHOOL"]
    imatches = [name for name, pattern in individual.items() if pattern.search(all_text)]
    smatches = [name for name, pattern in school.items() if pattern.search(all_text)]
    org = bool(school["organization"].search(title + " " + about))
    personal = bool(individual["personal"].search(title + " " + about))
    if row["type"] == "INDIVIDUAL_TUTOR":
        if not personal or len(imatches) < 2:
            return "INCORRECT", "individual-tutor criteria do not reach two signals"
        detected = "INDIVIDUAL_TUTOR"
        signals = imatches
    elif row["type"] == "ONLINE_SCHOOL":
        if not org or len(smatches) < 2:
            return "INCORRECT", "online-school criteria do not reach two signals"
        detected = "ONLINE_SCHOOL"
        signals = smatches
    else:
        return "INCORRECT", "unsupported type"
    if detected != row["type"]:
        return "INCORRECT", f"detected {detected}"
    if row["confidence"] == "HIGH":
        if not research.RU_RE.search(all_text):
            return "INCORRECT", "HIGH lacks Russian-market signal"
        if len(signals) < 2 or activity not in {"VERY_ACTIVE", "ACTIVE"}:
            return "INCORRECT", "HIGH criteria not met"
    return "CORRECT", f"{detected}; signals={','.join(signals)}; activity={activity}"


def check_rows(rows: list[dict[str, str]], filename: str) -> list[dict[str, str]]:
    checked_at = datetime.now(timezone.utc).isoformat()
    results = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(research.fetch_public_preview_retry, row["telegram_username"]): row
            for row in rows
        }
        for future in as_completed(futures):
            row = futures[future]
            try:
                preview = future.result()
                result, reason = independent_check(row, preview)
            except Exception as exc:
                result, reason = "UNCERTAIN", f"QC worker error: {type(exc).__name__}: {exc}"
            results.append({
                "batch": row.get("batch", ""),
                "telegram_username": row["telegram_username"],
                "telegram_url": row["telegram_url"],
                "type": row["type"],
                "confidence": row["confidence"],
                "result": result,
                "reason": reason,
                "checked_at": checked_at,
            })
    results.sort(key=lambda x: x["telegram_username"])
    with (QC_DIR / filename).open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(results)
    return results


def main() -> None:
    rows = read_rows()
    batches = []
    for index, row in enumerate(rows):
        row = dict(row)
        row["batch"] = f"batch_{index // 100 + 1:03d}"
        batches.append(row)
    batch_sample = []
    for batch_no in range((len(batches) + 99) // 100):
        batch_rows = batches[batch_no * 100:(batch_no + 1) * 100]
        batch_sample.extend(random.Random(7300 + batch_no).sample(batch_rows, min(25, len(batch_rows))))
    high = [r for r in batches if r["confidence"] == "HIGH"]
    high_sample = random.Random(91015).sample(high, min(100, len(high)))
    schools = [r for r in batches if r["type"] == "ONLINE_SCHOOL"]
    school_sample = random.Random(91030).sample(schools, min(30, len(schools)))
    all_results = []
    all_results.extend(check_rows(batch_sample, "batch_samples.csv"))
    check_rows(high_sample, "high_100.csv")
    check_rows(school_sample, "online_schools_30.csv")
    all_results = list(csv.DictReader((QC_DIR / "batch_samples.csv").open(encoding="utf-8", newline="")))
    for filename in ["high_100.csv", "online_schools_30.csv"]:
        result_rows = list(csv.DictReader((QC_DIR / filename).open(encoding="utf-8", newline="")))
        all_results.extend(result_rows)
    print(json.dumps({
        "valid": len(rows),
        "batch_sample": len(all_results),
        "batch_counts": {k: sum(1 for r in all_results if r["result"] == k) for k in ["CORRECT", "INCORRECT", "UNCERTAIN"]},
        "high_100": {k: sum(1 for r in list(csv.DictReader((QC_DIR / "high_100.csv").open(encoding="utf-8", newline=""))) if r["result"] == k) for k in ["CORRECT", "INCORRECT", "UNCERTAIN"]},
        "online_schools_30": {k: sum(1 for r in list(csv.DictReader((QC_DIR / "online_schools_30.csv").open(encoding="utf-8", newline=""))) if r["result"] == k) for k in ["CORRECT", "INCORRECT", "UNCERTAIN"]},
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
