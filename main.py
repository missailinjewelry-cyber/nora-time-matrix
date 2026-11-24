
import os
import base64
import logging
import asyncio
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

MODEL = "gpt-4.1-mini"
MAX_IMAGES = 5
ALBUM_WAIT = 3.0
DB_PATH = "/data/nora.db"

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

H4 у поточній матриці умовно прив'язана до 00:00
часу платформи. Якщо межі не збігаються,
не використовуй розрахунок H4 як точний.

Окремо визнач:
1. Момент входу: зараз, орієнтовно через N хв
   або чекати конкретну умову.
2. Експірацію: від 1 до 60 хвилин ПІСЛЯ фактичного входу.

Не прив'язуй експірацію до таймфрейму.
Не використовуй постійні 3 хвилини.
Не обирай експірацію лише через часовий стик.
Не вигадуй точний час входу.
Якщо немає обґрунтованого рішення — WAIT.

CALL — можливий рух угору.
PUT — можливий рух униз.
WAIT — немає достатніх підстав для входу.

Відповідай коротко, українською.
Не обіцяй прибуток.

ФОРМАТ:

Пара: ...
Картина: ...
Напрямок: CALL / PUT / WAIT
Вхід: ...
Експірація: N хв після входу / не визначена
Причина: ...
"""


def db_connect():
    return sqlite3.connect(DB_PATH)


def init_db():
    os.makedirs("/data", exist_ok=True)

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

    logger.info("База даних Nora готова: %s", DB_PATH)


def get_field(text, field):
    match = re.search(
        rf"^{re.escape(field)}:\s*(.+)$",
        text,
        re.IGNORECASE | re.MULTILINE
    )
    return match.group(1).strip() if match else ""


def validate_analysis(result):
    lines = result.strip().splitlines()

    direction = get_field(result, "Напрямок").upper()
    if direction not in ("CALL", "PUT", "WAIT"):
        direction = "WAIT"

    expiry_text = get_field(result, "Експірація").lower()
    match = re.search(r"(\d{1,2})\s*хв", expiry_text)

    expiry = None
    if match:
        number = int(match.group(1))
        if 1 <= number <= 60:
            expiry = number

    if direction in ("CALL", "PUT") and expiry is None:
        direction = "WAIT"

    output = []

    for line in lines:
        if line.lower().startswith("напрямок:"):
            output.append(f"Напрямок: {direction}")
        elif line.lower().startswith("експірація:"):
            if expiry:
                output.append(
                    f"Експірація: {expiry} хв після входу"
                )
            else:
                output.append("Експірація: не визначена")
        elif line.lower().startswith("вхід:") and direction == "WAIT":
            output.append("Вхід: не входити")
        else:
            output.append(line)

    if not any(x.lower().startswith("напрямок:") for x in output):
        output.append(f"Напрямок: {direction}")

    if not any(x.lower().startswith("експірація:") for x in output):
        output.append(
            f"Експірація: {expiry} хв після входу"
            if expiry else "Експірація: не визначена"
        )

    if direction == "WAIT" and not any(
        x.lower().startswith("вхід:") for x in output
    ):
        output.append("Вхід: не входити")

    return "\n".join(output)


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
            "image_url": f"data:image/jpeg;base64,{encoded}"
        })

    response = client.responses.create(
        model=MODEL,
        instructions=SYSTEM_PROMPT,
        input=[{"role": "user", "content": content}],
        max_output_tokens=250
    )

    return validate_analysis(response.output_text)


def save_prediction(analysis, images, platform_time):
    pair = get_field(analysis, "Пара")
    direction = get_field(analysis, "Напрямок").upper()
    entry = get_field(analysis, "Вхід")
    expiry_text = get_field(analysis, "Експірація")

    match = re.search(r"(\d{1,2})\s*хв", expiry_text)
    expiry = int(match.group(1)) if match else None

    with db_connect() as db:
        cursor = db.execute("""
            INSERT INTO predictions (
                created_at, platform_time, pair, direction,
                entry, expiry_minutes, analysis,
                screenshot_count, outcome
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
        """, (
            platform_time.isoformat(),
            platform_time.strftime("%Y-%m-%d %H:%M:%S"),
            pair,
            direction,
            entry,
            expiry,
            analysis,
            len(images)
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
    await query.answer()

    try:
        _, prediction_id, outcome = query.data.split(":")
        prediction_id = int(prediction_id)
    except (ValueError, AttributeError):
        await query.answer("Не вдалося прочитати результат.")
        return

    if outcome not in ("win", "loss", "skip"):
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


async def download_images(messages, context):
    images = []

    for message in messages:
        if not message.photo:
            continue

        photo = message.photo[-1]
        telegram_file = await context.bot.get_file(photo.file_id)

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

        result = await asyncio.to_thread(
            analyze_images,
            images,
            matrix_text
        )

        prediction_id = await asyncio.to_thread(
            save_prediction,
            result,
            images,
            analysis_time
        )

        direction = get_field(result, "Напрямок").upper()

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

    except Exception:
        logger.exception("Помилка обробки альбому")


async def receive_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    message = update.message

    if not message:
        return

    media_group_id = message.media_group_id

    if not media_group_id:
        await process_album([message], context)
        return

    key = (message.chat_id, media_group_id)

    if key not in ALBUMS:
        ALBUMS[key] = {
            "messages": [],
            "task": None
        }

    album = ALBUMS[key]
    album["messages"].append(message)

    old_task = album.get("task")

    if old_task and not old_task.done():
        old_task.cancel()

    album["task"] = asyncio.create_task(
        flush_album(key, context)
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привіт! Я Nora.\n\n"
        "Надсилай 3–5 скриншотів одного інструмента "
        "одним альбомом.\n\n"
        "Я аналізую Матрицю часу, напрямок, момент входу "
        "та експірацію від 1 до 60 хвилин.\n\n"
        "/time — часова матриця\n"
        "/stats — статистика прогнозів"
    )


async def time_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    try:
        analysis_time = now_platform()
        await update.message.reply_text(
            full_matrix(analysis_time)
        )
    except Exception:
        logger.exception("Помилка матриці часу")
        await update.message.reply_text(
            "Не вдалося побудувати матрицю часу."
        )


async def stats_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    with db_connect() as db:
        total = db.execute(
            "SELECT COUNT(*) FROM predictions"
        ).fetchone()[0]

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

    counts = {
        "win": 0,
        "loss": 0,
        "skip": 0,
        "pending": 0
    }

    for outcome, count in rows:
        counts[outcome] = count

    completed = counts["win"] + counts["loss"]
    accuracy = (
        counts["win"] / completed * 100
        if completed else 0
    )

    text = (
        "СТАТИСТИКА NORA\n\n"
        f"Усього прогнозів: {total}\n"
        f"Плюсів: {counts['win']}\n"
        f"Мінусів: {counts['loss']}\n"
        f"Пропущено: {counts['skip']}\n"
        f"Очікують результату: {counts['pending']}\n\n"
        f"Точність серед угод із результатом: "
        f"{accuracy:.1f}%\n"
        f"Враховано угод: {completed}\n"
    )

    if directions:
        text += "\nЗА НАПРЯМКАМИ:\n"

        for direction, outcome, count in directions:
            text += f"{direction} — {outcome}: {count}\n"

    await update.message.reply_text(text)


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    keys = [key for key in ALBUMS if key[0] == chat_id]

    for key in keys:
        album = ALBUMS.pop(key, None)
        if album and album.get("task"):
            album["task"].cancel()

    await update.message.reply_text(
        "Незавершені пакети очищені."
    )


def main():
    init_db()

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("time", time_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("reset", reset))

    app.add_handler(
        CallbackQueryHandler(
            handle_result,
            pattern=r"^result:"
        )
    )

    app.add_handler(
        MessageHandler(filters.PHOTO, receive_photo)
    )

    logger.info("Nora запущена!")
    app.run_polling()


if __name__ == "__main__":
    main()
