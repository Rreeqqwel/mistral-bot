import asyncio
import logging
import os
import sqlite3
from datetime import datetime
from urllib.parse import quote

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, Message, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from mistralai import Mistral
import aiohttp
import re

# Пытаемся импортировать duckduckgo-search
try:
    from duckduckgo_search import DDGS
    DDGS_AVAILABLE = True
except ImportError:
    DDGS_AVAILABLE = False
    print("duckduckgo-search не установлен → используется fallback-поиск")

# ────────────────────────────────────────
#               НАСТРОЙКИ
# ────────────────────────────────────────

BOT_TOKEN       = "8514230306:AAE-EtoSqaAOuYpt-SRjjKm5KO0ZA89Tkvk"
MISTRAL_API_KEY = "D3kqGMWHab0Y06dsdd1ljqo4Xjs6isKW"

MODEL = "mistral-large-latest"          # или mistral-small-latest

# ────────────────────────────────────────
#              БАЗА ДАННЫХ
# ────────────────────────────────────────

DB_FILE = "chat_history.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            role TEXT,
            content TEXT,
            timestamp DATETIME
        )
    ''')
    conn.commit()
    conn.close()

def save_message(user_id: int, role: str, content: str):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO messages (user_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        (user_id, role, content, datetime.utcnow())
    )
    conn.commit()
    conn.close()

def get_history(user_id: int, max_messages: int = 12) -> list:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "SELECT role, content FROM messages WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, max_messages)
    )
    rows = c.fetchall()
    conn.close()
    return [{"role": r, "content": c} for r, c in reversed(rows)]

# ────────────────────────────────────────
#               КЛИЕНТЫ
# ────────────────────────────────────────

mistral_client = Mistral(api_key=MISTRAL_API_KEY)

# ────────────────────────────────────────
#                СОСТОЯНИЯ
# ────────────────────────────────────────

class GenStates(StatesGroup):
    waiting_for_image_prompt = State()
    waiting_for_search_query = State()

# ────────────────────────────────────────
#             КЛАВИАТУРА
# ────────────────────────────────────────

main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="Поиск в интернете 🔎"),
            KeyboardButton(text="Нарисовать картинку 🎨"),
        ],
        [
            KeyboardButton(text="Очистить память 🧹"),
        ]
    ],
    resize_keyboard=True,
    input_field_placeholder="Напиши сообщение..."
)

# ────────────────────────────────────────
#               ХЕНДЛЕРЫ
# ────────────────────────────────────────

dp = Dispatcher(storage=MemoryStorage())
bot: Bot = None

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Я бот с Mistral AI + поиском в реальном времени.\n\n"
        "Могу отвечать на вопросы, искать свежую информацию, рисовать картинки.\n"
        "Просто пиши или используй кнопки!",
        reply_markup=main_kb
    )

@dp.message(lambda m: m.text == "Очистить память 🧹")
async def clear_history(message: Message):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM messages WHERE user_id = ?", (message.from_user.id,))
    conn.commit()
    conn.close()
    await message.answer("Память очищена ✓")

@dp.message(lambda m: m.text == "Поиск в интернете 🔎")
async def ask_search_query(message: Message, state: FSMContext):
    await message.answer("Какой запрос отправить в поиск?")
    await state.set_state(GenStates.waiting_for_search_query)

@dp.message(lambda m: m.text == "Нарисовать картинку 🎨")
async def ask_image_prompt(message: Message, state: FSMContext):
    await message.answer("Напиши описание картинки (на любом языке):")
    await state.set_state(GenStates.waiting_for_image_prompt)

@dp.message(GenStates.waiting_for_image_prompt)
async def generate_image(message: Message, state: FSMContext):
    prompt = message.text.strip()
    if not prompt:
        await message.answer("Пустой запрос не принимаю :)")
        return

    await message.answer("Генерирую... подожди 8–20 секунд")

    try:
        safe_prompt = quote(prompt)
        url = f"https://image.pollinations.ai/prompt/{safe_prompt}?model=flux&width=1152&height=896&nologo=true"

        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=45) as resp:
                if resp.status != 200:
                    await message.answer(f"Ошибка генерации: статус {resp.status}")
                    return
                image_data = await resp.read()

        path = f"gen_{message.from_user.id}.jpg"
        with open(path, "wb") as f:
            f.write(image_data)

        await message.answer_photo(
            photo=FSInputFile(path),
            caption=f"**Prompt:** {prompt}"
        )
        os.remove(path)

    except Exception as e:
        await message.answer(f"Не получилось сгенерировать\n\n{str(e)[:300]}")

    await state.clear()

@dp.message(GenStates.waiting_for_search_query)
async def process_search(message: Message, state: FSMContext):
    query = message.text.strip()
    if not query:
        await message.answer("Пустой запрос не принимаю :)")
        await state.clear()
        return

    await message.answer("Ищу...")
    search_result = await perform_search(query)

    # Передаём результаты поиска в Mistral как контекст
    user_id = message.from_user.id
    save_message(user_id, "user", f"[ПОИСК ЗАПРОС] {query}")
    save_message(user_id, "assistant", f"[ПОИСК РЕЗУЛЬТАТ] {search_result[:1500]}...")  # обрезаем, чтобы не перегрузить

    history = get_history(user_id, 12)

    messages = [
        {"role": "system", "content": "Ты имеешь доступ к свежим результатам поиска из интернета (февраль 2026). Используй их для точных и актуальных ответов. Отвечай по-русски."},
        {"role": "user", "content": f"Результаты поиска по запросу '{query}':\n{search_result}\n\nТеперь ответь на запрос пользователя подробно и точно."}
    ] + history

    try:
        resp = mistral_client.chat.complete(
            model=MODEL,
            messages=messages,
            temperature=0.7,
            max_tokens=1800
        )
        answer = resp.choices[0].message.content.strip()
        await message.answer(answer)
        save_message(user_id, "assistant", answer)

    except Exception as e:
        await message.answer(f"Ошибка при обработке:\n{str(e)[:400]}\n\nНо вот сырые результаты поиска:\n{search_result}")

    await state.clear()

async def perform_search(query: str) -> str:
    if DDGS_AVAILABLE:
        try:
            with DDGS() as ddgs:
                results = [r for r in ddgs.text(query, max_results=6)]
            if not results:
                return "Ничего не нашлось."
            answer = f"Результаты DuckDuckGo по '{query}':\n\n"
            for r in results:
                title = r.get('title', 'Без заголовка')
                body = r.get('body', '')[:220] + '...' if r.get('body') else ''
                href = r.get('href', '')
                answer += f"**{title}**\n{body}\n{href}\n\n"
            return answer
        except Exception as e:
            return f"DDGS ошибка: {str(e)}\n\nПереключаюсь на fallback..."

    # Fallback: простой HTML-парсинг
    return await fallback_ddg_search(query)

async def fallback_ddg_search(query: str) -> str:
    url = f"https://duckduckgo.com/html/?q={quote(query)}"
    headers = {"User-Agent": "Mozilla/5.0 (Android; Mobile)"}

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers, timeout=15) as resp:
                if resp.status != 200:
                    return f"Ошибка: статус {resp.status}"
                html = await resp.text()
        except Exception as e:
            return f"Не удалось подключиться: {str(e)}"

    results = []
    pattern = r'<a class="result__a" href="[^"]*uddg=(?P<url>[^&"]+).*?>(?P<title>.*?)</a>.*?<a class="result__snippet"[^>]*>(?P<snippet>.*?)</a>'
    for match in re.finditer(pattern, html, re.DOTALL | re.IGNORECASE):
        if len(results) >= 5:
            break
        url_dec = match.group('url')
        title = re.sub(r'<.*?>', '', match.group('title')).strip()
        snippet = re.sub(r'<.*?>', '', match.group('snippet')).strip()[:220]
        results.append(f"**{title}**\n{snippet}...\n{url_dec}\n")

    if not results:
        return "Ничего не нашлось или ошибка парсинга."
    
    return "Результаты DuckDuckGo (fallback):\n\n" + "\n".join(results)

@dp.message()
async def handle_message(message: Message, state: FSMContext):
    user_id = message.from_user.id
    text = message.text.strip()

    if not text:
        return

    current_state = await state.get_state()
    if current_state:
        # Если в состоянии — пропускаем в соответствующий хендлер
        return

    # Авто-триггер на новости/актуальное
    lower_text = text.lower()
    if any(w in lower_text for w in ["новости", "новое", "свежие", "2026", "февраль 2026", "последние", "что сейчас", "что происходит"]):
        await message.answer("Похоже, тебе нужны свежие данные — ищу...")
        search_result = await perform_search(text)
        context_msg = f"Результаты поиска:\n{search_result}"
        
        save_message(user_id, "user", text)
        save_message(user_id, "assistant", context_msg[:1500])

        history = get_history(user_id)

        messages = [
            {"role": "system", "content": "Используй свежие данные из поиска (февраль 2026) для ответа."},
            {"role": "user", "content": f"{context_msg}\n\nТеперь ответь подробно на: {text}"}
        ] + history

    else:
        save_message(user_id, "user", text)
        history = get_history(user_id)

        messages = [
            {"role": "system", "content": "Ты полезный, остроумный помощник. Отвечай по-русски, если вопрос на русском. Если нужны свежие данные — скажи, но старайся отвечать на основе имеющегося контекста."}
        ] + history

    await message.answer("Думаю...")

    try:
        resp = mistral_client.chat.complete(
            model=MODEL,
            messages=messages,
            temperature=0.75,
            max_tokens=2048
        )
        answer = resp.choices[0].message.content.strip()
        await message.answer(answer)
        save_message(user_id, "assistant", answer)

    except Exception as e:
        await message.answer(f"Ошибка Mistral API\n\n{str(e)[:500]}")

async def main():
    global bot
    init_db()
    bot = Bot(token=BOT_TOKEN)
    await dp.start_polling(bot, allowed_updates=types.default_allowed_updates)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
