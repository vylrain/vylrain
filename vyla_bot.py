import asyncio
import logging
import os
import urllib.parse
import sqlite3

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

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

ADMIN_ID = int(os.environ.get("ADMIN_ID", "8582945012"))
LOG_CHANNEL_ID = int(os.environ.get("LOG_CHANNEL_ID", "0"))

DB_PATH = "vyla.db"

MODEL = os.environ.get(
    "VYLA_MODEL",
    "openai/gpt-oss-120b",
)

VISION_MODEL = os.environ.get(
    "VYLA_VISION_MODEL",
    "llama-3.2-11b-vision-preview",
)

MAX_HISTORY = 20
MAX_TG_LEN = 4000

WAIT_REASON = 1
WAIT_REPLY = 2

client = AsyncOpenAI(
    api_key=OPENAI_API_KEY,
    base_url=os.environ.get(
        "OPENAI_BASE_URL",
        "https://api.groq.com/openai/v1",
    ),
)

_history = defaultdict(lambda: deque(maxlen=MAX_HISTORY))
_locks = defaultdict(asyncio.Lock)


# =========================
# AI PERSONALITY
# =========================

SYSTEM_PROMPT = (
    "Ты VylaDigital — ИИ-ассистент, созданный vylrein.\n"
    "Твой создатель — школьник и энтузиаст, "
    "самостоятельно занимающийся электроникой и программированием.\n"
    "Если спрашивают о создателе, отвечай: "
    "'Меня создал vylrein — школьник и энтузиаст, "
    "занимающийся электроникой и программированием.'\n"
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
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                uid INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                first_seen TEXT,
                last_seen TEXT,
                msg_count INTEGER DEFAULT 0,
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

        db.commit()


def get_user(uid):
    with closing(sqlite3.connect(DB_PATH)) as db:
        return db.execute(
            "SELECT * FROM users WHERE uid=?",
            (uid,),
        ).fetchone()


def is_banned(uid):
    user = get_user(uid)
    return bool(user and user[6] == 1)


def get_ban_reason(uid):
    user = get_user(uid)
    return user[7] if user else None


def log_user(uid, username, first_name):
    now = datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M"
    )

    with closing(sqlite3.connect(DB_PATH)) as db:
        row = db.execute(
            "SELECT uid FROM users WHERE uid=?",
            (uid,),
        ).fetchone()

        if row:
            db.execute("""
                UPDATE users
                SET last_seen=?,
                    msg_count=msg_count+1,
                    username=?,
                    first_name=?
                WHERE uid=?
            """, (
                now,
                username or "",
                first_name or "",
                uid,
            ))
        else:
            db.execute("""
                INSERT INTO users (
                    uid,
                    username,
                    first_name,
                    first_seen,
                    last_seen,
                    msg_count
                )
                VALUES (?, ?, ?, ?, ?, 1)
            """, (
                uid,
                username or "",
                first_name or "",
                now,
                now,
            ))

        db.commit()


def log_msg(uid, role, text):
    now = datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M"
    )

    with closing(sqlite3.connect(DB_PATH)) as db:
        db.execute(
            """
            INSERT INTO messages(uid, role, text, ts)
            VALUES (?, ?, ?, ?)
            """,
            (uid, role, text, now),
        )

        db.commit()


def set_ban(uid, reason=None):
    with closing(sqlite3.connect(DB_PATH)) as db:
        if reason is not None:
            db.execute(
                """
                UPDATE users
                SET is_banned=1, ban_reason=?
                WHERE uid=?
                """,
                (reason, uid),
            )
        else:
            db.execute(
                """
                UPDATE users
                SET is_banned=0, ban_reason=NULL
                WHERE uid=?
                """,
                (uid,),
            )

        db.commit()


# =========================
# TEXT UTILITIES
# =========================

def split_text(text):
    parts = []
    buf = text

    while len(buf) > MAX_TG_LEN:
        cut = buf.rfind("\n", 0, MAX_TG_LEN)

        if cut < MAX_TG_LEN // 2:
            cut = buf.rfind(" ", 0, MAX_TG_LEN)

        if cut <= 0:
            cut = MAX_TG_LEN

        parts.append(buf[:cut])
        buf = buf[cut:].lstrip()

    if buf:
        parts.append(buf)

    return parts


async def send_long_message(message, text):
    for part in split_text(text):
        await message.reply_text(part)


# =========================
# LOGGING
# =========================

async def send_log(ctx, text, kb=None):
    target = LOG_CHANNEL_ID if LOG_CHANNEL_ID else ADMIN_ID

    try:
        await ctx.bot.send_message(
            chat_id=target,
            text=text[:4000],
            reply_markup=kb,
        )
    except Exception:
        log.exception("Logging error")


async def log_user_message(update, ctx, text):
    user = update.effective_user

    if user.id == ADMIN_ID and not LOG_CHANNEL_ID:
        return

    tag = (
        f"@{user.username}"
        if user.username
        else user.first_name
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "Забанить",
            callback_data=f"ban_{user.id}",
        ),
        InlineKeyboardButton(
            "История",
            callback_data=f"hist_{user.id}",
        ),
        InlineKeyboardButton(
            "Ответить",
            callback_data=f"reply_{user.id}",
        ),
    ]])

    await send_log(
        ctx,
        f"Пользователь: {tag}\n"
        f"ID: {user.id}\n\n"
        f"{text}",
        keyboard,
    )


# =========================
# AI CHAT
# =========================

async def ask_llm(update, messages):
    await update.message.chat.send_action(
        ChatAction.TYPING
    )

    placeholder = await update.message.reply_text(
        "Думаю..."
    )

    buf = ""
    last = 0.0
    loop = asyncio.get_running_loop()

    try:
        stream = await client.chat.completions.create(
            model=MODEL,
            messages=messages,
            stream=True,
            temperature=0.7,
        )

        async for chunk in stream:
            delta = (
                chunk.choices[0].delta.content
                if chunk.choices
                else None
            )

            if not delta:
                continue

            buf += delta

            if (
                loop.time() - last > 1.2
                and len(buf) <= MAX_TG_LEN
            ):
                last = loop.time()

                with suppress(Exception):
                    await placeholder.edit_text(buf)

    except Exception as e:
        log.exception("LLM error")

        with suppress(Exception):
            await placeholder.edit_text(
                f"Ошибка ИИ: {type(e).__name__}\n"
                f"{str(e)[:1000]}"
            )

        return None

    if not buf.strip():
        await placeholder.edit_text(
            "ИИ вернул пустой ответ."
        )
        return None

    parts = split_text(buf)

    with suppress(Exception):
        await placeholder.edit_text(parts[0])

    for part in parts[1:]:
        await update.message.reply_text(part)

    return buf


# =========================
# COMMANDS
# =========================

async def post_init(app):
    user_commands = [
        BotCommand("start", "Запустить бота"),
        BotCommand("draw", "Создать изображение"),
        BotCommand("reset", "Очистить память"),
    ]

    await app.bot.set_my_commands(user_commands)

    admin_commands = user_commands + [
        BotCommand("admin", "Админ-панель"),
        BotCommand("unban", "Разблокировать пользователя"),
        BotCommand("history", "История сообщений"),
    ]

    try:
        await app.bot.set_my_commands(
            admin_commands,
            scope=BotCommandScopeChat(
                chat_id=ADMIN_ID
            ),
        )
    except Exception:
        log.exception("Admin commands error")


async def start(update, ctx):
    user = update.effective_user

    log_user(
        user.id,
        user.username,
        user.first_name,
    )

    if is_banned(user.id):
        reason = get_ban_reason(user.id) or "Не указана"

        await update.message.reply_text(
            f"Ваш аккаунт заблокирован.\n"
            f"Причина: {reason}"
        )
        return

    _history[user.id].clear()

    await update.message.reply_text(
        "*VylaDigital на связи.*\n"
        "Пиши вопрос, отправляй фото или используй /draw.\n\n"
        "/reset — очистить память.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def reset(update, ctx):
    uid = update.effective_user.id

    if is_banned(uid):
        await update.message.reply_text(
            "Вы заблокированы."
        )
        return

    _history[uid].clear()

    await update.message.reply_text(
        "Память очищена."
    )


# =========================
# TEXT MESSAGES
# =========================

async def on_message(update, ctx):
    user = update.effective_user
    text = update.message.text

    if not text:
        return

    log_user(
        user.id,
        user.username,
        user.first_name,
    )

    if is_banned(user.id):
        reason = get_ban_reason(user.id) or "Не указана"

        await update.message.reply_text(
            f"Вы заблокированы.\nПричина: {reason}"
        )
        return

    log_msg(user.id, "user", text)

    await log_user_message(update, ctx, text)

    async with _locks[user.id]:
        history = _history[user.id]

        messages = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            },
            *history,
            {
                "role": "user",
                "content": text,
            },
        ]

        answer = await ask_llm(
            update,
            messages,
        )

        if answer:
            history.append({
                "role": "user",
                "content": text,
            })

            history.append({
                "role": "assistant",
                "content": answer,
            })

            log_msg(
                user.id,
                "bot",
                answer,
            )


# =========================
# PHOTO RECOGNITION
# =========================

async def on_photo(update, ctx):
    user = update.effective_user

    log_user(
        user.id,
        user.username,
        user.first_name,
    )

    if is_banned(user.id):
        await update.message.reply_text(
            "Вы заблокированы."
        )
        return

    file_id = update.message.photo[-1].file_id

    caption = (
        update.message.caption
        or "Что на этом фото? Опиши подробно."
    )

    log_msg(
        user.id,
        "user",
        f"[ФОТО] {caption}",
    )

    await log_user_message(
        update,
        ctx,
        f"[ФОТО] {caption}",
    )

    placeholder = await update.message.reply_text(
        "Смотрю фото..."
    )

    try:
        tg_file = await ctx.bot.get_file(file_id)

        image_url = tg_file.file_path

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
                            "url": image_url,
                        },
                    },
                ],
            }],
            temperature=0.5,
        )

        answer = (
            response.choices[0].message.content
            or "Не удалось распознать фото."
        )

        log_msg(
            user.id,
            "bot",
            answer,
        )

        parts = split_text(answer)

        with suppress(Exception):
            await placeholder.edit_text(parts[0])

        for part in parts[1:]:
            await update.message.reply_text(part)

    except Exception as e:
        log.exception("Vision error")

        await placeholder.edit_text(
            f"Ошибка распознавания: {type(e).__name__}\n"
            f"{str(e)[:1000]}"
        )


# =========================
# IMAGE GENERATION
# =========================

async def draw(update, ctx):
    user = update.effective_user

    log_user(
        user.id,
        user.username,
        user.first_name,
    )

    if is_banned(user.id):
        await update.message.reply_text(
            "Вы заблокированы."
        )
        return

    user_prompt = " ".join(ctx.args).strip()

    if not user_prompt:
        await update.message.reply_text(
            "Использование:\n"
            "/draw кот в космосе"
        )
        return

    if user.id != ADMIN_ID or LOG_CHANNEL_ID:
        await send_log(
            ctx,
            f"Запрос на изображение от {user.id}:\n"
            f"{user_prompt}",
        )

    await update.message.chat.send_action(
        ChatAction.UPLOAD_PHOTO
    )

    placeholder = await update.message.reply_text(
        "Готовлю промт и рисую..."
    )

    try:
        try:
            enhancer = await client.chat.completions.create(
                model=MODEL,
                messages=[{
                    "role": "user",
                    "content": (
                        "Convert the user's image request into a "
                        "detailed English image generation prompt. "
                        "Describe the subject, composition, lighting "
                        "and visual style. Return only the prompt.\n"
                        f"Request: {user_prompt}"
                    ),
                }],
                temperature=0.7,
            )

            final_prompt = (
                enhancer.choices[0].message.content
                or user_prompt
            ).strip()

        except Exception:
            log.exception(
                "Prompt enhancement error"
            )
            final_prompt = user_prompt

        encoded = urllib.parse.quote(
            final_prompt
        )

        url = (
            f"https://image.pollinations.ai/prompt/{encoded}"
            "?width=1024&height=1024&nologo=true&safe=true"
        )

        import httpx

        async with httpx.AsyncClient(
            timeout=90.0
        ) as http:
            response = await http.get(url)
            response.raise_for_status()

            if len(response.content) < 1000:
                raise ValueError(
                    "Сервер вернул пустую картинку"
                )

            await update.message.reply_photo(
                photo=response.content,
                caption=f"Результат: {user_prompt}"[:1000],
            )

        await placeholder.delete()

    except Exception as e:
        log.exception("Draw error")

        await placeholder.edit_text(
            f"Ошибка генерации: {type(e).__name__}\n"
            f"{str(e)[:500]}"
        )


# =========================
# HISTORY COMMAND
# =========================

async def history_command(update, ctx):
    if update.effective_user.id != ADMIN_ID:
        return

    if not ctx.args:
        await update.message.reply_text(
            "Использование:\n"
            "/history ID\n\n"
            "Например:\n"
            "/history 123456789"
        )
        return

    try:
        uid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text(
            "Некорректный ID."
        )
        return

    user = get_user(uid)

    if not user:
        await update.message.reply_text(
            "Пользователь не найден в базе."
        )
        return

    with closing(sqlite3.connect(DB_PATH)) as db:
        rows = db.execute("""
            SELECT role, text, ts
            FROM messages
            WHERE uid=?
            ORDER BY id DESC
            LIMIT 100
        """, (uid,)).fetchall()

    if not rows:
        await update.message.reply_text(
            "История сообщений не найдена.\n"
            "Возможно, старая версия бота не сохраняла сообщения."
        )
        return

    username = user[1] or user[2] or str(uid)

    history_text = (
        f"История пользователя: {username}\n"
        f"ID: {uid}\n\n"
    )

    for role, text, ts in reversed(rows):
        history_text += (
            f"[{ts}] {role.upper()}:\n"
            f"{text}\n\n"
        )

    for part in split_text(history_text):
        await update.message.reply_text(part)


# =========================
# ADMIN CALLBACKS
# =========================

async def admin_callback(update, ctx):
    query = update.callback_query

    if query.from_user.id != ADMIN_ID:
        await query.answer(
            "У вас нет прав.",
            show_alert=True,
        )
        return ConversationHandler.END

    await query.answer()

    data = query.data

    if data.startswith("ban_"):
        uid = int(data.split("_", 1)[1])

        ctx.user_data["ban_target"] = uid

        await query.message.reply_text(
            f"Введите причину бана для ID {uid}:\n"
            "Или напишите «отмена»."
        )

        return WAIT_REASON

    elif data.startswith("reply_"):
        uid = int(data.split("_", 1)[1])

        ctx.user_data["reply_target"] = uid

        await query.message.reply_text(
            f"Напишите текст или отправьте фото для ID {uid}:\n"
            "Или напишите «отмена»."
        )

        return WAIT_REPLY

    elif data.startswith("hist_"):
        uid = int(data.split("_", 1)[1])

        with closing(sqlite3.connect(DB_PATH)) as db:
            rows = db.execute("""
                SELECT role, text, ts
                FROM messages
                WHERE uid=?
                ORDER BY id DESC
                LIMIT 20
            """, (uid,)).fetchall()

        if not rows:
            await query.message.reply_text(
                "История не найдена."
            )
            return ConversationHandler.END

        history_text = "\n\n".join(
            f"[{ts}] {role.upper()}:\n{text[:500]}"
            for role, text, ts in reversed(rows)
        )

        await send_long_message(
            query.message,
            f"История пользователя {uid}:\n\n{history_text}",
        )

    return ConversationHandler.END


# =========================
# BAN REASON
# =========================

async def receive_reason(update, ctx):
    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END

    text = (
        update.message.text or ""
    ).strip()

    if text.lower() == "отмена":
        await update.message.reply_text(
            "Отменено."
        )
        return ConversationHandler.END

    uid = ctx.user_data.get("ban_target")

    if not uid or not get_user(uid):
        await update.message.reply_text(
            "Пользователь не найден."
        )
        return ConversationHandler.END

    set_ban(uid, text)
    _history[uid].clear()

    await update.message.reply_text(
        f"Пользователь {uid} заблокирован."
    )

    try:
        await ctx.bot.send_message(
            uid,
            f"Ваш аккаунт заблокирован.\n"
            f"Причина: {text}",
        )
    except Exception:
        log.warning(
            "Не удалось уведомить пользователя %s",
            uid,
        )

    return ConversationHandler.END


# =========================
# ADMIN REPLY
# =========================

async def receive_reply(update, ctx):
    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END

    uid = ctx.user_data.get("reply_target")

    if not uid:
        return ConversationHandler.END

    if (
        update.message.text
        and update.message.text.lower() == "отмена"
    ):
        await update.message.reply_text(
            "Отменено."
        )
        return ConversationHandler.END

    try:
        if update.message.text:
            await ctx.bot.send_message(
                chat_id=uid,
                text=update.message.text,
            )

        elif update.message.photo:
            file_id = update.message.photo[-1].file_id

            await ctx.bot.send_photo(
                chat_id=uid,
                photo=file_id,
                caption=update.message.caption or "",
            )

        else:
            await update.message.reply_text(
                "Поддерживаются текст и фотографии."
            )
            return WAIT_REPLY

        await update.message.reply_text(
            "Сообщение отправлено."
        )

    except Exception as e:
        await update.message.reply_text(
            f"Ошибка отправки: {type(e).__name__}"
        )

    return ConversationHandler.END


# =========================
# ADMIN PANEL
# =========================

async def admin_panel(update, ctx):
    if update.effective_user.id != ADMIN_ID:
        return

    with closing(sqlite3.connect(DB_PATH)) as db:
        users = db.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()[0]

        banned = db.execute(
            "SELECT COUNT(*) FROM users WHERE is_banned=1"
        ).fetchone()[0]

        messages = db.execute(
            "SELECT COUNT(*) FROM messages"
        ).fetchone()[0]

    await update.message.reply_text(
        f"АДМИН-ПАНЕЛЬ\n\n"
        f"Пользователей: {users}\n"
        f"Заблокировано: {banned}\n"
        f"Записей в истории: {messages}\n\n"
        "Команды:\n"
        "/history ID — история пользователя\n"
        "/unban ID — разблокировать пользователя"
    )


async def unban_command(update, ctx):
    if update.effective_user.id != ADMIN_ID:
        return

    if not ctx.args:
        await update.message.reply_text(
            "Использование: /unban ID"
        )
        return

    try:
        uid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text(
            "Некорректный ID."
        )
        return

    if not get_user(uid):
        await update.message.reply_text(
            "Пользователь не найден."
        )
        return

    set_ban(uid)

    await update.message.reply_text(
        f"Пользователь {uid} разблокирован."
    )

    try:
        await ctx.bot.send_message(
            uid,
            "Ваш аккаунт разблокирован.",
        )
    except Exception:
        pass


# =========================
# CANCEL
# =========================

async def cancel_conv(update, ctx):
    await update.message.reply_text(
        "Действие отменено."
    )

    return ConversationHandler.END


# =========================
# ERROR HANDLER
# =========================

async def error_handler(update, ctx):
    log.exception(
        "Ошибка при обработке обновления:",
        exc_info=ctx.error,
    )


# =========================
# START BOT
# =========================

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
                pattern=r"^ban_\d+$",
            ),
            CallbackQueryHandler(
                admin_callback,
                pattern=r"^reply_\d+$",
            ),
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
                    filters.TEXT & ~filters.COMMAND,
                    receive_reply,
                ),
                MessageHandler(
                    filters.PHOTO,
                    receive_reply,
                ),
            ],
        },
        fallbacks=[
            CommandHandler(
                "cancel",
                cancel_conv,
            ),
        ],
        allow_reentry=True,
    )

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        CommandHandler("reset", reset)
    )

    app.add_handler(
        CommandHandler("draw", draw)
    )

    app.add_handler(
        CommandHandler("admin", admin_panel)
    )

    app.add_handler(
        CommandHandler("unban", unban_command)
    )

    app.add_handler(
        CommandHandler("history", history_command)
    )

    app.add_handler(admin_conv)

    app.add_handler(
        CallbackQueryHandler(admin_callback)
    )

    app.add_handler(
        MessageHandler(
            filters.PHOTO,
            on_photo,
        )
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
