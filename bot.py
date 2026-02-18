import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Callable, Set, List, Tuple

import aiohttp
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram.ext import Application, ApplicationBuilder

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

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
TARGET_CHAT_ID: str = os.getenv("TARGET_CHAT_ID", "")
OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
OPENAI_ORG: str = os.getenv("OPENAI_ORG", "")

CHECK_INTERVAL_SECONDS: int = 120
TOTAL_PER_CYCLE = 8

# =========================
# OpenAI клиент
# =========================

openai_client = AsyncOpenAI(
    api_key=OPENAI_API_KEY,
    organization=OPENAI_ORG if OPENAI_ORG else None,
)

# =========================
# Модель источника
# =========================

@dataclass(slots=True)
class NewsSource:
    name: str
    url: str
    parser: Callable[[BeautifulSoup], List[Tuple[str, str]]]

# =========================
# Парсеры
# =========================

def parse_destructoid(soup: BeautifulSoup):
    result = []
    for a in soup.select("a.title"):
        title = a.get_text(strip=True)
        link = a.get("href")
        if title and link:
            if link.startswith("/"):
                link = "https://www.destructoid.com" + link
            result.append((title, link))
    return result


def parse_pcgamer(soup: BeautifulSoup):
    result = []
    for a in soup.select("a.article-link"):
        title = a.get_text(strip=True)
        link = a.get("href")
        if title and link:
            result.append((title, link))
    return result


def parse_rps(soup: BeautifulSoup):
    result = []
    for a in soup.select("a.c-block-link__overlay"):
        title = a.get_text(strip=True)
        link = a.get("href")
        if title and link:
            result.append((title, link))
    return result


def parse_nvidia_blog(soup: BeautifulSoup):
    result = []
    for a in soup.select("a.blog-card__link"):
        title = a.get_text(strip=True)
        link = a.get("href")
        if title and link:
            result.append((title, link))
    return result

# =========================
# Источники
# =========================

SOURCES = [
    NewsSource("Destructoid", "https://www.destructoid.com/news/", parse_destructoid),
    NewsSource("PC Gamer", "https://www.pcgamer.com/news/", parse_pcgamer),
    NewsSource("Rock Paper Shotgun", "https://www.rockpapershotgun.com/news/", parse_rps),
    NewsSource("NVIDIA Blog", "https://blogs.nvidia.com/", parse_nvidia_blog),
]

# =========================
# Память
# =========================

sent_links: Set[str] = set()

# =========================
# HTTP
# =========================

async def fetch_html(session: aiohttp.ClientSession, url: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; NewsDigestBot/1.0)"}
    timeout = aiohttp.ClientTimeout(total=30)
    async with session.get(url, headers=headers, timeout=timeout) as response:
        response.raise_for_status()
        return await response.text()

# =========================
# ChatGPT генерация дайджеста
# =========================

async def generate_digest(news_items: List[Tuple[str, str, str]]) -> str:
    """
    news_items: (source_name, title, link)
    """

    formatted_news = "\n".join(
        f"- [{source}] {title} ({link})"
        for source, title, link in news_items
    )

    prompt = f"""
Ты — редактор игрового новостного канала.

Сделай единый краткий, структурированный дайджест новостей.
Пиши живо, но профессионально.
Не выдумывай факты.
Сохрани ссылки.

Новости:
{formatted_news}
"""

    response = await openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Ты профессиональный игровой редактор."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.7,
    )

    return response.choices[0].message.content.strip()

# =========================
# Проверка новостей
# =========================

async def check_news(application: Application) -> None:
    logger.info("Проверка новостей...")

    collected = []

    async with aiohttp.ClientSession() as session:
        for source in SOURCES:
            try:
                html = await fetch_html(session, source.url)
                soup = BeautifulSoup(html, "html.parser")
                articles = source.parser(soup)

                for title, link in articles:
                    if link in sent_links:
                        continue

                    collected.append((source.name, title, link))

            except Exception as e:
                logger.error(f"{source.name}: {e}")

    if not collected:
        return

    selected = collected[:TOTAL_PER_CYCLE]

    try:
        digest_text = await generate_digest(selected)

        await application.bot.send_message(
            chat_id=TARGET_CHAT_ID,
            text=digest_text,
            disable_web_page_preview=True,
        )

        for _, _, link in selected:
            sent_links.add(link)

    except Exception as e:
        logger.error(f"Digest generation error: {e}")

# =========================
# Фоновый цикл
# =========================

async def news_loop(application: Application) -> None:
    while True:
        try:
            await check_news(application)
        except Exception as e:
            logger.error(f"Loop error: {e}")

        await asyncio.sleep(CHECK_INTERVAL_SECONDS)

# =========================
# Запуск
# =========================

def main() -> None:
    if not BOT_TOKEN or not TARGET_CHAT_ID:
        raise RuntimeError("BOT_TOKEN или TARGET_CHAT_ID не заданы")

    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY не задан")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    application = ApplicationBuilder().token(BOT_TOKEN).build()

    async def startup(app: Application) -> None:
        asyncio.create_task(news_loop(app))

    application.post_init = startup

    logger.info("Бот запущен.")
    application.run_polling()

if __name__ == "__main__":
    main()
