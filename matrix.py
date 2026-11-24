"""Матриця часу Nora.
Матриця створює часову сітку. Вона не прогнозує напрямок руху.
Усі часові розрахунки виконує Python, не ШІ.
"""

import os
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo

    timezone_name = os.getenv("PLATFORM_TIMEZONE", "").strip()

    if timezone_name:
        TZ = ZoneInfo(timezone_name)
        TZ_LABEL = timezone_name
    else:
        offset = float(os.getenv("PLATFORM_UTC_OFFSET", "3"))
        TZ = timezone(timedelta(hours=offset))
        TZ_LABEL = f"UTC{offset:+g}"

except Exception:
    offset = 3.0
    TZ = timezone(timedelta(hours=offset))
    TZ_LABEL = f"UTC{offset:+g}"


# Усі масштаби, які використовує Nora
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
    """Поточний час у часовій зоні торгової платформи."""
    return datetime.now(TZ)


def format_duration(seconds):
    """Зручне відображення тривалості."""
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours:
        return f"{hours}г {minutes:02d}хв {seconds:02d}с"

    return f"{minutes}хв {seconds:02d}с"


def timeframe_state(now, name, minutes):
    """Положення поточної свічки в одному часовому масштабі."""

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
    """
    Створює поточний часовий зріз усіх таймфреймів.
    Не містить прогнозу напрямку.
    """

    now = now or now_platform()

    lines = [
        "МАТРИЦЯ ЧАСУ NORA",
        f"Час платформи: {now:%Y-%m-%d %H:%M:%S}",
        f"Часова зона: {TZ_LABEL}",
        "",
        "ПОТОЧНЕ ПОЛОЖЕННЯ СВІЧОК:",
    ]

    for name, minutes in TIMEFRAMES:
        state = timeframe_state(now, name, minutes)

        lines.append(
            f"{name}: відкрилася {state['opened_at']} | "
            f"закриється {state['closes_at']} | "
            f"минуло {format_duration(state['elapsed_seconds'])} | "
            f"залишилося {format_duration(state['remaining_seconds'])} | "
            f"заповнення {state['progress_percent']}%"
        )

    lines.extend([
        "",
        "ВАЖЛИВО:",
        "Це лише часова структура.",
        "Вона не визначає напрямок, розворот або продовження руху.",
        "Для цього потрібен аналіз скриншотів графіка.",
    ])

    return "\n".join(lines)


def upcoming_junctions(
    now=None,
    horizon_min=60,
    min_timeframes=3,
    limit=15
):
    """
    Знаходить майбутні хвилини, у які закриваються
    щонайменше min_timeframes свічок одночасно.
    """

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
            f"У наступні {horizon_min} хвилин "
            f"немає стиків із {min_timeframes}+ таймфреймів."
        )

    return (
        "МАЙБУТНІ ЧАСОВІ СТИКИ:\n"
        + "\n".join(rows)
        + "\n\nСтик — це часова подія, а не торговий сигнал."
    )


def full_matrix(now=None):
    """Повна матриця для передачі ШІ."""

    now = now or now_platform()

    return (
        matrix_snapshot(now)
        + "\n\n"
        + upcoming_junctions(now)
    )
