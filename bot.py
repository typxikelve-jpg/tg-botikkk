import logging
import asyncio
import json
import os
from datetime import datetime, timedelta
import pytz

from fastapi import FastAPI, Request  # ✅ теперь работает
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage


# -------------------------------
# Настройки
# -------------------------------
API_TOKEN = "8370797958:AAE5eXZOq66IhaK3D9Y5tU9ad-2AQQPuf3s"   # <-- токен бота
ADMINS = [7721203223, 7565250716, 8048631870]                 # <-- список айди админов
app = FastAPI()
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = os.getenv("https://tg-botikkk.vercel.app/webhook")

PAYMENT_DETAILS = """
💳 Реквизиты для перевода:
✅ Сбербанк: 
Имя получателя: Иван Иванов
❗ После перевода отправьте ФИО отправителя и способ оплаты.
"""

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# -------------------------------
# Каталог (JSON)
# -------------------------------
CATALOG_FILE = "catalog.json"
if not os.path.exists(CATALOG_FILE):
    with open(CATALOG_FILE, "w", encoding="utf-8") as f:
        json.dump([], f, ensure_ascii=False, indent=2)

def load_catalog():
    with open(CATALOG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_catalog():
    with open(CATALOG_FILE, "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=2)

async def add_option_callback(callback: types.CallbackQuery, state: FSMContext):
    try:
        # Распаковываем cat_id и idx
        cat_id, idx = map(int, callback.data.split("_")[2:])
    except ValueError:
        await callback.answer("Ошибка в данных кнопки!", show_alert=True)
        return

    # Получаем текущие данные из состояния
    data = await state.get_data()
    options = data.get(f"category_{cat_id}_options", [])

    # Проверяем, чтобы не выйти за границы массива
    if idx >= len(options):
        await callback.answer("Неверный индекс!", show_alert=True)
        return

    # Пример добавления нового вкуса
    new_option = f"Новый вкус {len(options)+1}"
    options.append(new_option)

    # Сохраняем обратно
    await state.update_data({f"category_{cat_id}_options": options})

    # Обновляем сообщение с кнопками (если нужно)
    await callback.answer(f"Добавлен новый вкус: {new_option}")

def add_option_to_item(catalog, cat_id: int, new_option: str, option_value: int = 0):
    """
    Добавляет новый вкус в последний товар категории
    """
    if not (0 <= cat_id < len(catalog)):
        return "❌ Категория не найдена"

    if not catalog[cat_id]["items"]:
        return "❌ В категории нет товаров"

    # Берём последний товар
    item = catalog[cat_id]["items"][-1]

    if "options" not in item:
        item["options"] = []

    if new_option in item["options"]:
        return "❌ Такой вкус уже есть"

    item["options"].append(new_option)
    if "options_stock" not in item:
        item["options_stock"] = {}
    item["options_stock"][new_option] = option_value
    save_catalog()
    return f"✅ Вкус «{new_option}» добавлен"


catalog = load_catalog()

USERS_FILE = "users.json"
if not os.path.exists(USERS_FILE):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump({}, f, ensure_ascii=False, indent=2)

def load_users():
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_users():
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)

users = load_users()

def get_status(user_id: int) -> str:
    return users.get(str(user_id), {}).get("status", "none")

def set_status(user_id: int, status: str, username: str = None):
    users[str(user_id)] = {
        "status": status,
        "username": username or users.get(str(user_id), {}).get("username", "")
    }
    save_users()

def add_stock_to_item(catalog, cat_id: int, idx: int, amount: int):
    """
    Добавляет остатки к товару.
    Если товар без вкусов — редактируем общий stock.
    Если с вкусами — редактируем options_stock (необязательно).
    """
    if not (0 <= cat_id < len(catalog)):
        return "❌ Категория не найдена"
    if not (0 <= idx < len(catalog[cat_id]["items"])):
        return "❌ Товар не найден"

    item = catalog[cat_id]["items"][idx]

    # Если у товара есть options_stock — увеличиваем остатки всех вкусов
    if "options_stock" in item and item["options_stock"]:
        for opt in item["options_stock"]:
            item["options_stock"][opt] += amount
        save_catalog()
        return f"✅ Остатки всех вкусов увеличены на {amount}"
    else:
        # Для товаров без вкусов используем общий stock
        if "stock" not in item:
            item["stock"] = 0
        item["stock"] += amount
        save_catalog()
        return f"✅ Остаток товара увеличен на {amount}"

# -------------------------------
# Данные (в памяти)
# -------------------------------
carts: dict[int, list] = {}
orders: dict[int, dict] = {}
booked_slots: set[str] = set()

# -------------------------------
# FSM состояния
# -------------------------------
class EditCategory(StatesGroup):
    waiting_new_name = State()

class EditProduct(StatesGroup):
    waiting_price = State()
    waiting_name = State()
    waiting_new_category = State()
    waiting_options = State()
    waiting_remove_option = State()  # новое состояние для удаления вкуса
    waiting_new_option = State()  # новое состояние для добавления вкус

class OrderProcess(StatesGroup):
    choosing_type = State()
    choosing_date = State()
    choosing_time = State()
    entering_name = State()
    entering_payment = State()

# -------------------------------
# Вспомогательные функции
# -------------------------------
def is_admin(user_id: int) -> bool:
    return user_id in ADMINS   # ✅ исправлено


def get_cart(user_id: int):
    """Возвращает корзину пользователя (список словарей)"""
    return carts.setdefault(user_id, [])

def add_to_cart(user_id: int, item: dict):
    """
    Добавляем товар в корзину.
    item должен содержать:
        - name
        - price
        - cat_id
        - idx
        - option (если есть)
    """
    carts.setdefault(user_id, []).append(item)

def clear_cart(user_id: int):
    """Очищаем корзину пользователя"""
    carts[user_id] = []

def format_cart(user_id: int) -> str:
    cart = get_cart(user_id)
    if not cart:
        return "Ваша корзина пуста."
    text = "🛒 Ваша корзина:\n\n"
    total = 0
    for i, item in enumerate(cart, start=1):
        line = f"{i}. {item['name']} - {item['price']}₽"
        if "option" in item:
            line += f" ({item['option']})"
        text += line + "\n"
        total += item["price"]
    text += f"\n💰 Итого: {total}₽"
    return text

MSK = pytz.timezone("Europe/Moscow")

def generate_time_slots() -> list[str]:
    slots = []
    start_time = datetime.strptime("12:00", "%H:%M")
    end_time = datetime.strptime("22:00", "%H:%M")
    while start_time <= end_time:
        slots.append(start_time.strftime("%H:%M"))
        start_time += timedelta(minutes=30)
    return slots

async def send_admin_catalog_menu(chat_id: int):
    kb = InlineKeyboardBuilder()
    if not catalog:
        kb.button(text="➕ Добавить категорию", callback_data="add_category")
        kb.button(text="⬅️ В админ-панель", callback_data="admin_root")
        kb.adjust(1)
        await bot.send_message(chat_id, "Категорий пока нет. Добавьте первую:", reply_markup=kb.as_markup())
        return

    for i, category in enumerate(catalog):
        kb.button(text=f"📦 {category['name']}", callback_data=f"admin_cat_{i}")
    kb.button(text="➕ Добавить категорию", callback_data="add_category")
    kb.button(text="⬅️ В админ-панель", callback_data="admin_root")
    kb.adjust(1)
    await bot.send_message(chat_id, "Категории:", reply_markup=kb.as_markup())

async def send_admin_category_menu(chat_id: int, cat_id: int):
    if not (0 <= cat_id < len(catalog)):
        await bot.send_message(chat_id, "Категория не найдена")
        return
    category = catalog[cat_id]
    kb = InlineKeyboardBuilder()
    if category["items"]:
        for idx, item in enumerate(category["items"]):
            kb.button(text=f"{item['name']} — {item['price']}₽", callback_data=f"admin_item_{cat_id}_{idx}")
    else:
        kb.button(text="(товаров нет)", callback_data="noop")
    kb.button(text="➕ Добавить товар", callback_data=f"admin_add_{cat_id}")
    kb.button(text="🗑 Удалить категорию", callback_data=f"del_cat_{cat_id}")
    kb.button(text="⬅️ Назад к категориям", callback_data="admin_catalog")
    kb.adjust(1)
    await bot.send_message(chat_id, f"Категория: {category['name']}", reply_markup=kb.as_markup())

async def send_item_card(chat_id: int, cat_id: int, idx: int):
    if not (0 <= cat_id < len(catalog)) or not (0 <= idx < len(catalog[cat_id]["items"])):
        await bot.send_message(chat_id, "❌ Товар не найден")
        return
    item = catalog[cat_id]["items"][idx]
    options_line = ""
    if "options" in item and item["options"]:
        options_line = "\n• Вкусы: " + ", ".join(item["options"])
    text = f"Товар:\n• Название: {item['name']}\n• Цена: {item['price']}₽{options_line}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить название", callback_data=f"editname_{cat_id}_{idx}")],
        [InlineKeyboardButton(text="💵 Изменить цену", callback_data=f"editprice_{cat_id}_{idx}")],
        [InlineKeyboardButton(text="❌ Убрать вкус", callback_data=f"removeopt_{cat_id}_{idx}")],
        [InlineKeyboardButton(text="➕ Добавить вкус", callback_data=f"add_option_{cat_id}_{idx}")],
        [InlineKeyboardButton(text="🛠 Редактировать остатки вкусов", callback_data=f"editflavor_{cat_id}_{idx}")],
        [InlineKeyboardButton(text="🗑 Удалить товар", callback_data=f"del_item_{cat_id}_{idx}")],
        [InlineKeyboardButton(text="⬅️ Назад к категории", callback_data=f"admin_cat_{cat_id}")]

    ])

    await bot.send_message(chat_id, text, reply_markup=kb)

# -------------------------------
# Главное меню (клиент)
# -------------------------------
main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Каталог")],
        [KeyboardButton(text="Корзина"), KeyboardButton(text="Очистить корзину")],
        [KeyboardButton(text="Оформить заказ")]
    ],
    resize_keyboard=True
)

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Привет! 👋 Выберите действие:", reply_markup=main_kb)


def remove_out_of_stock_from_carts():
    """
    Убирает из корзин всех пользователей товары, которых нет в наличии.
    """
    for user_id, cart in carts.items():
        new_cart = []
        for item in cart:
            cat_id = item.get("cat_id")
            idx = item.get("idx")
            option = item.get("option")

            # Проверяем, есть ли категория и товар
            if cat_id is None or idx is None or cat_id >= len(catalog) or idx >= len(catalog[cat_id]["items"]):
                continue

            catalog_item = catalog[cat_id]["items"][idx]

            # Проверяем наличие опции (вкуса) если она есть
            if option:
                available = catalog_item.get("options_stock", {}).get(option, 0)
                if available > 0:
                    new_cart.append(item)
            else:
                # Товар без опций — считаем, что его доступность определяется наличием item["options_stock"]?
                # Если нет stock — оставляем
                new_cart.append(item)

        carts[user_id] = new_cart


# -------------------------------
# Каталог (клиент)
# -------------------------------
@dp.message(F.text == "Каталог")
async def show_catalog(message: types.Message):
    if not catalog:
        await message.answer("Каталог пуст. Обратитесь к администратору.")
        return
    kb = InlineKeyboardBuilder()
    for i, category in enumerate(catalog):
        kb.button(text=category["name"], callback_data=f"cat_{i}")
    kb.adjust(1)
    await message.answer("📂 Выберите категорию:", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("cat_"))
async def show_items(callback: types.CallbackQuery):
    cat_id = int(callback.data.split("_")[1])
    if not (0 <= cat_id < len(catalog)):
        await callback.answer("Категория не найдена", show_alert=True)
        return
    category = catalog[cat_id]
    kb = InlineKeyboardBuilder()

    for idx, item in enumerate(category["items"]):
        # Формируем строку с остатками вкусов
        options_line = ""
        if "options_stock" in item and item["options_stock"]:
            options_line = " | ".join([f"{opt}: {qty}" for opt, qty in item["options_stock"].items()])
            options_line = f" ({options_line})"
        elif "options" in item and item["options"]:
            options_line = f" ({', '.join(item['options'])})"
        elif "stock" in item:  # для товаров без вкусов
            options_line = f" ({item['stock']} шт.)"

        kb.button(
            text=f"{item['name']} - {item['price']}₽{options_line}",
            callback_data=f"item_{cat_id}_{idx}"
        )

    kb.button(text="⬅️ Назад", callback_data="back_catalog")
    kb.adjust(1)
    await callback.message.edit_text(f"📦 {category['name']}:", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("item_"))
async def choose_item(callback: types.CallbackQuery):
    _, cat_id, idx = callback.data.split("_")
    cat_id, idx = int(cat_id), int(idx)
    if not (0 <= cat_id < len(catalog)) or not (0 <= idx < len(catalog[cat_id]["items"])):
        await callback.answer("Товар не найден", show_alert=True)
        return
    item = catalog[cat_id]["items"][idx]

    # Формируем текст с остатками вкусов / количества
    options_line = ""
    if "options_stock" in item and item["options_stock"]:
        options_line = "\n• Вкусы и остатки:\n"
        options_line += "\n".join([f"{opt}: {qty} шт." for opt, qty in item["options_stock"].items()])
    elif "options" in item and item["options"]:
        options_line = "\n• Вкусы: " + ", ".join(item["options"])
    elif "stock" in item:  # для товаров без вкусов
        options_line = f"\n• Остаток: {item['stock']} шт."

    # Если у товара есть опции (вкусы) — предложим выбрать
    if "options" in item and item["options"]:
        kb = InlineKeyboardBuilder()
        for opt in item["options"]:
            kb.button(text=opt, callback_data=f"chooseopt_{cat_id}_{idx}_{opt}")
        kb.button(text="⬅️ Назад", callback_data=f"cat_{cat_id}")
        kb.adjust(1)
        await callback.message.edit_text(
            f"🍭 {item['name']} ({item['price']}₽){options_line}\n\nВыберите вкус:",
            reply_markup=kb.as_markup()
        )
    else:
        # ======= товары без вкусов =======
        stock = item.get("stock", 0)
        if stock <= 0:
            await callback.answer("❌ Товар закончился", show_alert=True)
            return

        # считаем сколько уже в корзине
        user_cart = get_cart(callback.from_user.id)
        in_cart_count = sum(1 for c in user_cart if c.get("name") == item["name"])

        if in_cart_count >= stock:
            await callback.answer(f"❌ Доступно только {stock} шт.", show_alert=True)
            return

        add_to_cart(callback.from_user.id, {
            "name": item["name"],
            "price": item["price"],
            "cat_id": cat_id,
            "idx": idx
        })
        await callback.answer(f"✅ Добавлено в корзину\nОсталось: {stock - in_cart_count - 1}")
@dp.callback_query(F.data.startswith("chooseopt_"))
async def choose_option(callback: types.CallbackQuery):
    _, cat_id, idx, opt = callback.data.split("_", 3)
    cat_id, idx = int(cat_id), int(idx)
    item = catalog[cat_id]["items"][idx]

    # Получаем текущую корзину пользователя
    user_id = callback.from_user.id
    cart = get_cart(user_id)

    # Считаем, сколько уже есть этого вкуса в корзине
    in_cart_count = sum(
        1 for c in cart
        if c.get("cat_id") == cat_id and c.get("idx") == idx and c.get("option") == opt
    )

    # Проверяем остаток с учётом корзины
    available = item.get("options_stock", {}).get(opt, 0)
    if in_cart_count >= available:
        await callback.answer(f"Нельзя добавить больше '{opt}', всего доступно {available}❌", show_alert=True)
        return

    # Добавляем товар в корзину
    add_to_cart(user_id, {
        "name": item["name"],
        "price": item["price"],
        "cat_id": cat_id,
        "idx": idx,
        "option": opt
    })

    await callback.answer(f"Добавлено в корзину ✅ ({opt})")



@dp.callback_query(F.data == "back_catalog")
async def back_to_catalog(callback: types.CallbackQuery):
    kb = InlineKeyboardBuilder()
    for i, category in enumerate(catalog):
        kb.button(text=category["name"], callback_data=f"cat_{i}")
    kb.adjust(1)
    await callback.message.edit_text("📂 Выберите категорию:", reply_markup=kb.as_markup())

@dp.message(F.text == "Корзина")
async def show_cart(message: types.Message):
    await message.answer(format_cart(message.from_user.id))

@dp.message(F.text == "Очистить корзину")
async def clear_user_cart(message: types.Message):
    clear_cart(message.from_user.id)
    await message.answer("Корзина очищена 🗑️")

# -------------------------------
# Оформление заказа (финальная версия)
# -------------------------------

# -------------------------------
# Оформление заказа
# -------------------------------
@dp.message(F.text == "Оформить заказ")
async def checkout(message: types.Message, state: FSMContext):
    cart = get_cart(message.from_user.id)
    if not cart:
        await message.answer("Ваша корзина пуста ❌")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏃 Самовывоз сегодня", callback_data="order_pickup")],
        [InlineKeyboardButton(text="📅 Бронь самовывоза", callback_data="order_reserve")],
        [InlineKeyboardButton(text="🚚 Доставка", callback_data="order_delivery")]
    ])
    await message.answer("Выберите способ получения:", reply_markup=kb)
    await state.set_state(OrderProcess.choosing_type)


@dp.callback_query(F.data.startswith("order_"))
async def choose_order_type(callback: types.CallbackQuery, state: FSMContext):
    order_type = callback.data.split("_")[1]  # pickup | reserve | delivery
    await state.update_data(order_type=order_type)

    if order_type == "pickup":
        today = datetime.now(MSK).date()
        slots = generate_time_slots()
        now = datetime.now(MSK)

        available = []
        for slot in slots:
            slot_time = datetime.strptime(slot, "%H:%M").time()
            if slot_time <= now.time():
                continue
            key = f"{today} {slot}"
            if key not in booked_slots:
                available.append(slot)

        if not available:
            await callback.answer("Сегодня нет доступных слотов ❌", show_alert=True)
            return

        kb = InlineKeyboardBuilder()
        for slot in available:
            kb.button(text=slot, callback_data=f"pickup_time_{slot}")
        kb.adjust(4)

        await callback.message.edit_text("⏰ Выберите время для самовывоза:", reply_markup=kb.as_markup())
        return

    # Бронь и доставка — календарь на 30 дней
    kb = InlineKeyboardBuilder()
    today = datetime.now(MSK).date()
    for i in range(0, 30):
        date = today + timedelta(days=i)
        kb.button(text=date.strftime("%d.%m"), callback_data=f"date_{date}")
    kb.adjust(4)
    await callback.message.edit_text("📅 Выберите дату:", reply_markup=kb.as_markup())
    await state.set_state(OrderProcess.choosing_date)


@dp.callback_query(F.data.startswith("pickup_time_"))
async def pickup_time(callback: types.CallbackQuery, state: FSMContext):
    time_slot = callback.data.split("_")[2]
    today = datetime.now(MSK).date()
    key = f"{today} {time_slot}"

    if key in booked_slots:
        await callback.answer("Это время уже занято ❌", show_alert=True)
        return

    booked_slots.add(key)

    user_id = callback.from_user.id
    user_cart = get_cart(user_id)  # Получаем корзину

    # Уменьшаем остатки вкусов
    for cart_item in user_cart:
        cat_id = cart_item.get("cat_id")
        idx = cart_item.get("idx")
        option = cart_item.get("option")

        if cat_id is not None and idx is not None and option:
            item = catalog[cat_id]["items"][idx]
            if "options_stock" in item and option in item["options_stock"]:
                item["options_stock"][option] -= 1
                if item["options_stock"][option] < 0:
                    item["options_stock"][option] = 0
                # Если нет опций — уменьшаем stock
                elif "stock" in item:
                    item["stock"] -= 1
                    if item["stock"] < 0:
                        item["stock"] = 0


    remove_out_of_stock_from_carts()

    # Сохраняем каталог после изменения stock
    save_catalog()

    # Создаём заказ
    order_id = len(orders) + 1
    orders[order_id] = {
        "user_id": user_id,
        "username": f"@{callback.from_user.username}" if callback.from_user.username else callback.from_user.full_name,
        "items": user_cart,
        "type": "Самовывоз сегодня",
        "date": str(today),
        "time": time_slot,
        "status": "new"
    }

    # Очищаем корзину
    clear_cart(user_id)

    # Сообщение клиенту
    await callback.message.edit_text(f"✅ Ваш заказ отправлен админу. Время: {time_slot}")

    # Формируем сообщение для админов
    user = callback.from_user
    text = f"Новый заказ #{order_id}\nТип: Самовывоз сегодня\nДата: {today}\nВремя: {time_slot}\n👤 Пользователь: @{user.username if user.username else user.full_name}\n\n📦 Состав заказа:\n"
    for i, item in enumerate(orders[order_id]["items"], start=1):
        line = f"{i}. {item['name']} - {item['price']}₽"
        if "option" in item:
            line += f" ({item['option']})"
        text += line + "\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принять", callback_data=f"accept_{order_id}"),
         InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{order_id}")]
    ])

    for admin_id in ADMINS:
        await bot.send_message(admin_id, text, reply_markup=kb)



@dp.callback_query(F.data.startswith("date_"))
async def choose_date(callback: types.CallbackQuery, state: FSMContext):
    date_str = callback.data.split("_")[1]
    await state.update_data(date=date_str)

    slots = generate_time_slots()
    now = datetime.now(MSK)
    selected_date = datetime.strptime(date_str, "%Y-%m-%d").date()

    available = []
    for slot in slots:
        if selected_date == now.date() and datetime.strptime(slot, "%H:%M").time() <= now.time():
            continue
        key = f"{date_str} {slot}"
        if key not in booked_slots:
            available.append(slot)

    if not available:
        await callback.answer("Нет доступных слотов ❌", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    for slot in available:
        kb.button(text=slot, callback_data=f"time_{slot}")
    kb.adjust(4)

    await callback.message.edit_text("⏰ Выберите время:", reply_markup=kb.as_markup())
    await state.set_state(OrderProcess.choosing_time)


@dp.callback_query(F.data.startswith("time_"))
async def choose_time(callback: types.CallbackQuery, state: FSMContext):
    time_slot = callback.data.split("_")[1]

    data = await state.get_data()
    date_str = data.get("date")
    order_type = data.get("order_type")

    if not date_str or not order_type:
        await callback.answer("Ошибка выбора даты. Начните заново.", show_alert=True)
        await state.clear()
        return

    key = f"{date_str} {time_slot}"
    if key in booked_slots:
        await callback.answer("Это время уже занято ❌", show_alert=True)
        return

    booked_slots.add(key)
    await state.update_data(time=time_slot)

    await callback.message.edit_text(PAYMENT_DETAILS)
    await asyncio.sleep(0.5)
    await bot.send_message(callback.from_user.id, "Введите ФИО отправителя платежа:")
    await state.set_state(OrderProcess.entering_name)


# -------------------------------
# Получаем имя отправителя
# -------------------------------
@dp.message(OrderProcess.entering_name)
async def get_name(message: types.Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("❌ Введите корректное ФИО.")
        return
    await state.update_data(name=name)
    await message.answer("Введите адрес доставки (или напишите «0» для брони):")
    await state.set_state(OrderProcess.entering_payment)


# -------------------------------
# Получаем адрес/оплату и уменьшаем остатки
# -------------------------------
@dp.message(OrderProcess.entering_payment)
async def get_payment_info(message: types.Message, state: FSMContext):
    payment = message.text.strip()
    data = await state.get_data()
    user_id = message.from_user.id

    # Берём корзину до очистки
    user_cart = get_cart(user_id)

    # Уменьшаем количество выбранных опций
    for cart_item in user_cart:
        cat_id = cart_item.get("cat_id")
        idx = cart_item.get("idx")
        option = cart_item.get("option")

        if cat_id is not None and idx is not None:
            item = catalog[cat_id]["items"][idx]

            # Инициализируем options_stock, если его нет
            if "options" in item and "options_stock" not in item:
                item["options_stock"] = {opt: 10 for opt in item["options"]}  # стартовое количество

            # Уменьшаем остаток выбранного вкуса
            if option and option in item.get("options_stock", {}):
                item["options_stock"][option] -= 1
                if item["options_stock"][option] < 0:
                    item["options_stock"][option] = 0


    remove_out_of_stock_from_carts()

    # Сохраняем изменения в каталоге
    save_catalog()

    # Создаём заказ после уменьшения stock
    order_id = len(orders) + 1
    orders[order_id] = {
        "user_id": user_id,
        "username": f"@{message.from_user.username}" if message.from_user.username else message.from_user.full_name,
        "items": user_cart,
        "type": "Бронь самовывоза" if data.get("order_type") == "reserve" else "Доставка",
        "date": data.get("date"),
        "time": data.get("time"),
        "name": data.get("name"),
        "payment": payment,
        "status": "new"
    }

    # Очищаем корзину после оформления
    clear_cart(user_id)
    await state.clear()

    await message.answer("✅ Ваш заказ отправлен на проверку админу.")

    # Отправка заказа админам
    user = message.from_user
    text = (
        f"Новый заказ #{order_id}\n"
        f"Тип: {orders[order_id]['type']}\n"
        f"Дата: {data.get('date','—')}\n"
        f"Время: {data.get('time','—')}\n"
        f"ФИО: {data.get('name','—')}\n"
        f"Адрес/Примечание: {payment}\n"
        f"👤 Пользователь: @{user.username if user.username else user.full_name}\n\n"
        f"📦 Состав заказа:\n"
    )
    for i, item in enumerate(orders[order_id]["items"], start=1):
        line = f"{i}. {item['name']} - {item['price']}₽"
        if "option" in item:
            line += f" ({item['option']})"
        text += line + "\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Принять", callback_data=f"accept_{order_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{order_id}")
    ]])
    for admin_id in ADMINS:
        await bot.send_message(admin_id, text, reply_markup=kb)


# -------------------------------
# Управление заказами (админ)
# -------------------------------
# -------------------------------
# Управление заказами (админ)
# -------------------------------
@dp.callback_query(F.data == "admin_orders")
async def admin_orders(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    if not orders:
        await callback.message.edit_text("📦 Нет заказов.")
        return

    kb = InlineKeyboardBuilder()
    for order_id, order in orders.items():
        kb.button(
            text=f"Заказ #{order_id} — {order['status']}",
            callback_data=f"view_order_{order_id}"
        )

    kb.button(text="⬅️ Назад в админ-панель", callback_data="admin_root")
    kb.adjust(1)
    await callback.message.edit_text("📦 Список заказов:", reply_markup=kb.as_markup())

# -------------------------------
# Просмотр конкретного заказа
# -------------------------------
@dp.callback_query(F.data.startswith("view_order_"))
async def view_order(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    order_id = int(callback.data.split("_")[2])
    order = orders.get(order_id)
    if not order:
        await callback.answer("Заказ не найден", show_alert=True)
        return

    text = (
        f"📦 Заказ #{order_id}\n"
        f"👤 Клиент: {order['username']}\n"
        f"🆔 ID: {order['user_id']}\n"
        f"Тип: {order['type']}\n"
        f"Дата: {order.get('date', '—')}\n"
        f"Время: {order.get('time', '—')}\n"
        f"Статус: {order['status']}\n\n"
        f"📦 Состав заказа:\n"
    )

    for i, item in enumerate(order["items"], start=1):
        line = f"{i}. {item['name']} - {item['price']}₽"
        if "option" in item:
            line += f" ({item['option']})"
        text += line + "\n"

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Принять", callback_data=f"accept_{order_id}")
    kb.button(text="❌ Отклонить", callback_data=f"reject_{order_id}")
    kb.button(text="⬅️ Назад к заказам", callback_data="admin_orders")
    kb.adjust(2)  # две кнопки в ряду
    await callback.message.edit_text(text, reply_markup=kb.as_markup())

# -------------------------------
# Принять заказ
# -------------------------------
@dp.callback_query(F.data.startswith("accept_"))
async def accept_order(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    order_id = int(callback.data.split("_")[1])
    order = orders.get(order_id)
    if not order or order["status"] != "new":
        await callback.answer("Заказ уже обработан ❌", show_alert=True)
        return

    order["status"] = "accepted"
    await callback.message.edit_text(f"Заказ #{order_id} принят ✅")
    await bot.send_message(order["user_id"], f"Ваш заказ #{order_id} принят ✅")

# -------------------------------
# Отклонить заказ
# -------------------------------
@dp.callback_query(F.data.startswith("reject_"))
async def reject_order(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    order_id = int(callback.data.split("_")[1])
    order = orders.get(order_id)
    if not order or order["status"] != "new":
        await callback.answer("Заказ уже обработан ❌", show_alert=True)
        return

    order["status"] = "rejected"
    # освобождаем слот, если был
    key = f"{order.get('date','')} {order.get('time','')}".strip()
    booked_slots.discard(key)
    await callback.message.edit_text(f"Заказ #{order_id} отклонён ❌")
    await bot.send_message(order["user_id"], f"Ваш заказ #{order_id} отклонён ❌")


@dp.callback_query(F.data.startswith("accept_"))
async def accept_order(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    order_id = int(callback.data.split("_")[1])
    order = orders.get(order_id)
    if not order or order["status"] != "new":
        await callback.answer("Заказ уже обработан ❌", show_alert=True)
        return
    order["status"] = "accepted"
    await callback.message.edit_text(f"Заказ #{order_id} принят ✅")
    await bot.send_message(order["user_id"], f"Ваш заказ #{order_id} принят ✅")

@dp.callback_query(F.data.startswith("reject_"))
async def reject_order(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    order_id = int(callback.data.split("_")[1])
    order = orders.get(order_id)
    if not order or order["status"] != "new":
        await callback.answer("Заказ уже обработан ❌", show_alert=True)
        return
    order["status"] = "rejected"
    # освободим слот, если был
    key = f"{order.get('date','')} {order.get('time','')}".strip()
    booked_slots.discard(key)
    await callback.message.edit_text(f"Заказ #{order_id} отклонён ❌")
    await bot.send_message(order["user_id"], f"Ваш заказ #{order_id} отклонён ❌")

# -------------------------------
# Админ-панель (вход)
# -------------------------------
@dp.message(Command("admin"))
async def admin_main(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Управление каталогом", callback_data="admin_catalog")],
        [InlineKeyboardButton(text="🛒 Управление заказами", callback_data="admin_orders")],
        [InlineKeyboardButton(text="➕ Добавить категорию", callback_data="add_category")]
    ])
    await message.answer("⚙️ Админ-панель:", reply_markup=kb)

# -------------------------------
# Управление каталогом (админ)
# -------------------------------
@dp.callback_query(F.data == "admin_catalog")
async def admin_catalog_menu(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    if not catalog:
        kb.button(text="➕ Добавить категорию", callback_data="add_category")
        kb.button(text="⬅️ В админ-панель", callback_data="admin_root")
        kb.adjust(1)
        await callback.message.edit_text("Категорий пока нет. Добавьте первую:", reply_markup=kb.as_markup())
        return

    for i, category in enumerate(catalog):
        kb.button(text=f"📦 {category['name']}", callback_data=f"admin_cat_{i}")
    kb.button(text="➕ Добавить категорию", callback_data="add_category")
    kb.button(text="⬅️ В админ-панель", callback_data="admin_root")
    kb.adjust(1)
    await callback.message.edit_text("Категории:", reply_markup=kb.as_markup())

@dp.callback_query(F.data == "admin_root")
async def admin_root(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Управление каталогом", callback_data="admin_catalog")],
        [InlineKeyboardButton(text="🛒 Управление заказами", callback_data="admin_orders")],
        [InlineKeyboardButton(text="➕ Добавить категорию", callback_data="add_category")]
    ])
    await callback.message.edit_text("⚙️ Админ-панель:", reply_markup=kb)

# ---- Категория
@dp.callback_query(F.data == "add_category")
async def add_category(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(EditProduct.waiting_new_category)
    await callback.message.answer("Введите название новой категории:")

@dp.message(EditProduct.waiting_new_category)
async def save_new_category(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return
    title = message.text.strip()
    if not title:
        await message.answer("❌ Пустое название. Введите ещё раз.")
        return
    catalog.append({"name": title, "items": []})
    save_catalog()
    await state.clear()
    await message.answer(f"✅ Категория «{title}» добавлена.")
    await send_admin_catalog_menu(message.chat.id)

@dp.callback_query(F.data.startswith("admin_cat_"))
async def admin_category(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    cat_id = int(callback.data.split("_")[2])
    if not (0 <= cat_id < len(catalog)):
        await callback.answer("Категория не найдена", show_alert=True)
        return

    category = catalog[cat_id]
    kb = InlineKeyboardBuilder()
    if category["items"]:
        for idx, item in enumerate(category["items"]):
            kb.button(text=f"{item['name']} — {item['price']}₽", callback_data=f"admin_item_{cat_id}_{idx}")
    else:
        kb.button(text="(товаров нет)", callback_data="noop")
    kb.button(text="➕ Добавить товар", callback_data=f"admin_add_{cat_id}")
    kb.button(text="✏️ Редактировать название категории", callback_data=f"editcat_{cat_id}")
    kb.button(text="🗑 Удалить категорию", callback_data=f"del_cat_{cat_id}")
    kb.button(text="⬅️ Назад к категориям", callback_data="admin_catalog")
    kb.adjust(1)
    await callback.message.edit_text(f"Категория: {category['name']}", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("del_cat_"))
async def delete_category(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    cat_id = int(callback.data.split("_")[2])
    if 0 <= cat_id < len(catalog):
        name = catalog[cat_id]["name"]
        del catalog[cat_id]
        save_catalog()
        await callback.message.edit_text(f"✅ Категория «{name}» удалена.")
        await send_admin_catalog_menu(callback.message.chat.id)
    else:
        await callback.answer("Категория не найдена", show_alert=True)

# ---- Товар: добавление
@dp.callback_query(F.data.startswith("admin_add_"))
async def admin_add_item(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    cat_id = int(callback.data.split("_")[2])
    if cat_id < 0 or cat_id >= len(catalog):
        await callback.answer("Категория не найдена", show_alert=True)
        return
    await state.update_data(mode="add_item", cat_id=cat_id)
    await state.set_state(EditProduct.waiting_name)
    await callback.message.answer("Введите название товара:")

@dp.message(EditProduct.waiting_name)
async def handle_item_name(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return
    data = await state.get_data()
    mode = data.get("mode")
    if mode not in ("add_item", "edit_name"):
        await message.answer("Неожиданный шаг. Попробуйте снова /admin")
        await state.clear()
        return

    name = message.text.strip()
    if not name:
        await message.answer("❌ Пустое название. Введите ещё раз.")
        return

    if mode == "add_item":
        await state.update_data(temp_name=name)
        await state.set_state(EditProduct.waiting_price)
        await message.answer("Введите цену (числом):")
    else:
        cat_id = int(data["cat_id"]); idx = int(data["idx"])
        catalog[cat_id]["items"][idx]["name"] = name
        save_catalog()
        await state.clear()
        await message.answer("✅ Название обновлено.")
        await send_item_card(message.chat.id, cat_id, idx)

@dp.message(EditProduct.waiting_price)
async def handle_item_price(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return
    data = await state.get_data()
    mode = data.get("mode")
    if mode not in ("add_item", "edit_price"):
        await message.answer("Неожиданный шаг. Попробуйте снова /admin")
        await state.clear()
        return

    try:
        price = int(message.text.strip())
        if price < 0:
            raise ValueError
    except Exception:
        await message.answer("❌ Неверная цена. Введите целое число, например 350.")
        return

    if mode == "add_item":
        await state.update_data(temp_price=price)
        await state.set_state(EditProduct.waiting_options)
        await message.answer("Введите варианты вкусов через запятую (или напишите «нет»):")
    else:
        cat_id = int(data["cat_id"]); idx = int(data["idx"])
        catalog[cat_id]["items"][idx]["price"] = price
        save_catalog()
        await state.clear()
        await message.answer("✅ Цена обновлена.")
        await send_item_card(message.chat.id, cat_id, idx)

@dp.message(EditProduct.waiting_options)
async def handle_item_options(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return

    data = await state.get_data()
    mode = data.get("mode")

    # Режим добавления товара
    if mode == "add_item":
        cat_id = data["cat_id"]
        name = data["temp_name"]
        price = data["temp_price"]

        item = {"name": name, "price": price}
        options_text = message.text.strip()

        if options_text.lower() != "нет":
            options = [opt.strip() for opt in options_text.split(",") if opt.strip()]
            if options:
                item["options"] = options
                # Сразу создаём stock с нуля для каждого вкуса
                item["options_stock"] = {opt: 0 for opt in options}

        catalog[cat_id]["items"].append(item)
        save_catalog()
        await state.clear()
        await message.answer(f"✅ Товар «{name}» добавлен за {price}₽.")
        await send_admin_category_menu(message.chat.id, cat_id)
        return

    # Режим редактирования остатка вкусов
    if mode == "edit_flavor":
        cat_id = int(data["cat_id"])
        idx = int(data["idx"])
        item = catalog[cat_id]["items"][idx]
        options_text = message.text.strip()

        try:
            new_stock = {}
            for part in options_text.split(","):
                opt, qty = part.split("=")
                opt = opt.strip()
                qty = int(qty.strip())
                if opt not in item.get("options", []):
                    await message.answer(f"❌ Вкуса {opt} нет в товаре")
                    return
                new_stock[opt] = qty
            item["options_stock"] = new_stock
            save_catalog()
            await message.answer("✅ Остатки вкусов обновлены")
            await send_item_card(message.chat.id, cat_id, idx)
            await state.clear()
        except Exception:
            await message.answer("❌ Ошибка формата. Используйте Вкус=Количество через запятую")
            return

    # Режим редактирования вариантов (название/добавление вкусов)
    if mode == "edit_options":
        cat_id = int(data["cat_id"])
        idx = int(data["idx"])
        options_text = message.text.strip()

        item = catalog[cat_id]["items"][idx]

        if options_text.lower() == "нет":
            # удаляем все вкусы
            item.pop("options", None)
            item.pop("options_stock", None)
        else:
            new_options = [opt.strip() for opt in options_text.split(",") if opt.strip()]

            # Добавляем новые вкусы к существующим
            existing_options = set(item.get("options", []))
            for opt in new_options:
                if opt not in existing_options:
                    item.setdefault("options", []).append(opt)
                    item.setdefault("options_stock", {})[opt] = 0  # стартовое количество

        save_catalog()
        await state.clear()
        await message.answer("✅ Вкусы обновлены.")
        await send_item_card(message.chat.id, cat_id, idx)


# ---- Товар: карточка + действия
@dp.callback_query(F.data.startswith("admin_item_"))
async def admin_item(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    _, _, cat_id, idx = callback.data.split("_")
    await send_item_card(callback.message.chat.id, int(cat_id), int(idx))

@dp.callback_query(F.data.startswith("editname_"))
async def edit_name(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    _, cat_id, idx = callback.data.split("_")
    await state.update_data(mode="edit_name", cat_id=int(cat_id), idx=int(idx))
    await state.set_state(EditProduct.waiting_name)
    await callback.message.answer("Введите новое название товара:")

@dp.callback_query(F.data.startswith("editprice_"))
async def edit_price(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    _, cat_id, idx = callback.data.split("_")
    await state.update_data(mode="edit_price", cat_id=int(cat_id), idx=int(idx))
    await state.set_state(EditProduct.waiting_price)
    await callback.message.answer("Введите новую цену (числом):")


@dp.callback_query(F.data.startswith("del_item_"))
async def delete_item(callback: types.CallbackQuery):
    await callback.answer()  # убираем "крутилку"
    logging.info("Удаление товара: %s", callback.data)

    parts = callback.data.split("_")  # ['del','item','0','1']
    cat_id = int(parts[2])
    idx = int(parts[3])

    name = catalog[cat_id]["items"][idx]["name"]
    del catalog[cat_id]["items"][idx]
    save_catalog()

    try:
        await callback.message.delete()
    except Exception as e:
        logging.exception("Не удалось удалить сообщение: %s", e)

    await send_admin_category_menu(callback.message.chat.id, cat_id)


# Шаг 1: Выбор вкуса
@dp.callback_query(F.data.startswith("editflavor_"))
async def edit_flavor_callback(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    try:
        _, cat_id, idx = callback.data.split("_")
        cat_id, idx = int(cat_id), int(idx)
    except Exception:
        await callback.answer("Ошибка данных кнопки ❌", show_alert=True)
        return

    item = catalog[cat_id]["items"][idx]
    await state.update_data(cat_id=cat_id, idx=idx)

    kb = InlineKeyboardBuilder()
    if "options" in item and item["options"]:
        for opt in item["options"]:
            kb.button(text=f"{opt}: {item.get('options_stock', {}).get(opt,0)} шт.", callback_data=f"editopt_{cat_id}_{idx}_{opt}")
        kb.adjust(1)
        await callback.message.answer(f"Выберите вкус для редактирования остатков товара '{item['name']}':", reply_markup=kb.as_markup())
    else:
        # Для товаров без вкусов
        if "stock" not in item:
            item["stock"] = 0
        kb.button(text=f"Остаток: {item['stock']} шт.", callback_data=f"editstock_{cat_id}_{idx}")
        kb.adjust(1)
        await callback.message.answer(f"Редактировать остаток товара '{item['name']}':", reply_markup=kb.as_markup())
# Шаг 2: Ввод нового остатка для конкретного вкуса
@dp.callback_query(F.data.startswith("editopt_"))
async def edit_option_stock(callback: types.CallbackQuery, state: FSMContext):
    _, cat_id, idx, opt = callback.data.split("_", 3)
    cat_id, idx = int(cat_id), int(idx)

    item = catalog[cat_id]["items"][idx]
    await state.update_data(option=opt)
    await state.set_state(EditProduct.waiting_new_option)
    await callback.message.answer(f"Текущий остаток вкуса '{opt}': {item['options_stock'].get(opt,0)}\nВведите новое количество:")
# Шаг 3: Ввод нового остатка (универсальный)
@dp.message(EditProduct.waiting_new_option)
async def process_single_stock(message: types.Message, state: FSMContext):
    data = await state.get_data()
    cat_id = data.get("cat_id")
    idx = data.get("idx")
    opt = data.get("option")  # может быть None для товаров без вкусов

    item = catalog[cat_id]["items"][idx]

    try:
        amount = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введите корректное число!")
        return

    if opt:  # для конкретного вкуса
        if "options_stock" not in item:
            item["options_stock"] = {opt: 0}
        item["options_stock"][opt] = max(amount, 0)
        await message.answer(f"✅ Остаток вкуса '{opt}' для товара '{item['name']}' установлен на {amount}")
    else:  # для товара без вкусов
        if "stock" not in item:
            item["stock"] = 0
        item["stock"] = max(amount, 0)
        await message.answer(f"✅ Остаток товара '{item['name']}' установлен на {amount}")

    save_catalog()
    await state.clear()
@dp.callback_query(F.data.startswith("editstock_"))
async def edit_stock_callback(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    try:
        _, cat_id, idx = callback.data.split("_")
        cat_id, idx = int(cat_id), int(idx)
    except Exception:
        await callback.answer("Ошибка данных кнопки ❌", show_alert=True)
        return

    item = catalog[cat_id]["items"][idx]
    if "stock" not in item:
        item["stock"] = 0

    # Сохраняем данные в FSM
    await state.update_data(cat_id=cat_id, idx=idx, option=None)
    await state.set_state(EditProduct.waiting_new_option)

    await callback.message.answer(
        f"Текущий остаток товара '{item['name']}': {item['stock']} шт.\n"
        "Введите новое количество:"
    )

@dp.callback_query(F.data.startswith("editcat_"))
async def edit_category_name(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    cat_id = int(callback.data.split("_")[1])
    if not (0 <= cat_id < len(catalog)):
        await callback.answer("Категория не найдена", show_alert=True)
        return

    await state.update_data(cat_id=cat_id)
    await state.set_state(EditCategory.waiting_new_name)
    await callback.message.answer(f"Введите новое название категории «{catalog[cat_id]['name']}»:")
@dp.message(EditCategory.waiting_new_name)
async def save_new_category_name(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return

    new_name = message.text.strip()
    if not new_name:
        await message.answer("❌ Название не может быть пустым. Попробуйте снова.")
        return

    data = await state.get_data()
    cat_id = data.get("cat_id")
    if cat_id is None or not (0 <= cat_id < len(catalog)):
        await message.answer("Ошибка. Категория не найдена.")
        await state.clear()
        return

    catalog[cat_id]["name"] = new_name
    save_catalog()
    await state.clear()
    await message.answer(f"✅ Название категории изменено на «{new_name}»")

    # обновляем админ-меню категории
    await send_admin_category_menu(message.chat.id, cat_id)


@dp.callback_query(F.data.startswith("removeopt_"))
async def remove_option_callback(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    _, cat_id, idx = callback.data.split("_")
    cat_id, idx = int(cat_id), int(idx)

    await state.update_data(cat_id=cat_id, idx=idx)
    await state.set_state(EditProduct.waiting_remove_option)
    await callback.message.answer("Введите вкус, который хотите убрать:")
@dp.message(EditProduct.waiting_remove_option)
async def handle_remove_option(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return

    data = await state.get_data()
    cat_id = data["cat_id"]
    idx = data["idx"]
    item = catalog[cat_id]["items"][idx]

    opt_to_remove = message.text.strip()

    if "options" not in item or opt_to_remove not in item["options"]:
        await message.answer("❌ Вкуса нет в списке")
        return

    # Удаляем вкус из словаря
    item["options"].pop(opt_to_remove)

    save_catalog()
    await state.clear()
    await message.answer(f"✅ Вкус «{opt_to_remove}» удалён")
    await send_item_card(message.chat.id, cat_id, idx)


@dp.callback_query(F.data.startswith("add_option_"))
async def add_option_callback(callback: types.CallbackQuery, state: FSMContext):
    # Разбираем данные колбэка
    parts = callback.data.split("_")
    cat_id = int(parts[2]) if len(parts) > 2 else None  # Приводим к int
    idx = int(parts[3]) if len(parts) > 3 else None      # Приводим к int

    # Если данные корректные, показываем карточку
    if cat_id is not None:
        await send_item_card(callback.message.chat.id, cat_id, idx)
    else:
        await callback.message.answer("Ошибка: не удалось определить категорию.")

    # Сохраняем данные в состоянии
    await state.update_data(cat_id=cat_id, idx=idx)
    await state.set_state(EditProduct.waiting_new_option)
    await callback.message.answer("Введите название нового вкуса:")


@dp.message(EditProduct.waiting_new_option)
async def handle_add_option(message: types.Message, state: FSMContext):
    data = await state.get_data()
    cat_id = data.get("cat_id")
    idx = data.get("idx")  # получаем idx из состояния, если он есть
    new_opt = message.text.strip()

    if not new_opt:
        await message.answer("❌ Название вкуса не может быть пустым")
        return

    # Добавляем новый вариант
    result = add_option_to_item(catalog, cat_id, new_opt, option_value=0)
    await message.answer(result)

    # Отправляем карточку товара с idx, если он есть
    await send_item_card(message.chat.id, cat_id, idx)

    # Очищаем состояние
    await state.clear()

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    status = get_status(user_id)

    if status == "approved":
        await message.answer("Привет! 👋 Выберите действие:", reply_markup=main_kb)
    elif status == "pending":
        await message.answer("⏳ Ваша заявка на верификацию рассматривается администратором.")
    elif status == "denied":
        await message.answer("❌ Верификация отклонена. Попробуйте снова и отправьте новый кружок.")
        set_status(user_id, "none")
    else:
        await message.answer("👤 Для продолжения отправьте кружок 🎥 с вашим лицом.")

@dp.message(F.video_note)
async def handle_video_note(message: types.Message):
    user_id = message.from_user.id
    if get_status(user_id) == "approved":
        await message.answer("✅ Вы уже прошли верификацию.")
        return

    set_status(user_id, "pending", message.from_user.username or message.from_user.full_name)
    await message.answer("⏳ Видео отправлено на проверку админу.")

    for admin_id in ADMINS:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Принять", callback_data=f"verify_allow_{user_id}")],
            [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"verify_deny_{user_id}")]
        ])
        sent = await bot.send_video_note(admin_id, message.video_note.file_id, reply_markup=kb)
        await bot.send_message(admin_id, f"Заявка на верификацию от @{message.from_user.username or message.from_user.full_name}\nID: {user_id}")

@dp.callback_query(F.data.startswith("verify_allow_"))
async def verify_allow(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа", show_alert=True)

    user_id = int(callback.data.split("_")[2])
    set_status(user_id, "approved")

    # Убираем кнопки под кружком
    await callback.message.edit_reply_markup()

    # Сообщение админу
    await callback.message.answer(f"✅ Пользователь {user_id} подтверждён.")

    # Сообщение пользователю
    await bot.send_message(user_id, "🎉 Ваша верификация успешно пройдена! Теперь вы можете пользоваться ботом.")

    await callback.answer()

@dp.callback_query(F.data.startswith("verify_deny_"))
async def verify_deny(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа", show_alert=True)

    user_id = int(callback.data.split("_")[2])
    set_status(user_id, "denied")

    # Убираем кнопки под кружком
    await callback.message.edit_reply_markup()

    # Сообщение админу
    await callback.message.answer(f"❌ Пользователь {user_id} отклонён.")

    # Сообщение пользователю
    await bot.send_message(user_id, "❌ Верификация отклонена. Отправьте новое видео.")

    await callback.answer()

@dp.message(Command("allow"))
async def allow_cmd(message: types.Message):
    if not is_admin(message.from_user.id):
        return await message.answer("Нет доступа ❌")

    parts = message.text.split()
    if len(parts) < 2:
        return await message.answer("⚠️ Использование: /allow <user_id>")

    try:
        user_id = int(parts[1])
    except ValueError:
        return await message.answer("❌ user_id должен быть числом")

    # Создаём запись, даже если пользователя не было
    set_status(user_id, "approved")

    await message.answer(f"✅ Пользователь {user_id} подтверждён")

    # Попробуем отправить сообщение пользователю
    try:
        await bot.send_message(user_id, "🎉 Ваша верификация успешно пройдена! Теперь вы можете пользоваться ботом.")
    except Exception:
        await message.answer("⚠️ Сообщение пользователю не доставлено (он ещё не запускал бота).")

    @dp.message(Command("deny"))
    async def deny_cmd(message: types.Message):
        if not is_admin(message.from_user.id):
            return await message.answer("Нет доступа ❌")

        parts = message.text.split()
        if len(parts) < 2:
            return await message.answer("⚠️ Использование: /deny <user_id>")

        try:
            user_id = int(parts[1])
        except ValueError:
            return await message.answer("❌ user_id должен быть числом")

        # Создаём запись, даже если пользователя не было
        set_status(user_id, "denied")

        await message.answer(f"❌ Пользователь {user_id} отклонён")

        # Попробуем отправить сообщение пользователю
        try:
            await bot.send_message(user_id, "❌ Верификация отклонена. Отправьте новое видео.")
        except Exception:
            await message.answer("⚠️ Сообщение пользователю не доставлено (он ещё не запускал бота).")

@dp.callback_query(F.data == "noop")
async def noop_handler(callback: types.CallbackQuery):
    await callback.answer("Недоступно", show_alert=True)

# -------------------------------
# Сброс слотов ежедневно
# -------------------------------
async def reset_booked_slots():
    while True:
        now = datetime.now(MSK)
        target = now.replace(hour=0, minute=1, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        wait_seconds = (target - now).total_seconds()
        await asyncio.sleep(wait_seconds)
        booked_slots.clear()   # 🔴 тут очищаются все слоты
        logging.info("✅ Слоты сброшены (новый день)")

# -------------------------------
# Запуск бота
@app.on_event("startup")
async def startup_event():
    await bot.delete_webhook()
    await bot.set_webhook(WEBHOOK_URL)
    asyncio.create_task(reset_booked_slots())
    print(f"✅ Вебхук установлен: {WEBHOOK_URL}")

@app.on_event("shutdown")
async def shutdown_event():
    await bot.delete_webhook()
    print("❌ Вебхук удалён")

@app.post(WEBHOOK_PATH)
async def telegram_webhook(req: Request):
    data = await req.json()
    update = types.Update(**data)
    await dp.process_update(update)
    return {"ok": True}


