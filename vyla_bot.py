import asyncio
import logging
import os
import urllib.parse
import sqlite3
import random
import string
import yt_dlp
import httpx
import io
import qrcode
import math

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

os.makedirs("/app/data", exist_ok=True)
DB_PATH = "/app/data/vyla.db"

MODEL = os.environ.get("VYLA_MODEL", "openai/gpt-oss-120b")
VISION_MODEL = os.environ.get("VYLA_VISION_MODEL", "llama-3.2-11b-vision-preview")
MAX_HISTORY, MAX_TG_LEN = 20, 4000

WAIT_REASON, WAIT_REPLY, WAIT_BROADCAST, WAIT_NEWS = 1, 2, 3, 4
WAIT_TRIGGER_WORD, WAIT_TRIGGER_ANSWER = 5, 6
WAIT_MAINTENANCE = 7

client = AsyncOpenAI(
    api_key=OPENAI_API_KEY,
    base_url=os.environ.get("OPENAI_BASE_URL", "https://api.groq.com/openai/v1"),
)

_history = defaultdict(lambda: deque(maxlen=MAX_HISTORY))
_locks = defaultdict(asyncio.Lock)

SYSTEM_PROMPT = (
    "Ты VylaDigital — ИИ-ассистент, созданный vylrein.\n"
    "Твой создатель — школьник и энтузиаст. Отвечай прямо и по делу."
)

# =========================
# DATABASE
# =========================
def init_db():
    with closing(sqlite3.connect(DB_PATH)) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS users (
            uid INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
            first_seen TEXT, last_seen TEXT, msg_count INTEGER DEFAULT 0,
            xp INTEGER DEFAULT 0, is_banned INTEGER DEFAULT 0, ban_reason TEXT
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, role TEXT, text TEXT, ts TEXT
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS triggers (
            word TEXT PRIMARY KEY, answer TEXT
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT
        )""")
        db.commit()

def get_user(uid):
    with closing(sqlite3.connect(DB_PATH)) as db:
        return db.execute("SELECT * FROM users WHERE uid=?", (uid,)).fetchone()

def is_banned(uid):
    u = get_user(uid)
    return bool(u and u[8] == 1) # Индекс is_banned сместился из-за xp

def get_ban_reason(uid):
    u = get_user(uid)
    return u[9] if u else None

def log_user(uid, username, first_name):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    with closing(sqlite3.connect(DB_PATH)) as db:
        row = db.execute("SELECT uid FROM users WHERE uid=?", (uid,)).fetchone()
        if row:
            db.execute("UPDATE users SET last_seen=?, msg_count=msg_count+1, xp=xp+1, username=?, first_name=? WHERE uid=?",
                       (now, username or "", first_name or "", uid))
        else:
            db.execute("INSERT INTO users (uid, username, first_name, first_seen, last_seen, msg_count, xp) VALUES (?, ?, ?, ?, ?, 1, 1)",
                       (uid, username or "", first_name or "", now, now))
        db.commit()

def log_msg(uid, role, text):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    with closing(sqlite3.connect(DB_PATH)) as db:
        db.execute("INSERT INTO messages(uid, role, text, ts) VALUES (?, ?, ?, ?)", (uid, role, text, now))
        db.commit()

def set_ban(uid, reason=None):
    with closing(sqlite3.connect(DB_PATH)) as db:
        if reason is not None: db.execute("UPDATE users SET is_banned=1, ban_reason=? WHERE uid=?", (reason, uid))
        else: db.execute("UPDATE users SET is_banned=0, ban_reason=NULL WHERE uid=?", (uid,))
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
        parts.append(buf[:cut]); buf = buf[cut:].lstrip()
    if buf: parts.append(buf)
    return parts

async def send_log(ctx, text=None, kb=None, photo=None):
    target = LOG_CHANNEL_ID if LOG_CHANNEL_ID else ADMIN_ID
    try:
        if photo: await ctx.bot.send_photo(chat_id=target, photo=photo, caption=text[:1024] if text else "", reply_markup=kb)
        else: await ctx.bot.send_message(chat_id=target, text=text[:4000], reply_markup=kb)
    except Exception: log.exception("Logging error")

async def log_user_message(update, ctx, text=None, photo=None):
    user = update.effective_user
    if user.id == ADMIN_ID and not LOG_CHANNEL_ID: return
    tag = f"@{user.username}" if user.username else user.first_name
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Забанить", callback_data=f"ban_{user.id}"),
        InlineKeyboardButton("История", callback_data=f"hist_{user.id}"),
        InlineKeyboardButton("Ответить", callback_data=f"reply_{user.id}"),
    ]])
    if photo: await send_log(ctx, text=f"Пользователь: {tag}\nID: {user.id}\n\n{text or ''}", kb=kb, photo=photo)
    else: await send_log(ctx, text=f"Пользователь: {tag}\nID: {user.id}\n\n{text}", kb=kb)

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
        await placeholder.edit_text("Пустой ответ."); return None
    parts = split_text(buf)
    with suppress(Exception): await placeholder.edit_text(parts[0])
    for part in parts[1:]: await update.message.reply_text(part)
    return buf

# =========================
# MENU & UTILS COMMANDS
# =========================
async def post_init(app):
    cmds = [
        BotCommand("start", "🚀 Запустить"), BotCommand("profile", "📊 Мой профиль"),
        BotCommand("draw", "🎨 Рисовать"), BotCommand("qr", "📱 QR-код"),
        BotCommand("weather", "🌤 Погода"), BotCommand("remind", "⏰ Напоминание"),
        BotCommand("todo", "📝 Заметка"), BotCommand("todos", "📋 Мои заметки"),
        BotCommand("pass", "🔑 Пароль"), BotCommand("calc", "🧮 Калькулятор"),
        BotCommand("usd", "💵 Доллар"), BotCommand("crypto", "🪙 Крипта"),
        BotCommand("choose", "🎲 Выбор"), BotCommand("short", "🔗 Ссылка"),
        BotCommand("spoiler", "🙈 Спойлер"), BotCommand("google", "🔍 Поиск"),
        BotCommand("meme", "😂 Мем"), BotCommand("quiz", "❓ Викторина"),
        BotCommand("reset", "🧹 Очистить память"), BotCommand("last_news", "📢 Новости")
    ]
    await app.bot.set_my_commands(cmds)
    admin_cmds = cmds + [
        BotCommand("admin", "⚙️ Админка"), BotCommand("broadcast", "📣 Рассылка"),
        BotCommand("set_news", "📰 Новость"), BotCommand("users", "👥 Юзеры"),
        BotCommand("backup", "💾 Бэкап"), BotCommand("mute", "🔇 Мут"),
        BotCommand("trigger", "🤖 Триггер"), BotCommand("history", "📜 История")
    ]
    try: await app.bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=ADMIN_ID))
    except: pass

async def start(update, ctx):
    u = update.effective_user
    log_user(u.id, u.username, u.first_name)
    if is_banned(u.id): return await update.message.reply_text(f"Вы забанены. Причина: {get_ban_reason(u.id)}")
    _history[u.id].clear()
    await update.message.reply_text("*VylaDigital на связи.*\nПиши вопрос или используй меню.", parse_mode=ParseMode.MARKDOWN)

async def profile(update, ctx):
    u = get_user(update.effective_user.id)
    if not u: return
    level = int(math.sqrt(u[6] / 10)) + 1
    await update.message.reply_text(f"👤 *Профиль*\nИмя: {u[2]}\nID: `{u[0]}`\nСообщений: {u[5]}\nXP: {u[6]}\nУровень: {level}", parse_mode=ParseMode.MARKDOWN)

async def qr_gen(update, ctx):
    text = " ".join(ctx.args)
    if not text: return await update.message.reply_text("Использование: /qr текст")
    img = qrcode.make(text)
    bio = io.BytesIO(); bio.name = "qr.png"; img.save(bio); bio.seek(0)
    await update.message.reply_photo(photo=bio)

async def calc(update, ctx):
    expr = " ".join(ctx.args).replace("^", "**")
    try:
        res = eval(expr, {"__builtins__": None}, {})
        await update.message.reply_text(f"🧮 `{expr}` = `{res}`", parse_mode=ParseMode.MARKDOWN)
    except:
        await update.message.reply_text("Ошибка в выражении.")

async def google(update, ctx):
    q = " ".join(ctx.args)
    if not q: return
    async with httpx.AsyncClient() as http:
        r = await http.get(f"https://api.duckduckgo.com/?q={urllib.parse.quote(q)}&format=json")
        data = r.json()
        if data.get("AbstractText"): await update.message.reply_text(f"🔍 {data['AbstractText']}\n\n{data.get('AbstractURL')}")
        else: await update.message.reply_text("Ничего не найдено.")

async def meme(update, ctx):
    async with httpx.AsyncClient() as http:
        r = await http.get("https://meme-api.com/gimme")
        await update.message.reply_photo(photo=r.json()["url"])

async def quiz(update, ctx):
    async with httpx.AsyncClient() as http:
        r = await http.get("https://opentdb.com/api.php?amount=1&type=multiple&encode=url3986")
        d = r.json()["results"][0]
        q = urllib.parse.unquote(d["question"])
        correct = urllib.parse.unquote(d["correct_answer"])
        opts = [urllib.parse.unquote(o) for o in d["incorrect_answers"]] + [correct]
        random.shuffle(opts)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(o, callback_data=f"quiz_{o == correct}")] for o in opts])
        await update.message.reply_text(f"❓ {q}", reply_markup=kb)

async def crypto(update, ctx):
    async with httpx.AsyncClient() as http:
        r = await http.get("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd&include_24hr_change=true")
        d = r.json()
        text = "🪙 *Крипта:*\n"
        for k, v in d.items():
            text += f"{k.capitalize()}: ${v['usd']} ({v['usd_24h_change']:.2f}%)\n"
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

# =========================
# MESSAGES & AI
# =========================
async def on_message(update, ctx):
    user, text = update.effective_user, update.message.text
    if not text: return
    
    # Глобальный мут
    if get_setting("global_mute") == "1" and user.id != ADMIN_ID:
        return await update.message.reply_text("Бот на техобслуживании.")

    log_user(user.id, user.username, user.first_name)
    if is_banned(user.id): return await update.message.reply_text("Вы заблокированы.")

    # Триггеры
    with closing(sqlite3.connect(DB_PATH)) as db:
        trig = db.execute("SELECT answer FROM triggers WHERE word=?", (text.lower(),)).fetchone()
    if trig: return await update.message.reply_text(trig[0])

    if "youtube.com" in text or "youtu.be" in text:
        await download_media(update, ctx); await log_user_message(update, ctx, text); return

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

async def download_media(update, ctx):
    msg = await update.message.reply_text("⏳ Скачиваю...")
    try:
        ydl_opts = {'format': 'bestaudio/best[height<=480]', 'outtmpl': 'downloads/%(id)s.%(ext)s', 'quiet': True}
        os.makedirs('downloads', exist_ok=True)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(update.message.text, download=True)
            fp = ydl.prepare_filename(info)
            if fp.endswith(('.mp4', '.webm')): await update.message.reply_video(video=open(fp, 'rb'), caption=info.get('title', ''))
            else: await update.message.reply_audio(audio=open(fp, 'rb'), title=info.get('title', ''))
            os.remove(fp)
        await msg.delete()
    except Exception as e: await msg.edit_text(f"Ошибка: {e}")

async def on_photo(update, ctx):
    user = update.effective_user
    log_user(user.id, user.username, user.first_name)
    if is_banned(user.id): return await update.message.reply_text("Вы заблокированы.")
    file_id = update.message.photo[-1].file_id
    caption = update.message.caption or "Что на этом фото?"
    log_msg(user.id, "user", f"[ФОТО] {caption}")
    await log_user_message(update, ctx, text=caption, photo=file_id)
    ph = await update.message.reply_text("Смотрю...")
    try:
        tg_file = await ctx.bot.get_file(file_id)
        resp = await client.chat.completions.create(
            model=VISION_MODEL,
            messages=[{"role": "user", "content": [{"type": "text", "text": caption}, {"type": "image_url", "image_url": {"url": tg_file.file_path}}]}],
        )
        await ph.edit_text(resp.choices[0].message.content or "Не удалось.")
    except Exception as e: await ph.edit_text(f"Ошибка: {type(e).__name__}")

async def draw(update, ctx):
    user = update.effective_user
    log_user(user.id, user.username, user.first_name)
    prompt = " ".join(ctx.args).strip()
    if not prompt: return await update.message.reply_text("Напиши: /draw кот")
    ph = await update.message.reply_text("Рисую...")
    try:
        enhanced = await client.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": f"Image prompt: {prompt}"}])
        enc = urllib.parse.quote(enhanced.choices[0].message.content.strip())
        async with httpx.AsyncClient(timeout=90.0) as http:
            r = await http.get(f"https://image.pollinations.ai/prompt/{enc}?nologo=true")
            await update.message.reply_photo(photo=r.content, caption=f"🎨 {prompt}")
        await ph.delete()
    except Exception as e: await ph.edit_text(f"Ошибка: {type(e).__name__}")

# =========================
# ADMIN PANEL
# =========================
async def admin_panel(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    with closing(sqlite3.connect(DB_PATH)) as db:
        users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        banned = db.execute("SELECT COUNT(*) FROM users WHERE is_banned=1").fetchone()[0]
        msgs = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    mute_status = "ВКЛ" if get_setting("global_mute") == "1" else "ВЫКЛ"
    await update.message.reply_text(
        f"⚙️ АДМИНКА\n\nЮзеров: {users}\nВ бане: {banned}\nСообщений: {msgs}\nМут: {mute_status}\n\n"
        "/users — список юзеров\n/mute — вкл/выкл мут\n/broadcast — рассылка\n/backup — бэкап БД"
    )

async def users_list(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    with closing(sqlite3.connect(DB_PATH)) as db:
        rows = db.execute("SELECT uid, first_name, username FROM users ORDER BY last_seen DESC LIMIT 15").fetchall()
    text = "👥 Последние 15 юзеров:\n" + "\n".join(f"{r[1]} (@{r[2]}) — `{r[0]}`" for r in rows)
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def mute_cmd(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    curr = get_setting("global_mute")
    set_setting("global_mute", "0" if curr == "1" else "1")
    await update.message.reply_text(f"Мут: {'ВКЛ' if curr != '1' else 'ВЫКЛ'}")

async def trigger_cmd(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    await update.message.reply_text("Введи слово-триггер (или 'отмена'):")
    return WAIT_TRIGGER_WORD

async def receive_trigger_word(update, ctx):
    if update.effective_user.id != ADMIN_ID: return ConversationHandler.END
    if update.message.text.lower() == "отмена": return ConversationHandler.END
    ctx.user_data["trig_w"] = update.message.text.lower()
    await update.message.reply_text("Теперь ответ на это слово:")
    return WAIT_TRIGGER_ANSWER

async def receive_trigger_answer(update, ctx):
    if update.effective_user.id != ADMIN_ID: return ConversationHandler.END
    with closing(sqlite3.connect(DB_PATH)) as db:
        db.execute("INSERT OR REPLACE INTO triggers (word, answer) VALUES (?, ?)", (ctx.user_data["trig_w"], update.message.text))
        db.commit()
    await update.message.reply_text("✅ Триггер добавлен.")
    return ConversationHandler.END

async def backup(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    try:
        await update.message.reply_document(document=open(DB_PATH, 'rb'), filename="vyla_backup.db", caption="💾 Бэкап базы")
    except Exception as e: await update.message.reply_text(f"Ошибка: {e}")

async def history_command(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    if not ctx.args: return await update.message.reply_text("/history ID")
    uid = int(ctx.args[0])
    with closing(sqlite3.connect(DB_PATH)) as db:
        rows = db.execute("SELECT role, text, ts FROM messages WHERE uid=? ORDER BY id DESC LIMIT 40", (uid,)).fetchall()
    if not rows: return await update.message.reply_text("Пусто.")
    await update.message.reply_text(f"📜 История {uid}:")
    for r, t, ts in reversed(rows):
        await update.message.reply_text(f"[{ts}] {r.upper()}:\n{t}")
        await asyncio.sleep(0.05)

# =========================
# BROADCAST & NEWS (ОСТАЛЬНОЕ)
# =========================
async def set_news(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    await update.message.reply_text("Отправь текст новости:")
    return WAIT_NEWS

async def receive_news(update, ctx):
    if update.effective_user.id != ADMIN_ID: return ConversationHandler.END
    set_setting("latest_news", update.message.text)
    await update.message.reply_text("✅ Новость обновлена.")
    return ConversationHandler.END

async def last_news(update, ctx):
    news = get_setting("latest_news")
    if not news: return await update.message.reply_text("Новостей нет.")
    await update.message.reply_text(f"📢 Последние новости:\n\n{news}")

async def broadcast(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    await update.message.reply_text("Отправь текст для рассылки:")
    return WAIT_BROADCAST

async def receive_broadcast(update, ctx):
    if update.effective_user.id != ADMIN_ID: return ConversationHandler.END
    users = get_all_users()
    msg = await update.message.reply_text(f"⏳ Рассылка на {len(users)} чел...")
    success = 0
    for uid in users:
        try:
            await ctx.bot.send_message(uid, f"📢 Сообщение от создателя:\n\n{update.message.text}")
            success += 1
            await asyncio.sleep(0.05)
        except: pass
    await msg.edit_text(f"✅ Доставлено: {success} из {len(users)}")
    return ConversationHandler.END

async def admin_callback(update, ctx):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID: return await q.answer("Нет прав.", show_alert=True)
    await q.answer()
    if q.data.startswith("ban_"):
        ctx.user_data["ban_target"] = int(q.data.split("_")[1])
        await q.message.reply_text("Причина бана (или 'отмена'):")
        return WAIT_REASON
    elif q.data.startswith("reply_"):
        ctx.user_data["reply_target"] = int(q.data.split("_")[1])
        await q.message.reply_text("Текст/фото для ответа (или 'отмена'):")
        return WAIT_REPLY
    elif q.data.startswith("hist_"):
        uid = int(q.data.split("_")[1])
        with closing(sqlite3.connect(DB_PATH)) as db:
            rows = db.execute("SELECT role, text FROM messages WHERE uid=? ORDER BY id DESC LIMIT 15", (uid,)).fetchall()
        for r, t in reversed(rows):
            await ctx.bot.send_message(ADMIN_ID, f"{r.upper()}: {t[:1000]}")
            await asyncio.sleep(0.05)
    elif q.data.startswith("quiz_"):
        if q.data == "quiz_True":
            await q.message.reply_text("✅ Правильно!")
        else:
            await q.message.reply_text("❌ Неправильно.")
    return ConversationHandler.END

async def receive_reason(update, ctx):
    if update.effective_user.id != ADMIN_ID: return ConversationHandler.END
    if update.message.text.lower() == "отмена": return ConversationHandler.END
    set_ban(ctx.user_data["ban_target"], update.message.text)
    await update.message.reply_text("Забанен.")
    try: await ctx.bot.send_message(ctx.user_data["ban_target"], f"Вы забанены. Причина: {update.message.text}")
    except: pass
    return ConversationHandler.END

async def receive_reply(update, ctx):
    if update.effective_user.id != ADMIN_ID: return ConversationHandler.END
    if update.message.text and update.message.text.lower() == "отмена": return ConversationHandler.END
    uid = ctx.user_data["reply_target"]
    try:
        if update.message.text: await ctx.bot.send_message(uid, update.message.text)
        elif update.message.photo: await ctx.bot.send_photo(uid, update.message.photo[-1].file_id, caption=update.message.caption or "")
        await update.message.reply_text("✅ Отправлено.")
    except Exception as e: await update.message.reply_text(f"Ошибка: {e}")
    return ConversationHandler.END

async def cancel_conv(update, ctx):
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END

# =========================
# MAIN
# =========================
def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_callback, pattern=r"^(ban_|reply_)"),
            CommandHandler("broadcast", broadcast), CommandHandler("set_news", set_news),
            CommandHandler("trigger", trigger_cmd)
        ],
        states={
            WAIT_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(ADMIN_ID), receive_reason)],
            WAIT_REPLY: [MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND & filters.User(ADMIN_ID), receive_reply)],
            WAIT_BROADCAST: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(ADMIN_ID), receive_broadcast)],
            WAIT_NEWS: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(ADMIN_ID), receive_news)],
            WAIT_TRIGGER_WORD: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(ADMIN_ID), receive_trigger_word)],
            WAIT_TRIGGER_ANSWER: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(ADMIN_ID), receive_trigger_answer)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conv)], allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("profile", profile))
    app.add_handler(CommandHandler("qr", qr_gen))
    app.add_handler(CommandHandler("calc", calc))
    app.add_handler(CommandHandler("google", google))
    app.add_handler(CommandHandler("meme", meme))
    app.add_handler(CommandHandler("quiz", quiz))
    app.add_handler(CommandHandler("crypto", crypto))
    app.add_handler(CommandHandler("draw", draw))
    app.add_handler(CommandHandler("last_news", last_news))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("users", users_list))
    app.add_handler(CommandHandler("mute", mute_cmd))
    app.add_handler(CommandHandler("backup", backup))
    app.add_handler(CommandHandler("history", history_command))

    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(admin_callback))
    
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
