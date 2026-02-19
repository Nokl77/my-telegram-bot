import asyncio
import logging
import aiohttp
from bs4 import BeautifulSoup
from openai import AsyncOpenAI
from aiogram import Bot
import traceback
import sys
import signal

print("=== BOT FILE LOADED ===", flush=True)

# ================= CONFIG =================

BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
CHANNEL_ID = "@your_channel"
OPENAI_API_KEY = "YOUR_OPENAI_API_KEY"

CHECK_INTERVAL = 60 * 2  # 2 minutes

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

# ================= LOGGING =================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    stream=sys.stdout
)

logger = logging.getLogger(__name__)

# ================= GLOBALS =================

bot = Bot(token=BOT_TOKEN)
client = AsyncOpenAI(api_key=OPENAI_API_KEY)

news_queue = asyncio.Queue()
sent_links = set()

# ================= SIGNAL HANDLING =================

def handle_shutdown(signum, frame):
    logger.error(f"Received shutdown signal: {signum}")
    sys.exit(1)

signal.signal(signal.SIGTERM, handle_shutdown)
signal.signal(signal.SIGINT, handle_shutdown)

# ================= PARSING =================

async def parse_sources(session):
    collected = []

    for name, config in SOURCES.items():
        try:
            logger.info(f"Parsing source: {name}")

            async with session.get(config["url"], timeout=30) as response:
                if response.status != 200:
                    logger.error(f"{name} returned status {response.status}")
                    continue

                html = await response.text()
                soup = BeautifulSoup(html, "html.parser")

                links = soup.select(config["selector"])
                logger.info(f"{name}: found {len(links)} raw links")

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

        except Exception as e:
            logger.error(f"Error parsing {name}: {e}")
            logger.error(traceback.format_exc())

    logger.info(f"Total new articles before deduplication: {len(collected)}")
    return collected

# ================= PARSER WORKER =================

async def parser_worker():
    logger.info("Parser worker started")

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                logger.info("Starting parsing cycle")

                news = await parse_sources(session)

                if news:
                    await news_queue.put(news)
                    logger.info(f"Queued {len(news)} new articles")
                else:
                    logger.info("No new articles found in this cycle")

            except Exception as e:
                logger.error(f"Parser worker error: {e}")
                logger.error(traceback.format_exc())

            await asyncio.sleep(CHECK_INTERVAL)

# ================= AI GENERATION =================

async def generate_post(news_items):
    try:
        combined_text = "\n\n".join(
            [f"{n['title']} ({n['source']})" for n in news_items]
        )

        prompt = f"""
Rewrite these crypto news headlines into one engaging Telegram post.
Avoid repetition.
Keep it concise and informative.

News:
{combined_text}
"""

        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7
        )

        return response.choices[0].message.content

    except Exception as e:
        logger.error(f"AI generation error: {e}")
        logger.error(traceback.format_exc())
        return None

# ================= GENERATOR WORKER =================

async def generator_worker():
    logger.info("Generator worker started")

    while True:
        try:
            logger.info("Waiting for news in queue")
            news_items = await news_queue.get()

            logger.info(f"Generating post for {len(news_items)} articles")

            post = await generate_post(news_items)

            if post:
                await bot.send_message(CHANNEL_ID, post)
                logger.info("Post sent to Telegram")

                for item in news_items:
                    sent_links.add(item["url"])
            else:
                logger.error("Post generation returned empty result")

        except Exception as e:
            logger.error(f"Generator worker error: {e}")
            logger.error(traceback.format_exc())

# ================= HEARTBEAT =================

async def heartbeat():
    while True:
        logger.info("Bot is alive")
        await asyncio.sleep(300)

# ================= MAIN =================

async def main():
    logger.info("Bot starting")

    try:
        await asyncio.gather(
            parser_worker(),
            generator_worker(),
            heartbeat()
        )
    except Exception as e:
        logger.critical(f"Fatal error in main: {e}")
        logger.critical(traceback.format_exc())

if __name__ == "__main__":
    asyncio.run(main())

