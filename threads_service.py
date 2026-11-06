import re
import os
import asyncio
import logging
import aiohttp
import config

logger = logging.getLogger(__name__)

GRAPH_API_BASE = "https://graph.threads.net/v1.0"
THREADS_BASE_URL = "https://graph.threads.net"

def is_configured() -> bool:
    """Checks whether Threads API credentials are set"""
    return bool(config.THREADS_ACCESS_TOKEN and config.THREADS_USER_ID)

def clean_threads_text(text: str) -> str:
    """Strips markdown headers, rules, tags, and formats links cleanly for Threads without duplicates"""
    if not text:
        return ""
    
    # Strip section headers (e.g. "### 📌 1. TELEGRAM", "### 🧵 2. THREADS", etc.)
    cleaned = re.sub(r'(?m)^\s*#{1,6}\s*.*(?:TELEGRAM|THREADS).*$', '', text, flags=re.IGNORECASE)
    cleaned = re.sub(r'(?m)^\s*#{1,6}\s*', '', cleaned)

    # Strip markdown horizontal rules ('---', '***')
    cleaned = re.sub(r'(?m)^\s*[-*_]{3,}\s*$', '', cleaned)

    # Clean markdown links [Name](url) -> if Name is already the domain/url, show single link, no duplicates!
    def _link_repl(m):
        name = m.group(1).strip()
        url = m.group(2).strip()
        c_url = re.sub(r'^https?://(www\.)?', '', url).rstrip('/')
        c_name = re.sub(r'^https?://(www\.)?', '', name).rstrip('/')
        if c_name.lower() == c_url.lower():
            return url
        return f"{name}: {url}"

    cleaned = re.sub(r'\[([^\]]+)\]\((https?://[^\)]+)\)', _link_repl, cleaned)
    
    # Strip markdown bold/italic
    cleaned = re.sub(r'\*\*([^*]+)\*\*', r'\1', cleaned)
    cleaned = re.sub(r'\*([^*]+)\*', r'\1', cleaned)
    cleaned = re.sub(r'__([^_]+)__', r'\1', cleaned)
    cleaned = re.sub(r'_([^_]+)_', r'\1', cleaned)
    cleaned = re.sub(r'`([^`]+)`', r'\1', cleaned)
    
    # Strip basic HTML tags
    cleaned = re.sub(r'<[^>]+>', '', cleaned)

    # Clean up excess newlines
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    
    return cleaned.strip()

def split_into_thread_parts(raw_text: str) -> list[str]:
    """
    Intelligently parses generated Threads content into separate tweet parts (<= 500 chars).
    Handles patterns like:
      **Пост 1:** ...
      **Пост 2:** ...
    Never creates tiny or fragmented 1-sentence tweets.
    """
    cleaned = clean_threads_text(raw_text)
    if not cleaned:
        return []
    
    # Try splitting by "Пост 1:", "Пост 2:", "1/3", etc.
    pattern = r'(?:\bПост\s*\d+\s*[:\.]?|\bЧасть\s*\d+\s*[:\.]?|\bТвит\s*\d+\s*[:\.]?|\b\d+/\d+\b)'
    parts = re.split(pattern, cleaned, flags=re.IGNORECASE)
    
    # Filter out empty or tiny chunks
    chunks = [p.strip() for p in parts if p.strip() and len(p.strip()) > 20]
    
    # If regex didn't find multiple distinct posts (e.g. standard paragraphs without 'Пост 1:')
    if len(chunks) <= 1:
        # Group paragraphs together so each tweet is a full, coherent post (300-480 chars)
        raw_paras = [p.strip() for p in re.split(r'\n+', cleaned) if p.strip()]
        grouped = []
        cur = ""
        for p in raw_paras:
            if not cur:
                cur = p
            elif len(cur) + len(p) + 2 <= 480:
                cur += "\n\n" + p
            else:
                grouped.append(cur)
                cur = p
        if cur:
            grouped.append(cur)
        chunks = grouped

    # Ensure no single part exceeds 500 characters and filter out stubs
    final_tweets = []
    for chunk in chunks:
        chunk = chunk.strip()
        if len(chunk) < 25:
            continue
        if len(chunk) <= 500:
            final_tweets.append(chunk)
        else:
            sentences = re.split(r'(?<=[.!?])\s+', chunk)
            current_tweet = ""
            for sentence in sentences:
                if len(current_tweet) + len(sentence) + 1 <= 500:
                    current_tweet = f"{current_tweet} {sentence}".strip()
                else:
                    if current_tweet and len(current_tweet) >= 25:
                        final_tweets.append(current_tweet)
                    current_tweet = sentence[:500]
            if current_tweet and len(current_tweet) >= 25:
                final_tweets.append(current_tweet)

    return final_tweets if final_tweets else [cleaned[:500]]

async def get_profile_info() -> dict:
    """Fetches Threads account profile details and publishing rate limits"""
    token = config.THREADS_ACCESS_TOKEN
    user_id = config.THREADS_USER_ID
    if not token or not user_id:
        return {"status": "error", "message": "Threads credentials not configured"}

    async with aiohttp.ClientSession() as session:
        # Profile info
        url_profile = f"{GRAPH_API_BASE}/me?fields=id,username,name,threads_biography,threads_profile_picture_url&access_token={token}"
        async with session.get(url_profile) as resp:
            if resp.status != 200:
                err_text = await resp.text()
                return {"status": "error", "message": f"Profile error: {err_text}"}
            profile_data = await resp.json()

        # Rate limits
        url_limit = f"{GRAPH_API_BASE}/{user_id}/threads_publishing_limit?access_token={token}"
        quota_usage = 0
        async with session.get(url_limit) as resp:
            if resp.status == 200:
                limit_data = await resp.json()
                quota_usage = limit_data.get("data", [{}])[0].get("quota_usage", 0)

        profile_data["status"] = "success"
        profile_data["quota_usage"] = quota_usage
        return profile_data

async def create_media_container(session: aiohttp.ClientSession, text: str, reply_to_id: str = None) -> str:
    """Step 1: Creates a media container for text or reply"""
    token = config.THREADS_ACCESS_TOKEN
    user_id = config.THREADS_USER_ID
    url = f"{GRAPH_API_BASE}/{user_id}/threads"
    
    payload = {
        "media_type": "TEXT",
        "text": text,
        "access_token": token
    }
    if reply_to_id:
        payload["reply_to_id"] = reply_to_id

    async with session.post(url, data=payload) as resp:
        res_data = await resp.json()
        if resp.status != 200 or "id" not in res_data:
            err_msg = res_data.get("error", {}).get("message", str(res_data))
            raise RuntimeError(f"Threads container creation failed: {err_msg}")
        return res_data["id"]

async def wait_for_container_ready(session: aiohttp.ClientSession, container_id: str, max_attempts: int = 10) -> bool:
    """Checks container status until FINISHED or timeout"""
    token = config.THREADS_ACCESS_TOKEN
    url = f"{GRAPH_API_BASE}/{container_id}?fields=status,error_message&access_token={token}"
    
    for _ in range(max_attempts):
        await asyncio.sleep(2)
        async with session.get(url) as resp:
            if resp.status == 200:
                data = await resp.json()
                st = data.get("status")
                if st == "FINISHED":
                    return True
                elif st == "ERROR":
                    err_msg = data.get("error_message", "Unknown container error")
                    raise RuntimeError(f"Threads container processing error: {err_msg}")
            # If status endpoint isn't supported or returns 200 without status, wait a moment and proceed
    return True

async def publish_container(session: aiohttp.ClientSession, container_id: str) -> str:
    """Step 2: Publishes the created media container"""
    token = config.THREADS_ACCESS_TOKEN
    user_id = config.THREADS_USER_ID
    url = f"{GRAPH_API_BASE}/{user_id}/threads_publish"
    
    payload = {
        "creation_id": container_id,
        "access_token": token
    }
    
    # Try publishing with retries if Meta is still processing container
    for attempt in range(5):
        async with session.post(url, data=payload) as resp:
            res_data = await resp.json()
            if resp.status == 200 and "id" in res_data:
                return res_data["id"]
            
            err_obj = res_data.get("error", {})
            err_msg = err_obj.get("message", str(res_data))
            if "processing" in err_msg.lower() or "not ready" in err_msg.lower() or err_obj.get("code") == 9007:
                logger.info(f"Threads container {container_id} is still processing, retry in 3s (attempt {attempt+1})...")
                await asyncio.sleep(3)
                continue
            raise RuntimeError(f"Threads publishing failed: {err_msg}")
            
    raise RuntimeError(f"Threads publishing timed out for container {container_id}")

async def publish_full_thread(raw_text: str) -> dict:
    """
    Publishes a complete thread (1 or more linked tweets) to Threads account.
    Returns:
      {
        "status": "success",
        "root_post_id": "...",
        "post_ids": [...],
        "post_count": 3,
        "url": "https://www.threads.net/@tanbox.kz/post/..."
      }
    """
    if not is_configured():
        return {"status": "error", "message": "Threads API credentials not configured"}

    tweets = split_into_thread_parts(raw_text)
    if not tweets:
        return {"status": "error", "message": "No text to publish"}

    username = config.THREADS_USERNAME or "tanbox.kz"
    published_ids = []
    
    async with aiohttp.ClientSession() as session:
        prev_post_id = None
        for idx, tweet_text in enumerate(tweets):
            logger.info(f"Publishing Threads tweet {idx+1}/{len(tweets)}: {tweet_text[:60]}...")
            
            # Step 1: Create container
            container_id = await create_media_container(
                session,
                text=tweet_text,
                reply_to_id=prev_post_id
            )
            
            # Wait a few seconds for Meta processing
            await wait_for_container_ready(session, container_id)
            
            # Step 2: Publish container
            pub_id = await publish_container(session, container_id)
            published_ids.append(pub_id)
            prev_post_id = pub_id
            
            # Brief pause between sequential replies in a thread
            if idx < len(tweets) - 1:
                await asyncio.sleep(2)

    root_id = published_ids[0]
    post_url = f"https://www.threads.net/@{username}/post/{root_id}"
    logger.info(f"Successfully published thread of {len(published_ids)} posts! Root URL: {post_url}")
    
    return {
        "status": "success",
        "root_post_id": root_id,
        "post_ids": published_ids,
        "post_count": len(published_ids),
        "url": post_url
    }

async def refresh_threads_token() -> dict:
    """
    Refreshes the 60-day Meta Threads access token and updates .env file.
    """
    token = config.THREADS_ACCESS_TOKEN
    if not token:
        return {"status": "error", "message": "No access token configured"}

    url = f"{THREADS_BASE_URL}/refresh_access_token?grant_type=th_refresh_token&access_token={token}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            if resp.status == 200 and "access_token" in data:
                new_token = data["access_token"]
                config.THREADS_ACCESS_TOKEN = new_token
                
                # Update .env
                env_path = os.path.join(config.BASE_DIR, ".env")
                if os.path.exists(env_path):
                    with open(env_path, "r", encoding="utf-8") as f:
                        lines = f.readlines()
                    with open(env_path, "w", encoding="utf-8") as f:
                        for line in lines:
                            if line.startswith("THREADS_ACCESS_TOKEN="):
                                f.write(f"THREADS_ACCESS_TOKEN={new_token}\n")
                            else:
                                f.write(line)
                logger.info(f"Threads token successfully refreshed (expires in {data.get('expires_in')}s)")
                return {"status": "success", "expires_in": data.get("expires_in")}
            else:
                return {"status": "error", "message": data.get("error", {}).get("message", str(data))}

