"""Снимок метрик пилота по карточкам, которые есть в локальной базе.

Запуск из каталога backend:
    .\\.venv\\Scripts\\python.exe scripts\\metrics_snapshot.py
"""

from __future__ import annotations

import json
import statistics
import sys
import urllib.parse
import urllib.request
from datetime import datetime

API = "http://127.0.0.1:8000/api/v1/ai"


def get(url: str):
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.loads(response.read().decode())


def parse_stamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S.%f%z")
    except ValueError:
        return None


def duration_seconds(run: dict) -> int | None:
    start = parse_stamp(run.get("created_at"))
    end = parse_stamp(run.get("updated_at"))
    if start is None or end is None or end < start:
        return None
    return round((end - start).total_seconds())


def main() -> int:
    cards = get(f"{API}/cards")
    if not cards:
        print("Карточек нет. Создайте карточку в интерфейсе.")
        return 1

    rows = []
    total_facts = total_pending = total_rejected = 0
    facts_per_card = []

    for card in cards:
        cid = urllib.parse.quote(card["id"], safe="")
        detail = get(f"{API}/cards/{cid}")
        run = detail.get("run") or {}
        facts = run.get("facts") or []
        pending = sum(1 for f in facts if f.get("verification_status") == "pending")
        rejected = sum(1 for f in facts if f.get("verification_status") == "rejected")
        total_facts += len(facts)
        total_pending += pending
        total_rejected += rejected
        if facts:
            facts_per_card.append(len(facts))

        seconds = duration_seconds(run)

        rows.append(
            {
                "name": (card.get("name") or "")[:34],
                "site": card.get("website") or "",
                "run": run.get("status") or "—",
                "facts": len(facts),
                "pending": pending,
                "rejected": rejected,
                "seconds": seconds,
            }
        )

    print(f"{'Вуз':36} {'сайт':28} {'сбор':10} {'факты':>6} {'пров.':>6} {'откл.':>6} {'сек':>6}")
    for row in rows:
        print(
            f"{row['name']:36} {row['site'][:27]:28} {row['run']:10} "
            f"{row['facts']:6} {row['pending']:6} {row['rejected']:6} "
            f"{(row['seconds'] if row['seconds'] is not None else '—'):>6}"
        )

    print()
    print(f"Карточек: {len(rows)} | фактов всего: {total_facts} | на проверке: {total_pending} | отклонено: {total_rejected}")
    if total_facts:
        print(f"Доля отклонённых: {total_rejected / total_facts:.1%} (цель ≤ 15 %)")
    if facts_per_card:
        print(f"Фактов на карточку: медиана {statistics.median(facts_per_card):.0f} (цель ≥ 50)")
    durations = [row["seconds"] for row in rows if row["seconds"] is not None]
    if durations:
        print(f"Время сбора: медиана {statistics.median(durations):.0f} с, максимум {max(durations):.0f} с")
    return 0


if __name__ == "__main__":
    sys.exit(main())
