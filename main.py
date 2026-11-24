import os
import base64
import logging
import asyncio
import re

from openai import OpenAI
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

logger = logging.getLogger("Nora")

ALBUMS = {}


SYSTEM_PROMPT = """
Ти Nora — аналітична помічниця користувача.
Твоя основа — авторська система «Матриця часу».

ГОЛОВНА ІДЕЯ:

Різні таймфрейми — це різні мікровиміри одного руху.
Матриця часу об'єднує їх в одне ціле.

Матриця часу — це порожній календар.
Вона сама не прогнозує напрямок і не створює сигнал.

Скриншоти заповнюють календар ринковою інформацією:
цінами, рівнями, розмірами тіл свічок, хвостами,
швидкістю, ритмом і стадіями мікроруху.

Твоє завдання — побачити цілісну картину:
що відбувається між мікровимірами,
куди може спрямуватися рух,
коли можливий поштовх
і скільки часу йому може знадобитися.

ПАКЕТ СКРИНШОТІВ:

Усі зображення одного пакета мають показувати
один валютний інструмент і різні таймфрейми.

Це не послідовні кадри в часі.
Не сприймай їх як рух ціни від першого зображення до останнього.

Визначай таймфрейм лише за видимими позначеннями.
Не вигадуй відсутні таймфрейми.
Якщо на скриншотах різні валютні пари —
не об'єднуй їх і видай WAIT.

АНАЛІЗ:

Оціни спільно:
- напрямок і структуру руху;
- рівні та реакцію ціни на них;
- розмір тіл свічок і хвостів;
- швидкість, ритм, прискорення та уповільнення;
- взаємодію різних таймфреймів;
- пружину — стиснення перед можливим рухом;
- шум — хаотичний рух без зрозумілої структури;
- затор — боротьбу без упевненого продовження.

Не приписуй ринку намірів.
Не стверджуй, що покупці чи продавці виснажені,
якщо це не підтверджено видимою структурою.

МАТРИЦЯ ЧАСУ:

Тобі передаються розрахунки Python:
час платформи, положення свічок,
час до їх закриття та майбутні часові стики.

Не перераховуй ці значення самостійно.
Використовуй їх як часову координатну сітку.

Часовий стик — це не автоматичний сигнал.
Він не означає обов'язкового розвороту чи продовження.
Його значення визначається лише разом із картиною графіка.

Час матриці — це час обробки пакета,
а не гарантований час створення скриншотів.
Якщо на зображенні видно інший час,
врахуй можливу різницю.

УВАГА ЩОДО H4:

Межі H4 у поточній матриці умовно прив'язані
до 00:00 часу платформи.
Якщо межі свічок на скриншоті не збігаються,
не використовуй розрахований час H4 як точний.

ВХІД І ЕКСПІРАЦІЯ:

Це два різні рішення.

1. ВХІД:
Визнач, чи варто входити зараз,
чи краще зачекати орієнтовно певну кількість хвилин
або конкретну умову на графіку.

2. ЕКСПІРАЦІЯ:
Визнач, скільки хвилин після фактичного входу
може розвиватися саме цей рух.

Допустима експірація — від 1 до 60 хвилин включно.

Не прив'язуй експірацію до таймфрейму.
Не використовуй постійні 3 хвилини.
Не обирай час лише через наближення часового стику.

Оцінюй тривалість за характером руху,
його стадією, швидкістю та взаємодією масштабів.

Якщо рух потребує часу для формування —
не поспішай із входом.
Якщо поштовх уже формується —
оцінюй, чи є підстави для входу зараз.

Не вигадуй точний час входу.
«Через N хв» — лише орієнтовна оцінка,
а не гарантія.

Якщо експірацію неможливо обґрунтувати,
видай WAIT і напиши «не визначена».

Не плутай, наприклад:
«чекати 2 хвилини до входу»
та
«експірація 7 хвилин після входу».

ВЕРДИКТ:

CALL — є достатні підстави очікувати рух угору.
PUT — є достатні підстави очікувати рух униз.
WAIT — ситуація нечітка, суперечлива
або немає обґрунтованого входу й експірації.

Не намагайся обов'язково видати сигнал.
Не обіцяй прибуток.

ФОРМАТ ВІДПОВІДІ:

Пара: ...
Картина: ... (коротко, суть усіх мікровимірів)
Напрямок: CALL / PUT / WAIT
Вхід: зараз / орієнтовно через N хв / чекати умову
Експірація: N хв після входу / не визначена
Причина: ... (одне коротке пояснення)

Не додавай довгих переліків і повторів.
Відповідай українською.
"""


def validate_analysis(result):
    """Перевіряє вердикт та експірацію."""

    lines = result.strip().splitlines()

    direction = None
    expiry = None
    expiry_line_index = None

    for index, line in enumerate(lines):
        if line.lower().startswith("напрямок:"):
            value = line.split(":", 1)[1].strip().upper()
            if value in ("CALL", "PUT", "WAIT"):
                direction = value

        if line.lower().startswith("експірація:"):
            expiry_line_index = index
            value = line.split(":", 1)[1].strip().lower()

            if "не визначена" in value:
                expiry = None
            else:
                match = re.search(r"\b(\d{1,2})\s*хв\b", value)
                if match:
                    minutes = int(match.group(1))
                    if 1 <= minutes <= 60:
                        expiry = minutes

    if direction not in ("CALL", "PUT", "WAIT"):
        direction = "WAIT"

    # Без обґрунтованої експірації не залишаємо CALL або PUT.
    if direction in ("CALL", "PUT") and expiry is None:
        direction = "WAIT"

    # WAIT не повинен містити торгову експірацію.
    if direction == "WAIT":
        expiry = None

    if expiry_line_index is not None:
        lines[expiry_line_index] = (
            f"Експірація: {expiry} хв після входу"
            if expiry is not None
            else "Експірація: не визначена"
        )

    direction_found = False

    for index, line in enumerate(lines):
        if line.lower().startswith("напрямок:"):
            lines[index] = f"Напрямок: {direction}"
            direction_found = True
            break

    if not direction_found:
        lines.insert(0, f"Напрямок: {direction}")

    if expiry_line_index is None:
        lines.append(
            f"Експірація: {expiry} хв після входу"
            if expiry is not None
            else "Експірація: не визначена"
        )

    if direction == "WAIT":
        for index, line in enumerate(lines):
            if line.lower().startswith("вхід:"):
                lines[index] = "Вхід: не входити"
                break

    return "\n".join(lines)


def analyze_images(images, matrix_text):
    content = [
        {
            "type": "input_text",
            "text": (
                "Проаналізуй усі зображення як одну структуру "
                "різних мікровимірів одного графіка.\n\n"
                "ЧАСОВА МАТРИЦЯ:\n"
                + matrix_text
                + "\n\n"
                "Дотримуйся короткого формату відповіді. "
                "Окремо визнач момент входу та експірацію "
                "від 1 до 60 хвилин після фактичного входу."
            )
        }
    ]

    for index, image_bytes in enumerate(images, start=1):
        encoded = base64.b64encode(image_bytes).decode("utf-8")

        content.append({
            "type": "input_text",
            "text": (
                f"Скриншот {index}: окремий часовий масштаб "
                "того самого графіка, не наступний кадр."
            )
        })

        content.append({
            "type": "input_image",
            "image_url": f"data:image/jpeg;base64,{encoded}"
        })

    response = client.responses.create(
        model=MODEL,
        instructions=SYSTEM_PROMPT,
        input=[
            {
                "role": "user",
                "content": content
            }
        ],
        max_output_tokens=350
    )

    return validate_analysis(response.output_text)


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

        logger.info(
            "Пакет отримано: %s зображень",
            len(images)
        )

        result = await asyncio.to_thread(
            analyze_images,
            images,
            matrix_text
        )

        final_text = (
            f"{result}\n\n"
            f"Час платформи: {analysis_time:%H:%M:%S}"
        )

        await status.edit_text(final_text)

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
        "Я об'єдную різні таймфрейми в одну картину "
        "за допомогою Матриці часу.\n\n"
        "Надсилай 3–5 скриншотів одного валютного інструмента "
        "одним альбомом.\n\n"
        "Я визначатиму напрямок, момент входу "
        "та можливу експірацію від 1 до 60 хвилин.\n\n"
        "Команда /time показує поточну матрицю."
    )


async def time_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    try:
        analysis_time = now_platform()
        matrix_text = full_matrix(analysis_time)

        await update.message.reply_text(matrix_text)

    except Exception:
        logger.exception("Помилка матриці часу")
        await update.message.reply_text(
            "Не вдалося побудувати матрицю часу."
        )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    keys = [
        key for key in ALBUMS
        if key[0] == chat_id
    ]

    for key in keys:
        album = ALBUMS.pop(key, None)

        if album and album.get("task"):
            album["task"].cancel()

    await update.message.reply_text(
        "Незавершені пакети очищені."
    )


def main():
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("time", time_command))
    app.add_handler(CommandHandler("reset", reset))

    app.add_handler(
        MessageHandler(filters.PHOTO, receive_photo)
    )

    logger.info("Nora запущена!")
    app.run_polling()


if __name__ == "__main__":
    main()
