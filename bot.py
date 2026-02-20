import asyncio
import logging
import aiohttp
from bs4 import BeautifulSoup
from openai import AsyncOpenAI
from aiogram import Bot
import traceback
import sys
import os
import signal

print("PYTHON PROCESS STARTED", flush=True)

# ================= CONFIG =================

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

CHECK_INTERVAL = 120

# ================= LOGGING =================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    stream=sys.stdout,
    force=True
)

logger = logging.getLogger(__name__)

# ================= VALIDATION =================

if not BOT_TOKEN:
    logger.critical("BOT_TOKEN not set")
    sys.exit(1)

if not CHANNEL_ID:
    logger.critical("CHANNEL_ID not set")
    sys.exit(1)

if not OPENAI_API_KEY:
    logger.critical("OPENAI_API_KEY not set")
    sys.exit(1)

# ================= SOURCES =================

SOURCES = {
    "Cointelegraph": {
        "url": "https://cointelegraph.com/",
        "selector": "a.post-card-inline__title-link"
    },
    "Decrypt": {
        "url": "https://decrypt.co/",
        "selector": "a.heading"
    }
}

# ================= SIGNAL HANDLING =================

def handle_shutdown(signum, frame):
    logger.error(f"Shutdown signal received: {signum}")
    sys.exit(1)

signal.signal(signal.SIGTERM, handle_shutdown)
signal.signal(signal.SIGINT, handle_shutdown)

# ================= PARSING =================

async def parse_sources(session, sent_links):
    collected = []

    for name, config in SOURCES.items():
        try:
            logger.info(f"Parsing {name}")

            async with session.get(config["url"], timeout=30) as response:
                logger.info(f"{name} status: {response.status}")

                if response.status != 200:
                    continue

                html = await response.text()
                soup = BeautifulSoup(html, "html.parser")

                links = soup.select(config["selector"])
                logger.info(f"{name}: {len(links)} links found")

                for link in links[:10]:
                    href = link.get("href")
                    title = link.get_text(strip=True)

                    if not href or not title:
                        continue

                    if href.startswith("/"):
                        href = config["url"].rstrip("/") + href

                    if href not in sent_links:
                        collected.append({
                            "title": title,
                            "url": href,
                            "source": name
                        })

        except Exception:
            logger.error(traceback.format_exc())

    logger.info(f"Collected {len(collected)} new articles")
    return collected

# ================= GENERATION =================

async def generate_post(client, news_items):
    combined = "\n\n".join(
        f"{n['title']} ({n['source']})"
        for n in news_items
    )

    prompt = f"""
Rewrite these crypto headlines into one concise Telegram post.
Avoid repetition.

News:
{combined}
"""

    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7
        )

        return response.choices[0].message.content

    except Exception:
        logger.error(traceback.format_exc())
        return None

# ================= MAIN LOOP =================

async def main():
    logger.info("Bot starting main loop")

    bot = Bot(token=BOT_TOKEN)
    client = AsyncOpenAI(api_key=OPENAI_API_KEY)

    sent_links = set()

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                news = await parse_sources(session, sent_links)

                if news:
                    post = await generate_post(client, news)

                    if post:
                        await bot.send_message(CHANNEL_ID, post)
                        logger.info("Post sent")

                        for item in news:
                            sent_links.add(item["url"])
                    else:
                        logger.error("Post generation failed")

                else:
                    logger.info("No new articles")

            except Exception:
                logger.error("Unhandled error in main loop")
                logger.error(traceback.format_exc())

            await asyncio.sleep(CHECK_INTERVAL)

# ================= ENTRY =================

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        logger.critical("Fatal crash")
        logger.critical(traceback.format_exc())
