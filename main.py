"""Nora: Telegram-бот з аналізом OTC-графіків, матрицею часу і гнучкою експірацією.

Рішення приймаються за критеріями Mia (prompts.py). Nora сама вирішує після кожного
кроку: дати ФІНАЛ, попросити ще один скрін ЗАРАЗ чи попросити скрін ПІЗНІШЕ.
Файли: main.py (Telegram і сесії), memory.py (база, розбір відповіді, статистика),
prompts.py (інструкція), ai.py (звернення до ШІ), matrix.py (матриця часу).
"""
import asyncio
import logging
import os
import re
from datetime import datetime, timedelta

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

import ai
import matrix
import memory
from prompts import SYSTEM_PROMPT, limits_text

# ============================================================
# 1. НАЛАШТУВАННЯ
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_TOKEN or not ai.API_KEY:
    raise RuntimeError("Не задані TELEGRAM_BOT_TOKEN або OPENAI_API_KEY")


def _int_env(name, default):
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _float_env(name, default):
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


MAX_IMAGES = _int_env("MAX_IMAGES", 5)  # скрінів в одній пачці
MAX_MODEL_IMAGES = _int_env("MAX_MODEL_IMAGES", 8)  # скільки останніх скрінів бачить модель
ALBUM_WAIT = _float_env("ALBUM_WAIT", 3.0)  # чекаємо решту альбому
SOLO_WAIT = _float_env("SOLO_WAIT", 1.5)  # чекаємо, чи не надішлють ще скрін поспіль
MAX_NOW_REQUESTS = _int_env("MAX_NOW_REQUESTS", 2)  # скільки разів Nora може попросити скрін(и) ЗАРАЗ
MAX_LATER_REQUESTS = _int_env("MAX_LATER_REQUESTS", 2)  # скільки разів може попросити ПІЗНІШЕ
MAX_LATER_MINUTES = 60
REMINDER_DELAY_SECONDS = _int_env("REMINDER_DELAY_SECONDS", 120)
CANCEL_AFTER_REMINDER_SECONDS = _int_env("CANCEL_AFTER_REMINDER_SECONDS", 120)
EARLY_TOLERANCE_SECONDS = _int_env("EARLY_TOLERANCE_SECONDS", 20)
SCHEDULER_INTERVAL_SECONDS = _int_env("SCHEDULER_INTERVAL_SECONDS", 10)
CLOSE_PADDING_SECONDS = 3  # скрін просимо за кілька секунд після закриття свічки
REMINDER_MAX_LATE_SECONDS = 600  # нагадування, що запізнилось більш ніж на 10 хв, не надсилаємо
SNAP_SECONDS = 60  # якщо запитаний час ближче за стільки до закриття свічки, зсуваємо на закриття


def _ids(raw):
    return {int(x) for x in re.findall(r"-?\d+", raw or "")}


ALLOWED_USER_IDS = _ids(os.getenv("ALLOWED_USER_IDS", ""))  # порожньо = відкрито для всіх

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# У URL цих бібліотек є токен Telegram. Не показуємо їх в логах.
for noisy in ("httpx", "httpcore"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
logger = logging.getLogger("Nora")

BATCHES = {}
SESSION_LOCKS = {}
CHAT_LOCKS = {}
BACKGROUND = set()


def spawn(coro):
    """Запускає фонове завдання й тримає на нього посилання, щоб воно не зникло."""
    task = asyncio.create_task(coro)
    BACKGROUND.add(task)
    task.add_done_callback(BACKGROUND.discard)
    return task


def allowed(update):
    if not ALLOWED_USER_IDS:
        return True
    user = update.effective_user
    return bool(user and user.id in ALLOWED_USER_IDS)


async def send_long(bot, chat_id, text, reply_markup=None):
    """Telegram приймає до 4096 символів: довге ріжемо по рядках."""
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > 3900:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:3900])
            line = line[3900:]
        if len(current) + len(line) + 1 > 3900:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    for i, chunk in enumerate(chunks):
        last = i == len(chunks) - 1
        await bot.send_message(chat_id, chunk, reply_markup=reply_markup if last else None)


def human_left(seconds):
    seconds = max(0, int(seconds))
    minutes, sec = divmod(seconds, 60)
    return f"{minutes} хв {sec:02d} с" if minutes else f"{sec} с"


# ============================================================
# 2. ПОВІДОМЛЕННЯ ДЛЯ ШІ
# ============================================================

def build_parts(session, image_rows, now, matrix_text, extra_note=None):
    now_left = max(0, MAX_NOW_REQUESTS - (session["now_used"] or 0))
    later_left = max(0, MAX_LATER_REQUESTS - (session["later_used"] or 0))
    rows = image_rows[-MAX_MODEL_IMAGES:]
    rounds_total = len({r[0] for r in image_rows})

    header = [
        "МАТРИЦЯ ЧАСУ (рахував код, довіряй цим числам):",
        matrix_text,
        "",
        limits_text(now_left, later_left),
        "",
        f"ЩОДЕННИК (останні {memory.DIARY_HOURS} год, лише для послідовності):",
        memory.diary_text(now),
    ]
    notes = (session.get("notes") or "").strip()
    if notes:
        header += ["", "ТВОЇ ПОПЕРЕДНІ ВИСНОВКИ В ЦІЙ СЕСІЇ (скріни нижче вже враховані в них):", notes]
    header += [
        "",
        f"Нижче скріни цієї сесії, раундів: {rounds_total}. Скріни одного раунду це різні таймфрейми "
        "приблизно одного моменту. Пізніший раунд це нове спостереження в часі.",
    ]
    if len(rows) < len(image_rows):
        header.append(f"(Показано останні {len(rows)} із {len(image_rows)} скрінів.)")

    parts = [{"text": "\n".join(header)}]
    previous_round = None
    for index, (round_no, captured_at, _matrix, image_bytes) in enumerate(rows, start=1):
        if round_no != previous_round:
            try:
                moment = datetime.fromisoformat(captured_at)
                ago = human_left((now - moment).total_seconds())
                label = f"{moment:%H:%M:%S}, {ago} тому"
            except ValueError:
                label = captured_at
            parts.append({"text": f"РАУНД {round_no}: скріни отримано о {label} (час платформи)."})
            previous_round = round_no
        parts.append({"text": f"Скрін {index}. Визнач таймфрейм за позначкою."})
        parts.append({"image": image_bytes})

    tail = "Дай відповідь за форматом розділу 8. Перший рядок «ДІЯ:»."
    if extra_note:
        tail = extra_note + "\n" + tail
    parts.append({"text": tail})
    return parts


STUB_FINAL = (
    "ДІЯ: ФІНАЛ\n"
    "ПАРА: невідома\n"
    "ХАРАКТЕР ГРАФІКА: не визначено\n"
    "ВЕРДИКТ: НЕ ВХІД, ЧЕКАЄМО\n"
    "ТОЧКА ВХОДУ: немає\n"
    "ЕКСПІРАЦІЯ: не визначена\n"
    "РИЗИК: відповідь ШІ не за форматом, тому рішення безпечне: не входимо\n"
    "ПІДСУМОК: не вхід (відповідь ШІ не за форматом)"
)


# ============================================================
# 3. РІШЕННЯ ПО СЕСІЇ
# ============================================================

def snap_to_close(tf, due, now):
    """Якщо запитаний час лежить поблизу закриття свічки tf, зсуваємо його на цю мить.

    Скрін, зроблений одразу після закриття, показує готову свічку, а не напівсформовану.
    """
    step = matrix.tf_minutes(tf)
    following = matrix.next_close(tf, due)
    if step is None or following is None:
        return due
    previous = following - timedelta(minutes=step)
    nearest = min((previous, following), key=lambda t: abs((t - due).total_seconds()))
    if abs((nearest - due).total_seconds()) <= SNAP_SECONDS:
        return max(nearest + timedelta(seconds=CLOSE_PADDING_SECONDS), now)
    return due


def compute_due(request, now):
    """Коли просимо скрін ПІЗНІШЕ. Модель може дати «через N хв» чи «о HH:MM:SS».

    Якщо нічого зрозумілого, просимо одразу після найближчого закриття свічки потрібного таймфрейму.
    Повертає (момент, пояснення).
    """
    tf = request["tf"]
    if request.get("due") is not None:  # час уже порахував код (фінал «чекаємо N хв»)
        return request["due"], None
    if request["delay_min"] is not None and 1 <= request["delay_min"] <= MAX_LATER_MINUTES:
        return snap_to_close(tf, now + timedelta(minutes=request["delay_min"]), now), None
    if request["clock"] is not None:
        h, m, s = request["clock"]
        target = now.replace(hour=h, minute=m, second=s, microsecond=0)
        if now - timedelta(seconds=5) <= target <= now + timedelta(minutes=MAX_LATER_MINUTES):
            return snap_to_close(tf, target, now), None
    closing = matrix.next_close(tf, now)
    if closing is None:
        return now + timedelta(minutes=1), "час не розібрано, ставлю через 1 хв"
    due = closing + timedelta(seconds=CLOSE_PADDING_SECONDS)
    return due, f"час не розібрано, ставлю після закриття найближчої свічки {tf}"


async def apply_request(session, request, raw, round_no, bot):
    now = matrix.now_platform()
    kind, tf = request["kind"], request["tf"]
    note = None
    if kind == "now":
        due = now
    else:
        due, note = compute_due(request, now)

    reading = memory.get_block(raw, "ЧИТАННЯ")
    if not reading:  # запит, утворений із фіналу, не має рядка ЧИТАННЯ
        reading = " ".join(x for x in (memory.get_field(raw, "АКТИВНИЙ ТАЙМФРЕЙМ"),
                                       memory.get_field(raw, "МАТРИЦЯ")) if x)
    character = memory.parse_character(raw)
    pair = memory.parse_pair(raw)
    summary = (
        f"Раунд {round_no}: пара {pair or 'невідома'}, характер {character or 'не визначено'}. "
        f"Читання: {reading or '—'}. Попросила скрін {tf} "
        f"({'зараз' if kind == 'now' else 'пізніше'}): {request['reason']}"
    )
    memory.append_notes(session["id"], summary[:900])
    memory.set_session_request(
        session["id"], round_no + 1, kind, tf, request["reason"], due, now,
        REMINDER_DELAY_SECONDS, CANCEL_AFTER_REMINDER_SECONDS,
    )

    seen = [f"Поки бачу: {character.lower()}." if character else ""]
    if reading:
        seen.append(reading[:350])
    seen_text = " ".join(x for x in seen if x)

    if kind == "now":
        many = "," in tf
        text = (
            (f"🔎 Потрібні ще скріни ЗАРАЗ: {tf}.\n" if many else f"🔎 Потрібен ще один скрін ЗАРАЗ: {tf}.\n")
            + f"Навіщо: {request['reason']}\n"
            + (f"\n{seen_text}\n" if seen_text else "")
            + ("\nНадішли їх разом (альбомом), я додам до цієї ж сесії." if many
               else "\nНадішли його одним повідомленням, я додам до цієї ж сесії.")
        )
    else:
        text = (
            f"⏳ Потрібен скрін {tf} о {due:%H:%M:%S} "
            f"(через {human_left((due - now).total_seconds())}).\n"
            f"Навіщо: {request['reason']}\n"
            + (f"\n{seen_text}\n" if seen_text else "")
            + ("\nЦе ще не фінал: я продовжу цей самий аналіз, коли надішлеш скрін. "
               "Можеш додати й інші таймфрейми, якщо вважаєш потрібним.\n" if request.get("converted") else "")
            + "\nРаніше не надсилай: потрібна вже сформована свічка. Я нагадаю, коли час настане."
        )
        if note:
            text += f"\n({note})"
    await send_long(bot, session["chat_id"], text, reply_markup=stop_keyboard("⏹ Скасувати цей аналіз"))


def stop_keyboard(label="⏹ Не стежити"):
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data="stop:chat")]])


def result_keyboard(prediction_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Зайшла (+)", callback_data=f"result:{prediction_id}:win"),
            InlineKeyboardButton("Не зайшла (−)", callback_data=f"result:{prediction_id}:loss"),
        ],
        [InlineKeyboardButton("Пропустила", callback_data=f"result:{prediction_id}:skip")],
    ])


def edit_keyboard(prediction_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✏️ Змінити результат", callback_data=f"result:{prediction_id}:edit")
    ]])


async def send_final(session, raw, matrix_time, rounds_used, image_rows, bot):
    body, direction, expiry = memory.finalize_text(raw, matrix_time)
    platform_time = matrix.now_platform()
    images = [row[3] for row in image_rows]

    prediction_id = memory.save_prediction(
        body, raw, images, platform_time, direction, expiry, rounds_used
    )
    memory.finish_session(session["id"], "completed", platform_time)

    pair = memory.parse_pair(body) or "пара не визначена"
    reminder_line = ""
    if direction == "WAIT":
        due = memory.wait_due(raw, matrix_time)
        if due is not None:
            note = memory.get_field(body, "ТОЧКА ВХОДУ")[:250]
            memory.add_reminder(session["chat_id"], pair, note, due, platform_time)
            reminder_line = f"🔔 Нагадаю о {due:%H:%M:%S}, щоб глянути знову.\n\n"
    text = (
        f"📊 {pair}\n"
        "━━━━━━━━━━━━━━\n"
        f"🧭 {memory.direction_label(direction)}\n\n"
        f"{body}\n\n"
        f"{reminder_line}"
        f"🕒 Час платформи: {platform_time:%H:%M:%S}\n"
        f"🔢 Прогноз №{prediction_id}"
    )
    if direction in ("CALL", "PUT"):
        keyboard = result_keyboard(prediction_id)
    elif reminder_line:
        keyboard = stop_keyboard("⏹ Не нагадувати")
    else:
        keyboard = None
    await send_long(bot, session["chat_id"], text, reply_markup=keyboard)


async def evaluate_session(session_id, bot):
    lock = SESSION_LOCKS.setdefault(session_id, asyncio.Lock())
    async with lock:
        session = memory.get_session(session_id)
        if not session or session["status"] not in ("processing", "waiting"):
            return
        if not memory.is_working():
            memory.finish_session(session_id, "cancelled", matrix.now_platform())
            return
        image_rows = memory.load_session_images(session_id)
        if not image_rows:
            memory.finish_session(session_id, "cancelled", matrix.now_platform())
            return

        chat_id = session["chat_id"]
        round_no = max(row[0] for row in image_rows)
        now_left = max(0, MAX_NOW_REQUESTS - (session["now_used"] or 0))
        later_left = max(0, MAX_LATER_REQUESTS - (session["later_used"] or 0))

        try:
            await bot.send_chat_action(chat_id, "typing")
        except Exception:
            pass

        try:
            T0 = matrix.now_platform()  # від цього моменту рахуються вхід і експірація
            matrix_text = matrix.prompt_matrix(T0)
            extra = None
            raw, verdict = "", {"type": "invalid", "why": "порожня відповідь"}

            for attempt in range(2):
                parts = build_parts(session, image_rows, T0, matrix_text, extra)
                raw = await ai.ask_model(SYSTEM_PROMPT, parts)
                current = memory.get_session(session_id)
                if not current or current["status"] not in ("processing", "waiting"):
                    logger.info("Сесію %s скасовано, поки ШІ думав. Відповідь відкинуто.", session_id)
                    return
                verdict = memory.interpret_reply(raw, now_left, later_left)
                if verdict["type"] != "invalid":
                    break
                logger.warning("Відповідь не прийнято (спроба %d): %s", attempt + 1, verdict["why"])
                extra = (
                    f"ТВОЯ ПОПЕРЕДНЯ ВІДПОВІДЬ НЕ ПРИЙНЯТА: {verdict['why']}. "
                    f"Ось вона:\n{raw[:700]}\nДай нову відповідь за форматом."
                )

            if verdict["type"] == "request":
                await apply_request(session, verdict, raw, round_no, bot)
                return

            if verdict["type"] == "final":
                converted = memory.wait_as_request(raw, T0, later_left)
                if converted is not None:
                    logger.info("Фінал «чекаємо» перетворено на запит скріна пізніше (сесія %s)", session_id)
                    await apply_request(session, converted, raw, round_no, bot)
                    return
                await send_final(session, raw, T0, round_no, image_rows, bot)
                return

            # Не вдалося отримати придатну відповідь: безпечне «не вхід».
            logger.error("Двічі відповідь не за форматом, даю безпечний «не вхід»")
            stub = STUB_FINAL + f"\n\nЩо відповіла модель:\n{raw[:800]}"
            await send_final(session, stub, T0, round_no, image_rows, bot)

        except ai.AIError as error:
            logger.error("Помилка ШІ в сесії %s: %s", session_id, error)
            memory.finish_session(session_id, "cancelled", matrix.now_platform())
            await send_long(bot, chat_id, f"⚠️ {error}\nСесію скасовано, надішли скріни ще раз.")
        except Exception:
            logger.exception("Помилка оцінювання сесії %s", session_id)
            memory.finish_session(session_id, "cancelled", matrix.now_platform())
            await send_long(
                bot, chat_id,
                "Не вдалося завершити аналіз через технічну помилку. "
                "Сесію скасовано, прогноз до статистики не додано.",
            )


# ============================================================
# 4. ПРИЙОМ СКРІНІВ
# ============================================================

async def download_images(messages, context):
    images = []
    for message in messages:
        file_id = None
        if message.photo:
            file_id = message.photo[-1].file_id
        elif message.document and (message.document.mime_type or "").startswith("image/"):
            file_id = message.document.file_id
        if not file_id:
            continue
        telegram_file = await context.bot.get_file(file_id)
        images.append(bytes(await telegram_file.download_as_bytearray()))
    return images


async def process_batch(messages, context):
    if not messages:
        return
    messages.sort(key=lambda m: m.message_id)
    extra_count = max(0, len(messages) - MAX_IMAGES)
    messages = messages[:MAX_IMAGES]

    first = messages[0]
    chat_id = first.chat_id
    user_id = first.from_user.id if first.from_user else 0
    if not memory.is_working():
        return

    lock = CHAT_LOCKS.setdefault(chat_id, asyncio.Lock())
    session_to_run = None
    status_message = None
    try:
        async with lock:
            active = memory.get_active_session(chat_id)

            if active and active["status"] == "processing":
                await first.reply_text("Я ще обробляю попередні скріни. Дочекайся мого наступного повідомлення.")
                return

            if active and active["request_kind"] == "later" and active["due_at"]:
                due = datetime.fromisoformat(active["due_at"])
                wait = (due - matrix.now_platform()).total_seconds()
                if wait > EARLY_TOLERANCE_SECONDS:
                    await first.reply_text(
                        f"Ще рано. Скрін {active['request_timeframe']} потрібен о {due:%H:%M:%S} "
                        f"(через {human_left(wait)}), коли свічка вже сформується. "
                        "Надішли його тоді.\n\nЯкщо це інша пара чи новий графік, скасуй попереднє "
                        "стеження кнопкою нижче й надішли скріни ще раз.",
                        reply_markup=stop_keyboard("⏹ Скасувати стеження"),
                    )
                    return

            status_message = await first.reply_text("📷 Отримала скріни. Дивлюсь...")
            images = await download_images(messages, context)
            if not images:
                await status_message.edit_text("Не вдалося отримати зображення.")
                return

            captured_at = matrix.now_platform()
            matrix_text = matrix.matrix_snapshot(captured_at)

            if active:  # сесія чекала на додатковий скрін
                session_id = active["id"]
                memory.add_session_images(
                    session_id, active["request_round"], images, matrix_text, captured_at
                )
                text = f"✅ Додала {len(images)} скрін(ів) до поточної сесії. Порівнюю з попередніми..."
            else:
                dropped = memory.cancel_reminders(chat_id)
                session_id = memory.create_session(chat_id, user_id, captured_at)
                memory.add_session_images(session_id, 0, images, matrix_text, captured_at)
                text = f"📷 Отримала скрінів: {len(images)}. Аналізую..."
                if dropped:
                    text += "\n(Нагадування про попередній графік скасовано.)"
            if extra_count:
                text += f"\n(Взяла перші {MAX_IMAGES}, ще {extra_count} пропущено.)"
            await status_message.edit_text(text)
            session_to_run = session_id

        if session_to_run is not None:
            await evaluate_session(session_to_run, context.bot)

    except Exception:
        logger.exception("Помилка обробки скрінів")
        try:
            if status_message is not None:
                await status_message.edit_text("Не вдалося обробити скріни. Спробуй надіслати ще раз.")
        except Exception:
            pass


async def flush_batch(key, wait, context):
    try:
        await asyncio.sleep(wait)
        batch = BATCHES.pop(key, None)
        if batch:
            await process_batch(batch["messages"], context)
    except asyncio.CancelledError:
        pass


async def photo_handler(update, context):
    message = update.effective_message
    if not message or not (message.photo or message.document):
        return
    if not allowed(update) or not memory.is_working():
        return

    chat_id = update.effective_chat.id
    if message.media_group_id:
        key, wait = (chat_id, message.media_group_id), ALBUM_WAIT
    else:
        key, wait = (chat_id, "solo"), SOLO_WAIT  # кілька скрінів поспіль теж збираємо разом

    batch = BATCHES.setdefault(key, {"messages": [], "task": None})
    batch["messages"].append(message)
    old = batch.get("task")
    if old and not old.done():
        old.cancel()
    batch["task"] = spawn(flush_batch(key, wait, context))


# ============================================================
# 5. КНОПКИ, КОМАНДИ, РЕЗУЛЬТАТИ
# ============================================================

def main_keyboard():
    return ReplyKeyboardMarkup(
        [["▶️ Почати роботу"], ["🌙 Завершити роботу"]],
        resize_keyboard=True,
        is_persistent=True,
    )


async def start_work(update, context):
    memory.set_working(True)
    await update.effective_message.reply_text(
        "🟢 Nora почала роботу.\nНадсилай будь-які скріни: один чи кілька, будь-яких таймфреймів. "
        "Чого не вистачає, я попрошу сама: зараз або пізніше, коли свічка сформується.",
        reply_markup=main_keyboard(),
    )


async def stop_work(update, context):
    memory.cancel_all_sessions()
    memory.set_working(False)
    await update.effective_message.reply_text(
        "🌙 Nora відпочиває. Незавершені аналізи й нагадування скасовано.",
        reply_markup=main_keyboard(),
    )


async def start_command(update, context):
    if not allowed(update):
        await update.message.reply_text(f"Немає доступу. Твій id: {update.effective_user.id}")
        return
    status = "працюю" if memory.is_working() else "відпочиваю"
    await update.message.reply_text(
        "Я Nora, аналітична система Матриці часу.\n"
        f"Поточний режим: {status}.\n\n"
        "▶️ Почати роботу: активувати аналіз.\n"
        "🌙 Завершити роботу: скасувати незавершені сесії.\n\n"
        "/time: матриця часу зараз\n"
        "/stats: статистика\n"
        "/cancel: скасувати поточний аналіз і нагадування\n"
        "/last: сира відповідь ШІ останнього прогнозу\n"
        "/id: твій Telegram id",
        reply_markup=main_keyboard(),
    )


async def time_command(update, context):
    if not allowed(update):
        return
    await send_long(context.bot, update.effective_chat.id, matrix.full_matrix(matrix.now_platform()))


async def stats_command(update, context):
    if not allowed(update):
        return
    await send_long(context.bot, update.effective_chat.id, memory.stats_text())


async def last_command(update, context):
    if not allowed(update):
        return
    with memory.db_connect() as db:
        row = db.execute(
            "SELECT id, raw_text FROM predictions ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        await update.message.reply_text("Прогнозів ще немає.")
        return
    text = f"Сира відповідь ШІ, прогноз №{row[0]}:\n\n{(row[1] or '—')[:3500]}"
    await send_long(context.bot, update.effective_chat.id, text)


def cancel_report(chat_id):
    sessions, reminders = memory.cancel_tracking(chat_id)
    if not sessions and not reminders:
        return "Зараз немає чого скасовувати. Можеш надсилати скріни."
    parts = []
    if sessions:
        parts.append("поточний аналіз")
    if reminders:
        parts.append("нагадування")
    return f"⏹ Стеження скасовано ({' і '.join(parts)}). Надсилай скріни нового графіка."


async def cancel_command(update, context):
    if not allowed(update):
        return
    await update.effective_message.reply_text(cancel_report(update.effective_chat.id))


async def handle_stop(update, context):
    query = update.callback_query
    if not allowed(update):
        await query.answer("Немає доступу.")
        return
    report = cancel_report(query.message.chat_id)
    await query.answer("Скасовано.")
    base = (query.message.text or "").split("\n\n⏹")[0]
    try:
        await query.edit_message_text(base + "\n\n" + report.replace("\n", " "), reply_markup=None)
    except Exception:
        logger.exception("Не вдалося оновити повідомлення після скасування")


async def id_command(update, context):
    await update.message.reply_text(f"Твій Telegram id: {update.effective_user.id}")


async def text_button_handler(update, context):
    if not allowed(update):
        return
    text = (update.effective_message.text or "").strip()
    if text == "▶️ Почати роботу":
        await start_work(update, context)
    elif text == "⏹ Скасувати стеження":  # лише для старої клавіатури, що могла лишитись у чаті
        await update.effective_message.reply_text(cancel_report(update.effective_chat.id))
    elif text == "🌙 Завершити роботу":
        await stop_work(update, context)


RESULT_LABELS = {"win": "ПЛЮС ✅", "loss": "МІНУС ❌", "skip": "ПРОПУЩЕНО ⏭"}


async def handle_result(update, context):
    query = update.callback_query
    if not allowed(update):
        await query.answer("Немає доступу.")
        return
    try:
        _, prediction_id, outcome = query.data.split(":")
        prediction_id = int(prediction_id)
    except (ValueError, AttributeError):
        await query.answer("Не вдалося прочитати результат.")
        return

    if outcome == "edit":
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=result_keyboard(prediction_id))
        return
    if outcome not in RESULT_LABELS:
        await query.answer("Невідомий результат.")
        return

    changed, previous = memory.set_outcome(prediction_id, outcome)
    if not changed:
        await query.answer("Цей прогноз не оцінюється.")
        return
    if previous == outcome:
        await query.answer("Цей результат уже збережено.")
        await query.edit_message_reply_markup(reply_markup=edit_keyboard(prediction_id))
        return

    await query.answer("Результат збережено.")
    base = (query.message.text or "").split("\n\nРезультат:")[0]
    await query.edit_message_text(
        base + f"\n\nРезультат: {RESULT_LABELS[outcome]}",
        reply_markup=edit_keyboard(prediction_id),
    )


# ============================================================
# 6. НАГАДУВАННЯ ТА АВТОМАТИЧНЕ СКАСУВАННЯ
# ============================================================

async def scheduler_loop(application):
    while True:
        try:
            if memory.is_working():
                now = matrix.now_platform()
                for s in memory.waiting_sessions():
                    if not s["due_at"] or not s["reminder_at"] or not s["cancel_at"]:
                        continue
                    due_at = datetime.fromisoformat(s["due_at"])
                    reminder_at = datetime.fromisoformat(s["reminder_at"])
                    cancel_at = datetime.fromisoformat(s["cancel_at"])
                    chat_id, tf = s["chat_id"], s["request_timeframe"]

                    if not s["due_notified"] and now >= due_at:
                        memory.mark_due_notified(s["id"], now)
                        await application.bot.send_message(
                            chat_id,
                            f"⏰ Час скрина {tf}. Надішли його зараз.\nМета: {s['request_reason']}",
                        )
                    elif s["due_notified"] and not s["reminder_sent"] and now >= reminder_at:
                        memory.mark_reminder_sent(s["id"], now)
                        await application.bot.send_message(
                            chat_id,
                            f"🔔 Nora ще чекає скрін {tf}. Надішли його протягом "
                            f"{human_left((cancel_at - now).total_seconds())}, інакше аналіз буде скасовано.",
                        )
                    elif s["reminder_sent"] and now >= cancel_at:
                        memory.finish_session(s["id"], "cancelled", now)
                        await application.bot.send_message(
                            chat_id,
                            "⌛ Скрін не надійшов. Аналіз скасовано, прогноз не створено, статистику не змінено.",
                        )
                for r in memory.due_reminders(now):
                    memory.mark_reminder_done(r["id"])
                    if (now - r["due_at"]).total_seconds() > REMINDER_MAX_LATE_SECONDS:
                        continue  # бот довго не працював, нагадування застаріло
                    text = f"⏰ Час глянути знову: {r['pair'] or 'графік'}.\nМинуле рішення: не вхід."
                    if r["note"]:
                        text += f"\nЩо мало з'явитися: {r['note']}"
                    text += "\n\nНадішли свіжі скріни (які вважаєш потрібними), Nora подивиться ще раз."
                    await application.bot.send_message(r["chat_id"], text)
            await asyncio.sleep(SCHEDULER_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Помилка планувальника Nora")
            await asyncio.sleep(SCHEDULER_INTERVAL_SECONDS)


async def post_init(application):
    stuck = memory.startup_cleanup()
    if stuck:
        logger.info("Після перезапуску скасовано сесій, що не завершились: %d", stuck)
    spawn(scheduler_loop(application))


async def on_error(update, context):
    logger.error("Необроблена помилка: %s", context.error, exc_info=context.error)


# ============================================================
# 7. ЗАПУСК
# ============================================================

def main():
    memory.init_db()
    logger.info(
        "Nora (%s). База: %s. Моделі: %s. Режим роботи: %s. Доступ: %s",
        memory.version_label(), memory.DB_PATH, ", ".join(ai.MODELS), memory.is_working(),
        f"{len(ALLOWED_USER_IDS)} id" if ALLOWED_USER_IDS else "відкрито для всіх",
    )

    application = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("time", time_command))
    application.add_handler(CommandHandler("last", last_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("id", id_command))
    application.add_handler(CallbackQueryHandler(handle_result, pattern=r"^result:"))
    application.add_handler(CallbackQueryHandler(handle_stop, pattern=r"^stop:"))
    application.add_handler(
        MessageHandler(filters.Regex(r"^(▶️ Почати роботу|⏹ Скасувати стеження|🌙 Завершити роботу)$"), text_button_handler)
    )
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, photo_handler))
    application.add_error_handler(on_error)
    application.run_polling()


if __name__ == "__main__":
    main()
