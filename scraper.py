import re
import aiohttp
import asyncio
import requests
import feedparser
from bs4 import BeautifulSoup
from urllib.parse import urljoin

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}

def clean_title(title: str) -> str:
    """Cleans up raw parsed title by removing badge prefixes and trailing timestamps"""
    if not title:
        return ""
    # Remove leading category badges
    t = re.sub(r"^(Маркировка|Новости|Пресс-релиз|Статья|Важно|Внимание|Официально)\s*[:—\-]?\s*", "", title, flags=re.IGNORECASE)
    # Remove trailing relative dates like '3 дня назад, 15:26' or '22.09.2026, 12:00'
    t = re.sub(r"\s*\d{1,2}\s+(дня|дней|часа|часов|минут|минуты|недели|месяца)\s+назад.*$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*\d{2}\.\d{2}\.\d{4}(?:,\s*\d{2}:\d{2})?.*$", "", t)
    return t.strip()

def fetch_html(url: str, timeout: int = 12) -> str:
    """Synchronous fetch with encoding fallback"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        resp.encoding = resp.apparent_encoding or "utf-8"
        if resp.status_code == 200:
            return resp.text
    except Exception as e:
        print(f"[Scraper] Error fetching {url}: {e}")
    return ""

async def async_fetch_html(url: str, timeout: int = 15) -> str:
    """Asynchronous non-blocking fetch with aiohttp"""
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    try:
        async with aiohttp.ClientSession(headers=HEADERS, timeout=client_timeout) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    text = await resp.text(encoding="utf-8", errors="replace")
                    return text
    except Exception as e:
        print(f"[Scraper Async] Error fetching {url}: {e}")
    return ""

def parse_uchet_news(url: str = "https://uchet.kz/news/") -> list[dict]:
    """Parse latest news items from uchet.kz"""
    html = fetch_html(url)
    if not html:
        return []
    
    soup = BeautifulSoup(html, "html.parser")
    items = []
    seen_urls = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("/news/") and href != "/news/" and not href.startswith("/news/search"):
            full_url = urljoin("https://uchet.kz", href)
            title = clean_title(a.get_text(separator=" ", strip=True))
            if len(title) > 25 and full_url not in seen_urls:
                seen_urls.add(full_url)
                items.append({
                    "title": title,
                    "url": full_url,
                    "summary": ""
                })
    return items

def parse_uchet_questions(url: str) -> list[dict]:
    """Parse questions and answers from uchet.kz consultation sections (ESF, SNT, KGD)"""
    html = fetch_html(url)
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    items = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/question/" in href and any(char.isdigit() for char in href):
            full_url = urljoin("https://uchet.kz", href)
            title = clean_title(a.get_text(separator=" ", strip=True))
            if len(title) > 25 and full_url not in seen:
                seen.add(full_url)
                items.append({
                    "title": title,
                    "url": full_url,
                    "summary": ""
                })
    return items

def parse_rss_feed(url: str) -> list[dict]:
    """Parse standard RSS/Atom feed"""
    items = []
    try:
        feed = feedparser.parse(url)
        for entry in feed.entries:
            title = clean_title(entry.get("title", ""))
            link = entry.get("link", "")
            summary = entry.get("summary", "")
            if title and link:
                items.append({
                    "title": title,
                    "url": link,
                    "summary": summary
                })
    except Exception as e:
        print(f"[Scraper] Error parsing RSS {url}: {e}")
    return items

def parse_generic_page(url: str) -> list[dict]:
    """Extract article links from a generic news page (e.g. tanba.telecom.kz)"""
    html = fetch_html(url)
    if not html:
        return []
    
    soup = BeautifulSoup(html, "html.parser")
    items = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        raw_title = a.get_text(separator=" ", strip=True)
        title = clean_title(raw_title)
        
        is_news_link = any(kw in href.lower() for kw in ("news", "article", "post", "press", "markirovka"))
        if len(title) > 25 and is_news_link:
            full_url = urljoin(url, href)
            if full_url not in seen and full_url.rstrip("/") != url.rstrip("/"):
                seen.add(full_url)
                items.append({
                    "title": title,
                    "url": full_url,
                    "summary": ""
                })
    return items

def parse_telegram_channel(channel_url: str) -> list[dict]:
    """
    Parses public preview of a Telegram channel (https://t.me/s/{channel}).
    Extracts text, post link, and generates a punchy title from the first sentence.
    """
    clean_url = channel_url.strip()
    if not clean_url.startswith("http"):
        clean_url = f"https://t.me/s/{clean_url.lstrip('@')}"
    elif "/s/" not in clean_url:
        clean_url = clean_url.replace("t.me/", "t.me/s/")

    html = fetch_html(clean_url)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    items = []
    messages = soup.find_all("div", class_="tgme_widget_message_wrap")

    for msg in messages:
        text_el = msg.find("div", class_="tgme_widget_message_text")
        if not text_el:
            continue
        
        full_text = text_el.get_text(separator="\n", strip=True)
        if len(full_text) < 40:
            continue

        date_a = msg.find("a", class_="tgme_widget_message_date")
        post_link = date_a["href"] if date_a and date_a.has_attr("href") else ""
        if not post_link:
            continue

        # Extract title from the first sentence or first 100 characters
        first_line = full_text.split("\n")[0].strip()
        first_sentence = re.split(r"[.!?]\s+", first_line)[0].strip()
        title = first_sentence if len(first_sentence) > 20 else first_line[:90]
        title = clean_title(title)

        items.append({
            "title": title,
            "url": post_link,
            "summary": full_text
        })

    # Return newest first
    return list(reversed(items))

def parse_legalacts_npa(url: str = "https://legalacts.egov.kz/list") -> list[dict]:
    """
    Parses Open NPA portal (legalacts.egov.kz) for draft laws, orders and resolutions.
    """
    html = fetch_html(url)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    items = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/npa/view" in href or "/view?id=" in href:
            full_url = urljoin("https://legalacts.egov.kz", href)
            raw_title = a.get_text(separator=" ", strip=True)
            # Skip non-title link anchors like 'Подробнее' / 'Толығырақ'
            if raw_title.lower() in ("подробнее", "толығырақ", "читать далее"):
                continue
            title = clean_title(raw_title)
            if len(title) > 25 and full_url not in seen:
                seen.add(full_url)
                items.append({
                    "title": title,
                    "url": full_url,
                    "summary": f"Проект нормативного правового акта на открытом обсуждении (портал legalacts.egov.kz): {title}"
                })
    return items

def fetch_tanbox_channel_metrics(channel: str = "tanboxkz") -> dict[int, int]:
    """
    Fetches real-time views from the public preview of @tanboxkz (https://t.me/s/tanboxkz).
    Returns mapping: {message_id: views_count}
    """
    url = f"https://t.me/s/{channel.lstrip('@')}"
    html = fetch_html(url)
    if not html:
        return {}

    soup = BeautifulSoup(html, "html.parser")
    result = {}

    for m in soup.find_all("div", class_="tgme_widget_message"):
        data_post = m.get("data-post", "")
        if "/" not in data_post:
            continue
        try:
            msg_id = int(data_post.split("/")[-1])
        except ValueError:
            continue

        views_el = m.find(class_="tgme_widget_message_views")
        if not views_el:
            continue

        views_str = views_el.get_text(strip=True).upper()
        # Parse '1.2K' -> 1200 or '450' -> 450
        views = 0
        try:
            if "K" in views_str:
                views = int(float(views_str.replace("K", "").strip()) * 1000)
            elif "M" in views_str:
                views = int(float(views_str.replace("M", "").strip()) * 1000000)
            else:
                views = int(views_str)
        except Exception:
            views = 0

        result[msg_id] = views

    return result

def extract_article_content(url: str) -> dict:
    """Fetch article and extract full text for AI analysis"""
    # If URL is a Telegram post link (e.g. https://t.me/prg_buh/1234)
    if "t.me/" in url:
        t_url = url
        if "/s/" not in t_url:
            t_url = t_url.replace("t.me/", "t.me/s/")
        html = fetch_html(t_url)
        if html:
            soup = BeautifulSoup(html, "html.parser")
            text_el = soup.find("div", class_="tgme_widget_message_text")
            if text_el:
                content = text_el.get_text(separator="\n\n", strip=True)
                title = content.split("\n")[0][:90].strip()
                return {"title": title, "text": content, "url": url}

    html = fetch_html(url)
    if not html:
        return {"title": "", "text": "", "url": url}
    
    soup = BeautifulSoup(html, "html.parser")

    for el in soup(["script", "style", "nav", "footer", "header", "aside", "noscript", "svg", "button"]):
        el.decompose()

    h1 = soup.find("h1")
    title = clean_title(h1.get_text(strip=True)) if h1 else ""

    container = (
        soup.find("div", class_="news-detail") or
        soup.find("div", class_="news-detail__content") or
        soup.find("div", class_="article-body") or
        soup.find("div", class_="article__body") or
        soup.find("div", class_="article-content") or
        soup.find("div", class_="article__content") or
        soup.find("div", class_="detail-text") or
        soup.find("article") or
        soup.find("div", class_="content") or
        soup.find("main") or
        soup.body
    )

    paragraphs = []
    if container:
        for p in container.find_all(["p", "ul", "ol", "h2", "h3"]):
            if p.find_parent("nav") or p.find_parent("footer"):
                continue
            t = p.get_text(separator=" ", strip=True)
            if len(t) > 25:
                paragraphs.append(t)
    
    full_text = "\n\n".join(paragraphs)
    if len(full_text) > 5000:
        full_text = full_text[:5000] + "..."

    return {
        "title": title,
        "text": full_text,
        "url": url
    }
