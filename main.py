
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
        "Тепер я можу пам'ятати до п'яти останніх скриншотів "
        "у межах однієї сесії та порівнювати їх між собою.\n\n"
        "Надсилай скриншоти послідовно.\n"
        "Команда /reset починає новий аналіз."
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["chart_history"] = []

    await update.message.reply_text(
        "Пам'ять графіка очищена ❤️\n"
        "Можемо починати нову сесію."
    )


def analyze_images(history):
    content = [
        {
            "type": "input_text",
            "text": (
                "Ти Nora — помічниця з аналізу графіків.\n"
                "Перед тобою послідовність скриншотів одного графіка, "
                "надісланих у хронологічному порядку.\n\n"
                "ВАЖЛИВО:\n"
                "1. Не аналізуй останній скриншот ізольовано.\n"
                "2. Порівняй усі зображення між собою.\n"
                "3. Визнач, що змінилося від першого до останнього.\n"
                "4. Відстежуй напрямок, швидкість, тіла та хвости свічок.\n"
                "5. Відокремлюй видимі факти від припущень.\n"
                "6. Не вигадуй часові інтервали й ціни.\n\n"
                "Поки що це базовий аналіз послідовності, "
                "а не повна Матриця часу.\n\n"
                "Дай відповідь українською у форматі:\n"
                "• Загальна картина руху\n"
                "• Як змінювався рух між скриншотами\n"
                "• Поточний напрямок і швидкість\n"
                "• Що може відбутися далі\n"
                "• Попередній висновок: CALL / PUT / WAIT\n"
                "• Що потрібно побачити для підтвердження\n\n"
                "Не обіцяй прибутку. Якщо даних недостатньо, "
                "чесно скажи про це."
            )
        }
    ]

    for index, image_bytes in enumerate(history, start=1):
        encoded = base64.b64encode(image_bytes).decode("utf-8")

        content.append({
            "type": "input_text",
            "text": f"Скриншот номер {index} у хронологічній послідовності:"
        })

        content.append({
            "type": "input_image",
            "image_url": f"data:image/jpeg;base64,{encoded}"
        })

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=[{
            "role": "user",
            "content": content
        }]
    )

    return response.output_text


async def analyze_chart(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    message = update.message

    history = context.user_data.setdefault("chart_history", [])

    await message.reply_text(
        f"Отримала скриншот. Усього в пам'яті буде до "
        f"{MAX_IMAGES} зображень. Аналізую послідовність... 🔎"
    )

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
            f"Скриншотів у поточній пам'яті: {len(history)}\n\n"
            f"{result}"
        )

    except Exception:
        logger.exception("Помилка аналізу")
        await message.reply_text(
            "Не вдалося проаналізувати графік. "
            "Спробуй ще раз."
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
