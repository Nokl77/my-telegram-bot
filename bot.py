import logging
import os
import random
import threading
import time
from typing import List, Set, Dict
import requests
from bs4 import BeautifulSoup

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    CallbackContext,
    filters
)

# Загрузка переменных окружения
load_dotenv()
BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_ORG = os.getenv("OPENAI_ORG")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

CHECK_INTERVAL = 60 * 240  # 4 часа

chat_ids: Set[int] = set()
last_news_ids: Set[str] = set()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
}

MAX_TEXT_LEN = 3900

# --- CHATGPT BLOCK ---
def ask_gpt(messages: List[Dict]) -> str:
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Organization": OPENAI_ORG,
        "Content-Type": "application/json"
    }
    body = {
        "model": "gpt-3.5-turbo",
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 1200
    }
    try:
        resp = requests.post(url, headers=headers, json=body, timeout=30)
        if resp.status_code != 200:
            text = resp.text
            return f"Ошибка OpenAI API [{resp.status_code}]: {text[:500]}"
        jd = resp.json()
        return jd["choices"][0]["message"]["content"]
    except Exception as e:
        return f"Ошибка общения с OpenAI: {e}"

# --- DALL-E 3 IMAGE BLOCK ---
def generate_dalle_image(prompt: str) -> str:
    url = "https://api.openai.com/v1/images/generations"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Organization": OPENAI_ORG,
        "Content-Type": "application/json"
    }
    data = {
        "model": "dall-e-3",
        "prompt": prompt,
        "n": 1,
        "size": "1024x1024",
        "response_format": "url"
    }
    try:
        resp = requests.post(url, headers=headers, json=data, timeout=60)
        if resp.status_code != 200:
            txt = resp.text
            raise RuntimeError(f"Ошибка DALL-E: {txt[:300]}")
        jd = resp.json()
        return jd["data"][0]["url"]
    except Exception as e:
        raise RuntimeError(f"Ошибка генерации изображения: {e}")

# --- NEWS BLOCK ---
def get_full_article_text(url: str, site: str) -> str:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        text = resp.text
        soup = BeautifulSoup(text, "html.parser")
        if site == 'destructoid':
            main = soup.find("div", class_="article-content")
            if main:
                return main.get_text(separator="\n", strip=True)
        elif site == 'pcgamer':
            main = soup.find("div", class_="article-body")
            if main:
                return main.get_text(separator="\n", strip=True)
        elif site == 'rockpapershotgun':
            main = soup.find("div", class_="article-body")
            if main:
                return main.get_text(separator="\n", strip=True)
        elif site == 'nvidia':
            main = soup.find("div", class_="td-post-content")
            if not main:
                main = soup.find("div", class_="tdb-block-inner td-fix-index")
            if main:
                return main.get_text(separator="\n", strip=True)
        return ""
    except Exception as e:
        print(f"[DEBUG:{site}] Ошибка парсинга полного текста: {e}")
        return ""

def fetch_destructoid_news() -> List[Dict]:
    print("[DEBUG:destructoid] Получаю новости Destructoid")
    url = "https://www.destructoid.com/news/"
    news = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        soup = BeautifulSoup(resp.text, "html.parser")
        articles = soup.find_all("article")
        print(f"[DEBUG:destructoid] Найдено {len(articles)} <article>")
        for art in articles[:7]:
            a = art.find("a", href=True)
            title = art.find("h2") or art.find("h3")
            if not a or not title:
                continue
            href = a['href']
            if not href.startswith("http"):
                href = "https://www.destructoid.com" + href
            full_text = get_full_article_text(href, 'destructoid')
            if not full_text:
                full_text = title.get_text(strip=True)
            news.append({
                'id': href,
                'title': title.get_text(strip=True),
                'url': href,
                'full_text': full_text,
                'source': 'destructoid'
            })
    except Exception as e:
        print(f"[DEBUG:destructoid] Ошибка: {e}")
    return news

def fetch_pcgamer_news() -> List[Dict]:
    print("[DEBUG:pcgamer] Получаю новости PCGamer")
    url = "https://www.pcgamer.com/news/"
    news = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        soup = BeautifulSoup(resp.text, "html.parser")
        articles = soup.find_all("div", class_="listingResult")
        print(f"[DEBUG:pcgamer] Найдено {len(articles)} новостных блоков")
        for art in articles[:7]:
            a = art.find("a", href=True)
            title = art.find("h3") or art.find("h4")
            if not a or not title:
                continue
            href = a['href']
            if not href.startswith("http"):
                href = "https://www.pcgamer.com" + href
            full_text = get_full_article_text(href, 'pcgamer')
            if not full_text:
                full_text = title.get_text(strip=True)
            news.append({
                'id': href,
                'title': title.get_text(strip=True),
                'url': href,
                'full_text': full_text,
                'source': 'pcgamer'
            })
    except Exception as e:
        print(f"[DEBUG:pcgamer] Ошибка: {e}")
    return news

def fetch_rockpapershotgun_news() -> List[Dict]:
    print("[DEBUG:rps] Получаю новости Rock Paper Shotgun")
    url = "https://www.rockpapershotgun.com/news/"
    news = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        soup = BeautifulSoup(resp.text, "html.parser")
        articles = soup.find_all("article")
        print(f"[DEBUG:rps] Найдено {len(articles)} <article>")
        for art in articles[:7]:
            a = art.find("a", href=True)
            title = art.find("h2") or art.find("h3")
            if not a or not title:
                continue
            href = a['href']
            if not href.startswith("http"):
                href = "https://www.rockpapershotgun.com" + href
            full_text = get_full_article_text(href, 'rockpapershotgun')
            if not full_text:
                full_text = title.get_text(strip=True)
            news.append({
                'id': href,
                'title': title.get_text(strip=True),
                'url': href,
                'full_text': full_text,
                'source': 'rockpapershotgun'
            })
    except Exception as e:
        print(f"[DEBUG:rps] Ошибка: {e}")
    return news

def fetch_nvidia_news() -> List[Dict]:
    print("[DEBUG:nvidia] Получаю новости NVIDIA")
    url = "https://blogs.nvidia.com/"
    news = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        soup = BeautifulSoup(resp.text, "html.parser")
        articles = soup.find_all("article")
        print(f"[DEBUG:nvidia] Найдено {len(articles)} <article>")
        for art in articles[:7]:
            a = art.find("a", href=True)
            title = art.find("h2") or art.find("h3")
            if not a or not title:
                continue
            href = a['href']
            if not href.startswith("http"):
                href = "https://blogs.nvidia.com" + href
            full_text = get_full_article_text(href, 'nvidia')
            if not full_text:
                full_text = title.get_text(strip=True)
            news.append({
                'id': href,
                'title': title.get_text(strip=True),
                'url': href,
                'full_text': full_text,
                'source': 'nvidia'
            })
    except Exception as e:
        print(f"[DEBUG:nvidia] Ошибка: {e}")
    return news

def fetch_all_selected_news() -> Dict[str, List[Dict]]:
    result = {}
    fetchers = [
        ('destructoid', fetch_destructoid_news),
        ('pcgamer', fetch_pcgamer_news),
        ('rockpapershotgun', fetch_rockpapershotgun_news),
        ('nvidia', fetch_nvidia_news),
    ]
    for name, fetcher in fetchers:
        try:
            news = fetcher()
            print(f"[DEBUG] {name}: {len(news)} news parsed")
            result[name] = news
        except Exception as e:
            logging.error(f"{name}: {e}")
            print(f"[DEBUG] {name}: error {e}")
            result[name] = []
    return result

def split_digest_by_news(digest: str, max_news_per_msg: int = 4) -> List[str]:
    news_blocks = [block.strip() for block in digest.split('\n\n') if block.strip()]
    messages = []
    for i in range(0, len(news_blocks), max_news_per_msg):
        chunk = '\n\n'.join(news_blocks[i:i+max_news_per_msg])
        messages.append(chunk)
    return messages

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
    first_prompt = ask_gpt(messages)
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

def send_news_periodically(application):
    global last_news_ids
    while True:
        all_news = fetch_all_selected_news()
        new_news = []
        for news_list in all_news.values():
            for news in news_list:
                if news['id'] not in last_news_ids:
                    new_news.append(news)
        if not new_news or not chat_ids:
            time.sleep(CHECK_INTERVAL)
            continue

        digest_input = ""
        for news in new_news:
            digest_input += f"{news['title']}\n"
            content = news['full_text'].strip()
            if content:
                short_content = content if len(content) < 550 else content[:550].rsplit(' ', 1)[0] + "…"
                digest_input += short_content
            digest_input += "\n\n"
        digest_input = digest_input.strip()
        if len(digest_input) > 2800:
            digest_input = "\n\n".join(news['title'] for news in new_news)

        chatgpt_prompt = (
            "Составь дайджест новостей игрового и IT-миров за прошедшее время обязательно на русском языке. "
            "Для каждой новости напиши отдельный абзац. Названия игр и компаний приводи только на английском (оригинал), не переводя их, всё остальное по-русски. "
            "Не используй нумерацию, не навязывай субъективные оценки, не растекайся мыслями, не переводи названия игр и компаний. "
            "Название каждой игры или компании выделяй жирным шрифтом.\n\n"
            f"{digest_input}"
        )
        messages = [
            {"role": "system", "content": "Ты профессиональный редактор гейм- и IT-дайджестов. Не переводишь названия игр и компаний."},
            {"role": "user", "content": chatgpt_prompt}
        ]
        digest = ask_gpt(messages)
        if not digest:
            digest = "Не удалось получить дайджест от ChatGPT."

        news_for_prompt = random.choice(new_news)
        img_prompt = get_image_prompt(news_for_prompt)
        try:
            img_url = generate_dalle_image(img_prompt)
        except Exception as e:
            logging.error(f"Ошибка генерации картинки через DALL-E: {e}")
            img_url = None

        for chat_id in chat_ids.copy():
            try:
                if img_url:
                    application.bot.send_photo(chat_id=chat_id, photo=img_url, caption="🖼️ Сгенерировано ИИ (DALL-E 3), персонаж из image.png")
                else:
                    application.bot.send_message(chat_id=chat_id, text="Ошибка генерации изображения.")
                time.sleep(2)
            except Exception as e:
                logging.error(f"Ошибка отправки изображения в {chat_id}: {e}")

            split_msgs = split_digest_by_news(digest, max_news_per_msg=4)
            for idx, msg in enumerate(split_msgs):
                try:
                    if len(msg) > MAX_TEXT_LEN:
                        parts = [msg[i:i+MAX_TEXT_LEN] for i in range(0, len(msg), MAX_TEXT_LEN)]
                    else:
                        parts = [msg]
                    for part in parts:
                        application.bot.send_message(chat_id=chat_id, text=part)
                        time.sleep(2)
                except Exception as e:
                    logging.error(f"Ошибка отправки дайджеста в {chat_id}: {e}")

        last_news_ids.update(news['id'] for news in new_news)
        time.sleep(CHECK_INTERVAL)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat_ids.add(chat_id)
    await update.message.reply_text("Доброго времени суток. Теперь я буду публиковать новости раз в 4 часа.")

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in chat_ids:
        chat_ids.remove(chat_id)
        await update.message.reply_text("❌ Бот больше не будет публиковать новости в этом чате.")
    else:
        await update.message.reply_text("Вы не подписаны на новости.")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Я понимаю только команды /start и /stop.")

def main():
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stop", stop))
    application.add_handler(MessageHandler(filters.ALL, echo))

    # Запуск новостной функции в отдельном потоке (чтобы не мешать poll'ингу бота)
    threading.Thread(target=send_news_periodically, args=(application,), daemon=True).start()

    print("Бот запущен!")
    application.run_polling()

if __name__ == "__main__":
    main()