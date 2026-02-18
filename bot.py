import logging
import os
import random
import asyncio
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from typing import List, Dict, Mapping, Sequence, Set

# --- ПЕРЕМЕННЫЕ И НАСТРОЙКИ ---
load_dotenv()
BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_ORG = os.getenv("OPENAI_ORG")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

CHECK_INTERVAL = 60 * 2
MAX_TEXT_LEN = 3900
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

chat_ids: Set[int] = set()
last_news_ids: Set[str] = set()

# --- CHATGPT ---
def ask_gpt(messages: List[Dict]) -> str:
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Organization": OPENAI_ORG,
        "Content-Type": "application/json"
    }
    body = {"model": "gpt-3.5-turbo", "messages": messages, "temperature": 0.7, "max_tokens": 1200}
    try:
        resp = requests.post(url, headers=headers, json=body, timeout=30)
        if resp.status_code != 200:
            return f"Ошибка OpenAI API [{resp.status_code}]: {resp.text[:500]}"
        jd = resp.json()
        return jd["choices"][0]["message"]["content"]
    except Exception as e:
        return f"Ошибка общения с OpenAI: {e}"

# --- DALL-E 3 ---
def generate_dalle_image(prompt: str) -> str:
    url = "https://api.openai.com/v1/images/generations"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Organization": OPENAI_ORG,
        "Content-Type": "application/json"
    }
    data = {"model": "dall-e-3", "prompt": prompt, "n": 1, "size": "1024x1024", "response_format": "url"}
    try:
        resp = requests.post(url, headers=headers, json=data, timeout=60)
        if resp.status_code != 200:
            raise RuntimeError(f"Ошибка DALL-E: {resp.text[:300]}")
        jd = resp.json()
        return jd["data"][0]["url"]
    except Exception as e:
        raise RuntimeError(f"Ошибка генерации изображения: {e}")

# --- ПАРСИНГ НОВОСТЕЙ ---
def get_full_article_text(url: str, site: str) -> str:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        soup = BeautifulSoup(resp.text, "html.parser")
        main = None
        if site == 'destructoid':
            main = soup.find("div", class_="article-content")
        elif site in ['pcgamer', 'rockpapershotgun']:
            main = soup.find("div", class_="article-body")
        elif site == 'nvidia':
            main = soup.find("div", class_="td-post-content") or soup.find("div", class_="tdb-block-inner td-fix-index")
        return main.get_text(separator="\n", strip=True) if main else ""
    except Exception as e:
        logging.debug(f"[DEBUG:{site}] Ошибка парсинга: {e}")
        return ""

def fetch_news_generic(url: str, article_tag: str, title_tags: List[str], site: str, prefix: str = "") -> List[Dict]:
    news = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        soup = BeautifulSoup(resp.text, "html.parser")
        articles = soup.find_all(article_tag)
        for art in articles[:7]:
            a = art.find("a", href=True)
            title = None
            for t in title_tags:
                title = art.find(t)
                if title:
                    break
            if not a or not title:
                continue
            href = a['href']
            if not href.startswith("http"):
                href = prefix + href
            full_text = get_full_article_text(href, site) or title.get_text(strip=True)
            news.append({"id": href, "title": title.get_text(strip=True), "url": href, "full_text": full_text, "source": site})
    except Exception as e:
        logging.debug(f"[DEBUG:{site}] Ошибка: {e}")
    return news

def fetch_all_selected_news() -> Dict[str, List[Dict]]:
    return {
        'destructoid': fetch_news_generic("https://www.destructoid.com/news/", "article", ["h2","h3"], "destructoid", "https://www.destructoid.com"),
        'pcgamer': fetch_news_generic("https://www.pcgamer.com/news/", "div", ["h3","h4"], "pcgamer", "https://www.pcgamer.com"),
        'rockpapershotgun': fetch_news_generic("https://www.rockpapershotgun.com/news/", "article", ["h2","h3"], "rockpapershotgun", "https://www.rockpapershotgun.com"),
        'nvidia': fetch_news_generic("https://blogs.nvidia.com/", "article", ["h2","h3"], "nvidia", "https://blogs.nvidia.com")
    }

# --- ДАЙДЖЕСТ ---
def split_digest_by_news(digest: str, max_news_per_msg: int = 4) -> List[str]:
    news_blocks = [block.strip() for block in digest.split('\n\n') if block.strip()]
    return ['\n\n'.join(news_blocks[i:i+max_news_per_msg]) for i in range(0, len(news_blocks), max_news_per_msg)]

def get_image_prompt(news: Dict) -> str:
    sys_prompt = (
        "You are creating prompts for image generation AIs in English. "
        "Base your prompt on the news article below..."
    )
    user_prompt = f"News:\n{news['title']}\n{news['full_text'][:600]}\n"
    messages = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_prompt}]
    first_prompt = ask_gpt(messages).strip() or "Prompt could not be generated."
    final_prompt = (
        "The character from image.png (a pixel art character...) "
        f"is present in the scene. {first_prompt} The character interacts with main elements."
    )
    return final_prompt.strip()

from telegram.ext import Application
    while True:
        all_news = fetch_all_selected_news()
        new_news = [n for lst in all_news.values() for n in lst if n["id"] not in last_news_ids]
        if not new_news or not chat_ids:
            await asyncio.sleep(CHECK_INTERVAL)
            continue

        digest_input = ""
        for news in new_news:
            digest_input += f"{news['title']}\n{news['full_text'][:550].rsplit(' ',1)[0]}…\n\n"
        digest_input = digest_input.strip()[:2800]

        chatgpt_prompt = (
            "Составь дайджест новостей игрового и IT-миров..."
            f"{digest_input}"
        )
        messages = [
            {"role": "system", "content": "Ты профессиональный редактор гейм- и IT-дайджестов."},
            {"role": "user", "content": chatgpt_prompt}
        ]
        digest = ask_gpt(messages) or "Не удалось получить дайджест от ChatGPT."

        news_for_prompt = random.choice(new_news)
        try:
            img_url = generate_dalle_image(get_image_prompt(news_for_prompt))
        except Exception as e:
            logging.error(f"Ошибка генерации картинки: {e}")
            img_url = None

        for chat_id in chat_ids.copy():
            try:
                if img_url:
                    await application.bot.send_photo(chat_id=chat_id, photo=img_url, caption="🖼️ Сгенерировано ИИ (DALL-E 3)")
                else:
                    await application.bot.send_message(chat_id=chat_id, text="Ошибка генерации изображения.")
                await asyncio.sleep(2)
                for msg in split_digest_by_news(digest):
                    parts = [msg[i:i+MAX_TEXT_LEN] for i in range(0,len(msg),MAX_TEXT_LEN)]
                    for part in parts:
                        await application.bot.send_message(chat_id=chat_id, text=part)
                        await asyncio.sleep(2)
            except Exception as e:
                logging.error(f"Ошибка отправки в {chat_id}: {e}")

        last_news_ids.update(n['id'] for n in new_news)
        await asyncio.sleep(CHECK_INTERVAL)

# --- КОМАНДЫ ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat_ids.add(chat_id)
    logging.info(f"Subscribed: {chat_ids}")
    await update.message.reply_text(f"Hello! Your chat ID is {chat_id}.\nДоброго времени суток.")

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat_ids.discard(chat_id)
    logging.info(f"Unsubscribed: {chat_ids}")
    await update.message.reply_text("Вы больше не получаете новости." if chat_id not in chat_ids else "Goodbye!")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Я понимаю только команды /start и /stop.")

async def post_init(application: Application):
    application.create_task(
        send_news_periodically(application, last_news_ids, chat_ids)
    )
    
# --- MAIN --- 
def main():
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан в .env")

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stop", stop))
    application.add_handler(MessageHandler(filters.ALL, echo))

     application.run_polling()


if __name__ == "__main__":
    main()

