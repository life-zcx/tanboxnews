import logging
import html
import re
from aiogram import Bot, types
from aiogram.exceptions import TelegramBadRequest

import config
import database as db
import threads_service
from google_sheets_service import sync_to_google_sheet
from keyboards import get_test_preview_keyboard

logger = logging.getLogger(__name__)

def format_telegram_html(t: str) -> str:
    """
    Cleans up raw AI output for Telegram:
    - Strips markdown horizontal rules ('---', '***')
    - Converts [text](url) -> <a href="url">text</a> so links NEVER duplicate in text
    - Converts **bold** -> <b>bold</b>
    - Cleans up trailing whitespace and headers
    """
    if not t:
        return ""
    # Strip markdown separator rules like "---"
    t = re.sub(r'(?m)^\s*[-*_]{3,}\s*$', '', t)
    # Strip stray section headers like "### 📌 1. TELEGRAM"
    t = re.sub(r'(?m)^\s*#{1,4}\s*.*TELEGRAM.*$', '', t, flags=re.IGNORECASE)
    # Convert markdown links [Text](url) -> <a href="url">Text</a>
    # If Text == url or [tanbox.kz](https://tanbox.kz), this displays as a single clickable link "tanbox.kz"!
    t = re.sub(r'\[([^\]]+)\]\((https?://[^\)]+)\)', r'<a href="\2">\1</a>', t)
    # Convert **bold** -> <b>bold</b>
    t = re.sub(r'\*\*([^*]+)\*\*', r'<b>\1</b>', t)
    # Convert __bold__ -> <b>bold</b>
    t = re.sub(r'__([^_]+)__', r'<b>\1</b>', t)
    # Clean up excess linebreaks
    t = re.sub(r'\n{3,}', '\n\n', t).strip()
    return t

async def send_test_preview(
    bot: Bot,
    user_id: int,
    pack_id: int,
    return_page: int = 1,
    return_filter: str = "all"
) -> dict:
    """
    Sends the exact formatted Telegram post directly to admin's private chat
    with action buttons to publish or return.
    """
    pack = db.get_content_pack(pack_id)
    if not pack:
        return {"status": "error", "message": f"Пост #{pack_id} не найден."}

    tg_text = format_telegram_html(pack.get("tg_post") or "")
    if not tg_text:
        return {"status": "error", "message": "Текст Telegram-поста пуст."}

    header = (
        f"👁 <b>[ПРЕДПРОСМОТР В ЛС — Пост #{pack_id}]</b>\n"
        "<i>Ниже показано, как именно пост будет выглядеть в канале @tanboxkz:</i>\n"
        "────────────────────────"
    )
    
    await bot.send_message(user_id, header, parse_mode="HTML")

    preview_keyboard = get_test_preview_keyboard(pack_id, return_page, return_filter)
    
    try:
        sent = await bot.send_message(
            user_id,
            tg_text,
            parse_mode="HTML",
            reply_markup=preview_keyboard,
            disable_web_page_preview=False
        )
        return {"status": "success", "message_id": sent.message_id}
    except Exception as e:
        logger.warning(f"HTML test preview failed: {e}, falling back to plain text...")
        sent = await bot.send_message(
            user_id,
            tg_text,
            parse_mode=None,
            reply_markup=preview_keyboard
        )
        return {"status": "success", "message_id": sent.message_id}

async def publish_to_telegram(bot: Bot, pack_id: int) -> dict:
    """Publishes post to official Telegram channel @tanboxkz"""
    pack = db.get_content_pack(pack_id)
    if not pack or not pack.get("tg_post"):
        return {"status": "error", "platform": "telegram", "message": "Пост или текст не найден."}

    channel_target = config.AUTO_POST_CHANNEL_ID or "@tanboxkz"
    post_text = format_telegram_html(pack["tg_post"])

    try:
        sent = await bot.send_message(
            chat_id=channel_target,
            text=post_text,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.warning(f"HTML send failed to {channel_target}: {e}, trying plain text...")
        try:
            sent = await bot.send_message(
                chat_id=channel_target,
                text=post_text,
                parse_mode=None
            )
        except Exception as e2:
            logger.error(f"Failed to post to {channel_target}: {e2}")
            return {"status": "error", "platform": "telegram", "message": str(e2)}

    db.update_content_pack_status(pack_id, "published", channel_message_id=sent.message_id)
    post_link = f"https://t.me/tanboxkz/{sent.message_id}"
    logger.info(f"Published post #{pack_id} to Telegram channel {channel_target}: {post_link}")

    saved_pack = db.get_content_pack(pack_id)
    await sync_to_google_sheet(saved_pack)

    return {
        "status": "success",
        "platform": "telegram",
        "message_id": sent.message_id,
        "url": post_link
    }

async def publish_to_threads(pack_id: int) -> dict:
    """Publishes post as a linked thread to Meta Threads @tanbox.kz"""
    pack = db.get_content_pack(pack_id)
    if not pack or not pack.get("threads_post"):
        return {"status": "error", "platform": "threads", "message": "Текст для Threads не найден."}

    if not threads_service.is_configured():
        return {"status": "error", "platform": "threads", "message": "Threads API не настроен в .env"}

    try:
        res = await threads_service.publish_full_thread(pack["threads_post"])
        if res.get("status") == "success":
            root_id = res["root_post_id"]
            db.update_content_pack_threads_published(pack_id, root_id)
            post_url = res["url"]
            
            saved_pack = db.get_content_pack(pack_id)
            await sync_to_google_sheet(saved_pack)
            
            return {
                "status": "success",
                "platform": "threads",
                "root_post_id": root_id,
                "url": post_url,
                "post_count": res.get("post_count", 1)
            }
        else:
            return {
                "status": "error",
                "platform": "threads",
                "message": res.get("message", "Неизвестная ошибка Threads API")
            }
    except Exception as e:
        logger.error(f"Threads publishing error for #{pack_id}: {e}", exc_info=True)
        return {"status": "error", "platform": "threads", "message": str(e)}

async def publish_everywhere(bot: Bot, pack_id: int) -> dict:
    """
    1-Click Multi-Publish to both Telegram channel and Meta Threads.
    Returns a unified report.
    """
    tg_result = await publish_to_telegram(bot, pack_id)
    th_result = await publish_to_threads(pack_id)

    tg_ok = (tg_result.get("status") == "success")
    th_ok = (th_result.get("status") == "success")

    if tg_ok and th_ok:
        overall = "success"
    elif tg_ok or th_ok:
        overall = "partial"
    else:
        overall = "error"

    return {
        "overall_status": overall,
        "telegram": tg_result,
        "threads": th_result
    }

