
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

from matrix import (
    full_matrix,
    now_platform,
    H4_ANCHOR_HOUR,
)


# ---------- Налаштування ----------

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
    raise RuntimeError("Не задані ключі Telegram або OpenAI")

client = OpenAI(api_key=OPENAI_API_KEY)

MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
MAX_IMAGES = 5
ALBUM_WAIT = 2.5
MAX_OUTPUT_TOKENS = 950
DB_PATH = os.getenv("DB_PATH", "/data/nora.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

logger = logging.getLogger("Nora")
ALBUMS = {}


# ---------- Аналітична логіка Nora ----------

SYSTEM_PROMPT = f"""
Ти Nora — аналітична помічниця користувача.
Твоя основа — авторська система «Матриця часу».

ГОЛОВНИЙ ПРИНЦИП:
Різні таймфрейми — різні мікровиміри одного руху.
Матриця часу об'єднує ці масштаби в одну часову структуру.
Матриця — порожній календар. Вона сама не визначає напрямок.
Скриншоти заповнюють календар цінами, рівнями, тілами,
хвостами, швидкістю, ритмом і стадіями руху.

УСІ СКРИНШОТИ:
- Аналізуй їх як одну структуру одного інструмента.
- Це різні масштаби приблизно одного моменту, не послідовні кадри.
- Не вважай порядок надсилання послідовністю руху.
- Визначай таймфрейм тільки за видимими позначками.
- Не вигадуй відсутні таймфрейми, ціни або рівні.
- Якщо пари на скриншотах різні або це неможливо перевірити,
  обирай WAIT.

ПОСЛІДОВНІСТЬ АНАЛІЗУ:

1. СПОЧАТКУ ВИЗНАЧ СТРУКТУРУ:
- продовження руху;
- розворот;
- корекція;
- накопичення / стискання;
- затор;
- пружина;
- шум;
- змішана структура;
- недостатньо даних.

Не називай пружиною будь-який довгий хвіст.
Не називай розворотом будь-який відскок.
Шукай підтвердження в декількох масштабах.

2. ПРОЧИТАЙ ВЗАЄМОДІЮ МІКРОВИМІРІВ:
- де рух прискорюється або сповільнюється;
- де свічки стискаються або розширюються;
- як співвідносяться тіла та хвости;
- чи є реакція на рівень;
- чи підтримують нижчі масштаби старші;
- чи суперечать масштаби один одному;
- на якій стадії перебуває рух.

Не вигадуй причину руху, якщо її не видно.

3. ВИЗНАЧ СЦЕНАРІЙ:
Оціни, що ймовірніше:
- продовження;
- зміна напрямку;
- відкат;
- боковий рух;
- невизначеність.

Відокремлюй те, що вже видно, від того, що лише очікується.

4. ПРОГНОЗ ПОШТОВХУ:
Визнач:
- можливий напрямок;
- що має статися перед поштовхом;
- коли орієнтовно може з'явитися імпульс;
- яка умова скасує сценарій.

Не вигадуй точну хвилину, якщо її неможливо обґрунтувати.
Часовий стик матриці — лише часова подія.
Він не означає автоматичного розвороту чи продовження.

5. ВХІД:
Обери тільки один варіант:
- ЗАРАЗ;
- ЧЕРЕЗ N ХВИЛИН;
- ПІСЛЯ УМОВИ: конкретна видима умова;
- НЕ ВХОДИТИ.

Не пиши кілька альтернатив в одному полі.
Не вигадуй точний час входу.

6. ЕКСПІРАЦІЯ:
Визнач окремо від напрямку та входу.
Експірація — це тривалість після фактичного входу.
Діапазон — від 1 до 60 хвилин.

НЕ використовуй формулу:
кількість свічок одного таймфрейму × його тривалість.

НЕ прив'язуй експірацію автоматично до таймфрейму.
НЕ використовуй постійні 3 або 15 хвилин.
НЕ обирай експірацію лише через часовий стик.

Оціни:
- очікуваний шлях руху;
- стадію формування імпульсу;
- швидкість і ритм;
- опірні та підтримувальні рівні на шляху;
- взаємодію швидких і повільних масштабів;
- скільки часу може знадобитися саме цьому сценарію;
- чи не завершиться очікуваний рух раніше за експірацію.

Вибери одне обґрунтоване число від 1 до 60.
Якщо неможливо обґрунтувати тривалість — WAIT.
Не підганяй число під бажаний результат.

7. ПЕРЕВІРКА:
Якщо напрямок суперечливий, вхід не має підтвердження,
скриншотів недостатньо або експірація не обґрунтована —
обирай WAIT.

Не вигадуй відсоток точності.
Не обіцяй прибуток.
Не видавай припущення за факт.

H4 у матриці починається о {H4_ANCHOR_HOUR:02d}:00
за часом платформи. Реальне вирівнювання H4 у брокера
може відрізнятися — враховуй це обмеження.

ВІДПОВІДЬ КОРОТКА. Українською.
Кожне поле з нового рядка. Без довгих вступів.

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


# ---------- База даних ----------

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

        # Безпечне доповнення вже наявної бази.
        existing = {
            row[1]
            for row in db.execute(
                "PRAGMA table_info(predictions)"
            ).fetchall()
        }

        additions = {
            "raw_text": "TEXT",
            "platform_minute": "INTEGER",
        }

        for column, kind in additions.items():
            if column not in existing:
                db.execute(
                    f"ALTER TABLE predictions ADD COLUMN {column} {kind}"
                )

    logger.info("База Nora готова: %s", DB_PATH)


# ---------- Розбір прогнозу ----------

FIELD_PATTERN = (
    r"^[\s>*_#\-•]*{field}[\s*_]*:[\s*_]*(.*)$"
)


def get_field(text, field):
    match = re.search(
        FIELD_PATTERN.format(field=re.escape(field)),
        text,
        re.IGNORECASE | re.MULTILINE
    )
    return match.group(1).strip(" *_") if match else ""


def parse_direction(text):
    value = get_field(text, "Напрямок").upper()
    found = set(re.findall(r"\b(CALL|PUT|WAIT)\b", value))

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

    # Для CALL/PUT обов'язкова експірація 1–60 хв.
    if direction in ("CALL", "PUT") and expiry is None:
        direction = "WAIT"

    if direction == "WAIT":
        expiry = None

    replacements = {
        "Напрямок": (
            f"Напрямок: {direction}"
        ),
        "Експірація": (
            f"Експірація: {expiry} хв після входу"
            if expiry
            else "Експірація: не визначена"
        ),
    }

    lines = []
    found = set()

    for line in result.strip().splitlines():
        match = re.match(
            r"^[\s>*_#\-•]*(Напрямок|Експірація)\s*:",
            line,
            re.IGNORECASE
        )

        if match:
            key = match.group(1).capitalize()
            found.add(key)
            lines.append(replacements[key])
        else:
            lines.append(line)

    for key, value in replacements.items():
        if key not in found:
            lines.append(value)

    if direction == "WAIT":
        entry_line = get_field("\n".join(lines), "Вхід")
        if entry_line and entry_line.lower() not in (
            "не входити", "wait", "не визначено"
        ):
            lines = [
                "Вхід: не входити" if re.match(
                    r"^\s*Вхід\s*:", line, re.IGNORECASE
                ) else line
                for line in lines
            ]

    return "\n".join(lines)


# ---------- Аналіз зображень ----------

def image_mime(image_bytes):
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return "image/jpeg"


def analyze_images(images, matrix_text):
    content = [{
        "type": "input_text",
        "text": (
            "Проаналізуй цей пакет скриншотів за інструкцією Nora.\n"
            "Це один інструмент і приблизно один момент часу.\n"
            "Не сприймай скриншоти як послідовність кадрів.\n\n"
            "ЧАСОВА МАТРИЦЯ:\n"
            + matrix_text
        )
    }]

    for index, image_bytes in enumerate(images, start=1):
        encoded = base64.b64encode(image_bytes).decode("utf-8")

        content.append({
            "type": "input_text",
            "text": (
                f"Зображення {index}. "
                "Визнач масштаб за видимою позначкою."
            )
        })

        content.append({
            "type": "input_image",
            "image_url": (
                f"data:{image_mime(image_bytes)};base64,{encoded}"
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

    validated = validate_analysis(raw)

    if getattr(response, "status", None) == "incomplete":
        validated += (
            "\n\nУВАГА: відповідь моделі була обірвана."
        )

    return validated, raw


# ---------- Збереження прогнозу ----------

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
            """, (
                prediction_id,
                sqlite3.Binary(image_bytes)
            ))

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
        await query.answer("Цей прогноз уже зафіксований.")
        return

    await query.answer("Результат збережено.")

    labels = {
        "win": "ПЛЮС",
        "loss": "МІНУС",
        "skip": "ПРОПУЩЕНО",
    }

    old_text = query.message.text or ""
    await query.edit_message_text(
        old_text + f"\n\nРезультат: {labels[outcome]}",
        reply_markup=None
    )


# ---------- Статистика ----------

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
        lines.append(f"{direction} — {outcome}: {count}")

    await update.message.reply_text("\n".join(lines))


# ---------- Часова матриця ----------

async def time_command(update, context):
    now = now_platform()
    await update.message.reply_text(full_matrix(now))


async def start_command(update, context):
    await update.message.reply_text(
        "Я Nora — аналізую графік через Матрицю часу.\n\n"
        "Надішли 3–5 скриншотів однієї валютної пари "
        "одним пакетом.\n\n"
        "Команди:\n"
        "/time — часова матриця\n"
        "/stats — статистика прогнозів"
    )


# ---------- Прийом скриншотів ----------

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
        f"Отримала пакет. Скриншотів: {len(messages)}.\n"
        "Об'єдную мікровиміри та Матрицю часу..."
    )

    try:
        images = await download_images(messages, context)

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
            "Перевір журнал Railway та спробуй ще раз."
        )


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

    if not message:
        return

    if not (message.photo or message.document):
        return

    media_group_id = message.media_group_id

    # Окреме зображення — окремий пакет.
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


# ---------- Запуск ----------

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
