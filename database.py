import sqlite3
import os
import csv
from datetime import datetime, timedelta
from config import DB_PATH, ADMIN_IDS, DATA_DIR

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    # Sources table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        url TEXT NOT NULL UNIQUE,
        source_type TEXT NOT NULL,
        is_active INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # Processed news table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS processed_news (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id INTEGER,
        url TEXT UNIQUE,
        title TEXT,
        relevance_score INTEGER DEFAULT 0,
        is_relevant INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # Generated content packs
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS content_packs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        news_id INTEGER,
        title TEXT,
        source_url TEXT,
        full_response TEXT,
        tg_post TEXT,
        ig_post TEXT,
        threads_post TEXT,
        status TEXT DEFAULT 'pending',
        scheduled_at TIMESTAMP,
        published_at TIMESTAMP,
        channel_message_id INTEGER,
        views_count INTEGER DEFAULT 0,
        reactions_count INTEGER DEFAULT 0,
        last_analyzed_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # Migrations for content_packs (in case table already existed without new columns)
    existing_cols = [r["name"] for r in cursor.execute("PRAGMA table_info(content_packs)").fetchall()]
    new_cols = [
        ("scheduled_at", "TIMESTAMP"),
        ("published_at", "TIMESTAMP"),
        ("channel_message_id", "INTEGER"),
        ("views_count", "INTEGER DEFAULT 0"),
        ("reactions_count", "INTEGER DEFAULT 0"),
        ("last_analyzed_at", "TIMESTAMP"),
        ("threads_post_id", "TEXT"),
        ("threads_published_at", "TIMESTAMP")
    ]
    for col_name, col_type in new_cols:
        if col_name not in existing_cols:
            try:
                cursor.execute(f"ALTER TABLE content_packs ADD COLUMN {col_name} {col_type};")
            except Exception:
                pass

    # Authorized Admins
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS admins (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        is_active INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # Settings table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    """)

    # Regulatory Deadlines Kazakhstan table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS regulatory_deadlines (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        deadline_date TEXT NOT NULL, -- YYYY-MM-DD
        category TEXT NOT NULL,       -- 'marking', 'taxes', 'reporting'
        target_business TEXT,        -- 'Малый/средний бизнес', 'Аптеки', 'Обувные ритейлеры'
        description TEXT,
        fines_info TEXT,
        last_notified_days TEXT,     -- e.g. '14,7'
        is_active INTEGER DEFAULT 1
    );
    """)

    # Indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_processed_news_url ON processed_news (url);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_content_packs_status ON content_packs (status);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_content_packs_sched ON content_packs (scheduled_at);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sources_active ON sources (is_active);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_deadlines_date ON regulatory_deadlines (deadline_date);")
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_deadlines_unique ON regulatory_deadlines (title, deadline_date);")

    # Migration: update outdated markirovka.kz url if present in DB
    cursor.execute("""
        UPDATE sources
        SET url = 'https://tanba.telecom.kz/ru/news', name = 'Tañba (Казахтелеком)'
        WHERE url = 'https://markirovka.kz/ru/news'
    """)

    # Extended default sources (Websites + Telegram Channels + Open NPA)
    default_sources = [
        # Regulatory Websites & Portals
        ("Uchet.kz Новости", "https://uchet.kz/news/", "uchet_news"),
        ("Uchet.kz RSS", "https://uchet.kz/news/rss/", "rss"),
        ("Tañba (Казахтелеком)", "https://tanba.telecom.kz/ru/news", "web_page"),
        ("Tañba B2B Новости", "https://b2b.telecom.kz/ru/news/markirovka/drugie-novosti", "web_page"),
        ("Uchet.kz Разъяснения КГД (СНТ/ЭСФ)", "https://uchet.kz/question/consult-po-is-esf-vs-snt/", "uchet_questions"),
        ("Uchet.kz Налоговые консультации", "https://uchet.kz/question/konsultatsii-nalogovykh-organov/", "uchet_questions"),
        ("Inbusiness.kz Новости", "https://inbusiness.kz/ru/rss", "rss"),
        ("Курсив Казахстан (Бизнес)", "https://kz.kursiv.media/feed/", "rss"),
        # Telegram Channels (Zero latency)
        ("ТГ: Параграф Бухгалтер РК", "https://t.me/s/prg_buh", "telegram_channel"),
        ("ТГ: MyBuh.kz (Налоги/Бизнес)", "https://t.me/s/mybuh_kz", "telegram_channel"),
        ("ТГ: Uchet.kz Новости", "https://t.me/s/uchet_kz", "telegram_channel"),
        ("ТГ: Inbusiness.kz", "https://t.me/s/inbusinesskz", "telegram_channel"),
        # Draft Legal Acts
        ("Открытые НПА (legalacts.egov.kz)", "https://legalacts.egov.kz/list", "legal_egov"),
    ]
    cursor.executemany(
        "INSERT OR IGNORE INTO sources (name, url, source_type) VALUES (?, ?, ?)",
        default_sources
    )

    # Seed Regulatory Deadlines Kazakhstan (Real 2026/2027 Regulatory Dates)
    default_deadlines = [
        (
            "Сдача ФНО 910.00 за 2 полугодие",
            "2026-11-15",
            "reporting",
            "Малый бизнес на упрощенке (ИП и ТОО)",
            "Срок сдачи формы 910.00 (упрощенная декларация). Опоздание влечет предупреждение или штраф по ст. 272 КоАП РК (от 15 до 70 МРП).",
            "Штраф по ст. 272 КоАП: 15–70 МРП (от 58 980 до 275 240 ₸)"
        ),
        (
            "Сдача ФНО 300.00 по НДС за 3 квартал",
            "2026-11-15",
            "taxes",
            "Плательщики НДС",
            "Крайний срок подачи декларации по налогу на добавленную стоимость. Нарушение влечет блокировку расчетных счетов КГД.",
            "Приостановление расходных операций по банковским счетам"
        ),
        (
            "Ежемесячный дедлайн налогов и соцплатежей за работников",
            "2026-11-25",
            "taxes",
            "Все работодатели (ИП и ТОО)",
            "Срок уплаты ОПВ, СО, ВОСМС, ООСМС и ИПН у источника выплаты за предыдущий месяц.",
            "Пеня 1.25 базовой ставки НБ РК за каждый день просрочки"
        ),
        (
            "Окончание переходного периода по маркировке остатков обуви",
            "2026-12-31",
            "marking",
            "Обувной ритейл, оптовики, импортеры",
            "Полный запрет на продажу и оборот немаркированных остатков обуви в Казахстане. Только DataMatrix коды.",
            "Штраф по ст. 283-1 КоАП: от 50 до 500 МРП с полной конфискацией обуви"
        ),
        (
            "Ввод новых ставок МРП и МЗП на 2027 год",
            "2026-12-31",
            "reporting",
            "Все субъекты предпринимательства РК",
            "Пересчет минимальной зарплаты, налоговых порогов и размеров штрафов в МРП.",
            "Перенастройка учетных систем 1С и кассовых аппаратов"
        ),
        (
            "100% обязательная поштучная маркировка лекарств",
            "2027-01-01",
            "marking",
            "Аптеки, фармацевтические дистрибьюторы, клиники",
            "Полный запрет на отпуск любых лекарств без сканирования 2D-кода DataMatrix и фиксации выбытия в ИС МПТ.",
            "Лишение лицензии + штрафы по ст. 426 КоАП РК"
        ),
        (
            "Сдача годовой отчетности ФНО 100.00 и 220.00",
            "2027-03-31",
            "reporting",
            "ТОО на ОУР и ИП на общеустановленном режиме",
            "Годовые декларации по корпоративному (КПН) и индивидуальному подоходному налогу.",
            "Штраф до 250 МРП и ограничение счетов"
        ),
        (
            "Маркировка упакованных безалкогольных напитков (Этап 3)",
            "2027-07-01",
            "marking",
            "Производители напитков, дистрибьюторы, ритейл",
            "Обязательный учет и сканирование упакованных соков, вод и напитков на кассах.",
            "Штрафы за торговлю без маркировки по КоАП 283-1"
        ),
    ]
    for d in default_deadlines:
        cursor.execute("SELECT id FROM regulatory_deadlines WHERE title = ? AND deadline_date = ?", (d[0], d[1]))
        if not cursor.fetchone():
            cursor.execute("""
                INSERT INTO regulatory_deadlines 
                (title, deadline_date, category, target_business, description, fines_info, is_active)
                VALUES (?, ?, ?, ?, ?, ?, 1)
            """, d)

    # Pre-seed admins from ADMIN_IDS
    if ADMIN_IDS:
        for a_id in ADMIN_IDS:
            cursor.execute(
                "INSERT OR IGNORE INTO admins (user_id, username, first_name, is_active) VALUES (?, '', 'Owner', 1)",
                (a_id,)
            )

    conn.commit()
    conn.close()

# Source management
def add_source(name: str, url: str, source_type: str = "web_page") -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO sources (name, url, source_type) VALUES (?, ?, ?)",
            (name, url, source_type)
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

def get_active_sources():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM sources WHERE is_active = 1")
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_all_sources():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM sources ORDER BY id ASC")
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def toggle_source(source_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE sources SET is_active = 1 - is_active WHERE id = ?", (source_id,))
    conn.commit()
    conn.close()

def delete_source(source_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM sources WHERE id = ?", (source_id,))
    conn.commit()
    conn.close()

# News deduplication
def should_skip_news(url: str) -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, is_relevant FROM processed_news WHERE url = ?", (url,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False
    
    news_id, is_relevant = row[0], row[1]
    if not is_relevant:
        conn.close()
        return True
        
    cursor.execute("SELECT id FROM content_packs WHERE news_id = ? OR source_url = ?", (news_id, url))
    pack = cursor.fetchone()
    conn.close()
    return pack is not None

def mark_news_processed(source_id: int, url: str, title: str, relevance_score: int, is_relevant: bool) -> int:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO processed_news (source_id, url, title, relevance_score, is_relevant)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
            relevance_score = excluded.relevance_score,
            is_relevant = excluded.is_relevant
        """,
        (source_id, url, title, relevance_score, 1 if is_relevant else 0)
    )
    cursor.execute("SELECT id FROM processed_news WHERE url = ?", (url,))
    row = cursor.fetchone()
    news_id = row[0] if row else cursor.lastrowid
    conn.commit()
    conn.close()
    return news_id

# Content pack management
def save_content_pack(news_id: int, title: str, source_url: str, full_response: str, tg_post: str, threads_post: str = "", ig_post: str = "") -> int:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO content_packs (news_id, title, source_url, full_response, tg_post, ig_post, threads_post, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
        """,
        (news_id, title, source_url, full_response, tg_post, ig_post, threads_post)
    )
    pack_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return pack_id

def get_content_pack(pack_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM content_packs WHERE id = ?", (pack_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def update_content_pack_status(pack_id: int, status: str, channel_message_id: int = None):
    conn = get_connection()
    cursor = conn.cursor()
    now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if status == "published":
        if channel_message_id:
            cursor.execute(
                "UPDATE content_packs SET status = ?, published_at = ?, channel_message_id = ? WHERE id = ?",
                (status, now_iso, channel_message_id, pack_id)
            )
        else:
            cursor.execute(
                "UPDATE content_packs SET status = ?, published_at = ? WHERE id = ?",
                (status, now_iso, pack_id)
            )
    else:
        cursor.execute("UPDATE content_packs SET status = ? WHERE id = ?", (status, pack_id))
    conn.commit()
    conn.close()

def update_content_pack_threads_published(pack_id: int, threads_post_id: str):
    conn = get_connection()
    cursor = conn.cursor()
    now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "UPDATE content_packs SET threads_post_id = ?, threads_published_at = ? WHERE id = ?",
        (threads_post_id, now_iso, pack_id)
    )
    conn.commit()
    conn.close()

def schedule_content_pack(pack_id: int, scheduled_at_iso: str):
    """Schedules a post for delayed automatic publication"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE content_packs SET status = 'scheduled', scheduled_at = ? WHERE id = ?",
        (scheduled_at_iso, pack_id)
    )
    conn.commit()
    conn.close()

def cancel_scheduled_pack(pack_id: int):
    """Cancels a scheduled post and returns it to pending drafts"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE content_packs SET status = 'pending', scheduled_at = NULL WHERE id = ?",
        (pack_id,)
    )
    conn.commit()
    conn.close()

def get_due_scheduled_packs() -> list[dict]:
    """Finds all scheduled posts whose time has arrived"""
    now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM content_packs WHERE status = 'scheduled' AND scheduled_at <= ? ORDER BY scheduled_at ASC",
        (now_iso,)
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_upcoming_scheduled_packs(limit: int = 10) -> list[dict]:
    """Returns list of future scheduled posts"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM content_packs WHERE status = 'scheduled' ORDER BY scheduled_at ASC LIMIT ?",
        (limit,)
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

MONTH_NAMES_RU = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
WEEKDAY_NAMES_RU = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]

def format_human_slot(dt: datetime) -> str:
    weekday = WEEKDAY_NAMES_RU[dt.weekday()]
    month = MONTH_NAMES_RU[dt.month - 1]
    return f"{weekday}, {dt.day} {month} в {dt.strftime('%H:%M')}"

def get_next_auto_pilot_slot(exclude_booked: set = None) -> tuple[str, str]:
    """
    Computes the next optimal marketing slot for B2B audience in Kazakhstan.
    Slots: Tuesday 11:00, Thursday 11:00, Saturday 11:00 Almaty time (UTC+5).
    Finds the earliest slot that is NOT already booked.
    Returns: (iso_str, human_str)
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT scheduled_at FROM content_packs WHERE status = 'scheduled' AND scheduled_at > datetime('now', 'localtime')")
    booked = {r[0][:16] for r in cursor.fetchall()}
    conn.close()

    if exclude_booked:
        booked.update(exclude_booked)

    now = datetime.now()
    # Look up to 30 days ahead
    for day_offset in range(30):
        candidate = (now + timedelta(days=day_offset)).replace(hour=11, minute=0, second=0, microsecond=0)
        # B2B prime slots: Tuesday (1), Thursday (3), Saturday (5)
        if candidate.weekday() in (1, 3, 5) and candidate > (now + timedelta(minutes=45)):
            slot_key = candidate.strftime("%Y-%m-%d %H:%M")
            if slot_key not in booked:
                iso_str = candidate.strftime("%Y-%m-%d %H:%M:%S")
                human_str = format_human_slot(candidate)
                return iso_str, human_str

    # Fallback: tomorrow at 11:00
    fallback = (now + timedelta(days=1)).replace(hour=11, minute=0, second=0, microsecond=0)
    return fallback.strftime("%Y-%m-%d %H:%M:%S"), format_human_slot(fallback)

def assign_pack_to_autopilot(pack_id: int) -> tuple[bool, str, str]:
    """
    Assigns a post to the earliest available free autopilot slot.
    Returns: (success, iso_str, human_str)
    """
    iso_str, human_str = get_next_auto_pilot_slot()
    schedule_content_pack(pack_id, iso_str)
    return True, iso_str, human_str

def batch_fill_autopilot(limit: int = 3) -> list[dict]:
    """
    Takes up to `limit` pending drafts and fills consecutive free autopilot slots.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, title FROM content_packs WHERE status = 'pending' ORDER BY id DESC LIMIT ?", (limit,))
    drafts = cursor.fetchall()
    conn.close()

    if not drafts:
        return []

    assigned = []
    booked_temp = set()
    for row in drafts:
        p_id = row["id"]
        title = row["title"]
        iso_str, human_str = get_next_auto_pilot_slot(exclude_booked=booked_temp)
        schedule_content_pack(p_id, iso_str)
        booked_temp.add(iso_str[:16])
        assigned.append({
            "id": p_id,
            "title": title,
            "slot_human": human_str,
            "slot_iso": iso_str
        })
    return assigned

def get_autopilot_overview() -> dict:
    """
    Returns full status of the autopilot grid:
    scheduled posts, next free slots, pending drafts count.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, title, scheduled_at, status 
        FROM content_packs 
        WHERE status = 'scheduled' AND scheduled_at > datetime('now', 'localtime')
        ORDER BY scheduled_at ASC
    """)
    scheduled = [dict(r) for r in cursor.fetchall()]

    cursor.execute("SELECT COUNT(*) FROM content_packs WHERE status = 'pending'")
    pending_count = cursor.fetchone()[0]
    conn.close()

    # Find next 3 free slots
    booked_keys = {p["scheduled_at"][:16] for p in scheduled}
    free_slots = []
    now = datetime.now()
    for day_offset in range(30):
        if len(free_slots) >= 3:
            break
        cand = (now + timedelta(days=day_offset)).replace(hour=11, minute=0, second=0, microsecond=0)
        if cand.weekday() in (1, 3, 5) and cand > (now + timedelta(minutes=45)):
            k = cand.strftime("%Y-%m-%d %H:%M")
            if k not in booked_keys:
                free_slots.append({
                    "iso": cand.strftime("%Y-%m-%d %H:%M:%S"),
                    "human": format_human_slot(cand)
                })

    return {
        "scheduled_packs": scheduled,
        "free_slots": free_slots,
        "pending_count": pending_count
    }

def get_content_packs_paginated(page: int = 1, page_size: int = 5, status_filter: str = "all") -> tuple[list[dict], int]:
    conn = get_connection()
    cursor = conn.cursor()
    
    where_clause = "WHERE status != 'archived'"
    params = []
    if status_filter and status_filter != "all":
        where_clause += " AND status = ?"
        params.append(status_filter)

    cursor.execute(f"SELECT COUNT(*) FROM content_packs {where_clause}", tuple(params))
    total = cursor.fetchone()[0]
    
    offset = max(0, (page - 1) * page_size)
    query_params = list(params) + [page_size, offset]
    cursor.execute(
        f"""
        SELECT id, title, source_url, status, scheduled_at, published_at, views_count, created_at,
               substr(tg_post, 1, 150) as preview
        FROM content_packs
        {where_clause}
        ORDER BY id DESC
        LIMIT ? OFFSET ?
        """,
        tuple(query_params)
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows], total

def get_content_packs_counts() -> dict:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT status, COUNT(*) as cnt 
        FROM content_packs 
        WHERE status != 'archived'
        GROUP BY status
    """)
    rows = cursor.fetchall()
    counts = {"all": 0, "pending": 0, "published": 0, "scheduled": 0}
    for row in rows:
        st = row["status"]
        c = row["cnt"]
        if st in counts:
            counts[st] = c
        counts["all"] += c
    conn.close()
    return counts

def search_content_packs(query: str, limit: int = 10) -> list[dict]:
    conn = get_connection()
    cursor = conn.cursor()
    search_term = f"%{query}%"
    cursor.execute(
        """
        SELECT id, title, source_url, status, scheduled_at, published_at, views_count, created_at,
               substr(tg_post, 1, 150) as preview
        FROM content_packs
        WHERE status != 'archived' AND (title LIKE ? OR tg_post LIKE ?)
        ORDER BY id DESC
        LIMIT ?
        """,
        (search_term, search_term, limit)
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def update_content_pack_post(pack_id: int, field: str, new_text: str):
    if field not in ("tg_post", "ig_post", "threads_post"):
        return
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(f"UPDATE content_packs SET {field} = ? WHERE id = ?", (new_text, pack_id))
    conn.commit()
    conn.close()

def delete_content_pack(pack_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM content_packs WHERE id = ?", (pack_id,))
    conn.commit()
    conn.close()

# Regulatory Deadlines Kazakhstan
def get_upcoming_deadlines(days_ahead: int = 90) -> list[dict]:
    """Returns sorted list of upcoming deadlines within days_ahead"""
    today_str = datetime.now().strftime("%Y-%m-%d")
    max_date = (datetime.now() + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT *,
               CAST((julianday(deadline_date) - julianday(?)) AS INTEGER) as days_left
        FROM regulatory_deadlines
        WHERE is_active = 1 AND deadline_date >= ? AND deadline_date <= ?
        ORDER BY deadline_date ASC
        """,
        (today_str, today_str, max_date)
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_deadline(deadline_id: int) -> dict:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM regulatory_deadlines WHERE id = ?", (deadline_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def mark_deadline_notified(deadline_id: int, days_left: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT last_notified_days FROM regulatory_deadlines WHERE id = ?", (deadline_id,))
    row = cursor.fetchone()
    existing = row[0] or "" if row else ""
    notified_set = set(filter(None, existing.split(",")))
    notified_set.add(str(days_left))
    new_val = ",".join(sorted(notified_set))
    cursor.execute("UPDATE regulatory_deadlines SET last_notified_days = ? WHERE id = ?", (new_val, deadline_id))
    conn.commit()
    conn.close()

def get_due_deadline_alerts() -> list[dict]:
    """
    Finds deadlines that are exactly 14, 7, or 2 days away,
    and have not yet been notified for that interval.
    """
    deadlines = get_upcoming_deadlines(days_ahead=20)
    alerts = []
    for d in deadlines:
        days = d.get("days_left", 999)
        if days in (14, 7, 2):
            notified = set(filter(None, (d.get("last_notified_days") or "").split(",")))
            if str(days) not in notified:
                alerts.append(d)
    return alerts

# Analytics & Post Metrics
def sync_channel_metrics(views_map: dict[str, int]):
    """
    Updates views count in content_packs based on data scraped from t.me/s/tanboxkz.
    views_map: e.g. {'tanboxkz/8': 6, 'tanboxkz/9': 7} or {8: 6, 9: 7}
    """
    now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_connection()
    cursor = conn.cursor()
    for key, views in views_map.items():
        try:
            msg_id = int(str(key).split("/")[-1])
            cursor.execute(
                """
                UPDATE content_packs
                SET views_count = ?, last_analyzed_at = ?
                WHERE channel_message_id = ?
                """,
                (views, now_iso, msg_id)
            )
        except Exception:
            continue
    conn.commit()
    conn.close()

def get_channel_analytics() -> dict:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM content_packs WHERE status = 'published'")
    total_published = cursor.fetchone()[0]

    cursor.execute("SELECT SUM(views_count), AVG(views_count) FROM content_packs WHERE status = 'published' AND views_count > 0")
    row = cursor.fetchone()
    total_views = row[0] or 0
    avg_views = round(row[1] or 0, 1)

    cursor.execute(
        """
        SELECT id, title, channel_message_id, views_count, published_at
        FROM content_packs
        WHERE status = 'published'
        ORDER BY views_count DESC, id DESC
        LIMIT 5
        """
    )
    top_posts = [dict(r) for r in cursor.fetchall()]

    cursor.execute(
        """
        SELECT COUNT(*) FROM content_packs WHERE status = 'scheduled'
        """
    )
    scheduled_count = cursor.fetchone()[0]

    conn.close()
    return {
        "total_published": total_published,
        "total_views": total_views,
        "avg_views": avg_views,
        "top_posts": top_posts,
        "scheduled_count": scheduled_count
    }

# CSV & Excel Export
def export_content_plan_csv() -> str:
    """
    Exports all posts into data/content_plan_tanbox.csv with UTF-8 BOM
    (opens directly in MS Excel without garbled Russian text).
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    file_path = os.path.join(DATA_DIR, "content_plan_tanbox.csv")
    
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, created_at, status, scheduled_at, published_at, title, source_url,
               views_count, tg_post, ig_post, threads_post
        FROM content_packs
        ORDER BY id DESC
        """
    )
    rows = cursor.fetchall()
    conn.close()

    # UTF-8 with BOM ('utf-8-sig') ensures native Excel compatibility on Windows
    with open(file_path, mode="w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow([
            "ID", "Дата создания", "Статус", "Запланировано на", "Дата публикации",
            "Заголовок / Тема", "Источник", "Просмотры в канале",
            "Telegram пост", "Threads тред"
        ])
        for r in rows:
            status_rus = {
                "pending": "Черновик",
                "scheduled": "Запланирован",
                "published": "Опубликован",
                "archived": "В архиве"
            }.get(r["status"], r["status"])

            writer.writerow([
                r["id"],
                r["created_at"],
                status_rus,
                r["scheduled_at"] or "—",
                r["published_at"] or "—",
                r["title"],
                r["source_url"] or "—",
                r["views_count"] or 0,
                r["tg_post"] or "",
                r["threads_post"] or ""
            ])

    return file_path

# Admin management
def is_admin(user_id: int) -> bool:
    if ADMIN_IDS and user_id in ADMIN_IDS:
        return True
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM admins WHERE user_id = ? AND is_active = 1", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row is not None

def register_admin(user_id: int, username: str, first_name: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO admins (user_id, username, first_name, is_active) VALUES (?, ?, ?, 1)",
        (user_id, username or "", first_name or "")
    )
    conn.commit()
    conn.close()

def get_all_admins() -> list[int]:
    admins = set(ADMIN_IDS)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM admins WHERE is_active = 1")
    for r in cursor.fetchall():
        admins.add(r[0])
    conn.close()
    return list(admins)

def get_stats():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM sources")
    sources_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM processed_news")
    news_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM processed_news WHERE is_relevant = 1")
    relevant_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM content_packs")
    packs_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM content_packs WHERE status = 'scheduled'")
    scheduled_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM content_packs WHERE status = 'published'")
    published_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM regulatory_deadlines WHERE is_active = 1")
    deadlines_count = cursor.fetchone()[0]
    conn.close()
    return {
        "sources": sources_count,
        "scanned_news": news_count,
        "relevant_news": relevant_count,
        "content_packs": packs_count,
        "scheduled_packs": scheduled_count,
        "published_packs": published_count,
        "deadlines": deadlines_count
    }

# Settings & Scanner State
def get_setting(key: str, default: str = "") -> str:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else default

def set_setting(key: str, value: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()

def is_scanner_enabled() -> bool:
    val = get_setting("scanner_enabled", "1")
    return val == "1"

def set_scanner_enabled(enabled: bool):
    set_setting("scanner_enabled", "1" if enabled else "0")

def toggle_scanner() -> bool:
    curr = is_scanner_enabled()
    set_scanner_enabled(not curr)
    return not curr
