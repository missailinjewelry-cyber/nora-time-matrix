
import os
import base64
import logging
import asyncio

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from openai import OpenAI

# Ключі доступу з Railway
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
    raise RuntimeError("Не задані ключі Telegram або OpenAI")

client = OpenAI(api_key=OPENAI_API_KEY)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MAX_IMAGES = 5


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["chart_history"] = []

    await update.message.reply_text(
        "Привіт! Я Nora ❤️\n\n"
        "Я пам'ятаю до п'яти останніх скриншотів "
        "і порівнюю їх як єдиний рух.\n\n"
        "Надсилай скриншоти послідовно.\n"
        "Команда /reset очищує пам'ять графіка."
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["chart_history"] = []

    await update.message.reply_text(
        "Пам'ять графіка очищена ❤️\n"
        "Можемо починати новий аналіз."
    )


def analyze_images(history):
    content = [
        {
            "type": "input_text",
            "text": (
                "Ти Nora — аналітична помічниця користувача.\n"
                "Перед тобою послідовність скриншотів одного графіка "
                "у хронологічному порядку.\n\n"

                "ГОЛОВНЕ:\n"
                "Не аналізуй кожен скриншот окремо. "
                "Розглядай усі зображення як розвиток одного руху. "
                "Порівнюй попередній стан із наступним. "
                "Відстежуй напрямок, швидкість, тіла та хвости свічок, "
                "зміни сили руху та можливі переходи між стадіями.\n\n"

                "Не вигадуй ціни, часові інтервали чи події, "
                "яких не видно. Якщо скриншоти не дозволяють "
                "надійно порівняти рух, скажи про це.\n\n"

                "ВІДПОВІДАЙ УКРАЇНСЬКОЮ.\n"
                "Максимум 120 слів. Без довгих вступів "
                "і повторів очевидного.\n\n"

                "ФОРМАТ ВІДПОВІДІ:\n"
                "1. Загальний рух — коротко.\n"
                "2. Що змінилося між скриншотами.\n"
                "3. Поточна стадія руху.\n"
                "4. Що спостерігати далі.\n"
                "5. Висновок: CALL / PUT / WAIT та причина.\n\n"

                "Це поки базовий аналіз послідовності, "
                "а не повна Матриця часу. "
                "Не подавай сигнал як гарантію результату."
            )
        }
    ]

    for index, image_bytes in enumerate(history, start=1):
        encoded = base64.b64encode(image_bytes).decode("utf-8")

        content.append({
            "type": "input_text",
            "text": f"Скриншот {index} у хронологічному порядку:"
        })

        content.append({
            "type": "input_image",
            "image_url": f"data:image/jpeg;base64,{encoded}"
        })

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=[
            {
                "role": "user",
                "content": content
            }
        ],
        max_output_tokens=300
    )

    return response.output_text


async def analyze_chart(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    message = update.message

    history = context.user_data.setdefault("chart_history", [])

    await message.reply_text("Отримала скриншот. Аналізую рух... 🔎")

    try:
        photo = message.photo[-1]

        telegram_file = await context.bot.get_file(photo.file_id)

        image_bytes = bytes(
            await telegram_file.download_as_bytearray()
        )

        history.append(image_bytes)

        if len(history) > MAX_IMAGES:
            del history[0]

        result = await asyncio.to_thread(
            analyze_images,
            history.copy()
        )

        await message.reply_text(
            f"У пам'яті: {len(history)} скриншотів.\n\n{result}"
        )

    except Exception:
        logger.exception("Помилка аналізу графіка")

        await message.reply_text(
            "Не вдалося проаналізувати графік. "
            "Спробуй надіслати скриншот ще раз."
        )


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))

    app.add_handler(
        MessageHandler(filters.PHOTO, analyze_chart)
    )

    print("Nora запущена!")

    app.run_polling()


if __name__ == "__main__":
    main()
