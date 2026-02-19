import asyncio
import logging
import os
import base64
from io import BytesIO
from dataclasses import dataclass
from typing import Callable, Set, List, Tuple, Dict
import random

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

CHECK_INTERVAL = 120
TOTAL_PER_CYCLE = 8

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
# Telegram отправка
# =========================

async def send_message(session, text):
    async with session.post(
        f"{TELEGRAM_API}/sendMessage",
        json={"chat_id": TARGET_CHAT_ID, "text": text}
    ) as r:
        await r.text()

async def send_photo(session, image_bytes):
    data = aiohttp.FormData()
    data.add_field("chat_id", TARGET_CHAT_ID)
    data.add_field("photo", image_bytes,
                   filename="digest.png",
                   content_type="image/png")

    async with session.post(f"{TELEGRAM_API}/sendPhoto", data=data) as r:
        await r.text()

# =========================
# OpenAI
# =========================

async def generate_digest(news_items):

    formatted = "\n".join(
        f"- [{src}] {title} ({link})"
        for src, title, link in news_items
    )

    chatgpt_prompt = (
        "Составь дайджест новостей игрового и IT-миров за прошедшее время обязательно на русском языке. "
        "Для каждой новости напиши отдельный абзац. Названия игр и компаний приводи только на английском (оригинал), не переводя их, всё остальное по-русски. "
        "Не используй нумерацию, не навязывай субъективные оценки, не растекайся мыслями, не переводи названия игр и компаний. "
        "Название каждой игры или компании выделяй жирным шрифтом.\n\n"
        f"{formatted}"
    )

    response = await openai_client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[{
            "role": "system", "content": "Ты профессиональный редактор гейм- и IT-дайджестов. Не переводишь названия игр и компаний."
        }, {
            "role": "user", "content": chatgpt_prompt
        }],
        temperature=0.6,
    )

    return response.choices[0].message.content.strip()

async def generate_image(digest_text):

    img_prompt = get_image_prompt(digest_text)

    response = await openai_client.images.generate(
        model="gpt-image-1",
        prompt=img_prompt,
        size="1024x1024",
    )

    image_base64 = response.data[0].b64_json
    return base64.b64decode(image_base64)

# =========================
# Генерация промта для изображения
# =========================

def get_image_prompt(news: Dict) -> str:
    sys_prompt = (
        "You are creating prompts for image generation AIs in English. "
        "Base your prompt on the news article below. Come up with a short scene description or illustration idea (1-2 sentences), "
        "do not use names of real game characters: only general descriptions (for example, 'a young knight in green costume'). "
        "Keep only English names of games and companies. Do not include names, brands, logos, interfaces or text elements."
    )
    user_prompt = (
        f"News:\n{news['title']}\n{news['full_text'][:600]}\n"
    )
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_prompt}
    ]
    first_prompt = await ask_gpt(messages)
    if not first_prompt:
        return "Prompt could not be generated."
    first_prompt = first_prompt.strip()
    final_prompt = (
        "The character from image.png (a pixel art character with short brown hair, black square glasses, "
        "a white T-shirt with a black spiral symbol, red and orange checkered suspenders, blue jeans with a brown belt, and brown shoes) "
        f"is present in the scene. {first_prompt} The character is visibly interacting with the main elements of the scene or other characters; "
        "make their interaction clear and meaningful (for example, talking, working together, or sharing an activity)."
    )
    return final_prompt.strip()

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
                        logger.error(f"{source.name} error: {e}")

                if collected:
                    selected = collected[:TOTAL_PER_CYCLE]

                    digest = await generate_digest(selected)
                    image = await generate_image(digest)

                    await send_photo(session, image)
                    await send_message(session, digest)

                    for _, _, link in selected:
                        sent_links.add(link)

        except Exception as e:
            logger.error(f"Main loop error: {e}")

        await asyncio.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())
