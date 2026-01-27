from fastapi import FastAPI, Request
import uvicorn
import os
import logging
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from ebooklib import epub
from bs4 import BeautifulSoup
from pymongo import MongoClient

# ================== НАСТРОЙКИ ==================

BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
MAX_CHARS_PER_PAGE = 900

PARAGRAPHS_PER_PAGE = 3

# ================== ЛОГИ (БЕЗОПАСНО) ==================

logging.basicConfig(level=logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

# ================== MONGODB ==================

client = MongoClient(MONGO_URI)
db = client["telegram_reader"]
users = db["users"]

# ================== КЭШ ==================

book_cache = {}  
# user_id -> {
#   title,
#   author,
#   pages,
#   message_id
# }

# ================== ВСПОМОГАТЕЛЬНОЕ ==================

def clean_html(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "nav"]):
        tag.decompose()

    paragraphs = []

    for el in soup.find_all(["h1", "h2", "h3", "p"]):
        text = el.get_text(" ", strip=True)
        if not text:
            continue

        if el.name.startswith("h"):
            paragraphs.append(f"*{text.upper()}*")
        else:
            paragraphs.append(text)

    return paragraphs


def split_pages(paragraphs: list[str]) -> list[str]:
    pages = []
    current_page = ""
    
    for p in paragraphs:
        # если абзац сам по себе слишком длинный
        if len(p) > MAX_CHARS_PER_PAGE:
            words = p.split(" ")
            chunk = ""
            for w in words:
                if len(chunk) + len(w) + 1 > MAX_CHARS_PER_PAGE:
                    pages.append(chunk.strip())
                    chunk = w + " "
                else:
                    chunk += w + " "
            if chunk.strip():
                pages.append(chunk.strip())
            continue

        # обычная логика
        if len(current_page) + len(p) + 2 > MAX_CHARS_PER_PAGE:
            pages.append(current_page.strip())
            current_page = p + "\n\n"
        else:
            current_page += p + "\n\n"

    if current_page.strip():
        pages.append(current_page.strip())

    return pages


def reader_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⬅️", callback_data="prev"),
                InlineKeyboardButton("➡️", callback_data="next"),
            ],
            [
                InlineKeyboardButton("🗑 Удалить книгу", callback_data="clear")
            ]
        ]
    )

# ================== КОМАНДЫ ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📚 Отправь EPUB-файл.\n"
        "⬅️ ➡️ — листать страницы."
    )

# ================== EPUB ==================

async def handle_epub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    document = update.message.document

    if not document.file_name.lower().endswith(".epub"):
        await update.message.reply_text("❌ Только EPUB")
        return

    file = await document.get_file()
    path = f"{user_id}.epub"
    await file.download_to_drive(path)

    book = epub.read_epub(path)

    paragraphs = []

    # 🔑 ЧИТАЕМ СТРОГО ПО SPINE
    for item_id, _ in book.spine:
        item = book.get_item_with_id(item_id)
        if item and item.get_type() == 9:
            paragraphs.extend(
                clean_html(item.get_content().decode("utf-8"))
            )

    pages = split_pages(paragraphs)

    title = book.get_metadata("DC", "title")
    author = book.get_metadata("DC", "creator")

    title = title[0][0] if title else "Без названия"
    author = author[0][0] if author else "Автор неизвестен"

    users.update_one(
        {"user_id": user_id},
        {"$set": {
            "user_id": user_id,
            "page": 0
        }},
        upsert=True
    )

    book_cache[user_id] = {
        "title": title,
        "author": author,
        "pages": pages,
        "message_id": None
    }

    await send_page(update, context, user_id, first=True)

# ================== СТРАНИЦЫ ==================

async def send_page(update, context, user_id, first=False):
    data = users.find_one({"user_id": user_id})
    cache = book_cache.get(user_id)

    if not data or not cache:
        return

    page = data["page"]
    total = len(cache["pages"])
    progress = int((page + 1) / total * 100)

    text = (
    	f"📘 *{cache['title']}*\n"
    	f"✍️ _{cache['author']}_\n\n"
    	f"{cache['pages'][page]}\n\n"
    	f"📖 {page + 1}/{total} • {progress}%"
    )


    if first or cache["message_id"] is None:
        msg = await update.message.reply_text(
            text,
            reply_markup=reader_keyboard(),
            parse_mode="Markdown"
        )
        cache["message_id"] = msg.message_id
    else:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=cache["message_id"],
            text=text,
            reply_markup=reader_keyboard(),
            parse_mode="Markdown"
        )

# ================== КНОПКИ ==================

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    # мгновенно отвечаем Telegram
    try:
        await query.answer()
    except:
        pass

    user_id = query.from_user.id
    data = users.find_one({"user_id": user_id})
    cache = book_cache.get(user_id)

    if not data or not cache:
        return

    page = data["page"]
    total_pages = len(cache["pages"])

    if query.data == "next":
        if page < total_pages - 1:
            page += 1

    elif query.data == "prev":
        if page > 0:
            page -= 1

    elif query.data == "clear":
        # удалить данные пользователя
        users.delete_one({"user_id": user_id})
        book_cache.pop(user_id, None)

        # удалить сообщение с книгой
        try:
            await context.bot.delete_message(
                chat_id=query.message.chat_id,
                message_id=query.message.message_id
            )
        except:
            pass

        # вернуть стартовое состояние
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=(
                "📚 Читалка сброшена.\n\n"
                "Отправь EPUB-файл, чтобы начать читать."
            )
        )
        return

    # сохранить страницу
    users.update_one(
        {"user_id": user_id},
        {"$set": {"page": page}}
    )

    # 🔴 ВАЖНО: обновляем сообщение
    await send_page(update, context, user_id)


# ================== ЗАПУСК ==================

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        MessageHandler(filters.Document.FileExtension("epub"), handle_epub)
    )
    app.add_handler(CallbackQueryHandler(callbacks))

    print("✅ Бот запущен")
    WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
    WEBHOOK_URL = f"https://ТВОЙ-RENDER-URL{WEBHOOK_PATH}"

    fastapi_app = FastAPI()
    telegram_app = ApplicationBuilder().token(BOT_TOKEN).build()

    @fastapi_app.on_event("startup")
    async def on_startup():
       await telegram_app.bot.set_webhook(WEBHOOK_URL)

    @fastapi_app.post(WEBHOOK_PATH)
    async def telegram_webhook(request: Request):
       data = await request.json()
       update = Update.de_json(data, telegram_app.bot)
       await telegram_app.process_update(update)
       return {"ok": True}

    def main():
      telegram_app.add_handler(CommandHandler("start", start))
      telegram_app.add_handler(
        MessageHandler(filters.Document.FileExtension("epub"), handle_epub)
      )
      telegram_app.add_handler(CallbackQueryHandler(callbacks))


if __name__ == "__main__":
    main()
