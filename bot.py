import asyncio
import logging
import os
import base64
from dataclasses import dataclass
from typing import Callable, Set, List, Tuple
import aiohttp
from bs4 import BeautifulSoup
from openai import AsyncOpenAI

print("=== PROCESS STARTED ===", flush=True)

# =========================
# Настройки
# =========================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

logger.info("Logger initialized")

BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

logger.info(f"BOT_TOKEN present: {bool(BOT_TOKEN)}")
logger.info(f"TARGET_CHAT_ID present: {bool(TARGET_CHAT_ID)}")
logger.info(f"OPENAI_API_KEY present: {bool(OPENAI_API_KEY)}")

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

async def send_photo_with_caption(session, image_bytes, caption_text):
    data = aiohttp.FormData()
    data.add_field("chat_id", TARGET_CHAT_ID)
    data.add_field("photo", image_bytes,
                   filename="digest.png",
                   content_type="image/png")
    data.add_field("caption", caption_text)
    data.add_field("parse_mode", "Markdown")

    async with session.post(f"{TELEGRAM_API}/sendPhoto", data=data) as r:
        await r.text()

# =========================
# OpenAI helper
# =========================

async def ask_gpt(messages, temperature=0.6):
    response = await openai_client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=messages,
        temperature=temperature,
    )
    return response.choices[0].message.content.strip()

# =========================
# Семантический фильтр дублей
# =========================

async def filter_semantic_duplicates(news_items):

    if len(news_items) <= 1:
        return news_items

    formatted = "\n".join(
        f"{i+1}. [{src}] {title} ({link})"
        for i, (src, title, link) in enumerate(news_items)
    )

    system_prompt = (
        "Ты редактор новостей. Удали новости, которые повторяют друг друга по смыслу. "
        "Если две новости описывают одно и то же событие, оставь только одну — самую информативную. "
        "Верни номера новостей, которые нужно оставить, через запятую. Только числа."
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": formatted}
    ]

    response = await ask_gpt(messages, temperature=0)

    try:
        keep_indexes = [
            int(x.strip()) - 1
            for x in response.split(",")
            if x.strip().isdigit()
        ]
        filtered = [news_items[i] for i in keep_indexes if 0 <= i < len(news_items)]
        return filtered if filtered else news_items
    except:
        return news_items

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
        "Не используй нумерацию. Без субъективных оценок. "
        "Название каждой игры или компании выделяй жирным.\n\n"
        f"{formatted}"
    )

    messages = [
        {"role": "system", "content": "Ты профессиональный редактор гейм- и IT-дайджестов."},
        {"role": "user", "content": chatgpt_prompt}
    ]

    return await ask_gpt(messages)

# =========================
# Генерация промта изображения
# =========================

async def get_image_prompt(digest_text: str) -> str:

    sys_prompt = (
        "You create prompts for image generation AIs in English. "
        "Based on the news below, describe a short illustration idea (1-2 sentences). "
        "Do not use names of real characters. No brands, logos, text elements."
    )

    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": digest_text[:600]}
    ]

    base_prompt = await ask_gpt(messages)

    final_prompt = (
        "The pixel art character with short brown hair, black square glasses, "
        "white T-shirt with a spiral symbol, red-orange suspenders, blue jeans, "
        "brown belt and shoes is in the scene. "
        f"{base_prompt} The character actively interacts with the environment."
    )

    return final_prompt.strip()

# =========================
# Генерация изображения
# =========================

async def generate_image(digest_text):

    img_prompt = await get_image_prompt(digest_text)

    response = await openai_client.images.generate(
        model="gpt-image-1",
        prompt=img_prompt,
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

# =========================
# Главный цикл
# =========================

async def main():
    logger.info("=== BOT STARTED ===")
    logger.info("Entering main loop")

    while True:
        logger.info("New cycle started")

            async with aiohttp.ClientSession() as session:

                for source in SOURCES:
                    try:
                        html = await fetch_html(session, source.url)
                        soup = BeautifulSoup(html, "html.parser")
                        articles = source.parser(soup)

                        for title, link in articles:
                            if link not in sent_links:
                                collected.append((source.name, title, link))

                        if not collected:
                            logger.info("No new articles found this cycle")
    
                    except Exception as e:
                        logger.error(f"{source.name} error: {e}")

                if collected:
                    selected = collected[:TOTAL_PER_CYCLE]
                    selected = await filter_semantic_duplicates(selected)

                    if selected:
                        digest = await generate_digest(selected)
                        image = await generate_image(digest)

                        await send_photo_with_caption(session, image, digest)

                        for _, _, link in selected:
                            sent_links.add(link)

        except Exception as e:
            logger.error(f"Main loop error: {e}")

        await asyncio.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())

