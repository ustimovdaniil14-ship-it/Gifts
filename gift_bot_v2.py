import asyncio
import json
import os
import random
import time
from datetime import date

import aiohttp
import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton as Btn,
    InlineKeyboardMarkup as Kb,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

# ===== Переменные окружения (задаются на Render) =====
BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ.get("DATABASE_URL", "")  # строка подключения из Neon
FREE_API_URL = os.environ.get("FREE_API_URL", "").rstrip("/")  # адрес твоего API для бесплатной отправки
API_URL = os.environ.get("API_URL", "http://31.77.10.54:18081").rstrip("/")  # адрес не-оригинального Bot API
ADMINS = {int(x) for x in os.environ.get("ADMINS", "").replace(" ", "").split(",") if x}

bot = Bot(BOT_TOKEN, session=AiohttpSession(api=TelegramAPIServer.from_base(API_URL)))
dp = Dispatcher()

# ===== Настройки (меняются из бота через /settings) =====
SETTINGS_FILE = "settings.json"
S = {"min_stars": 15, "max_stars": 100, "min_msgs": 3, "cooldown_min": 30, "daily_limit": 1, "custom_gifts": []}
try:
    S.update(json.load(open(SETTINGS_FILE)))
except Exception:
    pass

LABELS = {
    "min_stars": "Мин. цена гифта (⭐, не меньше 15)",
    "max_stars": "Макс. цена гифта (⭐, не больше 100)",
    "min_msgs": "Мин. сообщений в чате для участия",
    "cooldown_min": "Пауза между ивентами в чате (мин)",
    "daily_limit": "Макс. подарков одному человеку в сутки",
}


pool = None  # пул подключений к Neon (если задан DATABASE_URL)


async def init_db():
    """Подключаемся к Neon, создаём таблицы, подтягиваем настройки и сегодняшние победы."""
    global pool
    if not DATABASE_URL:
        print("DATABASE_URL не задан - настройки хранятся в файле и пропадут при деплое")
        return
    pool = await asyncpg.create_pool(
        DATABASE_URL, min_size=1, max_size=3,
        statement_cache_size=0, max_inactive_connection_lifetime=30,
    )
    async with pool.acquire() as con:
        await con.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        await con.execute("CREATE TABLE IF NOT EXISTS wins (user_id BIGINT PRIMARY KEY, day DATE NOT NULL, n INT NOT NULL)")
        row = await con.fetchrow("SELECT value FROM settings WHERE key = 'settings'")
        if row:
            S.update(json.loads(row["value"]))
        for r in await con.fetch("SELECT user_id, day, n FROM wins WHERE day = $1", date.today()):
            wins[r["user_id"]] = (r["day"], r["n"])
    print("База подключена")


async def save_settings():
    if pool:
        try:
            async with pool.acquire() as con:
                await con.execute(
                    "INSERT INTO settings (key, value) VALUES ('settings', $1) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                    json.dumps(S),
                )
        except Exception as e:
            print("Ошибка сохранения настроек:", e)
        return
    try:
        json.dump(S, open(SETTINGS_FILE, "w"))
    except Exception:
        pass


async def save_win(uid: int):
    if not pool:
        return
    d, n = wins[uid]
    try:
        async with pool.acquire() as con:
            await con.execute(
                "INSERT INTO wins (user_id, day, n) VALUES ($1, $2, $3) "
                "ON CONFLICT (user_id) DO UPDATE SET day = EXCLUDED.day, n = EXCLUDED.n",
                uid, d, n,
            )
    except Exception as e:
        print("Ошибка сохранения победы:", e)


# ===== Память =====
events: dict[int, dict] = {}      # chat_id -> данные идущего ивента
launches: dict[int, dict] = {}    # admin_id -> настройка запуска
last_event: dict[int, float] = {}  # chat_id -> время последнего ивента
wins: dict[int, tuple] = {}       # user_id -> (дата, сколько подарков сегодня)

GROUP = F.chat.type.in_({"group", "supergroup"})
PRIVATE = F.chat.type == "private"
ADMIN = F.from_user.id.in_(ADMINS)


class Edit(StatesGroup):
    value = State()


class Custom(StatesGroup):
    chance = State()


HELP = (
    "📖 Команды бота\n\n"
    "В личке с ботом (только админы):\n"
    "/start — главное меню\n"
    "/help — список команд\n"
    "/settings — настройки (цена гифтов, мин. сообщений, пауза, лимиты)\n"
    "/balance — баланс бота в звёздах\n"
    "/gifts — список подарков Telegram с их ID\n"
    "/addgift ID — добавить подарок в розыгрыш по ID\n"
    "/delgift ID — убрать добавленный подарок\n"
    "/topup 500 — пополнить баланс\n"
    "/cancel — отменить текущее действие\n\n"
    "В чате:\n"
    "/event — запустить ивент. Бот напишет тебе в личку и спросит шанс, "
    "длительность и число призов."
)


def menu_kb():
    return Kb(inline_keyboard=[
        [Btn(text="⚙️ Настройки", callback_data="m:settings"), Btn(text="💰 Баланс", callback_data="m:balance")],
        [Btn(text="➕ Пополнить", callback_data="m:topup"), Btn(text="📖 Команды", callback_data="m:help")],
    ])


async def get_balance() -> int:
    try:
        return (await bot.get_my_star_balance()).amount
    except Exception:
        return 0


def who(u) -> str:
    return f"@{u.username}" if u.username else u.full_name


# ===== Меню и помощь =====
@dp.message(Command("start"), PRIVATE)
async def start(m: Message):
    if m.from_user.id not in ADMINS:
        await m.answer("Привет! Участвуй в ивентах в чате, пиши и активничай 🎁")
        return
    await m.answer("Привет, админ! Что делаем?\n\nИвент запускается командой /event в нужном чате.", reply_markup=menu_kb())


@dp.message(Command("help"), PRIVATE, ADMIN)
async def help_cmd(m: Message):
    await m.answer(HELP)


@dp.message(Command("cancel"), PRIVATE, ADMIN)
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    launches.pop(m.from_user.id, None)
    await m.answer("Отменено.", reply_markup=menu_kb())


@dp.callback_query(F.data.startswith("m:"), ADMIN)
async def menu_cb(c: CallbackQuery):
    act = c.data[2:]
    if act == "settings":
        await show_settings(c.message)
    elif act == "balance":
        await c.message.answer(f"💰 Баланс бота: {await get_balance()} ⭐")
    elif act == "help":
        await c.message.answer(HELP)
    elif act == "topup":
        kb = Kb(inline_keyboard=[[Btn(text=f"{n} ⭐", callback_data=f"top:{n}") for n in (100, 500, 1000)]])
        await c.message.answer("Сколько звёзд закинуть? Или напиши /topup число", reply_markup=kb)
    await c.answer()


# ===== Баланс и пополнение =====
async def send_invoice(chat_id: int, amount: int):
    await bot.send_invoice(
        chat_id=chat_id,
        title="Пополнение баланса бота",
        description=f"{amount} звёзд на подарки",
        payload="topup",
        currency="XTR",
        prices=[LabeledPrice(label="Звёзды", amount=amount)],
    )


@dp.message(Command("balance"), PRIVATE, ADMIN)
async def balance(m: Message):
    await m.answer(f"💰 Баланс бота: {await get_balance()} ⭐")


@dp.message(Command("topup"), PRIVATE, ADMIN)
async def topup(m: Message):
    parts = m.text.split()
    if len(parts) < 2 or not parts[1].isdigit() or int(parts[1]) < 1:
        await m.answer("Напиши так: /topup 500")
        return
    await send_invoice(m.chat.id, int(parts[1]))


@dp.callback_query(F.data.startswith("top:"), ADMIN)
async def topup_cb(c: CallbackQuery):
    await send_invoice(c.message.chat.id, int(c.data[4:]))
    await c.answer()


@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)


@dp.message(F.successful_payment)
async def paid(m: Message):
    await m.answer(f"✅ Баланс пополнен. Сейчас: {await get_balance()} ⭐")


# ===== Настройки =====
async def show_settings(msg: Message):
    text = "⚙️ Настройки\n\n" + "\n".join(f"• {LABELS[k]}: {S[k]}" for k in LABELS)
    kb = Kb(inline_keyboard=[[Btn(text=LABELS[k], callback_data=f"set:{k}")] for k in LABELS])
    await msg.answer(text + "\n\nНажми, что хочешь поменять:", reply_markup=kb)


@dp.message(Command("settings"), PRIVATE, ADMIN)
async def settings_cmd(m: Message):
    await show_settings(m)


@dp.callback_query(F.data.startswith("set:"), ADMIN)
async def set_cb(c: CallbackQuery, state: FSMContext):
    key = c.data[4:]
    await state.set_state(Edit.value)
    await state.update_data(key=key)
    await c.message.answer(f"{LABELS[key]}\nСейчас: {S[key]}\n\nНапиши новое число (/cancel — отмена)")
    await c.answer()


@dp.message(StateFilter(Edit.value), PRIVATE, ADMIN)
async def set_value(m: Message, state: FSMContext):
    key = (await state.get_data())["key"]
    if not m.text or not m.text.isdigit() or int(m.text) < 1:
        await m.answer("Нужно целое число больше нуля.")
        return
    v = int(m.text)
    if key in ("min_stars", "max_stars") and not 15 <= v <= 100:
        await m.answer("Для гифтов подходит диапазон 15–100 звёзд.")
        return
    new = {**S, key: v}
    if new["min_stars"] > new["max_stars"]:
        await m.answer("Минимум не может быть больше максимума.")
        return
    S[key] = v
    await save_settings()
    await state.clear()
    await m.answer("✅ Сохранено.")
    await show_settings(m)


# ===== Подарки: список и добавление по ID =====
EMOJI_NAMES = {
    "🧸": "мишку", "💝": "сердечко", "❤️": "сердечко", "🌹": "розу", "🎁": "подарок",
    "🎂": "торт", "💐": "букет", "🚀": "ракету", "🍾": "шампанское", "🏆": "кубок",
    "💍": "кольцо", "💎": "бриллиант",
}


def gift_word(g: dict) -> str:
    return EMOJI_NAMES.get(g["emoji"], "подарок")


def pool_item(g) -> dict:
    st = getattr(g, "sticker", None)
    return {"id": str(g.id), "stars": g.star_count, "emoji": getattr(st, "emoji", None) or "🎁"}


async def load_pool() -> list[dict]:
    """Обычные подарки Telegram + те, что ты добавил через /addgift."""
    items: dict[str, dict] = {}
    try:
        gifts = (await bot.get_available_gifts()).gifts
    except Exception:
        gifts = []
    for g in gifts:
        if getattr(g, "total_count", None) is None:  # лимитированные только через /addgift
            items[str(g.id)] = pool_item(g)
    for c in S["custom_gifts"]:
        items[str(c["id"])] = c
    return list(items.values())


async def api_send_gift(user_id: int, gift_id: str):
    """БЕСПЛАТНАЯ отправка через твой API.
    !!! Путь и поля ниже - заглушка, подстрой под свой API (пришли мне его описание). """
    if not FREE_API_URL:
        raise RuntimeError("не задан FREE_API_URL")
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.post(f"{FREE_API_URL}/send_gift", json={"user_id": user_id, "gift_id": gift_id}) as r:
            if r.status != 200:
                raise RuntimeError(f"API {r.status}: {(await r.text())[:200]}")


@dp.message(Command("gifts"), PRIVATE, ADMIN)
async def gifts_cmd(m: Message):
    try:
        gifts = (await bot.get_available_gifts()).gifts
    except Exception as e:
        await m.answer(f"Не получилось загрузить список: {e}")
        return
    added = {str(c["id"]) for c in S["custom_gifts"]}
    lines = []
    for g in sorted(gifts, key=lambda x: x.star_count):
        it = pool_item(g)
        lim = "" if getattr(g, "total_count", None) is None else " (лимитка)"
        mark = " ➕" if str(g.id) in added else ""
        lines.append(f"{it['emoji']} {g.star_count} ⭐{lim}{mark} — {g.id}")
    text = "🎁 Доступные подарки (➕ = добавлен тобой)\nОбычные участвуют сами, лимитки добавляй через /addgift\n\n"
    await m.answer(text + "\n".join(lines)[:3800] + "\n\nДобавить: /addgift ID")


@dp.message(Command("addgift"), PRIVATE, ADMIN)
async def addgift_cmd(m: Message):
    parts = m.text.split()
    if len(parts) < 2:
        await m.answer("Напиши так: /addgift ID_подарка\nСписок ID: /gifts")
        return
    gid = parts[1]
    try:
        gifts = (await bot.get_available_gifts()).gifts
    except Exception as e:
        await m.answer(f"Не получилось загрузить список подарков: {e}")
        return
    g = next((x for x in gifts if str(x.id) == gid), None)
    if not g:
        await m.answer("Такого ID нет среди доступных подарков Telegram. Проверь ID в /gifts")
        return
    item = pool_item(g)
    S["custom_gifts"] = [c for c in S["custom_gifts"] if str(c["id"]) != gid] + [item]
    await save_settings()
    await m.answer(f"✅ Добавлен: {item['emoji']} за {item['stars']} ⭐ (ID {gid})\nТеперь он может выпасть в ивенте.")


@dp.message(Command("delgift"), PRIVATE, ADMIN)
async def delgift_cmd(m: Message):
    parts = m.text.split()
    if len(parts) < 2:
        await m.answer("Напиши так: /delgift ID_подарка")
        return
    before = len(S["custom_gifts"])
    S["custom_gifts"] = [c for c in S["custom_gifts"] if str(c["id"]) != parts[1]]
    await save_settings()
    await m.answer("🗑 Убран." if len(S["custom_gifts"]) < before else "Такого добавленного подарка нет.")


# ===== Запуск ивента: /event в чате -> вопросы в личке =====
@dp.message(Command("event"), GROUP)
async def event_cmd(m: Message):
    if m.from_user.id not in ADMINS:
        return
    cid = m.chat.id
    if cid in events:
        await m.reply("Ивент уже идёт ⏳")
        return
    left = last_event.get(cid, 0) + S["cooldown_min"] * 60 - time.time()
    if left > 0:
        await m.reply(f"Рано! Подожди ещё ~{int(left // 60) + 1} мин. Так подарки не падают слишком часто.")
        return
    launches[m.from_user.id] = {"chat_id": cid, "title": m.chat.title, "admin": who(m.from_user)}
    kb = Kb(inline_keyboard=[
        [Btn(text=f"{n}%", callback_data=f"ch:{n}") for n in (10, 25, 50)],
        [Btn(text=f"{n}%", callback_data=f"ch:{n}") for n in (75, 100)],
        [Btn(text="✍️ Свой шанс", callback_data="ch:custom")],
    ])
    try:
        await bot.send_message(
            m.from_user.id,
            f"🎯 Ивент в чате «{m.chat.title}»\n\nКакой шанс, что подарок вообще упадёт?",
            reply_markup=kb,
        )
        await m.reply("📩 Я написал тебе в личку, настрой ивент там.")
    except Exception:
        launches.pop(m.from_user.id, None)
        await m.reply("Сначала запусти меня в личке (/start), потом повтори /event.")


def need_launch(c: CallbackQuery):
    return launches.get(c.from_user.id)


@dp.callback_query(F.data.startswith("ch:"), ADMIN)
async def chance_cb(c: CallbackQuery, state: FSMContext):
    l = need_launch(c)
    if not l:
        await c.answer("Сессия устарела, напиши /event в чате заново", show_alert=True)
        return
    v = c.data[3:]
    if v == "custom":
        await state.set_state(Custom.chance)
        await c.message.answer("Напиши шанс числом от 1 до 100")
    else:
        l["chance"] = int(v)
        await ask_duration(c.message)
    await c.answer()


@dp.message(StateFilter(Custom.chance), PRIVATE, ADMIN)
async def custom_chance(m: Message, state: FSMContext):
    l = launches.get(m.from_user.id)
    if not l:
        await state.clear()
        await m.answer("Сессия устарела, напиши /event в чате заново.")
        return
    if not m.text or not m.text.isdigit() or not 1 <= int(m.text) <= 100:
        await m.answer("Нужно число от 1 до 100.")
        return
    l["chance"] = int(m.text)
    await state.clear()
    await ask_duration(m)


async def ask_duration(msg: Message):
    kb = Kb(inline_keyboard=[[Btn(text=f"{n} мин", callback_data=f"du:{n}") for n in (1, 3, 5, 10, 15)]])
    await msg.answer("⏳ Сколько минут будет идти ивент? (люди пишут в чате и копят активность)", reply_markup=kb)


@dp.callback_query(F.data.startswith("du:"), ADMIN)
async def duration_cb(c: CallbackQuery):
    l = need_launch(c)
    if not l:
        await c.answer("Сессия устарела", show_alert=True)
        return
    l["minutes"] = int(c.data[3:])
    kb = Kb(inline_keyboard=[[Btn(text=f"{n}", callback_data=f"wn:{n}") for n in (1, 2, 3, 5)]])
    await c.message.answer(
        "🏆 Сколько подарков может упасть максимум?\n(шанс бросается отдельно на каждый подарок)",
        reply_markup=kb,
    )
    await c.answer()


@dp.callback_query(F.data.startswith("wn:"), ADMIN)
async def winners_cb(c: CallbackQuery):
    l = need_launch(c)
    if not l:
        await c.answer("Сессия устарела", show_alert=True)
        return
    l["winners"] = int(c.data[3:])
    mkb = Kb(inline_keyboard=[
        [Btn(text="💰 С баланса бота", callback_data="md:balance")],
        [Btn(text="🆓 Бесплатно через API", callback_data="md:api")],
    ])
    await c.message.answer("💳 Откуда брать подарки?", reply_markup=mkb)
    await c.answer()


@dp.callback_query(F.data.startswith("md:"), ADMIN)
async def mode_cb(c: CallbackQuery):
    l = need_launch(c)
    if not l or "winners" not in l:
        await c.answer("Сессия устарела", show_alert=True)
        return
    l["mode"] = c.data[3:]
    if l["mode"] == "api" and not FREE_API_URL:
        await c.answer("FREE_API_URL не задан в переменных Render", show_alert=True)
        return
    kb = Kb(inline_keyboard=[[Btn(text="🚀 Запустить", callback_data="go"), Btn(text="❌ Отмена", callback_data="stop")]])
    await c.message.answer(
        f"Проверь настройки:\n\n"
        f"💬 Чат: {l['title']}\n"
        f"🎯 Шанс: {l['chance']}%\n"
        f"⏳ Время: {l['minutes']} мин\n"
        f"🏆 Подарков максимум: {l['winners']}\n"
        f"🎁 Гифт: {S['min_stars']}–{S['max_stars']} ⭐\n"
        f"💬 Мин. сообщений: {S['min_msgs']}\n"
        f"💳 Источник: {'🆓 через API (бесплатно)' if l['mode'] == 'api' else '💰 баланс бота ' + str(await get_balance()) + ' ⭐'}",
        reply_markup=kb,
    )
    await c.answer()


@dp.callback_query(F.data == "stop", ADMIN)
async def stop_cb(c: CallbackQuery):
    launches.pop(c.from_user.id, None)
    await c.message.answer("Отменено.")
    await c.answer()


@dp.callback_query(F.data == "go", ADMIN)
async def go_cb(c: CallbackQuery):
    l = launches.pop(c.from_user.id, None)
    if not l or "mode" not in l:
        await c.answer("Сессия устарела, напиши /event в чате заново", show_alert=True)
        return
    cid = l["chat_id"]
    if cid in events:
        await c.answer("В чате уже идёт ивент", show_alert=True)
        return
    events[cid] = {"counts": {}, "names": {}, "last": {}}
    last_event[cid] = time.time()
    l["admin_id"] = c.from_user.id
    try:
        await bot.send_message(
            cid,
            f"🎉🎉🎉 ЗАПУСТИЛСЯ ИВЕНТ 🎉🎉🎉\n\n"
            f"👑 Запустил: {l['admin']}\n\n"
            f"💬 Пиши и активничай в чате, чтобы получить награду!\n"
            f"🎁 Приз: подарок на {S['min_stars']}–{S['max_stars']} ⭐\n"
            f"⏳ Ивент идёт {l['minutes']} мин\n"
            f"📝 Нужно минимум {S['min_msgs']} сообщений, чем активнее, тем выше шанс!\n\n"
            f"Удачи! 🍀",
        )
    except Exception as e:
        events.pop(cid, None)
        await c.message.answer(f"Не смог написать в чат: {e}\nПроверь, что бот добавлен в чат.")
        await c.answer()
        return
    await c.message.answer("🚀 Ивент запущен!")
    await c.answer()
    asyncio.create_task(finish_event(cid, l))


# ===== Подсчёт активности (регистрируется после команд) =====
@dp.message(GROUP, F.from_user, ~F.from_user.is_bot)
async def count_msg(m: Message):
    ev = events.get(m.chat.id)
    uid = m.from_user.id
    if not ev or uid in ADMINS:
        return
    now = time.time()
    if now - ev["last"].get(uid, 0) < 5:  # антифлуд: считаем не чаще раза в 5 сек
        return
    ev["last"][uid] = now
    ev["counts"][uid] = ev["counts"].get(uid, 0) + 1
    ev["names"][uid] = who(m.from_user)


def wins_today(uid: int) -> int:
    d, n = wins.get(uid, (None, 0))
    return n if d == date.today() else 0


async def congratulate(uid: int, g: dict, title: str):
    try:
        await bot.send_message(
            uid,
            f"🎉 Поздравляю! Ты получил {g['emoji']} {gift_word(g)} за {g['stars']} ⭐\n"
            f"Награда за активность в чате «{title}». Проверь подарки в своём профиле 💙",
        )
    except Exception:
        pass  # человек не запускал бота, напишем только в чате


async def finish_event(cid: int, l: dict):
    await asyncio.sleep(l["minutes"] * 60)
    ev = events.pop(cid, None)
    if not ev:
        return

    eligible = [
        u for u, n in ev["counts"].items()
        if n >= S["min_msgs"] and wins_today(u) < S["daily_limit"]
    ]
    if not eligible:
        await bot.send_message(cid, "⌛ Ивент закончился. Никто не набрал нужную активность 😢")
        return

    free = l.get("mode") == "api"
    balance_now = 10**9 if free else await get_balance()
    gifts = await load_pool()

    won, failed, no_money = [], [], False
    pool = eligible[:]
    for _ in range(l["winners"]):
        if not pool:
            break
        if random.random() * 100 >= l["chance"]:
            continue
        uid = random.choices(pool, weights=[min(ev["counts"][u], 20) for u in pool])[0]
        pool.remove(uid)
        suitable = [g for g in gifts if S["min_stars"] <= g["stars"] <= min(S["max_stars"], balance_now)]
        if not suitable:
            no_money = True
            break
        g = random.choice(suitable)
        try:
            if free:
                await api_send_gift(uid, g["id"])
            else:
                await bot.send_gift(user_id=uid, gift_id=g["id"])
            balance_now -= g["stars"]
            wins[uid] = (date.today(), wins_today(uid) + 1)
            await save_win(uid)
            won.append((ev["names"][uid], g))
            await congratulate(uid, g, l["title"])
        except Exception as e:
            failed.append((ev["names"][uid], str(e)))

    if won:
        lines = "\n".join(f"🎁 {n} получает {g['emoji']} {gift_word(g)} ({g['stars']} ⭐)" for n, g in won)
        await bot.send_message(cid, f"🏁 Ивент завершён!\n\nПодарки получают:\n{lines}\n\nСпасибо всем за активность! 💙")
    else:
        await bot.send_message(cid, "🏁 Ивент завершён. В этот раз подарок не выпал 😔 Следующий ивент будет скоро!")

    # отчёт админу в личку
    report = []
    if no_money:
        report.append("⚠️ Подарок выпал, но нет подходящего подарка или не хватило звёзд. Пополни: /topup или проверь /gifts")
    for n, err in failed:
        report.append(f"⚠️ Не удалось отправить {n}: {err}\n(человек должен хотя бы раз запустить бота в личке)")
    if report:
        try:
            await bot.send_message(l["admin_id"], "\n\n".join(report))
        except Exception:
            pass


# ===== Веб-сервер для Render + запуск =====
async def health(_):
    return web.Response(text="ok")


async def main():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 10000))).start()
    await init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
