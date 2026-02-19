import asyncio
import logging
import os
import base64
from io import BytesIO
from dataclasses import dataclass
from typing import Callable, Set, List, Tuple

import aiohttp
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import Bot
from telegram.ext import ApplicationBuilder

from openai import AsyncOpenAI

# =========================
# Логирование
# =========================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

# =========================
# Конфигурация
# =========================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

CHECK_INTERVAL = 120
TOTAL_PER_CYCLE = 8

if not BOT_TOKEN or not TARGET_CHAT_ID:
    raise RuntimeError("BOT_TOKEN или TARGET_CHAT_ID не заданы")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY не задан")

# =========================
# OpenAI клиент
# =========================

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# =========================
# Источники
# =========================

@dataclass(slots=True)
class NewsSource:
    name: str
    url: str
    parser: Callable[[BeautifulSoup], List[Tuple[str, str]]]

def parse_destructoid(soup):
    result = []
    for a in soup.select("a.title"):
        title = a.get_text(strip=True)
        link = a.get("href")
        if title and link:
            if link.startswith("/"):
                link = "https://www.destructoid.com" + link
            result.append((title, link))
    return result

def parse_pcgamer(soup):
    return [(a.get_text(strip=True), a.get("href"))
            for a in soup.select("a.article-link")
            if a.get_text(strip=True) and a.get("href")]

def parse_rps(soup):
    return [(a.get_text(strip=True), a.get("href"))
            for a in soup.select("a.c-block-link__overlay")
            if a.get_text(strip=True) and a.get("href")]

def parse_nvidia_blog(soup):
    return [(a.get_text(strip=True), a.get("href"))
            for a in soup.select("a.blog-card__link")
            if a.get_text(strip=True) and a.get("href")]

SOURCES = [
    NewsSource("Destructoid", "https://www.destructoid.com/news/", parse_destructoid),
    NewsSource("PC Gamer", "https://www.pcgamer.com/news/", parse_pcgamer),
    NewsSource("Rock Paper Shotgun", "https://www.rockpapershotgun.com/news/", parse_rps),
    NewsSource("NVIDIA Blog", "https://blogs.nvidia.com/", parse_nvidia_blog),
]

sent_links: Set[str] = set()

# =========================
# HTTP
# =========================

async def fetch_html(session, url):
    headers = {"User-Agent": "Mozilla/5.0"}
    async with session.get(url, headers=headers, timeout=30) as response:
        response.raise_for_status()
        return await response.text()

# =========================
# GPT: дайджест
# =========================

async def generate_digest(news_items):

    formatted_news = "\n".join(
        f"- [{source}] {title} ({link})"
        for source, title, link in news_items
    )

    prompt = f"""
Ты — редактор игрового новостного канала.
Сделай единый краткий структурированный дайджест.
Не выдумывай факты. Сохрани ссылки.

Новости:
{formatted_news}
"""

    response = await openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.6,
    )

    return response.choices[0].message.content.strip()

# =========================
# GPT: prompt для картинки
# =========================

async def generate_image_prompt(digest_text):

    prompt = (
        "You are creating prompts for image generation AIs in English. "
        "Base your prompt on the news article below. "
        "Come up with a short scene description (1-2 sentences). "
        "Do not use real character names. "
        "Do not include logos or text.\n\n"
        f"{digest_text}"
    )

    response = await openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
    )

    return response.choices[0].message.content.strip()

# =========================
# GPT: генерация изображения
# =========================

async def generate_image(scene_prompt):

    final_prompt = (
        "The pixel art character with short brown hair, black square glasses, "
        "white T-shirt with black spiral symbol, red suspenders, blue jeans and brown shoes "
        f"is in the scene. {scene_prompt} The character interacts with the scene."
    )

    result = await openai_client.images.generate(
        model="gpt-image-1",
        prompt=final_prompt,
        size="1024x1024",
    )

    image_base64 = result.data[0].b64_json
    image_bytes = base64.b64decode(image_base64)

    bio = BytesIO(image_bytes)
    bio.name = "digest.png"
    bio.seek(0)

    return bio

# =========================
# Основной цикл
# =========================

async def news_loop(bot: Bot):

    while True:
        try:
            collected = []

            async with aiohttp.ClientSession() as session:
                for source in SOURCES:
                    try:
                        html = await fetch_html(session, source.url)
                        soup = BeautifulSoup(html, "html.parser")
                        articles = source.parser(soup)

                        for title, link in articles:
                            if link not in sent_links:
                                collected.append((source.name, title, link))

                    except Exception as e:
                        logger.error(f"{source.name}: {e}")

            if collected:
                selected = collected[:TOTAL_PER_CYCLE]

                digest = await generate_digest(selected)
                scene_prompt = await generate_image_prompt(digest)
                image = await generate_image(scene_prompt)

                await bot.send_photo(chat_id=TARGET_CHAT_ID, photo=image)
                await bot.send_message(chat_id=TARGET_CHAT_ID, text=digest)

                for _, _, link in selected:
                    sent_links.add(link)

        except Exception as e:
            logger.error(f"Main loop error: {e}")

        await asyncio.sleep(CHECK_INTERVAL)

# =========================
# Запуск
# =========================

async def main():
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    bot = application.bot

    await application.initialize()
    await application.start()

    asyncio.create_task(news_loop(bot))

    logger.info("Бот запущен.")
    await application.updater.start_polling()

if __name__ == "__main__":
    asyncio.run(main())
