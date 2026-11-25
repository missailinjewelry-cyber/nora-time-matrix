"""Матриця часу Nora.
Порожній часовий календар для аналізу мікровимірів.
"""

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

# Година (за часом платформи), з якої стартує перша свічка H4 за добу.
# За таймерами платформи (UTC+3): 01, 05, 09, 13, 17, 21.
try:
    H4_ANCHOR_HOUR = int(os.getenv("H4_ANCHOR_HOUR", "1").strip())
except ValueError:
    H4_ANCHOR_HOUR = 1


def _parse_offset(text):
    """Приймає «3», «+3», «UTC+3», «3.5». Повертає None, якщо не вдалося."""
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text or "")
    return float(match.group(0)) if match else None


def now_platform():
    """Час платформи.

    Краще задати PLATFORM_UTC_OFFSET у Railway (наприклад, 3).
    Якщо змінної немає: Київ + 1 година.
    """

    utc_now = datetime.now(timezone.utc)

    offset = _parse_offset(os.getenv("PLATFORM_UTC_OFFSET", ""))

    if offset is None:
        kyiv_now = utc_now.astimezone(ZoneInfo("Europe/Kyiv"))
        offset = kyiv_now.utcoffset().total_seconds() / 3600 + 1

    platform_tz = timezone(timedelta(hours=offset))

    return utc_now.astimezone(platform_tz)


def format_duration(seconds):
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours:
        return f"{hours}г {minutes:02d}хв {seconds:02d}с"

    return f"{minutes}хв {seconds:02d}с"


def _shift_minutes(minutes):
    """Зсув сітки: H4 стартує не з 00:00, а з H4_ANCHOR_HOUR."""
    return H4_ANCHOR_HOUR * 60 if minutes == 240 else 0


def timeframe_state(now, name, minutes):
    """Поточне положення свічки в конкретному масштабі."""

    now = now.replace(microsecond=0)
    total_minutes = now.hour * 60 + now.minute - _shift_minutes(minutes)

    elapsed = (total_minutes % minutes) * 60 + now.second
    remaining = minutes * 60 - elapsed

    # Секунди вже входять в elapsed, тому replace(second=0) тут не потрібен.
    candle_open = now - timedelta(seconds=elapsed)
    candle_close = candle_open + timedelta(minutes=minutes)

    progress = elapsed / (minutes * 60) * 100

    return {
        "timeframe": name,
        "minutes": minutes,
        "opened_at": candle_open.strftime("%H:%M:%S"),
        "closes_at": candle_close.strftime("%H:%M:%S"),
        "elapsed_seconds": elapsed,
        "remaining_seconds": remaining,
        "progress_percent": round(progress, 1),
    }


def minute_composition(now):
    """Склад хвилини: цілі свічки + хвіст для M2-M30."""

    parts = []
    for name, minutes in TIMEFRAMES:
        if name in ("M1", "H1", "H4"):
            continue
        q, r = divmod(now.minute, minutes)
        parts.append(f"{name} {q}+{r}м")

    return f"Склад хвилини :{now.minute:02d}: " + ", ".join(parts)


def matrix_snapshot(now=None):
    """Створює часовий зріз усіх дев'яти масштабів."""

    now = now or now_platform()

    lines = [
        "МАТРИЦЯ ЧАСУ NORA",
        f"ЧАС ПЛАТФОРМИ: {now:%Y-%m-%d %H:%M:%S}",
        f"UTC ЗСУВ: {now.strftime('%z')}",
        "",
        "ПОТОЧНІ МІКРОВИМІРИ:",
    ]

    for name, minutes in TIMEFRAMES:
        state = timeframe_state(now, name, minutes)

        lines.append(
            f"{name}: відкриття {state['opened_at']} | "
            f"закриття {state['closes_at']} | "
            f"минуло {format_duration(state['elapsed_seconds'])} | "
            f"залишилось {format_duration(state['remaining_seconds'])} | "
            f"заповнення {state['progress_percent']}%"
        )

    lines.extend([
        "",
        minute_composition(now),
        "",
        "Матриця показує тільки часові координати.",
        "Вона не визначає напрямок руху.",
        f"H4 прив'язана до {H4_ANCHOR_HOUR:02d}:00 часу платформи "
        "(свічки H4 відкриваються кожні 4 години від цієї години).",
    ])

    return "\n".join(lines)


def upcoming_junctions(
    now=None,
    horizon_min=60,
    min_timeframes=3,
    limit=10
):
    """Знаходить майбутні збіги закриття свічок.

    M1 не рахується: вона закривається щохвилини і створювала б шум.
    """

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
            and (total - _shift_minutes(minutes)) % minutes == 0
        ]

        if len(closing) >= min_timeframes:
            seconds_until = int((moment - now).total_seconds())

            rows.append(
                f"{moment:%H:%M:%S} "
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
        "МАЙБУТНІ ЧАСОВІ СТИКИ (3+ таймфрейми без M1):\n"
        + "\n".join(rows)
        + "\nСтик — часова подія, не торговий сигнал."
    )


def full_matrix(now=None):
    """Повна часова структура для передачі Nora."""

    now = now or now_platform()

    return (
        matrix_snapshot(now)
        + "\n\n"
        + upcoming_junctions(now)
    )
