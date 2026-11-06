import json
import re
import aiohttp
import asyncio
from config import GEMINI_API_KEY, GEMINI_MODEL, SYSTEM_PROMPT

MODELS_CHAIN = [
    GEMINI_MODEL,
    "gemini-3.5-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-3.1-flash-lite",
    "gemini-3.8-flash",
    "gemini-3.5-flash"
]
# Remove duplicates while preserving order
MODELS_CHAIN = list(dict.fromkeys(MODELS_CHAIN))

async def call_gemini(prompt: str, system_instruction: str = None, temperature: float = 0.4) -> str:
    """Async call to Gemini REST API with automatic fallback to secondary models on 429/503 errors"""
    payload = {
        "contents": [
            {
                "parts": [{"text": prompt}]
            }
        ],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": 4096
        }
    }

    if system_instruction:
        payload["systemInstruction"] = {
            "parts": [{"text": system_instruction}]
        }

    for model_name in MODELS_CHAIN:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
        for attempt in range(2):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=75)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            candidates = data.get("candidates", [])
                            if candidates:
                                parts = candidates[0].get("content", {}).get("parts", [])
                                if parts:
                                    return parts[0].get("text", "").strip()
                        elif resp.status in (404, 429, 503, 500):
                            print(f"[Gemini] HTTP {resp.status} on model {model_name}, switching to next fallback...")
                            break  # Try next model in chain immediately
                        else:
                            err_text = await resp.text()
                            print(f"[Gemini] HTTP {resp.status} on {model_name} (attempt {attempt+1}): {err_text[:200]}")
            except Exception as e:
                print(f"[Gemini] Request failed on {model_name} (attempt {attempt+1}): {e}")
                await asyncio.sleep(1)
    return ""

async def evaluate_relevance(title: str, text: str) -> tuple[int, str]:
    """
    Evaluates news relevance to Kazakh digital marking (IS MPT / Tanba),
    tax regulations, 1C accounting, and KGD compliance on a 1-10 scale.
    """
    prompt = f"""
Ты — главный контент-стратег платформы Tanbox.kz по маркировке товаров (ИС МПТ / Tañba) и налогам в Казахстане.
Оцени релевантность новости для нашей целевой аудитории (ИП, ТОО, розница, импортеры, аптеки, обувной ритейл, бухгалтеры РК):

ЗАГОЛОВОК: {title}
ТЕКСТ: {text[:1800]}

Критерии оценки:
- 8–10: Прямо касается маркировки товаров (обувь, лекарства, табак, молочка, вода), интеграции 1С/касс, проверок КГД РК, штрафов в МРП, дедлайнов СНТ/ЭСФ.
- 5–7: Общие налоговые изменения в Казахстане, проверки МСБ, новые правила розничной торговли.
- 1–4: Макроэкономика, курсы валют, общие политические события, не связанные напрямую с операционной деятельностью и маркировкой.

Ответь строго в формате JSON:
{{
  "score": <число от 1 до 10>,
  "reason": "<краткое объяснение на 1 предложение>"
}}
"""
    response_text = await call_gemini(prompt, temperature=0.2)
    try:
        clean_json = response_text.replace("```json", "").replace("```", "").strip()
        data = json.loads(clean_json)
        return int(data.get("score", 0)), data.get("reason", "")
    except Exception:
        # Fallback keyword-based evaluation
        keywords = ["маркировк", "ис мпт", "таңба", "tanba", "datamatrix", "обув", "лекарств", "кгд", "снт", "эсф", "штраф", "1с"]
        text_lower = (title + " " + text).lower()
        matches = sum(1 for kw in keywords if kw in text_lower)
        score = min(10, matches * 2)
        return score, "Эвристическая оценка по ключевым словам РК"

def clean_telegram_post(t: str) -> str:
    """Cleans raw AI output for Telegram, prevents duplicate links and removes stray markdown lines"""
    if not t:
        return ""
    t = re.sub(r'(?m)^\s*[-*_]{3,}\s*$', '', t)
    t = re.sub(r'(?m)^\s*#{1,4}\s*.*TELEGRAM.*$', '', t, flags=re.IGNORECASE)
    t = re.sub(r'\[([^\]]+)\]\((https?://[^\)]+)\)', r'<a href="\2">\1</a>', t)
    t = re.sub(r'\*\*([^*]+)\*\*', r'<b>\1</b>', t)
    t = re.sub(r'__([^_]+)__', r'<b>\1</b>', t)
    t = re.sub(r'\n{3,}', '\n\n', t).strip()
    return t

def parse_generated_sections(full_text: str) -> dict:
    """Split generated full response into Telegram and Threads sections"""
    res = {
        "tg_post": "",
        "ig_post": "",
        "threads_post": "",
        "full_text": full_text
    }

    # Split using markdown section markers (supporting both 2-platform and legacy 3-platform formats)
    tg_match = re.search(r"###\s*📌?\s*1\.?\s*TELEGRAM\s*\n(.*?)(?=###\s*(?:📸|🧵)?\s*(?:2|3)\.|\Z)", full_text, re.DOTALL | re.IGNORECASE)
    threads_match = re.search(r"###\s*🧵?\s*(?:2|3)\.?\s*THREADS\s*\n(.*)", full_text, re.DOTALL | re.IGNORECASE)

    if tg_match:
        res["tg_post"] = tg_match.group(1).strip()
    if threads_match:
        res["threads_post"] = threads_match.group(1).strip()

    # Fallback if sections were not cleanly parsed
    if not res["tg_post"]:
        clean_full = re.sub(r'(?m)^\s*#{1,6}\s*.*TELEGRAM.*$', '', full_text, flags=re.IGNORECASE).strip()
        res["tg_post"] = clean_full[:1500]

    res["tg_post"] = clean_telegram_post(res["tg_post"])

    if not res["threads_post"]:
        # Generate a coherent, clean 2-post thread from tg_post instead of dumping raw markdown!
        clean_tg_plain = re.sub(r'<[^>]+>', '', res["tg_post"]).strip()
        paragraphs = [p.strip() for p in clean_tg_plain.split("\n\n") if p.strip() and len(p.strip()) > 30]
        if len(paragraphs) >= 2:
            hook = paragraphs[0][:400]
            if not hook.endswith(("?", "!", ".")):
                hook += "..."
            hook += "\n\nЧто думаете по этому поводу? 👇"
            
            cta = paragraphs[-1][:400]
            if "tanbox.kz" not in cta.lower():
                cta += "\n\nЭксперты tanbox.kz помогут закрыть вопрос «под ключ»."
            res["threads_post"] = f"**Пост 1:**\n{hook}\n\n**Пост 2:**\n{cta}"
        else:
            res["threads_post"] = clean_tg_plain[:450]

    res["threads_post"] = re.sub(r'(?m)^\s*[-*_]{3,}\s*$', '', res["threads_post"]).strip()
    return res

async def generate_posts_from_news(title: str, text: str, url: str) -> dict:
    """Generate complete content pack for Telegram and Threads based on input news"""
    prompt = f"""
На основе регуляторной новости Казахстана подготовь экспертный контент-пакет для 2 платформ (Telegram и Threads) строго по системному промпту:
НОВОСТЬ: {title}
ИСТОЧНИК: {url}
СОДЕРЖАНИЕ:
{text[:2500]}

Сформируй комплект для 2 площадок строго по шаблону (с разделителями):
---
### 📌 1. TELEGRAM
(Экспертный лонгрид: цепляющий заголовок + суть изменений простым языком + риски/штрафы в МРП + 3-4 конкретных шага + нативный CTA для Tanbox.kz)

---
### 🧵 2. THREADS
(Емкий тред из 2-3 твитов: парадоксальный хук + выжимка без воды + вопрос в конце для комментариев)
---
"""
    full_response = await call_gemini(prompt, system_instruction=SYSTEM_PROMPT, temperature=0.45)
    parsed = parse_generated_sections(full_response)
    return parsed

async def generate_evergreen_posts(topic: str = "") -> dict:
    """Generate high-impact content when there is no fresh regulatory news"""
    if not topic:
        topic = "Топ-5 критических ошибок интеграции 1С с ИС МПТ, из-за которых рознице и импортерам в Казахстане грозят блокировки касс и штрафы КГД"

    prompt = f"""
Сегодня нет горячих регуляторных законов. Напиши мощный экспертный B2B-материал на практическую боль бизнеса для Telegram и Threads:
ТЕМА: {topic}

Сформируй комплект строго по шаблону:
---
### 📌 1. TELEGRAM
(Экспертный разбор проблемы + риски + решение от Tanbox.kz)

---
### 🧵 2. THREADS
**Пост 1:** ...
**Пост 2:** ...
---
"""
    full_response = await call_gemini(prompt, system_instruction=SYSTEM_PROMPT, temperature=0.5)
    parsed = parse_generated_sections(full_response)
    return parsed

async def generate_deadline_warning_post(deadline: dict) -> dict:
    """Generate high-urgency regulatory warning post for Tanbox before a critical Kazakh deadline"""
    title = deadline.get("title", "")
    deadline_date = deadline.get("deadline_date", "")
    days_left = deadline.get("days_left", 7)
    target = deadline.get("target_business", "Малый и средний бизнес")
    fines = deadline.get("fines_info", "")
    desc = deadline.get("description", "")

    prompt = f"""
Срочный предупреждающий B2B-материал для предпринимателей Казахстана:
ДЕЙСТВИЕ: Приближается критический дедлайн регулятора!
НАЗВАНИЕ ДЕДЛАЙНА: {title}
КРАЙНИЙ СРОК: {deadline_date} (осталось всего {days_left} дней!)
ЦЕЛЕВАЯ АУДИТОРИЯ: {target}
ОПИСАНИЕ И СУТЬ: {desc}
ШТРАФЫ И РИСКИ: {fines}

Сформируй срочный комплект материалов для 2 площадок (Telegram и Threads):
1) Напомни о сроке ({days_left} дней);
2) Объясни реальные риски и суммы штрафов в тенге / МРП и блокировки счетов;
3) Дай пошаговый чек-лист из 3–4 шагов, как успеть все закрыть без паники;
4) Органично предложи Tanbox (tanbox.kz): автоматизация касс, 2D-сканеров, учет остатков и интеграция с 1С под ключ за 24 часа.

---
### 📌 1. TELEGRAM
...
---
### 🧵 2. THREADS
**Пост 1:** ...
**Пост 2:** ...
---
"""
    full_response = await call_gemini(prompt, system_instruction=SYSTEM_PROMPT, temperature=0.45)
    parsed = parse_generated_sections(full_response)
    return parsed
    full_response = await call_gemini(prompt, system_instruction=SYSTEM_PROMPT, temperature=0.45)
    parsed = parse_generated_sections(full_response)
    return parsed

async def rewrite_section(original_text: str, instruction: str) -> str:
    """Rewrite a specific post according to user feedback"""
    prompt = f"""
Ты — ведущий B2B-маркетер по маркировке ИС МПТ в Казахстане.
Перепиши следующий текст поста с учетом пожелания пользователя.
ВАЖНО: Объем текста должен быть строго в пределах 1000–1600 символов. Не делай текст длинным!

ИСХОДНЫЙ ТЕКСТ:
{original_text}

ПОЖЕЛАНИЕ ПОЛЬЗОВАТЕЛЯ:
{instruction}

Выдай только готовый переписанный текст без лишних предисловий и без дублирования исходника.
"""
    return await call_gemini(prompt, system_instruction=SYSTEM_PROMPT, temperature=0.4)
