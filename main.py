
import os
import base64
import logging
import asyncio

from openai import OpenAI
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------- НАЛАШТУВАННЯ ----------

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
    raise RuntimeError("Не задані ключі Telegram або OpenAI")

client = OpenAI(api_key=OPENAI_API_KEY)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("Nora")

MAX_IMAGES = 5
ALBUM_WAIT = 2.0

# Тимчасове збирання фотографій одного альбому
ALBUMS = {}


# ---------- ІНСТРУКЦІЯ ДЛЯ ШІ ----------

SYSTEM_PROMPT = """
Ти Nora — аналітична помічниця користувачки.
Твоя мета — аналізувати графік як єдину мультитаймфреймову структуру.

КОРИСТУВАЧКА НАДСИЛАЄ 3-5 ОКРЕМИХ СКРИНШОТІВ
ОДНІЄЇ ВАЛЮТНОЇ ПАРИ.

ВАЖЛИВО:
1. Розглядай усі скриншоти разом, як різні масштаби
одного руху, а не як послідовні моменти часу.
2. Визначай таймфрейм кожного скриншота за підписом,
якщо він видимий. Не вигадуй його, якщо не видно.
3. Перевіряй, чи однакова валютна пара на всіх скриншотах.
Якщо пари різні — не давай торгового сигналу.
4. Аналізуй напрямок, швидкість, розмір тіл і хвостів,
ритм, структуру та стадію руху.
5. Визначай характер графіка: пружина, шум, затор
або змішаний стан.
6. Шукай взаємодію молодших і старших таймфреймів.
7. Не вигадуй ціни, таймфрейми та події, яких не видно.
8. Якщо даних недостатньо або графік неадекватний
для входу — чесно обирай WAIT.
9. Не сприймай закриття таймфрейму як автоматичний
сигнал для входу.
10. Не обіцяй результату угоди.

Відповідай українською, стисло, до 180 слів.

ФОРМАТ:

ПАРА:
ТАЙМФРЕЙМИ:
ЄДИНА СТРУКТУРА РУХУ:
ХАРАКТЕР ГРАФІКА:
НАПРЯМОК:
ЩО ПІДТВЕРДЖУЄ РУХ:
ЩО МОЖЕ ЙОГО ЗЛАМАТИ:
ВЕРДИКТ: CALL / PUT / WAIT
ЕКСПІРАЦІЯ: поки не визначена — не вигадуй.
"""


# ---------- АНАЛІЗ ЗОБРАЖЕНЬ ----------

def analyze_images(images):
    content = [
        {
            "type": "input_text",
            "text": (
                "Проаналізуй цей пакет скриншотів як одну "
                "мультитаймфреймову структуру руху. "
                "Усі зображення належать одному пакету. "
                "Порядок їх надсилання не означає, що це "
                "послідовні моменти руху."
            ),
        }
    ]

    for index, image_bytes in enumerate(images, start=1):
        encoded = base64.b64encode(image_bytes).decode("ascii")

        content.append({
            "type": "input_text",
            "text": f"Скриншот {index}:",
        })

        content.append({
            "type": "input_image",
            "image_url": f"data:image/jpeg;base64,{encoded}",
        })

    response = client.responses.create(
        model="gpt-4.1-mini",
        instructions=SYSTEM_PROMPT,
        input=[
            {
                "role": "user",
                "content": content,
            }
        ],
        max_output_tokens=500,
    )

    return response.output_text


# ---------- ЗАВАНТАЖЕННЯ ФОТО ----------

async def download_images(messages, context):
    images = []

    for message in messages:
        photo = message.photo[-1]
        telegram_file = await context.bot.get_file(photo.file_id)

        image_bytes = bytes(
            await telegram_file.download_as_bytearray()
        )

        images.append(image_bytes)

    return images


# ---------- СПІЛЬНИЙ АНАЛІЗ ПАКЕТА ----------

async def process_album(messages, context):
    messages.sort(key=lambda item: item.message_id)

    if not 1 <= len(messages) <= MAX_IMAGES:
        await messages[0].reply_text(
            "Я можу проаналізувати від 1 до 5 скриншотів "
            "за один раз. Надішли пакет ще раз."
        )
        return

    status = await messages[0].reply_text(
        f"Отримала {len(messages)} скриншотів. "
        "Об'єдную їх в одну структуру руху... 🔎"
    )

    try:
        images = await download_images(messages, context)

        result = await asyncio.to_thread(
            analyze_images,
            images,
        )

        await status.edit_text(
            f"ПАКЕТ: {len(images)} скриншотів\n\n{result}"
        )

    except Exception:
        logger.exception("Помилка аналізу пакета")

        await status.edit_text(
            "Не вдалося проаналізувати пакет. "
            "Спробуй надіслати скриншоти ще раз."
        )


# ---------- ЗБИРАННЯ АЛЬБОМУ TELEGRAM ----------

async def flush_album(key, context):
    try:
        await asyncio.sleep(ALBUM_WAIT)

        album = ALBUMS.pop(key, None)

        if not album:
            return

        messages = album["messages"]

        await process_album(messages, context)

    except asyncio.CancelledError:
        pass

    except Exception:
        logger.exception("Помилка збирання альбому")


async def receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message

    if not message or not message.photo:
        return

    # Одиночне фото
    if not message.media_group_id:
        await process_album([message], context)
        return

    # Ключ альбому: чат + ID групи
    key = (message.chat_id, message.media_group_id)

    if key not in ALBUMS:
        ALBUMS[key] = {
            "messages": [],
            "task": None,
        }

    album = ALBUMS[key]
    album["messages"].append(message)

    # Кожне нове фото продовжує очікування
    if album["task"] and not album["task"].done():
        album["task"].cancel()

    album["task"] = asyncio.create_task(
        flush_album(key, context)
    )


# ---------- КОМАНДИ ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привіт! Я Nora ❤️\n\n"
        "Надсилай мені 3–5 скриншотів однієї валютної пари "
        "одним пакетом.\n\n"
        "Я об'єднаю різні таймфрейми та проаналізую "
        "їх як одну структуру руху.\n\n"
        "Команда /reset очищує незавершені пакети."
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    keys = [
        key for key in ALBUMS
        if key[0] == chat_id
    ]

    for key in keys:
        album = ALBUMS.pop(key, None)

        if album and album["task"]:
            album["task"].cancel()

    await update.message.reply_text(
        "Незавершені пакети очищено ❤️\n"
        "Можемо починати новий аналіз."
    )


# ---------- ЗАПУСК ----------

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))

    app.add_handler(
        MessageHandler(filters.PHOTO, receive_photo)
    )

    logger.info("Nora запущена!")

    app.run_polling()


if __name__ == "__main__":
    main()
