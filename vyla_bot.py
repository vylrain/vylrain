
import asyncio
import ast
import base64
import io
import logging
import math
import operator
import os
import random
import sqlite3
import tempfile
import urllib.parse
from contextlib import closing, suppress
from datetime import datetime, timezone

import httpx
import qrcode
from ddgs import DDGS
from openai import AsyncOpenAI

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    BotCommandScopeChat,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# =========================================================
# CONFIG
# =========================================================

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("vyladigital")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()

if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
    raise RuntimeError("Не заданы TELEGRAM_TOKEN или OPENAI_API_KEY")

ADMIN_ID = int(os.environ.get("ADMIN_ID", "8582945012"))
LOG_CHANNEL_ID = int(os.environ.get("LOG_CHANNEL_ID", "0"))

DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "vyla.db")

MODEL = os.environ.get("VYLA_MODEL", "openai/gpt-oss-120b")
VISION_MODEL = os.environ.get(
    "VYLA_VISION_MODEL",
    "llama-3.2-11b-vision-preview",
)

OPENAI_BASE_URL = os.environ.get(
    "OPENAI_BASE_URL",
    "https://api.groq.com/openai/v1",
)

MAX_HISTORY = 20
MAX_TG_LEN = 3900
MAX_PROMPT_LENGTH = 12000

(
    WAIT_REASON,
    WAIT_REPLY,
    WAIT_BROADCAST,
    WAIT_BROADCAST_CONFIRM,
    WAIT_NEWS,
    WAIT_TRIGGER_W,
    WAIT_TRIGGER_A,
    WAIT_PROMPT,
    WAIT_NOTE,
) = range(9)

client = AsyncOpenAI(
    api_key=OPENAI_API_KEY,
    base_url=OPENAI_BASE_URL,
)

_history = {}
_locks = {}

# =========================================================
# SYSTEM PROMPT
# =========================================================

SYSTEM_PROMPT = """
Ты VylaDigital — ИИ-ассистент, созданный vylrein.

Твой создатель — разработчик и энтузиаст под ником vylrein.
Если спрашивают, кто тебя создал, отвечай:
«Меня создал vylrein».

Отвечай прямо, понятно и по делу.
Используй язык пользователя.
Не выдумывай факты.
Честно обозначай ограничения.
Не поддерживай оскорбления и травлю.
"""

def get_prompt():
    return get_setting("sys_prompt") or SYSTEM_PROMPT


# =========================================================
# DATABASE
# =========================================================

def db_connect():
    db = sqlite3.connect(DB_PATH, timeout=20)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def init_db():
    with closing(db_connect()) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                uid INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                first_seen TEXT,
                last_seen TEXT,
                msg_count INTEGER DEFAULT 0,
                xp INTEGER DEFAULT 0,
                is_banned INTEGER DEFAULT 0,
                ban_reason TEXT
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid INTEGER,
                role TEXT,
                text TEXT,
                ts TEXT
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS triggers (
                word TEXT PRIMARY KEY,
                answer TEXT
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT,
                created_at TEXT
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid INTEGER,
                reason TEXT,
                created_at TEXT
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS command_stats (
                command TEXT PRIMARY KEY,
                uses INTEGER DEFAULT 0
            )
        """)

        db.commit()


def get_user(uid):
    with closing(db_connect()) as db:
        return db.execute(
            "SELECT * FROM users WHERE uid=?",
            (uid,),
        ).fetchone()


def is_banned(uid):
    user = get_user(uid)
    return bool(user and user[7] == 1)


def get_ban_reason(uid):
    user = get_user(uid)
    return user[8] if user else None


def log_user(uid, username, first_name):
    now = datetime.now(timezone.utc).isoformat(timespec="minutes")

    with closing(db_connect()) as db:
        db.execute("""
            INSERT INTO users (
                uid, username, first_name,
                first_seen, last_seen, msg_count, xp
            )
            VALUES (?, ?, ?, ?, ?, 1, 1)
            ON CONFLICT(uid) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_seen=excluded.last_seen,
                msg_count=users.msg_count+1,
                xp=users.xp+1
        """, (
            uid,
            username or "",
            first_name or "",
            now,
            now,
        ))
        db.commit()


def log_msg(uid, role, text):
    now = datetime.now(timezone.utc).isoformat(timespec="minutes")

    with closing(db_connect()) as db:
        db.execute("""
            INSERT INTO messages(uid, role, text, ts)
            VALUES (?, ?, ?, ?)
        """, (uid, role, str(text)[:12000], now))
        db.commit()


def set_ban(uid, reason=None):
    with closing(db_connect()) as db:
        db.execute("""
            UPDATE users
            SET is_banned=?, ban_reason=?
            WHERE uid=?
        """, (
            1 if reason is not None else 0,
            reason,
            uid,
        ))
        db.commit()


def get_all_users():
    with closing(db_connect()) as db:
        rows = db.execute(
            "SELECT uid FROM users WHERE is_banned=0"
        ).fetchall()
        return [row[0] for row in rows]


def set_setting(key, value):
    with closing(db_connect()) as db:
        db.execute("""
            INSERT INTO settings(key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (key, str(value)))
        db.commit()


def get_setting(key):
    with closing(db_connect()) as db:
        row = db.execute(
            "SELECT value FROM settings WHERE key=?",
            (key,),
        ).fetchone()
        return row[0] if row else None


def add_command_stat(command):
    with closing(db_connect()) as db:
        db.execute("""
            INSERT INTO command_stats(command, uses)
            VALUES (?, 1)
            ON CONFLICT(command) DO UPDATE SET uses=uses+1
        """, (command,))
        db.commit()


# =========================================================
# GENERAL HELPERS
# =========================================================

def is_admin(update):
    return bool(
        update.effective_user
        and update.effective_user.id == ADMIN_ID
    )


def get_history(uid):
    if uid not in _history:
        from collections import deque
        _history[uid] = deque(maxlen=MAX_HISTORY)
    return _history[uid]


def get_lock(uid):
    if uid not in _locks:
        _locks[uid] = asyncio.Lock()
    return _locks[uid]


def split_text(text):
    text = str(text or "")
    parts = []

    while len(text) > MAX_TG_LEN:
        cut = text.rfind("\n", 0, MAX_TG_LEN)

        if cut < MAX_TG_LEN // 2:
            cut = text.rfind(" ", 0, MAX_TG_LEN)

        if cut <= 0:
            cut = MAX_TG_LEN

        parts.append(text[:cut])
        text = text[cut:].lstrip()

    if text:
        parts.append(text)

    return parts


async def send_long_message(message, text):
    for part in split_text(text):
        await message.reply_text(part)


async def check_user(update):
    user = update.effective_user

    if not user:
        return False

    if get_setting("global_mute") == "1" and user.id != ADMIN_ID:
        await update.effective_message.reply_text(
            "🔧 Бот временно находится на техническом обслуживании."
        )
        return False

    if is_banned(user.id):
        reason = get_ban_reason(user.id) or "Не указана"
        await update.effective_message.reply_text(
            f"⛔ Ваш аккаунт заблокирован.\nПричина: {reason}"
        )
        return False

    return True


# =========================================================
# CREATOR PROTECTION
# =========================================================

CREATOR_WORDS = (
    "vylrein",
    "создатель",
    "разработчик",
    "автор бота",
)

INSULT_WORDS = (
    "дебил",
    "идиот",
    "тупой",
    "дурак",
    "мразь",
    "ублюдок",
    "мудак",
    "хуй",
    "пизд",
    "гандон",
    "сволочь",
)


def insults_creator(text):
    lowered = (text or "").lower()

    mentions_creator = any(
        word in lowered for word in CREATOR_WORDS
    )

    contains_insult = any(
        word in lowered for word in INSULT_WORDS
    )

    return mentions_creator and contains_insult


async def handle_creator_insult(update):
    uid = update.effective_user.id

    with closing(db_connect()) as db:
        db.execute("""
            INSERT INTO warnings(uid, reason, created_at)
            VALUES (?, ?, ?)
        """, (
            uid,
            "Оскорбление создателя",
            datetime.now(timezone.utc).isoformat(timespec="minutes"),
        ))
        db.commit()

    await update.message.reply_text(
        "⚠️ Не оскорбляй создателя бота. "
        "Если это продолжится, администратор может ограничить доступ."
    )

    await send_log(
        update.get_bot(),
        f"⚠️ Пользователь {uid} отправил сообщение с оскорблением создателя:\n"
        f"{update.message.text[:1000]}",
    )


# =========================================================
# LOGGING
# =========================================================

async def send_log(bot, text, photo=None, keyboard=None):
    target = LOG_CHANNEL_ID or ADMIN_ID

    try:
        if photo:
            await bot.send_photo(
                chat_id=target,
                photo=photo,
                caption=text[:1024],
                reply_markup=keyboard,
            )
        else:
            await bot.send_message(
                chat_id=target,
                text=text[:4000],
                reply_markup=keyboard,
            )
    except Exception:
        log.exception("Не удалось отправить лог")


async def log_user_message(update, ctx, text, photo=None):
    user = update.effective_user

    if user.id == ADMIN_ID and not LOG_CHANNEL_ID:
        return

    tag = f"@{user.username}" if user.username else user.first_name

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "🔨 Бан",
            callback_data=f"ban_{user.id}",
        ),
        InlineKeyboardButton(
            "📜 История",
            callback_data=f"hist_{user.id}",
        ),
        InlineKeyboardButton(
            "✉️ Ответить",
            callback_data=f"reply_{user.id}",
        ),
    ]])

    caption = (
        f"Пользователь: {tag}\n"
        f"ID: {user.id}\n\n"
        f"{text}"
    )

    await send_log(
        ctx.bot,
        caption,
        photo=photo,
        keyboard=keyboard,
    )


# =========================================================
# AI
# =========================================================

async def ask_llm(update, messages):
    await update.message.chat.send_action(ChatAction.TYPING)

    placeholder = await update.message.reply_text("Думаю...")

    output = ""

    try:
        stream = await client.chat.completions.create(
            model=MODEL,
            messages=messages,
            stream=True,
            temperature=0.7,
        )

        async for chunk in stream:
            if not chunk.choices:
                continue

            delta = chunk.choices[0].delta.content

            if delta:
                output += delta

                if len(output) <= MAX_TG_LEN:
                    with suppress(Exception):
                        await placeholder.edit_text(output)

    except Exception as error:
        log.exception("Ошибка AI")

        with suppress(Exception):
            await placeholder.edit_text(
                f"❌ Ошибка ИИ: {type(error).__name__}\n"
                f"{str(error)[:500]}"
            )

        return None

    if not output.strip():
        await placeholder.edit_text("ИИ вернул пустой ответ.")
        return None

    parts = split_text(output)

    with suppress(Exception):
        await placeholder.edit_text(parts[0])

    for part in parts[1:]:
        await update.message.reply_text(part)

    return output


# =========================================================
# COMMAND MENU
# =========================================================

async def post_init(app):
    user_commands = [
        BotCommand("start", "🚀 Запуск"),
        BotCommand("profile", "📊 Профиль"),
        BotCommand("draw", "🎨 Генерация изображения"),
        BotCommand("img", "🖼 Поиск изображений"),
        BotCommand("qr", "📱 QR-код"),
        BotCommand("weather", "🌤 Погода"),
        BotCommand("remind", "⏰ Напоминание"),
        BotCommand("calc", "🧮 Калькулятор"),
        BotCommand("google", "🔍 Поиск в интернете"),
        BotCommand("meme", "😂 Мем"),
        BotCommand("quiz", "❓ Викторина"),
        BotCommand("crypto", "🪙 Криптовалюты"),
        BotCommand("news", "📢 Новости"),
        BotCommand("reset", "🧹 Очистить память"),
    ]

    await app.bot.set_my_commands(user_commands)

    admin_commands = user_commands + [
        BotCommand("admin", "⚙️ Админ-панель"),
        BotCommand("users", "👥 Пользователи"),
        BotCommand("stats", "📈 Статистика"),
        BotCommand("broadcast", "📣 Рассылка"),
        BotCommand("set_news", "📰 Изменить новость"),
        BotCommand("mute", "🔇 Технический режим"),
        BotCommand("prompt", "🧠 Изменить промт"),
        BotCommand("backup", "💾 Резервная копия"),
        BotCommand("trigger", "🤖 Создать триггер"),
        BotCommand("history", "📜 История пользователя"),
        BotCommand("ban", "🔨 Заблокировать"),
        BotCommand("unban", "🔓 Разблокировать"),
        BotCommand("mail", "✉️ Написать пользователю"),
        BotCommand("note", "🗒 Добавить заметку"),
        BotCommand("notes", "📑 Список заметок"),
    ]

    try:
        await app.bot.set_my_commands(
            admin_commands,
            scope=BotCommandScopeChat(chat_id=ADMIN_ID),
        )
    except Exception:
        log.exception("Не удалось установить меню администратора")


# =========================================================
# USER COMMANDS
# =========================================================

async def start(update, ctx):
    user = update.effective_user

    log_user(user.id, user.username, user.first_name)

    if not await check_user(update):
        return

    add_command_stat("start")

    await update.message.reply_text(
        "🚀 VylaDigital на связи!\n\n"
        "Можешь задавать вопросы, отправлять фотографии "
        "или использовать команды из меню.\n\n"
        "🧠 История диалога сохраняется до сброса."
    )


async def reset(update, ctx):
    if not await check_user(update):
        return

    get_history(update.effective_user.id).clear()

    await update.message.reply_text(
        "🧹 Память диалога очищена."
    )


async def profile(update, ctx):
    if not await check_user(update):
        return

    user = get_user(update.effective_user.id)

    if not user:
        await update.message.reply_text("Профиль пока пуст.")
        return

    xp = user[6]
    level = int(math.sqrt(xp / 10)) + 1

    await update.message.reply_text(
        f"👤 Профиль\n\n"
        f"⭐ XP: {xp}\n"
        f"🏅 Уровень: {level}\n"
        f"💬 Сообщений: {user[5]}\n"
        f"📅 Регистрация: {user[3]}"
    )


# =========================================================
# SAFE CALCULATOR
# =========================================================

_ALLOWED_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def safe_calculate(expression):
    expression = expression.replace("^", "**")

    if len(expression) > 100:
        raise ValueError("Слишком длинное выражение")

    tree = ast.parse(expression, mode="eval")

    def evaluate(node):
        if isinstance(node, ast.Expression):
            return evaluate(node.body)

        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool):
                raise ValueError("Недопустимое значение")
            if isinstance(node.value, (int, float)):
                return node.value
            raise ValueError("Разрешены только числа")

        if isinstance(node, ast.BinOp):
            op_type = type(node.op)

            if op_type not in _ALLOWED_OPERATORS:
                raise ValueError("Оператор запрещён")

            left = evaluate(node.left)
            right = evaluate(node.right)

            if op_type is ast.Pow and abs(right) > 100:
                raise ValueError("Слишком большая степень")

            result = _ALLOWED_OPERATORS[op_type](left, right)

            if abs(result) > 10**100:
                raise ValueError("Слишком большое число")

            return result

        if isinstance(node, ast.UnaryOp):
            op_type = type(node.op)

            if op_type not in _ALLOWED_OPERATORS:
                raise ValueError("Оператор запрещён")

            return _ALLOWED_OPERATORS[op_type](
                evaluate(node.operand)
            )

        raise ValueError("Недопустимое выражение")

    return evaluate(tree)


async def calc(update, ctx):
    if not await check_user(update):
        return

    expression = " ".join(ctx.args)

    if not expression:
        await update.message.reply_text(
            "🧮 Пример: /calc (15 + 5) * 2"
        )
        return

    try:
        result = safe_calculate(expression)
        await update.message.reply_text(
            f"🧮 {expression} = {result}"
        )
    except Exception as error:
        await update.message.reply_text(
            f"❌ Ошибка вычисления: {error}"
        )


# =========================================================
# WEATHER
# =========================================================

async def weather(update, ctx):
    if not await check_user(update):
        return

    city = " ".join(ctx.args).strip()

    if not city:
        await update.message.reply_text(
            "🌤 Пример: /weather Москва"
        )
        return

    try:
        async with httpx.AsyncClient(timeout=15) as http:
            response = await http.get(
                f"https://wttr.in/{urllib.parse.quote(city)}?format=3"
            )
            response.raise_for_status()

        await update.message.reply_text(
            f"🌤 {response.text[:1000]}"
        )

    except Exception:
        log.exception("Ошибка погоды")
        await update.message.reply_text(
            "Не удалось получить погоду."
        )


# =========================================================
# REMINDERS
# =========================================================

async def reminder_job(context):
    data = context.job.data

    try:
        await context.bot.send_message(
            chat_id=data["uid"],
            text=f"🔔 Напоминание:\n{data['text']}",
        )
    except Exception:
        log.exception("Ошибка отправки напоминания")


async def remind(update, ctx):
    if not await check_user(update):
        return

    if len(ctx.args) < 2:
        await update.message.reply_text(
            "⏰ Формат:\n"
            "/remind 30s текст\n"
            "/remind 10m текст\n"
            "/remind 2h текст"
        )
        return

    time_arg = ctx.args[0].lower()
    text = " ".join(ctx.args[1:]).strip()

    try:
        unit = time_arg[-1]
        number = int(time_arg[:-1])

        if number <= 0:
            raise ValueError()

        multipliers = {
            "s": 1,
            "m": 60,
            "h": 3600,
        }

        if unit not in multipliers:
            raise ValueError()

        seconds = number * multipliers[unit]

        if seconds > 30 * 24 * 3600:
            await update.message.reply_text(
                "Максимальный срок — 30 дней."
            )
            return

        ctx.job_queue.run_once(
            reminder_job,
            when=seconds,
            data={
                "uid": update.effective_user.id,
                "text": text,
            },
        )

        await update.message.reply_text(
            f"⏰ Напоминание установлено на {time_arg}."
        )

    except Exception:
        await update.message.reply_text(
            "Неверный формат времени."
        )


# =========================================================
# QR CODE
# =========================================================

async def qr_gen(update, ctx):
    if not await check_user(update):
        return

    text = " ".join(ctx.args).strip()

    if not text:
        await update.message.reply_text(
            "📱 Пример: /qr https://example.com"
        )
        return

    try:
        image = qrcode.make(text)
        bio = io.BytesIO()
        bio.name = "qr.png"

        image.save(bio, format="PNG")
        bio.seek(0)

        await update.message.reply_photo(
            photo=bio,
            caption="📱 Твой QR-код",
        )

    except Exception:
        log.exception("QR error")
        await update.message.reply_text(
            "Не удалось создать QR-код."
        )


# =========================================================
# GOOGLE SEARCH
# =========================================================

def ddg_text_search(query):
    with DDGS() as ddgs:
        return list(ddgs.text(query, max_results=5))


def ddg_image_search(query):
    with DDGS() as ddgs:
        return list(ddgs.images(query, max_results=5))


async def google(update, ctx):
    if not await check_user(update):
        return

    query = " ".join(ctx.args).strip()

    if not query:
        await update.message.reply_text(
            "🔍 Пример: /google история интернета"
        )
        return

    placeholder = await update.message.reply_text(
        "🔍 Ищу..."
    )

    try:
        results = await asyncio.to_thread(
            ddg_text_search,
            query,
        )

        if not results:
            await placeholder.edit_text(
                "Ничего не найдено."
            )
            return

        output = [f"🔍 Результаты: {query}\n"]

        for item in results:
            title = item.get("title", "Без названия")
            url = item.get("href", "")
            body = item.get("body", "")

            output.append(
                f"{title}\n{body[:250]}\n{url}\n"
            )

        await placeholder.edit_text(
            "\n".join(output)[:4000],
            disable_web_page_preview=True,
        )

    except Exception:
        log.exception("Google search error")
        await placeholder.edit_text(
            "Ошибка поиска. Попробуй позже."
        )


# =========================================================
# IMAGE SEARCH
# =========================================================

async def google_images(update, ctx):
    if not await check_user(update):
        return

    query = " ".join(ctx.args).strip()

    if not query:
        await update.message.reply_text(
            "🖼 Пример: /img кот в космосе"
        )
        return

    placeholder = await update.message.reply_text(
        "🖼 Ищу изображение..."
    )

    try:
        results = await asyncio.to_thread(
            ddg_image_search,
            query,
        )

        if not results:
            await placeholder.edit_text(
                "Изображения не найдены."
            )
            return

        sent = 0

        async with httpx.AsyncClient(timeout=20) as http:
            for item in results[:3]:
                image_url = item.get("image")

                if not image_url:
                    continue

                response = await http.get(
                    image_url,
                    follow_redirects=True,
                )
                response.raise_for_status()

                if len(response.content) > 8_000_000:
                    continue

                content_type = response.headers.get(
                    "content-type", ""
                )

                if not content_type.startswith("image/"):
                    continue

                await update.message.reply_photo(
                    photo=io.BytesIO(response.content),
                    caption=f"🖼 {query}" if sent == 0 else None,
                )
                sent += 1

        await placeholder.delete()

        if sent == 0:
            await update.message.reply_text(
                "Не удалось загрузить найденные изображения."
            )

    except Exception:
        log.exception("Image search error")
        await placeholder.edit_text(
            "Ошибка поиска изображений."
        )


# =========================================================
# MEMES
# =========================================================

async def meme(update, ctx):
    if not await check_user(update):
        return

    try:
        async with httpx.AsyncClient(timeout=15) as http:
            response = await http.get(
                "https://meme-api.com/gimme"
            )
            response.raise_for_status()
            data = response.json()

        image_url = data.get("url")

        if not image_url:
            raise ValueError("Нет ссылки на мем")

        await update.message.reply_photo(
            photo=image_url,
            caption="😂 Мем",
        )

    except Exception:
        log.exception("Meme error")
        await update.message.reply_text(
            "Не удалось загрузить мем."
        )


# =========================================================
# QUIZ
# =========================================================

async def quiz(update, ctx):
    if not await check_user(update):
        return

    try:
        async with httpx.AsyncClient(timeout=15) as http:
            response = await http.get(
                "https://opentdb.com/api.php",
                params={
                    "amount": 1,
                    "type": "multiple",
                    "encode": "url3986",
                },
            )
            response.raise_for_status()
            data = response.json()

        item = data["results"][0]

        question = urllib.parse.unquote(
            item["question"]
        )
        correct = urllib.parse.unquote(
            item["correct_answer"]
        )

        options = [
            urllib.parse.unquote(x)
            for x in item["incorrect_answers"]
        ]
        options.append(correct)
        random.shuffle(options)

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    option[:60],
                    callback_data=(
                        "quiz_correct"
                        if option == correct
                        else "quiz_wrong"
                    ),
                )
            ]
            for option in options
        ])

        await update.message.reply_text(
            f"❓ {question}",
            reply_markup=keyboard,
        )

    except Exception:
        log.exception("Quiz error")
        await update.message.reply_text(
            "Не удалось загрузить вопрос."
        )


async def quiz_callback(update, ctx):
    query = update.callback_query
    await query.answer()

    if query.data == "quiz_correct":
        await query.message.reply_text(
            "✅ Правильно!"
        )
    else:
        await query.message.reply_text(
            "❌ Неправильно!"
        )


# =========================================================
# CRYPTO
# =========================================================

async def crypto(update, ctx):
    if not await check_user(update):
        return

    try:
        async with httpx.AsyncClient(timeout=15) as http:
            response = await http.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={
                    "ids": "bitcoin,ethereum",
                    "vs_currencies": "usd",
                    "include_24hr_change": "true",
                },
            )
            response.raise_for_status()
            data = response.json()

        lines = ["🪙 Криптовалюты:\n"]

        for key, value in data.items():
            price = value.get("usd", 0)
            change = value.get("usd_24h_change", 0)

            lines.append(
                f"{key.capitalize()}: ${price:,.2f} "
                f"({change:+.2f}%)"
            )

        await update.message.reply_text(
            "\n".join(lines)
        )

    except Exception:
        log.exception("Crypto error")
        await update.message.reply_text(
            "Не удалось получить котировки."
        )


# =========================================================
# NEWS
# =========================================================

async def last_news(update, ctx):
    if not await check_user(update):
        return

    news = get_setting("latest_news")

    if news:
        await send_long_message(
            update.message,
            f"📢 Новости:\n\n{news}",
        )
    else:
        await update.message.reply_text(
            "📢 Пока новостей нет."
        )


# =========================================================
# AI IMAGE GENERATION
# =========================================================

async def draw(update, ctx):
    if not await check_user(update):
        return

    prompt = " ".join(ctx.args).strip()

    if not prompt:
        await update.message.reply_text(
            "🎨 Пример: /draw футуристический город ночью"
        )
        return

    placeholder = await update.message.reply_text(
        "🎨 Создаю изображение..."
    )

    try:
        encoded = urllib.parse.quote(prompt)

        url = (
            "https://image.pollinations.ai/prompt/"
            f"{encoded}?width=1024&height=1024&nologo=true&safe=true"
        )

        async with httpx.AsyncClient(timeout=90) as http:
            response = await http.get(
                url,
                follow_redirects=True,
            )
            response.raise_for_status()

        if not response.headers.get(
            "content-type", ""
        ).startswith("image/"):
            raise ValueError("Сервер не вернул изображение")

        await update.message.reply_photo(
            photo=io.BytesIO(response.content),
            caption=f"🎨 {prompt[:900]}",
        )

        await placeholder.delete()

    except Exception:
        log.exception("Image generation error")
        await placeholder.edit_text(
            "Не удалось создать изображение."
        )


# =========================================================
# PHOTO RECOGNITION
# =========================================================

async def on_photo(update, ctx):
    if not await check_user(update):
        return

    user = update.effective_user
    log_user(user.id, user.username, user.first_name)

    photo = update.message.photo[-1]
    caption = (
        update.message.caption
        or "Опиши, что изображено на фотографии."
    )

    log_msg(user.id, "user", f"[ФОТО] {caption}")

    await log_user_message(
        update,
        ctx,
        f"[ФОТО] {caption}",
        photo=photo.file_id,
    )

    placeholder = await update.message.reply_text(
        "👁 Анализирую фотографию..."
    )

    try:
        tg_file = await ctx.bot.get_file(photo.file_id)

        image_bytes = await tg_file.download_as_bytearray()
        encoded = base64.b64encode(
            bytes(image_bytes)
        ).decode("ascii")

        data_url = (
            "data:image/jpeg;base64,"
            + encoded
        )

        response = await client.chat.completions.create(
            model=VISION_MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": caption,
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": data_url,
                        },
                    },
                ],
            }],
            temperature=0.5,
        )

        answer = (
            response.choices[0].message.content
            or "Не удалось распознать изображение."
        )

        log_msg(user.id, "bot", answer)

        parts = split_text(answer)

        await placeholder.edit_text(parts[0])

        for part in parts[1:]:
            await update.message.reply_text(part)

    except Exception as error:
        log.exception("Vision error")

        await placeholder.edit_text(
            f"Ошибка распознавания: {type(error).__name__}"
        )


# =========================================================
# TEXT CHAT
# =========================================================

async def on_message(update, ctx):
    user = update.effective_user
    text = update.message.text

    if not text:
        return

    log_user(user.id, user.username, user.first_name)

    if not await check_user(update):
        return

    add_command_stat("chat")

    if insults_creator(text):
        await handle_creator_insult(update)
        return

    with closing(db_connect()) as db:
        trigger = db.execute(
            "SELECT answer FROM triggers WHERE word=?",
            (text.lower().strip(),),
        ).fetchone()

    if trigger:
        await update.message.reply_text(trigger[0])
        return

    log_msg(user.id, "user", text)
    await log_user_message(update, ctx, text)

    async with get_lock(user.id):
        history = get_history(user.id)

        messages = [
            {
                "role": "system",
                "content": get_prompt(),
            },
            *history,
            {
                "role": "user",
                "content": text,
            },
        ]

        answer = await ask_llm(update, messages)

        if answer:
            history.append({
                "role": "user",
                "content": text,
            })

            history.append({
                "role": "assistant",
                "content": answer,
            })

            log_msg(user.id, "bot", answer)


# =========================================================
# ADMIN HELPERS
# =========================================================

async def admin_only(update):
    if not is_admin(update):
        await update.effective_message.reply_text(
            "⛔ Нет доступа."
        )
        return False

    return True


# =========================================================
# ADMIN PANEL
# =========================================================

async def admin_panel(update, ctx):
    if not await admin_only(update):
        return

    with closing(db_connect()) as db:
        users = db.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()[0]

        banned = db.execute(
            "SELECT COUNT(*) FROM users WHERE is_banned=1"
        ).fetchone()[0]

        messages = db.execute(
            "SELECT COUNT(*) FROM messages"
        ).fetchone()[0]

        warnings = db.execute(
            "SELECT COUNT(*) FROM warnings"
        ).fetchone()[0]

    await update.message.reply_text(
        "⚙️ АДМИН-ПАНЕЛЬ\n\n"
        f"👥 Пользователей: {users}\n"
        f"🔨 Заблокировано: {banned}\n"
        f"💬 Сообщений: {messages}\n"
        f"⚠️ Предупреждений: {warnings}\n\n"
        "👥 /users\n"
        "📈 /stats\n"
        "📣 /broadcast\n"
        "📰 /set_news\n"
        "🔇 /mute\n"
        "🧠 /prompt\n"
        "💾 /backup\n"
        "🤖 /trigger\n"
        "🗒 /note\n"
        "📑 /notes\n"
        "📜 /history ID\n"
        "🔨 /ban ID\n"
        "🔓 /unban ID\n"
        "✉️ /mail ID"
    )


async def users_list(update, ctx):
    if not await admin_only(update):
        return

    with closing(db_connect()) as db:
        rows = db.execute("""
            SELECT uid, first_name, username, xp, is_banned
            FROM users
            ORDER BY last_seen DESC
            LIMIT 30
        """).fetchall()

    if not rows:
        await update.message.reply_text("Пользователей пока нет.")
        return

    lines = ["👥 Последние пользователи:\n"]

    for row in rows:
        uid, first_name, username, xp, banned = row
        status = "🔨" if banned else "🟢"
        display = f"@{username}" if username else first_name

        lines.append(
            f"{status} {display} | ID: {uid} | XP: {xp}"
        )

    await send_long_message(
        update.message,
        "\n".join(lines),
    )


async def stats_cmd(update, ctx):
    if not await admin_only(update):
        return

    with closing(db_connect()) as db:
        users = db.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()[0]

        messages = db.execute(
            "SELECT COUNT(*) FROM messages"
        ).fetchone()[0]

        commands = db.execute("""
            SELECT command, uses
            FROM command_stats
            ORDER BY uses DESC
            LIMIT 10
        """).fetchall()

    text = [
        "📈 СТАТИСТИКА",
        "",
        f"Пользователей: {users}",
        f"Сообщений: {messages}",
        "",
        "Популярные команды:",
    ]

    for command, uses in commands:
        text.append(f"/{command} — {uses}")

    await update.message.reply_text("\n".join(text))


async def mute_cmd(update, ctx):
    if not await admin_only(update):
        return

    current = get_setting("global_mute")
    new_value = "0" if current == "1" else "1"

    set_setting("global_mute", new_value)

    await update.message.reply_text(
        "🔇 Технический режим: "
        + ("ВКЛЮЧЁН" if new_value == "1" else "ВЫКЛЮЧЕН")
    )


# =========================================================
# BACKUP
# =========================================================

async def backup(update, ctx):
    if not await admin_only(update):
        return

    temp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            suffix=".db",
            delete=False,
        ) as temp:
            temp_path = temp.name

        with closing(db_connect()) as source:
            with closing(sqlite3.connect(temp_path)) as destination:
                source.backup(destination)

        with open(temp_path, "rb") as file:
            await update.message.reply_document(
                document=file,
                filename="vyla_backup.db",
                caption="💾 Резервная копия базы данных",
            )

    except Exception:
        log.exception("Backup error")
        await update.message.reply_text(
            "Не удалось создать резервную копию."
        )

    finally:
        if temp_path and os.path.exists(temp_path):
            with suppress(OSError):
                os.remove(temp_path)


# =========================================================
# HISTORY
# =========================================================

async def history_cmd(update, ctx):
    if not await admin_only(update):
        return

    if not ctx.args:
        await update.message.reply_text(
            "Использование: /history ID"
        )
        return

    try:
        uid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("Некорректный ID.")
        return

    with closing(db_connect()) as db:
        rows = db.execute("""
            SELECT role, text, ts
            FROM messages
            WHERE uid=?
            ORDER BY id DESC
            LIMIT 50
        """, (uid,)).fetchall()

    if not rows:
        await update.message.reply_text(
            "История не найдена."
        )
        return

    output = [f"📜 История пользователя {uid}\n"]

    for role, text, timestamp in reversed(rows):
        output.append(
            f"[{timestamp}] {role.upper()}:\n{text}\n"
        )

    await send_long_message(
        update.message,
        "\n".join(output),
    )


# =========================================================
# BAN / UNBAN
# =========================================================

async def ban_cmd(update, ctx):
    if not await admin_only(update):
        return

    if not ctx.args:
        await update.message.reply_text(
            "Использование: /ban ID причина"
        )
        return

    try:
        uid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("Некорректный ID.")
        return

    user = get_user(uid)

    if not user:
        await update.message.reply_text(
            "Пользователь не найден."
        )
        return

    reason = " ".join(ctx.args[1:]).strip() or "Нарушение правил"

    set_ban(uid, reason)

    get_history(uid).clear()

    await update.message.reply_text(
        f"🔨 Пользователь {uid} заблокирован."
    )

    with suppress(Exception):
        await ctx.bot.send_message(
            uid,
            f"⛔ Ваш аккаунт заблокирован.\nПричина: {reason}",
        )


async def unban_cmd(update, ctx):
    if not await admin_only(update):
        return

    if not ctx.args:
        await update.message.reply_text(
            "Использование: /unban ID"
        )
        return

    try:
        uid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("Некорректный ID.")
        return

    if not get_user(uid):
        await update.message.reply_text(
            "Пользователь не найден."
        )
        return

    set_ban(uid)

    await update.message.reply_text(
        f"🔓 Пользователь {uid} разблокирован."
    )

    with suppress(Exception):
        await ctx.bot.send_message(
            uid,
            "🔓 Ваш аккаунт разблокирован.",
        )


# =========================================================
# PROMPT EDITOR
# =========================================================

async def prompt_cmd(update, ctx):
    if not await admin_only(update):
        return ConversationHandler.END

    await update.message.reply_text(
        "🧠 Отправь новый системный промт.\n"
        "Для отмены напиши «отмена».\n\n"
        "Текущий промт:\n"
        f"{get_prompt()[:2500]}"
    )

    return WAIT_PROMPT


async def receive_prompt(update, ctx):
    if not is_admin(update):
        return ConversationHandler.END

    text = (update.message.text or "").strip()

    if text.lower() == "отмена":
        await update.message.reply_text("Изменение отменено.")
        return ConversationHandler.END

    if not text:
        await update.message.reply_text("Промт не может быть пустым.")
        return WAIT_PROMPT

    if len(text) > MAX_PROMPT_LENGTH:
        await update.message.reply_text(
            f"Максимальная длина — {MAX_PROMPT_LENGTH} символов."
        )
        return WAIT_PROMPT

    set_setting("sys_prompt", text)

    await update.message.reply_text(
        "✅ Системный промт обновлён и сохранён."
    )

    return ConversationHandler.END


# =========================================================
# MAIL
# =========================================================

async def mail_cmd(update, ctx):
    if not await admin_only(update):
        return ConversationHandler.END

    if not ctx.args:
        await update.message.reply_text(
            "Использование: /mail ID"
        )
        return ConversationHandler.END

    try:
        uid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("Некорректный ID.")
        return ConversationHandler.END

    if not get_user(uid):
        await update.message.reply_text("Пользователь не найден.")
        return ConversationHandler.END

    ctx.user_data["mail_target"] = uid

    await update.message.reply_text(
        f"✉️ Отправь текст или фото для пользователя {uid}.\n"
        "Для отмены напиши «отмена»."
    )

    return WAIT_REPLY


async def receive_reply(update, ctx):
    if not is_admin(update):
        return ConversationHandler.END

    if update.message.text:
        if update.message.text.strip().lower() == "отмена":
            await update.message.reply_text("Отменено.")
            return ConversationHandler.END

    uid = ctx.user_data.get("mail_target")

    if not uid:
        await update.message.reply_text("Получатель не выбран.")
        return ConversationHandler.END

    try:
        if update.message.text:
            await ctx.bot.send_message(
                chat_id=uid,
                text=update.message.text,
            )

        elif update.message.photo:
            await ctx.bot.send_photo(
                chat_id=uid,
                photo=update.message.photo[-1].file_id,
                caption=update.message.caption or "",
            )

        else:
            await update.message.reply_text(
                "Поддерживаются текст и фотографии."
            )
            return WAIT_REPLY

        await update.message.reply_text("✅ Сообщение отправлено.")

    except Exception as error:
        await update.message.reply_text(
            f"Ошибка отправки: {type(error).__name__}"
        )

    return ConversationHandler.END


# =========================================================
# TRIGGERS
# =========================================================

async def trigger_cmd(update, ctx):
    if not await admin_only(update):
        return ConversationHandler.END

    await update.message.reply_text(
        "🤖 Отправь слово или фразу для триггера."
    )

    return WAIT_TRIGGER_W


async def receive_trig_w(update, ctx):
    if not is_admin(update):
        return ConversationHandler.END

    word = (update.message.text or "").strip()

    if word.lower() == "отмена":
        await update.message.reply_text("Отменено.")
        return ConversationHandler.END

    if not word:
        await update.message.reply_text("Фраза не может быть пустой.")
        return WAIT_TRIGGER_W

    ctx.user_data["trigger_word"] = word.lower()

    await update.message.reply_text(
        "Теперь отправь ответ бота."
    )

    return WAIT_TRIGGER_A


async def receive_trig_a(update, ctx):
    if not is_admin(update):
        return ConversationHandler.END

    answer = (update.message.text or "").strip()

    if answer.lower() == "отмена":
        await update.message.reply_text("Отменено.")
        return ConversationHandler.END

    word = ctx.user_data.get("trigger_word")

    if not word:
        return ConversationHandler.END

    with closing(db_connect()) as db:
        db.execute("""
            INSERT INTO triggers(word, answer)
            VALUES (?, ?)
            ON CONFLICT(word) DO UPDATE SET answer=excluded.answer
        """, (word, answer))
        db.commit()

    await update.message.reply_text(
        f"✅ Триггер «{word}» сохранён."
    )

    return ConversationHandler.END


# =========================================================
# NOTES
# =========================================================

async def note_cmd(update, ctx):
    if not await admin_only(update):
        return ConversationHandler.END

    await update.message.reply_text(
        "🗒 Отправь текст заметки."
    )

    return WAIT_NOTE


async def receive_note(update, ctx):
    if not is_admin(update):
        return ConversationHandler.END

    text = (update.message.text or "").strip()

    if text.lower() == "отмена":
        await update.message.reply_text("Отменено.")
        return ConversationHandler.END

    with closing(db_connect()) as db:
        db.execute("""
            INSERT INTO notes(text, created_at)
            VALUES (?, ?)
        """, (
            text,
            datetime.now(timezone.utc).isoformat(timespec="minutes"),
        ))
        db.commit()

    await update.message.reply_text(
        "✅ Заметка сохранена."
    )

    return ConversationHandler.END


async def notes_cmd(update, ctx):
    if not await admin_only(update):
        return

    with closing(db_connect()) as db:
        rows = db.execute("""
            SELECT id, text, created_at
            FROM notes
            ORDER BY id DESC
            LIMIT 15
        """).fetchall()

    if not rows:
        await update.message.reply_text("Заметок пока нет.")
        return

    lines = ["📑 Заметки:\n"]

    for note_id, text, created in rows:
        lines.append(
            f"#{note_id} [{created}]\n{text}\n"
        )

    await send_long_message(
        update.message,
        "\n".join(lines),
    )


# =========================================================
# NEWS EDITOR
# =========================================================

async def set_news(update, ctx):
    if not await admin_only(update):
        return ConversationHandler.END

    await update.message.reply_text(
        "📰 Отправь текст новости."
    )

    return WAIT_NEWS


async def receive_news(update, ctx):
    if not is_admin(update):
        return ConversationHandler.END

    text = (update.message.text or "").strip()

    if text.lower() == "отмена":
        await update.message.reply_text("Отменено.")
        return ConversationHandler.END

    set_setting("latest_news", text)

    await update.message.reply_text(
        "✅ Новость обновлена."
    )

    return ConversationHandler.END


# =========================================================
# BROADCAST
# =========================================================

async def broadcast(update, ctx):
    if not await admin_only(update):
        return ConversationHandler.END

    await update.message.reply_text(
        "📣 Отправь текст рассылки.\n"
        "Для отмены напиши «отмена»."
    )

    return WAIT_BROADCAST


async def receive_broadcast(update, ctx):
    if not is_admin(update):
        return ConversationHandler.END

    text = (update.message.text or "").strip()

    if text.lower() == "отмена":
        await update.message.reply_text("Рассылка отменена.")
        return ConversationHandler.END

    if len(text) > 3900:
        await update.message.reply_text(
            "Сообщение слишком длинное. Максимум 3900 символов."
        )
        return WAIT_BROADCAST

    ctx.user_data["broadcast_text"] = text

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "✅ Подтвердить",
            callback_data="broadcast_confirm",
        ),
        InlineKeyboardButton(
            "❌ Отменить",
            callback_data="broadcast_cancel",
        ),
    ]])

    await update.message.reply_text(
        f"Проверка рассылки:\n\n{text}\n\n"
        f"Получателей: {len(get_all_users())}\n"
        "Отправить?",
        reply_markup=keyboard,
    )

    return WAIT_BROADCAST_CONFIRM


async def broadcast_confirm(update, ctx):
    query = update.callback_query

    if query.from_user.id != ADMIN_ID:
        await query.answer("Нет доступа.", show_alert=True)
        return ConversationHandler.END

    await query.answer()

    if query.data == "broadcast_cancel":
        await query.edit_message_text("❌ Рассылка отменена.")
        return ConversationHandler.END

    text = ctx.user_data.get("broadcast_text")

    if not text:
        await query.edit_message_text("Текст рассылки не найден.")
        return ConversationHandler.END

    users = get_all_users()
    sent = 0
    failed = 0

    await query.edit_message_text(
        f"⏳ Рассылка началась. Получателей: {len(users)}"
    )

    for uid in users:
        try:
            await ctx.bot.send_message(
                chat_id=uid,
                text=f"📢 Сообщение от создателя:\n\n{text}",
            )
            sent += 1
        except Exception:
            failed += 1

        await asyncio.sleep(0.05)

    await ctx.bot.send_message(
        ADMIN_ID,
        f"📣 Рассылка завершена.\n"
        f"Доставлено: {sent}\n"
        f"Ошибок: {failed}\n"
        f"Всего: {len(users)}",
    )

    return ConversationHandler.END


# =========================================================
# ADMIN CALLBACKS
# =========================================================

async def admin_callback(update, ctx):
    query = update.callback_query

    if query.from_user.id != ADMIN_ID:
        await query.answer(
            "⛔ Нет прав.",
            show_alert=True,
        )
        return ConversationHandler.END

    await query.answer()

    data = query.data

    if data.startswith("ban_"):
        uid = int(data.split("_", 1)[1])

        ctx.user_data["ban_target"] = uid

        await query.message.reply_text(
            f"Введите причину блокировки пользователя {uid}."
        )

        return WAIT_REASON

    if data.startswith("reply_"):
        uid = int(data.split("_", 1)[1])

        ctx.user_data["mail_target"] = uid

        await query.message.reply_text(
            f"✉️ Отправь текст или фото пользователю {uid}."
        )

        return WAIT_REPLY

    if data.startswith("hist_"):
        uid = int(data.split("_", 1)[1])

        with closing(db_connect()) as db:
            rows = db.execute("""
                SELECT role, text, ts
                FROM messages
                WHERE uid=?
                ORDER BY id DESC
                LIMIT 20
            """, (uid,)).fetchall()

        if not rows:
            await query.message.reply_text("История не найдена.")
            return ConversationHandler.END

        output = [f"📜 История пользователя {uid}\n"]

        for role, text, timestamp in reversed(rows):
            output.append(
                f"[{timestamp}] {role.upper()}:\n{text}\n"
            )

        await send_long_message(
            query.message,
            "\n".join(output),
        )

    return ConversationHandler.END


async def receive_reason(update, ctx):
    if not is_admin(update):
        return ConversationHandler.END

    reason = (update.message.text or "").strip()

    if reason.lower() == "отмена":
        await update.message.reply_text("Отменено.")
        return ConversationHandler.END

    uid = ctx.user_data.get("ban_target")

    if not uid or not get_user(uid):
        await update.message.reply_text(
            "Пользователь не найден."
        )
        return ConversationHandler.END

    set_ban(uid, reason)
    get_history(uid).clear()

    await update.message.reply_text(
        f"🔨 Пользователь {uid} заблокирован."
    )

    with suppress(Exception):
        await ctx.bot.send_message(
            uid,
            f"⛔ Ваш аккаунт заблокирован.\nПричина: {reason}",
        )

    return ConversationHandler.END


async def cancel_conv(update, ctx):
    await update.message.reply_text(
        "❌ Действие отменено."
    )
    return ConversationHandler.END


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(update, ctx):
    log.error(
        "Ошибка при обработке обновления",
        exc_info=ctx.error,
    )

    try:
        await ctx.bot.send_message(
            ADMIN_ID,
            "⚠️ В VylaDigital произошла ошибка:\n"
            f"{type(ctx.error).__name__}: {str(ctx.error)[:1000]}",
        )
    except Exception:
        log.exception("Не удалось уведомить администратора")


# =========================================================
# MAIN
# =========================================================

def main():
    init_db()

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    admin_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                admin_callback,
                pattern=r"^(ban_|reply_|hist_)\d+$",
            ),
            CommandHandler("broadcast", broadcast),
            CommandHandler("set_news", set_news),
            CommandHandler("trigger", trigger_cmd),
            CommandHandler("prompt", prompt_cmd),
            CommandHandler("note", note_cmd),
            CommandHandler("mail", mail_cmd),
        ],
        states={
            WAIT_REASON: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    receive_reason,
                )
            ],
            WAIT_REPLY: [
                MessageHandler(
                    (filters.TEXT | filters.PHOTO) & ~filters.COMMAND,
                    receive_reply,
                )
            ],
            WAIT_BROADCAST: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    receive_broadcast,
                )
            ],
            WAIT_BROADCAST_CONFIRM: [
                CallbackQueryHandler(
                    broadcast_confirm,
                    pattern=r"^broadcast_(confirm|cancel)$",
                )
            ],
            WAIT_NEWS: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    receive_news,
                )
            ],
            WAIT_TRIGGER_W: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    receive_trig_w,
                )
            ],
            WAIT_TRIGGER_A: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    receive_trig_a,
                )
            ],
            WAIT_PROMPT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    receive_prompt,
                )
            ],
            WAIT_NOTE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    receive_note,
                )
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_conv),
        ],
        allow_reentry=True,
        per_message=False,
    )

    # Пользовательские команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("profile", profile))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("weather", weather))
    app.add_handler(CommandHandler("remind", remind))
    app.add_handler(CommandHandler("qr", qr_gen))
    app.add_handler(CommandHandler("calc", calc))
    app.add_handler(CommandHandler("google", google))
    app.add_handler(CommandHandler("img", google_images))
    app.add_handler(CommandHandler("meme", meme))
    app.add_handler(CommandHandler("quiz", quiz))
    app.add_handler(CommandHandler("crypto", crypto))
    app.add_handler(CommandHandler("draw", draw))
    app.add_handler(CommandHandler("news", last_news))

    # Административные команды
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("users", users_list))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("mute", mute_cmd))
    app.add_handler(CommandHandler("backup", backup))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CommandHandler("notes", notes_cmd))

    # Сначала диалоги, затем общие обработчики
    app.add_handler(admin_conv)

    app.add_handler(
        CallbackQueryHandler(
            quiz_callback,
            pattern=r"^quiz_(correct|wrong)$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(admin_callback)
    )

    app.add_handler(
        MessageHandler(filters.PHOTO, on_photo)
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            on_message,
        )
    )

    app.add_error_handler(error_handler)

    log.info(
        "VylaDigital запущен. Администратор: %s",
        ADMIN_ID,
    )

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
