from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
import config
import database as db

# ----------------- Persistent Bottom Reply Keyboard -----------------

def get_main_reply_keyboard() -> ReplyKeyboardMarkup:
    """Persistent 10-button keyboard docked below the message input"""
    builder = ReplyKeyboardBuilder()
    is_active = db.is_scanner_enabled()
    scanner_btn = "🟢 Авто-поиск: ВКЛ" if is_active else "🔴 Авто-поиск: ПАУЗА"

    # Row 1: Core content & Manual check
    builder.row(
        KeyboardButton(text="📁 Посты и черновики"),
        KeyboardButton(text="⚡ Сканировать сейчас")
    )
    # Row 2: Queue & Regulatory Deadlines
    builder.row(
        KeyboardButton(text="🤖 Автопилот (Сетка)"),
        KeyboardButton(text="📅 Дедлайны РК (Календарь)")
    )
    # Row 3: Analytics & Evergreen generator
    builder.row(
        KeyboardButton(text="📈 Аналитика @tanboxkz"),
        KeyboardButton(text="💡 Банк болей (Evergreen)")
    )
    # Row 4: Scanner toggle & Monitored Sources
    builder.row(
        KeyboardButton(text=scanner_btn),
        KeyboardButton(text="🌐 Источники Казахстана")
    )
    # Row 5: Export & Help
    builder.row(
        KeyboardButton(text="📥 Скачать Excel/CSV"),
        KeyboardButton(text="ℹ️ Справка (/help)")
    )
    return builder.as_markup(resize_keyboard=True, is_persistent=True)

# ----------------- Post Viewing & Action Keyboard -----------------

def get_pack_keyboard(
    pack_id: int,
    current_tab: str = "card",
    status: str = "pending",
    scheduled_at: str = None,
    return_page: int = 1,
    return_filter: str = "all",
    threads_post_id: str = None,
    channel_message_id: int = None
) -> InlineKeyboardMarkup:
    """Unified action and tab switcher for post viewing"""
    builder = InlineKeyboardBuilder()
    
    card_lbl = "• Обзор •" if current_tab == "card" else "📄 Обзор"
    tg_lbl = "• Telegram •" if current_tab == "tg" else "📌 Telegram"
    th_lbl = "• Threads •" if current_tab == "th" else "🧵 Threads"

    # Row 1: Platform format tabs (3 buttons: Overview, Telegram, Threads)
    builder.button(text=card_lbl, callback_data=f"view_pack_{pack_id}_card_{return_page}_{return_filter}")
    builder.button(text=tg_lbl, callback_data=f"view_pack_{pack_id}_tg_{return_page}_{return_filter}")
    builder.button(text=th_lbl, callback_data=f"view_pack_{pack_id}_th_{return_page}_{return_filter}")
    
    # Row 2: AI Rewriters (3 buttons)
    builder.button(text="🔄 Проще", callback_data=f"rewrite_simple_{pack_id}")
    builder.button(text="⚠️ Штрафы", callback_data=f"rewrite_fines_{pack_id}")
    builder.button(text="✏️ Правка", callback_data=f"rewrite_custom_{pack_id}")
    
    # Row 3: Super-Button: 1-Click Multi-Publish Everywhere (1 button)
    is_tg_published = (status == "published")
    is_th_published = bool(threads_post_id)
    
    if is_tg_published and is_th_published:
        builder.button(text="✅ Опубликовано везде (TG + Threads)", callback_data="noop")
    else:
        builder.button(
            text="🚀 Опубликовать везде (TG + Threads)",
            callback_data=f"publish_all_{pack_id}_{return_page}_{return_filter}"
        )

    # Row 4: Test in PM + Platform-specific buttons
    builder.button(
        text="👁 Тест в ЛС",
        callback_data=f"preview_pm_{pack_id}_{return_page}_{return_filter}"
    )

    if is_tg_published:
        tg_url = f"https://t.me/tanboxkz/{channel_message_id}" if channel_message_id else "https://t.me/tanboxkz"
        builder.button(text="✈️ В канале ✅", url=tg_url)
    else:
        builder.button(text="✈️ В канал @tanboxkz", callback_data=f"publish_tg_{pack_id}")

    username = config.THREADS_USERNAME or "tanbox.kz"
    if is_th_published:
        builder.button(text="🧵 В Threads ✅", url=f"https://www.threads.net/@{username}/post/{threads_post_id}")
    else:
        builder.button(
            text="🧵 В Threads",
            callback_data=f"publish_th_{pack_id}_{return_page}_{return_filter}"
        )

    # Row 5: Scheduling
    if status == "scheduled":
        sched_short = scheduled_at[5:16] if scheduled_at else ""
        builder.button(text=f"⏰ В плане: {sched_short} (Изменить)", callback_data=f"sched_pack_{pack_id}")
    else:
        builder.button(text="⚡ В Автопилот", callback_data=f"autopilot_{pack_id}_{return_page}_{return_filter}")
        builder.button(text="⏰ Свое время", callback_data=f"sched_pack_{pack_id}")

    # Row 6: Navigation
    builder.button(text="⬅️ К списку постов", callback_data=f"posts_page_{return_filter}_{return_page}")
    builder.button(text="🗑 Удалить пост", callback_data=f"del_pack_{pack_id}_{return_page}_{return_filter}")
    
    row5_count = 1 if status == "scheduled" else 2
    builder.adjust(3, 3, 1, 3, row5_count, 2)
    return builder.as_markup()

# ----------------- Test Preview Actions Keyboard -----------------

def get_test_preview_keyboard(pack_id: int, return_page: int = 1, return_filter: str = "all") -> InlineKeyboardMarkup:
    """Attached beneath the test preview message sent to admin's private chat"""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🚀 Опубликовать везде (TG + Threads)",
        callback_data=f"publish_all_{pack_id}_{return_page}_{return_filter}"
    )
    builder.button(
        text="⚡ В Автопилот (ближайший B2B слот)",
        callback_data=f"autopilot_{pack_id}_{return_page}_{return_filter}"
    )
    builder.button(
        text="✈️ Только в канал @tanboxkz",
        callback_data=f"publish_tg_{pack_id}"
    )
    builder.button(
        text="🧵 Только в Threads",
        callback_data=f"publish_th_{pack_id}_{return_page}_{return_filter}"
    )
    builder.button(
        text="📄 Вернуться к карточке поста",
        callback_data=f"open_pack_{pack_id}_{return_page}_{return_filter}"
    )
    builder.adjust(1)
    return builder.as_markup()

# ----------------- Scheduling Slots Keyboard -----------------

def get_schedule_slots_keyboard(pack_id: int) -> InlineKeyboardMarkup:
    """Quick slot selection for scheduled posting"""
    builder = InlineKeyboardBuilder()
    
    # Row 1: Immediate relative delays
    builder.button(text="⚡ Через 10 мин", callback_data=f"sched_slot_{pack_id}_m10")
    builder.button(text="⚡ Через 30 мин", callback_data=f"sched_slot_{pack_id}_m30")
    builder.button(text="⚡ Через 1 час", callback_data=f"sched_slot_{pack_id}_h1")
    
    # Row 2: Today / Tomorrow business hours
    builder.button(text="☀️ Сегодня в 18:00", callback_data=f"sched_slot_{pack_id}_today18")
    builder.button(text="☀️ Завтра в 11:00", callback_data=f"sched_slot_{pack_id}_tomorrow")
    
    # Row 3: Prime Kazakhstan business peak days
    builder.button(text="📅 Вторник 11:00", callback_data=f"sched_slot_{pack_id}_tue")
    builder.button(text="📅 Четверг 11:00", callback_data=f"sched_slot_{pack_id}_thu")
    
    # Row 4: Intelligent autopilot
    builder.button(text="🚀 Автопилот (умное расписание)", callback_data=f"sched_slot_{pack_id}_autopilot")
    
    # Row 5: Flexible text input
    builder.button(text="⌨️ Свое время (в свободной форме)", callback_data=f"sched_slot_{pack_id}_custom")
    
    # Row 6: Navigation / Cancel
    builder.button(text="❌ Снять с расписания", callback_data=f"sched_cancel_{pack_id}")
    builder.button(text="⬅️ Назад к посту", callback_data=f"open_pack_{pack_id}")
    
    builder.adjust(3, 2, 2, 1, 1, 2)
    return builder.as_markup()

# ----------------- Autopilot Queue Keyboard -----------------

def get_autopilot_queue_keyboard(has_pending: bool = True) -> InlineKeyboardMarkup:
    """Action buttons for smart autopilot grid view"""
    builder = InlineKeyboardBuilder()
    if has_pending:
        builder.button(
            text="⚡ Заполнить автопилот (Топ-3 черновика)",
            callback_data="autopilot_fill_batch"
        )
    builder.button(
        text="📁 Открыть черновики",
        callback_data="posts_page_pending_1"
    )
    builder.button(
        text="🔄 Обновить сетку",
        callback_data="autopilot_refresh"
    )
    builder.adjust(1)
    return builder.as_markup()

# ----------------- Posts List Keyboard -----------------

def get_posts_page_keyboard(
    items: list[dict],
    page: int,
    total_pages: int,
    status_filter: str,
    counts: dict
) -> InlineKeyboardMarkup:
    """Constructs the compact numbered buttons and filter tabs for post list"""
    builder = InlineKeyboardBuilder()
    
    # Row 1: Compact numbered buttons
    if items:
        num_icons = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
        post_buttons = []
        for idx, p in enumerate(items, 1):
            num_badge = num_icons[idx - 1] if idx <= len(num_icons) else f"{idx}."
            post_buttons.append(
                InlineKeyboardButton(
                    text=f"{num_badge} #{p['id']}",
                    callback_data=f"open_pack_{p['id']}_{page}_{status_filter}"
                )
            )
        builder.row(*post_buttons)

    # Row 2: Pagination
    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"posts_page_{status_filter}_{page - 1}"))
    else:
        nav_row.append(InlineKeyboardButton(text="◀️", callback_data="noop"))
    nav_row.append(InlineKeyboardButton(text=f"• {page}/{total_pages} •", callback_data="noop"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton(text="Вперед ▶️", callback_data=f"posts_page_{status_filter}_{page + 1}"))
    else:
        nav_row.append(InlineKeyboardButton(text="▶️", callback_data="noop"))
    builder.row(*nav_row)

    # Row 3 & 4: Category filter tabs
    f_pending = f"• 🟡 Черновики ({counts['pending']}) •" if status_filter == "pending" else f"🟡 Черновики ({counts['pending']})"
    f_pub = f"• 🟢 Опубликовано ({counts['published']}) •" if status_filter == "published" else f"🟢 Опубликовано ({counts['published']})"
    builder.row(
        InlineKeyboardButton(text=f_pending, callback_data="posts_page_pending_1"),
        InlineKeyboardButton(text=f_pub, callback_data="posts_page_published_1")
    )

    f_sched = f"• ⏰ В плане ({counts['scheduled']}) •" if status_filter == "scheduled" else f"⏰ В плане ({counts['scheduled']})"
    f_all = f"• 🌐 Все ({counts['all']}) •" if status_filter == "all" else f"🌐 Все ({counts['all']})"
    builder.row(
        InlineKeyboardButton(text=f_sched, callback_data="posts_page_scheduled_1"),
        InlineKeyboardButton(text=f_all, callback_data="posts_page_all_1")
    )

    return builder.as_markup()

