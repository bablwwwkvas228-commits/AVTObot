import asyncio
import logging
import sqlite3
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, 
    ReplyKeyboardMarkup, KeyboardButton, FSInputFile
)

from curl_cffi.requests import AsyncSession
from bs4 import BeautifulSoup

# ================= 1. НАСТРОЙКИ =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("ОШИБКА: Переменная окружения BOT_TOKEN не задана в настройках хостинга!")

# Вставьте сюда ваш личный Telegram ID
ADMIN_ID = 1704471566

CHECK_INTERVAL_SECONDS = 40
DB_PATH = "data/bot_database.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ================= 2. БАЗА ДАННЫХ =================
def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS searches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            url TEXT,
            max_price INTEGER
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS deals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            title TEXT,
            buy_price INTEGER,
            repair_cost INTEGER,
            sell_price INTEGER,
            profit INTEGER,
            created_at TEXT
        )
    """)
        
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS seen_items (
            search_id INTEGER,
            item_id TEXT,
            PRIMARY KEY (search_id, item_id)
        )
    """)
    conn.commit()
    conn.close()

def is_admin(user_id: int) -> bool:
    return ADMIN_ID == 0 or user_id == ADMIN_ID

def register_user(user_id: int, username: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (user_id, username or "User"))
    cursor.execute("UPDATE users SET username = ? WHERE user_id = ?", (username or "User", user_id))
    conn.commit()
    conn.close()

def add_search(user_id: int, url: str, max_price: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO searches (user_id, url, max_price) VALUES (?, ?, ?)", (user_id, url, max_price))
    conn.commit()
    conn.close()

def get_all_searches():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, user_id, url, max_price FROM searches")
    rows = cursor.fetchall()
    conn.close()
    return rows

def delete_search(search_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM searches WHERE id = ?", (search_id,))
    cursor.execute("DELETE FROM seen_items WHERE search_id = ?", (search_id,))
    conn.commit()
    conn.close()

def add_deal(user_id: int, title: str, buy_price: int, repair_cost: int, sell_price: int):
    profit = sell_price - (buy_price + repair_cost)
    tz_moscow = timezone(timedelta(hours=3))
    date_str = datetime.now(tz_moscow).strftime("%d.%m.%Y")
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO deals (user_id, title, buy_price, repair_cost, sell_price, profit, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (user_id, title, buy_price, repair_cost, sell_price, profit, date_str))
    conn.commit()
    conn.close()

def get_user_deals(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, buy_price, repair_cost, sell_price, profit, created_at FROM deals WHERE user_id = ? ORDER BY id DESC", (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_deal_by_id(deal_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, user_id, title, buy_price, repair_cost, sell_price, profit, created_at FROM deals WHERE id = ?", (deal_id,))
    row = cursor.fetchone()
    conn.close()
    return row

def update_deal(deal_id: int, title: str, buy_price: int, repair_cost: int, sell_price: int):
    profit = sell_price - (buy_price + repair_cost)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE deals 
        SET title = ?, buy_price = ?, repair_cost = ?, sell_price = ?, profit = ?
        WHERE id = ?
    """, (title, buy_price, repair_cost, sell_price, profit, deal_id))
    conn.commit()
    conn.close()

def delete_deal(deal_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM deals WHERE id = ?", (deal_id,))
    conn.commit()
    conn.close()

def get_user_financial_stats(user_id: int):
    deals = get_user_deals(user_id)
    total_profit = sum(d[5] for d in deals)
    total_deals = len(deals)
    return total_deals, total_profit

def is_item_seen(search_id: int, item_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM seen_items WHERE search_id = ? AND item_id = ?", (search_id, item_id))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def mark_item_as_seen(search_id: int, item_id: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO seen_items (search_id, item_id) VALUES (?, ?)", (search_id, item_id))
    conn.commit()
    conn.close()

# ================= 3. ПАРСИНГ =================
def format_avito_url(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    query['s'] = ['104']
    new_query = urlencode(query, doseq=True)
    return urlunparse(parsed._replace(query=new_query))

def parse_avito_date(date_str: str) -> str:
    if not date_str:
        return "Неизвестно"
        
    tz_moscow = timezone(timedelta(hours=3))
    now = datetime.now(tz_moscow)
    clean_str = date_str.replace('\xa0', ' ').lower().strip()

    try:
        if "только что" in clean_str or "секунд" in clean_str:
            return now.strftime("%d.%m.%Y %H:%M (МСК)")
            
        match_min = re.search(r'(\d+)\s*минут', clean_str)
        if match_min:
            dt = now - timedelta(minutes=int(match_min.group(1)))
            return dt.strftime("%d.%m.%Y %H:%M (МСК)")
        if "минуту назад" in clean_str:
            dt = now - timedelta(minutes=1)
            return dt.strftime("%d.%m.%Y %H:%M (МСК)")
            
        match_hour = re.search(r'(\d+)\s*час', clean_str)
        if match_hour:
            dt = now - timedelta(hours=int(match_hour.group(1)))
            return dt.strftime("%d.%m.%Y %H:%M (МСК)")
        if "час назад" in clean_str:
            dt = now - timedelta(hours=1)
            return dt.strftime("%d.%m.%Y %H:%M (МСК)")
            
        match_today = re.search(r'сегодня\s*(?:в)?\s*(\d{1,2}):(\d{2})', clean_str)
        if match_today:
            dt = now.replace(hour=int(match_today.group(1)), minute=int(match_today.group(2)), second=0, microsecond=0)
            if (now - dt).total_seconds() > 2700:
                dt += timedelta(hours=1)
            return dt.strftime("%d.%m.%Y %H:%M (МСК)")
            
        match_yesterday = re.search(r'вчера\s*(?:в)?\s*(\d{1,2}):(\d{2})', clean_str)
        if match_yesterday:
            dt = (now - timedelta(days=1)).replace(hour=int(match_yesterday.group(1)), minute=int(match_yesterday.group(2)), second=0, microsecond=0)
            return dt.strftime("%d.%m.%Y %H:%M (МСК)")
            
        return f"{date_str.capitalize()} (обнаружено в {now.strftime('%H:%M МСК')})"
    except Exception:
        return now.strftime("%d.%m.%Y %H:%M (МСК)")

async def fetch_ads_direct(search_url: str) -> list[dict]:
    target_url = format_avito_url(search_url)
    results = []
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }
    for attempt in range(2):
        try:
            async with AsyncSession(impersonate="chrome120") as session:
                resp = await session.get(target_url, headers=headers, timeout=35)
                if resp.status_code != 200:
                    return []
                soup = BeautifulSoup(resp.text, "html.parser")
                cards = soup.select('div[data-marker="item"]')

                for card in cards:
                    item_id = card.get("data-item-id")
                    if not item_id:
                        continue
                    title_elem = card.select_one('[data-marker="item-title"]')
                    title = title_elem.get_text(strip=True) if title_elem else "Без названия"
                    raw_href = title_elem.get("href") if title_elem else ""
                    url = f"https://www.avito.ru{raw_href}" if raw_href.startswith("/") else raw_href
                    price_meta = card.select_one('meta[itemprop="price"]')
                    price = int(price_meta.get("content", 0)) if price_meta and price_meta.get("content") else 0

                    location = "Локация не указана"
                    geo_elem = card.find(attrs={"data-marker": "item-address"}) or card.select_one('[class*="geo-"]')
                    if geo_elem:
                        location = geo_elem.get_text(separator=" ", strip=True)

                    date_elem = card.select_one('[data-marker="item-date"]')
                    raw_date = date_elem.get_text(strip=True) if date_elem else ""
                    ad_date = parse_avito_date(raw_date)

                    results.append({
                        "id": item_id, "title": title, "price": price,
                        "url": url, "location": location, "ad_date": ad_date
                    })
                return results
        except Exception as e:
            logging.error(f"Попытка {attempt + 1}: Ошибка запроса к Авито: {e}")
            await asyncio.sleep(5)
    return results

# ================= 4. TELEGRAM БОТ =================
class SearchForm(StatesGroup):
    waiting_for_url = State()
    waiting_for_price = State()

class DealForm(StatesGroup):
    waiting_for_title = State()
    waiting_for_buy_price = State()
    waiting_for_repair_cost = State()
    waiting_for_sell_price = State()

class EditDealForm(StatesGroup):
    waiting_for_title = State()
    waiting_for_buy_price = State()
    waiting_for_repair_cost = State()
    waiting_for_sell_price = State()

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ Добавить поиск"), KeyboardButton(text="📋 Мои поиски")],
        [KeyboardButton(text="💼 Мои сделки"), KeyboardButton(text="➕ Добавить сделку")]
    ],
    resize_keyboard=True,
    input_field_placeholder="Выберите действие..."
)

@dp.message(CommandStart())
async def cmd_start(msg: Message):
    register_user(msg.from_user.id, msg.from_user.username)
    
    await msg.answer(
        f"👋 <b>FlipRadar • Бот мониторинга и аналитики</b>\n\n"
        f"🆔 Ваш ID: <code>{msg.from_user.id}</code>\n\n"
        f"Используйте кнопки ниже для управления:",
        reply_markup=main_keyboard,
        parse_mode="HTML"
    )

@dp.message(F.text == "➕ Добавить поиск")
async def btn_add(msg: Message, state: FSMContext):
    await msg.answer("🔗 Отправьте <b>ссылку на поиск с Авито</b>:", parse_mode="HTML")
    await state.set_state(SearchForm.waiting_for_url)

@dp.message(SearchForm.waiting_for_url)
async def process_url(msg: Message, state: FSMContext):
    url = msg.text.strip()
    if not url.startswith("http"):
        await msg.answer("Отправьте корректную ссылку, начинающуюся с http:// или https://")
        return
    await state.update_data(url=url)
    await msg.answer("💰 Введите <b>максимальную цену</b> в рублях:", parse_mode="HTML")
    await state.set_state(SearchForm.waiting_for_price)

@dp.message(SearchForm.waiting_for_price)
async def process_price(msg: Message, state: FSMContext):
    digits = re.sub(r"\D", "", msg.text)
    if not digits:
        await msg.answer("Введите число:")
        return
    max_price = int(digits)
    data = await state.get_data()
    add_search(msg.from_user.id, data["url"], max_price)
    await state.clear()
    await msg.answer(f"✅ Поиск успешно добавлен! Макс. цена: {max_price:,} ₽", reply_markup=main_keyboard)

@dp.message(F.text == "📋 Мои поиски")
async def btn_list(msg: Message):
    searches = [s for s in get_all_searches() if s[1] == msg.from_user.id]
    if not searches:
        await msg.answer("У вас нет активных поисков.")
        return
    
    await msg.answer(f"📋 Ваши активные поиски ({len(searches)}):")
    for s_id, _, url, max_price in searches:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del_search_{s_id}")]])
        await msg.answer(f"🔍 Поиск #{s_id}\n💰 Лимит: до {max_price:,} ₽\n🔗 <a href='{url}'>Ссылка на Авито</a>", reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data.startswith("del_search_"))
async def delete_search_callback(callback: CallbackQuery):
    delete_search(int(callback.data.split("_")[2]))
    await callback.message.edit_text("❌ Поиск удален.")
    await callback.answer()

# ================= УЧЕТ СДЕЛОК =================
@dp.message(F.text == "➕ Добавить сделку")
async def btn_add_deal(msg: Message, state: FSMContext):
    await msg.answer("📦 Введите <b>название товара</b> (например, <i>iPhone 14 Pro 128GB</i>):", parse_mode="HTML")
    await state.set_state(DealForm.waiting_for_title)

@dp.message(DealForm.waiting_for_title)
async def process_deal_title(msg: Message, state: FSMContext):
    await state.update_data(title=msg.text.strip())
    await msg.answer("💰 Введите <b>цену покупки</b> (в рублях):", parse_mode="HTML")
    await state.set_state(DealForm.waiting_for_buy_price)

@dp.message(DealForm.waiting_for_buy_price)
async def process_deal_buy(msg: Message, state: FSMContext):
    digits = re.sub(r"\D", "", msg.text)
    if not digits:
        await msg.answer("Введите число:")
        return
    await state.update_data(buy_price=int(digits))
    await msg.answer("🛠 Введите <b>расходы на ремонт / доставку</b> (в рублях, если расходов не было — введите 0):", parse_mode="HTML")
    await state.set_state(DealForm.waiting_for_repair_cost)

@dp.message(DealForm.waiting_for_repair_cost)
async def process_deal_repair(msg: Message, state: FSMContext):
    digits = re.sub(r"\D", "", msg.text)
    if not digits:
        await msg.answer("Введите число:")
        return
    await state.update_data(repair_cost=int(digits))
    await msg.answer("🏷 Введите <b>плановую или реальную цену продажи</b> (в рублях):", parse_mode="HTML")
    await state.set_state(DealForm.waiting_for_sell_price)

@dp.message(DealForm.waiting_for_sell_price)
async def process_deal_sell(msg: Message, state: FSMContext):
    digits = re.sub(r"\D", "", msg.text)
    if not digits:
        await msg.answer("Введите число:")
        return
    sell_price = int(digits)
    data = await state.get_data()
    
    add_deal(msg.from_user.id, data["title"], data["buy_price"], data["repair_cost"], sell_price)
    await state.clear()
    
    profit = sell_price - (data["buy_price"] + data["repair_cost"])
    await msg.answer(
        f"✅ <b>Сделка успешно сохранена!</b>\n"
        f"📱 Товар: {data['title']}\n"
        f"💵 Чистая прибыль: <b>{profit:,} ₽</b>",
        reply_markup=main_keyboard,
        parse_mode="HTML"
    )

@dp.message(F.text == "💼 Мои сделки")
async def btn_my_deals(msg: Message):
    deals = get_user_deals(msg.from_user.id)
    total_deals, total_profit = get_user_financial_stats(msg.from_user.id)
    
    if not deals:
        await msg.answer("💼 У вас пока нет сохраненных сделок. Нажмите «➕ Добавить сделку», чтобы записать первую!")
        return

    await msg.answer(
        f"📊 <b>Ваша бизнес-статистика:</b>\n"
        f"• Всего сделок: <b>{total_deals}</b>\n"
        f"• Общая чистая прибыль: <b>{total_profit:,} ₽</b>\n\n"
        f"📜 <b>Список ваших сделок:</b>",
        parse_mode="HTML"
    )
    
    for d_id, title, buy, repair, sell, profit, date in deals[:15]:
        profit_emoji = "🟢" if profit >= 0 else "🔴"
        text = (
            f"🆔 #{d_id} <b>{title}</b> ({date})\n"
            f"   Покупка: {buy:,} ₽ | Расходы: {repair:,} ₽\n"
            f"   Продажа: {sell:,} ₽ | Прибыль: {profit_emoji} <b>{profit:,} ₽</b>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"edit_deal_{d_id}"),
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del_deal_{d_id}")
            ]
        ])
        await msg.answer(text, reply_markup=kb, parse_mode="HTML")

# --- Удаление и Редактирование сделки через Callback ---
@dp.callback_query(F.data.startswith("del_deal_"))
async def delete_deal_callback(callback: CallbackQuery):
    deal_id = int(callback.data.split("_")[2])
    delete_deal(deal_id)
    await callback.message.edit_text("❌ Сделка успешно удалена.")
    await callback.answer()

@dp.callback_query(F.data.startswith("edit_deal_"))
async def edit_deal_callback(callback: CallbackQuery, state: FSMContext):
    deal_id = int(callback.data.split("_")[2])
    deal = get_deal_by_id(deal_id)
    if not deal:
        await callback.answer("Сделка не найдена!", show_alert=True)
        return
    
    await state.update_data(editing_deal_id=deal_id)
    await callback.message.answer(f"✏️ Редактирование сделки #{deal_id} ({deal[2]})\n\nВведите <b>новое название товара</b>:", parse_mode="HTML")
    await state.set_state(EditDealForm.waiting_for_title)
    await callback.answer()

@dp.message(EditDealForm.waiting_for_title)
async def process_edit_title(msg: Message, state: FSMContext):
    await state.update_data(title=msg.text.strip())
    await msg.answer("💰 Введите <b>новую цену покупки</b> (в рублях):", parse_mode="HTML")
    await state.set_state(EditDealForm.waiting_for_buy_price)

@dp.message(EditDealForm.waiting_for_buy_price)
async def process_edit_buy(msg: Message, state: FSMContext):
    digits = re.sub(r"\D", "", msg.text)
    if not digits:
        await msg.answer("Введите число:")
        return
    await state.update_data(buy_price=int(digits))
    await msg.answer("🛠 Введите <b>новые расходы на ремонт / доставку</b> (в рублях):", parse_mode="HTML")
    await state.set_state(EditDealForm.waiting_for_repair_cost)

@dp.message(EditDealForm.waiting_for_repair_cost)
async def process_edit_repair(msg: Message, state: FSMContext):
    digits = re.sub(r"\D", "", msg.text)
    if not digits:
        await msg.answer("Введите число:")
        return
    await state.update_data(repair_cost=int(digits))
    await msg.answer("🏷 Введите <b>новую цену продажи</b> (в рублях):", parse_mode="HTML")
    await state.set_state(EditDealForm.waiting_for_sell_price)

@dp.message(EditDealForm.waiting_for_sell_price)
async def process_edit_sell(msg: Message, state: FSMContext):
    digits = re.sub(r"\D", "", msg.text)
    if not digits:
        await msg.answer("Введите число:")
        return
    sell_price = int(digits)
    data = await state.get_data()
    deal_id = data["editing_deal_id"]
    
    update_deal(deal_id, data["title"], data["buy_price"], data["repair_cost"], sell_price)
    await state.clear()
    
    profit = sell_price - (data["buy_price"] + data["repair_cost"])
    await msg.answer(
        f"✅ <b>Сделка #{deal_id} успешно обновлена!</b>\n"
        f"📱 Товар: {data['title']}\n"
        f"💵 Новая чистая прибыль: <b>{profit:,} ₽</b>",
        reply_markup=main_keyboard,
        parse_mode="HTML"
    )

# ================= 5. ФОНОВЫЙ МОНИТОРИНГ =================
async def monitor_task():
    while True:
        try:
            for s_id, user_id, url, max_price in get_all_searches():
                items = await fetch_ads_direct(url)
                for item in items:
                    if not is_item_seen(s_id, item["id"]):
                        mark_item_as_seen(s_id, item["id"])
                        if 0 < item["price"] <= max_price:
                            safe_title = item['title'].replace('<', '&lt;').replace('>', '&gt;')
                            
                            estimated_sell = int(item['price'] * 1.15)
                            estimated_profit = estimated_sell - item['price']
                            
                            text = (
                                f"📌 <b>{safe_title}</b>\n"
                                f"💰 Цена: <b>{item['price']:,} ₽</b>\n"
                                f"🌍 {item['location']}\n"
                                f"🕒 Опубликовано: {item['ad_date']}\n\n"
                                f"📈 <b>Аналитика:</b>\n"
                                f"• Плановая цена перепродажи: ~<b>{estimated_sell:,} ₽</b>\n"
                                f"• Ориентир прибыли: ~<b>{estimated_profit:,} ₽</b>\n\n"
                                f"🔗 <a href='{item['url']}'>Открыть на Авито</a>"
                            )
                            try:
                                await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
                            except Exception:
                                pass
                await asyncio.sleep(4)
        except Exception as e:
            logging.error(f"Ошибка мониторинга: {e}")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)

# --- УПРАВЛЕНИЕ БАЗОЙ ДАННЫХ И АДМИНКА ЧЕРЕЗ TELEGRAM ---
@dp.message(Command("stats"))
async def cmd_stats(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM users")
    users_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM searches")
    searches_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM deals")
    deals_count = cursor.fetchone()[0]
    conn.close()

    await msg.answer(
        f"📊 <b>Статистика базы:</b>\n\n"
        f"👥 Всего пользователей: <b>{users_count}</b>\n"
        f"🔍 Активных поисков: <b>{searches_count}</b>\n"
        f"💼 Записанных сделок: <b>{deals_count}</b>\n\n"
        f"📥 Скачать базу для DBeaver: /get_db",
        parse_mode="HTML"
    )

@dp.message(Command("get_db"))
async def cmd_get_db(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    if os.path.exists(DB_PATH):
        document = FSInputFile(DB_PATH)
        await msg.answer_document(document, caption="📦 Вот актуальный файл базы данных с хостинга!")
    else:
        await msg.answer("❌ Файл базы данных еще не найден.")

@dp.message(F.document)
async def upload_db_handler(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    if msg.document.file_name == "bot_database.db":
        os.makedirs("data", exist_ok=True)
        file = await msg.bot.get_file(msg.document.file_id)
        await msg.bot.download_file(file.file_path, DB_PATH)
        await msg.answer("✅ База данных на хостинге успешно обновлена!")

# ================= 6. ЗАПУСК =================
async def main():
    init_db()
    asyncio.create_task(monitor_task())
    logging.info("Бот запущен без подписок и лимитов...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())