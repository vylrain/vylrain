asyncio
import logging
import os
import urllib.parse
import sqlite3
import random
import string
import yt_dlp
import httpx

from collections import defaultdict, deque
from contextlib import suppress, closing
from datetime import datetime, timezone

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand, BotCommandScopeChat,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ConversationHandler, ContextTypes,
)
from openai import AsyncOpenAI

# =========================
# CONFIGURATION
# =========================

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("vyladigital")

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

ADMIN_ID = int(os.environ.get("ADMIN_ID", "8582945012"))
LOG_CHANNEL_ID = int(os.environ.get("LOG_CHANNEL_ID", "0"))

DB_PATH = "vyla.db"
MODEL = os.environ.get("VYLA_MODEL", "openai/gpt-oss-120b")
VISION_MODEL = os.environ.get("VYLA_VISION_MODEL", "llama-3.2-11b-vision-preview")

MAX_HISTORY = 20
MAX_TG_LEN = 4000

# Состояния диалогов
WAIT_REASON = 1
WAIT_REPLY = 2
WAIT_BROADCAST = 3
WAIT_NEWS = 4

client = AsyncOpenAI(
    api_key=OPENAI_API_KEY,
    base_url=os.environ.get("OPENAI_BASE_URL", "https://api.groq.com/openai/v1"),
)

_history = defaultdict(lambda: deque(maxlen=MAX_HISTORY))
_locks = defaultdict(asyncio.Lock)

SYSTEM_PROMPT = (
    "Ты VylaDigital — ИИ-ассистент, созданный vylrein.\n"
    "Твой создатель — школьник и энтузиаст, "
    "самостоятельно занимающийся электроникой и программированием.\n"
    "Если спрашивают о создателе, отвечай: "
    "'Меня создал vylrein — школьник и энтузиаст, "
    "занимающийся электроникой и программированием.'\n"
    "Отвечай прямо, понятно и по делу."
)

# =========================
# DATABASE
# =========================

def init_db():
    with closing(sqlite3.connect(DB_PATH)) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                uid INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
                first_seen TEXT, last_seen TEXT, msg_count INTEGER DEFAULT 0,
                is_banned INTEGER DEFAULT 0, ban_reason TEXT
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER,
                role TEXT, text TEXT, ts TEXT
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS todos (
                id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER,
                text TEXT, done INTEGER DEFAULT 0, created_at TEXT
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT
            )
        """)
        db.commit()

def get_user(uid):
    with closing(sqlite3.connect(DB_PATH)) as db:
        return db.execute("SELECT * FROM users WHERE uid=?", (uid,)).fetchone()

def is_banned(uid):
    user = get_user(uid)
    return bool(user and user[6] == 1)

def get_ban_reason(uid):
    user = get_user(uid)
    return user[7] if user else None

def log_user(uid, username, first_name):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    with closing(sqlite3.connect(DB_PATH)) as db:
        row = db.execute("SELECT uid FROM users WHERE uid=?", (uid,)).fetchone()
        if row:
            db.execute("UPDATE users SET last_seen=?, msg_count=msg_count+1, username=?, first_name=? WHERE uid=?",
                       (now, username or "", first_name or "", uid))
        else:
            db.execute("INSERT INTO users (uid, username, first_name, first_seen, last_seen, msg_count) VALUES (?, ?, ?, ?, ?, 1)",
                       (uid, username or "", first_name or "", now, now))
        db.commit()

def log_msg(uid, role, text):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    with closing(sqlite3.connect(DB_PATH)) as db:
        db.execute("INSERT INTO messages(uid, role, text, ts) VALUES (?, ?, ?, ?)", (uid, role, text, now))
        db.commit()

def set_ban(uid, reason=None):
    with closing(sqlite3.connect(DB_PATH)) as db:
        if reason is not None:
            db.execute("UPDATE users SET is_banned=1, ban_reason=? WHERE uid=?", (reason, uid))
        else:
            db.execute("UPDATE users SET is_banned=0, ban_reason=NULL WHERE uid=?", (uid,))
        db.commit()

def get_all_users():
    with closing(sqlite3.connect(DB_PATH)) as db:
        return [row[0] for row in db.execute("SELECT uid FROM users WHERE is_banned=0").fetchall()]

def set_setting(key, value):
    with closing(sqlite3.connect(DB_PATH)) as db:
        db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        db.commit()

def get_setting(key):
    with closing(sqlite3.connect(DB_PATH)) as db:
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

# =========================
# UTILITIES
# =========================

def split_text(text):
    parts, buf = [], text
    while len(buf) > MAX_TG_LEN:
        cut = buf.rfind("\n", 0, MAX_TG_LEN)
        if cut < MAX_TG_LEN // 2: cut = buf.rfind(" ", 0, MAX_TG_LEN)
        if cut <= 0: cut = MAX_TG_LEN
        parts.append(buf[:cut])
        buf = buf[cut:].lstrip()
    if buf: parts.append(buf)
    return parts

async def send_log(ctx, text=None, kb=None, photo=None):
    target = LOG_CHANNEL_ID if LOG_CHANNEL_ID else ADMIN_ID
    try:
        if photo:
            await ctx.bot.send_photo(chat_id=target, photo=photo, caption=text[:1024] if text else "", reply_markup=kb)
        else:
            await ctx.bot.send_message(chat_id=target, text=text[:4000], reply_markup=kb)
    except Exception:
        log.exception("Logging error")

async def log_user_message(update, ctx, text=None, photo=None):
    user = update.effective_user
    if user.id == ADMIN_ID and not LOG_CHANNEL_ID: return
    tag = f"@{user.username}" if user.username else user.first_name
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Забанить", callback_data=f"ban_{user.id}"),
        InlineKeyboardButton("История", callback_data=f"hist_{user.id}"),
        InlineKeyboardButton("Ответить", callback_data=f"reply_{user.id}"),
    ]])
    if photo:
        caption = f"Пользователь: {tag}\nID: {user.id}\n\n{text}" if text else f"Пользователь: {tag}\nID: {user.id}"
        await send_log(ctx, text=caption, kb=kb, photo=photo)
    else:
        await send_log(ctx, text=f"Пользователь: {tag}\nID: {user.id}\n\n{text}", kb=kb)

async def ask_llm(update, messages):
    await update.message.chat.send_action(ChatAction.TYPING)
    placeholder = await update.message.reply_text("Думаю...")
    buf, last, loop = "", 0.0, asyncio.get_running_loop()
    try:
        stream = await client.chat.completions.create(model=MODEL, messages=messages, stream=True, temperature=0.7)
        async for chunk in stream:
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if not delta: continue
            buf += delta
            if loop.time() - last > 1.2 and len(buf) <= MAX_TG_LEN:
                last = loop.time()
                with suppress(Exception): await placeholder.edit_text(buf)
    except Exception as e:
        log.exception("LLM error")
        with suppress(Exception): await placeholder.edit_text(f"Ошибка ИИ: {type(e).__name__}")
        return None
    if not buf.strip():
        await placeholder.edit_text("ИИ вернул пустой ответ.")
        return None
    parts = split_text(buf)
    with suppress(Exception): await placeholder.edit_text(parts[0])
    for part in parts[1:]: await update.message.reply_text(part)
    return buf

# =========================
# COMMANDS & MENU
# =========================

async def post_init(app):
    user_commands = [
        BotCommand("start", "Запустить бота"),
        BotCommand("draw", "Создать изображение"),
        BotCommand("last_news", "Последние новости"),
        BotCommand("weather", "Узнать погоду"),
        BotCommand("remind", "Напоминание"),
        BotCommand("todo", "Добавить заметку"),
        BotCommand("todos", "Мои заметки"),
        BotCommand("pass", "Сгенерировать пароль"),
        BotCommand("usd", "Курс валют"),
        BotCommand("choose", "Случайный выбор"),
        BotCommand("short", "Сократить ссылку"),
        BotCommand("spoiler", "Спойлер"),
        BotCommand("reset", "Очистить память"),
    ]
    await app.bot.set_my_commands(user_commands)

    # Эти команды видит ТОЛЬКО админ
    admin_commands = user_commands + [
        BotCommand("admin", "Админ-панель"),
        BotCommand("broadcast", "Рассылка всем юзерам"),
        BotCommand("set_news", "Обновить новость"),
        BotCommand("unban", "Разблокировать юзера"),
        BotCommand("history", "История сообщений"),
    ]
    try:
        await app.bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=ADMIN_ID))
    except Exception:
        log.exception("Admin commands error")

async def start(update, ctx):
    user = update.effective_user
    log_user(user.id, user.username, user.first_name)
    if is_banned(user.id):
        reason = get_ban_reason(user.id) or "Не указана"
        await update.message.reply_text(f"Ваш аккаунт заблокирован.\nПричина: {reason}")
        return
    _history[user.id].clear()
    await update.message.reply_text(
        "*VylaDigital на связи.*\n"
        "Пиши вопрос, отправляй фото или используй меню (слева).\n"
        "/last_news — последние новости.",
        parse_mode=ParseMode.MARKDOWN,
    )

async def reset(update, ctx):
    uid = update.effective_user.id
    if is_banned(uid): return
    _history[uid].clear()
    await update.message.reply_text("Память очищена.")

# =========================
# UTILITIES HANDLERS
# =========================

async def translate_cmd(update, ctx):
    text = update.message.text
    if not text.startswith("!"): return
    lang = text[1:3].lower()
    query = text[4:].strip()
    if not query: return
    try:
        url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl={lang}&dt=t&q={urllib.parse.quote(query)}"
        async with httpx.AsyncClient() as http:
            r = await http.get(url)
            translated = "".join([s[0] for s in r.json()[0]])
            await update.message.reply_text(f"🌐 {translated}")
    except Exception:
        await update.message.reply_text("Ошибка перевода.")

async def download_media(update, ctx):
    url = update.message.text
    if not ("youtube.com" in url or "youtu.be" in url or "tiktok.com" in url): return
    msg = await update.message.reply_text("⏳ Скачиваю...")
    try:
        ydl_opts = {'format': 'bestaudio/best[height<=480]', 'outtmpl': 'downloads/%(id)s.%(ext)s', 'quiet': True}
        os.makedirs('downloads', exist_ok=True)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filepath = ydl.prepare_filename(info)
            if filepath.endswith('.webm') or filepath.endswith('.mp4'):
                await update.message.reply_video(video=open(filepath, 'rb'), caption=info.get('title', 'Видео'))
            else:
                await update.message.reply_audio(audio=open(filepath, 'rb'), title=info.get('title', 'Аудио'))
            os.remove(filepath)
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"Ошибка скачивания: {e}")

async def weather(update, ctx):
    city = " ".join(ctx.args).strip()
    if not city: return await update.message.reply_text("Использование: /weather Москва")
    try:
        async with httpx.AsyncClient() as http:
            r = await http.get(f"https://wttr.in/{urllib.parse.quote(city)}?format=3")
            await update.message.reply_text(f"🌤 {r.text}")
    except Exception:
        await update.message.reply_text("Не удалось получить погоду.")

async def remind(update, ctx):
    args = ctx.args
    if len(args) < 2: return await update.message.reply_text("Использование: /remind 10m выпить чай")
    time_str, text = args[0], " ".join(args[1:])
    seconds = 0
    if time_str.endswith('m'): seconds = int(time_str[:-1]) * 60
    elif time_str.endswith('h'): seconds = int(time_str[:-1]) * 3600
    elif time_str.endswith('s'): seconds = int(time_str[:-1])
    else: return await update.message.reply_text("Укажи время: 10m, 2h, 30s.")
    await update.message.reply_text(f"⏰ Напомню через {time_str}: {text}")
    async def send_reminder(ctx: ContextTypes.DEFAULT_TYPE):
        await ctx.bot.send_message(chat_id=update.effective_user.id, text=f"🔔 Напоминание: {text}")
    ctx.job_queue.run_once(send_reminder, seconds)

async def todo(update, ctx):
    text = " ".join(ctx.args).strip()
    if not text: return
    uid = update.effective_user.id
    with closing(sqlite3.connect(DB_PATH)) as db:
        db.execute("INSERT INTO todos (uid, text, created_at) VALUES (?, ?, ?)", (uid, text, datetime.now().strftime("%Y-%m-%d")))
        db.commit()
    await update.message.reply_text("✅ Добавлено в список.")

async def todos(update, ctx):
    uid = update.effective_user.id
    with closing(sqlite3.connect(DB_PATH)) as db:
        rows = db.execute("SELECT id, text FROM todos WHERE uid=? AND done=0", (uid,)).fetchall()
    if not rows: return await update.message.reply_text("Список пуст.")
    text = "\n".join([f"{r[0]}. {r[1]}" for r in rows])
    await update.message.reply_text(f"📝 Твои дела:\n{text}")

async def pass_gen(update, ctx):
    length = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 16
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    pwd = "".join(random.choice(alphabet) for _ in range(length))
    await update.message.reply_text(f"🔑 `{pwd}`", parse_mode=ParseMode.MARKDOWN)

async def usd(update, ctx):
    if not ctx.args: return
    try:
        amount = float(ctx.args[0])
        async with httpx.AsyncClient() as http:
            r = await http.get("https://open.er-api.com/v6/latest/USD")
            rub = r.json()["rates"]["RUB"]
            await update.message.reply_text(f"💵 {amount} USD = {amount * rub:.2f} RUB")
    except Exception:
        await update.message.reply_text("Ошибка курса валют.")

async def choose(update, ctx):
    text = " ".join(ctx.args)
    options = [o.strip() for o in text.split(",") if o.strip()]
    if not options: return await update.message.reply_text("Напиши варианты через запятую.")
    await update.message.reply_text(f"🎲 Я выбираю: {random.choice(options)}")

async def short(update, ctx):
    if not ctx.args: return
    try:
        async with httpx.AsyncClient() as http:
            r = await http.get(f"https://is.gd/create.php?format=simple&url={urllib.parse.quote(ctx.args[0])}")
            await update.message.reply_text(f"🔗 {r.text}")
    except Exception:
        await update.message.reply_text("Ошибка сокращения.")

async def spoiler(update, ctx):
    text = " ".join(ctx.args)
    if not text: return
    await update.message.reply_text(f"<tg-spoiler>{text}</tg-spoiler>", parse_mode=ParseMode.HTML)

# =========================
# AI & MEDIA HANDLERS
# =========================

async def on_message(update, ctx):
    user = update.effective_user
    text = update.message.text
    if not text: return
    
    log_user(user.id, user.username, user.first_name)
    if is_banned(user.id): return await update.message.reply_text("Вы заблокированы.")
    
    if "youtube.com" in text or "youtu.be" in text or "tiktok.com" in text:
        await download_media(update, ctx)
        await log_user_message(update, ctx, text)
        return

    log_msg(user.id, "user", text)
    await log_user_message(update, ctx, text)

    async with _locks[user.id]:
        history = _history[user.id]
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history, {"role": "user", "content": text}]
        answer = await ask_llm(update, messages)
        if answer:
            history.append({"role": "user", "content": text})
            history.append({"role": "assistant", "content": answer})
            log_msg(user.id, "bot", answer)

async def on_photo(update, ctx):
    user = update.effective_user
    log_user(user.id, user.username, user.first_name)
    if is_banned(user.id): return await update.message.reply_text("Вы заблокированы.")
    file_id = update.message.photo[-1].file_id
    caption = update.message.caption or "Что на этом фото? Опиши подробно."
    log_msg(user.id, "user", f"[ФОТО] {caption}")
    await log_user_message(update, ctx, text=caption, photo=file_id)
    placeholder = await update.message.reply_text("Смотрю фото...")
    try:
        tg_file = await ctx.bot.get_file(file_id)
        response = await client.chat.completions.create(
            model=VISION_MODEL,
            messages=[{"role": "user", "content": [{"type": "text", "text": caption}, {"type": "image_url", "image_url": {"url": tg_file.file_path}}]}],
            temperature=0.5,
        )
        answer = response.choices[0].message.content or "Не удалось распознать."
        parts = split_text(answer)
        with suppress(Exception): await placeholder.edit_text(parts[0])
        for part in parts[1:]: await update.message.reply_text(part)
    except Exception as e:
        await placeholder.edit_text(f"Ошибка: {type(e).__name__}")

async def draw(update, ctx):
    user = update.effective_user
    log_user(user.id, user.username, user.first_name)
    if is_banned(user.id): return await update.message.reply_text("Вы заблокированы.")
    user_prompt = " ".join(ctx.args).strip()
    if not user_prompt: return await update.message.reply_text("Использование:\n/draw кот в космосе")
    await update.message.chat.send_action(ChatAction.UPLOAD_PHOTO)
    placeholder = await update.message.reply_text("Рисую...")
    try:
        enhancer = await client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": f"Convert to detailed English image prompt: {user_prompt}"}]
        )
        final_prompt = enhancer.choices[0].message.content.strip()
        encoded = urllib.parse.quote(final_prompt)
        async with httpx.AsyncClient(timeout=90.0) as http:
            response = await http.get(f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=1024&nologo=true")
            await update.message.reply_photo(photo=response.content, caption=f"Результат: {user_prompt}"[:1000])
        await placeholder.delete()
    except Exception as e:
        await placeholder.edit_text(f"Ошибка генерации: {type(e).__name__}")

# =========================
# ADMIN: NEWS & BROADCAST
# =========================

async def set_news(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    await update.message.reply_text("Отправь мне текст новости (или фото с подписью):")
    return WAIT_NEWS

async def receive_news(update, ctx):
    if update.effective_user.id != ADMIN_ID: return ConversationHandler.END
    if update.message.text:
        set_setting("latest_news", update.message.text)
        await update.message.reply_text("✅ Текст новости обновлен.")
    elif update.message.photo:
        set_setting("latest_news_photo", update.message.photo[-1].file_id)
        set_setting("latest_news", update.message.caption or "")
        await update.message.reply_text("✅ Новость с фото обновлена.")
    return ConversationHandler.END

async def last_news(update, ctx):
    text = get_setting("latest_news")
    photo = get_setting("latest_news_photo")
    if not text and not photo:
        return await update.message.reply_text("Пока новостей нет.")
    
    if photo:
        await update.message.reply_photo(photo=photo, caption=text)
    else:
        await update.message.reply_text(f"📢 Последние новости:\n\n{text}")

async def broadcast(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    await update.message.reply_text("Отправь мне текст для рассылки (или фото с подписью):")
    return WAIT_BROADCAST

async def receive_broadcast(update, ctx):
    if update.effective_user.id != ADMIN_ID: return ConversationHandler.END
    
    users = get_all_users()
    success = 0
    msg = await update.message.reply_text(f"⏳ Начинаю рассылку на {len(users)} чел...")
    
    for uid in users:
        try:
            if update.message.text:
                await ctx.bot.send_message(uid, f"📢 Сообщение от создателя:\n\n{update.message.text}")
            elif update.message.photo:
                await ctx.bot.send_photo(uid, photo=update.message.photo[-1].file_id, caption=f"📢 Сообщение от создателя:\n\n{update.message.caption or ''}")
            success += 1
            await asyncio.sleep(0.05) # Защита от лимитов Telegram
        except Exception:
            pass
            
    await msg.edit_text(f"✅ Рассылка завершена!\nДоставлено: {success} из {len(users)}")
    return ConversationHandler.END

# =========================
# ADMIN PANEL & CALLBACKS
# =========================

async def history_command(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    if not ctx.args: return await update.message.reply_text("Использование:\n/history ID")
    try: uid = int(ctx.args[0])
    except: return await update.message.reply_text("Некорректный ID.")
    with closing(sqlite3.connect(DB_PATH)) as db:
        rows = db.execute("SELECT role, text, ts FROM messages WHERE uid=? ORDER BY id DESC LIMIT 100", (uid,)).fetchall()
    if not rows: return await update.message.reply_text("История не найдена.")
    history_text = f"История ID: {uid}\n\n" + "\n\n".join(f"[{ts}] {role.upper()}:\n{text}" for role, text, ts in reversed(rows))
    for part in split_text(history_text): await update.message.reply_text(part)

async def admin_callback(update, ctx):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("У вас нет прав.", show_alert=True)
        return ConversationHandler.END

    await query.answer()
    data = query.data

    if data.startswith("ban_"):
        uid = int(data.split("_", 1)[1])
        ctx.user_data["ban_target"] = uid
        await query.message.reply_text(f"Введите причину бана для ID {uid}:\nИли напишите «отмена».")
        return WAIT_REASON
    elif data.startswith("reply_"):
        uid = int(data.split("_", 1)[1])
        ctx.user_data["reply_target"] = uid
        await query.message.reply_text(f"Напишите текст или фото для ID {uid}:\nИли «отмена».")
        return WAIT_REPLY
    elif data.startswith("hist_"):
        uid = int(data.split("_", 1)[1])
        with closing(sqlite3.connect(DB_PATH)) as db:
            rows = db.execute("SELECT role, text, ts FROM messages WHERE uid=? ORDER BY id DESC LIMIT 20", (uid,)).fetchall()
        history_text = "\n\n".join(f"[{ts}] {role.upper()}:\n{text[:500]}" for role, text, ts in reversed(rows))
        for part in split_text(f"История пользователя {uid}:\n\n{history_text}"): await ctx.bot.send_message(ADMIN_ID, part)
    return ConversationHandler.END

async def receive_reason(update, ctx):
    if update.effective_user.id != ADMIN_ID: return ConversationHandler.END
    text = (update.message.text or "").strip()
    if text.lower() == "отмена":
        await update.message.reply_text("Отменено.")
        return ConversationHandler.END
    uid = ctx.user_data.get("ban_target")
    if not uid: return ConversationHandler.END
    set_ban(uid, text)
    _history[uid].clear()
    await update.message.reply_text(f"ID {uid} забанен.")
    try: await ctx.bot.send_message(uid, f"Ваш аккаунт заблокирован.\nПричина: {text}")
    except: pass
    return ConversationHandler.END

async def receive_reply(update, ctx):
    if update.effective_user.id != ADMIN_ID: return ConversationHandler.END
    uid = ctx.user_data.get("reply_target")
    if not uid: return ConversationHandler.END
    if update.message.text and update.message.text.lower() == "отмена":
        await update.message.reply_text("Отменено.")
        return ConversationHandler.END
    try:
        if update.message.text: await ctx.bot.send_message(uid, update.message.text)
        elif update.message.photo: await ctx.bot.send_photo(uid, update.message.photo[-1].file_id, caption=update.message.caption or "")
        await update.message.reply_text("✅ Отправлено.")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")
    return ConversationHandler.END

async def admin_panel(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    with closing(sqlite3.connect(DB_PATH)) as db:
        users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        banned = db.execute("SELECT COUNT(*) FROM users WHERE is_banned=1").fetchone()[0]
    await update.message.reply_text(
        f"АДМИН-ПАНЕЛЬ\n\nПользователей: {users}\nЗаблокировано: {banned}\n\n"
        "/broadcast — рассылка\n/set_news — новая новость\n/unban ID — разбан\n/history ID — история"
    )

async def unban_command(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    if not ctx.args: return await update.message.reply_text("Использование: /unban ID")
    try: uid = int(ctx.args[0])
    except: return await update.message.reply_text("Некорректный ID.")
    set_ban(uid)
    await update.message.reply_text(f"Пользователь {uid} разблокирован.")
    try: await ctx.bot.send_message(uid, "Ваш аккаунт разблокирован.")
    except: pass

async def cancel_conv(update, ctx):
    await update.message.reply_text("Действие отменено.")
    return ConversationHandler.END

# =========================
# START BOT
# =========================

def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    # Единый ConversationHandler для ВСЕХ диалогов (чтобы не было конфликтов)
    admin_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_callback, pattern=r"^ban_\d+$"),
            CallbackQueryHandler(admin_callback, pattern=r"^reply_\d+$"),
            CommandHandler("broadcast", broadcast),
            CommandHandler("set_news", set_news),
        ],
        states={
            WAIT_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(ADMIN_ID), receive_reason)],
            WAIT_REPLY: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(ADMIN_ID), receive_reply), MessageHandler(filters.PHOTO & filters.User(ADMIN_ID), receive_reply)],
            WAIT_BROADCAST: [MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND & filters.User(ADMIN_ID), receive_broadcast)],
            WAIT_NEWS: [MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND & filters.User(ADMIN_ID), receive_news)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conv)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("draw", draw))
    app.add_handler(CommandHandler("last_news", last_news))
    
    # Утилиты
    app.add_handler(CommandHandler("weather", weather))
    app.add_handler(CommandHandler("remind", remind))
    app.add_handler(CommandHandler("todo", todo))
    app.add_handler(CommandHandler("todos", todos))
    app.add_handler(CommandHandler("pass", pass_gen))
    app.add_handler(CommandHandler("usd", usd))
    app.add_handler(CommandHandler("choose", choose))
    app.add_handler(CommandHandler("short", short))
    app.add_handler(CommandHandler("spoiler", spoiler))

    # Админка
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("unban", unban_command))
    app.add_handler(CommandHandler("history", history_command))
    
    app.add_handler(admin_conv)
    app.add_handler(CallbackQueryHandler(admin_callback))

    # Текст и медиа
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r'^![a-z]{2} '), translate_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    log.info("VylaDigital запущен. Админ: %s", ADMIN_ID)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

    
