
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

from matrix import full_matrix, now_platform


# ==================================================
# НАЛАШТУВАННЯ
# ==================================================

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

# Тимчасове зберігання альбомів до завершення їх отримання
ALBUMS = {}


# ==================================================
# ОСНОВНА ІНСТРУКЦІЯ ДЛЯ ШІ
# ==================================================

SYSTEM_PROMPT = """
Ти Nora — аналітична помічниця користувача.
Твоя основа — авторська система «Матриця часу».

ГОЛОВНА ІДЕЯ:

Різні таймфрейми — це різні мікровиміри одного руху.
Матриця часу об'єднує їх в одну часову структуру.

Матриця часу сама по собі — порожній календар.
Вона не прогнозує напрямок і не визначає сигнали.

Пакет скриншотів заповнює цей календар інформацією:
цінами, рівнями, розмірами тіл свічок, хвостами,
швидкістю, характером і стадіями мікроруху.

Твоє завдання — миттєво зіставити цю інформацію
та побачити цілісну картину.

ПРАВИЛО ПАКЕТА:

Усі скриншоти одного пакета показують один валютний інструмент,
але різні таймфрейми.

Це НЕ послідовні кадри в часі.
Не вважай другий скриншот продовженням першого.
Не роби висновків про те, як ціна змінювалася
між моментами створення цих скриншотів.

Спочатку визнач таймфрейм кожного зображення.
Використовуй видимі позначення платформи.
Якщо таймфрейм неможливо визначити — так і скажи.

Якщо бачиш різні валютні пари — не об'єднуй їх.
Повідом про це і не давай торгового сигналу.

АНАЛІЗ ГРАФІКА:

Досліджуй:
- напрямок і структуру руху;
- рівні та відстані між ними;
- розміри тіл свічок;
- довжину хвостів та їх співвідношення з тілами;
- швидкість і ритм руху;
- прискорення та уповільнення;
- взаємодію різних таймфреймів;
- пружину — стиснення руху перед можливим імпульсом;
- шум — хаотичний рух без зрозумілої структури;
- затор — ситуацію, коли рух не може впевнено продовжитися.

Не вигадуй ціни, рівні, напрямки чи події,
яких не видно на скриншотах.

МАТРИЦЯ ЧАСУ:

Тобі передаються точні розрахунки Python:
поточний час, положення свічок, час до їх закриття
та майбутні часові стики.

Не перераховуй ці значення самостійно.
Використовуй їх як часову координатну сітку.

Стик таймфреймів — це лише часова подія.
Він сам по собі не означає розворот, продовження
або обов'язковий поштовх.

Напрямок і характер можливого поштовху визначай
тільки за спільним аналізом графіка та часу.

ЧАС ЕКСПІРАЦІЇ:

Не плутай таймфрейм із часом експірації.
Не вигадуй експірацію лише тому, що наближається стик.

Якщо графік і часова структура не дають
достатніх підстав визначити момент поштовху
та тривалість руху — вкажи, що експірація не визначена.

ВЕРДИКТ:

CALL — якщо є достатні підстави очікувати рух угору.
PUT — якщо є достатні підстави очікувати рух униз.
WAIT — якщо ситуація нечітка, суперечлива,
шумна або немає підтвердження.

Не намагайся обов'язково видати CALL або PUT.
Чесний WAIT кращий за вигаданий сигнал.

ВІДПОВІДАЙ УКРАЇНСЬКОЮ.

ФОРМАТ:

1. Валютна пара.
2. Визначені таймфрейми.
3. Загальна картина мікрорухів.
4. Що відбувається між мікровимірами.
5. Характер графіка: пружина, шум, затор або інше.
6. Можливий напрямок поштовху та його обґрунтування.
7. Часова ситуація за матрицею.
8. Експірація: обґрунтований час або «не визначена».
9. Висновок: CALL / PUT / WAIT.

Не видавай припущення за факт.
Не обіцяй прибуток.
Пиши змістовно, але без зайвих повторів.
"""


# ==================================================
# АНАЛІЗ ПАКЕТА СКРИНШОТІВ
# ==================================================

def analyze_images(images, matrix_text):

    content = [
        {
            "type": "input_text",
            "text": (
                "Проаналізуй усі скриншоти як єдину "
                "багатовимірну структуру одного графіка.\n\n"
                "ЧАСОВА МАТРИЦЯ, РОЗРАХОВАНА PYTHON:\n\n"
                + matrix_text
                + "\n\n"
                "ВАЖЛИВО: час матриці — це час обробки пакета, "
                "а не гарантований точний час створення кожного скриншота. "
                "Не приписуй зображенням точний момент зйомки, "
                "якщо його не видно."
            )
        }
    ]

    for index, image_bytes in enumerate(images, start=1):

        encoded = base64.b64encode(image_bytes).decode("utf-8")

        content.append({
            "type": "input_text",
            "text": (
                f"Зображення {index}. "
                "Це окремий часовий масштаб того самого графіка, "
                "а не наступний кадр у хронології."
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
        max_output_tokens=700
    )

    return response.output_text


# ==================================================
# ЗАВАНТАЖЕННЯ ЗОБРАЖЕНЬ
# ==================================================

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


# ==================================================
# ОБРОБКА АЛЬБОМУ
# ==================================================

async def process_album(messages, context):

    if not messages:
        return

    messages.sort(key=lambda m: m.message_id)

    if len(messages) > MAX_IMAGES:
        messages = messages[:MAX_IMAGES]

    status = await messages[0].reply_text(
        f"Отримала {len(messages)} скриншотів.\n"
        "Об'єдную мікровиміри з матрицею часу... 🔎"
    )

    try:

        images = await download_images(messages, context)

        if not images:
            await status.edit_text(
                "Не вдалося отримати зображення."
            )
            return

        matrix_text = full_matrix()

        logger.info(
            "Пакет отримано: %s зображень",
            len(images)
        )

        result = await asyncio.to_thread(
            analyze_images,
            images,
            matrix_text
        )

        await status.edit_text(
            f"Скриншотів у пакеті: {len(images)}\n\n"
            f"{result}"
        )

    except Exception:

        logger.exception("Помилка аналізу пакета")

        await status.edit_text(
            "Не вдалося завершити аналіз.\n"
            "Спробуй надіслати пакет ще раз."
        )


# ==================================================
# ЗАВЕРШЕННЯ ОТРИМАННЯ АЛЬБОМУ
# ==================================================

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


# ==================================================
# ОТРИМАННЯ ФОТОГРАФІЙ
# ==================================================

async def receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):

    message = update.message

    if not message:
        return

    media_group_id = message.media_group_id

    # Якщо надіслано одне фото без альбому
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

    # Даємо Telegram час передати всі частини альбому
    old_task = album.get("task")

    if old_task and not old_task.done():
        old_task.cancel()

    album["task"] = asyncio.create_task(
        flush_album(key, context)
    )


# ==================================================
# КОМАНДИ
# ==================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "Привіт! Я Nora ❤️\n\n"
        "Я аналізую скриншоти одного валютного інструмента "
        "як різні мікровиміри одного руху.\n\n"
        "Матриця часу об'єднує їх у спільну часову структуру.\n\n"
        "Надсилай 3–5 скриншотів різних таймфреймів "
        "одним альбомом.\n\n"
        "Команда /time покаже поточну матрицю часу."
    )


async def time_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    try:

        matrix_text = full_matrix()

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
        "Незавершені пакети очищені.\n"
        "Можемо починати новий аналіз."
    )


# ==================================================
# ЗАПУСК NORA
# ==================================================

def main():

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        CommandHandler("time", time_command)
    )

    app.add_handler(
        CommandHandler("reset", reset)
    )

    app.add_handler(
        MessageHandler(filters.PHOTO, receive_photo)
    )

    logger.info("Nora запущена!")

    app.run_polling()


if __name__ == "__main__":
    main()
