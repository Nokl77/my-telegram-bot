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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("news-bot")

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

CHECK_INTERVAL = 60 * 2
TOTAL_PER_CYCLE = 24

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
    try:
        data = aiohttp.FormData()
        data.add_field("chat_id", TARGET_CHAT_ID)
        data.add_field("photo", image_bytes,
                       filename="digest.png",
                       content_type="image/png")
        data.add_field("caption", caption)
        data.add_field("parse_mode", "Markdown")

        async with session.post(
            f"{TELEGRAM_API}/sendPhoto",
            data=data,
            timeout=aiohttp.ClientTimeout(total=30)
        ) as r:
            text = await r.text()
            if r.status != 200:
                logger.error(f"Telegram API error {r.status}: {text}")
            else:
                logger.info("Post successfully sent to Telegram")

    except Exception:
        logger.exception("Failed to send message to Telegram")

# =========================
# OpenAI helper
# =========================

async def ask_gpt(messages, temperature=0.6):
    try:
        response = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=temperature,
        )
        return response.choices[0].message.content.strip()
    except Exception:
        logger.exception("OpenAI request failed")
        raise

# =========================
# Генерация
# =========================

async def generate_digest(news_items):
    formatted = "\n".join(
        f"- [{src}] {title} ({link})"
        for src, title, link in news_items
    )

    messages = [
    {
        "role": "system",
        "content": (
            "You are a professional editor who writes concise, factual news digests about gaming and IT. "
            "The entire output must be in Russian (no English sentences), except that names of games and companies must remain in English. "
            "Write a digest based only on the provided list of articles.\n\n"
            "Formatting rules:\n"
            "- Output multiple news items.\n"
            "- Each news item must be at least 250 characters.\n"
            "- Separate news items with ONE blank line.\n"
            "- Do NOT use numbering or bullet lists.\n"
            "- Do NOT include subjective opinions.\n"
            "- Each news item MUST start with a relevant sticker (use a single emoji) that matches the content (e.g., 🎮 for games, 🧠 for AI, 🖥️ for hardware, 🔒 for security, 🚀 for launches, 💾 for software, 🕹️ for game updates, etc.).\n"
            "- Keep the sticker as the very first character of the news item.\n"
        )
    },
    {"role": "user", "content": formatted}
]

    return await ask_gpt(messages)

async def generate_image(digest_text):
    try:
        response = await openai_client.images.generate(
            model="gpt-image-1",
            prompt=digest_text[:800],
            size="1024x1024",
        )
        image_base64 = response.data[0].b64_json
        return base64.b64decode(image_base64)
    except Exception:
        logger.exception("Image generation failed")
        raise

# =========================
# Парсинг
# =========================

async def fetch_html(session, url):
    try:
        async with session.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=aiohttp.ClientTimeout(total=30)
        ) as r:
            return await r.text()
    except Exception:
        logger.exception(f"Failed to fetch {url}")
        raise

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

        except Exception:
            logger.exception(f"Error while parsing {source.name}")

    return collected[:TOTAL_PER_CYCLE]

# =========================
# Очереди
# =========================

news_queue: asyncio.Queue = asyncio.Queue()
publish_queue: asyncio.Queue = asyncio.Queue()

# =========================
# Workers
# =========================

async def parser_worker():
    logger.info("Parser worker started")
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                news = await parse_sources(session)
                if news:
                    await news_queue.put(news)
                    logger.info(f"Collected {len(news)} new articles")

        except asyncio.CancelledError:
            logger.warning("Parser worker cancelled")
            raise
        except Exception:
            logger.exception("Unexpected error in parser_worker")

        await asyncio.sleep(CHECK_INTERVAL)

async def generator_worker():
    logger.info("Generator worker started")
    while True:
        try:
            news_items = await news_queue.get()

            digest = await generate_digest(news_items)
            image = await generate_image(digest)

            await publish_queue.put((digest, image, news_items))
            news_queue.task_done()

        except asyncio.CancelledError:
            logger.warning("Generator worker cancelled")
            raise
        except Exception:
            logger.exception("Unexpected error in generator_worker")

async def publisher_worker():
    logger.info("Publisher worker started")
    while True:
        try:
            digest, image, news_items = await publish_queue.get()

            async with aiohttp.ClientSession() as session:
                await send_photo_with_caption(session, image, digest)

            for _, _, link in news_items:
                sent_links.add(link)

            publish_queue.task_done()

        except asyncio.CancelledError:
            logger.warning("Publisher worker cancelled")
            raise
        except Exception:
            logger.exception("Unexpected error in publisher_worker")

# =========================
# Heartbeat
# =========================

async def heartbeat():
    while True:
        logger.info("Bot is alive")
        await asyncio.sleep(300)

# =========================
# Main
# =========================

async def main():
    loop = asyncio.get_running_loop()

    def handle_async_exception(loop, context):
        logger.error(f"Unhandled asyncio exception: {context}", exc_info=True)

    loop.set_exception_handler(handle_async_exception)

    tasks = [
        asyncio.create_task(parser_worker()),
        asyncio.create_task(generator_worker()),
        asyncio.create_task(publisher_worker()),
        asyncio.create_task(heartbeat())
    ]

    try:
        await asyncio.gather(*tasks)
    except Exception:
        logger.exception("Fatal error in main()")
    finally:
        for task in tasks:
            task.cancel()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        logger.exception("Bot crashed at top level")

