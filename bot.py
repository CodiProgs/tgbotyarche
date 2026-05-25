import asyncio
import logging
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

import aiosqlite
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandStart
from aiogram.exceptions import TelegramBadRequest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = "bot_data.db"
SCHEDULE_TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow").strip()
DEFAULT_HOUR = int(os.getenv("SCHED_HOUR", 10))
DEFAULT_MINUTE = int(os.getenv("SCHED_MIN", 0))

ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = set()
if ADMIN_IDS_RAW.strip():
    for uid in ADMIN_IDS_RAW.split(","):
        uid = uid.strip()
        if uid.isdigit():
            ADMIN_IDS.add(int(uid))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

# 🔘 Постоянная клавиатура для всех пользователей
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="▶️ Начать"), KeyboardButton(text="⏹️ Закончить")]
    ],
    resize_keyboard=True,
    one_time_keyboard=False,
    input_field_placeholder="Выберите действие"
)


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                step INTEGER DEFAULT 1,
                active INTEGER DEFAULT 1,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS content (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT,
                media_type TEXT DEFAULT 'text',
                file_id TEXT,
                position INTEGER UNIQUE NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('sched_hour', ?)", (DEFAULT_HOUR,))
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('sched_min', ?)", (DEFAULT_MINUTE,))
        
        await db.execute("UPDATE users SET step = MAX(step, 1), active = 1")
        
        await db.commit()
    logging.info("✅ База данных инициализирована")


def is_admin(user_id: int) -> bool:
    try:
        creator_id = int(BOT_TOKEN.split(":")[0])
        return user_id in ADMIN_IDS or user_id == creator_id
    except (ValueError, IndexError):
        return user_id in ADMIN_IDS


async def add_user(user_id: int, username: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO users (user_id, username, step, active) VALUES (?, ?, MAX(1, COALESCE((SELECT step FROM users WHERE user_id = ?), 0)), 1)",
            (user_id, username, user_id)
        )
        await db.commit()


async def get_active_users():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, step FROM users WHERE active = 1") as cursor:
            return await cursor.fetchall()


async def update_step(user_id: int, new_step: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET step = ? WHERE user_id = ?", (new_step, user_id))
        await db.commit()


async def deactivate_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET active = 0 WHERE user_id = ?", (user_id,))
        await db.commit()


async def add_content(text: str = None, media_type: str = "text", file_id: str = None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COALESCE(MAX(position), 0) FROM content") as cursor:
            result = await cursor.fetchone()
            next_pos = (result[0] or 0) + 1
        await db.execute(
            "INSERT INTO content (text, media_type, file_id, position) VALUES (?, ?, ?, ?)",
            (text, media_type, file_id, next_pos)
        )
        await db.commit()
        return next_pos


async def get_content_by_position(pos: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT text, media_type, file_id FROM content WHERE position = ?", (pos,)
        ) as cursor:
            result = await cursor.fetchone()
            if result:
                return {
                    "text": result[0],
                    "media_type": result[1] or "text",
                    "file_id": result[2]
                }
            return None


async def get_all_content():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT position, text, media_type, file_id FROM content ORDER BY position"
        ) as cursor:
            return await cursor.fetchall()


async def delete_content_by_position(pos: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM content WHERE position = ?", (pos,))
        await db.commit()
        await db.execute("UPDATE content SET position = position - 1 WHERE position > ?", (pos,))
        await db.commit()
        return cursor.rowcount > 0


async def edit_content_by_position(pos: int, text: str = None, media_type: str = None, file_id: str = None) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        if text is not None and media_type is not None and file_id is not None:
            await db.execute(
                "UPDATE content SET text = ?, media_type = ?, file_id = ? WHERE position = ?",
                (text, media_type, file_id, pos)
            )
        elif text is not None:
            await db.execute("UPDATE content SET text = ? WHERE position = ?", (text, pos))
        await db.commit()
        return True


async def get_content_count():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM content") as cursor:
            result = await cursor.fetchone()
            return result[0] if result else 0


async def get_schedule():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT key, value FROM settings WHERE key IN ('sched_hour', 'sched_min')") as cursor:
            rows = await cursor.fetchall()
            hour, minute = DEFAULT_HOUR, DEFAULT_MINUTE
            for key, value in rows:
                try:
                    if key == 'sched_hour':
                        hour = int(value)
                    elif key == 'sched_min':
                        minute = int(value)
                except (ValueError, TypeError):
                    pass
            return hour, minute
    return DEFAULT_HOUR, DEFAULT_MINUTE


async def update_schedule(hour: int, minute: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('sched_hour', ?)", (hour,))
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('sched_min', ?)", (minute,))
        await db.commit()


# 🔘 Обработчик кнопки "▶️ Начать"
@dp.message(F.text == "▶️ Начать")
async def btn_start(message: types.Message):
    await add_user(message.from_user.id, message.from_user.username)
    count = await get_content_count()
    h, m = await get_schedule()
    await message.answer(
        f"✅ Вы подписаны!\n"
        f"Вас ждёт {count} сообщений.\n"
        f"Рассылка: ежедневно в {h:02d}:{m:02d} ({SCHEDULE_TIMEZONE}).",
        reply_markup=MAIN_KEYBOARD
    )


# 🔘 Обработчик кнопки "⏹️ Закончить"
@dp.message(F.text == "⏹️ Закончить")
async def btn_stop(message: types.Message):
    await deactivate_user(message.from_user.id)
    await message.answer(
        "🔕 Рассылка остановлена. Нажмите «▶️ Начать», чтобы вернуться.",
        reply_markup=MAIN_KEYBOARD
    )


# ✅ /start тоже работает (для совместимости)
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await add_user(message.from_user.id, message.from_user.username)
    count = await get_content_count()
    h, m = await get_schedule()
    await message.answer(
        f"✅ Вы подписаны!\n"
        f"Вас ждёт {count} сообщений.\n"
        f"Рассылка: ежедневно в {h:02d}:{m:02d} ({SCHEDULE_TIMEZONE}).",
        reply_markup=MAIN_KEYBOARD
    )


# ✅ /stop тоже работает (для совместимости)
@dp.message(F.text == "/stop")
async def cmd_stop(message: types.Message):
    await deactivate_user(message.from_user.id)
    await message.answer(
        "🔕 Рассылка остановлена. Нажмите «▶️ Начать», чтобы вернуться.",
        reply_markup=MAIN_KEYBOARD
    )


# 🔘 Любое текстовое сообщение — показываем клавиатуру (чтобы она "прилипла")
@dp.message(F.text)
async def any_text_with_keyboard(message: types.Message):
    # Не отвечаем на команды админа, чтобы не спамить
    if message.text.startswith('/') and is_admin(message.from_user.id):
        return
    # Если пользователь уже нажал кнопку — обработано выше
    if message.text in ["▶️ Начать", "⏹️ Закончить"]:
        return
    # Просто показываем клавиатуру в ответ на любое сообщение
    await message.answer("👆 Используйте кнопки ниже:", reply_markup=MAIN_KEYBOARD)


@dp.message(Command("add_user"))
async def cmd_add_user(message: types.Message):
    if not is_admin(message.from_user.id):
        return await message.answer("❌ Доступ запрещён")
    
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.answer("📝 Использование: /add_user 123456789")
    
    target = args[1].strip()
    if target.isdigit():
        user_id = int(target)
        await add_user(user_id)
        await message.answer(f"✅ Пользователь {user_id} добавлен в рассылку")
    else:
        await message.answer("⚠️ Укажите числовой ID пользователя")


@dp.message(Command("add_post"))
async def cmd_add_post(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    
    raw_text = message.caption if message.caption else message.text
    
    text = None
    if raw_text:
        parts = raw_text.split(maxsplit=1)
        if len(parts) > 1 and parts[0].lower().startswith('/add_post'):
            text = parts[1].strip()
        elif len(parts) > 0 and not parts[0].lower().startswith('/add_post'):
            text = raw_text.strip()
    
    media_type = "text"
    file_id = None
    
    if message.photo:
        media_type = "photo"
        file_id = message.photo[-1].file_id
    elif message.video:
        media_type = "video"
        file_id = message.video.file_id
    elif message.document:
        media_type = "document"
        file_id = message.document.file_id
    elif message.audio:
        media_type = "audio"
        file_id = message.audio.file_id
    elif message.voice:
        media_type = "voice"
        file_id = message.voice.file_id
    
    if not text and media_type != "text":
        text = None
    
    pos = await add_content(text=text, media_type=media_type, file_id=file_id)
    
    media_emoji = {"photo": "📷", "video": "🎥", "document": "📄", "audio": "🎵", "voice": "🎤"}
    emoji = media_emoji.get(media_type, "📝")
    
    await message.answer(f"{emoji} Пост #{pos} добавлен ({media_type})")


@dp.message(Command("posts"))
async def cmd_list_posts(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    
    posts = await get_all_content()
    if not posts:
        return await message.answer("📭 Постов пока нет")
    
    media_emoji = {"photo": "📷", "video": "🎥", "document": "📄", "audio": "🎵", "voice": "🎤", "text": "📝"}
    
    result = "📋 Список постов:\n"
    for p in posts:
        pos, text, media_type, file_id = p
        emoji = media_emoji.get(media_type, "📝")
        preview = text[:50] + "..." if text and len(text) > 50 else (text or "[без текста]")
        result += f"{emoji} #{pos}: {preview} ({media_type})\n"
    
    await message.answer(result)


@dp.message(Command("delete_post"))
async def cmd_delete_post(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        return await message.answer("📝 Использование: /delete_post <номер>")
    
    pos = int(args[1])
    if await delete_content_by_position(pos):
        await message.answer(f"✅ Пост #{pos} удалён. Позиции обновлены.")
    else:
        await message.answer(f"❌ Пост #{pos} не найден")


@dp.message(Command("edit_post"))
async def cmd_edit_post(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    
    raw_text = message.caption if message.caption else message.text
    
    new_text = None
    if raw_text:
        match = re.match(r'^/edit_post\s+(\d+)\s*(.+)?$', raw_text, re.DOTALL)
        if match:
            pos = int(match.group(1))
            new_text = match.group(2).strip() if match.group(2) else None
            
            media_type = None
            file_id = None
            
            if message.photo:
                media_type = "photo"
                file_id = message.photo[-1].file_id
            elif message.video:
                media_type = "video"
                file_id = message.video.file_id
            elif message.document:
                media_type = "document"
                file_id = message.document.file_id
            
            if await edit_content_by_position(pos, text=new_text, media_type=media_type, file_id=file_id):
                await message.answer(f"✅ Пост #{pos} обновлён")
            else:
                await message.answer(f"❌ Пост #{pos} не найден")
        else:
            await message.answer("📝 Использование:\n/edit_post <номер> <новый текст>")
    else:
        await message.answer("📝 Использование:\n/edit_post <номер> <новый текст>")


@dp.message(Command("schedule"))
async def cmd_schedule(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    
    args = message.text.split()
    if len(args) < 3 or not args[1].isdigit() or not args[2].isdigit():
        h, m = await get_schedule()
        return await message.answer(f"⏰ Текущее: {h:02d}:{m:02d}\n📝 Изменить: /schedule <час> <мин>")
    
    hour, minute = int(args[1]), int(args[2])
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return await message.answer("❌ Некорректное время")
    
    await update_schedule(hour, minute)
    scheduler.remove_all_jobs()
    await setup_scheduler()
    await message.answer(f"✅ Время изменено на {hour:02d}:{minute:02d}")


@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users WHERE active=1") as c:
            active = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM content") as c:
            posts = (await c.fetchone())[0]
        async with db.execute("SELECT AVG(step) FROM users WHERE active=1") as c:
            avg_step = round((await c.fetchone())[0] or 0, 1)
    
    await message.answer(
        f"📊 Статистика:\n"
        f"• Активных: {active}\n"
        f"• Постов: {posts}\n"
        f"• Средний шаг: {avg_step}"
    )


@dp.message(Command("test_send"))
async def cmd_test_send(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    
    user_id = message.from_user.id
    total = await get_content_count()
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT step FROM users WHERE user_id = ?", (user_id,)) as cursor:
            result = await cursor.fetchone()
            step = result[0] if result else 1
    
    if step < 1 or step > total:
        return await message.answer(f"❌ Нечего отправлять. Шаг: {step}, постов: {total}")
    
    content = await get_content_by_position(step)
    if content:
        await send_media_message(user_id, content, test_mode=True)
        await message.answer(f"✅ Тест отправлен (шаг #{step})")
    else:
        await message.answer("❌ Пост не найден")


async def send_media_message(user_id: int, content: dict, test_mode: bool = False):
    text = content.get("text")
    media_type = content.get("media_type", "text")
    file_id = content.get("file_id")
    
    prefix = "🧪 **ТЕСТ**:\n\n" if test_mode else ""
    
    try:
        if media_type == "photo" and file_id:
            await bot.send_photo(user_id, photo=file_id, caption=prefix + (text or ""))
        elif media_type == "video" and file_id:
            await bot.send_video(user_id, video=file_id, caption=prefix + (text or ""))
        elif media_type == "document" and file_id:
            await bot.send_document(user_id, document=file_id, caption=prefix + (text or ""))
        elif media_type == "audio" and file_id:
            await bot.send_audio(user_id, audio=file_id, caption=prefix + (text or ""))
        elif media_type == "voice" and file_id:
            await bot.send_voice(user_id, voice=file_id, caption=prefix + (text or ""))
        else:
            await bot.send_message(user_id, prefix + (text or ""), parse_mode="Markdown" if test_mode else None)
    except Exception as e:
        logging.error(f"❌ Ошибка отправки медиа {media_type} для {user_id}: {e}")
        if text:
            await bot.send_message(user_id, prefix + text)


@dp.message(Command("debug"))
async def cmd_debug(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    
    now = datetime.now(ZoneInfo(SCHEDULE_TIMEZONE))
    h, m = await get_schedule()
    jobs = scheduler.get_jobs()
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT step FROM users WHERE user_id = ?", (message.from_user.id,)) as cursor:
            result = await cursor.fetchone()
            my_step = result[0] if result else None
    
    weekday_map = ["Вс", "Пн", "Вт", "Ср", "Чт", "Пт", "Сб"]
    
    await message.answer(
        f"🔍 ОТЛАДКА:\n"
        f"• Сейчас: {now.strftime('%H:%M %d.%m.%Y')} ({weekday_map[now.weekday()]})\n"
        f"• Рассылка: ежедневно в {h:02d}:{m:02d} {SCHEDULE_TIMEZONE}\n"
        f"• Ваш шаг: {my_step}\n"
        f"• Постов в БД: {await get_content_count()}\n"
        f"• Задач в планировщике: {len(jobs)}\n" +
        (f"• След. запуск: {jobs[0].next_run_time.astimezone(ZoneInfo(SCHEDULE_TIMEZONE)).strftime('%H:%M %d.%m.%Y')}\n" if jobs else "") +
        f"• ADMIN_IDS: {ADMIN_IDS}\n"
        f"• Создатель бота: {BOT_TOKEN.split(':')[0] if BOT_TOKEN else 'N/A'}"
    )


async def send_scheduled_content():
    logging.info("🔔 [JOB START] Запуск рассылки")
    
    users = await get_active_users()
    total = await get_content_count()
    
    logging.info(f"📊 Пользователей: {len(users)}, постов: {total}")
    
    if total == 0:
        logging.warning("⚠️ Нет контента")
        return

    sent_count = 0
    for user_id, step in users:
        if step < 1 or step > total:
            continue
        try:
            content = await get_content_by_position(step)
            if content:
                await send_media_message(user_id, content, test_mode=False)
                await update_step(user_id, step + 1)
                logging.info(f"📤 Шаг {step} → {user_id} ({content['media_type']})")
                sent_count += 1
            await asyncio.sleep(0.05)
        except TelegramBadRequest as e:
            if "bot was blocked" in str(e).lower():
                await deactivate_user(user_id)
                logging.warning(f"🚫 {user_id} заблокировал бота")
        except Exception as e:
            logging.error(f"❌ Ошибка {user_id}: {e}")
    
    logging.info(f"✅ [JOB END] Отправлено: {sent_count}")


async def setup_scheduler():
    hour, minute = await get_schedule()
    scheduler.remove_all_jobs()
    
    scheduler.add_job(
        send_scheduled_content,
        CronTrigger(
            hour=hour,
            minute=minute,
            timezone=ZoneInfo(SCHEDULE_TIMEZONE),
            jitter=30
        ),
        id="drip_job",
        replace_existing=True,
        misfire_grace_time=3600
    )
    logging.info(f"⏰ Планировщик: ежедневно в {hour:02d}:{minute:02d} {SCHEDULE_TIMEZONE}")


async def on_startup():
    await init_db()
    await setup_scheduler()
    scheduler.start()
    logging.info("🤖 Бот запущен")


async def on_shutdown():
    scheduler.shutdown(wait=False)
    logging.info("🛑 Бот остановлен")


async def main():
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())