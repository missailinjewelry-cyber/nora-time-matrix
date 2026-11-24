
"""Матриця часу Nora.
Порожній часовий календар для аналізу мікровимірів.
"""

import os
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


def now_platform():
    """Час платформи: Київ + 1 година."""

    utc_now = datetime.now(timezone.utc)

    # Можна задати точний UTC-зсув платформи через Railway.
    manual_offset = os.getenv("PLATFORM_UTC_OFFSET", "").strip()

    if manual_offset:
        offset = float(manual_offset)
    else:
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


def timeframe_state(now, name, minutes):
    """Поточне положення свічки в конкретному масштабі."""

    total_minutes = now.hour * 60 + now.minute

    elapsed = (total_minutes % minutes) * 60 + now.second
    remaining = minutes * 60 - elapsed

    candle_open = (
        now.replace(second=0, microsecond=0)
        - timedelta(seconds=elapsed)
    )

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
        "Матриця показує тільки часові координати.",
        "Вона не визначає напрямок руху.",
        "H4 у цій версії прив'язана до 00:00 часу платформи.",
    ])

    return "\n".join(lines)


def upcoming_junctions(
    now=None,
    horizon_min=60,
    min_timeframes=2,
    limit=15
):
    """Знаходить майбутні збіги закриття свічок."""

    now = now or now_platform()
    base = now.replace(second=0, microsecond=0)

    rows = []

    for step in range(1, horizon_min + 1):
        moment = base + timedelta(minutes=step)
        total_minutes = moment.hour * 60 + moment.minute

        closing = [
            name
            for name, minutes in TIMEFRAMES
            if total_minutes % minutes == 0
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
            "не знайдено часових стиків."
        )

    return (
        "МАЙБУТНІ ЧАСОВІ СТИКИ:\n"
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
