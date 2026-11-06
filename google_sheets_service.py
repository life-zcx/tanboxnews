import logging
import aiohttp
import config

logger = logging.getLogger(__name__)

async def sync_to_google_sheet(pack: dict) -> bool:
    """
    Pushes post data to the Google Sheets webhook if configured in .env.
    Includes Telegram, Threads status, metrics, and links.
    """
    webhook_url = config.GOOGLE_SHEET_WEBHOOK_URL
    if not webhook_url:
        return False

    pack_id = pack.get("id")
    threads_id = pack.get("threads_post_id")
    threads_url = f"https://www.threads.net/@tanbox.kz/post/{threads_id}" if threads_id else ""
    channel_msg_id = pack.get("channel_message_id")
    tg_url = f"https://t.me/tanboxkz/{channel_msg_id}" if channel_msg_id else ""

    payload = {
        "id": pack_id,
        "created_at": pack.get("created_at"),
        "status": pack.get("status"),
        "scheduled_at": pack.get("scheduled_at"),
        "title": pack.get("title"),
        "source_url": pack.get("source_url"),
        "views_count": pack.get("views_count", 0),
        "tg_post_url": tg_url,
        "threads_post_id": threads_id,
        "threads_url": threads_url,
        "tg_post": pack.get("tg_post"),
        "threads_post": pack.get("threads_post")
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                webhook_url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                if resp.status == 200:
                    logger.info(f"Successfully synced post #{pack_id} to Google Sheets.")
                    return True
                else:
                    logger.warning(f"Google Sheets webhook returned HTTP {resp.status} for post #{pack_id}")
                    return False
    except Exception as e:
        logger.warning(f"Google Sheets sync failed for post #{pack_id}: {e}")
        return False

