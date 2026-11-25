import os
import re
import json
import base64
import logging
import asyncio
import sqlite3

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from openai import OpenAI
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)


# ============================================================
# 1. НАЛАШТУВАННЯ
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
    raise RuntimeError("Не задані ключі Telegram або OpenAI")

client = OpenAI(api_key=OPENAI_API_KEY)

MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
MAX_IMAGES = 5
ALBUM_WAIT = 3.0
MAX_OUTPUT_TOKENS = 1100
DB_PATH = os.getenv("DB_PATH", "/data/nora.db")

# Кількість додаткових раундів після початкового пакета.
MAX_FOLLOWUP_ROUNDS = 2

# Після запланованого часу Nora чекає 2 хвилини,
# надсилає одне нагадування, а ще через 2 хвилини скасовує сесію.
REMINDER_DELAY_SECONDS = 120
CANCEL_AFTER_REMINDER_SECONDS = 120
SCHEDULER_INTERVAL_SECONDS = 10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

logger = logging.getLogger("Nora")
ALBUMS = {}
SESSION_LOCKS = {}


# ============================================================
# 2. МАТРИЦЯ ЧАСУ — ЗБЕРЕЖЕНА ПОПЕРЕДНЯ ЛОГІКА
# ============================================================

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
        offset = kyiv_now.utcoffset().total_seconds() / 3600

    return utc_now.astimezone(timezone(timedelta(hours=offset)))


def format_duration(seconds):
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours:
        return f"{hours}г {minutes:02d}хв {seconds:02d}с"
    return f"{minutes}хв {seconds:02d}с"


def timeframe_shift(minutes):
    if minutes == 240:
        return H4_ANCHOR_HOUR * 60
    return 0


def timeframe_state(now, name, minutes):
    now = now.replace(microsecond=0)
    total_minutes = now.hour * 60 + now.minute
    elapsed_minutes = (
        total_minutes - timeframe_shift(minutes)
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
    # Збережена початкова логіка декомпозиції хвилини.
    parts = []
    for name, minutes in TIMEFRAMES:
        if name in ("M1", "H1", "H4"):
            continue
        q, r = divmod(now.minute, minutes)
        parts.append(f"{name} {q}+{r}м")

    return f"Склад хвилини :{now.minute:02d}: " + ", ".join(parts)


def matrix_snapshot(now=None):
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
        "Матриця показує часові координати, а не напрямок.",
        f"H4 починається о {H4_ANCHOR_HOUR:02d}:00 "
        "за часом платформи.",
        "Фактичне вирівнювання H4 брокера потребує перевірки.",
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
            name for name, minutes in TIMEFRAMES
            if name != "M1"
            and (total - timeframe_shift(minutes)) % minutes == 0
        ]

        if len(closing) >= min_timeframes:
            seconds_until = int((moment - now).total_seconds())
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
    return matrix_snapshot(now) + "\n\n" + upcoming_junctions(now)


# ============================================================
# 3. ІНСТРУКЦІЇ ДЛЯ АНАЛІЗУ
# ============================================================

SYSTEM_PROMPT = f"""
Ти Nora — аналітична система авторської «Матриці часу».

Різні таймфрейми — різні мікровиміри одного руху.
Матриця — порожній календар, а не самостійний прогноз.
Скриншоти заповнюють календар цінами, рівнями, розмірами тіл
і хвостів, швидкістю, ритмом та стадіями руху.

Усі скриншоти однієї сесії аналізуй як одну структуру одного
інструмента. Різні таймфрейми приблизно одного моменту не є
послідовністю. Але повторний скриншот того самого таймфрейму
в наступному раунді — це нове спостереження в часі, яке треба
порівняти з попереднім.

Визначай таймфрейм за видимою позначкою. Не вигадуй відсутні
масштаби. Якщо пари різні або це неможливо перевірити — WAIT.

Перевір:
1. Хто контролював попередній рух — покупці чи продавці?
2. Хто втрачає силу: тіла, хвости, швидкість, амплітуда,
   невдалі продовження, стискання?
3. Чи перехоплює інша сторона ініціативу: послідовність,
   збільшення тіл, зміна ритму, утримання рівнів,
   синхронізація молодших масштабів?
4. Чи новий поштовх готується, формується, розвивається,
   втрачає силу або ще не визначений?

Старші масштаби дають контекст, молодші — поточну фазу,
середні — перевірку взаємодії. Не надавай автоматичної
переваги жодному масштабу. Не перевертай напрямок механічно.

Визнач структуру: продовження, розворот, корекція,
накопичення, затор, пружина, шум, змішана структура,
невизначеність. Не називай будь-який хвіст пружиною,
відскок — розворотом, а втрату швидкості — завершенням руху.

Часовий стик сам по собі не є сигналом.
Експірація від 1 до 60 хвилин після фактичного входу.
Не прив'язуй її автоматично до одного таймфрейму або стику.
Якщо напрямок чи експірацію неможливо обґрунтувати — WAIT.
Не створюй сигнал лише для уникнення пропуску.
Не вигадуй відсотки точності й не обіцяй прибуток.

H4 у матриці прив'язана до {H4_ANCHOR_HOUR:02d}:00
за часом платформи. Вирівнювання H4 брокера може відрізнятися.

Пиши українською, коротко та зрозуміло.
"""


DECISION_PROMPT = """
Ти працюєш у багатоступеневій сесії аналізу Nora.

Початковий пакет зазвичай містить H1, M15 і M3.
Додаткові скриншоти можуть бути іншими таймфреймами або
повторним знімком того самого таймфрейму пізніше.

Твоє завдання — вирішити, чи вже достатньо даних для
завершеного прогнозу, чи потрібне ще одне конкретне
спостереження.

Не проси додаткові скриншоти просто для накопичення даних.
Проси їх лише тоді, коли вони перевірять конкретну
невизначеність, наприклад:
- чи формується локальний поштовх на M1/M3;
- чи утримується пробитий рівень;
- чи змінюється ритм на M5;
- чи підтверджує середній масштаб рух молодшого;
- як змінилася свічка того самого таймфрейму з часом.

Враховуй поточний час платформи та стан свічок у матриці.
Якщо потрібна повторна фотографія, обери час, коли її
інформативність буде вищою (наприклад, ближче до закриття
потрібної свічки), а не довільний інтервал.

Дозволено максимум два додаткові раунди після початкового.
Якщо після них недостатньо даних — заверши з WAIT.

Поверни ЛИШЕ коректний JSON без Markdown у такій структурі:

{
  "action": "request" або "final",
  "timeframe": "M1/M2/M3/M5/M10/M15/M30/H1/H4 або порожньо",
  "delay_minutes": ціле число від 0 до 60,
  "reason": "коротко: що саме перевірить новий скриншот",
  "analysis": "повний фінальний аналіз українською, якщо action=final"
}

Якщо action=request:
- timeframe обов'язковий;
- delay_minutes = 0 означає надіслати зараз;
- delay_minutes > 0 означає попросити скриншот через стільки хвилин;
- reason має бути конкретним;
- analysis залиш порожнім.

Якщо action=final:
- заповни analysis за форматом нижче;
- timeframe порожній, delay_minutes=0, reason порожній.

ФОРМАТ ФІНАЛЬНОГО АНАЛІЗУ:

Пара: ...
Ситуація: ...
Масштаби: ...
Стадія поштовху: формується / розвивається /
втрачає силу / не визначено
Сценарій: ...
Напрямок: ВГОРУ ↑ / ВНИЗ ↓ / ЧЕКАЄМО ⏳
Вхід: ЗАРАЗ / ПІСЛЯ УМОВИ: ... / НЕ ВХОДИТИ
Експірація: N хв після входу / не визначена
Скасування: ...
Чому: ...
"""


# ============================================================
# 4. БАЗА ДАНИХ
# ============================================================

def db_connect():
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.execute("PRAGMA foreign_keys = ON")
    return db


def init_db():
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)

    with db_connect() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                platform_time TEXT NOT NULL,
                pair TEXT,
                direction TEXT NOT NULL,
                entry TEXT,
                expiry_minutes INTEGER,
                analysis TEXT NOT NULL,
                screenshot_count INTEGER NOT NULL,
                outcome TEXT NOT NULL DEFAULT 'pending'
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS screenshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prediction_id INTEGER NOT NULL,
                image_data BLOB NOT NULL,
                FOREIGN KEY(prediction_id)
                    REFERENCES predictions(id)
            )
        """)

        existing = {
            row[1] for row in db.execute(
                "PRAGMA table_info(predictions)"
            ).fetchall()
        }

        for column, kind in (
            ("raw_text", "TEXT"),
            ("platform_minute", "INTEGER"),
        ):
            if column not in existing:
                db.execute(
                    f"ALTER TABLE predictions ADD COLUMN {column} {kind}"
                )

        # Стан роботи Nora зберігається після перезапуску.
        db.execute("""
            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        db.execute("""
            INSERT OR IGNORE INTO bot_state(key, value)
            VALUES ('working', '0')
        """)

        # Одна сесія — один аналіз, який може мати кілька раундів.
        db.execute("""
            CREATE TABLE IF NOT EXISTS analysis_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'processing',
                request_round INTEGER NOT NULL DEFAULT 0,
                request_timeframe TEXT,
                request_reason TEXT,
                due_at TEXT,
                due_notified INTEGER NOT NULL DEFAULT 0,
                reminder_at TEXT,
                reminder_sent INTEGER NOT NULL DEFAULT 0,
                cancel_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS session_images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                round_no INTEGER NOT NULL,
                captured_at TEXT NOT NULL,
                matrix_text TEXT NOT NULL,
                image_data BLOB NOT NULL,
                FOREIGN KEY(session_id)
                    REFERENCES analysis_sessions(id)
            )
        """)

    logger.info("База Nora готова: %s", DB_PATH)


def is_working():
    with db_connect() as db:
        row = db.execute(
            "SELECT value FROM bot_state WHERE key='working'"
        ).fetchone()
    return bool(row and row[0] == "1")


def set_working(value):
    with db_connect() as db:
        db.execute("""
            INSERT INTO bot_state(key, value)
            VALUES ('working', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, ("1" if value else "0",))


def get_active_session(chat_id):
    with db_connect() as db:
        return db.execute("""
            SELECT *
            FROM analysis_sessions
            WHERE chat_id = ?
              AND status IN ('processing', 'waiting')
            ORDER BY id DESC
            LIMIT 1
        """, (chat_id,)).fetchone()


def create_session(chat_id, user_id, now):
    stamp = now.isoformat()
    with db_connect() as db:
        cursor = db.execute("""
            INSERT INTO analysis_sessions (
                chat_id, user_id, status, request_round,
                created_at, updated_at
            )
            VALUES (?, ?, 'processing', 0, ?, ?)
        """, (chat_id, user_id, stamp, stamp))
        return cursor.lastrowid


def get_session(session_id):
    with db_connect() as db:
        return db.execute("""
            SELECT * FROM analysis_sessions WHERE id=?
        """, (session_id,)).fetchone()


def add_session_images(session_id, round_no, images, matrix_text, captured_at):
    with db_connect() as db:
        for image_bytes in images:
            db.execute("""
                INSERT INTO session_images (
                    session_id, round_no, captured_at,
                    matrix_text, image_data
                )
                VALUES (?, ?, ?, ?, ?)
            """, (
                session_id,
                round_no,
                captured_at.isoformat(),
                matrix_text,
                sqlite3.Binary(image_bytes)
            ))

        db.execute("""
            UPDATE analysis_sessions
            SET status='processing', updated_at=?
            WHERE id=?
        """, (captured_at.isoformat(), session_id))


def load_session_images(session_id):
    with db_connect() as db:
        rows = db.execute("""
            SELECT round_no, captured_at, matrix_text, image_data
            FROM session_images
            WHERE session_id=?
            ORDER BY id
        """, (session_id,)).fetchall()
    return rows


def set_session_request(session_id, round_no, timeframe, reason, due_at, now):
    reminder_at = due_at + timedelta(seconds=REMINDER_DELAY_SECONDS)
    cancel_at = reminder_at + timedelta(
        seconds=CANCEL_AFTER_REMINDER_SECONDS
    )

    with db_connect() as db:
        db.execute("""
            UPDATE analysis_sessions
            SET status='waiting',
                request_round=?,
                request_timeframe=?,
                request_reason=?,
                due_at=?,
                due_notified=0,
                reminder_at=?,
                reminder_sent=0,
                cancel_at=?,
                updated_at=?
            WHERE id=?
        """, (
            round_no,
            timeframe,
            reason,
            due_at.isoformat(),
            reminder_at.isoformat(),
            cancel_at.isoformat(),
            now.isoformat(),
            session_id
        ))


def mark_due_notified(session_id, now):
    with db_connect() as db:
        db.execute("""
            UPDATE analysis_sessions
            SET due_notified=1, updated_at=?
            WHERE id=?
        """, (now.isoformat(), session_id))


def mark_reminder_sent(session_id, now):
    with db_connect() as db:
        db.execute("""
            UPDATE analysis_sessions
            SET reminder_sent=1, updated_at=?
            WHERE id=?
        """, (now.isoformat(), session_id))


def finish_session(session_id, status, now):
    with db_connect() as db:
        db.execute("""
            UPDATE analysis_sessions
            SET status=?, due_at=NULL, reminder_at=NULL,
                cancel_at=NULL, updated_at=?
            WHERE id=?
        """, (status, now.isoformat(), session_id))


def cancel_all_sessions(chat_id=None):
    now = now_platform().isoformat()
    with db_connect() as db:
        if chat_id is None:
            db.execute("""
                UPDATE analysis_sessions
                SET status='cancelled', due_at=NULL,
                    reminder_at=NULL, cancel_at=NULL, updated_at=?
                WHERE status IN ('processing', 'waiting')
            """, (now,))
        else:
            db.execute("""
                UPDATE analysis_sessions
                SET status='cancelled', due_at=NULL,
                    reminder_at=NULL, cancel_at=NULL, updated_at=?
                WHERE chat_id=?
                  AND status IN ('processing', 'waiting')
            """, (now, chat_id))


# ============================================================
# 5. РОЗБІР І ПЕРЕВІРКА АНАЛІЗУ
# ============================================================

def get_field(text, field):
    pattern = (
        r"^[\s>*_#\-•]*"
        + re.escape(field)
        + r"[\s*_]*:[\s*_]*(.*)$"
    )
    match = re.search(
        pattern, text, re.IGNORECASE | re.MULTILINE
    )
    return match.group(1).strip(" *_") if match else ""


def parse_direction(text):
    value = get_field(text, "Напрямок").upper()
    if "ВГОРУ" in value or "CALL" in value:
        return "CALL"
    if "ВНИЗ" in value or "PUT" in value:
        return "PUT"
    return "WAIT"


def direction_label(direction):
    return {
        "CALL": "ВГОРУ ↑",
        "PUT": "ВНИЗ ↓",
        "WAIT": "ЧЕКАЄМО ⏳",
    }.get(direction, "ЧЕКАЄМО ⏳")


def parse_expiry(text):
    value = get_field(text, "Експірація").lower()
    match = re.search(r"\b(\d{1,2})\s*хв", value)
    if not match:
        return None
    number = int(match.group(1))
    return number if 1 <= number <= 60 else None


def validate_analysis(result):
    direction = parse_direction(result)
    expiry = parse_expiry(result)

    if direction in ("CALL", "PUT") and expiry is None:
        direction = "WAIT"
    if direction == "WAIT":
        expiry = None

    expiry_line = (
        f"Експірація: {expiry} хв після входу"
        if expiry is not None
        else "Експірація: не визначена"
    )

    lines = []
    found = set()

    for line in result.strip().splitlines():
        match = re.match(
            r"^[\s>*_#\-•]*(Напрямок|Експірація)\s*:",
            line, re.IGNORECASE
        )
        if match:
            key = match.group(1).lower()
            found.add(key)
            if key == "напрямок":
                lines.append(
                    f"Напрямок: {direction_label(direction)}"
                )
            else:
                lines.append(expiry_line)
        else:
            lines.append(line)

    if "напрямок" not in found:
        lines.append(f"Напрямок: {direction_label(direction)}")
    if "експірація" not in found:
        lines.append(expiry_line)

    if direction == "WAIT":
        replaced = False
        new_lines = []
        for line in lines:
            if re.match(r"^\s*Вхід\s*:", line, re.IGNORECASE):
                new_lines.append("Вхід: НЕ ВХОДИТИ")
                replaced = True
            else:
                new_lines.append(line)
        if not replaced:
            new_lines.append("Вхід: НЕ ВХОДИТИ")
        lines = new_lines

    return "\n".join(lines)


def parse_model_json(raw):
    raw = (raw or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise RuntimeError("Nora повернула некоректну структуру відповіді")
        data = json.loads(match.group(0))

    if not isinstance(data, dict):
        raise RuntimeError("Некоректна відповідь моделі")
    return data


# ============================================================
# 6. АНАЛІЗ ЗОБРАЖЕНЬ ТА РІШЕННЯ ПРО НАСТУПНИЙ КРОК
# ============================================================

def image_mime(image_bytes):
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def call_model_for_decision(image_rows, matrix_text, round_no):
    content = [{
        "type": "input_text",
        "text": (
            f"Це раунд {round_no} сесії аналізу Nora.\n"
            "Зображення згруповані за раундами. У межах одного "
            "раунду вони є різними масштабами приблизно одного "
            "моменту. Повторні знімки того самого масштабу в "
            "пізнішому раунді — спостереження за зміною в часі.\n\n"
            "ПОТОЧНА МАТРИЦЯ ЧАСУ:\n" + matrix_text
        )
    }]

    previous_round = None
    for index, row in enumerate(image_rows, start=1):
        round_number, captured_at, old_matrix, image_bytes = row
        if round_number != previous_round:
            content.append({
                "type": "input_text",
                "text": (
                    f"РАУНД {round_number}; час отримання: {captured_at}. "
                    "Порівнюй із попередніми раундами, якщо це той самий "
                    "таймфрейм."
                )
            })
            previous_round = round_number

        content.append({
            "type": "input_text",
            "text": f"Зображення {index}. Визнач таймфрейм за позначкою."
        })
        encoded = base64.b64encode(image_bytes).decode("utf-8")
        content.append({
            "type": "input_image",
            "image_url": (
                f"data:{image_mime(image_bytes)};base64,{encoded}"
            ),
            "detail": "high"
        })

    response = client.responses.create(
        model=MODEL,
        instructions=SYSTEM_PROMPT + "\n\n" + DECISION_PROMPT,
        input=[{"role": "user", "content": content}],
        max_output_tokens=MAX_OUTPUT_TOKENS
    )

    raw = response.output_text or ""
    if not raw.strip():
        raise RuntimeError("Модель повернула порожню відповідь")
    if getattr(response, "status", None) == "incomplete":
        raise RuntimeError("Відповідь моделі обірвалася")
    return parse_model_json(raw)


def save_prediction(analysis, raw, images, platform_time):
    pair = get_field(analysis, "Пара")
    direction = parse_direction(analysis)
    entry = get_field(analysis, "Вхід")
    expiry = parse_expiry(analysis)

    with db_connect() as db:
        cursor = db.execute("""
            INSERT INTO predictions (
                created_at, platform_time, pair, direction,
                entry, expiry_minutes, analysis,
                screenshot_count, outcome, raw_text, platform_minute
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        """, (
            platform_time.isoformat(),
            platform_time.strftime("%Y-%m-%d %H:%M:%S"),
            pair,
            direction,
            entry,
            expiry,
            analysis,
            len(images),
            raw,
            platform_time.minute
        ))

        prediction_id = cursor.lastrowid
        for image_bytes in images:
            db.execute("""
                INSERT INTO screenshots(prediction_id, image_data)
                VALUES (?, ?)
            """, (prediction_id, sqlite3.Binary(image_bytes)))

    return prediction_id


# ============================================================
# 7. КНОПКИ РЕЗУЛЬТАТІВ І СТАТИСТИКА
# ============================================================

def result_keyboard(prediction_id, direction):
    if direction == "WAIT":
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "ЧЕКАЄМО — пропуск",
                callback_data=f"result:{prediction_id}:skip"
            )
        ]])

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "Зайшла (+)",
                callback_data=f"result:{prediction_id}:win"
            ),
            InlineKeyboardButton(
                "Не зайшла (−)",
                callback_data=f"result:{prediction_id}:loss"
            )
        ],
        [InlineKeyboardButton(
            "Пропустила",
            callback_data=f"result:{prediction_id}:skip"
        )]
    ])


async def handle_result(update, context):
    query = update.callback_query
    try:
        _, prediction_id, outcome = query.data.split(":")
        prediction_id = int(prediction_id)
    except (ValueError, AttributeError):
        await query.answer("Не вдалося прочитати результат.")
        return

    if outcome not in ("win", "loss", "skip"):
        await query.answer("Невідомий результат.")
        return

    with db_connect() as db:
        cursor = db.execute("""
            UPDATE predictions
            SET outcome=?
            WHERE id=? AND outcome='pending'
        """, (outcome, prediction_id))
        changed = cursor.rowcount

    if not changed:
        await query.answer("Цей прогноз уже зафіксований.")
        return

    await query.answer("Результат збережено.")
    labels = {"win": "ПЛЮС", "loss": "МІНУС", "skip": "ПРОПУЩЕНО"}
    old_text = query.message.text or ""
    await query.edit_message_text(
        old_text + f"\n\nРезультат: {labels[outcome]}",
        reply_markup=None
    )


async def stats_command(update, context):
    with db_connect() as db:
        rows = db.execute("""
            SELECT outcome, COUNT(*) FROM predictions GROUP BY outcome
        """).fetchall()
        directions = db.execute("""
            SELECT direction, outcome, COUNT(*)
            FROM predictions
            GROUP BY direction, outcome
            ORDER BY direction, outcome
        """).fetchall()

    counts = dict(rows)
    wins = counts.get("win", 0)
    losses = counts.get("loss", 0)
    skipped = counts.get("skip", 0)
    pending = counts.get("pending", 0)
    total = sum(counts.values())
    completed = wins + losses
    accuracy = wins / completed * 100 if completed else 0

    direction_names = {
        "CALL": "ВГОРУ",
        "PUT": "ВНИЗ",
        "WAIT": "ЧЕКАЄМО",
    }
    outcome_names = {
        "win": "плюс",
        "loss": "мінус",
        "skip": "пропуск",
        "pending": "очікує",
    }

    lines = [
        "📊 СТАТИСТИКА NORA",
        "",
        f"Усього прогнозів: {total}",
        f"🟢 Плюсів: {wins}",
        f"🔴 Мінусів: {losses}",
        f"⚪ Пропущено: {skipped}",
        f"⏳ Очікують: {pending}",
        "",
        f"Точність серед угод: {accuracy:.1f}%",
        f"Враховано угод: {completed}",
        "",
        "ЗА НАПРЯМКАМИ:",
    ]

    for direction, outcome, count in directions:
        name = direction_names.get(direction, direction)
        result = outcome_names.get(outcome, outcome)
        lines.append(f"{name} — {result}: {count}")

    await update.message.reply_text("\n".join(lines))


# ============================================================
# 8. КНОПКИ РОБОТИ / ВІДПОЧИНКУ
# ============================================================

def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["▶️ Почати роботу"],
            ["🌙 Завершити роботу"],
        ],
        resize_keyboard=True,
        is_persistent=True
    )


async def start_work(update, context):
    chat_id = update.effective_chat.id
    set_working(True)
    await update.effective_message.reply_text(
        "🟢 Nora почала роботу.\n"
        "Надсилай початковий пакет із трьох скриншотів: H1, M15 і M3.",
        reply_markup=main_keyboard()
    )


async def stop_work(update, context):
    chat_id = update.effective_chat.id
    cancel_all_sessions()
    set_working(False)
    await update.effective_message.reply_text(
        "🌙 Nora завершує роботу й відпочиває.\n"
        "Усі незавершені аналізи та заплановані нагадування скасовано.",
        reply_markup=main_keyboard()
    )


async def start_command(update, context):
    status = "працюю" if is_working() else "відпочиваю"
    await update.message.reply_text(
        "Я Nora — аналітична система Матриці часу.\n"
        f"Поточний режим: {status}.\n\n"
        "▶️ Почати роботу — активувати аналіз.\n"
        "🌙 Завершити роботу — скасувати незавершені сесії.\n\n"
        "/time — часова матриця\n"
        "/stats — статистика прогнозів",
        reply_markup=main_keyboard()
    )


async def time_command(update, context):
    await update.message.reply_text(full_matrix(now_platform()))


# ============================================================
# 9. ОБРОБКА СЕСІЇ
# ============================================================

def clean_request(data):
    action = str(data.get("action", "")).lower()
    timeframe = str(data.get("timeframe", "")).upper().strip()
    reason = str(data.get("reason", "")).strip()
    try:
        delay = int(data.get("delay_minutes", 0))
    except (TypeError, ValueError):
        delay = 0

    allowed = {name for name, _ in TIMEFRAMES}
    if action == "request":
        if timeframe not in allowed or not reason:
            raise RuntimeError("Модель запросила некоректний скриншот")
        delay = max(0, min(60, delay))
    return action, timeframe, delay, reason


async def send_final_prediction(
    chat_id, session_id, analysis, raw, context
):
    analysis = validate_analysis(analysis)
    direction = parse_direction(analysis)
    platform_time = now_platform()

    image_rows = load_session_images(session_id)
    images = [row[3] for row in image_rows]

    if not images:
        finish_session(session_id, "cancelled", platform_time)
        await context.bot.send_message(
            chat_id,
            "Сесію скасовано: не залишилося скриншотів для фінального аналізу."
        )
        return

    prediction_id = await asyncio.to_thread(
        save_prediction, analysis, raw, images, platform_time
    )

    finish_session(session_id, "completed", platform_time)

    keyboard = result_keyboard(prediction_id, direction)
    final_text = (
        f"📊 {get_field(analysis, 'Пара')}\n"
        f"━━━━━━━━━━━━━━\n"
        f"🧭 {direction_label(direction)}\n\n"
        f"{analysis}\n\n"
        f"🕒 Час платформи: {platform_time:%H:%M:%S}\n"
        f"🔢 Прогноз №{prediction_id}\n\n"
        "Зафіксуй результат:"
    )
    await context.bot.send_message(
        chat_id, final_text, reply_markup=keyboard
    )


async def evaluate_session(session_id, context):
    lock = SESSION_LOCKS.setdefault(session_id, asyncio.Lock())
    async with lock:
        session = get_session(session_id)
        if not session or session[3] not in ("processing", "waiting"):
            return
        if not is_working():
            finish_session(session_id, "cancelled", now_platform())
            return

        image_rows = load_session_images(session_id)
        if not image_rows:
            return

        now = now_platform()
        matrix_text = full_matrix(now)
        round_no = max(row[0] for row in image_rows)

        try:
            decision = await asyncio.to_thread(
                call_model_for_decision,
                image_rows,
                matrix_text,
                round_no
            )
            action, timeframe, delay, reason = clean_request(decision)

            # Якщо запит додаткового скриншота допустимий — чекаємо.
            if action == "request" and round_no < MAX_FOLLOWUP_ROUNDS:
                due_at = now_platform() + timedelta(minutes=delay)
                set_session_request(
                    session_id,
                    round_no + 1,
                    timeframe,
                    reason,
                    due_at,
                    now_platform()
                )

                when_text = (
                    "зараз"
                    if delay == 0
                    else f"через {delay} хвилин, приблизно о {due_at:%H:%M:%S}"
                )
                if delay == 0:
                    # Запит уже надіслано нижче; позначаємо його як повідомлений,
                    # щоб планувальник не дублював повідомлення через 10 секунд.
                    with db_connect() as db:
                        db.execute("""
                            UPDATE analysis_sessions
                            SET due_notified=1, updated_at=?
                            WHERE id=?
                        """, (now_platform().isoformat(), session_id))

                await context.bot.send_message(
                    session[1],
                    f"🔎 Потрібне ще одне спостереження.\n\n"
                    f"Скриншот: {timeframe}\n"
                    f"Коли: {when_text}.\n"
                    f"Навіщо: {reason}\n\n"
                    "Надішли скриншот окремим повідомленням або альбомом. "
                    "Він буде доданий до поточної сесії."
                )
                return

            # Не дозволяємо нескінченні додаткові раунди.
            analysis = str(decision.get("analysis", "")).strip()
            if action != "final" or not analysis:
                analysis = (
                    "Пара: не визначена\n"
                    "Ситуація: недостатньо підтверджень після додаткових спостережень\n"
                    "Масштаби: надані скриншоти\n"
                    "Стадія поштовху: не визначено\n"
                    "Сценарій: потрібне нове спостереження\n"
                    "Напрямок: ЧЕКАЄМО ⏳\n"
                    "Вхід: НЕ ВХОДИТИ\n"
                    "Експірація: не визначена\n"
                    "Скасування: —\n"
                    "Чому: ліміт додаткових раундів вичерпано або модель не надала фінального рішення."
                )

            await send_final_prediction(
                session[1], session_id, analysis,
                json.dumps(decision, ensure_ascii=False), context
            )

        except Exception:
            logger.exception("Помилка оцінювання сесії %s", session_id)
            finish_session(session_id, "cancelled", now_platform())
            await context.bot.send_message(
                session[1],
                "Не вдалося завершити аналіз через технічну помилку. "
                "Сесію скасовано; прогноз до статистики не додано."
            )


# ============================================================
# 10. ПРИЙМАННЯ СКРИНШОТІВ
# ============================================================

async def download_images(messages, context):
    images = []
    for message in messages:
        file_id = None
        if message.photo:
            file_id = message.photo[-1].file_id
        elif (
            message.document
            and (message.document.mime_type or "").startswith("image/")
        ):
            file_id = message.document.file_id

        if not file_id:
            continue

        telegram_file = await context.bot.get_file(file_id)
        image_bytes = bytes(
            await telegram_file.download_as_bytearray()
        )
        images.append(image_bytes)

    return images


async def process_album(messages, context):
    if not messages:
        return

    messages.sort(key=lambda message: message.message_id)
    messages = messages[:MAX_IMAGES]

    first = messages[0]
    chat_id = first.chat_id
    user_id = first.from_user.id if first.from_user else 0

    if not is_working():
        # У режимі відпочинку Nora не реагує на скриншоти.
        return

    active = get_active_session(chat_id)

    status_message = await first.reply_text(
        "📷 Отримала скриншоти. Перевіряю та додаю до сесії..."
    )

    try:
        images = await download_images(messages, context)
        if not images:
            await status_message.edit_text(
                "Не вдалося отримати зображення."
            )
            return

        captured_at = now_platform()
        matrix_text = full_matrix(captured_at)

        if active:
            session_id = active[0]
            session_status = active[3]
            requested_tf = active[5]
            requested_round = active[4]

            if session_status != "waiting":
                await status_message.edit_text(
                    "Nora ще обробляє поточний пакет. "
                    "Дочекайся її наступного запиту."
                )
                return

            # Користувач надіслав скриншот до запланованого часу —
            # це все одно може бути корисним спостереженням.
            round_no = requested_round
            add_session_images(
                session_id, round_no, images, matrix_text, captured_at
            )
            finish_session(session_id, "processing", captured_at)

            await status_message.edit_text(
                f"✅ Додала {len(images)} скриншот(ів) до поточної сесії. "
                "Порівнюю їх із попередніми спостереженнями..."
            )
            await evaluate_session(session_id, context)
            return

        # Нова сесія: початковий пакет має містити H1, M15 і M3.
        session_id = create_session(chat_id, user_id, captured_at)
        add_session_images(
            session_id, 0, images, matrix_text, captured_at
        )

        await status_message.edit_text(
            f"📷 Початковий пакет отримано: {len(images)} зображень.\n"
            "Об'єдную H1, M15 і M3 та оцінюю, чи потрібні додаткові спостереження..."
        )
        await evaluate_session(session_id, context)

    except Exception:
        logger.exception("Помилка обробки скриншотів")
        try:
            await status_message.edit_text(
                "Не вдалося обробити скриншоти. "
                "Сесію не зараховано до статистики."
            )
        except Exception:
            pass


async def flush_album(key, context):
    try:
        await asyncio.sleep(ALBUM_WAIT)
        album = ALBUMS.pop(key, None)
        if album:
            await process_album(album["messages"], context)
    except asyncio.CancelledError:
        pass


async def photo_handler(update, context):
    message = update.effective_message
    if not message or not (message.photo or message.document):
        return

    if not is_working():
        return

    media_group_id = message.media_group_id
    if not media_group_id:
        await process_album([message], context)
        return

    key = (update.effective_chat.id, media_group_id)
    album = ALBUMS.setdefault(
        key, {"messages": [], "task": None}
    )
    album["messages"].append(message)

    old_task = album.get("task")
    if old_task and not old_task.done():
        old_task.cancel()

    album["task"] = asyncio.create_task(
        flush_album(key, context)
    )


# ============================================================
# 11. НАГАДУВАННЯ ТА АВТОМАТИЧНЕ СКАСУВАННЯ
# ============================================================

def due_sessions(now):
    with db_connect() as db:
        return db.execute("""
            SELECT id, chat_id, request_timeframe, request_reason,
                   due_at, due_notified, reminder_at, reminder_sent,
                   cancel_at
            FROM analysis_sessions
            WHERE status='waiting'
        """).fetchall()


async def scheduler_loop(application):
    while True:
        try:
            if is_working():
                now = now_platform()
                rows = due_sessions(now)

                for row in rows:
                    (
                        session_id, chat_id, timeframe, reason,
                        due_at_raw, due_notified, reminder_at_raw,
                        reminder_sent, cancel_at_raw
                    ) = row

                    due_at = datetime.fromisoformat(due_at_raw)
                    reminder_at = datetime.fromisoformat(reminder_at_raw)
                    cancel_at = datetime.fromisoformat(cancel_at_raw)

                    if not due_notified and now >= due_at:
                        mark_due_notified(session_id, now)
                        await application.bot.send_message(
                            chat_id,
                            f"⏰ Час скриншота {timeframe}.\n"
                            f"Надішли його зараз.\n"
                            f"Мета: {reason}"
                        )

                    elif due_notified and not reminder_sent and now >= reminder_at:
                        mark_reminder_sent(session_id, now)
                        await application.bot.send_message(
                            chat_id,
                            f"🔔 Нагадування: Nora ще чекає скриншот {timeframe}.\n"
                            "Надішли його протягом наступних 2 хвилин, "
                            "інакше поточний аналіз буде скасовано."
                        )

                    elif reminder_sent and now >= cancel_at:
                        finish_session(session_id, "cancelled", now)
                        await application.bot.send_message(
                            chat_id,
                            "⌛ Скриншот не надійшов. Поточний аналіз скасовано.\n"
                            "Прогноз не створено, статистику не змінено."
                        )

            await asyncio.sleep(SCHEDULER_INTERVAL_SECONDS)

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Помилка планувальника Nora")
            await asyncio.sleep(SCHEDULER_INTERVAL_SECONDS)


async def post_init(application):
    application.create_task(scheduler_loop(application))


# ============================================================
# 12. ЗАПУСК NORA
# ============================================================

async def text_button_handler(update, context):
    text = (update.effective_message.text or "").strip()

    if text == "▶️ Почати роботу":
        await start_work(update, context)
    elif text == "🌙 Завершити роботу":
        await stop_work(update, context)


def main():
    init_db()

    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start_command)
    )
    application.add_handler(
        CommandHandler("stats", stats_command)
    )
    application.add_handler(
        CommandHandler("time", time_command)
    )
    application.add_handler(
        CallbackQueryHandler(
            handle_result,
            pattern=r"^result:"
        )
    )

    application.add_handler(
        MessageHandler(
            filters.Regex(r"^(▶️ Почати роботу|🌙 Завершити роботу)$"),
            text_button_handler
        )
    )

    application.add_handler(
        MessageHandler(
            filters.PHOTO | filters.Document.IMAGE,
            photo_handler
        )
    )

    logger.info(
        "Nora запускається. Режим роботи збережено: %s",
        is_working()
    )
    application.run_polling()


if __name__ == "__main__":
    main()
