import asyncio
import html
import logging
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, Tuple

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram import Chat
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import os
TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = "assistant_bot.db"

logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# -------------------- БАЗА ДАННЫХ --------------------

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
cur = conn.cursor()


def init_db() -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            created_at TEXT NOT NULL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS groups_table (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            UNIQUE(user_id, name)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            group_name TEXT NOT NULL,
            done INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            event_at TEXT NOT NULL,
            remind_before_minutes INTEGER NOT NULL,
            remind_at TEXT NOT NULL,
            notified INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )

    conn.commit()


def ensure_user(user_id: int) -> None:
    cur.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    if not row:
        now = datetime.now().isoformat()
        cur.execute(
            "INSERT INTO users (user_id, created_at) VALUES (?, ?)",
            (user_id, now),
        )
        default_groups = ["Личное"]
        for group_name in default_groups:
            cur.execute(
                "INSERT OR IGNORE INTO groups_table (user_id, name) VALUES (?, ?)",
                (user_id, group_name),
            )
        conn.commit()


def get_groups(user_id: int) -> list[str]:
    cur.execute(
        "SELECT name FROM groups_table WHERE user_id = ? ORDER BY name COLLATE NOCASE",
        (user_id,),
    )
    return [row["name"] for row in cur.fetchall()]


def add_group(user_id: int, group_name: str) -> bool:
    group_name = group_name.strip()
    if not group_name:
        return False
    try:
        cur.execute(
            "INSERT INTO groups_table (user_id, name) VALUES (?, ?)",
            (user_id, group_name),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False

def count_tasks_in_group(user_id: int, group_name: str) -> int:
    cur.execute(
        "SELECT COUNT(*) AS cnt FROM tasks WHERE user_id = ? AND group_name = ?",
        (user_id, group_name),
    )
    row = cur.fetchone()
    return int(row["cnt"])


def delete_group_db(user_id: int, group_name: str) -> bool:
    cur.execute(
        "DELETE FROM groups_table WHERE user_id = ? AND name = ?",
        (user_id, group_name),
    )
    conn.commit()
    return cur.rowcount > 0


def add_task_db(user_id: int, text: str, group_name: str) -> int:
    now = datetime.now().isoformat()
    cur.execute(
        """
        INSERT INTO tasks (user_id, text, group_name, done, created_at)
        VALUES (?, ?, ?, 0, ?)
        """,
        (user_id, text.strip(), group_name.strip(), now),
    )
    conn.commit()
    return cur.lastrowid


def list_tasks_db(user_id: int, include_done: bool = True) -> list[sqlite3.Row]:
    if include_done:
        cur.execute(
            """
            SELECT * FROM tasks
            WHERE user_id = ?
            ORDER BY done ASC, group_name COLLATE NOCASE, id DESC
            """,
            (user_id,),
        )
    else:
        cur.execute(
            """
            SELECT * FROM tasks
            WHERE user_id = ? AND done = 0
            ORDER BY group_name COLLATE NOCASE, id DESC
            """,
            (user_id,),
        )
    return cur.fetchall()


def get_task(task_id: int, user_id: int) -> Optional[sqlite3.Row]:
    cur.execute(
        "SELECT * FROM tasks WHERE id = ? AND user_id = ?",
        (task_id, user_id),
    )
    return cur.fetchone()


def mark_task_done(task_id: int, user_id: int) -> bool:
    now = datetime.now().isoformat()
    cur.execute(
        """
        UPDATE tasks
        SET done = 1, completed_at = ?
        WHERE id = ? AND user_id = ?
        """,
        (now, task_id, user_id),
    )
    conn.commit()
    return cur.rowcount > 0


def delete_task(task_id: int, user_id: int) -> bool:
    cur.execute(
        "DELETE FROM tasks WHERE id = ? AND user_id = ?",
        (task_id, user_id),
    )
    conn.commit()
    return cur.rowcount > 0

def parse_task_ids_input(raw: str) -> list[int]:
    parts = re.split(r"[,\s]+", raw.strip())
    result = []

    for part in parts:
        if not part:
            continue
        if not part.isdigit():
            raise ValueError("Некорректный список номеров")
        result.append(int(part))

    unique = []
    seen = set()
    for x in result:
        if x not in seen:
            seen.add(x)
            unique.append(x)
    return unique

def map_display_task_numbers_to_ids(
    display_numbers: list[int],
    number_map: dict[int, int],
) -> tuple[list[int], list[int]]:
    real_ids = []
    invalid_numbers = []

    for number in display_numbers:
        real_id = number_map.get(number)
        if real_id is None:
            invalid_numbers.append(number)
        else:
            real_ids.append(real_id)

    return real_ids, invalid_numbers


def bulk_mark_tasks_done(user_id: int, task_ids: list[int]) -> tuple[list[int], list[int]]:
    done_ids = []
    failed_ids = []

    for task_id in task_ids:
        if mark_task_done(task_id, user_id):
            done_ids.append(task_id)
        else:
            failed_ids.append(task_id)

    return done_ids, failed_ids


def bulk_delete_tasks(user_id: int, task_ids: list[int]) -> tuple[list[int], list[int]]:
    deleted_ids = []
    failed_ids = []

    for task_id in task_ids:
        if delete_task(task_id, user_id):
            deleted_ids.append(task_id)
        else:
            failed_ids.append(task_id)

    return deleted_ids, failed_ids

def bulk_delete_reminders(user_id: int, reminder_ids: list[int]) -> tuple[list[int], list[int]]:
    deleted_ids = []
    failed_ids = []

    for reminder_id in reminder_ids:
        if delete_reminder(reminder_id, user_id):
            deleted_ids.append(reminder_id)
        else:
            failed_ids.append(reminder_id)

    return deleted_ids, failed_ids


def map_display_reminder_numbers_to_ids(
    display_numbers: list[int],
    number_map: dict[int, int],
) -> tuple[list[int], list[int]]:
    real_ids = []
    invalid_numbers = []

    for number in display_numbers:
        real_id = number_map.get(number)
        if real_id is None:
            invalid_numbers.append(number)
        else:
            real_ids.append(real_id)

    return real_ids, invalid_numbers


def add_reminder_db(
    user_id: int,
    chat_id: int,
    text: str,
    event_at: datetime,
    remind_before_minutes: int,
) -> int:
    remind_at = event_at - timedelta(minutes=remind_before_minutes)
    now = datetime.now().isoformat()

    cur.execute(
        """
        INSERT INTO reminders (
            user_id, chat_id, text, event_at, remind_before_minutes,
            remind_at, notified, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, 0, ?)
        """,
        (
            user_id,
            chat_id,
            text.strip(),
            event_at.isoformat(),
            remind_before_minutes,
            remind_at.isoformat(),
            now,
        ),
    )
    conn.commit()
    return cur.lastrowid


def get_reminder(reminder_id: int, user_id: int) -> Optional[sqlite3.Row]:
    cur.execute(
        "SELECT * FROM reminders WHERE id = ? AND user_id = ?",
        (reminder_id, user_id),
    )
    return cur.fetchone()

def find_duplicate_reminder(
    user_id: int,
    chat_id: int,
    text: str,
    event_at: datetime,
    remind_before_minutes: int,
) -> Optional[sqlite3.Row]:
    cur.execute(
        """
        SELECT * FROM reminders
        WHERE user_id = ?
          AND chat_id = ?
          AND text = ?
          AND event_at = ?
          AND remind_before_minutes = ?
          AND notified = 0
        LIMIT 1
        """,
        (
            user_id,
            chat_id,
            text.strip(),
            event_at.isoformat(),
            remind_before_minutes,
        ),
    )
    return cur.fetchone()


def list_reminders_db(user_id: int, future_only: bool = False) -> list[sqlite3.Row]:
    if future_only:
        cur.execute(
            """
            SELECT * FROM reminders
            WHERE user_id = ? AND event_at >= ?
            ORDER BY event_at ASC
            """,
            (user_id, datetime.now().isoformat()),
        )
    else:
        cur.execute(
            """
            SELECT * FROM reminders
            WHERE user_id = ?
            ORDER BY event_at ASC
            """,
            (user_id,),
        )
    return cur.fetchall()


def delete_reminder(reminder_id: int, user_id: int) -> bool:
    cur.execute(
        "DELETE FROM reminders WHERE id = ? AND user_id = ?",
        (reminder_id, user_id),
    )
    conn.commit()
    return cur.rowcount > 0


def mark_reminder_notified(reminder_id: int) -> None:
    cur.execute(
        "UPDATE reminders SET notified = 1 WHERE id = ?",
        (reminder_id,),
    )
    conn.commit()


def get_pending_reminders() -> list[sqlite3.Row]:
    cur.execute(
        """
        SELECT * FROM reminders
        WHERE notified = 0
        ORDER BY remind_at ASC
        """
    )
    return cur.fetchall()

def delete_all_tasks(user_id: int) -> None:
    cur.execute("DELETE FROM tasks WHERE user_id = ?", (user_id,))
    conn.commit()


def delete_all_reminders(user_id: int) -> None:
    cur.execute("DELETE FROM reminders WHERE user_id = ?", (user_id,))
    conn.commit()


def delete_all_groups(user_id: int) -> None:
    cur.execute("DELETE FROM groups_table WHERE user_id = ?", (user_id,))
    conn.commit()


def reset_default_groups(user_id: int) -> None:
    default_groups = ["Личное", "Работа", "Учёба"]
    for group_name in default_groups:
        cur.execute(
            "INSERT OR IGNORE INTO groups_table (user_id, name) VALUES (?, ?)",
            (user_id, group_name),
        )
    conn.commit()


# -------------------- УТИЛИТЫ --------------------

def escape(text: str) -> str:
    return html.escape(text, quote=False)


def only_private(update: Update) -> bool:
    message = update.effective_message
    chat = update.effective_chat
    return bool(message and chat and chat.type == Chat.PRIVATE)


def format_dt(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y %H:%M")


def reminder_offset_label(minutes: int) -> str:
    if minutes < 60:
        return f"за {minutes} мин"
    hours = minutes // 60
    if minutes % 60 == 0:
        return f"за {hours} ч"
    return f"за {hours} ч {minutes % 60} мин"


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("➕ Новая задача", callback_data="menu:add_task"),
                InlineKeyboardButton("⏰ Новое напоминание", callback_data="menu:add_reminder"),
            ],
            [
                InlineKeyboardButton("📋 Мои задачи", callback_data="menu:list_tasks"),
                InlineKeyboardButton("🗓 Мои напоминания", callback_data="menu:list_reminders"),
            ],
            [
                InlineKeyboardButton("🏷 Группы", callback_data="menu:groups"),
                InlineKeyboardButton("ℹ️ Помощь", callback_data="menu:help"),
            ],
        ]
    )


def groups_menu(user_id: int, prefix: str) -> InlineKeyboardMarkup:
    groups = get_groups(user_id)
    keyboard = []
    for group_name in groups:
        keyboard.append(
            [InlineKeyboardButton(group_name, callback_data=f"{prefix}:{group_name}")]
        )
    keyboard.append([InlineKeyboardButton("➕ Добавить группу", callback_data="groups:add")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu:back")])
    return InlineKeyboardMarkup(keyboard)

def groups_delete_menu(user_id: int) -> InlineKeyboardMarkup:
    groups = get_groups(user_id)
    keyboard = []
    for group_name in groups:
        keyboard.append(
            [InlineKeyboardButton(f"🗑 {group_name}", callback_data=f"group_delete:{group_name}")]
        )
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu:groups")])
    return InlineKeyboardMarkup(keyboard)


def reminder_offsets_menu() -> InlineKeyboardMarkup:
    options = [
        ("5 мин", 5),
        ("15 мин", 15),
        ("30 мин", 30),
        ("1 час", 60),
        ("2 часа", 120),
        ("1 день", 1440),
    ]
    keyboard = [
        [InlineKeyboardButton(label, callback_data=f"remind_offset:{minutes}")]
        for label, minutes in options
    ]
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu:back")])
    return InlineKeyboardMarkup(keyboard)


def parse_relative_duration(text: str) -> Optional[datetime]:
    s = text.strip().lower()

    match = re.search(r"через\s+(\d+)\s*(мин|мину|минут|м|час|часа|часов|ч)\b", s)
    if not match:
        return None

    value = int(match.group(1))
    unit = match.group(2)

    now = datetime.now()
    if unit.startswith("ч"):
        return now + timedelta(hours=value)
    return now + timedelta(minutes=value)


def parse_reminder_text(text: str) -> Optional[Tuple[str, datetime]]:
    raw = " ".join(text.strip().split())
    s = raw.lower()
    now = datetime.now()

    # через N минут/часов
    rel_dt = parse_relative_duration(s)
    if rel_dt:
        cleaned = re.sub(
            r"через\s+\d+\s*(мин|мину|минут|м|час|часа|часов|ч)\b",
            "",
            raw,
            flags=re.IGNORECASE,
        ).strip(" ,.-")
        if cleaned:
            return cleaned, rel_dt

    # завтра в HH:MM ...
    match = re.search(r"\bзавтра\b(?:\s*в)?\s*(\d{1,2}:\d{2})", s)
    if match:
        hour, minute = map(int, match.group(1).split(":"))
        event_dt = (now + timedelta(days=1)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        cleaned = re.sub(r"\bзавтра\b(?:\s*в)?\s*\d{1,2}:\d{2}", "", raw, flags=re.IGNORECASE).strip(" ,.-")
        if cleaned:
            return cleaned, event_dt

    # сегодня в HH:MM ...
    match = re.search(r"\bсегодня\b(?:\s*в)?\s*(\d{1,2}:\d{2})", s)
    if match:
        hour, minute = map(int, match.group(1).split(":"))
        event_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if event_dt <= now:
            return None
        cleaned = re.sub(r"\bсегодня\b(?:\s*в)?\s*\d{1,2}:\d{2}", "", raw, flags=re.IGNORECASE).strip(" ,.-")
        if cleaned:
            return cleaned, event_dt

    # DD.MM HH:MM текст
    match = re.search(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?\s+(\d{1,2}:\d{2})\b", s)
    if match:
        day = int(match.group(1))
        month = int(match.group(2))
        year = int(match.group(3)) if match.group(3) else now.year
        hour, minute = map(int, match.group(4).split(":"))

        try:
            event_dt = datetime(year, month, day, hour, minute)
            if not match.group(3) and event_dt < now:
                event_dt = datetime(year + 1, month, day, hour, minute)
        except ValueError:
            return None

        cleaned = re.sub(
            r"\b\d{1,2}\.\d{1,2}(?:\.\d{4})?\s+\d{1,2}:\d{2}\b",
            "",
            raw,
            flags=re.IGNORECASE,
        ).strip(" ,.-")
        if cleaned:
            return cleaned, event_dt

    return None


def looks_like_reminder(text: str) -> bool:
    s = text.lower()
    patterns = [
        r"\bзавтра\b",
        r"\bсегодня\b",
        r"\bчерез\s+\d+\s*(мин|мину|минут|м|час|часа|часов|ч)\b",
        r"\b\d{1,2}\.\d{1,2}(?:\.\d{4})?\s+\d{1,2}:\d{2}\b",
        r"\b\d{1,2}:\d{2}\b",
    ]
    return any(re.search(p, s) for p in patterns)

def looks_like_greeting(text: str) -> bool:
    normalized = text.strip().lower()
    greetings = {
        "привет",
        "приветик",
        "здравствуй",
        "здравствуйте",
        "добрый день",
        "доброе утро",
        "добрый вечер",
        "хай",
        "hello",
        "hi",
        "start",
        "/start",
    }
    return normalized in greetings


async def send_or_edit(query, text: str, reply_markup=None) -> None:
    try:
        await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except Exception:
        await query.message.reply_text(text=text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


# -------------------- НАПОМИНАНИЯ --------------------

async def reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    reminder_id = context.job.data["reminder_id"]

    cur.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,))
    row = cur.fetchone()
    if not row:
        return
    if row["notified"] == 1:
        return

    try:
        event_at = datetime.fromisoformat(row["event_at"])
        text = (
            f"⏰ <b>Напоминание</b>\n\n"
            f"<b>Событие:</b> {escape(row['text'])}\n"
            f"<b>Когда:</b> {escape(format_dt(event_at))}\n"
            f"<b>Напомнили:</b> {escape(reminder_offset_label(row['remind_before_minutes']))}"
        )
        await context.bot.send_message(
            chat_id=row["chat_id"],
            text=text,
            parse_mode=ParseMode.HTML,
        )
        mark_reminder_notified(reminder_id)
    except Exception as e:
        logger.exception("Ошибка отправки напоминания %s: %s", reminder_id, e)


def schedule_one_reminder(app: Application, reminder_row: sqlite3.Row) -> None:
    if not app.job_queue:
        logger.warning("job_queue недоступен, напоминание не запланировано")
        return

    remind_at = datetime.fromisoformat(reminder_row["remind_at"])
    now = datetime.now()

    if reminder_row["notified"] == 1:
        return

    delay_seconds = (remind_at - now).total_seconds()
    if delay_seconds <= 0:
        return

    job_name = f"reminder_{reminder_row['id']}"
    old_jobs = app.job_queue.get_jobs_by_name(job_name)
    for job in old_jobs:
        job.schedule_removal()

    app.job_queue.run_once(
        reminder_job,
        when=delay_seconds,
        data={"reminder_id": reminder_row["id"]},
        name=job_name,
    )

    logger.info(
        "Напоминание %s запланировано через %.1f сек",
        reminder_row["id"],
        delay_seconds,
    )


def schedule_all_pending_reminders(app: Application) -> None:
    rows = get_pending_reminders()
    for row in rows:
        schedule_one_reminder(app, row)


# -------------------- ЭКРАНЫ --------------------

async def show_help(target, is_query: bool = False) -> None:
    text = (
        "<b>Как пользоваться</b>\n\n"
        "Ты можешь писать просто сообщением:\n"
        "• <code>купить молоко</code> → задача\n"
        "• <code>завтра в 14:00 к врачу</code> → напоминание\n"
        "• <code>25.04 16:30 встреча</code> → напоминание\n"
        "• <code>через 2 часа выключить духовку</code> → напоминание\n\n"
        "<b>Команды</b>\n"
        "• /start — начало работы\n"
        "• /menu — меню\n"
        "• /help — помощь\n"
	"• /deleteall — удалить все данные\n\n"
        "Бот работает только в личных сообщениях."
    )
    if is_query:
        await send_or_edit(target, text, main_menu())
    else:
        await target.reply_text(text, reply_markup=main_menu(), parse_mode=ParseMode.HTML)


async def show_tasks(query, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = list_tasks_db(user_id, include_done=True)
    if not rows:
        context.user_data["task_number_map"] = {}
        await send_or_edit(
            query,
            "📋 <b>Задач пока нет.</b>",
            InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("➕ Добавить задачу", callback_data="menu:add_task")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="menu:back")],
                ]
            ),
        )
        return

    text_lines = ["<b>Твои задачи</b>\n"]
    current_group = None
    number_map = {}
    display_number = 1

    for row in rows:
        group_name = row["group_name"]
        if group_name != current_group:
            current_group = group_name
            text_lines.append(f"\n<b>{escape(group_name)}</b>")

        task_text = escape(row["text"])
        if row["done"]:
            task_text = f"<s>{task_text}</s>"
            prefix = "✅"
        else:
            prefix = "▫️"

        text_lines.append(f"{prefix} {display_number}. {task_text}")
        number_map[display_number] = row["id"]
        display_number += 1

    context.user_data["task_number_map"] = number_map

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Отметить по номерам", callback_data="tasks:bulk_done")],
            [InlineKeyboardButton("🗑 Удалить по номерам", callback_data="tasks:bulk_delete")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="menu:back")],
        ]
    )

    await send_or_edit(
        query,
        "\n".join(text_lines),
        keyboard,
    )


async def show_reminders(query, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = list_reminders_db(user_id, future_only=False)
    if not rows:
        context.user_data["reminder_number_map"] = {}
        await send_or_edit(
            query,
            "🗓 <b>Напоминаний пока нет.</b>",
            InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("⏰ Создать напоминание", callback_data="menu:add_reminder")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="menu:back")],
                ]
            ),
        )
        return

    now = datetime.now()
    text_lines = ["<b>Твои напоминания</b>\n"]
    number_map = {}
    display_number = 1

    for row in rows:
        event_at = datetime.fromisoformat(row["event_at"])
        status = "🔜" if event_at >= now else "🕓"

        text_lines.append(
            f"{status} {display_number}. {escape(row['text'])}\n"
            f"— {escape(format_dt(event_at))} ({escape(reminder_offset_label(row['remind_before_minutes']))})"
        )

        number_map[display_number] = row["id"]
        display_number += 1

    context.user_data["reminder_number_map"] = number_map

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🗑 Удалить напоминания по номерам", callback_data="reminders:bulk_delete")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="menu:back")],
        ]
    )

    await send_or_edit(
        query,
        "\n\n".join(text_lines),
        keyboard,
    )


# -------------------- КОМАНДЫ --------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not only_private(update):
        return

    user = update.effective_user
    ensure_user(user.id)

    # сброс черновиков состояний
    context.user_data.pop("pending_task_text", None)
    context.user_data.pop("pending_task_group", None)
    context.user_data.pop("pending_reminder_text", None)
    context.user_data.pop("pending_reminder_dt", None)

    await update.message.reply_text(
        "Привет. Это твой личный бот для задач и напоминаний.",
        reply_markup=main_menu(),
    )


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not only_private(update):
        return
    ensure_user(update.effective_user.id)
    await update.message.reply_text("Главное меню:", reply_markup=main_menu())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not only_private(update):
        return
    ensure_user(update.effective_user.id)
    await show_help(update.message, is_query=False)

async def deleteall_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not only_private(update):
        return

    user_id = update.effective_user.id
    ensure_user(user_id)

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Да, удалить всё", callback_data="deleteall:confirm")],
            [InlineKeyboardButton("❌ Отмена", callback_data="deleteall:cancel")],
        ]
    )

    await update.message.reply_text(
        "⚠️ Ты точно хочешь удалить все данные бота?\n\n"
        "Будет удалено:\n"
        "• все задачи\n"
        "• все напоминания\n"
        "• все пользовательские группы\n\n"
        "Стандартные группы будут восстановлены.",
        reply_markup=keyboard,
    )

# -------------------- CALLBACKS --------------------

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not update.effective_chat or update.effective_chat.type != Chat.PRIVATE:
        return

    user_id = query.from_user.id
    ensure_user(user_id)

    data = query.data

    if data == "menu:back":
        await send_or_edit(query, "Главное меню:", main_menu())
        return

    if data == "menu:help":
        await show_help(query, is_query=True)
        return

    if data == "deleteall:cancel":
        await send_or_edit(
            query,
            "Удаление отменено.",
            main_menu(),
        )
        return

    if data == "deleteall:confirm":
        # снять запланированные напоминания пользователя
        if context.application.job_queue:
            rows = list_reminders_db(user_id, future_only=False)
            for row in rows:
                job_name = f"reminder_{row['id']}"
                for job in context.application.job_queue.get_jobs_by_name(job_name):
                    job.schedule_removal()

        delete_all_tasks(user_id)
        delete_all_reminders(user_id)
        delete_all_groups(user_id)
        reset_default_groups(user_id)

        context.user_data.clear()

        await send_or_edit(
            query,
            "🧹 Все данные бота удалены.\n\n"
            "• задачи очищены\n"
            "• напоминания очищены\n"
            "• группы сброшены к стандартным",
            main_menu(),
        )
        return

    if data == "menu:list_tasks":
        await show_tasks(query, user_id, context)
        return

    if data == "menu:list_reminders":
        await show_reminders(query, user_id, context)
        return

    if data == "menu:add_task":
        context.user_data["mode"] = "waiting_task_text"
        await send_or_edit(
            query,
            "Напиши текст задачи.\n\nНапример:\n<code>купить корм</code>",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="menu:back")]]),
        )
        return

    if data == "menu:add_reminder":
        context.user_data["mode"] = "waiting_reminder_text"
        await send_or_edit(
            query,
            "Напиши напоминание обычным текстом.\n\n"
            "Примеры:\n"
            "<code>завтра в 14:00 к врачу</code>\n"
            "<code>25.04 16:30 встреча</code>\n"
            "<code>через 2 часа выключить духовку</code>",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="menu:back")]]),
        )
        return

    if data == "menu:groups":
        await send_or_edit(
            query,
            "Управление группами:",
            InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("📋 Показать группы", callback_data="groups:list")],
                    [InlineKeyboardButton("➕ Добавить группу", callback_data="groups:add")],
                    [InlineKeyboardButton("🗑 Удалить группу", callback_data="groups:delete_menu")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="menu:back")],
                ]
            ),
        )
        return

    if data == "groups:add":
        context.user_data["mode"] = "waiting_new_group_name"
        await send_or_edit(
            query,
            "Напиши название новой группы.\n\nНапример: <code>Покупки</code>",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="menu:back")]]),
        )
        return

    if data == "groups:list":
        await send_or_edit(
            query,
            "Список групп:",
            groups_menu(user_id, "noop_group"),
        )
        return

    if data == "groups:delete_menu":
        await send_or_edit(
            query,
            "Выбери группу для удаления:",
            groups_delete_menu(user_id),
        )
        return

    if data.startswith("group_delete:"):
        group_name = data.split(":", 1)[1]
        tasks_count = count_tasks_in_group(user_id, group_name)

        if tasks_count > 0:
            await send_or_edit(
                query,
                f"Нельзя удалить группу <b>{escape(group_name)}</b>, "
                f"потому что в ней есть задачи: <b>{tasks_count}</b>.\n\n"
                f"Сначала удали или перенеси задачи из этой группы.",
                groups_delete_menu(user_id),
            )
            return

        ok = delete_group_db(user_id, group_name)
        if ok:
            await send_or_edit(
                query,
                f"🏷 Группа <b>{escape(group_name)}</b> удалена.",
                groups_delete_menu(user_id),
            )
        else:
            await send_or_edit(
                query,
                "Не удалось удалить группу.",
                groups_delete_menu(user_id),
            )
        return

    if data.startswith("noop_group:"):
        await send_or_edit(query, "Твои группы:", groups_menu(user_id, "noop_group"))
        return

    if data.startswith("task_group:"):
        group_name = data.split(":", 1)[1]
        task_text = context.user_data.get("pending_task_text")
        if not task_text:
            await send_or_edit(query, "Не нашёл текст задачи. Попробуй ещё раз.", main_menu())
            return

        task_id = add_task_db(user_id, task_text, group_name)
        context.user_data.pop("pending_task_text", None)
        context.user_data["mode"] = None

        await send_or_edit(
            query,
            f"✅ <b>Задача сохранена</b>\n\n"
            f"<b>ID:</b> {task_id}\n"
            f"<b>Текст:</b> {escape(task_text)}\n"
            f"<b>Группа:</b> {escape(group_name)}",
            main_menu(),
        )
        return

        
    if data == "tasks:bulk_done":
        context.user_data["mode"] = "waiting_bulk_done_ids"
        await send_or_edit(
            query,
            "Введи номера задач, которые нужно отметить выполненными.\n\n"
            "Пример:\n<code>1,2,5</code>",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="menu:list_tasks")]]),
        )
        return

    if data == "tasks:bulk_delete":
        context.user_data["mode"] = "waiting_bulk_delete_ids"
        await send_or_edit(
            query,
            "Введи номера задач, которые нужно удалить.\n\n"
            "Пример:\n<code>3,4,9</code>",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="menu:list_tasks")]]),
        )
        return

    if data == "reminders:bulk_delete":
        context.user_data["mode"] = "waiting_bulk_delete_reminder_ids"
        await send_or_edit(
            query,
            "Введи номера напоминаний, которые нужно удалить.\n\n"
            "Пример:\n<code>1,3,5</code>",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="menu:list_reminders")]]),
        )
        return

    if data.startswith("remind_offset:"):
        if context.user_data.get("saving_reminder"):
            await query.answer("Напоминание уже сохраняется...")
            return

        context.user_data["saving_reminder"] = True

        try:
            minutes = int(data.split(":", 1)[1])
            pending_text = context.user_data.get("pending_reminder_text")
            pending_dt_iso = context.user_data.get("pending_reminder_dt")

            if not pending_text or not pending_dt_iso:
                context.user_data["saving_reminder"] = False
                await send_or_edit(query, "Черновик напоминания не найден. Попробуй заново.", main_menu())
                return

            event_at = datetime.fromisoformat(pending_dt_iso)
            remind_at = event_at - timedelta(minutes=minutes)

            if remind_at <= datetime.now():
                context.user_data["saving_reminder"] = False
                await send_or_edit(
                    query,
                    "⚠️ Время напоминания уже прошло.\n"
                    "Выбери меньший интервал или более позднее событие.",
                    reminder_offsets_menu(),
                )
                return

            duplicate = find_duplicate_reminder(
                user_id=user_id,
                chat_id=query.message.chat_id,
                text=pending_text,
                event_at=event_at,
                remind_before_minutes=minutes,
            )

            if duplicate:
                context.user_data.pop("pending_reminder_text", None)
                context.user_data.pop("pending_reminder_dt", None)
                context.user_data["mode"] = None
                context.user_data["saving_reminder"] = False

                await send_or_edit(
                    query,
                    f"⏰ <b>Такое напоминание уже есть</b>\n\n"
                    f"<b>Событие:</b> {escape(pending_text)}\n"
                    f"<b>Когда:</b> {escape(format_dt(event_at))}\n"
                    f"<b>Напомнить:</b> {escape(reminder_offset_label(minutes))}",
                    main_menu(),
                )
                return

            reminder_id = add_reminder_db(
                user_id=user_id,
                chat_id=query.message.chat_id,
                text=pending_text,
                event_at=event_at,
                remind_before_minutes=minutes,
            )

            row = get_reminder(reminder_id, user_id)
            if row:
                schedule_one_reminder(context.application, row)

            context.user_data.pop("pending_reminder_text", None)
            context.user_data.pop("pending_reminder_dt", None)
            context.user_data["mode"] = None
            context.user_data["saving_reminder"] = False

            await send_or_edit(
                query,
                f"⏰ <b>Напоминание сохранено</b>\n\n"
                f"<b>Событие:</b> {escape(pending_text)}\n"
                f"<b>Когда:</b> {escape(format_dt(event_at))}\n"
                f"<b>Напомнить:</b> {escape(reminder_offset_label(minutes))}",
                main_menu(),
            )
            return
        finally:
            context.user_data["saving_reminder"] = False


# -------------------- СООБЩЕНИЯ --------------------

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not only_private(update):
        return

    user = update.effective_user
    ensure_user(user.id)

    text = update.message.text.strip()
    if not text:
        return

    mode = context.user_data.get("mode")

    # 1) ожидаем новую группу
    if mode == "waiting_new_group_name":
        group_name = text.strip()
        if len(group_name) > 30:
            await update.message.reply_text("Название слишком длинное. До 30 символов.")
            return

        ok = add_group(user.id, group_name)
        context.user_data["mode"] = None

        if ok:
            await update.message.reply_text(
                f"🏷 Группа <b>{escape(group_name)}</b> добавлена.",
                reply_markup=main_menu(),
                parse_mode=ParseMode.HTML,
            )
        else:
            await update.message.reply_text(
                "Не удалось добавить группу. Возможно, она уже существует.",
                reply_markup=main_menu(),
            )
        return

    if mode == "waiting_bulk_done_ids":
        try:
            display_numbers = parse_task_ids_input(text)
        except ValueError:
            await update.message.reply_text("Неверный формат. Пример: 1,2,5")
            return

        number_map = context.user_data.get("task_number_map", {})
        task_ids, invalid_numbers = map_display_task_numbers_to_ids(display_numbers, number_map)

        done_ids, failed_ids = bulk_mark_tasks_done(user.id, task_ids)
        context.user_data["mode"] = None

        msg = []
        if done_ids:
            msg.append(f"✅ Отмечены как выполненные номера: {', '.join(map(str, display_numbers))}")
        if invalid_numbers:
            msg.append(f"⚠️ Нет таких номеров в текущем списке: {', '.join(map(str, invalid_numbers))}")
        if failed_ids:
            msg.append("⚠️ Часть задач не удалось изменить.")

        await update.message.reply_text("\n".join(msg), reply_markup=main_menu())
        return

    if mode == "waiting_bulk_delete_ids":
        try:
            display_numbers = parse_task_ids_input(text)
        except ValueError:
            await update.message.reply_text("Неверный формат. Пример: 3,4,9")
            return

        number_map = context.user_data.get("task_number_map", {})
        task_ids, invalid_numbers = map_display_task_numbers_to_ids(display_numbers, number_map)

        deleted_ids, failed_ids = bulk_delete_tasks(user.id, task_ids)
        context.user_data["mode"] = None

        msg = []
        if task_ids and deleted_ids:
            deleted_display_numbers = [n for n in display_numbers if n not in invalid_numbers]
            msg.append(f"🗑 Удалены номера: {', '.join(map(str, deleted_display_numbers))}")
        if invalid_numbers:
            msg.append(f"⚠️ Нет таких номеров в текущем списке: {', '.join(map(str, invalid_numbers))}")
        if failed_ids:
            msg.append("⚠️ Часть задач не удалось удалить.")

        await update.message.reply_text("\n".join(msg), reply_markup=main_menu())
        return 

    if mode == "waiting_bulk_delete_reminder_ids":
        try:
            display_numbers = parse_task_ids_input(text)
        except ValueError:
            await update.message.reply_text("Неверный формат. Пример: 1,3,5")
            return

        number_map = context.user_data.get("reminder_number_map", {})
        reminder_ids, invalid_numbers = map_display_reminder_numbers_to_ids(display_numbers, number_map)

        if context.application.job_queue:
            for reminder_id in reminder_ids:
                job_name = f"reminder_{reminder_id}"
                for job in context.application.job_queue.get_jobs_by_name(job_name):
                    job.schedule_removal()

        deleted_ids, failed_ids = bulk_delete_reminders(user.id, reminder_ids)
        context.user_data["mode"] = None

        msg = []
        if reminder_ids and deleted_ids:
            deleted_display_numbers = [n for n in display_numbers if n not in invalid_numbers]
            msg.append(f"🗑 Удалены напоминания с номерами: {', '.join(map(str, deleted_display_numbers))}")
        if invalid_numbers:
            msg.append(f"⚠️ Нет таких номеров в текущем списке: {', '.join(map(str, invalid_numbers))}")
        if failed_ids:
            msg.append("⚠️ Часть напоминаний не удалось удалить.")

        await update.message.reply_text("\n".join(msg), reply_markup=main_menu())
        return   

# 2) ожидаем текст задачи
    if mode == "waiting_task_text":
        context.user_data["pending_task_text"] = text
        context.user_data["mode"] = None
        await update.message.reply_text(
            f"Выбери группу для задачи:\n\n<b>{escape(text)}</b>",
            reply_markup=groups_menu(user.id, "task_group"),
            parse_mode=ParseMode.HTML,
        )
        return

    # 3) ожидаем текст напоминания
    if mode == "waiting_reminder_text":
        parsed = parse_reminder_text(text)
        if not parsed:
            await update.message.reply_text(
                "Не смог распознать дату/время.\n\n"
                "Примеры:\n"
                "• завтра в 14:00 к врачу\n"
                "• 25.04 16:30 встреча\n"
                "• через 2 часа выключить духовку"
            )
            return

        reminder_text, event_dt = parsed
        if event_dt <= datetime.now():
            await update.message.reply_text("Дата уже прошла. Напиши будущее время.")
            return

        context.user_data["pending_reminder_text"] = reminder_text
        context.user_data["pending_reminder_dt"] = event_dt.isoformat()
        context.user_data["mode"] = None

        await update.message.reply_text(
            f"⏰ <b>Распознал напоминание</b>\n\n"
            f"<b>Событие:</b> {escape(reminder_text)}\n"
            f"<b>Когда:</b> {escape(format_dt(event_dt))}\n\n"
            f"Выбери, за сколько напомнить:",
            reply_markup=reminder_offsets_menu(),
            parse_mode=ParseMode.HTML,
        )
        return

    if looks_like_greeting(text):
        await update.message.reply_text(
            "Привет! Напиши задачу или выбери действие:",
            reply_markup=main_menu(),
        )
        return

    # 4) свободный “умный” ввод
    parsed = parse_reminder_text(text)
    if parsed:
        reminder_text, event_dt = parsed
        if event_dt <= datetime.now():
            await update.message.reply_text("Похоже на напоминание, но дата уже прошла.")
            return

        context.user_data["pending_reminder_text"] = reminder_text
        context.user_data["pending_reminder_dt"] = event_dt.isoformat()

        await update.message.reply_text(
            f"⏰ <b>Похоже, это напоминание</b>\n\n"
            f"<b>Событие:</b> {escape(reminder_text)}\n"
            f"<b>Когда:</b> {escape(format_dt(event_dt))}\n\n"
            f"Выбери, за сколько напомнить:",
            reply_markup=reminder_offsets_menu(),
            parse_mode=ParseMode.HTML,
        )
        return

    # если похоже на напоминание, но не распарсилось
    if looks_like_reminder(text):
        await update.message.reply_text(
            "Похоже, ты хотела создать напоминание, но я не смог точно понять дату.\n\n"
            "Попробуй один из форматов:\n"
            "• завтра в 14:00 к врачу\n"
            "• 25.04 16:30 встреча\n"
            "• через 2 часа выключить духовку"
        )
        return

    # иначе считаем задачей
    context.user_data["pending_task_text"] = text
    await update.message.reply_text(
        f"📋 <b>Похоже, это задача</b>\n\n"
        f"<b>Текст:</b> {escape(text)}\n\n"
        f"Выбери группу:",
        reply_markup=groups_menu(user.id, "task_group"),
        parse_mode=ParseMode.HTML,
    )


# -------------------- ЗАПУСК --------------------

async def post_init(app: Application) -> None:
    schedule_all_pending_reminders(app)
    logger.info("Все будущие напоминания запланированы.")


def main() -> None:
    init_db()

    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("deleteall", deleteall_command))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Бот запущен.")
    app.run_polling()


if __name__ == "__main__":
    main()