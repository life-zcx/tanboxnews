import asyncio
import logging
import os
import html
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    FSInputFile
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import aiohttp

import config
import database as db
import scraper
import ai_service
import threads_service
import publisher_service
from google_sheets_service import sync_to_google_sheet
from keyboards import (
    get_main_reply_keyboard,
    get_pack_keyboard,
    get_test_preview_keyboard,
    get_schedule_slots_keyboard,
    get_posts_page_keyboard,
    get_autopilot_queue_keyboard
)

# Logging setup (Console + File data/bot.log)
log_file = os.path.join(config.DATA_DIR, "bot.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(log_file, encoding="utf-8")
    ]
)
logger = logging.getLogger(__name__)

bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

# ----------------- Texts -----------------

HELP_FULL_TEXT = (
    "📖 <b>Справка по системе Tanbox News AI:</b>\n\n"
    "⚡ <b>Автоматический мониторинг:</b>\n"
    "• Сканирует 13 источников: порталы РК (Uchet.kz, Tañba, Inbusiness, Курсив), Telegram-каналы (Параграф Бухгалтер, MyBuh) и Открытые НПА (legalacts.egov.kz);\n"
    "• Отбирает изменения в законах, дедлайны, проверки КГД и штрафы в МРП;\n"
    "• Готовит посты для Telegram (@tanboxkz) и Threads (@tanbox.kz);\n\n"
    "⏰ <b>Отложенный постинг и Контент-план:</b>\n"
    "• Планируйте посты на оптимальное время (вторник/четверг 11:00);\n"
    "• Автопилот сам распределяет посты по расписанию и публикует в @tanboxkz;\n\n"
    "📅 <b>Календарь дедлайнов регуляторики РК:</b>\n"
    "• Отслеживает даты сдачи ФНО 910/300, этапы маркировки обуви, лекарств и напитков;\n"
    "• За 14, 7 и 2 дня автоматически формирует предупреждающий контент-пак;\n\n"
    "📈 <b>Аналитика и экспорт:</b>\n"
    "• Сбор просмотров в реальном времени из @tanboxkz;\n"
    "• Выгрузка контент-плана в Excel/CSV файл прямо в чат;\n\n"
    "⚙️ <b>Быстрые команды:</b>\n"
    "• /start — Главное меню\n"
    "• /posts — Сохраненные посты\n"
    "• /schedule_queue — Очередь запланированных постов\n"
    "• /deadlines — Календарь дедлайнов Казахстана\n"
    "• /analytics — Аналитика просмотров канала @tanboxkz\n"
    "• /threads — Статус подключения к Threads (@tanbox.kz)\n"
    "• /export — Скачать контент-план в Excel/CSV\n"
    "• /check_now — Проверить источники сейчас\n"
    "• /pause / /resume — Управление авто-поиском\n"
    "• /sources — Список источников"
)

# ----------------- FSM States -----------------

class ContentFSM(StatesGroup):
    waiting_for_custom_rewrite = State()
    waiting_for_search_query = State()
    waiting_for_custom_schedule_date = State()

# ----------------- Safe Sending & Editing Helpers -----------------

async def safe_callback_answer(callback: CallbackQuery, text: str = None, show_alert: bool = False):
    try:
        await callback.answer(text=text, show_alert=show_alert)
    except TelegramBadRequest as e:
        if "query is too old" in str(e).lower() or "query id is invalid" in str(e).lower():
            logger.debug(f"Ignoring expired callback query: {e}")
        else:
            logger.warning(f"Callback answer warning: {e}")
    except Exception as e:
        logger.warning(f"Unexpected callback answer error: {e}")

async def safe_edit_text(message: types.Message, text: str, header: str = "", reply_markup=None):
    """Safely edits a message in-place, gracefully ignoring 'message not modified'"""
    full_text = (header + text).strip()
    MAX_CHUNK = 3800
    
    if len(full_text) <= MAX_CHUNK:
        try:
            return await message.edit_text(full_text, parse_mode="HTML", reply_markup=reply_markup)
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                return
            try:
                return await message.edit_text(full_text, parse_mode=None, reply_markup=reply_markup)
            except Exception:
                pass
        except Exception:
            try:
                return await message.edit_text(full_text, parse_mode=None, reply_markup=reply_markup)
            except Exception:
                pass
        return
    
    part1 = full_text[:MAX_CHUNK]
    part2 = full_text[MAX_CHUNK:]
    try:
        await message.edit_text(part1, parse_mode="HTML")
    except Exception:
        await message.edit_text(part1, parse_mode=None)
        
    try:
        await message.answer(part2, parse_mode="HTML", reply_markup=reply_markup)
    except Exception:
        await message.answer(part2, parse_mode=None, reply_markup=reply_markup)

# ----------------- In-Place Post Rendering (Tabs) -----------------

async def render_pack_view(message: types.Message, pack_id: int, tab: str = "card", return_page: int = 1, return_filter: str = "all"):
    pack = db.get_content_pack(pack_id)
    if not pack:
        builder = InlineKeyboardBuilder()
        builder.button(text="📁 Ко всем постам", callback_data=f"posts_page_{return_filter}_{return_page}")
        builder.adjust(1)
        await safe_edit_text(message, "⚠️ <b>Пост не найден или был удален.</b>", reply_markup=builder.as_markup())
        return

    clean_title = html.escape(pack['title'] or "Без названия")
    status = pack.get("status", "pending")
    
    if status == "published":
        views = pack.get("views_count") or 0
        status_label = f"🟢 Опубликован в @tanboxkz (👁 {views} просмотров)"
    elif status == "scheduled":
        sched = pack.get("scheduled_at") or ""
        status_label = f"⏰ Запланирован на <code>{sched[:16]}</code>"
    else:
        status_label = "🟡 Черновик (не опубликован)"

    date_str = pack.get("created_at", "")[:16]
    source_url = html.escape(pack.get('source_url', '—'))

    if tab == "tg":
        raw_tg = pack.get("tg_post") or "Текст для Telegram пуст."
        post_content = publisher_service.format_telegram_html(raw_tg)
        text = (
            f"📌 <b>ГОТОВЫЙ ПОСТ ДЛЯ TELEGRAM (Пост #{pack_id}):</b>\n"
            f"🎯 <b>Тема:</b> <i>{clean_title}</i>\n"
            f"📊 <b>Статус:</b> {status_label}\n"
            "<i>(Нажмите на текст ниже, чтобы скопировать)</i>\n\n"
            f"{post_content}"
        )
    elif tab == "th":
        raw_th = pack.get("threads_post") or "Текст для Threads пуст."
        post_content = html.escape(threads_service.clean_threads_text(raw_th))
        threads_post_id = pack.get("threads_post_id")
        username = config.THREADS_USERNAME or "tanbox.kz"
        if threads_post_id:
            th_status = f"🟢 Опубликован в Threads (@{username})\n🔗 <a href='https://www.threads.net/@{username}/post/{threads_post_id}'>Открыть ветку на threads.net</a>"
        else:
            th_status = "⚪ Не опубликован в Threads (нажмите кнопку внизу)"
        text = (
            f"🧵 <b>ГОТОВЫЙ ТРЕД ДЛЯ THREADS (Пост #{pack_id}):</b>\n"
            f"🎯 <b>Тема:</b> <i>{clean_title}</i>\n"
            f"📊 <b>Статус:</b> {th_status}\n\n"
            f"{post_content}"
        )
    else:  # tab == "card"
        preview = html.escape((pack.get("tg_post") or "")[:280] + "...")
        text = (
            f"📄 <b>Карточка поста #{pack['id']}</b>\n\n"
            f"🎯 <b>Тема:</b> {clean_title}\n"
            f"📊 <b>Статус:</b> {status_label}\n"
            f"📅 <b>Создан:</b> <code>{date_str}</code>\n"
            f"🔗 <b>Источник:</b> {source_url}\n\n"
            f"📝 <b>Краткое превью:</b>\n<i>{preview}</i>\n\n"
            "👇 <b>Выберите действие или переключите формат:</b>"
        )

    markup = get_pack_keyboard(
        pack_id=pack_id,
        current_tab=tab,
        status=status,
        scheduled_at=pack.get("scheduled_at"),
        return_page=return_page,
        return_filter=return_filter,
        threads_post_id=pack.get("threads_post_id"),
        channel_message_id=pack.get("channel_message_id")
    )
    await safe_edit_text(message, text, reply_markup=markup)

# ----------------- Paginated Posts View -----------------

async def render_posts_page(chat_id: int = None, message: types.Message = None, page: int = 1, status_filter: str = "all"):
    items, total = db.get_content_packs_paginated(page=page, page_size=5, status_filter=status_filter)
    total_pages = max(1, (total + 4) // 5)
    if page > total_pages:
        page = total_pages
        items, total = db.get_content_packs_paginated(page=page, page_size=5, status_filter=status_filter)

    counts = db.get_content_packs_counts()

    filter_titles = {
        "all": "Все посты",
        "pending": "Черновики",
        "published": "Опубликованные",
        "scheduled": "В плане"
    }
    filter_label = filter_titles.get(status_filter, "Все посты")

    if not items:
        text = (
            f"📁 <b>Посты и контент-план</b> • <code>{filter_label}</code>\n\n"
            f"📭 <b>В этой категории пока нет записей.</b>\n\n"
            "Переключите категорию кнопками ниже или нажмите «⚡ Сканировать сейчас»:"
        )
    else:
        text = (
            f"📁 <b>Посты и контент-план</b> • <code>{filter_label}</code>\n"
            f"Всего: <b>{total}</b> | Страница <b>{page}</b> из <b>{total_pages}</b>\n"
            f"────────────────────────\n"
        )
        num_icons = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
        for idx, p in enumerate(items, 1):
            num_badge = num_icons[idx - 1] if idx <= len(num_icons) else f"{idx}."
            if p["status"] == "published":
                views = p.get("views_count") or 0
                st_badge = "🟢 Опубликован"
                extra_info = f" • 👁 {views} просм."
            elif p["status"] == "scheduled":
                st_badge = "⏰ Запланирован"
                extra_info = f" • {p.get('scheduled_at', '')[5:16]}"
            else:
                st_badge = "🟡 Черновик"
                extra_info = ""

            date_str = p["created_at"][5:16] if p.get("created_at") else ""
            raw_title = p["title"].strip()
            title_display = html.escape((raw_title[:45] + "...") if len(raw_title) > 45 else raw_title)
            text += (
                f"{num_badge} <b>#{p['id']}</b> {title_display}\n"
                f"     └ <i>{st_badge}</i> • <code>{date_str}</code>{extra_info}\n\n"
            )

        text += "────────────────────────\n👇 <i>Выберите номер поста для просмотра:</i>"

    markup = get_posts_page_keyboard(
        items=items,
        page=page,
        total_pages=total_pages,
        status_filter=status_filter,
        counts=counts
    )

    if message:
        await safe_edit_text(message, text, reply_markup=markup)
    elif chat_id:
        await bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")

# ----------------- Commands -----------------

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user = message.from_user
    if not db.is_admin(user.id):
        await message.answer(
            f"👋 Здравствуйте, {html.escape(user.first_name)}!\n\n"
            "⛔ <b>Доступ ограничен.</b>\n"
            "Этот бот предназначен для закрытого управления контент-маркетингом Tanbox.kz.\n\n"
            f"Ваш Telegram ID: <code>{user.id}</code>. Сообщите его владельцу для добавления.",
            parse_mode="HTML"
        )
        return

    db.register_admin(user.id, user.username or "", user.first_name or "")
    
    welcome_text = (
        f"👋 <b>Здравствуйте, {html.escape(user.first_name or 'коллега')}!</b>\n\n"
        "Я — ваш автономный <b>B2B-контент-маркетер Tanbox (tanbox.kz)</b> по маркировке товаров (ИС МПТ / Tañba) и налогам в Казахстане.\n\n"
        "👇 <b>Для управления используйте кнопки внизу (под строкой ввода сообщения):</b>"
    )
    await message.answer(welcome_text, reply_markup=get_main_reply_keyboard(), parse_mode="HTML")

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    if not db.is_admin(message.from_user.id):
        return
    await message.answer(HELP_FULL_TEXT, reply_markup=get_main_reply_keyboard(), parse_mode="HTML")

@dp.message(Command("posts"))
async def cmd_posts(message: types.Message):
    if not db.is_admin(message.from_user.id):
        return
    await render_posts_page(chat_id=message.chat.id, page=1)

@dp.message(Command("schedule_queue"))
async def cmd_schedule_queue(message: types.Message):
    if not db.is_admin(message.from_user.id):
        return
    await show_content_plan_queue(message=message)

@dp.message(Command("deadlines"))
async def cmd_deadlines(message: types.Message):
    if not db.is_admin(message.from_user.id):
        return
    await show_regulatory_deadlines(message=message)

@dp.message(Command("analytics"))
async def cmd_analytics(message: types.Message):
    if not db.is_admin(message.from_user.id):
        return
    await show_analytics_dashboard(message=message)

@dp.message(Command("export"))
async def cmd_export(message: types.Message):
    if not db.is_admin(message.from_user.id):
        return
    await send_csv_export(message=message)

@dp.message(Command("threads"))
async def cmd_threads(message: types.Message):
    if not db.is_admin(message.from_user.id):
        return
    msg = await message.answer("🔄 <b>Проверяю подключение к Threads API (@tanbox.kz)...</b>", parse_mode="HTML")
    info = await threads_service.get_profile_info()
    if info.get("status") == "success":
        quota = info.get("quota_usage", 0)
        bio = html.escape(info.get("threads_biography") or "—")
        text = (
            "🧵 <b>Подключение к Threads активно!</b>\n\n"
            f"👤 <b>Аккаунт:</b> @{html.escape(info.get('username', 'tanbox.kz'))}\n"
            f"🆔 <b>ID:</b> <code>{info.get('id')}</code>\n"
            f"📝 <b>Био:</b> <i>{bio}</i>\n"
            f"📊 <b>Использовано лимита постов за сутки:</b> <code>{quota} / 250</code>\n"
            "🔑 <b>Токен:</b> Долгосрочный Meta Token (60 дней, авто-продление)\n\n"
            "<i>Вы можете публиковать ветки твитов в Threads прямо из карточки поста в разделе «📁 Посты и черновики».</i>"
        )
        builder = InlineKeyboardBuilder()
        builder.button(text="🌐 Открыть профиль @tanbox.kz", url="https://www.threads.net/@tanbox.kz")
        builder.button(text="🔄 Продлить токен Meta", callback_data="btn_refresh_threads_token")
        builder.adjust(1)
        await msg.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    else:
        await msg.edit_text(
            f"⚠️ <b>Ошибка связи с Threads:</b>\n<code>{html.escape(info.get('message', 'Неизвестная ошибка'))}</code>",
            parse_mode="HTML"
        )

@dp.callback_query(F.data == "btn_refresh_threads_token")
async def cb_refresh_threads_token(callback: CallbackQuery):
    await safe_callback_answer(callback, "Обновляю токен Meta...")
    res = await threads_service.refresh_threads_token()
    if res.get("status") == "success":
        await safe_callback_answer(callback, "Токен успешно продлен на 60 дней!", show_alert=True)
    else:
        await safe_callback_answer(callback, f"Ошибка обновления: {res.get('message')}", show_alert=True)

@dp.message(Command("pause"))
@dp.message(Command("stop"))
async def cmd_pause_scanner(message: types.Message):
    if not db.is_admin(message.from_user.id):
        return
    db.set_scanner_enabled(False)
    await message.answer(
        "⏸ <b>Авто-сканирование поставлено на ПАУЗУ.</b>\n"
        "Бот не будет слать автоматические сообщения в фоне. Вы по-прежнему можете запускать проверку вручную по кнопке «⚡ Сканировать сейчас».",
        parse_mode="HTML",
        reply_markup=get_main_reply_keyboard()
    )

@dp.message(Command("resume"))
async def cmd_resume_scanner(message: types.Message):
    if not db.is_admin(message.from_user.id):
        return
    db.set_scanner_enabled(True)
    await message.answer(
        "▶️ <b>Авто-сканирование ВОЗОБНОВЛЕНО.</b>\n"
        "Бот снова проверяет 13 порталов и ТГ-каналов РК по расписанию.",
        parse_mode="HTML",
        reply_markup=get_main_reply_keyboard()
    )

@dp.message(Command("check_now"))
async def cmd_check_now(message: types.Message):
    if not db.is_admin(message.from_user.id):
        return
    msg = await message.answer("🔄 <b>Запускаю проверку 13 источников по Казахстану...</b>", parse_mode="HTML")
    found = await run_news_check_cycle(notify_chat_id=message.chat.id)
    if found == 0:
        builder = InlineKeyboardBuilder()
        builder.button(text="📁 Открыть черновики", callback_data="posts_page_1")
        builder.adjust(1)
        await msg.edit_text(
            "✅ <b>Сканирование завершено.</b> Все последние публикации и посты из ТГ уже обработаны.\n\n"
            "Вы можете сгенерировать кейс кнопкой «💡 Банк болей (Evergreen)» внизу.",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
    else:
        try:
            await msg.delete()
        except Exception:
            pass

@dp.message(Command("evergreen"))
async def cmd_evergreen(message: types.Message):
    if not db.is_admin(message.from_user.id):
        return
    msg = await message.answer("🧠 <b>Анализирую топ-болей бизнеса РК и формирую экспертный контент-пак...</b>", parse_mode="HTML")
    pack = await ai_service.generate_evergreen_posts()
    pack_id = db.save_content_pack(
        news_id=None,
        title="Экспертный кейс: Ошибки интеграции 1С и касс с ИС МПТ в Казахстане",
        source_url="Внутренний банк кейсов платформы",
        full_response=pack["full_text"],
        tg_post=pack["tg_post"],
        threads_post=pack["threads_post"]
    )
    try:
        await msg.delete()
    except Exception:
        pass
    await send_content_pack_summary(message.chat.id, pack_id, "💡 Кейс из банка болей бизнеса РК", pack["tg_post"][:350] + "...")

@dp.message(Command("sources"))
async def cmd_sources(message: types.Message):
    if not db.is_admin(message.from_user.id):
        return
    sources = db.get_all_sources()
    text = f"🌐 <b>Мониторируемые источники Казахстана</b> ({len(sources)} шт.):\n\n"
    builder = InlineKeyboardBuilder()
    for s in sources:
        status_icon = "🟢" if s["is_active"] else "🔴"
        clean_name = html.escape(s['name'])
        clean_url = html.escape(s['url'])
        text += f"{status_icon} <b>{clean_name}</b>\n<code>{clean_url}</code>\n\n"
        toggle_text = f"Выкл #{s['id']}" if s["is_active"] else f"Вкл #{s['id']}"
        builder.button(text=toggle_text, callback_data=f"toggle_src_{s['id']}")
        builder.button(text=f"🗑 #{s['id']}", callback_data=f"del_src_{s['id']}")
    
    builder.adjust(2)
    builder.row(InlineKeyboardButton(text="➕ Добавить источник", callback_data="btn_add_source_hint"))
    await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")

# ----------------- Bottom Reply Keyboard Handlers -----------------

@dp.message(F.text == "📁 Посты и черновики")
@dp.message(F.text == "Посты и черновики")
async def btn_posts_handler(message: types.Message):
    if not db.is_admin(message.from_user.id):
        return
    await render_posts_page(chat_id=message.chat.id, page=1)

@dp.message(F.text == "⚡ Сканировать сейчас")
@dp.message(F.text == "Сканировать сейчас")
async def btn_check_handler(message: types.Message):
    if not db.is_admin(message.from_user.id):
        return
    await cmd_check_now(message)

@dp.message(F.text.contains("Контент-план") | F.text.contains("Очередь") | F.text.contains("Автопилот"))
async def btn_content_plan_handler(message: types.Message):
    if not db.is_admin(message.from_user.id):
        return
    await show_content_plan_queue(message=message)

@dp.message(F.text.contains("Дедлайны РК") | F.text.contains("Календарь дедлайнов"))
async def btn_deadlines_handler(message: types.Message):
    if not db.is_admin(message.from_user.id):
        return
    await show_regulatory_deadlines(message=message)

@dp.message(F.text.contains("Аналитика"))
async def btn_analytics_handler(message: types.Message):
    if not db.is_admin(message.from_user.id):
        return
    await show_analytics_dashboard(message=message)

@dp.message(F.text.contains("Скачать Excel") | F.text.contains("Экспорт"))
async def btn_export_handler(message: types.Message):
    if not db.is_admin(message.from_user.id):
        return
    await send_csv_export(message=message)

@dp.message(F.text.startswith("🟢 Авто-поиск") | F.text.startswith("🔴 Авто-поиск") | (F.text == "Авто-поиск: ВКЛ") | (F.text == "Авто-поиск: ПАУЗА"))
async def btn_toggle_scanner_reply_handler(message: types.Message):
    if not db.is_admin(message.from_user.id):
        return
    new_state = db.toggle_scanner()
    if new_state:
        msg_text = "▶️ <b>Авто-поиск новостей ВКЛЮЧЕН.</b>\nБот снова проверяет 13 порталов и ТГ-каналов РК по расписанию."
    else:
        msg_text = "⏸ <b>Авто-поиск новостей поставлен на ПАУЗУ.</b>\nБот не будет слать автоматические сообщения в фоне."
    await message.answer(msg_text, reply_markup=get_main_reply_keyboard(), parse_mode="HTML")

@dp.message(F.text.contains("Банк болей"))
async def btn_evergreen_handler(message: types.Message):
    if not db.is_admin(message.from_user.id):
        return
    await cmd_evergreen(message)

@dp.message(F.text.contains("Источники"))
async def btn_sources_handler(message: types.Message):
    if not db.is_admin(message.from_user.id):
        return
    await cmd_sources(message)

@dp.message(F.text.contains("Справка"))
async def btn_help_handler(message: types.Message):
    if not db.is_admin(message.from_user.id):
        return
    await cmd_help(message)

# ----------------- Feature Views: Content Plan Queue -----------------

async def show_content_plan_queue(message: types.Message = None, callback: CallbackQuery = None):
    ov = db.get_autopilot_overview()
    scheduled = ov["scheduled_packs"]
    free_slots = ov["free_slots"]
    pending_count = ov["pending_count"]

    text = (
        "🤖 <b>Умный Автопилот и Сетка публикаций Tanbox:</b>\n"
        "🎯 <i>Оптимальные B2B-слоты РК: Вторник, Четверг, Суббота в 11:00</i>\n"
        "────────────────────────\n\n"
    )

    builder = InlineKeyboardBuilder()

    if scheduled:
        text += f"📅 <b>Посты в очереди автопилота ({len(scheduled)}):</b>\n"
        for idx, p in enumerate(scheduled, 1):
            sched_dt = datetime.strptime(p["scheduled_at"][:19], "%Y-%m-%d %H:%M:%S")
            human_t = db.format_human_slot(sched_dt)
            raw_title = p["title"].strip()
            short_title = html.escape((raw_title[:42] + "...") if len(raw_title) > 42 else raw_title)
            text += f"<b>{idx}. ⏰ {human_t}</b>\n   └ #{p['id']}: <i>{short_title}</i>\n\n"
            builder.button(text=f"📄 #{p['id']} — {short_title[:20]}...", callback_data=f"open_pack_{p['id']}")
    else:
        text += "🗓 <b>Очередь сейчас пуста.</b> Посты будут выходить по мере добавления в автопилот.\n\n"

    if free_slots:
        text += "🟢 <b>Ближайшие свободные слоты:</b>\n"
        for s in free_slots:
            text += f"• <code>{s['human']}</code>\n"
        text += "\n"

    text += f"📥 Доступно черновиков для автопилота: <b>{pending_count}</b>\n"

    builder.adjust(1)

    # Action buttons
    if pending_count > 0:
        builder.button(text="⚡ Заполнить автопилот (Топ-3 черновика)", callback_data="autopilot_fill_batch")
    builder.button(text="📁 Открыть черновики", callback_data="posts_page_pending_1")
    builder.button(text="🔄 Обновить сетку", callback_data="autopilot_refresh")

    if callback:
        await safe_edit_text(callback.message, text, reply_markup=builder.as_markup())
    elif message:
        try:
            await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        except Exception as e:
            logger.warning(f"Error sending autopilot queue: {e}")
            await message.answer(text[:3900], reply_markup=builder.as_markup(), parse_mode=None)

# ----------------- Feature Views: Regulatory Deadlines -----------------

async def show_regulatory_deadlines(message: types.Message = None, callback: CallbackQuery = None):
    deadlines = db.get_upcoming_deadlines(days_ahead=90)
    
    if not deadlines:
        text = "📅 <b>В ближайшие 90 дней критических дедлайнов не найдено.</b>"
        if callback:
            await safe_edit_text(callback.message, text)
        elif message:
            await message.answer(text, parse_mode="HTML")
        return

    text = "📅 <b>Календарь регуляторных дедлайнов Казахстана (Ближайшие 90 дней):</b>\n\n"
    builder = InlineKeyboardBuilder()

    for d in deadlines:
        days = d.get("days_left", 0)
        badge = "🚨 СРОЧНО" if days <= 7 else ("⚠️ СКОРО" if days <= 14 else "📅")
        clean_title = html.escape(d["title"])
        clean_desc = html.escape(d.get("description", "")[:120])
        clean_fines = html.escape(d.get("fines_info", "") or "")
        
        text += (
            f"{badge} <b>{clean_title}</b>\n"
            f"⏳ Крайний срок: <code>{d['deadline_date']}</code> (<b>осталось {days} дн.</b>)\n"
            f"🎯 <i>{clean_desc}...</i>\n"
            f"⚖️ Штрафы: <code>{clean_fines}</code>\n\n"
        )
        builder.button(text=f"⚡ Создать пост: {d['title'][:25]}...", callback_data=f"gen_dl_post_{d['id']}")

    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="📁 Открыть черновики", callback_data="posts_page_1"))

    if callback:
        await safe_edit_text(callback.message, text, reply_markup=builder.as_markup())
    elif message:
        try:
            await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        except Exception as e:
            logger.warning(f"Error sending deadlines: {e}")
            await message.answer(text[:3900], reply_markup=builder.as_markup(), parse_mode=None)

# ----------------- Feature Views: Analytics Dashboard -----------------

async def show_analytics_dashboard(message: types.Message = None, callback: CallbackQuery = None):
    # First sync live views from Telegram channel preview
    views_map = scraper.fetch_tanbox_channel_metrics("tanboxkz")
    if views_map:
        db.sync_channel_metrics(views_map)

    stats = db.get_channel_analytics()
    top_posts = stats.get("top_posts", [])
    
    top_text = ""
    if top_posts:
        for idx, tp in enumerate(top_posts, 1):
            clean_t = html.escape(tp['title'][:50])
            views = tp.get("views_count") or 0
            post_id = tp.get("channel_message_id")
            link_html = f"<a href='https://t.me/tanboxkz/{post_id}'>#{post_id}</a>" if post_id else f"#{tp['id']}"
            top_text += f"{idx}. {link_html} <b>{clean_t}...</b> — <b>👁 {views} просм.</b>\n"
    else:
        top_text = "<i>Пока нет опубликованных постов с зафиксированными просмотрами.</i>\n"

    text = (
        "📈 <b>Аналитика публикаций канала @tanboxkz</b>\n\n"
        f"• Всего постов в канале: <code>{stats['total_published']}</code>\n"
        f"• Суммарно просмотров: <code>{stats['total_views']}</code>\n"
        f"• Средний охват на пост: <code>{stats['avg_views']} просм.</code>\n"
        f"• Постов в очереди автопилота: <code>{stats['scheduled_count']}</code>\n\n"
        "🔥 <b>ТОП публикаций по вовлечению:</b>\n"
        f"{top_text}\n"
        "💡 <b>Рекомендация контент-стратегии:</b>\n"
        "Материалы с разбором штрафов КГД в тенге и техническими сбоями касс/сканеров вызывают максимальный отклик у B2B-аудитории РК."
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Обновить просмотры", callback_data="btn_refresh_analytics")
    builder.button(text="📥 Скачать Excel/CSV отчет", callback_data="btn_export_csv_inline")
    builder.button(text="📁 Черновики", callback_data="posts_page_1")
    builder.adjust(1)

    if callback:
        await safe_edit_text(callback.message, text, reply_markup=builder.as_markup())
    elif message:
        try:
            await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        except Exception as e:
            logger.warning(f"Error sending deadlines: {e}")
            await message.answer(text[:3900], reply_markup=builder.as_markup(), parse_mode=None)

# ----------------- Feature Views: CSV Export -----------------

async def send_csv_export(message: types.Message = None, callback: CallbackQuery = None):
    csv_path = db.export_content_plan_csv()
    doc = FSInputFile(csv_path, filename=f"content_plan_tanbox_{datetime.now().strftime('%Y%m%d')}.csv")
    caption = (
        "📊 <b>Экспорт контент-плана Tanbox готов!</b>\n\n"
        "Файл сохранен в кодировке <b>UTF-8 с BOM</b> (открывается в Microsoft Excel и Google Таблицах без сбоев кириллицы).\n"
        "Содержит даты, статусы, тексты для всех 3 площадок и счетчики просмотров."
    )
    target = callback.message if callback else message
    await target.answer_document(doc, caption=caption, parse_mode="HTML")

# ----------------- Callbacks for Scheduling -----------------

@dp.callback_query(F.data.startswith("sched_pack_"))
async def cb_sched_pack_menu(callback: CallbackQuery):
    await safe_callback_answer(callback)
    pack_id = int(callback.data.split("_")[-1])
    pack = db.get_content_pack(pack_id)
    if not pack:
        await safe_callback_answer(callback, "Пост не найден", show_alert=True)
        return
    text = (
        f"⏰ <b>Отложенная публикация поста #{pack_id}</b>\n\n"
        f"🎯 <b>Тема:</b> <i>{html.escape(pack['title'][:55])}</i>\n\n"
        "Выберите удобный временной слот для автоматической публикации в канал @tanboxkz:\n"
        "<i>(Пик деловой активности в Казахстане: вторник и четверг в 11:00)</i>"
    )
    await safe_edit_text(callback.message, text, reply_markup=get_schedule_slots_keyboard(pack_id))

def parse_flexible_schedule_time(input_text: str) -> datetime | None:
    """Smart parser for human date/time formats"""
    text = input_text.strip().lower()
    now = datetime.now()

    # 1. Plain minutes or hours
    min_match = re.search(r'^(?:через\s*)?(\d+)\s*(?:мин|минут|m)?$', text)
    if min_match:
        mins = int(min_match.group(1))
        return (now + timedelta(minutes=mins)).replace(second=0, microsecond=0)

    hour_match = re.search(r'^(?:через\s*)?(\d+)\s*(?:ч|час|часа|часов|h)$', text)
    if hour_match:
        hrs = int(hour_match.group(1))
        return (now + timedelta(hours=hrs)).replace(second=0, microsecond=0)

    # 2. Time only: "23:25", "18:00"
    time_match = re.match(r'^(\d{1,2})[:\.-](\d{2})$', text)
    if time_match:
        h = int(time_match.group(1))
        m = int(time_match.group(2))
        candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    # 3. Tomorrow with time: "завтра 15:00", "завтра в 11:30"
    tomorrow_match = re.search(r'завтра\s*(?:в\s*)?(\d{1,2})[:\.-](\d{2})', text)
    if tomorrow_match:
        h = int(tomorrow_match.group(1))
        m = int(tomorrow_match.group(2))
        return (now + timedelta(days=1)).replace(hour=h, minute=m, second=0, microsecond=0)

    # 4. Day.Month Hour:Minute: "07.11 14:00"
    dm_match = re.match(r'^(\d{1,2})[\./-](\d{1,2})\s+(\d{1,2})[:\.-](\d{2})$', text)
    if dm_match:
        d = int(dm_match.group(1))
        mon = int(dm_match.group(2))
        h = int(dm_match.group(3))
        m = int(dm_match.group(4))
        candidate = now.replace(month=mon, day=d, hour=h, minute=m, second=0, microsecond=0)
        if candidate < now:
            candidate = candidate.replace(year=now.year + 1)
        return candidate

    # 5. Standard ISO / full date formats
    for fmt in ("%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass

    return None

@dp.callback_query(F.data.startswith("sched_slot_"))
async def cb_sched_slot_apply(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    pack_id = int(parts[2])
    slot_type = parts[3]

    now = datetime.now()
    if slot_type == "m10":
        target_dt = now + timedelta(minutes=10)
    elif slot_type == "m30":
        target_dt = now + timedelta(minutes=30)
    elif slot_type == "h1":
        target_dt = now + timedelta(hours=1)
    elif slot_type == "today18":
        if now.hour < 18:
            target_dt = now.replace(hour=18, minute=0, second=0, microsecond=0)
        else:
            target_dt = (now + timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)
    elif slot_type == "tomorrow":
        target_dt = (now + timedelta(days=1)).replace(hour=11, minute=0, second=0, microsecond=0)
    elif slot_type == "tue":
        days_ahead = (1 - now.weekday()) % 7
        if days_ahead == 0 and now.hour >= 11:
            days_ahead = 7
        target_dt = (now + timedelta(days=days_ahead)).replace(hour=11, minute=0, second=0, microsecond=0)
    elif slot_type == "thu":
        days_ahead = (3 - now.weekday()) % 7
        if days_ahead == 0 and now.hour >= 11:
            days_ahead = 7
        target_dt = (now + timedelta(days=days_ahead)).replace(hour=11, minute=0, second=0, microsecond=0)
    elif slot_type == "autopilot":
        target_dt_str, human_t = db.get_next_auto_pilot_slot()
        target_dt = datetime.strptime(target_dt_str, "%Y-%m-%d %H:%M:%S")
    elif slot_type == "custom":
        await state.set_state(ContentFSM.waiting_for_custom_schedule_date)
        await state.update_data(pack_id=pack_id)
        await safe_callback_answer(callback)
        builder = InlineKeyboardBuilder()
        builder.button(text="❌ Отмена", callback_data=f"open_pack_{pack_id}")
        await safe_edit_text(
            callback.message,
            f"⌨️ <b>Выбор даты и времени для поста #{pack_id}</b>\n\n"
            "Напишите удобное время в любом формате:\n\n"
            "• <code>через 15 мин</code> или <code>30</code>\n"
            "• <code>23:45</code> или <code>18:00</code>\n"
            "• <code>завтра 15:00</code>\n"
            "• <code>07.11 11:00</code>\n"
            "• <code>2026-11-12 14:30</code>",
            reply_markup=builder.as_markup()
        )
        return

    scheduled_iso = target_dt.strftime("%Y-%m-%d %H:%M:%S")
    db.schedule_content_pack(pack_id, scheduled_iso)
    await safe_callback_answer(
        callback,
        f"Запланировано на {scheduled_iso[:16]}! (TG + Threads)",
        show_alert=True
    )
    
    pack = db.get_content_pack(pack_id)
    await sync_to_google_sheet(pack)
    await render_pack_view(callback.message, pack_id, tab="card")

@dp.callback_query(F.data.startswith("sched_cancel_"))
async def cb_sched_cancel(callback: CallbackQuery):
    pack_id = int(callback.data.split("_")[-1])
    db.cancel_scheduled_pack(pack_id)
    await safe_callback_answer(callback, "Расписание отменено. Пост возвращен в черновики.", show_alert=True)
    await render_pack_view(callback.message, pack_id, tab="card")

# ----------------- Smart Autopilot Callbacks -----------------

@dp.callback_query(F.data.startswith("autopilot_"))
async def cb_autopilot_handler(callback: CallbackQuery):
    data = callback.data
    if data == "autopilot_refresh":
        await safe_callback_answer(callback, "Сетка автопилота обновлена")
        await show_content_plan_queue(callback=callback)
        return

    if data == "autopilot_fill_batch":
        assigned = db.batch_fill_autopilot(limit=3)
        if not assigned:
            await safe_callback_answer(callback, "Нет доступных черновиков для автопилота.", show_alert=True)
        else:
            alert_text = f"⚡ Поставлено {len(assigned)} постов в Автопилот:\n\n"
            for item in assigned:
                alert_text += f"• #{item['id']} → {item['slot_human']}\n"
            await safe_callback_answer(callback, alert_text, show_alert=True)
            for item in assigned:
                p = db.get_content_pack(item["id"])
                await sync_to_google_sheet(p)
        await show_content_plan_queue(callback=callback)
        return

    # Single post autopilot: autopilot_{pack_id}_{return_page}_{return_filter}
    parts = data.split("_")
    pack_id = int(parts[1])
    return_page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
    return_filter = parts[3] if len(parts) > 3 else "all"

    success, iso_str, human_str = db.assign_pack_to_autopilot(pack_id)
    await safe_callback_answer(
        callback,
        f"🤖 Пост #{pack_id} в Автопилоте!\n📅 {human_str}\n🌐 TG (@tanboxkz) + Threads",
        show_alert=True
    )
    p = db.get_content_pack(pack_id)
    await sync_to_google_sheet(p)
    await render_pack_view(callback.message, pack_id, tab="card", return_page=return_page, return_filter=return_filter)

@dp.message(ContentFSM.waiting_for_custom_schedule_date, F.text)
async def handle_custom_schedule_input(message: types.Message, state: FSMContext):
    data = await state.get_data()
    pack_id = data.get("pack_id")
    await state.clear()

    input_text = message.text.strip()
    dt = parse_flexible_schedule_time(input_text)
    if dt:
        scheduled_iso = dt.strftime("%Y-%m-%d %H:%M:%S")
        scheduled_display = dt.strftime("%Y-%m-%d %H:%M")
        db.schedule_content_pack(pack_id, scheduled_iso)
        await message.answer(
            f"✅ <b>Пост #{pack_id} успешно запланирован на {scheduled_display}!</b>\n\n"
            "В указанное время бот автоматически опубликует его сразу на двух площадках:\n"
            "✈️ В Telegram-канал <b>@tanboxkz</b>\n"
            "🧵 В официальный аккаунт <b>Threads (@tanbox.kz)</b>",
            parse_mode="HTML",
            reply_markup=get_main_reply_keyboard()
        )
        pack = db.get_content_pack(pack_id)
        await sync_to_google_sheet(pack)
    else:
        builder = InlineKeyboardBuilder()
        builder.button(text="⏰ Попробовать снова", callback_data=f"sched_pack_{pack_id}")
        builder.button(text="📁 К черновикам", callback_data="posts_page_1")
        builder.adjust(1)
        await message.answer(
            "⚠️ <b>Не удалось распознать время.</b>\n"
            "Примеры: <code>15</code>, <code>23:45</code>, <code>завтра 11:00</code>, <code>2026-11-12 14:30</code>",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )

# ----------------- Callbacks for Regulatory Deadlines -----------------

@dp.callback_query(F.data.startswith("gen_dl_post_"))
async def cb_gen_deadline_post(callback: CallbackQuery):
    dl_id = int(callback.data.split("_")[-1])
    dl = db.get_deadline(dl_id)
    if not dl:
        await safe_callback_answer(callback, "Дедлайн не найден", show_alert=True)
        return
    await safe_callback_answer(callback, "Генерирую срочный пост...")
    await safe_edit_text(
        callback.message,
        f"⏳ <b>Формирую предупреждающий контент-пак по дедлайну:</b>\n<i>«{html.escape(dl['title'])}»</i>...\n"
        "<i>Подбираю суммы штрафов в МРП и нативный CTA для Tanbox.</i>"
    )
    
    # Calculate days left
    try:
        dt = datetime.strptime(dl["deadline_date"], "%Y-%m-%d")
        dl["days_left"] = max(0, (dt - datetime.now()).days)
    except Exception:
        dl["days_left"] = 7

    pack = await ai_service.generate_deadline_warning_post(dl)
    pack_id = db.save_content_pack(
        news_id=None,
        title=f"⚠️ Дедлайн: {dl['title']}",
        source_url=f"Регуляторный календарь РК ({dl['deadline_date']})",
        full_response=pack["full_text"],
        tg_post=pack["tg_post"],
        threads_post=pack["threads_post"]
    )
    await render_pack_view(callback.message, pack_id, tab="card")

# ----------------- Callbacks for Analytics -----------------

@dp.callback_query(F.data == "btn_refresh_analytics")
async def cb_refresh_analytics(callback: CallbackQuery):
    await safe_callback_answer(callback, "Обновляю данные из Telegram...")
    await show_analytics_dashboard(callback=callback)

@dp.callback_query(F.data == "btn_export_csv_inline")
async def cb_export_csv_inline(callback: CallbackQuery):
    await safe_callback_answer(callback, "Формирую CSV...")
    await send_csv_export(callback=callback)

# ----------------- Standard Inline Navigation Callbacks -----------------

@dp.callback_query(F.data.startswith("posts_page_"))
async def cb_posts_page(callback: CallbackQuery):
    await safe_callback_answer(callback)
    parts = callback.data.split("_")
    if len(parts) == 3:
        if parts[2].isdigit():
            filter_val = "all"
            page = int(parts[2])
        else:
            filter_val = parts[2]
            page = 1
    elif len(parts) >= 4:
        filter_val = parts[2]
        page = int(parts[3]) if parts[3].isdigit() else 1
    else:
        filter_val = "all"
        page = 1
    await render_posts_page(message=callback.message, page=page, status_filter=filter_val)

@dp.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery):
    await safe_callback_answer(callback)

@dp.callback_query(F.data == "btn_add_source_hint")
async def cb_add_source_hint(callback: CallbackQuery):
    await safe_callback_answer(callback)
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ К источникам", callback_data="btn_sources")
    await safe_edit_text(
        callback.message,
        "➕ <b>Добавление нового сайта или Telegram-канала:</b>\n\n"
        "Отправьте команду:\n<code>/add_source &lt;Название&gt; &lt;URL&gt;</code>\n\n"
        "<i>Примеры:</i>\n"
        "• Сайт: <code>/add_source Минторг https://www.gov.kz/memleket/entities/mti/press/news</code>\n"
        "• Telegram: <code>/add_source ТГ_Канал https://t.me/s/channel_name</code>",
        reply_markup=builder.as_markup()
    )

@dp.callback_query(F.data == "btn_sources")
async def cb_sources_inline(callback: CallbackQuery):
    if not db.is_admin(callback.from_user.id):
        await safe_callback_answer(callback, "⛔ Доступ ограничен", show_alert=True)
        return
    await safe_callback_answer(callback)
    sources = db.get_all_sources()
    text = f"🌐 <b>Мониторируемые источники Казахстана</b> ({len(sources)} шт.):\n\n"
    builder = InlineKeyboardBuilder()
    for s in sources:
        status_icon = "🟢" if s["is_active"] else "🔴"
        clean_name = html.escape(s['name'])
        clean_url = html.escape(s['url'])
        text += f"{status_icon} <b>{clean_name}</b>\n<code>{clean_url}</code>\n\n"
        toggle_text = f"Выкл #{s['id']}" if s["is_active"] else f"Вкл #{s['id']}"
        builder.button(text=toggle_text, callback_data=f"toggle_src_{s['id']}")
        builder.button(text=f"🗑 #{s['id']}", callback_data=f"del_src_{s['id']}")
    
    builder.adjust(2)
    builder.row(InlineKeyboardButton(text="➕ Добавить источник", callback_data="btn_add_source_hint"))
    await safe_edit_text(callback.message, text, reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("toggle_src_"))
async def cb_toggle_source(callback: CallbackQuery):
    if not db.is_admin(callback.from_user.id):
        await safe_callback_answer(callback, "⛔ Доступ ограничен", show_alert=True)
        return
    src_id = int(callback.data.split("_")[-1])
    db.toggle_source(src_id)
    await safe_callback_answer(callback, "Статус источника изменен!")
    await cb_sources_inline(callback)

@dp.callback_query(F.data.startswith("del_src_"))
async def cb_del_source(callback: CallbackQuery):
    if not db.is_admin(callback.from_user.id):
        await safe_callback_answer(callback, "⛔ Доступ ограничен", show_alert=True)
        return
    src_id = int(callback.data.split("_")[-1])
    db.delete_source(src_id)
    await safe_callback_answer(callback, "Источник удален!")
    await cb_sources_inline(callback)

# ----------------- In-Place Post Viewing & Action Callbacks -----------------

@dp.callback_query(F.data.startswith("view_pack_"))
async def cb_view_pack_tab(callback: CallbackQuery, state: FSMContext = None):
    if state:
        await state.clear()
    await safe_callback_answer(callback)
    parts = callback.data.split("_")
    pack_id = int(parts[2])
    tab = parts[3] if len(parts) > 3 else "card"
    return_page = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 1
    return_filter = parts[5] if len(parts) > 5 else "all"
    await render_pack_view(callback.message, pack_id, tab=tab, return_page=return_page, return_filter=return_filter)

@dp.callback_query(F.data.startswith("open_pack_"))
async def cb_open_pack(callback: CallbackQuery, state: FSMContext = None):
    if state:
        await state.clear()
    await safe_callback_answer(callback)
    parts = callback.data.split("_")
    pack_id = int(parts[2])
    return_page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1
    return_filter = parts[4] if len(parts) > 4 else "all"
    await render_pack_view(callback.message, pack_id, tab="card", return_page=return_page, return_filter=return_filter)

@dp.callback_query(F.data.startswith("show_tg_"))
async def cb_show_tg(callback: CallbackQuery):
    await safe_callback_answer(callback)
    pack_id = int(callback.data.split("_")[-1])
    await render_pack_view(callback.message, pack_id, tab="tg")

@dp.callback_query(F.data.startswith("show_th_"))
async def cb_show_th(callback: CallbackQuery):
    await safe_callback_answer(callback)
    pack_id = int(callback.data.split("_")[-1])
    await render_pack_view(callback.message, pack_id, tab="th")

@dp.callback_query(F.data.startswith("rewrite_simple_"))
async def cb_rewrite_simple(callback: CallbackQuery):
    pack_id = int(callback.data.split("_")[-1])
    pack = db.get_content_pack(pack_id)
    if not pack:
        await safe_callback_answer(callback, "Пост не найден", show_alert=True)
        return
    await safe_callback_answer(callback, "Переписываю простыми словами...")
    msg = await callback.message.answer("⏳ <b>Адаптирую Telegram-пост простым языком без сложных терминов...</b>", parse_mode="HTML")
    new_tg = await ai_service.rewrite_section(
        pack["tg_post"],
        "Перепиши этот Telegram-пост максимально простым человеческим языком для владельцев малого бизнеса Казахстана, без канцелярщины, сохрани структуру и призыв к действию"
    )
    db.update_content_pack_post(pack_id, "tg_post", new_tg)
    try:
        await msg.delete()
    except Exception:
        pass

    await render_pack_view(callback.message, pack_id, tab="tg")

@dp.callback_query(F.data.startswith("rewrite_fines_"))
async def cb_rewrite_fines(callback: CallbackQuery):
    pack_id = int(callback.data.split("_")[-1])
    pack = db.get_content_pack(pack_id)
    if not pack:
        await safe_callback_answer(callback, "Пост не найден", show_alert=True)
        return
    await safe_callback_answer(callback, "Усиливаю акцент на штрафах...")
    msg = await callback.message.answer("⏳ <b>Переписываю с жестким акцентом на проверках КГД, штрафах в МРП и рисках блокировки...</b>", parse_mode="HTML")
    new_tg = await ai_service.rewrite_section(
        pack["tg_post"],
        "Усиль акцент на рисках, проверках КГД РК, штрафах в МРП и блокировках продаж/касс. Сделай тон предупреждающим и мотивирующим срочно навести порядок в Tanbox"
    )
    db.update_content_pack_post(pack_id, "tg_post", new_tg)
    try:
        await msg.delete()
    except Exception:
        pass

    await render_pack_view(callback.message, pack_id, tab="tg")

@dp.callback_query(F.data.startswith("rewrite_custom_"))
async def cb_rewrite_custom(callback: CallbackQuery, state: FSMContext):
    if not db.is_admin(callback.from_user.id):
        await safe_callback_answer(callback, "⛔ Доступ ограничен", show_alert=True)
        return
    pack_id = int(callback.data.split("_")[-1])
    pack = db.get_content_pack(pack_id)
    if not pack:
        await safe_callback_answer(callback, "Пост не найден", show_alert=True)
        return

    await state.set_state(ContentFSM.waiting_for_custom_rewrite)
    await state.update_data(pack_id=pack_id)
    await safe_callback_answer(callback)

    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data=f"open_pack_{pack_id}")
    await safe_edit_text(
        callback.message,
        f"✏️ <b>Редактирование поста #{pack_id}</b>\n\n"
        "Напишите в ответ, что именно нужно изменить или добавить.\n"
        "<i>(Например: «Убери обувь, сделай упор на лекарства и аптеки» или «Сократи на 30%»)</i>:",
        reply_markup=builder.as_markup()
    )

@dp.message(ContentFSM.waiting_for_custom_rewrite, F.text)
async def handle_custom_rewrite_input(message: types.Message, state: FSMContext):
    if not db.is_admin(message.from_user.id):
        await state.clear()
        return

    data = await state.get_data()
    pack_id = data.get("pack_id")
    await state.clear()

    pack = db.get_content_pack(pack_id)
    if not pack:
        await message.answer("⚠️ Пост не найден или был удален.")
        return

    instruction = message.text.strip()
    msg = await message.answer(f"⏳ <b>Переписываю пост с учетом пожелания:</b>\n<i>«{html.escape(instruction)}»</i>...", parse_mode="HTML")
    
    new_tg = await ai_service.rewrite_section(pack["tg_post"], instruction)
    db.update_content_pack_post(pack_id, "tg_post", new_tg)

    builder = InlineKeyboardBuilder()
    builder.button(text="🚀 Опубликовать в @tanboxkz", callback_data=f"publish_tg_{pack_id}")
    builder.button(text=f"📄 Карточка поста #{pack_id}", callback_data=f"open_pack_{pack_id}")
    builder.adjust(1)

    await safe_edit_text(msg, new_tg, header=f"✏️ <b>Обновленный текст сохранен (пост #{pack_id}):</b>\n\n", reply_markup=builder.as_markup())

# ----------------- Publishing & Preview Callbacks -----------------

@dp.callback_query(F.data.startswith("preview_pm_"))
async def cb_preview_pm(callback: CallbackQuery):
    """Sends exact Telegram post preview to admin's private chat"""
    if not db.is_admin(callback.from_user.id):
        await safe_callback_answer(callback, "⛔ Доступ ограничен", show_alert=True)
        return
    parts = callback.data.split("_")
    pack_id = int(parts[2])
    return_page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1
    return_filter = parts[4] if len(parts) > 4 else "all"

    await safe_callback_answer(callback, "Отправляю предпросмотр в ЛС...")
    res = await publisher_service.send_test_preview(
        bot=bot,
        user_id=callback.from_user.id,
        pack_id=pack_id,
        return_page=return_page,
        return_filter=return_filter
    )
    if res.get("status") == "success":
        await safe_callback_answer(callback, "✅ Тестовый пост отправлен вам в чат!", show_alert=True)
    else:
        await safe_callback_answer(callback, f"⚠️ Ошибка: {res.get('message')}", show_alert=True)

@dp.callback_query(F.data.startswith("publish_all_"))
async def cb_publish_all(callback: CallbackQuery):
    """1-Click Multi-Publish to both Telegram and Meta Threads"""
    if not db.is_admin(callback.from_user.id):
        await safe_callback_answer(callback, "⛔ Доступ ограничен", show_alert=True)
        return
    parts = callback.data.split("_")
    pack_id = int(parts[2])
    return_page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1
    return_filter = parts[4] if len(parts) > 4 else "all"

    await safe_callback_answer(callback, "Публикую везде (Telegram + Threads)...")
    wait_msg = await callback.message.answer(
        "🚀 <b>Публикую контент на всех платформах:</b>\n"
        "• Отправка в канал @tanboxkz...\n"
        "• Формирование треда в Threads (@tanbox.kz)...",
        parse_mode="HTML"
    )

    report = await publisher_service.publish_everywhere(bot, pack_id)
    try:
        await wait_msg.delete()
    except Exception:
        pass

    tg_res = report["telegram"]
    th_res = report["threads"]
    
    tg_icon = "🟢" if tg_res.get("status") == "success" else "🔴"
    th_icon = "🟢" if th_res.get("status") == "success" else "🔴"

    lines = [f"📊 <b>Результаты публикации поста #{pack_id}:</b>\n"]
    if tg_res.get("status") == "success":
        lines.append(f"{tg_icon} <b>Telegram:</b> <a href='{tg_res['url']}'>Открыть пост в @tanboxkz</a>")
    else:
        lines.append(f"{tg_icon} <b>Telegram:</b> Ошибка ({html.escape(tg_res.get('message', ''))})")

    if th_res.get("status") == "success":
        lines.append(f"{th_icon} <b>Threads:</b> <a href='{th_res['url']}'>Открыть тред в @tanbox.kz</a> ({th_res.get('post_count', 1)} твита)")
    else:
        lines.append(f"{th_icon} <b>Threads:</b> Ошибка ({html.escape(th_res.get('message', ''))})")

    builder = InlineKeyboardBuilder()
    if tg_res.get("url"):
        builder.button(text="✈️ Пост в Telegram", url=tg_res["url"])
    if th_res.get("url"):
        builder.button(text="🧵 Тред в Threads", url=th_res["url"])
    builder.button(text="📄 Вернуться к карточке", callback_data=f"open_pack_{pack_id}_{return_page}_{return_filter}")
    builder.adjust(1)

    await callback.message.answer(
        "\n".join(lines),
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
        disable_web_page_preview=False
    )
    await render_pack_view(callback.message, pack_id, tab="card", return_page=return_page, return_filter=return_filter)

@dp.callback_query(F.data.startswith("publish_tg_"))
async def cb_publish_tg(callback: CallbackQuery):
    """Publishes single post to Telegram channel"""
    if not db.is_admin(callback.from_user.id):
        await safe_callback_answer(callback, "⛔ Доступ ограничен", show_alert=True)
        return
    pack_id = int(callback.data.split("_")[-1])
    await safe_callback_answer(callback, "Публикую в канал @tanboxkz...")

    res = await publisher_service.publish_to_telegram(bot, pack_id)
    if res.get("status") == "success":
        builder = InlineKeyboardBuilder()
        builder.button(text="✈️ Открыть пост в канале", url=res["url"])
        builder.button(text="📄 Вернуться к карточке", callback_data=f"open_pack_{pack_id}")
        builder.adjust(1)
        await callback.message.answer(
            f"🚀 <b>Пост успешно опубликован в канале TANBOX!</b>\n\n🔗 {res['url']}",
            parse_mode="HTML",
            reply_markup=builder.as_markup(),
            disable_web_page_preview=False
        )
        await render_pack_view(callback.message, pack_id, tab="card")
    else:
        await callback.message.answer(
            f"❌ <b>Не удалось опубликовать в канале:</b>\n<code>{html.escape(res.get('message', ''))}</code>",
            parse_mode="HTML"
        )

@dp.callback_query(F.data.startswith("publish_th_"))
async def cb_publish_threads(callback: CallbackQuery):
    """Publishes single post as a thread to Meta Threads"""
    if not db.is_admin(callback.from_user.id):
        await safe_callback_answer(callback, "⛔ Доступ ограничен", show_alert=True)
        return
    parts = callback.data.split("_")
    pack_id = int(parts[2])
    return_page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1
    return_filter = parts[4] if len(parts) > 4 else "all"

    await safe_callback_answer(callback, "Публикую ветку в Threads...")
    status_msg = await callback.message.answer(
        "⏳ <b>Публикую ветку в Threads (@tanbox.kz)...</b>\n"
        "<i>Создаю медиа-контейнеры и связываю посты через Meta API.</i>",
        parse_mode="HTML"
    )

    res = await publisher_service.publish_to_threads(pack_id)
    try:
        await status_msg.delete()
    except Exception:
        pass

    if res.get("status") == "success":
        builder = InlineKeyboardBuilder()
        builder.button(text="🧵 Открыть опубликованный тред", url=res["url"])
        builder.button(text="📄 Вернуться к карточке", callback_data=f"open_pack_{pack_id}_{return_page}_{return_filter}")
        builder.adjust(1)

        await callback.message.answer(
            f"🎉 <b>Тред успешно опубликован в Threads (@tanbox.kz)!</b>\n\n"
            f"📊 <b>Опубликовано твитов:</b> <code>{res['post_count']} шт.</code>\n"
            f"🔗 {res['url']}",
            parse_mode="HTML",
            reply_markup=builder.as_markup(),
            disable_web_page_preview=False
        )
        await render_pack_view(callback.message, pack_id, tab="th", return_page=return_page, return_filter=return_filter)
    else:
        await callback.message.answer(
            f"❌ <b>Ошибка публикации в Threads:</b>\n<code>{html.escape(res.get('message', 'Неизвестная ошибка'))}</code>",
            parse_mode="HTML"
        )

@dp.callback_query(F.data.startswith("del_pack_"))
async def cb_del_pack(callback: CallbackQuery):
    if not db.is_admin(callback.from_user.id):
        await safe_callback_answer(callback, "⛔ Доступ ограничен", show_alert=True)
        return
    parts = callback.data.split("_")
    pack_id = int(parts[2])
    return_page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1
    return_filter = parts[4] if len(parts) > 4 else "all"
    db.delete_content_pack(pack_id)
    await safe_callback_answer(callback, f"Пост #{pack_id} удален!")
    await render_posts_page(message=callback.message, page=return_page, status_filter=return_filter)

# ----------------- Message Handler (Links & Custom Topics) -----------------

@dp.message(F.text)
async def handle_user_text_input(message: types.Message, state: FSMContext = None):
    user_id = message.from_user.id
    if not db.is_admin(user_id):
        await message.answer("⛔ Доступ ограничен.")
        return

    user_input = message.text.strip()

    if user_input.startswith("http://") or user_input.startswith("https://"):
        msg = await message.answer("🔎 <b>Загружаю материал по ссылке и провожу экспертный анализ...</b>", parse_mode="HTML")
        article = scraper.extract_article_content(user_input)
        title = article["title"] or "Регуляторная новость РК"
        content = article["text"] or user_input

        score, reason = await ai_service.evaluate_relevance(title, content)
        await msg.edit_text(f"⚖️ Релевантность теме ИС МПТ: <b>{score}/10</b> ({html.escape(reason)})\nГенерирую материалы для Telegram и Threads...", parse_mode="HTML")
        
        pack = await ai_service.generate_posts_from_news(title, content, user_input)
        pack_id = db.save_content_pack(
            news_id=None,
            title=title,
            source_url=user_input,
            full_response=pack["full_text"],
            tg_post=pack["tg_post"],
            threads_post=pack["threads_post"]
        )
        try:
            await msg.delete()
        except Exception:
            pass
        saved_pack = db.get_content_pack(pack_id)
        await sync_to_google_sheet(saved_pack)
        await send_content_pack_summary(message.chat.id, pack_id, title, pack["tg_post"][:350] + "...")
    else:
        # Custom topic
        msg = await message.answer(f"✍️ Принята тема: <i>«{html.escape(user_input)}»</i>\nГенерирую комплект постов для Telegram и Threads...", parse_mode="HTML")
        pack = await ai_service.generate_evergreen_posts(topic=user_input)
        pack_id = db.save_content_pack(
            news_id=None,
            title=user_input,
            source_url="Ручной ввод темы",
            full_response=pack["full_text"],
            tg_post=pack["tg_post"],
            threads_post=pack["threads_post"]
        )
        try:
            await msg.delete()
        except Exception:
            pass
        saved_pack = db.get_content_pack(pack_id)
        await sync_to_google_sheet(saved_pack)
        await send_content_pack_summary(message.chat.id, pack_id, user_input, pack["tg_post"][:350] + "...")

# ----------------- Helper Delivery Function -----------------

async def send_content_pack_summary(chat_id: int, pack_id: int, title: str, preview: str):
    clean_title = html.escape(title)
    clean_preview = html.escape(preview)
    text = (
        f"🔥 <b>Новый контент-пакет готов!</b>\n\n"
        f"🎯 <b>Тема:</b> {clean_title}\n\n"
        f"📝 <b>Превью:</b>\n<i>{clean_preview}</i>\n\n"
        "👇 <b>Выберите действие или формат:</b>"
    )
    await bot.send_message(
        chat_id,
        text,
        reply_markup=get_pack_keyboard(pack_id, current_tab="card"),
        parse_mode="HTML"
    )

# ----------------- Scheduled Job: Check Due Scheduled Posts (Every 60s) -----------------

async def check_and_publish_scheduled_posts():
    """Checks for posts whose scheduled_at time has arrived and publishes them everywhere"""
    due_packs = db.get_due_scheduled_packs()
    if not due_packs:
        return

    admins = db.get_all_admins()
    for p in due_packs:
        pack_id = p["id"]
        title = p.get("title", "")
        logger.info(f"[Auto-Post] Auto-publishing scheduled pack #{pack_id}...")
        report = await publisher_service.publish_everywhere(bot, pack_id)

        tg_res = report["telegram"]
        th_res = report["threads"]

        notify_text = (
            f"🚀 <b>[Авто-постинг] Опубликован отложенный пост #{pack_id}!</b>\n\n"
            f"🎯 <b>Тема:</b> <i>{html.escape(title)}</i>\n"
        )
        if tg_res.get("status") == "success":
            notify_text += f"\n✈️ <a href='{tg_res['url']}'>Открыть пост в Telegram</a>"
        if th_res.get("status") == "success":
            notify_text += f"\n🧵 <a href='{th_res['url']}'>Открыть тред в Threads (@tanbox.kz)</a>"

        for admin_id in admins:
            try:
                await bot.send_message(admin_id, notify_text, parse_mode="HTML", disable_web_page_preview=True)
            except Exception:
                pass

# ----------------- Scheduled Job: Check Deadlines (Daily at 09:00) -----------------

async def check_regulatory_deadlines_daily():
    """Checks if any critical Kazakhstan deadlines are 14, 7, or 2 days away"""
    alerts = db.get_due_deadline_alerts()
    if not alerts:
        return

    admins = db.get_all_admins()
    for dl in alerts:
        days = dl.get("days_left", 7)
        title = dl.get("title", "")
        
        try:
            pack = await ai_service.generate_deadline_warning_post(dl)
            pack_id = db.save_content_pack(
                news_id=None,
                title=f"⚠️ Дедлайн ({days} дн.): {title}",
                source_url=f"Календарь регулятора РК ({dl['deadline_date']})",
                full_response=pack["full_text"],
                tg_post=pack["tg_post"],
                threads_post=pack["threads_post"]
            )
            db.mark_deadline_notified(dl["id"], days)

            alert_text = (
                f"🚨 <b>Внимание: до дедлайна осталось {days} дней!</b>\n\n"
                f"🎯 <b>Событие:</b> {html.escape(title)}\n"
                f"📅 <b>Крайний срок:</b> <code>{dl['deadline_date']}</code>\n"
                f"⚖️ <b>Штрафы:</b> <code>{html.escape(dl.get('fines_info', ''))}</code>\n\n"
                f"🤖 Бот автоматически подготовил готовый предупреждающий контент-пак #{pack_id}!\n"
                "Нажмите кнопку ниже, чтобы посмотреть или сразу опубликовать:"
            )
            builder = InlineKeyboardBuilder()
            builder.button(text=f"📄 Открыть пост #{pack_id}", callback_data=f"open_pack_{pack_id}")
            builder.button(text="🚀 Опубликовать в @tanboxkz", callback_data=f"publish_tg_{pack_id}")
            builder.adjust(1)

            for admin_id in admins:
                try:
                    await bot.send_message(admin_id, alert_text, reply_markup=builder.as_markup(), parse_mode="HTML")
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Error generating deadline alert post: {e}")

# ----------------- Scheduled Job: Sync Views Analytics (Every 6h) -----------------

async def sync_views_analytics_periodic():
    """Scrapes views from @tanboxkz and syncs them into SQLite"""
    try:
        views_map = scraper.fetch_tanbox_channel_metrics("tanboxkz")
        if views_map:
            db.sync_channel_metrics(views_map)
            logger.info(f"Synced views for {len(views_map)} posts from @tanboxkz.")
    except Exception as e:
        logger.warning(f"Periodic views sync error: {e}")

# ----------------- Autonomous Monitoring Loop with Anti-Flood -----------------

async def run_news_check_cycle(notify_chat_id: int = None) -> int:
    """Core autonomous loop that checks all 13 sources without spamming admins"""
    is_manual = notify_chat_id is not None

    if not is_manual and not db.is_scanner_enabled():
        logger.info("Autonomous news check is PAUSED by admin. Skipping cycle.")
        return 0

    logger.info(f"Starting news check cycle across 13 sources (Manual={is_manual})...")
    sources = db.get_active_sources()
    relevant_found = 0
    created_packs = []
    MAX_PACKS_PER_CYCLE = 2  # Hard limit of 2 posts per cycle to prevent flooding!

    admins = [notify_chat_id] if notify_chat_id else db.get_all_admins()

    for s in sources:
        if relevant_found >= MAX_PACKS_PER_CYCLE:
            logger.info(f"Reached limit of {MAX_PACKS_PER_CYCLE} posts per cycle. Stopping scan to prevent flood.")
            break

        try:
            items = []
            stype = s.get("source_type")
            if stype == "uchet_news":
                items = scraper.parse_uchet_news(s["url"])
            elif stype == "uchet_questions":
                items = scraper.parse_uchet_questions(s["url"])
            elif stype == "rss":
                items = scraper.parse_rss_feed(s["url"])
            elif stype == "telegram_channel":
                items = scraper.parse_telegram_channel(s["url"])
            elif stype == "legal_egov":
                items = scraper.parse_legalacts_npa(s["url"])
            else:
                items = scraper.parse_generic_page(s["url"])
            
            for item in items[:6]:
                if relevant_found >= MAX_PACKS_PER_CYCLE:
                    break

                url = item["url"]
                title = item["title"]

                if db.should_skip_news(url):
                    continue
                
                try:
                    article = scraper.extract_article_content(url)
                    content = article["text"] or item.get("summary", "")
                    
                    if not content or len(content) < 40:
                        continue
                    
                    score, reason = await ai_service.evaluate_relevance(title, content)
                    is_relevant = score >= 8
                    
                    if is_relevant:
                        logger.info(f"Found high impact news ({score}/10): {title}")
                        pack = await ai_service.generate_posts_from_news(title, content, url)
                        if pack.get("tg_post"):
                            news_id = db.mark_news_processed(s["id"], url, title, score, True)
                            pack_id = db.save_content_pack(
                                news_id=news_id,
                                title=title,
                                source_url=url,
                                full_response=pack["full_text"],
                                tg_post=pack["tg_post"],
                                threads_post=pack["threads_post"]
                            )
                            relevant_found += 1
                            created_packs.append({
                                "id": pack_id,
                                "title": title,
                                "score": score
                            })
                            saved_p = db.get_content_pack(pack_id)
                            await sync_to_google_sheet(saved_p)
                    else:
                        db.mark_news_processed(s["id"], url, title, score, False)
                except Exception as item_err:
                    logger.error(f"Error processing news item {url}: {item_err}")
        except Exception as src_err:
            logger.error(f"Error processing source {s.get('name')}: {src_err}")

    # Send ONE combined summary digest instead of spamming separate messages
    if created_packs and admins:
        pack_lines = [f"📌 <b>#{p['id']}</b> <i>{html.escape(p['title'])}</i>\n🎯 Важность: <code>{p['score']}/10</code>" for p in created_packs]
        digest_text = (
            f"⚡ <b>Сканирование завершено! Подготовлено материалов: {len(created_packs)}</b>\n\n"
            + "\n\n".join(pack_lines)
            + "\n\n👇 Откройте нужный пост для просмотра, публикации или планирования:"
        )
        builder = InlineKeyboardBuilder()
        for p in created_packs:
            builder.button(text=f"📄 Открыть #{p['id']}", callback_data=f"open_pack_{p['id']}")
        builder.adjust(2)
        builder.row(InlineKeyboardButton(text="📁 Все черновики", callback_data="posts_page_1"))

        for admin_id in admins:
            try:
                await bot.send_message(admin_id, digest_text, reply_markup=builder.as_markup(), parse_mode="HTML")
            except Exception as e:
                logger.error(f"Failed to notify admin {admin_id}: {e}")

    logger.info(f"News check cycle completed. Relevant items created: {relevant_found}")
    return relevant_found

# ----------------- Error Handling -----------------

@dp.errors()
async def global_error_handler(event: types.ErrorEvent):
    """Gracefully handles expired button queries and unexpected errors"""
    if isinstance(event.exception, TelegramBadRequest) and ("query is too old" in str(event.exception).lower() or "query id is invalid" in str(event.exception).lower()):
        logger.info("Игнорирован устаревший клик по кнопке (таймаут Telegram истек).")
        return True
    logger.error(f"Необработанная ошибка обновления: {event.exception}", exc_info=True)
    return True

# ----------------- Startup -----------------

async def on_startup():
    db.init_db()
    logger.info("Database initialized.")

    # 1. Scheduled auto-publisher (checks every 60 seconds)
    scheduler.add_job(
        check_and_publish_scheduled_posts,
        "interval",
        minutes=1,
        id="scheduled_posts_worker"
    )

    # 2. Regulatory deadline daily alert (every 24h)
    scheduler.add_job(
        check_regulatory_deadlines_daily,
        "interval",
        hours=24,
        next_run_time=datetime.now() + timedelta(minutes=10),
        id="deadline_alerts_worker"
    )

    # 3. Channel views analytics sync (every 6 hours)
    scheduler.add_job(
        sync_views_analytics_periodic,
        "interval",
        hours=6,
        next_run_time=datetime.now() + timedelta(minutes=2),
        id="views_analytics_worker"
    )

    # 4. Hourly background news scan across 13 sources
    first_scan = datetime.now() + timedelta(minutes=config.CHECK_INTERVAL_MINUTES)
    scheduler.add_job(
        run_news_check_cycle,
        "interval",
        minutes=config.CHECK_INTERVAL_MINUTES,
        next_run_time=first_scan,
        id="news_checker"
    )

    scheduler.start()
    logger.info("Scheduler started with auto-posting, deadline alerts, views analytics, and news monitoring.")

async def main():
    await on_startup()
    logger.info("Bot starting polling...")
    try:
        await dp.start_polling(bot, drop_pending_updates=True)
    finally:
        scheduler.shutdown()
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
