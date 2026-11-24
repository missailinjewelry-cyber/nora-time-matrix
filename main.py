
import os
import base64
import logging

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from openai import OpenAI

# Налаштування
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not TELEGRAM_TOKEN:
    raise RuntimeError("Не знайдено TELEGRAM_BOT_TOKEN")

if not OPENAI_API_KEY:
    raise RuntimeError("Не знайдено OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привіт! Я Nora ❤️\n\n"
        "Надішли мені скриншот графіка, і я зроблю його первинний аналіз.\n"
        "Ми поступово навчимо мене твоєї Матриці часу."
    )


async def analyze_chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message

    await message.reply_text("Отримала графік. Розглядаю його... 🔎")

    try:
        photo = message.photo[-1]
        telegram_file = await context.bot.get_file(photo.file_id)
        image_bytes = await telegram_file.download_as_bytearray()

        image_base64 = base64.b64encode(image_bytes).decode("utf-8")

        response = client.responses.create(
            model="gpt-4.1-mini",
            input=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Ти Nora — помічниця з аналізу біржових графіків. "
                                "Проаналізуй лише те, що справді видно на скриншоті. "
                                "Опиши напрямок руху, швидкість, свічки, їхні тіла "
                                "та тіні, можливі зміни руху й невизначеність. "
                                "Не вигадуй часові інтервали, ціни чи дані, яких не видно. "
                                "Наприкінці дай обережний висновок: CALL, PUT або WAIT, "
                                "поясни причину та рівень упевненості. "
                                "Не подавай висновок як гарантію прибутку. "
                                "Поки що це первинний аналіз, не повний аналіз Матриці часу."
                            )
                        },
                        {
                            "type": "input_image",
                            "image_url": f"data:image/jpeg;base64,{image_base64}"
                        }
                    ]
                }
            ]
        )

        result = response.output_text or "Не вдалося отримати текст аналізу."
        await message.reply_text(result)

    except Exception:
        logger.exception("Помилка під час аналізу графіка")
        await message.reply_text(
            "Виникла помилка під час аналізу. Спробуй ще раз трохи пізніше."
        )


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, analyze_chart))

    print("Nora запущена!")
    app.run_polling()


if __name__ == "__main__":
    main()
