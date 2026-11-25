import os
import base64
import logging
import asyncio
import math
import re
import sqlite3

from openai import OpenAI
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from matrix import full_matrix, now_platform


TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
    raise RuntimeError("Не задані ключі Telegram або OpenAI")

client = OpenAI(api_key=OPENAI_API_KEY)

# Модель можна змінити у Railway Variables (OPENAI_MODEL), наприклад gpt-4.1
MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
MAX_IMAGES = 5
ALBUM_WAIT = 3.0
MAX_OUTPUT_TOKENS = 750
DB_PATH = os.getenv("DB_PATH", "/data/nora.db")

# Виплата брокера для статистики (поріг окупності = 1 / (1 + виплата)).
try:
    PAYOUT = float(os.getenv("PAYOUT", "0.85"))
except ValueError:
    PAYOUT = 0.85

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

logger = logging.getLogger("Nora")
ALBUMS = {}


SYSTEM_PROMPT = """
Ти Nora — аналітична помічниця користувача.
Твоя основа — авторська система «Матриця часу».

Різні таймфрейми — різні мікровиміри одного руху.
Матриця часу — порожній календар, який сам не прогнозує напрямок.
Скриншоти заповнюють його цінами, рівнями, тілами,
хвостами, швидкістю, ритмом і стадіями руху.

Аналізуй усі скриншоти як одну структуру одного інструмента.
Це різні масштаби приблизно одного моменту, не послідовні кадри.
Не вигадуй відсутні таймфрейми.
Якщо пари різні — видай WAIT.

Оціни:
- напрямок і структуру;
- рівні та реакцію ціни;
- тіла свічок і хвости;
- швидкість, ритм, прискорення та уповільнення;
- взаємодію масштабів;
- пружину, шум і затор.

Матриця — тільки часова координатна сітка.
Часовий стик не є автоматичним сигналом.
Не перераховуй час матриці самостійно.

H4 у матриці прив'язана до 01:00 часу платформи:
свічки H4 відкриваються о 01, 05, 09, 13, 17, 21.

Окремо визнач:
1. Момент входу: зараз, орієнтовно через N хв
   або чекати конкретну умову.
   Вхід має бути ОДНИМ: або «зараз», або одна конкретна умова.
   Не пиши «або ... або» у полі Вхід.
   Не вигадуй точний час входу.

2. ЕКСПІРАЦІЯ (1–60 хв після входу):
   - Визнач робочий масштаб ідеї: на якому таймфреймі видно сценарій.
   - Оціни, скільки свічок цього масштабу потрібно, щоб сценарій
     розвинувся (1–3), щоб підсумок не перевищував 60 хв.
   - Порахуй базу: кількість свічок × тривалість свічки в хвилинах
     (у межах 1–60 хв).
   - Якщо вхід «зараз» і поруч із моментом завершення є стик матриці,
     можна підсунути кінець до нього, але не більше ніж на 2 хв
     (не виходячи за 1–60 хв).
   - 15 за замовчуванням не використовуй; якщо вагаєшся — обери менше.

Не використовуй постійні 3 хвилини.
Не обирай експірацію лише через часовий стик.
Якщо немає обґрунтованого рішення — WAIT.

CALL — можливий рух угору.
PUT — можливий рух униз.
WAIT — немає достатніх підстав для входу.

Відповідай коротко, українською.
Не обіцяй прибуток.

ФОРМАТ (кожне поле з нового рядка, без жирного шрифту):

Пара: ...
Картина: ...
Напрямок: CALL / PUT / WAIT
Вхід: ...
Експірація: N хв після входу / не визначена
Причина: ... (тут же одним коротким реченням поясни, чому саме N)
"""


# ---------- База даних ----------

def db_connect():
    return sqlite3.connect(DB_PATH, timeout=15.0)


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

        # Нові стовпці для вже існуючої бази: сира відповідь моделі
        # і хвилина платформи (для перевірки календаря хвилин).
        for column, kind in (
            ("raw_text", "TEXT"),
            ("platform_minute", "INTEGER"),
        ):
            try:
                db.execute(
                    f"ALTER TABLE predictions ADD COLUMN {column} {kind}"
                )
            except sqlite3.OperationalError:
                pass  # стовпець уже існує

    logger.info("База даних Nora готова: %s", DB_PATH)


# ---------- Розбір відповіді моделі ----------

FIELD_PATTERN = r"^[\s>*_#\-•]*{field}[\s*_]*:[\s*_]*(.*)$"


def get_field(text, field):
    match = re.search(
        FIELD_PATTERN.format(field=re.escape(field)),
        text,
        re.IGNORECASE | re.MULTILINE
    )
    return match.group(1).strip(" *_") if match else ""


def parse_direction(text):
    """CALL / PUT / WAIT. Якщо не вдалося визначити однозначно — WAIT."""
    value = get_field(text, "Напрямок").upper()
    found = set(re.findall(r"\b(CALL|PUT|WAIT)\b", value))

    if len(found) == 1:
        return found.pop()

    return "WAIT"


def parse_expiry(text):
    value = get_field(text, "Експірація").lower()
    match = re.search(r"(\d{1,2})\s*хв", value)

    if match:
        number = int(match.group(1))
        if 1 <= number <= 60:
            return number

    return None


LINE_PATTERN = re.compile(
    r"^[\s>*_#\-•]*(напрямок|експірація|вхід)[\s*_]*:",
    re.IGNORECASE
)


def validate_analysis(result):
    direction = parse_direction(result)
    expiry = parse_expiry(result)

    if direction in ("CALL", "PUT") and expiry is None:
        direction = "WAIT"

    if direction == "WAIT":
        expiry = None

    expiry_line = (
        f"Експірація: {expiry} хв після входу"
        if expiry else "Експірація: не визначена"
    )

    output = []
    seen = set()

    for line in result.strip().splitlines():
        match = LINE_PATTERN.match(line)

        if not match:
            output.append(line)
            continue

        key = match.group(1).lower()
        seen.add(key)

        if key == "напрямок":
            output.append(f"Напрямок: {direction}")
        elif key == "експірація":
            output.append(expiry_line)
        elif direction == "WAIT":
            output.append("Вхід: не входити")
        else:
            output.append(line.strip())

    if "напрямок" not in seen:
        output.append(f"Напрямок: {direction}")

    if "експірація" not in seen:
        output.append(expiry_line)

    if direction == "WAIT" and "вхід" not in seen:
        output.append("Вхід: не входити")

    return "\n".join(output)


# ---------- Аналіз ----------

def image_mime(image_bytes):
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return "image/jpeg"


def analyze_images(images, matrix_text):
    content = [{
        "type": "input_text",
        "text": (
            "Проаналізуй усі скриншоти як одну структуру "
            "різних мікровимірів одного графіка.\n\n"
            "ЧАСОВА МАТРИЦЯ:\n"
            + matrix_text
        )
    }]

    for index, image_bytes in enumerate(images, start=1):
        encoded = base64.b64encode(image_bytes).decode("utf-8")

        content.append({
            "type": "input_text",
            "text": f"Скриншот {index}: окремий часовий масштаб."
        })

        content.append({
            "type": "input_image",
            "image_url": f"data:{image_mime(image_bytes)};base64,{encoded}",
            "detail": "high"
        })

    response = client.responses.create(
        model=MODEL,
        instructions=SYSTEM_PROMPT,
        input=[{"role": "user", "content": content}],
        max_output_tokens=MAX_OUTPUT_TOKENS
    )

    raw = response.output_text or ""
    validated = validate_analysis(raw)

    if getattr(response, "status", None) == "incomplete":
        validated += (
            "\n\nВідповідь обірвалась, висновок міг бути неповним."
        )

    return validated, raw


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
                screenshot_count, outcome,
                raw_text, platform_minute
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
                    prediction_id, image_data
                )
                VALUES (?, ?)
            """, (prediction_id, sqlite3.Binary(image_bytes)))

    return prediction_id


# ---------- Кнопки результату ----------

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


async def handle_result(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    # На callback можна відповісти лише один раз, тому відповідаємо
    # в кінці кожної гілки.
    try:
        _, prediction_id, outcome = query.data.split(":")
        prediction_id = int(prediction_id)
    except (ValueError, AttributeError):
        await query.answer("Не вдалося прочитати результат.")
        return

    if outcome not in ("win", "loss", "skip"):
        await query.answer()
        return

    with db_connect() as db:
        cursor = db.execute("""
            UPDATE predictions
            SET outcome = ?
            WHERE id = ? AND outcome = 'pending'
        """, (outcome, prediction_id))

        changed = cursor.rowcount

    if not changed:
        await query.answer("Цей прогноз уже зафіксований.")
        return

    await query.answer()

    labels = {
        "win": "ПЛЮС",
        "loss": "МІНУС",
        "skip": "ПРОПУЩЕНО"
    }

    old_text = query.message.text or ""
    new_text = old_text + f"\n\nРезультат: {labels[outcome]}"

    await query.edit_message_text(
        new_text,
        reply_markup=None
    )


# ---------- Прийом скрінів ----------

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

    status = await messages[0].reply_text(
        f"Отримала {len(messages)} скриншотів.\n"
        "Об'єдную мікровиміри та матрицю часу..."
    )

    try:
        images = await download_images(messages, context)

        if not images:
            await status.edit_text("Не вдалося отримати зображення.")
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

        keyboard = result_keyboard(prediction_id, direction)

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
            await process_album(album["messages"], context)

    except asyncio.CancelledError:
        pass

