
import os
import re
import base64
import logging
import asyncio
import sqlite3

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from openai import OpenAI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

logger = logging.getLogger("Nora")
ALBUMS = {}


# ============================================================
# 2. МАТРИЦЯ ЧАСУ — ВБУДОВАНА В MAIN.PY
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
    H4_ANCHOR_HOUR = int(
        os.getenv("H4_ANCHOR_HOUR", "1")
    ) % 24
except ValueError:
    H4_ANCHOR_HOUR = 1


def now_platform():
    utc_now = datetime.now(timezone.utc)

    offset = parse_offset(
        os.getenv("PLATFORM_UTC_OFFSET", "")
    )

    if offset is None:
        kyiv_now = utc_now.astimezone(
            ZoneInfo("Europe/Kyiv")
        )
        offset = (
            kyiv_now.utcoffset().total_seconds() / 3600 + 1
        )

    return utc_now.astimezone(
        timezone(timedelta(hours=offset))
    )


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

    return (
        f"Склад хвилини :{now.minute:02d}: "
        + ", ".join(parts)
    )


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
            name
            for name, minutes in TIMEFRAMES
            if name != "M1"
            and (
                total - timeframe_shift(minutes)
            ) % minutes == 0
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


# ============================================================
# 3. ІНСТРУКЦІЯ АНАЛІЗУ NORA
# ============================================================

SYSTEM_PROMPT = f"""
Ти Nora — аналітична помічниця користувача.
Твоя основа — авторська система «Матриця часу».

ГОЛОВНА ІДЕЯ:

Різні таймфрейми — різні мікровиміри одного руху.
Матриця часу об'єднує їх в одне ціле.
Матриця — порожній календар, а не самостійний прогноз.

Скриншоти заповнюють календар цінами, рівнями,
розмірами свічок і хвостів, швидкістю, ритмом
та стадіями руху.

УСІ СКРИНШОТИ:

- Аналізуй як одну структуру одного інструмента.
- Це різні масштаби приблизно одного моменту.
- Не вважай порядок скриншотів послідовністю руху.
- Визначай таймфрейм за видимою позначкою.
- Не вигадуй відсутні таймфрейми.
- Якщо валютні пари різні або це неможливо перевірити — WAIT.

ПОСЛІДОВНІСТЬ АНАЛІЗУ:

1. СТРУКТУРА РУХУ

Визнач, що зараз відбувається:
- продовження;
- розворот;
- корекція;
- накопичення;
- затор;
- пружина;
- шум;
- змішана структура;
- невизначеність.

Не називай будь-який хвіст пружиною.
Не називай будь-який відскок розворотом.

2. МІКРОРУХИ

Проаналізуй:
- розмір тіл свічок;
- довжину хвостів;
- прискорення та уповільнення;
- ритм руху;
- стискання та розширення;
- реакцію на рівні;
- взаємодію молодших і старших масштабів.

Визнач, де рух починається, де розвивається,
де втрачає швидкість і де може змінитися.

3. СЦЕНАРІЙ

Визнач найбільш обґрунтований сценарій:
- продовження руху;
- зміна напрямку;
- корекція;
- боковий рух;
- невизначеність.

Відокремлюй видимі факти від припущень.

4. ПОШТОВХ

Визнач:
- можливий напрямок;
- що може запустити поштовх;
- коли приблизно він може відбутися;
- яка умова підтвердить сценарій;
- що його скасує.

Часовий стик матриці сам по собі не є сигналом.

5. ВХІД

Обери тільки один варіант:
- ЗАРАЗ;
- ЧЕРЕЗ N ХВ;
- ПІСЛЯ УМОВИ: конкретна умова;
- НЕ ВХОДИТИ.

Не вигадуй точний час входу.

6. ЕКСПІРАЦІЯ

Визнач окремо від напрямку та входу.

Експірація — тривалість після фактичного входу.
Допустимий діапазон: 1–60 хвилин.

Не використовуй автоматично формулу:
кількість свічок одного таймфрейму × його тривалість.

Не прив'язуй експірацію тільки до одного масштабу.
Не використовуй постійні 3 або 15 хвилин.
Не обирай експірацію лише через часовий стик.

Оціни:
- очікуваний розвиток руху;
- стадію формування;
- швидкість і ритм;
- відстань до рівнів;
- взаємодію різних масштабів;
- можливий час розвитку саме цього сценарію.

Обери одне число від 1 до 60 хвилин.
Обов'язково поясни, чому саме стільки часу.
Якщо тривалість неможливо обґрунтувати — WAIT.

7. КОНТРОЛЬ

Якщо напрямок суперечливий,
вхід не має підтвердження,
скриншотів недостатньо
або експірація не обґрунтована — WAIT.

Не вигадуй відсотки точності.
Не обіцяй прибуток.

H4 у часовій матриці прив'язана до
{H4_ANCHOR_HOUR:02d}:00 за часом платформи.
Фактичне вирівнювання H4 брокера може відрізнятися.

ВІДПОВІДАЙ КОРОТКО, УКРАЇНСЬКОЮ.

ФОРМАТ:

Пара: ...
Структура: ...
Стадія руху: ...
Взаємодія масштабів: ...
Сценарій: ...
Напрямок: CALL / PUT / WAIT
Поштовх: ...
Вхід: ...
Експірація: N хв після входу / не визначена
Скасування сценарію: ...
Причина експірації: ...
Причина рішення: ...
"""


# ============================================================
# 4. БАЗА ДАНИХ
# ============================================================

def db_connect():
    db = sqlite3.connect(DB_PATH, timeout=20)
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
            row[1]
            for row in db.execute(
                "PRAGMA table_info(predictions)"
            ).fetchall()
        }

        for column, kind in (
            ("raw_text", "TEXT"),
            ("platform_minute", "INTEGER"),
        ):
            if column not in existing:
                db.execute(
                    f"ALTER TABLE predictions ADD COLUMN "
                    f"{column} {kind}"
                )

    logger.info("База Nora готова: %s", DB_PATH)


# ============================================================
# 5. РОЗБІР ВІДПОВІДІ
# ============================================================

def get_field(text, field):
    pattern = (
        r"^[\s>*_#\-•]*"
        + re.escape(field)
        + r"[\s*_]*:[\s*_]*(.*)$"
    )

    match = re.search(
        pattern,
        text,
        re.IGNORECASE | re.MULTILINE
    )

    return match.group(1).strip(" *_") if match else ""


def parse_direction(text):
    value = get_field(text, "Напрямок").upper()
    found = set(
        re.findall(r"\b(CALL|PUT|WAIT)\b", value)
    )

    if len(found) == 1:
        return found.pop()

    return "WAIT"


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
            line,
            re.IGNORECASE
        )

        if match:
            key = match.group(1).lower()
            found.add(key)

            if key == "напрямок":
                lines.append(f"Напрямок: {direction}")
            else:
                lines.append(expiry_line)
        else:
            lines.append(line)

    if "напрямок" not in found:
        lines.append(f"Напрямок: {direction}")

    if "експірація" not in found:
        lines.append(expiry_line)

    if direction == "WAIT":
        replaced = False
        new_lines = []

        for line in lines:
            if re.match(r"^\s*Вхід\s*:", line, re.IGNORECASE):
                new_lines.append("Вхід: не входити")
                replaced = True
            else:
                new_lines.append(line)

        if not replaced:
            new_lines.append("Вхід: не входити")

        lines = new_lines

    return "\n".join(lines)


# ============================================================
# 6. АНАЛІЗ ЗОБРАЖЕНЬ
# ============================================================

def image_mime(image_bytes):
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"

    return "image/jpeg"


def analyze_images(images, matrix_text):
    content = [{
        "type": "input_text",
        "text": (
            "Проаналізуй усі скриншоти як одну структуру "
            "різних мікровимірів одного графіка.\n"
            "Це приблизно один момент часу, не послідовність.\n\n"
            "ЧАСОВА МАТРИЦЯ:\n"
            + matrix_text
        )
    }]

    for index, image_bytes in enumerate(images, start=1):
        encoded = base64.b64encode(
            image_bytes
        ).decode("utf-8")

        content.append({
            "type": "input_text",
            "text": (
                f"Скриншот {index}. "
                "Визнач таймфрейм за видимою позначкою."
            )
        })

        content.append({
            "type": "input_image",
            "image_url": (
                f"data:{image_mime(image_bytes)};"
                f"base64,{encoded}"
            ),
            "detail": "high"
        })

    response = client.responses.create(
        model=MODEL,
        instructions=SYSTEM_PROMPT,
        input=[{
            "role": "user",
            "content": content
        }],
        max_output_tokens=MAX_OUTPUT_TOKENS
    )

    raw = response.output_text or ""

    if not raw.strip():
        raise RuntimeError("Модель повернула порожню відповідь")

    if getattr(response, "status", None) == "incomplete":
        raise RuntimeError(
            "Відповідь моделі обірвалася. "
            "Прогноз не збережено."
        )

    return validate_analysis(raw), raw


# ============================================================
# 7. ЗБЕРЕЖЕННЯ ПРОГНОЗУ
# ============================================================

def save_prediction(analysis, raw, images, platform_time):
    pair = get_field(analysis, "Пара")
    direction = parse_direction(analysis)
    entry = get_field(analysis, "Вхід")
    expiry = parse_expiry(analysis)

    with db_connect() as db:
        cursor = db.execute("""
            INSERT INTO predictions (
                created_at,
                platform_time,
                pair,
                direction,
                entry,
                expiry_minutes,
                analysis,
                screenshot_count,
                outcome,
                raw_text,
                platform_minute
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
                INSERT INTO screenshots (
                    prediction_id,
                    image_data
                )
                VALUES (?, ?)
            """, (
                prediction_id,
                sqlite3.Binary(image_bytes)
            ))

    return prediction_id


# ============================================================
# 8. КНОПКИ РЕЗУЛЬТАТІВ
# ============================================================

def result_keyboard(prediction_id, direction):
    if direction == "WAIT":
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "WAIT — пропуск",
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
        [
            InlineKeyboardButton(
                "Пропустила",
                callback_data=f"result:{prediction_id}:skip"
            )
        ]
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
            SET outcome = ?
            WHERE id = ? AND outcome = 'pending'
        """, (outcome, prediction_id))

        changed = cursor.rowcount

    if not changed:
        await query.answer(
            "Цей прогноз уже зафіксований."
        )
        return

    await query.answer("Результат збережено.")

    labels = {
        "win": "ПЛЮС",
        "loss": "МІНУС",
        "skip": "ПРОПУЩЕНО"
    }

    old_text = query.message.text or ""

    await query.edit_message_text(
        old_text + f"\n\nРезультат: {labels[outcome]}",
        reply_markup=None
    )


# ============================================================
# 9. СТАТИСТИКА
# ============================================================

async def stats_command(update, context):
    with db_connect() as db:
        rows = db.execute("""
            SELECT outcome, COUNT(*)
            FROM predictions
            GROUP BY outcome
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

    accuracy = (
        wins / completed * 100
        if completed else 0
    )

    lines = [
        "СТАТИСТИКА NORA",
        "",
        f"Усього прогнозів: {total}",
        f"Плюсів: {wins}",
        f"Мінусів: {losses}",
        f"Пропущено: {skipped}",
        f"Очікують результату: {pending}",
        "",
        f"Точність серед угод із результатом: {accuracy:.1f}%",
        f"Враховано угод: {completed}",
        "",
        "ЗА НАПРЯМКАМИ:",
    ]

    for direction, outcome, count in directions:
        lines.append(
            f"{direction} — {outcome}: {count}"
        )

    await update.message.reply_text(
        "\n".join(lines)
    )


# ============================================================
# 10. КОМАНДИ
# ============================================================

async def start_command(update, context):
    await update.message.reply_text(
        "Я Nora — аналітична система Матриці часу.\n\n"
        "Надішли 3–5 скриншотів однієї валютної пари "
        "одним пакетом.\n\n"
        "/time — часова матриця\n"
        "/stats — статистика прогнозів"
    )


async def time_command(update, context):
    now = now_platform()

    await update.message.reply_text(
        full_matrix(now)
    )


# ============================================================
# 11. ПРИЙМАННЯ СКРИНШОТІВ
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

    messages.sort(
        key=lambda message: message.message_id
    )

    messages = messages[:MAX_IMAGES]

    status = await messages[0].reply_text(
        f"Отримала {len(messages)} скриншотів.\n"
        "Об'єдную мікровиміри та Матрицю часу..."
    )

    try:
        images = await download_images(
            messages,
            context
        )

        if not images:
            await status.edit_text(
                "Не вдалося отримати зображення."
            )
            return

        analysis_time = now_platform()
        matrix_text = full_matrix(analysis_time)

        result, raw = await asyncio.to_thread(
            analyze_images,
            images,
            matrix_text
        )

        prediction_id = await asyncio.to_thread(
            save_prediction,
            result,
            raw,
            images,
            analysis_time
        )

        direction = parse_direction(result)

        keyboard = result_keyboard(
            prediction_id,
            direction
        )

        final_text = (
            f"{result}\n\n"
            f"Час платформи: {analysis_time:%H:%M:%S}\n"
            f"Номер прогнозу: #{prediction_id}\n\n"
            "Зафіксуй результат кнопкою нижче:"
        )

        await status.edit_text(
            final_text,
            reply_markup=keyboard
        )

    except Exception:
        logger.exception("Помилка аналізу пакета")

        await status.edit_text(
            "Не вдалося завершити аналіз.\n"
            "Спробуй надіслати пакет ще раз."
        )


async def flush_album(key, context):
    try:
        await asyncio.sleep(ALBUM_WAIT)

        album = ALBUMS.pop(key, None)

        if album:
            await process_album(
                album["messages"],
                context
            )

    except asyncio.CancelledError:
        pass


async def photo_handler(update, context):
    message = update.effective_message

    if not message:
        return

    if not (message.photo or message.document):
        return

    media_group_id = message.media_group_id

    if not media_group_id:
        await process_album([message], context)
        return

    key = (
        update.effective_chat.id,
        media_group_id
    )

    album = ALBUMS.setdefault(
        key,
        {"messages": [], "task": None}
    )

    album["messages"].append(message)

    old_task = album.get("task")

    if old_task and not old_task.done():
        old_task.cancel()

    album["task"] = asyncio.create_task(
        flush_album(key, context)
    )


# ============================================================
# 12. ЗАПУСК NORA
# ============================================================

def main():
    init_db()

    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
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
            filters.PHOTO | filters.Document.IMAGE,
            photo_handler
        )
    )

    logger.info("Nora запускається")

    application.run_polling()


if __name__ == "__main__":
    main()
