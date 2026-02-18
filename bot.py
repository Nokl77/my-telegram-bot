import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Callable, Set

import aiohttp
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram.ext import Application, ApplicationBuilder

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
CHECK_INTERVAL_SECONDS: int = 120

TOTAL_PER_CYCLE = 5
NVIDIA_QUOTA = max(1, TOTAL_PER_CYCLE // 5)  # 20%
OTHER_QUOTA = TOTAL_PER_CYCLE - NVIDIA_QUOTA

# =========================
# Модель источника
# =========================

@dataclass(slots=True)
class NewsSource:
    name: str
    url: str
    parser: Callable[[BeautifulSoup], list[tuple[str, str]]]
    is_nvidia: bool = False

# =========================
# Парсеры
# =========================

def parse_destructoid(soup: BeautifulSoup) -> list[tuple[str, str]]:
    result = []
    for a in soup.select("a.title"):
        title = a.get_text(strip=True)
        link = a.get("href")
        if title and link:
            if link.startswith("/"):
                link = "https://www.destructoid.com" + link
            result.append((title, link))
    return result


def parse_pcgamer(soup: BeautifulSoup) -> list[tuple[str, str]]:
    result = []
    for a in soup.select("a.article-link"):
        title = a.get_text(strip=True)
        link = a.get("href")
        if title and link:
            result.append((title, link))
    return result


def parse_rps(soup: BeautifulSoup) -> list[tuple[str, str]]:
    result = []
    for a in soup.select("a.c-block-link__overlay"):
        title = a.get_text(strip=True)
        link = a.get("href")
        if title and link:
            result.append((title, link))
    return result


def parse_nvidia_blog(soup: BeautifulSoup) -> list[tuple[str, str]]:
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
    NewsSource("NVIDIA Blog", "https://blogs.nvidia.com/", parse_nvidia_blog, is_nvidia=True),
]

# =========================
# Память
# =========================

sent_links: Set[str] = set()

# =========================
# HTTP
# =========================

async def fetch_html(session: aiohttp.ClientSession, url: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)"}
    timeout = aiohttp.ClientTimeout(total=30)
    async with session.get(url, headers=headers, timeout=timeout) as response:
        response.raise_for_status()
        return await response.text()

# =========================
# Проверка новостей
# =========================

async def check_news(application: Application) -> None:
    logger.info("Проверка новостей...")

    nvidia_articles = []
    other_articles = []

    async with aiohttp.ClientSession() as session:
        for source in SOURCES:
            try:
                html = await fetch_html(session, source.url)
                soup = BeautifulSoup(html, "html.parser")
                articles = source.parser(soup)

                for title, link in articles:
                    if link in sent_links:
                        continue

                    if source.is_nvidia:
                        nvidia_articles.append((source.name, title, link))
                    else:
                        other_articles.append((source.name, title, link))

            except Exception as e:
                logger.error(f"{source.name}: {e}")

    # Ограничиваем по квотам
    selected = (
        nvidia_articles[:NVIDIA_QUOTA] +
        other_articles[:OTHER_QUOTA]
    )

    for source_name, title, link in selected:
        text = (
            f"<b>{source_name}</b>\n"
            f"{title}\n"
            f"{link}"
        )

        try:
            await application.bot.send_message(
                chat_id=TARGET_CHAT_ID,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=False,
            )

            sent_links.add(link)
            await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"Send error: {e}")

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
