"""Матриця часу Nora. Усе рахується кодом, а не ШІ, щоб час ніколи не плутався."""
import os
import re
from datetime import datetime, timedelta, timezone


def _number(text, default):
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text or "")
    return float(match.group()) if match else default


# Зсув часу платформи відносно UTC (за годинником на графіку).
OFFSET = _number(os.getenv("PLATFORM_UTC_OFFSET", ""), 3.0)
TZ = timezone(timedelta(hours=OFFSET))
TZ_LABEL = f"UTC{OFFSET:+g}"

# Година, з якої стартує перша свічка H4 за добу (час платформи).
H4_ANCHOR_HOUR = int(_number(os.getenv("H4_ANCHOR_HOUR", ""), 1)) % 24
H4_STARTS = ", ".join(
    f"{h:02d}" for h in sorted((H4_ANCHOR_HOUR + 4 * k) % 24 for k in range(6))
)

# Скільки секунд минає від скріна до ручного входу (аналіз 10-40 с).
ENTRY_DELAY_SEC = int(_number(os.getenv("ENTRY_DELAY_SEC", ""), 40))

TAIL_TFS = [("M2", 2), ("M3", 3), ("M5", 5), ("M10", 10), ("M15", 15), ("M30", 30)]
ALL_TFS = TAIL_TFS + [("H1", 60), ("H4", 240)]
TF_NAMES = ["M1"] + [name for name, _ in ALL_TFS]

EXPIRY_CANDIDATES = list(range(1, 31)) + [35, 40, 45, 50, 55, 60]


def now_platform():
    return datetime.now(TZ)


def _shift(tf):
    """H4 стартує не з 00:00, а з H4_ANCHOR_HOUR."""
    return H4_ANCHOR_HOUR * 60 if tf == 240 else 0


def closes_at_minute(total_minutes, tf):
    return (total_minutes - _shift(tf)) % tf == 0


def _left(seconds):
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}г{m:02d}хв"
    return f"{m}хв{s:02d}с"


def entry_time(now=None):
    now = now or now_platform()
    return now.replace(microsecond=0) + timedelta(seconds=ENTRY_DELAY_SEC)


def matrix_snapshot(now=None):
    """Знімок матриці для поточної секунди."""
    now = (now or now_platform()).replace(microsecond=0)
    total = now.hour * 60 + now.minute
    lines = [f"ЧАС ПЛАТФОРМИ ({TZ_LABEL}): {now:%Y-%m-%d %H:%M:%S}"]

    closed = [name for name, tf in ALL_TFS if closes_at_minute(total, tf)]
    if closed:
        lines.append(
            f"Щойно закрилися (о {now:%H:%M}:00, {now.second}с тому): "
            f"{', '.join(closed)} (і M1)"
        )
    else:
        lines.append("Цієї хвилини жоден таймфрейм від M2 не закривався (лише M1)")

    tails = [f"{name}: {now.minute // tf}+{now.minute % tf}м" for name, tf in TAIL_TFS]
    lines.append("Хвости (цілі свічки + хвіст): " + " | ".join(tails))

    h4_elapsed = ((now.hour - H4_ANCHOR_HOUR) % 4) * 60 + now.minute
    lines.append(
        f"H1 відкрита {now.minute}хв тому | "
        f"H4 (старти о {H4_STARTS} за часом платформи) відкрита "
        f"{h4_elapsed // 60}г{h4_elapsed % 60:02d}хв тому"
    )

    lefts = []
    for name, tf in ALL_TFS:
        elapsed = ((total - _shift(tf)) % tf) * 60 + now.second
        remaining = tf * 60 - elapsed
        end = now + timedelta(seconds=remaining)
        lefts.append(f"{name} {_left(remaining)} (о {end:%H:%M:%S})")
    lines.append("До закриття поточної свічки: " + " | ".join(lefts))
    return "\n".join(lines)


def upcoming_junctions(now=None, horizon_min=60, min_tfs=3, limit=10):
    """Найближчі стики: закриття min_tfs+ таймфреймів одночасно (M1 не рахується)."""
    now = (now or now_platform()).replace(microsecond=0)
    base = now.replace(second=0)
    rows = []
    for k in range(1, horizon_min + 1):
        t = base + timedelta(minutes=k)
        total = t.hour * 60 + t.minute
        names = [name for name, tf in ALL_TFS if closes_at_minute(total, tf)]
        if len(names) >= min_tfs:
            rows.append(f"{t:%H:%M} (через {_left((t - now).total_seconds())}): {', '.join(names)}")
        if len(rows) >= limit:
            break
    if not rows:
        return (
            f"НАЙБЛИЖЧІ СТИКИ: у наступні {horizon_min} хв немає закриття "
            f"{min_tfs}+ таймфреймів одночасно."
        )
    return (
        f"НАЙБЛИЖЧІ СТИКИ (закриття {min_tfs}+ таймфреймів одночасно, "
        f"наступні {horizon_min} хв):\n" + "\n".join(rows)
    )


def expiry_end(n, now=None):
    """Кінець угоди для експірації N хв (вхід не раніше entry_time).

    Повертає (час кінця, список таймфреймів M2+, що закриваються на цій хвилині).
    Кінець округлюється до найближчої хвилини: свічки закриваються рівно на :00.
    """
    start = entry_time(now)
    end = start + timedelta(minutes=n)
    end = (end + timedelta(seconds=30)).replace(second=0, microsecond=0)
    total = end.hour * 60 + end.minute
    closing = [name for name, tf in ALL_TFS if closes_at_minute(total, tf)]
    return end, closing


def expiry_grid(now=None):
    """Для кожної можливої експірації N: що закриється в її кінці."""
    start = entry_time(now)
    lines = [
        f"КАНДИДАТИ ЕКСПІРАЦІЇ (вхід не раніше {start:%H:%M:%S}; "
        "кінець округлено до хвилини; M1 не рахується):"
    ]
    for n in EXPIRY_CANDIDATES:
        end, closing = expiry_end(n, now)
        label = ", ".join(closing) if closing else "жодна свічка M2+ не закривається"
        lines.append(f"N={n} хв → кінець ≈{end:%H:%M:%S}: {label}")
    return "\n".join(lines)


def tf_minutes(tf_name):
    if tf_name == "M1":
        return 1
    return dict(ALL_TFS).get(tf_name)


def next_close(tf_name, now=None):
    """Момент найближчого закриття свічки таймфрейму (для запиту скріна)."""
    now = (now or now_platform()).replace(microsecond=0)
    tf = tf_minutes(tf_name)
    if tf is None:
        return None
    total = now.hour * 60 + now.minute
    elapsed = ((total - _shift(tf)) % tf) * 60 + now.second
    return now + timedelta(seconds=tf * 60 - elapsed)


def full_matrix(now=None):
    now = now or now_platform()
    return (
        matrix_snapshot(now)
        + "\n\n"
        + upcoming_junctions(now)
        + "\n\n"
        + f"ВХІД НЕ РАНІШЕ: {entry_time(now):%H:%M:%S} "
        "(аналіз і ручний вхід займають 10-40 с)"
        + "\n\n"
        + expiry_grid(now)
    )
