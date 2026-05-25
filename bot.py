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
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

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
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()


# ==================== КЛАВИАТУРЫ ====================

def get_main_keyboard(is_active: bool) -> ReplyKeyboardMarkup:
    keyboard = [[KeyboardButton(text="⏹️ Закончить")]] if is_active else [[KeyboardButton(text="▶️ Начать")]]
    return ReplyKeyboardMarkup(
        keyboard=keyboard, resize_keyboard=True, one_time_keyboard=False,
        input_field_placeholder="Управление подпиской"
    )


def get_admin_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📝 Добавить пост"), KeyboardButton(text="📋 Все посты")],
            [KeyboardButton(text="✏️ Редактировать"), KeyboardButton(text="🗑️ Удалить пост")],
            [KeyboardButton(text="⏰ Настроить время"), KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="🔙 Обычное меню")]
        ],
        resize_keyboard=True, one_time_keyboard=False
    )


def get_post_list_keyboard(posts: list, mode: str = "edit") -> InlineKeyboardMarkup:
    """Кнопки выбора поста + предпросмотр"""
    keyboard = []
    for pos, text, media_type, _ in posts[:10]:
        emoji = {"photo": "📷", "video": "🎥", "document": "📄", "audio": "🎵", "voice": "🎤", "text": "📝"}.get(media_type, "📝")
        preview = (text or "[без текста]")[:25] + "..."
        # Две кнопки в ряд: выбор поста и предпросмотр
        keyboard.append([
            InlineKeyboardButton(text=f"{emoji} #{pos}", callback_data=f"{mode}_{pos}"),
            InlineKeyboardButton(text="👁️", callback_data=f"preview_{pos}")
        ])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_edit_options_keyboard(pos: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить текст", callback_data=f"edit_text_{pos}")],
        [InlineKeyboardButton(text="🖼️ Заменить медиа", callback_data=f"edit_media_{pos}")],
        [InlineKeyboardButton(text="👁️ Просмотреть", callback_data=f"preview_{pos}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")]
    ])


def get_confirm_delete_keyboard(pos: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"confirm_delete_{pos}")],
        [InlineKeyboardButton(text="🔙 Отмена", callback_data="admin_back")]
    ])


# ==================== FSM ====================

class AdminStates(StatesGroup):
    waiting_for_post_text = State()
    waiting_for_edit_text = State()
    waiting_for_media_replace = State()
    waiting_for_schedule_hour = State()
    waiting_for_schedule_minute = State()


# ==================== БД ====================

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, step INTEGER DEFAULT 1,
            active INTEGER DEFAULT 1, joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS content (
            id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, media_type TEXT DEFAULT 'text',
            file_id TEXT, position INTEGER UNIQUE NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)""")
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('sched_hour', ?)", (DEFAULT_HOUR,))
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('sched_min', ?)", (DEFAULT_MINUTE,))
        await db.execute("UPDATE users SET step = MAX(step, 1), active = 1")
        await db.commit()
    logging.info("✅ База данных инициализирована")


# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================

def is_admin(user_id: int) -> bool:
    try:
        return user_id in ADMIN_IDS or user_id == int(BOT_TOKEN.split(":")[0])
    except:
        return user_id in ADMIN_IDS


async def add_user(user_id: int, username: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO users (user_id, username, step, active) VALUES (?, ?, MAX(1, COALESCE((SELECT step FROM users WHERE user_id = ?), 0)), 1)",
            (user_id, username, user_id))
        await db.commit()


async def get_user_status(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT active FROM users WHERE user_id = ?", (user_id,)) as cursor:
            result = await cursor.fetchone()
            return bool(result and result[0])


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
            next_pos = (await cursor.fetchone())[0] or 0
        await db.execute("INSERT INTO content (text, media_type, file_id, position) VALUES (?, ?, ?, ?)",
                        (text, media_type, file_id, next_pos + 1))
        await db.commit()
        return next_pos + 1


async def get_content_by_position(pos: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT text, media_type, file_id FROM content WHERE position = ?", (pos,)) as cursor:
            result = await cursor.fetchone()
            if result:
                return {"text": result[0], "media_type": result[1] or "text", "file_id": result[2]}
            return None


async def get_all_content():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT position, text, media_type, file_id FROM content ORDER BY position") as cursor:
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
            await db.execute("UPDATE content SET text = ?, media_type = ?, file_id = ? WHERE position = ?",
                           (text, media_type, file_id, pos))
        elif text is not None:
            await db.execute("UPDATE content SET text = ? WHERE position = ?", (text, pos))
        elif media_type is not None and file_id is not None:
            await db.execute("UPDATE content SET media_type = ?, file_id = ? WHERE position = ?",
                           (media_type, file_id, pos))
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
                    if key == 'sched_hour': hour = int(value)
                    elif key == 'sched_min': minute = int(value)
                except: pass
            return hour, minute
    return DEFAULT_HOUR, DEFAULT_MINUTE


async def update_schedule(hour: int, minute: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('sched_hour', ?)", (hour,))
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('sched_min', ?)", (minute,))
        await db.commit()


# ==================== ОБЫЧНЫЕ ПОЛЬЗОВАТЕЛИ ====================

@dp.message(F.text == "▶️ Начать")
async def btn_start(message: types.Message):
    await add_user(message.from_user.id, message.from_user.username)
    count = await get_content_count()
    h, m = await get_schedule()
    await message.answer(
        f"✅ Вы подписаны!\nВас ждёт {count} сообщений.\nРассылка: ежедневно в {h:02d}:{m:02d} ({SCHEDULE_TIMEZONE}).",
        reply_markup=get_main_keyboard(is_active=True))


@dp.message(F.text == "⏹️ Закончить")
async def btn_stop(message: types.Message):
    await deactivate_user(message.from_user.id)
    await message.answer("🔕 Рассылка остановлена. Нажмите «▶️ Начать», чтобы вернуться.",
                        reply_markup=get_main_keyboard(is_active=False))


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    if is_admin(message.from_user.id):
        await message.answer("👋 Привет, админ! Выберите действие:", reply_markup=get_admin_keyboard())
    else:
        await add_user(message.from_user.id, message.from_user.username)
        count = await get_content_count()
        h, m = await get_schedule()
        await message.answer(
            f"✅ Вы подписаны!\nВас ждёт {count} сообщений.\nРассылка: ежедневно в {h:02d}:{m:02d} ({SCHEDULE_TIMEZONE}).",
            reply_markup=get_main_keyboard(is_active=True))


@dp.message(F.text == "/stop")
async def cmd_stop(message: types.Message):
    await deactivate_user(message.from_user.id)
    await message.answer("🔕 Рассылка остановлена.", reply_markup=get_main_keyboard(is_active=False))


# ==================== АДМИН: ГЛАВНОЕ МЕНЮ ====================

@dp.message(F.text == "🔙 Обычное меню")
async def admin_hide_panel(message: types.Message):
    if not is_admin(message.from_user.id): return
    is_active = await get_user_status(message.from_user.id)
    await message.answer("✅ Возвращено обычное меню", reply_markup=get_main_keyboard(is_active))


@dp.message(F.text == "📋 Все посты")
async def admin_list_posts(message: types.Message):
    if not is_admin(message.from_user.id): return
    posts = await get_all_content()
    if not posts:
        return await message.answer("📭 Постов пока нет", reply_markup=get_admin_keyboard())
    
    media_emoji = {"photo": "📷", "video": "🎥", "document": "📄", "audio": "🎵", "voice": "🎤", "text": "📝"}
    result = "📋 Все посты:\n\n"
    for pos, text, media_type, _ in posts:
        emoji = media_emoji.get(media_type, "📝")
        preview = (text or "[без текста]")[:60] + ("..." if text and len(text) > 60 else "")
        result += f"{emoji} #{pos}: {preview}\n"
    await message.answer(result, reply_markup=get_admin_keyboard())


@dp.message(F.text == "📊 Статистика")
async def admin_stats(message: types.Message):
    if not is_admin(message.from_user.id): return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users WHERE active=1") as c: active = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM content") as c: posts = (await c.fetchone())[0]
        async with db.execute("SELECT AVG(step) FROM users WHERE active=1") as c: avg_step = round((await c.fetchone())[0] or 0, 1)
    await message.answer(f"📊 Статистика:\n• Активных: {active}\n• Постов: {posts}\n• Средний шаг: {avg_step}",
                        reply_markup=get_admin_keyboard())


# ==================== 🔙 УНИВЕРСАЛЬНАЯ КНОПКА «НАЗАД» ====================

@dp.callback_query(F.data == "admin_back")
async def admin_back_callback(callback: types.CallbackQuery, state: FSMContext):
    """✅ Универсальный обработчик кнопки 'Назад' для админа"""
    if not is_admin(callback.from_user.id):
        return await callback.answer("❌ Доступ запрещён", show_alert=True)
    
    await state.clear()
    try:
        await callback.message.edit_text("🔙 Возврат в меню", reply_markup=get_admin_keyboard())
    except:
        await callback.message.answer("🔙 Возврат в меню", reply_markup=get_admin_keyboard())
    await callback.answer()


# ==================== 👁️ ПРЕДПРОСМОТР ПОСТА ====================

@dp.callback_query(F.data.startswith("preview_"))
async def admin_preview_post(callback: types.CallbackQuery):
    """✅ Показывает пост так, как его увидит обычный пользователь"""
    if not is_admin(callback.from_user.id):
        return await callback.answer("❌", show_alert=True)
    
    pos = int(callback.data.split("_")[1])
    content = await get_content_by_position(pos)
    if not content:
        return await callback.answer("❌ Пост не найден", show_alert=True)
    
    # Отправляем пост БЕЗ префикса "ТЕСТ" — точно как для пользователя
    await send_media_message(callback.from_user.id, content, test_mode=False)
    await callback.answer(f"👁️ Пост #{pos} отправлен вам в чат")


# ==================== АДМИН: ДОБАВИТЬ ПОСТ ====================

@dp.message(F.text == "📝 Добавить пост")
async def admin_add_post_start(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    await message.answer(
        "📝 **Добавление поста**\n\n"
        "Отправьте:\n• 📷 Фото, 🎥 Видео или 📄 Файл — с подписью (текстом поста)\n"
        "• Или просто текст — для обычного сообщения\n\n"
        "Нажмите 🔙 Назад, чтобы отменить",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")]]))
    await state.set_state(AdminStates.waiting_for_post_text)


@dp.message(AdminStates.waiting_for_post_text, F.text == "🔙 Назад")
@dp.callback_query(AdminStates.waiting_for_post_text, F.data == "admin_back")
async def admin_add_post_cancel(message: types.Message | types.CallbackQuery, state: FSMContext):
    if isinstance(message, types.CallbackQuery): await message.answer()
    await state.clear()
    target = message.message if isinstance(message, types.CallbackQuery) else message
    await target.answer("❌ Отменено", reply_markup=get_admin_keyboard())


@dp.message(AdminStates.waiting_for_post_text)
async def admin_add_post_save(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    
    raw_text = message.caption if message.caption else message.text
    text = raw_text.strip() if raw_text and not raw_text.strip().startswith("🔙") else None
    
    media_type, file_id = "text", None
    if message.photo: media_type, file_id = "photo", message.photo[-1].file_id
    elif message.video: media_type, file_id = "video", message.video.file_id
    elif message.document: media_type, file_id = "document", message.document.file_id
    elif message.audio: media_type, file_id = "audio", message.audio.file_id
    elif message.voice: media_type, file_id = "voice", message.voice.file_id
    
    if not text and media_type != "text": text = None
    
    pos = await add_content(text=text, media_type=media_type, file_id=file_id)
    await state.clear()
    
    emoji = {"photo": "📷", "video": "🎥", "document": "📄", "audio": "🎵", "voice": "🎤", "text": "📝"}.get(media_type, "📝")
    await message.answer(f"{emoji} ✅ Пост #{pos} добавлен!", reply_markup=get_admin_keyboard())


# ==================== АДМИН: РЕДАКТИРОВАТЬ ====================

@dp.message(F.text == "✏️ Редактировать")
async def admin_edit_start(message: types.Message):
    if not is_admin(message.from_user.id): return
    posts = await get_all_content()
    if not posts:
        return await message.answer("📭 Нечего редактировать", reply_markup=get_admin_keyboard())
    await message.answer("✏️ Выберите пост для редактирования:",
                        reply_markup=get_post_list_keyboard(posts, mode="edit"))


@dp.callback_query(F.data.startswith("edit_") and not F.data.startswith("edit_text") and not F.data.startswith("edit_media"))
async def admin_edit_select(callback: types.CallbackQuery):
    """Выбор поста для редактирования"""
    if not is_admin(callback.from_user.id):
        return await callback.answer("❌ Доступ запрещён", show_alert=True)
    
    pos = int(callback.data.split("_")[1])
    content = await get_content_by_position(pos)
    if not content:
        return await callback.answer("❌ Пост не найден", show_alert=True)
    
    emoji = {"photo": "📷", "video": "🎥", "document": "📄", "audio": "🎵", "voice": "🎤", "text": "📝"}.get(content["media_type"], "📝")
    preview = (content["text"] or "[без текста]")[:100]
    
    await callback.message.edit_text(
        f"{emoji} **Пост #{pos}**\n\n"
        f"Тип: {content['media_type']}\n"
        f"Текст: {preview}{'...' if content['text'] and len(content['text']) > 100 else ''}\n\n"
        f"Что изменить?",
        parse_mode="Markdown",
        reply_markup=get_edit_options_keyboard(pos))
    await callback.answer()


@dp.callback_query(F.data.startswith("edit_text_"))
async def admin_edit_text_start(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer("❌", show_alert=True)
    pos = int(callback.data.split("_")[2])
    await state.update_data(edit_pos=pos)
    await state.set_state(AdminStates.waiting_for_edit_text)
    await callback.message.edit_text(
        f"✏️ Введите новый текст для поста #{pos}:\n\n🔙 Назад — отменить",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")]]))
    await callback.answer()


@dp.callback_query(AdminStates.waiting_for_edit_text, F.data == "admin_back")
@dp.message(AdminStates.waiting_for_edit_text, F.text == "🔙 Назад")
async def admin_edit_text_cancel(message: types.Message | types.CallbackQuery, state: FSMContext):
    if isinstance(message, types.CallbackQuery): await message.answer()
    await state.clear()
    target = message.message if isinstance(message, types.CallbackQuery) else message
    if isinstance(target, types.Message):
        await target.edit_text("❌ Отменено", reply_markup=get_admin_keyboard())
    else:
        await target.answer("❌ Отменено", reply_markup=get_admin_keyboard())


@dp.message(AdminStates.waiting_for_edit_text)
async def admin_edit_text_save(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    data = await state.get_data()
    pos = data.get("edit_pos")
    if not pos: return await message.answer("❌ Ошибка")
    
    await edit_content_by_position(pos, text=message.text)
    await state.clear()
    await message.answer(f"✅ Текст поста #{pos} обновлён!", reply_markup=get_admin_keyboard())


@dp.callback_query(F.data.startswith("edit_media_"))
async def admin_edit_media_start(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer("❌", show_alert=True)
    pos = int(callback.data.split("_")[2])
    await state.update_data(edit_pos=pos)
    await state.set_state(AdminStates.waiting_for_media_replace)
    await callback.message.edit_text(
        f"🖼️ Отправьте новое медиа (фото/видео/файл) для поста #{pos}:\n\n🔙 Назад — отменить",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")]]))
    await callback.answer()


@dp.callback_query(AdminStates.waiting_for_media_replace, F.data == "admin_back")
@dp.message(AdminStates.waiting_for_media_replace, F.text == "🔙 Назад")
async def admin_edit_media_cancel(message: types.Message | types.CallbackQuery, state: FSMContext):
    if isinstance(message, types.CallbackQuery): await message.answer()
    await state.clear()
    target = message.message if isinstance(message, types.CallbackQuery) else message
    if isinstance(target, types.Message):
        await target.edit_text("❌ Отменено", reply_markup=get_admin_keyboard())
    else:
        await target.answer("❌ Отменено", reply_markup=get_admin_keyboard())


@dp.message(AdminStates.waiting_for_media_replace)
async def admin_edit_media_save(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    data = await state.get_data()
    pos = data.get("edit_pos")
    if not pos: return await message.answer("❌ Ошибка")
    
    media_type, file_id = None, None
    if message.photo: media_type, file_id = "photo", message.photo[-1].file_id
    elif message.video: media_type, file_id = "video", message.video.file_id
    elif message.document: media_type, file_id = "document", message.document.file_id
    elif message.audio: media_type, file_id = "audio", message.audio.file_id
    elif message.voice: media_type, file_id = "voice", message.voice.file_id
    
    if not media_type:
        return await message.answer("❌ Отправьте фото, видео или файл",
                                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")]]))
    
    await edit_content_by_position(pos, media_type=media_type, file_id=file_id)
    await state.clear()
    await message.answer(f"✅ Медиа поста #{pos} обновлено!", reply_markup=get_admin_keyboard())


# ==================== АДМИН: УДАЛИТЬ ====================

@dp.message(F.text == "🗑️ Удалить пост")
async def admin_delete_start(message: types.Message):
    if not is_admin(message.from_user.id): return
    posts = await get_all_content()
    if not posts:
        return await message.answer("📭 Нечего удалять", reply_markup=get_admin_keyboard())
    await message.answer("🗑️ Выберите пост для удаления:",
                        reply_markup=get_post_list_keyboard(posts, mode="delete"))


@dp.callback_query(F.data.startswith("delete_"))
async def admin_delete_select(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("❌ Доступ запрещён", show_alert=True)
    
    pos = int(callback.data.split("_")[1])
    content = await get_content_by_position(pos)
    if not content:
        return await callback.answer("❌ Пост не найден", show_alert=True)
    
    emoji = {"photo": "📷", "video": "🎥", "document": "📄", "audio": "🎵", "voice": "🎤", "text": "📝"}.get(content["media_type"], "📝")
    preview = (content["text"] or "[без текста]")[:100]
    
    await callback.message.edit_text(
        f"{emoji} **Удалить пост #{pos}?**\n\n"
        f"Текст: {preview}{'...' if content['text'] and len(content['text']) > 100 else ''}\n\n"
        f"⚠️ После удаления номера последующих постов сдвинутся!",
        parse_mode="Markdown",
        reply_markup=get_confirm_delete_keyboard(pos))
    await callback.answer()


@dp.callback_query(F.data.startswith("confirm_delete_"))
async def admin_delete_confirm(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("❌", show_alert=True)
    pos = int(callback.data.split("_")[2])
    
    if await delete_content_by_position(pos):
        await callback.message.edit_text(f"✅ Пост #{pos} удалён!", reply_markup=get_admin_keyboard())
    else:
        await callback.message.edit_text(f"❌ Пост #{pos} не найден", reply_markup=get_admin_keyboard())
    await callback.answer()


# ==================== АДМИН: ВРЕМЯ ====================

@dp.message(F.text == "⏰ Настроить время")
async def admin_schedule_start(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    h, m = await get_schedule()
    await message.answer(
        f"⏰ Текущее время: {h:02d}:{m:02d}\n\nВведите новый час (0-23):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")]]))
    await state.set_state(AdminStates.waiting_for_schedule_hour)


@dp.callback_query(AdminStates.waiting_for_schedule_hour, F.data == "admin_back")
@dp.message(AdminStates.waiting_for_schedule_hour, F.text == "🔙 Назад")
@dp.callback_query(AdminStates.waiting_for_schedule_minute, F.data == "admin_back")
@dp.message(AdminStates.waiting_for_schedule_minute, F.text == "🔙 Назад")
async def admin_schedule_cancel(message: types.Message | types.CallbackQuery, state: FSMContext):
    if isinstance(message, types.CallbackQuery): await message.answer()
    await state.clear()
    target = message.message if isinstance(message, types.CallbackQuery) else message
    if isinstance(target, types.Message):
        await target.edit_text("❌ Отменено", reply_markup=get_admin_keyboard())
    else:
        await target.answer("❌ Отменено", reply_markup=get_admin_keyboard())


@dp.message(AdminStates.waiting_for_schedule_hour)
async def admin_schedule_hour_save(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    if not message.text.isdigit() or not (0 <= int(message.text) < 24):
        return await message.answer("❌ Введите корректный час (0-23):")
    await state.update_data(schedule_hour=int(message.text))
    await state.set_state(AdminStates.waiting_for_schedule_minute)
    await message.answer("Введите минуты (0-59):",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")]]))


@dp.message(AdminStates.waiting_for_schedule_minute)
async def admin_schedule_minute_save(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    if not message.text.isdigit() or not (0 <= int(message.text) < 60):
        return await message.answer("❌ Введите корректные минуты (0-59):")
    
    data = await state.get_data()
    hour, minute = data["schedule_hour"], int(message.text)
    await update_schedule(hour, minute)
    scheduler.remove_all_jobs()
    await setup_scheduler()
    await state.clear()
    await message.answer(f"✅ Время изменено на {hour:02d}:{minute:02d}", reply_markup=get_admin_keyboard())


# ==================== ОТЛАДКА ====================

@dp.message(Command("debug"))
async def cmd_debug(message: types.Message):
    if not is_admin(message.from_user.id): return
    now = datetime.now(ZoneInfo(SCHEDULE_TIMEZONE))
    h, m = await get_schedule()
    jobs = scheduler.get_jobs()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT step FROM users WHERE user_id = ?", (message.from_user.id,)) as cursor:
            result = await cursor.fetchone()
            my_step = result[0] if result else None
    weekday_map = ["Вс", "Пн", "Вт", "Ср", "Чт", "Пт", "Сб"]
    is_active = await get_user_status(message.from_user.id)
    
    await message.answer(
        f"🔍 ОТЛАДКА:\n"
        f"• Сейчас: {now.strftime('%H:%M %d.%m.%Y')} ({weekday_map[now.weekday()]})\n"
        f"• Рассылка: ежедневно в {h:02d}:{m:02d} {SCHEDULE_TIMEZONE}\n"
        f"• Ваш статус: {'✅ Подписан' if is_active else '❌ Не подписан'}\n"
        f"• Ваш шаг: {my_step}\n"
        f"• Постов в БД: {await get_content_count()}\n"
        f"• Задач в планировщике: {len(jobs)}\n" +
        (f"• След. запуск: {jobs[0].next_run_time.astimezone(ZoneInfo(SCHEDULE_TIMEZONE)).strftime('%H:%M %d.%m.%Y')}\n" if jobs else "") +
        f"• ADMIN_IDS: {ADMIN_IDS}\n"
        f"• Создатель бота: {BOT_TOKEN.split(':')[0] if BOT_TOKEN else 'N/A'}",
        reply_markup=get_main_keyboard(is_active) if not is_admin(message.from_user.id) else get_admin_keyboard())


# ==================== РАССЫЛКА ====================

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
        logging.error(f"❌ Ошибка отправки {media_type} для {user_id}: {e}")
        if text: await bot.send_message(user_id, prefix + text)


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
        if step < 1 or step > total: continue
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
        CronTrigger(hour=hour, minute=minute, timezone=ZoneInfo(SCHEDULE_TIMEZONE), jitter=30),
        id="drip_job", replace_existing=True, misfire_grace_time=3600)
    logging.info(f"⏰ Планировщик: ежедневно в {hour:02d}:{minute:02d} {SCHEDULE_TIMEZONE}")


# ==================== ЗАПУСК ====================

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