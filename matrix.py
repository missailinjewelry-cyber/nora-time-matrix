
"""Матриця часу Nora — часова координатна сітка."""

import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TIMEFRAMES = [
    ("M1", 1),
    ("M2", 2),
    ("M3", 3),
    ("M5", 5),
    ("M10", 10),
    ("M15", 15),
    ("M30", 30),
    ("H1", 60),
    ("H4", 240),
]


def parse_offset(value):
    match = re.search(r"[-+]?\d+(?:\.\d+)?", value or "")
    return float(match.group()) if match else None


try:
    H4_ANCHOR_HOUR = int(os.getenv("H4_ANCHOR_HOUR", "1")) % 24
except ValueError:
    H4_ANCHOR_HOUR = 1


def now_platform():
    utc_now = datetime.now(timezone.utc)
    offset = parse_offset(os.getenv("PLATFORM_UTC_OFFSET", ""))

    if offset is None:
        kyiv_now = utc_now.astimezone(ZoneInfo("Europe/Kyiv"))
        offset = kyiv_now.utcoffset().total_seconds() / 3600 + 1

    return utc_now.astimezone(
        timezone(timedelta(hours=offset))
    )


def shift_minutes(minutes):
    return H4_ANCHOR_HOUR * 60 if minutes == 240 else 0


def format_duration(seconds):
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours:
        return f"{hours}г {minutes:02d}хв {seconds:02d}с"

    return f"{minutes}хв {seconds:02d}с"


def timeframe_state(now, name, minutes):
    now = now.replace(microsecond=0)
    total_minutes = now.hour * 60 + now.minute
    elapsed_minutes = (
        total_minutes - shift_minutes(minutes)
    ) % minutes

    elapsed = elapsed_minutes * 60 + now.second
    remaining = minutes * 60 - elapsed

    candle_open = now - timedelta(seconds=elapsed)
    candle_close = candle_open + timedelta(minutes=minutes)

    return {
        "timeframe": name,
        "minutes": minutes,
        "opened_at": candle_open.strftime("%H:%M:%S"),
        "closes_at": candle_close.strftime("%H:%M:%S"),
        "elapsed_seconds": elapsed,
        "remaining_seconds": remaining,
        "progress_percent": round(
            elapsed / (minutes * 60) * 100, 1
        ),
    }


def minute_composition(now):
    """Часова декомпозиція поточної години на хвилини та хвости."""
    total = now.hour * 60 + now.minute
    parts = []

    for name, minutes in TIMEFRAMES:
        if name in ("M1", "H1", "H4"):
            continue

        q, r = divmod(total, minutes)
        parts.append(f"{name}: {q} повних + {r} хв")

    return "Часова декомпозиція: " + " | ".join(parts)


def matrix_snapshot(now=None):
    now = now or now_platform()

    lines = [
        "МАТРИЦЯ ЧАСУ NORA",
        f"ЧАС ПЛАТФОРМИ: {now:%Y-%m-%d %H:%M:%S}",
        f"UTC ЗСУВ: {now.strftime('%z')}",
        "",
        "ПОТОЧНІ ЧАСОВІ МАСШТАБИ:",
    ]

    for name, minutes in TIMEFRAMES:
        state = timeframe_state(now, name, minutes)

        lines.append(
            f"{name}: відкриття {state['opened_at']} | "
            f"закриття {state['closes_at']} | "
            f"минуло {format_duration(state['elapsed_seconds'])} | "
            f"залишилось {format_duration(state['remaining_seconds'])} | "
            f"{state['progress_percent']}%"
        )

    lines.extend([
        "",
        minute_composition(now),
        "",
        "Матриця — часова координатна сітка, а не прогноз напрямку.",
        f"H4: початок добової сітки о {H4_ANCHOR_HOUR:02d}:00 "
        "за часом платформи.",
        "Фактичне вирівнювання свічок брокера потрібно перевірити.",
    ])

    return "\n".join(lines)


def upcoming_junctions(
    now=None,
    horizon_min=60,
    min_timeframes=3,
    limit=10
):
    now = now or now_platform()
    base = now.replace(second=0, microsecond=0)
    rows = []

    for step in range(1, horizon_min + 1):
        moment = base + timedelta(minutes=step)
        total = moment.hour * 60 + moment.minute

        closing = [
            name
            for name, minutes in TIMEFRAMES
            if name != "M1"
            and (total - shift_minutes(minutes)) % minutes == 0
        ]

        if len(closing) >= min_timeframes:
            seconds_until = int(
                (moment - now).total_seconds()
            )
            rows.append(
                f"{moment:%H:%M} "
                f"(через {format_duration(seconds_until)}): "
                f"{', '.join(closing)}"
            )

        if len(rows) >= limit:
            break

    if not rows:
        return (
            f"У наступні {horizon_min} хв "
            "не знайдено значних часових стиків."
        )

    return (
        "МАЙБУТНІ ЧАСОВІ СТИКИ:\n"
        + "\n".join(rows)
        + "\nСтик — часова подія, не торговий сигнал."
    )


def full_matrix(now=None):
    now = now or now_platform()
    return (
        matrix_snapshot(now)
        + "\n\n"
        + upcoming_junctions(now)
    )
