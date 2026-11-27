"""Строит SVG-диаграммы метрик пилота из docs/pilot_measurements.csv.

Запуск из корня репозитория:
    python backend/scripts/metrics_charts.py
"""

from __future__ import annotations

import csv
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[2]
CSV_PATH = ROOT / "docs" / "pilot_measurements.csv"
OUT_DIR = ROOT / "docs" / "charts"

INK = "#1f3d2b"
MUTED = "#6b7f72"
GREEN = "#21a038"
GREEN_LIGHT = "#9bd8a8"
BLUE = "#3b7ddd"


def read_rows() -> list[dict[str, str]]:
    with CSV_PATH.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def grouped_bars(rows: list[dict[str, str]]) -> str:
    width, left, right = 980, 250, 40
    top, row_height, bar, gap = 96, 46, 15, 6
    height = top + row_height * len(rows) + 40
    chart_width = width - left - right
    peak = max(int(row["leadership"]) for row in rows)
    scale = chart_width / max(peak, 1)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="Segoe UI, Arial, sans-serif">',
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
        f'<text x="{left}" y="34" font-size="20" font-weight="600" fill="{INK}">'
        "Руководство и контакты по вузам</text>",
        f'<text x="{left}" y="54" font-size="13" fill="{MUTED}">'
        "Записи в карточке после сбора, пилот 10 вузов</text>",
        f'<rect x="{left}" y="72" width="14" height="10" fill="{GREEN}"/>',
        f'<text x="{left + 20}" y="81" font-size="12" fill="{MUTED}">руководство</text>',
        f'<rect x="{left + 140}" y="72" width="14" height="10" fill="{GREEN_LIGHT}"/>',
        f'<text x="{left + 160}" y="81" font-size="12" fill="{MUTED}">контакты</text>',
    ]

    for index, row in enumerate(rows):
        y = top + index * row_height
        name = escape(row["university"])
        leadership = int(row["leadership"])
        contacts = int(row["contacts"])
        parts.append(
            f'<text x="{left - 12}" y="{y + 12}" text-anchor="end" font-size="13" '
            f'fill="{INK}">{name}</text>'
        )
        parts.append(
            f'<rect x="{left}" y="{y}" width="{leadership * scale:.1f}" height="{bar}" '
            f'rx="3" fill="{GREEN}"/>'
        )
        parts.append(
            f'<text x="{left + leadership * scale + 8:.1f}" y="{y + bar - 3}" '
            f'font-size="12" font-weight="600" fill="{INK}">{leadership}</text>'
        )
        parts.append(
            f'<rect x="{left}" y="{y + bar + gap}" width="{contacts * scale:.1f}" '
            f'height="{bar}" rx="3" fill="{GREEN_LIGHT}"/>'
        )
        parts.append(
            f'<text x="{left + contacts * scale + 8:.1f}" y="{y + bar + gap + bar - 3}" '
            f'font-size="12" fill="{MUTED}">{contacts}</text>'
        )

    parts.append("</svg>")
    return "\n".join(parts)


def kpi_tiles() -> str:
    width, height = 980, 210
    tiles = [
        ("Сайт найден", "10 / 10", "100 %", GREEN),
        ("Ректор определён", "10 / 10", "100 %", GREEN),
        ("Фото ректора", "8 / 10", "80 %", BLUE),
        ("Сборы без ошибок", "10 / 10", "100 %", GREEN),
    ]
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="Segoe UI, Arial, sans-serif">',
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
        f'<text x="40" y="38" font-size="20" font-weight="600" fill="{INK}">'
        "Ключевые показатели пилота</text>",
    ]
    tile_width, gap = 210, 22
    for index, (label, value, share, color) in enumerate(tiles):
        x = 40 + index * (tile_width + gap)
        parts.append(
            f'<rect x="{x}" y="60" width="{tile_width}" height="120" rx="14" '
            f'fill="#f4faf5" stroke="#d8ecdc"/>'
        )
        parts.append(
            f'<text x="{x + 18}" y="92" font-size="13" fill="{MUTED}">{escape(label)}</text>'
        )
        parts.append(
            f'<text x="{x + 18}" y="132" font-size="30" font-weight="700" '
            f'fill="{INK}">{escape(value)}</text>'
        )
        parts.append(
            f'<text x="{x + 18}" y="158" font-size="13" font-weight="600" '
            f'fill="{color}">{escape(share)}</text>'
        )
    parts.append("</svg>")
    return "\n".join(parts)


def facts_chart() -> str:
    rows = [("УрФУ", 451), ("ЮУрГУ", 163), ("МГУ", 19)]
    width, left, right = 900, 170, 60
    top, row_height = 86, 56
    height = top + row_height * len(rows) + 30
    chart_width = width - left - right
    peak = max(value for _, value in rows)
    scale = chart_width / peak
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="Segoe UI, Arial, sans-serif">',
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
        f'<text x="{left}" y="38" font-size="20" font-weight="600" fill="{INK}">'
        "Извлечённые факты по карточкам</text>",
        f'<text x="{left}" y="60" font-size="13" fill="{MUTED}">'
        "кандидаты после сбора и автопроверки</text>",
    ]
    for index, (name, value) in enumerate(rows):
        y = top + index * row_height
        parts.append(
            f'<text x="{left - 12}" y="{y + 22}" text-anchor="end" font-size="14" '
            f'fill="{INK}">{escape(name)}</text>'
        )
        parts.append(
            f'<rect x="{left}" y="{y}" width="{value * scale:.1f}" height="30" rx="4" '
            f'fill="{GREEN}"/>'
        )
        parts.append(
            f'<text x="{left + value * scale + 10:.1f}" y="{y + 21}" font-size="14" '
            f'font-weight="600" fill="{INK}">{value}</text>'
        )
    parts.append("</svg>")
    return "\n".join(parts)


def main() -> int:
    rows = read_rows()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "pilot_per_university.svg").write_text(grouped_bars(rows), encoding="utf-8")
    (OUT_DIR / "pilot_summary.svg").write_text(kpi_tiles(), encoding="utf-8")
    (OUT_DIR / "facts_per_card.svg").write_text(facts_chart(), encoding="utf-8")
    print("charts written to", OUT_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
