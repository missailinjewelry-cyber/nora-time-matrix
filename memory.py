"""Пам'ять Nora: база SQLite (сесії, прогнози, щоденник), розбір відповіді ШІ і статистика.

Тут немає нічого про Telegram, тому все це можна перевірити окремо від бота.
"""
import math
import os
import re
import sqlite3
from datetime import datetime, timedelta

import matrix

DB_PATH = os.getenv("DB_PATH", "/data/nora.db")
VERSION = "hybrid-1"  # мітка версії логіки, щоб рахувати статистику окремо від старих прогнозів


def version_label():
    """У режимі light мітка інша, тому статистика двох режимів рахується окремо."""
    return VERSION + ("-light" if matrix.MATRIX_MODE == "light" else "")
STORE_SCREENSHOTS = os.getenv("STORE_SCREENSHOTS", "1").strip().lower() not in ("0", "false", "no", "off")

try:
    PAYOUT = float(os.getenv("PAYOUT", "0.85"))
except ValueError:
    PAYOUT = 0.85
if not 0.3 <= PAYOUT <= 1.5:
    PAYOUT = 0.85

DIARY_HOURS = 3
DIARY_LIMIT = 12
TIMEFRAMES = ["M1", "M2", "M3", "M5", "M10", "M15", "M30", "H1", "H4"]
MAX_NOW_TFS = 3  # скільки таймфреймів Nora може попросити одним запитом СКРІН_ЗАРАЗ


# ============================================================
# 1. БАЗА
# ============================================================

def db_connect():
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.execute("PRAGMA foreign_keys = ON")
    return db


def _add_columns(db, table, columns):
    existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, kind in columns:
        if name not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")


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
                FOREIGN KEY(prediction_id) REFERENCES predictions(id)
            )
        """)
        _add_columns(db, "predictions", [
            ("raw_text", "TEXT"),
            ("platform_minute", "INTEGER"),
            ("summary", "TEXT"),
            ("character", "TEXT"),
            ("version", "TEXT"),
            ("rounds_used", "INTEGER"),
        ])

        db.execute("""
            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        db.execute("INSERT OR IGNORE INTO bot_state(key, value) VALUES ('working', '0')")

        db.execute("""
            CREATE TABLE IF NOT EXISTS analysis_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'processing',
                request_round INTEGER NOT NULL DEFAULT 0,
                request_timeframe TEXT,
                request_reason TEXT,
                due_at TEXT,
                due_notified INTEGER NOT NULL DEFAULT 0,
                reminder_at TEXT,
                reminder_sent INTEGER NOT NULL DEFAULT 0,
                cancel_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        _add_columns(db, "analysis_sessions", [
            ("request_kind", "TEXT"),
            ("now_used", "INTEGER NOT NULL DEFAULT 0"),
            ("later_used", "INTEGER NOT NULL DEFAULT 0"),
            ("notes", "TEXT"),
        ])

        # Нагадування «глянь знову» після фіналу «не вхід, чекаємо N хв»: без сесії й без ШІ.
        db.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                pair TEXT,
                note TEXT,
                due_at TEXT NOT NULL,
                done INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS session_images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                round_no INTEGER NOT NULL,
                captured_at TEXT NOT NULL,
                matrix_text TEXT NOT NULL,
                image_data BLOB NOT NULL,
                FOREIGN KEY(session_id) REFERENCES analysis_sessions(id)
            )
        """)


def startup_cleanup():
    """Після перезапуску: сесії, що «обробляються», вже мертві; зайві скріни прибираємо."""
    stamp = matrix.now_platform().isoformat()
    with db_connect() as db:
        stuck = db.execute("""
            UPDATE analysis_sessions
            SET status='cancelled', due_at=NULL, reminder_at=NULL,
                cancel_at=NULL, updated_at=?
            WHERE status='processing'
        """, (stamp,)).rowcount
        db.execute("""
            DELETE FROM session_images
            WHERE session_id IN (
                SELECT id FROM analysis_sessions
                WHERE status NOT IN ('processing', 'waiting')
            )
        """)
    return stuck


def is_working():
    with db_connect() as db:
        row = db.execute("SELECT value FROM bot_state WHERE key='working'").fetchone()
    return bool(row and row[0] == "1")


def set_working(value):
    with db_connect() as db:
        db.execute("""
            INSERT INTO bot_state(key, value) VALUES ('working', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, ("1" if value else "0",))


# ---------- сесії ----------

SESSION_COLUMNS = (
    "id, chat_id, user_id, status, request_round, request_timeframe, "
    "request_reason, due_at, due_notified, reminder_at, reminder_sent, "
    "cancel_at, request_kind, now_used, later_used, notes"
)


def _session_dict(row):
    if row is None:
        return None
    keys = [k.strip() for k in SESSION_COLUMNS.split(",")]
    return dict(zip(keys, row))


def get_active_session(chat_id):
    with db_connect() as db:
        row = db.execute(f"""
            SELECT {SESSION_COLUMNS} FROM analysis_sessions
            WHERE chat_id=? AND status IN ('processing', 'waiting')
            ORDER BY id DESC LIMIT 1
        """, (chat_id,)).fetchone()
    return _session_dict(row)


def get_session(session_id):
    with db_connect() as db:
        row = db.execute(
            f"SELECT {SESSION_COLUMNS} FROM analysis_sessions WHERE id=?", (session_id,)
        ).fetchone()
    return _session_dict(row)


def create_session(chat_id, user_id, now):
    stamp = now.isoformat()
    with db_connect() as db:
        db.execute("UPDATE reminders SET done=1 WHERE chat_id=? AND done=0", (chat_id,))
        cursor = db.execute("""
            INSERT INTO analysis_sessions (
                chat_id, user_id, status, request_round, created_at, updated_at
            ) VALUES (?, ?, 'processing', 0, ?, ?)
        """, (chat_id, user_id, stamp, stamp))
        return cursor.lastrowid


def add_session_images(session_id, round_no, images, matrix_text, captured_at):
    """Додає скріни й повертає сесію в стан «обробляється» (запит більше не актуальний)."""
    with db_connect() as db:
        for image_bytes in images:
            db.execute("""
                INSERT INTO session_images (
                    session_id, round_no, captured_at, matrix_text, image_data
                ) VALUES (?, ?, ?, ?, ?)
            """, (session_id, round_no, captured_at.isoformat(), matrix_text,
                  sqlite3.Binary(image_bytes)))
        db.execute("""
            UPDATE analysis_sessions
            SET status='processing', due_at=NULL, reminder_at=NULL,
                cancel_at=NULL, updated_at=?
            WHERE id=?
        """, (captured_at.isoformat(), session_id))


def load_session_images(session_id):
    with db_connect() as db:
        return db.execute("""
            SELECT round_no, captured_at, matrix_text, image_data
            FROM session_images WHERE session_id=? ORDER BY id
        """, (session_id,)).fetchall()


def set_session_request(session_id, round_no, kind, timeframe, reason, due_at, now,
                        reminder_delay, cancel_delay):
    """kind: 'now' або 'later'. due_at: коли скрін потрібен (для 'now' це зараз)."""
    reminder_at = due_at + timedelta(seconds=reminder_delay)
    cancel_at = reminder_at + timedelta(seconds=cancel_delay)
    now_inc = 1 if kind == "now" else 0
    later_inc = 1 if kind == "later" else 0
    with db_connect() as db:
        db.execute("""
            UPDATE analysis_sessions
            SET status='waiting', request_round=?, request_kind=?,
                request_timeframe=?, request_reason=?, due_at=?,
                due_notified=?, reminder_at=?, reminder_sent=0, cancel_at=?,
                now_used=now_used+?, later_used=later_used+?, updated_at=?
            WHERE id=?
        """, (round_no, kind, timeframe, reason, due_at.isoformat(),
              1 if kind == "now" else 0,  # про «зараз» користувачку повідомлено одразу
              reminder_at.isoformat(), cancel_at.isoformat(),
              now_inc, later_inc, now.isoformat(), session_id))


def mark_due_notified(session_id, now):
    with db_connect() as db:
        db.execute("UPDATE analysis_sessions SET due_notified=1, updated_at=? WHERE id=?",
                   (now.isoformat(), session_id))


def mark_reminder_sent(session_id, now):
    with db_connect() as db:
        db.execute("UPDATE analysis_sessions SET reminder_sent=1, updated_at=? WHERE id=?",
                   (now.isoformat(), session_id))


def finish_session(session_id, status, now):
    with db_connect() as db:
        db.execute("""
            UPDATE analysis_sessions
            SET status=?, due_at=NULL, reminder_at=NULL, cancel_at=NULL, updated_at=?
            WHERE id=?
        """, (status, now.isoformat(), session_id))
        if status in ("completed", "cancelled"):
            db.execute("DELETE FROM session_images WHERE session_id=?", (session_id,))


def cancel_all_sessions(chat_id=None):
    now = matrix.now_platform().isoformat()
    with db_connect() as db:
        where = "status IN ('processing', 'waiting')"
        args = [now]
        if chat_id is not None:
            where += " AND chat_id=?"
            args.append(chat_id)
        ids = [r[0] for r in db.execute(
            f"SELECT id FROM analysis_sessions WHERE {where}", args[1:]).fetchall()]
        db.execute(f"""
            UPDATE analysis_sessions
            SET status='cancelled', due_at=NULL, reminder_at=NULL,
                cancel_at=NULL, updated_at=?
            WHERE {where}
        """, args)
        for sid in ids:
            db.execute("DELETE FROM session_images WHERE session_id=?", (sid,))
        if chat_id is None:
            db.execute("UPDATE reminders SET done=1 WHERE done=0")
        else:
            db.execute("UPDATE reminders SET done=1 WHERE chat_id=? AND done=0", (chat_id,))


def cancel_reminders(chat_id):
    """Закриває нагадування чату. Повертає, скільки їх було."""
    with db_connect() as db:
        return db.execute("UPDATE reminders SET done=1 WHERE chat_id=? AND done=0", (chat_id,)).rowcount


def cancel_tracking(chat_id):
    """Припиняє стеження в чаті: активні сесії й нагадування. Повертає (сесій, нагадувань)."""
    with db_connect() as db:
        sessions = db.execute(
            "SELECT COUNT(*) FROM analysis_sessions WHERE chat_id=? AND status IN ('processing', 'waiting')",
            (chat_id,)).fetchone()[0]
        reminders = db.execute(
            "SELECT COUNT(*) FROM reminders WHERE chat_id=? AND done=0", (chat_id,)).fetchone()[0]
    cancel_all_sessions(chat_id)
    return sessions, reminders


def waiting_sessions():
    with db_connect() as db:
        rows = db.execute(f"""
            SELECT {SESSION_COLUMNS} FROM analysis_sessions WHERE status='waiting'
        """).fetchall()
    return [_session_dict(r) for r in rows]


def append_notes(session_id, text):
    with db_connect() as db:
        db.execute("""
            UPDATE analysis_sessions
            SET notes = CASE WHEN notes IS NULL OR notes='' THEN ? ELSE notes || char(10) || ? END
            WHERE id=?
        """, (text, text, session_id))


# ============================================================
# 2. РОЗБІР ВІДПОВІДІ ШІ (будь-яка неоднозначність = безпечний WAIT)
# ============================================================

_LEAD = r"^[\s>*_#\-•]*"


def _norm_tf_text(text):
    """Кирилічні М/Н поруч із цифрами → латинські M/H (M5, H1 нерідко пишуть кирилицею)."""
    return re.sub(r"(?<![A-Za-zА-Яа-яІіЇїЄє])([МмНн])(?=\d)",
                  lambda m: "M" if m.group(1) in "Мм" else "H", text)


def get_field(text, field):
    """Значення поля «ПОЛЕ: значення» з урахуванням markdown (**ПОЛЕ:**, - ПОЛЕ: ...)."""
    pattern = _LEAD + re.escape(field) + r"[\s*_]*:[\s*_]*(.*)$"
    match = re.search(pattern, text or "", re.IGNORECASE | re.MULTILINE)
    return match.group(1).strip(" *_") if match else ""


def parse_action(text):
    """Керівний рядок: ДІЯ: ФІНАЛ | СКРІН_ЗАРАЗ M1 | СКРІН_ПІЗНІШЕ M5 через 4 хв / о 14:35.

    Повертає словник {"kind": final|now|later|none|conflict, "tf", "delay_min", "clock"}.
    «conflict» — у рядку кілька різних дій (схоже на шаблон), «none» — рядка немає.
    """
    value = get_field(text, "ДІЯ")
    if not value:
        return {"kind": "none", "tf": None, "delay_min": None, "clock": None}

    value = _norm_tf_text(value)
    upper = value.upper().replace("_", " ").replace("-", " ")
    kinds = []
    if "ФІНАЛ" in upper:
        kinds.append("final")
    if "ЗАРАЗ" in upper:
        kinds.append("now")
    if "ПІЗНІШ" in upper:
        kinds.append("later")
    if len(kinds) != 1:
        return {"kind": "conflict" if kinds else "none", "tf": None, "delay_min": None, "clock": None}

    tfs = []
    for token in re.findall(r"\b(M1|M2|M3|M5|M10|M15|M30|H1|H4)\b", upper):
        if token not in tfs:
            tfs.append(token)
    # СКРІН_ЗАРАЗ може просити до трьох таймфреймів одразу («H1, M1»), СКРІН_ПІЗНІШЕ рівно один.
    # Більше це схоже на шаблон, а не на конкретне питання.
    if kinds[0] == "now":
        if len(tfs) > MAX_NOW_TFS:
            return {"kind": "conflict", "tf": None, "delay_min": None, "clock": None}
        tf = ", ".join(tfs) if tfs else None
    elif kinds[0] == "later":
        if len(tfs) > 1:
            return {"kind": "conflict", "tf": None, "delay_min": None, "clock": None}
        tf = tfs[0] if tfs else None
    else:
        tf = None

    delay = None
    clock = None
    rest = re.sub(r"\b(M1|M2|M3|M5|M10|M15|M30|H1|H4)\b", " ", upper)
    m = re.search(r"\b(?:ЧЕРЕЗ|ЗА)\s*(\d{1,3})\s*ХВ", rest)
    if m:
        delay = int(m.group(1))
    m = re.search(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)(?:[:.]([0-5]\d))?\b", rest)
    if m:
        clock = (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))
    return {"kind": kinds[0], "tf": tf, "delay_min": delay, "clock": clock}


_FIELD_LINE = re.compile(_LEAD + r"[А-ЯІЇЄҐA-Z][А-ЯІЇЄҐA-Z' ]{2,28}[\s*_]*:")


def get_block(text, field):
    """Поле, що може займати кілька рядків (до наступного «ПОЛЕ:»)."""
    lines = (text or "").splitlines()
    head = re.compile(_LEAD + re.escape(field) + r"[\s*_]*:[\s*_]*(.*)$", re.IGNORECASE)
    for i, line in enumerate(lines):
        m = head.match(line)
        if not m:
            continue
        chunk = [m.group(1).strip(" *_")]
        for nxt in lines[i + 1:]:
            if _FIELD_LINE.match(nxt):
                break
            chunk.append(nxt.strip(" *_"))
        return " ".join(part for part in chunk if part).strip()
    return ""


def interpret_reply(raw, now_left, later_left):
    """Що зробила модель: final | request | invalid (з причиною для повторного запиту)."""
    act = parse_action(raw)
    kind = act["kind"]
    if kind in ("now", "later"):
        if act["tf"] is None:
            return {"type": "invalid", "why": "у рядку ДІЯ має бути таймфрейм (M1, M2, M3, M5, M10, M15, M30, H1 або H4): для СКРІН_ЗАРАЗ до трьох через кому, для СКРІН_ПІЗНІШЕ один"}
        left = now_left if kind == "now" else later_left
        if left <= 0:
            name = "СКРІН_ЗАРАЗ" if kind == "now" else "СКРІН_ПІЗНІШЕ"
            return {"type": "invalid", "why": f"ліміт дії {name} вичерпано"}
        reason = get_block(raw, "ЩО ПЕРЕВІРИМО") or "перевірити розвиток руху"
        return {"type": "request", "kind": kind, "tf": act["tf"],
                "delay_min": act["delay_min"], "clock": act["clock"], "reason": reason[:400]}
    if kind == "final" or (kind == "none" and get_field(raw, "ВЕРДИКТ")):
        if not get_field(raw, "ВЕРДИКТ"):
            return {"type": "invalid", "why": "у фінальній відповіді немає рядка ВЕРДИКТ"}
        return {"type": "final"}
    if kind == "conflict":
        return {"type": "invalid", "why": "у рядку ДІЯ має бути одна дія й один таймфрейм, без шаблонних варіантів"}
    return {"type": "invalid", "why": "перший рядок має починатися з «ДІЯ:» (ФІНАЛ, СКРІН_ЗАРАЗ або СКРІН_ПІЗНІШЕ)"}


def parse_pair(text):
    value = get_field(text, "ПАРА")
    value = re.sub(r"\s+", " ", value).strip(" .*_`")
    return value[:40] if value else ""


def pair_key(pair):
    return re.sub(r"[^A-Z0-9]", "", (pair or "").upper().replace("OTC", ""))


def parse_character(text):
    value = get_field(text, "ХАРАКТЕР ГРАФІКА").upper()
    found = [name for name in ("ПРУЖИНА", "ШУМ", "ЗАТОР") if name in value]
    if "ЗМІШАН" in value or len(found) > 1:
        return "ЗМІШАНИЙ"
    return found[0] if found else ""


def parse_verdict(text):
    """CALL / PUT / WAIT за рядком ВЕРДИКТ. Усе незрозуміле або суперечливе → WAIT."""
    value = get_field(text, "ВЕРДИКТ").upper()
    if not value:
        return "WAIT"
    if re.search(r"\bНЕ\s+ВХІД|НЕ\s+ВХОДИТИ|\bWAIT\b", value):
        return "WAIT"
    up = bool(re.search(r"ВГОРУ|\bCALL\b|\bUP\b|↑", value))
    down = bool(re.search(r"ВНИЗ|\bPUT\b|\bDOWN\b|↓", value))
    if up == down:  # обидва або жодного: шаблон чи нісенітниця
        return "WAIT"
    if "ЧЕКАЄМО" in value and not re.search(r"ВХІД\W+(ВГОРУ|ВНИЗ)|\bCALL\b|\bPUT\b", value):
        return "WAIT"
    return "CALL" if up else "PUT"


def parse_expiry(text):
    """Хвилини з рядка ЕКСПІРАЦІЯ (1-60) або None."""
    value = get_field(text, "ЕКСПІРАЦІЯ").lower()
    match = re.search(r"(?<![\d:.])(\d{1,2})\s*хв", value)
    if not match:
        return None
    number = int(match.group(1))
    return number if 1 <= number <= 60 else None


_CLOCK = r"([01]?\d|2[0-3])[:.]([0-5]\d)(?:[:.]([0-5]\d))?"


def parse_expiry_clock(text, now):
    """Режим light: модель називає ЧАС КІНЦЯ («до 20:30:00»), хвилини рахує код від входу.

    Повертає хвилини (1-60) або None. Рядок, що починається з «до HH:MM», вважаємо часом кінця;
    якщо в рядку є «N хв», беремо хвилини; інакше шукаємо час кінця після слів «о», «до», «кінець».
    """
    value = get_field(text, "ЕКСПІРАЦІЯ").upper()
    if not value:
        return None
    start = matrix.entry_time(now)

    def minutes_to(h, mnt, sec):
        end = start.replace(hour=int(h), minute=int(mnt), second=int(sec or 0), microsecond=0)
        if end <= start:
            end += timedelta(days=1)
        minutes = round((end - start).total_seconds() / 60)
        return minutes if 1 <= minutes <= 60 else None

    lead = re.match(r"\s*(?:ДО|О|ОБ)\s*" + _CLOCK, value)
    if lead:
        return minutes_to(*lead.groups())
    plain = parse_expiry(text)
    if plain is not None:
        return plain
    anywhere = re.search(r"(?:ДО|ОБ|\bО|ЗАКІНЧУЄТЬСЯ|КІНЕЦЬ|ЗАКРИТТЯ)\s*" + _CLOCK, value)
    if anywhere:
        return minutes_to(*anywhere.groups())
    return None


def parse_summary(text):
    return get_field(text, "ПІДСУМОК")[:220]


def expiry_timeframes(text):
    """Таймфрейми, названі у рядку ЕКСПІРАЦІЯ (для звірки з розрахунком коду)."""
    value = _norm_tf_text(get_field(text, "ЕКСПІРАЦІЯ")).upper()
    found = []
    for token in re.findall(r"\b(M2|M3|M5|M10|M15|M30|H1|H4)\b", value):
        if token not in found:
            found.append(token)
    return found


def direction_label(direction):
    return {"CALL": "ВГОРУ ↑", "PUT": "ВНИЗ ↓", "WAIT": "НЕ ВХІД ⏳"}.get(direction, "НЕ ВХІД ⏳")


def finalize_text(text, now):
    """Приводить фінальну відповідь до безпечного вигляду.

    Повертає (текст, напрям, хвилини_експірації або None).
    Правила: вхід без зрозумілої експірації → WAIT; для WAIT експірація не потрібна;
    час кінця угоди додає код (за матрицею), а не модель.
    """
    text = (text or "").strip()
    direction = parse_verdict(text)
    expiry = parse_expiry_clock(text, now) if matrix.MATRIX_MODE == "light" else parse_expiry(text)
    notes = []

    if direction in ("CALL", "PUT") and expiry is None:
        notes.append("⚠️ Вхід скасовано кодом: у відповіді немає зрозумілої експірації (1-60 хв від входу).")
        direction = "WAIT"
    if not get_field(text, "ВЕРДИКТ"):
        notes.append("⚠️ У відповіді немає рядка ВЕРДИКТ, тому рахую як «не вхід».")

    if direction in ("CALL", "PUT"):
        end, closing = matrix.expiry_end(expiry, now)
        start = matrix.entry_time(now)
        closing_txt = ", ".join(closing) if closing else "жодна свічка M2+ не закривається"
        notes.append(
            f"🕒 Розрахунок коду: вхід не раніше {start:%H:%M:%S}, експірація {expiry} хв → "
            f"кінець ≈{end:%H:%M:%S}. Там закриваються: {closing_txt}.\n"
            f"Якщо входиш пізніше, зменш експірацію так, щоб кінець лишився о {end:%H:%M}:00."
        )
        named = expiry_timeframes(text)
        wrong = [name for name in named if name not in closing]
        if named and wrong:
            notes.append(
                f"⚠️ Перевірка: у пояснені згадано {', '.join(wrong)}, але в кінці угоди "
                "вони не закриваються. Подумай ще раз, чи підходить така експірація."
            )

    if direction == "WAIT":
        expiry = None

    body = "\n".join(
        line for line in text.splitlines()
        if not re.match(_LEAD + r"ДІЯ[\s*_]*:", line, re.IGNORECASE)
    ).strip()
    if notes:
        body += "\n\n" + "\n".join(notes)
    return body, direction, expiry


def wait_due(text, now):
    """З вердикту «НЕ ВХІД, ЧЕКАЄМО N хв» (або «чекаємо до HH:MM») дістає, коли нагадати.

    Повертає момент або None. Час округлюється до хвилини (стики на :00) і +3 с,
    щоб скрін зробили вже після закриття свічки.
    """
    value = get_field(text, "ВЕРДИКТ").upper()
    if not value:
        return None
    target = None
    match = re.search(r"ЧЕКАЄМО\s*(?:ЩЕ\s*)?(?:ПРИБЛИЗНО\s*|~\s*)?(\d{1,2})\s*ХВ", value)
    if match:
        minutes = int(match.group(1))
        if 1 <= minutes <= 60:
            target = now + timedelta(minutes=minutes)
    else:
        match = re.search(r"ЧЕКАЄМО\s*ДО\s*([01]?\d|2[0-3])[:.]([0-5]\d)", value)
        if match:
            cand = now.replace(hour=int(match.group(1)), minute=int(match.group(2)),
                               second=0, microsecond=0)
            if now < cand <= now + timedelta(minutes=60):
                target = cand
    if target is None:
        return None
    rounded = (target + timedelta(seconds=30)).replace(second=0, microsecond=0)
    return rounded + timedelta(seconds=3)


def wait_as_request(raw, now, later_left):
    """Фінал «НЕ ВХІД, ЧЕКАЄМО N хв» при вільному ліміті «пізніше» стає запитом скріна.

    Так аналіз триває в тій самій сесії (Nora побачить старі й нові скріни разом),
    а не закінчується й починається з нуля. Без числа хвилин або без ліміту повертає None.
    """
    if later_left <= 0 or parse_verdict(raw) != "WAIT":
        return None
    due = wait_due(raw, now)
    if due is None:
        return None
    tfs = []
    for source in (get_field(raw, "ТОЧКА ВХОДУ"), get_field(raw, "АКТИВНИЙ ТАЙМФРЕЙМ")):
        for token in re.findall(r"\b(M1|M2|M3|M5|M10|M15|M30|H1|H4)\b", _norm_tf_text(source).upper()):
            if token not in tfs:
                tfs.append(token)
        if tfs:
            break
    reason = get_field(raw, "ТОЧКА ВХОДУ") or get_field(raw, "ВЕРДИКТ") or "перевірити, що змінилось"
    return {"type": "request", "kind": "later", "tf": ", ".join(tfs[:2]) or "M1",
            "delay_min": None, "clock": None, "due": due, "reason": reason[:300], "converted": True}


def add_reminder(chat_id, pair, note, due_at, now):
    with db_connect() as db:
        db.execute("UPDATE reminders SET done=1 WHERE chat_id=? AND done=0", (chat_id,))
        db.execute("""
            INSERT INTO reminders(chat_id, pair, note, due_at, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (chat_id, pair, note, due_at.isoformat(), now.isoformat()))


def due_reminders(now):
    with db_connect() as db:
        rows = db.execute(
            "SELECT id, chat_id, pair, note, due_at FROM reminders WHERE done=0"
        ).fetchall()
    result = []
    for rid, chat_id, pair, note, due in rows:
        try:
            moment = datetime.fromisoformat(due)
        except ValueError:
            continue
        if moment <= now:
            result.append({"id": rid, "chat_id": chat_id, "pair": pair, "note": note, "due_at": moment})
    return result


def mark_reminder_done(reminder_id):
    with db_connect() as db:
        db.execute("UPDATE reminders SET done=1 WHERE id=?", (reminder_id,))


# ============================================================
# 3. ПРОГНОЗИ І РЕЗУЛЬТАТИ
# ============================================================

def save_prediction(analysis, raw, images, platform_time, direction, expiry, rounds_used):
    pair = parse_pair(analysis)
    entry = get_field(analysis, "ТОЧКА ВХОДУ")[:300]
    summary = parse_summary(analysis)
    character = parse_character(analysis)
    outcome = "pending" if direction in ("CALL", "PUT") else "skip"

    with db_connect() as db:
        cursor = db.execute("""
            INSERT INTO predictions (
                created_at, platform_time, pair, direction, entry,
                expiry_minutes, analysis, screenshot_count, outcome,
                raw_text, platform_minute, summary, character, version, rounds_used
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            platform_time.isoformat(), platform_time.strftime("%Y-%m-%d %H:%M:%S"),
            pair, direction, entry, expiry, analysis, len(images), outcome,
            raw, platform_time.minute, summary, character, version_label(), rounds_used,
        ))
        prediction_id = cursor.lastrowid
        if STORE_SCREENSHOTS:
            for image_bytes in images:
                db.execute("INSERT INTO screenshots(prediction_id, image_data) VALUES (?, ?)",
                           (prediction_id, sqlite3.Binary(image_bytes)))
    return prediction_id


def set_outcome(prediction_id, outcome):
    """Результат можна виправити: повертає (змінено, попередній_результат)."""
    with db_connect() as db:
        row = db.execute("SELECT outcome, direction FROM predictions WHERE id=?",
                         (prediction_id,)).fetchone()
        if row is None:
            return False, None
        previous, direction = row
        if direction not in ("CALL", "PUT"):
            return False, previous  # «не вхід» не оцінюється
        db.execute("UPDATE predictions SET outcome=? WHERE id=?", (outcome, prediction_id))
    return True, previous


# ============================================================
# 4. ЩОДЕННИК (останні години, для послідовності між скрінами)
# ============================================================

_MARK = {"win": "✅", "loss": "❌", "skip": "⏭", "pending": "⏳ результат невідомий"}


def diary_text(now, hours=DIARY_HOURS, limit=DIARY_LIMIT):
    with db_connect() as db:
        rows = db.execute("""
            SELECT id, created_at, pair, direction, expiry_minutes, summary, outcome
            FROM predictions ORDER BY id DESC LIMIT 40
        """).fetchall()

    border = now - timedelta(hours=hours)
    picked = []
    for pid, created, pair, direction, expiry, summary, outcome in rows:
        try:
            moment = datetime.fromisoformat(created)
        except (TypeError, ValueError):
            continue
        if moment < border:
            continue
        label = summary or f"{direction}" + (f", {expiry} хв" if expiry else "")
        if direction in ("CALL", "PUT"):
            mark = _MARK.get(outcome, outcome)
        else:
            mark = "не вхід"
        picked.append(f"[{moment:%H:%M}] №{pid} {pair or 'пара невідома'}: {label} [{mark}]")
        if len(picked) >= limit:
            break
    if not picked:
        return "Щоденник за останні години порожній."
    picked.reverse()
    return "\n".join(picked)


# ============================================================
# 5. СТАТИСТИКА
# ============================================================

def _rate_line(label, wins, losses, breakeven):
    n = wins + losses
    if not n:
        return f"{label}: немає угод"
    p = wins / n
    if n < 30:
        return f"{label}: {wins}/{n} = {p * 100:.0f}% (угод замало для оцінки)"
    margin = 1.96 * math.sqrt(p * (1 - p) / n)
    return f"{label}: {wins}/{n} = {p * 100:.0f}% (±{margin * 100:.0f} п.п.)"


def _bucket(expiry):
    if expiry is None:
        return "без експірації"
    if expiry <= 2:
        return "1-2 хв"
    if expiry <= 5:
        return "3-5 хв"
    if expiry <= 10:
        return "6-10 хв"
    if expiry <= 20:
        return "11-20 хв"
    return "21-60 хв"


_BUCKET_ORDER = ["1-2 хв", "3-5 хв", "6-10 хв", "11-20 хв", "21-60 хв"]


def stats_text():
    breakeven = 1 / (1 + PAYOUT)
    with db_connect() as db:
        rows = db.execute("""
            SELECT direction, outcome, expiry_minutes, character, COALESCE(version, 'старі')
            FROM predictions
        """).fetchall()

    trades = [r for r in rows if r[0] in ("CALL", "PUT")]
    waits = [r for r in rows if r[0] not in ("CALL", "PUT")]
    wins = sum(1 for r in trades if r[1] == "win")
    losses = sum(1 for r in trades if r[1] == "loss")
    skipped = sum(1 for r in trades if r[1] == "skip")
    pending = sum(1 for r in trades if r[1] == "pending")
    done = wins + losses

    lines = [
        "📊 СТАТИСТИКА NORA",
        "",
        f"Прогнозів із входом: {len(trades)} (✅ {wins} | ❌ {losses} | ⏭ пропущено {skipped} | ⏳ без результату {pending})",
        f"Відповідей «не вхід»: {len(waits)} (в угоди не рахуються)",
        "",
        _rate_line("Усього", wins, losses, breakeven),
        f"Поріг окупності при виплаті {PAYOUT * 100:.0f}%: {breakeven * 100:.1f}%",
    ]
    if done < 30:
        lines.append(f"Угод з результатом поки {done}. Менше 30 це замало, висновків робити рано.")
    elif done:
        p = wins / done
        margin = 1.96 * math.sqrt(p * (1 - p) / done)
        if p - margin > breakeven:
            lines.append("Є ознаки переваги над порогом (все одно перевіряй далі).")
        elif p + margin < breakeven:
            lines.append("Нижче порогу окупності.")
        else:
            lines.append("Від порогу окупності статистично не відрізнити.")

    by_dir = {}
    for direction, outcome, *_ in trades:
        if outcome in ("win", "loss"):
            by_dir.setdefault(direction, [0, 0])[0 if outcome == "win" else 1] += 1
    if by_dir:
        lines += ["", "ЗА НАПРЯМКОМ:"]
        for key, name in (("CALL", "ВГОРУ"), ("PUT", "ВНИЗ")):
            if key in by_dir:
                lines.append(_rate_line(name, by_dir[key][0], by_dir[key][1], breakeven))

    by_bucket = {}
    for direction, outcome, expiry, *_ in trades:
        if outcome in ("win", "loss"):
            by_bucket.setdefault(_bucket(expiry), [0, 0])[0 if outcome == "win" else 1] += 1
    if by_bucket:
        lines += ["", "ЗА ЕКСПІРАЦІЄЮ:"]
        for key in _BUCKET_ORDER + ["без експірації"]:
            if key in by_bucket:
                lines.append(_rate_line(key, by_bucket[key][0], by_bucket[key][1], breakeven))

    by_char = {}
    for direction, outcome, expiry, character, version in trades:
        if outcome in ("win", "loss") and character:
            by_char.setdefault(character, [0, 0])[0 if outcome == "win" else 1] += 1
    if by_char:
        lines += ["", "ЗА ХАРАКТЕРОМ ГРАФІКА:"]
        for key, (w, l) in sorted(by_char.items()):
            lines.append(_rate_line(key, w, l, breakeven))

    by_ver = {}
    for direction, outcome, expiry, character, version in trades:
        if outcome in ("win", "loss"):
            by_ver.setdefault(version, [0, 0])[0 if outcome == "win" else 1] += 1
    if len(by_ver) > 1:
        lines += ["", "ЗА ВЕРСІЄЮ ЛОГІКИ:"]
        for key, (w, l) in sorted(by_ver.items()):
            lines.append(_rate_line(key, w, l, breakeven))

    lines += [
        "",
        "Похибка при 30 угодах приблизно ±18 п.п., при 100 приблизно ±10 п.п.",
        "Один плюс чи мінус нічого не доводить. Дивись на серію.",
    ]
    return "\n".join(lines)
