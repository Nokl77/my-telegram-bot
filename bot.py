import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Callable, Awaitable, Set

import aiohttp
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import Bot
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ContextTypes,
)

# =========================
# Настройка логирования
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

# =========================
# Модель источника
# =========================

@dataclass(slots=True)
class NewsSource:
    name: str
    url: str
    parser: Callable[[BeautifulSoup], list[tuple[str, str]]]


# =========================
# Парсеры сайтов
# =========================

def parse_destructoid(soup: BeautifulSoup) -> list[tuple[str, str]]:
    articles: list[tuple[str, str]] = []
    for a in soup.select("a.title"):
        title = a.get_text(strip=True)
        link = a.get("href")
        if title and link:
            if link.startswith("/"):
                link = "https://www.destructoid.com" + link
            articles.append((title, link))
    return articles


def parse_pcgamer(soup: BeautifulSoup) -> list[tuple[str, str]]:
    articles: list[tuple[str, str]] = []
    for a in soup.select("a.article-link"):
        title = a.get_text(strip=True)
        link = a.get("href")
        if title and link:
            articles.append((title, link))
    return articles


def parse_rps(soup: BeautifulSoup) -> list[tuple[str, str]]:
    articles: list[tuple[str, str]] = []
    for a in soup.select("a.c-block-link__overlay"):
        title = a.get_text(strip=True)
        link = a.get("href")
        if title and link:
            articles.append((title, link))
    return articles


def parse_nvidia_blog(soup: BeautifulSoup) -> list[tuple[str, str]]:
    articles: list[tuple[str, str]] = []
    for a in soup.select("a.blog-card__link"):
        title = a.get_text(strip=True)
        link = a.get("href")
        if title and link:
            articles.append((title, link))
    return articles


# =========================
# Источники
# =========================

SOURCES: list[NewsSource] = [
    NewsSource(
        name="Destructoid",
        url="https://www.destructoid.com/news/",
        parser=parse_destructoid,
    ),
    NewsSource(
        name="PC Gamer",
        url="https://www.pcgamer.com/news/",
        parser=parse_pcgamer,
    ),
    NewsSource(
        name="Rock Paper Shotgun",
        url="https://www.rockpapershotgun.com/news/",
        parser=parse_rps,
    ),
    NewsSource(
        name="NVIDIA Blog",
        url="https://blogs.nvidia.com/",
        parser=parse_nvidia_blog,
    ),
]

# =========================
# Глобальное хранилище
# =========================

sent_links: Set[str] = set()


# =========================
# Загрузка страницы
# =========================

async def fetch_html(session: aiohttp.ClientSession, url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)"
    }
    async with session.get(url, headers=headers, timeout=30) as response:
        response.raise_for_status()
        return await response.text()


# =========================
# Проверка новостей
# =========================

async def check_news(application: Application) -> None:
    logger.info("Проверка новостей...")

    async with aiohttp.ClientSession() as session:
        for source in SOURCES:
            try:
                html = await fetch_html(session, source.url)
                soup = BeautifulSoup(html, "html.parser")
                articles = source.parser(soup)

                for title, link in articles[:5]:
                    if link not in sent_links:
                        message = (
                            f"<b>{source.name}</b>\n"
                            f"{title}\n"
                            f"{link}"
                        )
                        await application.bot.send_message(
                            chat_id=TARGET_CHAT_ID,
                            text=message,
                            parse_mode="HTML",
                            disable_web_page_preview=False,
                        )
                        sent_links.add(link)

            except Exception as e:
                logger.error(f"Ошибка при обработке {source.name}: {e}")


# =========================
# Job wrapper
# =========================

async def job_callback(context: ContextTypes.DEFAULT_TYPE) -> None:
    await check_news(context.application)


# =========================
# Запуск
# =========================

async def main() -> None:
    if not BOT_TOKEN or not TARGET_CHAT_ID:
        raise RuntimeError("BOT_TOKEN или TARGET_CHAT_ID не заданы")

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    application.job_queue.run_repeating(
        job_callback,
        interval=CHECK_INTERVAL_SECONDS,
        first=5,
    )

    logger.info("Бот запущен.")
    await application.run_polling()


if __name__ == "__main__":
    asyncio.run(main())
