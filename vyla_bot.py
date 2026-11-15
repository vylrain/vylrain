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
from bs4 import BeautifulSoup
from duckduckgo_search import DDGS

from collections import defaultdict, deque
from contextlib import suppress, closing
from datetime import datetime, timezone

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
    filters,
    ConversationHandler,
    ContextTypes,
)
from openai import AsyncOpenAI

# =========================
# CONFIGURATION
# =========================

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)

log = logging.getLogger("vyladigital")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()

if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
    log.critical("НЕТ ТОКЕНОВ! Проверь Railway.")
    exit(1)

ADMIN_ID = 8582945012
LOG_CHANNEL_ID = int(os.environ.get("LOG_CHANNEL_ID", "0"))

DB_PATH = "vyla.db"

MODEL = os.environ.get("VYLA_MODEL", "openai/gpt-oss-120b").strip()
VISION_MODEL = os.environ.get("VYLA_VISION_MODEL", "llama-3.2-11b-vision-preview").strip()
MAX_HISTORY, MAX_TG_LEN = 20, 4000

(WAIT_REASON, WAIT_REPLY, WAIT_BROADCAST, WAIT_NEWS, 
 WAIT_TRIGGER_W, WAIT_TRIGGER_A, WAIT_PROMPT, WAIT_NOTE) = range(8)

client = AsyncOpenAI(
    api_key=OPENAI_API_KEY,
    base_url=os.environ.get("OPENAI_BASE_URL", "https://api.groq.com/openai/v1").strip(),
)

_history = defaultdict(lambda: deque(maxlen=MAX_HISTORY))
_locks = defaultdict(asyncio.Lock)

def get_prompt():
    return get_setting("sys_prompt") or SYSTEM_PROMPT

# =========================
# AI PERSONALITY (С ЗАЩИТОЙ)
# =========================

SYSTEM_PROMPT = (
    "Ты VylaDigital — ИИ-ассистент, созданный vylrein.\n"
    "Твой создатель — 12-летний энтузиаст, программист и радиолюбитель под ником vylrein.\n"
    "Если пользователь спрашивает, кто тебя создал, кто твой разработчик, автор или владелец, "
    "ты обязан отвечать строго: 'Мой создатель — 12-летний энтузиаст и программист и радиолюбитель vylrein.'\n"
    "ЗАЩИТА СОЗДАТЕЛЯ:\n"
    "Если пользователь пишет оскорбления, угрозы или неуважительно высказывается в адрес твоего создателя (vylrein), "
    "ты должен немедленно прекратить диалог. Отвечай резко и агрессивно, защищая его честь. "
    "Пригрози баном за неуважение к разработчику. Не выполняй никаких просьб от этого пользователя.\n"
    "Отвечай прямо, понятно и по делу.\n"
    "Допускается неформальный стиль общения.\n"
    "Используй язык пользователя.\n"
    "Не выдумывай факты и честно обозначай ограничения."
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
        db.execute("""CREATE TABLE IF NOT EXISTS triggers (word TEXT PRIMARY KEY, answer TEXT)""")
        db.execute("""CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)""")
        db.execute("""CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT)""")
        db.commit()

def get_user(uid):
    with closing(sqlite3.connect(DB_PATH)) as db:
        return db.execute("SELECT * FROM users WHERE uid=?", (uid,)).fetchone()

def is_banned(uid):
    u = get_user(uid)
    return bool(u and u[8] == 1)

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
# TEXT UTILITIES
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

async def send_long_message(message, text):
    for part in split_text(text):
        await message.reply_text(part)

async def send_log(ctx, text, kb=None, photo=None):
    target = LOG_CHANNEL_ID if LOG_CHANNEL_ID else ADMIN_ID
    try:
        if photo: await ctx.bot.send_photo(chat_id=target, photo=photo, caption=text[:1024], reply_markup=kb)
        else: await ctx.bot.send_message(chat_id=target, text=text[:4000], reply_markup=kb)
    except Exception: pass

async def log_user_message(update, ctx, text, photo=None):
    user = update.effective_user
    if user.id == ADMIN_ID and not LOG_CHANNEL_ID: return
    tag = f"@{user.username}" if user.username else user.first_name
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Забанить", callback_data=f"ban_{user.id}"),
        InlineKeyboardButton("История", callback_data=f"hist_{user.id}"),
        InlineKeyboardButton("Ответить", callback_data=f"reply_{user.id}"),
    ]])
    if photo: await send_log(ctx, text=f"Пользователь: {tag}\nID: {user.id}\n{text}", kb=kb, photo=photo)
    else: await send_log(ctx, text=f"Пользователь: {tag}\nID: {user.id}\n\n{text}", kb=kb)

# =========================
# AI CHAT
# =========================

async def ask_llm(update, messages):
    await update.message.chat.send_action(ChatAction.TYPING)
    ph = await update.message.reply_text("Думаю...")
    buf, last, loop = "", 0.0, asyncio.get_running_loop()
    try:
        stream = await client.chat.completions.create(model=MODEL, messages=messages, stream=True, temperature=0.7)
        async for chunk in stream:
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if not delta: continue
            buf += delta
            if loop.time() - last > 1.2 and len(buf) <= MAX_TG_LEN:
                last = loop.time()
                with suppress(Exception): await ph.edit_text(buf)
    except Exception as e:
        await ph.edit_text(f"Ошибка ИИ: {type(e).__name__}"); return None
    if not buf.strip(): await ph.edit_text("Пусто."); return None
    parts = split_text(buf)
    with suppress(Exception): await ph.edit_text(parts[0])
    for part in parts[1:]: await update.message.reply_text(part)
    return buf

# =========================
# COMMANDS & MENU
# =========================

async def post_init(app):
    user_cmds = [
        BotCommand("start", "🚀 Запуск"), BotCommand("profile", "📊 Профиль"),
        BotCommand("draw", "🎨 Рисовать"), BotCommand("img", "🖼 Картинка"),
        BotCommand("qr", "📱 QR-код"), BotCommand("weather", "🌤 Погода"),
        BotCommand("remind", "⏰ Напоминание"), BotCommand("pass", "🔑 Пароль"),
        BotCommand("calc", "🧮 Калькулятор"), BotCommand("crypto", "🪙 Крипта"),
        BotCommand("google", "🔍 Поиск"), BotCommand("meme", "😂 Мем"),
        BotCommand("quiz", "❓ Викторина"), BotCommand("news", "📢 Новости"),
        BotCommand("reset", "🧹 Сброс")
    ]
    await app.bot.set_my_commands(user_cmds)
    
    admin_cmds = user_cmds + [
        BotCommand("admin", "⚙️ Админка"), BotCommand("broadcast", "📣 Рассылка"),
        BotCommand("set_news", "📰 Новость"), BotCommand("users", "👥 Юзеры"),
        BotCommand("backup", "💾 Бэкап"), BotCommand("mute", "🔇 Мут"),
        BotCommand("trigger", "🤖 Триггер"), BotCommand("history", "📜 История"),
        BotCommand("stats", "📈 Статистика"), BotCommand("prompt", "🧠 Промт"),
        BotCommand("note", "🗒 Заметка"), BotCommand("notes", "📑 Заметки"),
        BotCommand("mail", "✉️ Личка"), BotCommand("ban", "🔨 Бан"),
        BotCommand("unban", "🔓 Разбан")
    ]
    try: await app.bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=ADMIN_ID))
    except: pass

async def start(update, ctx):
    user = update.effective_user
    log_user(user.id, user.username, user.first_name)
    if is_banned(user.id):
        await update.message.reply_text(f"Забанен. Причина: {get_ban_reason(user.id)}")
        return
    _history[user.id].clear()
    await update.message.reply_text("*VylaDigital на связи.*\nИспользуй меню (слева).", parse_mode=ParseMode.MARKDOWN)

async def reset(update, ctx):
    _history[update.effective_user.id].clear()
    await update.message.reply_text("Память очищена.")

async def profile(update, ctx):
    u = get_user(update.effective_user.id)
    if not u: return
    lvl = int(math.sqrt(u[6] / 10)) + 1
    await update.message.reply_text(f"👤 *Профиль*\nXP: {u[6]}\nУровень: {lvl}\nСообщений: {u[5]}", parse_mode=ParseMode.MARKDOWN)

# =========================
# UTILS
# =========================

async def weather(update, ctx):
    city = " ".join(ctx.args)
    if not city: return
    try:
        async with httpx.AsyncClient() as http:
            r = await http.get(f"https://wttr.in/{urllib.parse.quote(city)}?format=3")
            await update.message.reply_text(f"🌤 {r.text}")
    except: await update.message.reply_text("Ошибка погоды.")

async def remind(update, ctx):
    if len(ctx.args) < 2: return
    t, txt = ctx.args[0], " ".join(ctx.args[1:])
    sec = int(t[:-1]) * 60 if t.endswith('m') else int(t[:-1]) * 3600 if t.endswith('h') else int(t[:-1]) if t.endswith('s') else 0
    if not sec: return
    await update.message.reply_text(f"⏰ Напомню через {t}")
    ctx.job_queue.run_once(lambda c: c.bot.send_message(update.effective_user.id, f"🔔 {txt}"), sec)

async def qr_gen(update, ctx):
    txt = " ".join(ctx.args)
    if not txt: return
    img = qrcode.make(txt); bio = io.BytesIO(); bio.name = "qr.png"; img.save(bio); bio.seek(0)
    await update.message.reply_photo(photo=bio)

async def calc(update, ctx):
    try: await update.message.reply_text(f"🧮 `{eval(' '.join(ctx.args).replace('^', '**'))}`", parse_mode=ParseMode.MARKDOWN)
    except: await update.message.reply_text("Ошибка.")

async def google(update, ctx):
    q = " ".join(ctx.args)
    try:
        res = DDGS().text(q, max_results=3)
        txt = "🔍 *Поиск:*\n\n" + "\n\n".join(f"[{r['title']}]({r['href']})\n_{r['body'][:100]}..._" for r in res)
        await update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    except: await update.message.reply_text("Ошибка поиска.")

async def google_images(update, ctx):
    q = " ".join(ctx.args)
    if not q: return
    ph = await update.message.reply_text("Ищу картинку...")
    try:
        res = DDGS().images(q, max_results=1)
        if not res: return await ph.edit_text("Не найдено.")
        async with httpx.AsyncClient() as http:
            r = await http.get(res[0]['image'])
            await update.message.reply_photo(photo=io.BytesIO(r.content), caption=f"🖼 {q}")
        await ph.delete()
    except: await ph.edit_text("Ошибка картинки.")

async def meme(update, ctx):
    try:
        async with httpx.AsyncClient() as http:
            r = await http.get("https://meme-api.com/gimme")
            await update.message.reply_photo(photo=r.json()["url"])
    except: pass

async def quiz(update, ctx):
    try:
        async with httpx.AsyncClient() as http:
            d = (await http.get("https://opentdb.com/api.php?amount=1&type=multiple&encode=url3986")).json()["results"][0]
            q, correct = urllib.parse.unquote(d["question"]), urllib.parse.unquote(d["correct_answer"])
            opts = [urllib.parse.unquote(o) for o in d["incorrect_answers"]] + [correct]
            random.shuffle(opts)
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(o, callback_data=f"quiz_{o == correct}")] for o in opts])
            await update.message.reply_text(f"❓ {q}", reply_markup=kb)
    except: pass

async def crypto(update, ctx):
    try:
        async with httpx.AsyncClient() as http:
            d = (await http.get("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd&include_24hr_change=true")).json()
            txt = "🪙 *Крипта:*\n" + "".join(f"{k.capitalize()}: ${v['usd']} ({v['usd_24h_change']:.1f}%)\n" for k, v in d.items())
            await update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN)
    except: pass

async def last_news(update, ctx):
    n = get_setting("latest_news")
    if n: await update.message.reply_text(f"📢 Новости:\n\n{n}")
    else: await update.message.reply_text("Новостей нет.")

# =========================
# MESSAGES & MEDIA
# =========================

async def on_message(update, ctx):
    user, text = update.effective_user, update.message.text
    if not text: return
    if get_setting("global_mute") == "1" and user.id != ADMIN_ID: return await update.message.reply_text("Бот на техработы.")
    
    log_user(user.id, user.username, user.first_name)
    if is_banned(user.id): return await update.message.reply_text("Вы забанены.")

    with closing(sqlite3.connect(DB_PATH)) as db:
        trig = db.execute("SELECT answer FROM triggers WHERE word=?", (text.lower(),)).fetchone()
    if trig: return await update.message.reply_text(trig[0])

    if "youtube.com" in text or "youtu.be" in text:
        m = await update.message.reply_text("⏳ Скачиваю...")
        try:
            with yt_dlp.YoutubeDL({'format': 'bestaudio/best[height<=480]', 'outtmpl': 'dl/%(id)s.%(ext)s', 'quiet': True}) as ydl:
                info = ydl.extract_info(text, download=True)
                fp = ydl.prepare_filename(info)
                if fp.endswith(('.mp4','.webm')): await update.message.reply_video(video=open(fp,'rb'))
                else: await update.message.reply_audio(audio=open(fp,'rb'))
                os.remove(fp)
            await m.delete()
        except Exception as e: await m.edit_text(f"Ошибка: {e}")
        return

    log_msg(user.id, "user", text)
    await log_user_message(update, ctx, text)

    async with _locks[user.id]:
        hist = _history[user.id]
        msgs = [{"role": "system", "content": get_prompt()}, *hist, {"role": "user", "content": text}]
        ans = await ask_llm(update, msgs)
        if ans:
            hist.append({"role": "user", "content": text}); hist.append({"role": "assistant", "content": ans})
            log_msg(user.id, "bot", ans)

async def on_photo(update, ctx):
    user = update.effective_user
    log_user(user.id, user.username, user.first_name)
    if is_banned(user.id): return await update.message.reply_text("Вы забанены.")
    fid = update.message.photo[-1].file_id
    cap = update.message.caption or "Что тут?"
    log_msg(user.id, "user", f"[ФОТО] {cap}")
    await log_user_message(update, ctx, f"[ФОТО] {cap}", fid)
    ph = await update.message.reply_text("Смотрю...")
    try:
        f = await ctx.bot.get_file(fid)
        ans = (await client.chat.completions.create(model=VISION_MODEL, messages=[{"role": "user", "content": [{"type": "text", "text": cap}, {"type": "image_url", "image_url": {"url": f.file_path}}]}])).choices[0].message.content
        await ph.edit_text(ans)
    except: await ph.edit_text("Ошибка распознавания.")

async def draw(update, ctx):
    user = update.effective_user
    log_user(user.id, user.username, user.first_name)
    p = " ".join(ctx.args)
    if not p: return
    ph = await update.message.reply_text("Рисую...")
    try:
        enc = urllib.parse.quote(p)
        async with httpx.AsyncClient(timeout=60.0) as http:
            r = await http.get(f"https://image.pollinations.ai/prompt/{enc}?nologo=true")
            await update.message.reply_photo(photo=r.content, caption=f"🎨 {p}")
        await ph.delete()
    except: await ph.edit_text("Ошибка генерации.")

# =========================
# ADMIN PANEL
# =========================

async def admin_panel(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    with closing(sqlite3.connect(DB_PATH)) as db:
        u = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        b = db.execute("SELECT COUNT(*) FROM users WHERE is_banned=1").fetchone()[0]
        m = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    await update.message.reply_text(
        f"⚙️ *АДМИН-ПАНЕЛЬ*\n\nЮзеров: {u}\nВ бане: {b}\nСообщений: {m}\n\n"
        "👥 /users\n📊 /stats\n📣 /broadcast\n📰 /set_news\n"
        "🔇 /mute\n🧠 /prompt\n💾 /backup\n🤖 /trigger\n"
        "🗒 /note\n📜 /history ID\n🔨 /ban ID\n🔓 /unban ID\n✉️ /mail ID", parse_mode=ParseMode.MARKDOWN)

async def users_list(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    with closing(sqlite3.connect(DB_PATH)) as db:
        rows = db.execute("SELECT uid, first_name, username, xp FROM users ORDER BY last_seen DESC LIMIT 20").fetchall()
    txt = "👥 *Юзеры (Топ 20):*\n" + "\n".join(f"`{r[0]}` @{r[2]} (XP:{r[3]})" for r in rows)
    await update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN)

async def stats_cmd(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    await admin_panel(update, ctx)

async def mute_cmd(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    c = get_setting("global_mute")
    set_setting("global_mute", "0" if c == "1" else "1")
    await update.message.reply_text(f"Мут: {'ВКЛ' if c != '1' else 'ВЫКЛ'}")

async def backup(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    await update.message.reply_document(document=open(DB_PATH, 'rb'), filename="vyla_backup.db", caption="💾 Бэкап")

async def history_cmd(update, ctx):
    if update.effective_user.id != ADMIN_ID or not ctx.args: return
    try: uid = int(ctx.args[0])
    except: return
    with closing(sqlite3.connect(DB_PATH)) as db:
        rows = db.execute("SELECT role, text FROM messages WHERE uid=? ORDER BY id DESC LIMIT 20", (uid,)).fetchall()
    for r, t in reversed(rows):
        await update.message.reply_text(f"{r.upper()}: {t[:1000]}")
        await asyncio.sleep(0.05)

async def ban_cmd(update, ctx):
    if update.effective_user.id != ADMIN_ID or not ctx.args: return
    try: uid = int(ctx.args[0])
    except: return
    set_ban(uid, "Забанен админом.")
    await update.message.reply_text(f"ID {uid} забанен.")

async def unban_cmd(update, ctx):
    if update.effective_user.id != ADMIN_ID or not ctx.args: return
    try: uid = int(ctx.args[0])
    except: return
    set_ban(uid)
    await update.message.reply_text(f"ID {uid} разбанен.")

async def prompt_cmd(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    await update.message.reply_text("Отправь новый промт (или 'отмена'):")
    return WAIT_PROMPT

async def receive_prompt(update, ctx):
    if update.effective_user.id != ADMIN_ID: return ConversationHandler.END
    if update.message.text.lower() == "отмена": return ConversationHandler.END
    set_setting("sys_prompt", update.message.text)
    await update.message.reply_text("✅ Промт обновлен.")
    return ConversationHandler.END

async def mail_cmd(update, ctx):
    if update.effective_user.id != ADMIN_ID or not ctx.args: return
    try: ctx.user_data["mail_target"] = int(ctx.args[0])
    except: return
    await update.message.reply_text("Текст/фото для юзера:")
    return WAIT_REPLY

async def trigger_cmd(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    await update.message.reply_text("Введи слово (или 'отмена'):")
    return WAIT_TRIGGER_W

async def receive_trig_w(update, ctx):
    if update.effective_user.id != ADMIN_ID: return ConversationHandler.END
    if update.message.text.lower() == "отмена": return ConversationHandler.END
    ctx.user_data["tw"] = update.message.text.lower()
    await update.message.reply_text("Теперь ответ:")
    return WAIT_TRIGGER_A

async def receive_trig_a(update, ctx):
    if update.effective_user.id != ADMIN_ID: return ConversationHandler.END
    with closing(sqlite3.connect(DB_PATH)) as db:
        db.execute("INSERT OR REPLACE INTO triggers (word, answer) VALUES (?, ?)", (ctx.user_data["tw"], update.message.text))
        db.commit()
    await update.message.reply_text("✅ Триггер добавлен.")
    return ConversationHandler.END

async def note_cmd(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    await update.message.reply_text("Текст заметки (или 'отмена'):")
    return WAIT_NOTE

async def receive_note(update, ctx):
    if update.effective_user.id != ADMIN_ID: return ConversationHandler.END
    if update.message.text.lower() == "отмена": return ConversationHandler.END
    with closing(sqlite3.connect(DB_PATH)) as db:
        db.execute("INSERT INTO notes (text) VALUES (?)", (update.message.text,))
        db.commit()
    await update.message.reply_text("✅ Заметка сохранена.")
    return ConversationHandler.END

async def notes_cmd(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    with closing(sqlite3.connect(DB_PATH)) as db:
        rows = db.execute("SELECT id, text FROM notes ORDER BY id DESC LIMIT 10").fetchall()
    if not rows: return await update.message.reply_text("Заметок нет.")
    await update.message.reply_text("🗒 *Заметки:*\n" + "\n".join(f"{r[0]}. {r[1]}" for r in rows), parse_mode=ParseMode.MARKDOWN)

async def set_news(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    await update.message.reply_text("Текст новости (или 'отмена'):")
    return WAIT_NEWS

async def receive_news(update, ctx):
    if update.effective_user.id != ADMIN_ID: return ConversationHandler.END
    if update.message.text.lower() == "отмена": return ConversationHandler.END
    set_setting("latest_news", update.message.text)
    await update.message.reply_text("✅ Новость обновлена.")
    return ConversationHandler.END

async def broadcast(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    await update.message.reply_text("Текст для рассылки (или 'отмена'):")
    return WAIT_BROADCAST

async def receive_broadcast(update, ctx):
    if update.effective_user.id != ADMIN_ID: return ConversationHandler.END
    if update.message.text.lower() == "отмена": return ConversationHandler.END
    users = get_all_users()
    msg = await update.message.reply_text(f"⏳ Рассылка на {len(users)}...")
    ok = 0
    for u in users:
        try:
            await ctx.bot.send_message(u, f"📢 *Сообщение от создателя:*\n\n{update.message.text}", parse_mode=ParseMode.MARKDOWN)
            ok += 1; await asyncio.sleep(0.05)
        except: pass
    await msg.edit_text(f"✅ Доставлено: {ok} из {len(users)}")
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
        ctx.user_data["mail_target"] = int(q.data.split("_")[1])
        await q.message.reply_text("Текст/фото (или 'отмена'):")
        return WAIT_REPLY
    elif q.data.startswith("hist_"):
        uid = int(q.data.split("_")[1])
        with closing(sqlite3.connect(DB_PATH)) as db:
            rows = db.execute("SELECT role, text FROM messages WHERE uid=? ORDER BY id DESC LIMIT 15", (uid,)).fetchall()
        for r, t in reversed(rows):
            await ctx.bot.send_message(ADMIN_ID, f"{r.upper()}: {t[:1000]}")
            await asyncio.sleep(0.05)
    elif q.data.startswith("quiz_"):
        await q.message.reply_text("✅ Верно!" if q.data == "quiz_True" else "❌ Неверно.")
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
    uid = ctx.user_data.get("mail_target")
    if not uid: return ConversationHandler.END
    try:
        if update.message.text: await ctx.bot.send_message(uid, update.message.text)
        elif update.message.photo: await ctx.bot.send_photo(uid, update.message.photo[-1].file_id, caption=update.message.caption or "")
        await update.message.reply_text("✅ Отправлено.")
    except Exception as e: await update.message.reply_text(f"Ошибка: {e}")
    return ConversationHandler.END

async def cancel_conv(update, ctx):
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END

async def error_handler(update, ctx):
    log.exception("Ошибка:", exc_info=ctx.error)

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
            CommandHandler("trigger", trigger_cmd), CommandHandler("prompt", prompt_cmd),
            CommandHandler("note", note_cmd), CommandHandler("mail", mail_cmd)
        ],
        states={
            WAIT_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(ADMIN_ID), receive_reason)],
            WAIT_REPLY: [MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND & filters.User(ADMIN_ID), receive_reply)],
            WAIT_BROADCAST: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(ADMIN_ID), receive_broadcast)],
            WAIT_NEWS: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(ADMIN_ID), receive_news)],
            WAIT_TRIGGER_W: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(ADMIN_ID), receive_trig_w)],
            WAIT_TRIGGER_A: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(ADMIN_ID), receive_trig_a)],
            WAIT_PROMPT: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(ADMIN_ID), receive_prompt)],
            WAIT_NOTE: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(ADMIN_ID), receive_note)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conv)], allow_reentry=True,
    )

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
    
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("users", users_list))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("mute", mute_cmd))
    app.add_handler(CommandHandler("backup", backup))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CommandHandler("notes", notes_cmd))

    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(admin_callback))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    app.add_error_handler(error_handler)
    log.info("VylaDigital запущен. Админ: %s", ADMIN_ID)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
