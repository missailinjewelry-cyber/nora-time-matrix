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

    if direction in ("CALL", "
