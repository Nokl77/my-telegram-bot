import asyncio
import logging
import os
import base64
from dataclasses import dataclass
from typing import Callable, Set, List, Tuple

import aiohttp
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import AsyncOpenAI

# =========================
# Настройки
# =========================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

CHECK_INTERVAL = 60 * 5
TOTAL_PER_CYCLE = 5

if not BOT_TOKEN or not TARGET_CHAT_ID:
    raise RuntimeError("BOT_TOKEN или TARGET_CHAT_ID не заданы")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY не задан")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# =========================
# Источники
# =========================

@dataclass(slots=True)
class NewsSource:
    name: str
    url: str
    parser: Callable[[BeautifulSoup], List[Tuple[str, str]]]

def parse_generic(soup, selector, base=""):
    result = []
    for a in soup.select(selector):
        title = a.get_text(strip=True)
        link = a.get("href")
        if title and link:
            if link.startswith("/") and base:
                link = base + link
            result.append((title, link))
    return result

SOURCES = [
    NewsSource("Destructoid", "https://www.destructoid.com/news/",
               lambda s: parse_generic(s, "a.title", "https://www.destructoid.com")),
    NewsSource("PC Gamer", "https://www.pcgamer.com/news/",
               lambda s: parse_generic(s, "a.article-link")),
    NewsSource("Rock Paper Shotgun", "https://www.rockpapershotgun.com/news/",
               lambda s: parse_generic(s, "a.c-block-link__overlay")),
    NewsSource("NVIDIA Blog", "https://blogs.nvidia.com/",
               lambda s: parse_generic(s, "a.blog-card__link")),
]

sent_links: Set[str] = set()

# =========================
# Telegram
# =========================

async def send_photo_with_caption(session, image_bytes, caption):
    data = aiohttp.FormData()
    data.add_field("chat_id", TARGET_CHAT_ID)
    data.add_field("photo", image_bytes,
                   filename="digest.png",
                   content_type="image/png")
    data.add_field("caption", caption)
    data.add_field("parse_mode", "Markdown")

    async with session.post(f"{TELEGRAM_API}/sendPhoto", data=data) as r:
        await r.text()

# =========================
# OpenAI helper
# =========================

async def ask_gpt(messages, temperature=0.6):
    response = await openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        temperature=temperature,
    )
    return response.choices[0].message.content.strip()

# =========================
# Генерация дайджеста
# =========================

async def generate_digest(news_items):
    formatted = "\n".join(
        f"- [{src}] {title} ({link})"
        for src, title, link in news_items
    )

    chatgpt_prompt = (
        "Составь дайджест новостей игрового и IT-миров обязательно на русском языке. "
        "Для каждой новости отдельный абзац. Названия игр и компаний только на английском. "
        "Без нумерации, без субъективных оценок.\n\n"
        f"{formatted}"
    )

    messages = [
        {"role": "system", "content": "Ты профессиональный редактор гейм- и IT-дайджестов."},
        {"role": "user", "content": chatgpt_prompt}
    ]

    return await ask_gpt(messages)

# =========================
# Генерация изображения
# =========================

async def generate_image(digest_text):
    response = await openai_client.images.generate(
        model="gpt-image-1",
        prompt=f"Illustration based on this news digest:\n{digest_text[:800]}",
        size="1024x1024",
    )

    image_base64 = response.data[0].b64_json
    return base64.b64decode(image_base64)

# =========================
# Парсинг
# =========================

async def fetch_html(session, url):
    async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as r:
        return await r.text()

async def parse_sources(session):
    collected = []

    for source in SOURCES:
        try:
            html = await fetch_html(session, source.url)
            soup = BeautifulSoup(html, "html.parser")
            articles = source.parser(soup)

            for title, link in articles:
                if link not in sent_links:
                    collected.append((source.name, title, link))
        except Exception as e:
            logger.error(f"{source.name} error: {e}")

    return collected[:TOTAL_PER_CYCLE]

# =========================
# Очереди и синхронизация
# =========================

news_queue: asyncio.Queue = asyncio.Queue()
publish_queue: asyncio.Queue = asyncio.Queue()
cycle_lock = asyncio.Lock()

# =========================
# Workers
# =========================

async def parser_worker():
    while True:
        async with cycle_lock:
            logger.info("Парсинг источников...")

            async with aiohttp.ClientSession() as session:
                news = await parse_sources(session)

            if news:
                await news_queue.put(news)

        await asyncio.sleep(CHECK_INTERVAL)

async def generator_worker():
    while True:
        news_items = await news_queue.get()

        try:
            logger.info("Генерация дайджеста...")
            digest = await generate_digest(news_items)

            logger.info("Генерация изображения...")
            image = await generate_image(digest)

            await publish_queue.put((digest, image, news_items))

        except Exception as e:
            logger.error(f"Generation error: {e}")

        news_queue.task_done()

async def publisher_worker():
    while True:
        digest, image, news_items = await publish_queue.get()

        try:
            logger.info("Отправка поста в Telegram...")
            async with aiohttp.ClientSession() as session:
                await send_photo_with_caption(session, image, digest)

            for _, _, link in news_items:
                sent_links.add(link)

        except Exception as e:
            logger.error(f"Publish error: {e}")

        publish_queue.task_done()

# =========================
# Запуск
# =========================

async def main():
    await asyncio.gather(
        parser_worker(),
        generator_worker(),
        publisher_worker()
    )

if __name__ == "__main__":
    asyncio.run(main())

