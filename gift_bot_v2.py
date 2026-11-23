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
    User,
)

# ===== Переменные окружения (задаются на Render) =====
BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ.get("DATABASE_URL", "")  # строка подключения из Neon
FREE_API_URL = os.environ.get("FREE_API_URL", "").rstrip("/")  # адрес твоего API для бесплатной отправки
API_URL = os.environ.get("API_URL", "http://31.77.10.54:18081").rstrip("/")  # адрес не-оригинального Bot API
PAYOUT_METHOD = os.environ.get("PAYOUT_METHOD", "sendGift").strip() or "sendGift"  # метод API для выдачи подарка
ADMINS = {int(x) for x in os.environ.get("ADMINS", "").replace(" ", "").split(",") if x}

bot = Bot(BOT_TOKEN, session=AiohttpSession(api=TelegramAPIServer.from_base(API_URL)))
dp = Dispatcher()

# ===== Настройки (меняются из бота через /settings) =====
SETTINGS_FILE = "settings.json"
S = {"min_stars": 15, "max_stars": 100, "min_msgs": 3, "cooldown_min": 30, "daily_limit": 1, "custom_gifts": [], "banned_gifts": []}
try:
    S.update(json.load(open(SETTINGS_FILE)))
except Exception:
    pass

LABELS = {
    "min_stars": "Мин. цена гифта (⭐)",
    "max_stars": "Макс. цена гифта (⭐)",
    "min_msgs": "Мин. сообщений в чате для участия",
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
    "/settings — настройки (цена гифтов, мин. сообщений, лимиты)\n"
    "/balance — баланс бота в звёздах\n"
    "/gifts — список подарков Telegram с их ID\n"
    "/addgift ID — добавить подарок в розыгрыш по ID\n"
    "/delgift ID — убрать добавленный подарок\n"
    "/ban ID — исключить подарок из розыгрыша (можно несколько ID через пробел)\n"
    "/unban ID — вернуть подарок (/unban all — вернуть все)\n"
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


async def raw_api(method: str, **params) -> dict:
    """Прямой вызов метода твоего API без проверки aiogram (формат ответов у сервера урезанный)."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as sess:
        async with sess.post(f"{API_URL}/bot{BOT_TOKEN}/{method}", json=params) as r:
            return await r.json(content_type=None)


async def get_balance() -> int:
    try:
        data = await raw_api("getMyStarBalance")
        print("getMyStarBalance:", data)  # смотри в логах Render, что реально отвечает сервер
        res = data.get("result", data)
        if isinstance(res, dict):
            for k in ("amount", "balance", "stars", "star_count"):
                if k in res:
                    return int(res[k])
        elif isinstance(res, (int, float)):
            return int(res)
    except Exception as e:
        print("Ошибка getMyStarBalance:", e)
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
    if key in ("min_stars", "max_stars") and not 1 <= v <= 100000:
        await m.answer("Цена гифта: от 1 до 100000 звёзд.")
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


async def raw_gifts() -> list:
    """Список подарков напрямую из твоего API (aiogram не принимает урезанный ответ сервера)."""
    from types import SimpleNamespace as NS
    data = await raw_api("getAvailableGifts")
    if not data.get("ok"):
        raise RuntimeError(data.get("description") or str(data)[:200])
    res = data.get("result", {})
    items = res.get("gifts", []) if isinstance(res, dict) else res
    out = []
    for g in items:
        st = g.get("sticker") or {}
        out.append(NS(
            id=str(g["id"]),
            star_count=int(g.get("star_count", 0)),
            total_count=g.get("total_count"),
            remaining_count=g.get("remaining_count"),
            sticker=NS(emoji=st.get("emoji") or "🎁"),
        ))
    return out


def pool_item(g) -> dict:
    st = getattr(g, "sticker", None)
    return {"id": str(g.id), "stars": g.star_count, "emoji": getattr(st, "emoji", None) or "🎁"}


async def load_pool() -> list[dict]:
    """Обычные подарки Telegram + те, что ты добавил через /addgift."""
    items: dict[str, dict] = {}
    try:
        gifts = (await raw_gifts())
    except Exception as e:
        print("Ошибка загрузки подарков:", e)
        gifts = []
    print("Подарков из API:", len(gifts))
    for g in gifts:
        if getattr(g, "remaining_count", None) == 0:  # лимитка распродана
            continue
        items[str(g.id)] = pool_item(g)
    for c in S["custom_gifts"]:
        items[str(c["id"])] = c
    banned = {str(x) for x in S.get("banned_gifts", [])}
    return [it for gid, it in items.items() if gid not in banned]


async def api_send_gift(user_id: int, gift_id: str):
    """Выдача подарка методом PAYOUT_METHOD твоего API. При ошибке показывает ответ сервера целиком."""
    timeout = aiohttp.ClientTimeout(total=20)
    url = f"{API_URL}/bot{BOT_TOKEN}/{PAYOUT_METHOD}"
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(url, json={"user_id": user_id, "gift_id": gift_id}) as r:
                text = await r.text()
                status = r.status
    except Exception as e:
        raise RuntimeError(f"нет связи с API {API_URL}: {e!r}")
    try:
        data = json.loads(text)
    except Exception:
        raise RuntimeError(f"{PAYOUT_METHOD}: HTTP {status}, ответ не JSON: {text[:300]}")
    if not data.get("ok"):
        raise RuntimeError(f"{PAYOUT_METHOD}(user_id={user_id}, gift_id={gift_id}): HTTP {status}, ответ сервера: {text[:400]}")


@dp.message(Command("gifts"), PRIVATE, ADMIN)
async def gifts_cmd(m: Message):
    try:
        gifts = (await raw_gifts())
    except Exception as e:
        await m.answer(f"Не получилось загрузить список: {e}")
        return
    added = {str(c["id"]) for c in S["custom_gifts"]}
    lines = []
    for g in sorted(gifts, key=lambda x: x.star_count):
        it = pool_item(g)
        lim = "" if getattr(g, "total_count", None) is None else " (лимитка)"
        mark = " ➕" if str(g.id) in added else ""
        if str(g.id) in {str(x) for x in S.get("banned_gifts", [])}:
            mark += " 🚫"
        lines.append(f"{it['emoji']} {g.star_count} ⭐{lim}{mark} — {g.id}")
    text = "🎁 Доступные подарки (➕ = добавлен тобой, 🚫 = исключён)\nВсе участвуют сами, кроме исключённых.\n\n"
    await m.answer(text + "\n".join(lines)[:3800] + "\n\nИсключить: /ban ID\nВернуть: /unban ID")


LAST_GROUP_MSG = {"t": 0, "chat": None, "title": None}


@dp.message(Command("diag"), PRIVATE, ADMIN)
async def diag_cmd(m: Message):
    """Проверяет всё по цепочке и пишет, где ломается."""
    out = [f"API_URL: {API_URL}", f"PAYOUT_METHOD: {PAYOUT_METHOD}", f"ADMINS: {sorted(ADMINS)} (твой id: {m.from_user.id})"]
    try:
        gifts = await raw_gifts()
        ok = [g for g in gifts if S["min_stars"] <= g.star_count <= S["max_stars"]]
        out.append(f"✅ Подарков из API: {len(gifts)}, в диапазоне {S['min_stars']}-{S['max_stars']} ⭐: {len(ok)}")
    except Exception as e:
        out.append(f"❌ getAvailableGifts: {e}")
    try:
        out.append(f"💰 getMyStarBalance: {json.dumps(await raw_api('getMyStarBalance'), ensure_ascii=False)[:200]}")
    except Exception as e:
        out.append(f"❌ getMyStarBalance: {e!r}")
    if LAST_GROUP_MSG["t"]:
        out.append(f"✅ Последнее сообщение из группы «{LAST_GROUP_MSG['title']}» бот видел {int(time.time() - LAST_GROUP_MSG['t'])} сек назад")
    else:
        out.append("❌ Бот НИ РАЗУ не получал сообщений из групп с момента запуска (проверь, что бот админ в чате и отключён privacy)")
    if events:
        for cid, ev in events.items():
            out.append(f"🎉 Идёт ивент в чате {cid}: подсчитано {dict((ev['names'].get(u, u), n) for u, n in ev['counts'].items())}, выдано {ev['given']}/{ev['l']['winners']}, шанс {ev['l']['chance']}%, подарков в пуле {len(ev['gifts'])}")
    else:
        out.append("ℹ️ Сейчас ивентов нет")
    out.append("\nТест выдачи подарка тебе: /testgift")
    await m.answer("\n".join(out)[:3900])


@dp.message(Command("testgift"), PRIVATE, ADMIN)
async def testgift_cmd(m: Message):
    """Отправляет подарок тебе самому и показывает ответ сервера."""
    parts = m.text.split()
    try:
        gifts = await raw_gifts()
    except Exception as e:
        await m.answer(f"❌ Не получил список подарков: {e}")
        return
    if len(parts) > 1:
        gid = parts[1]
    else:
        ok = [g for g in gifts if S["min_stars"] <= g.star_count <= S["max_stars"]]
        if not ok:
            await m.answer("❌ Нет подарков в диапазоне цен из /settings")
            return
        gid = min(ok, key=lambda g: g.star_count).id
    try:
        await api_send_gift(m.from_user.id, gid)
        await m.answer(f"✅ Сервер принял {PAYOUT_METHOD}, gift_id={gid}. Проверь подарки у себя в профиле.")
    except Exception as e:
        await m.answer(f"❌ Ошибка выдачи:\n{e}")


@dp.message(Command("probe"), PRIVATE, ADMIN)
async def probe_cmd(m: Message):
    """Проверяет, какой метод твоего API отдаёт список подарков с их ID."""
    out = []
    for name in ("getAvailableGifts", "getGifts", "getStarGifts", "getGiftList", "getAllGifts", "gifts", "getUserGifts"):
        try:
            data = await raw_api(name)
            out.append(f"{name}: {json.dumps(data, ensure_ascii=False)[:300]}")
        except Exception as e:
            out.append(f"{name}: ошибка {e}")
    await m.answer("\n\n".join(out)[:3800])


@dp.message(Command("ban"), PRIVATE, ADMIN)
async def ban_cmd(m: Message):
    parts = m.text.split()
    if len(parts) < 2:
        await m.answer("Напиши так: /ban ID_подарка\nСписок ID: /gifts")
        return
    ids = [str(x) for x in S.get("banned_gifts", [])]
    for gid in parts[1:]:
        if gid not in ids:
            ids.append(gid)
    S["banned_gifts"] = ids
    await save_settings()
    await m.answer(f"🚫 Исключено. Сейчас исключено подарков: {len(ids)}\nВернуть: /unban ID")


@dp.message(Command("unban"), PRIVATE, ADMIN)
async def unban_cmd(m: Message):
    parts = m.text.split()
    if len(parts) < 2:
        await m.answer("Напиши так: /unban ID_подарка (или /unban all, чтобы вернуть все)")
        return
    if parts[1] == "all":
        S["banned_gifts"] = []
    else:
        S["banned_gifts"] = [x for x in S.get("banned_gifts", []) if str(x) not in parts[1:]]
    await save_settings()
    await m.answer(f"✅ Готово. Исключено подарков: {len(S['banned_gifts'])}")


@dp.message(Command("addgift"), PRIVATE, ADMIN)
async def addgift_cmd(m: Message):
    parts = m.text.split()
    if len(parts) < 2:
        await m.answer("Напиши так: /addgift ID ЗВЁЗДЫ [эмодзи]\nНапример: /addgift 5170233102089322756 50 🧸")
        return
    gid = parts[1]
    item = None
    try:
        gifts = (await raw_gifts())
        g = next((x for x in gifts if str(x.id) == gid), None)
        if g:
            item = pool_item(g)
    except Exception:
        pass  # на твоём API getAvailableGifts может не быть - тогда вручную
    if not item:
        if len(parts) < 3 or not parts[2].isdigit():
            await m.answer("Список подарков недоступен, укажи цену сам:\n/addgift ID ЗВЁЗДЫ [эмодзи]")
            return
        item = {"id": gid, "stars": int(parts[2]), "emoji": parts[3] if len(parts) > 3 else "🎁"}
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
    l["admin_id"] = c.from_user.id
    events[cid] = {
        "counts": {}, "names": {}, "last": {}, "l": l,
        "rolled": set(), "given": 0, "won": [], "failed": [], "no_money": False,
        "gifts": await load_pool(),
    }
    last_event[cid] = time.time()
    try:
        await bot.send_message(
            cid,
            f"🎉🎉🎉 ЗАПУСТИЛСЯ ИВЕНТ 🎉🎉🎉\n\n"
            f"👑 Запустил: {l['admin']}\n\n"
            f"💬 Пиши и активничай в чате, чтобы получить награду!\n"
            f"🎁 Приз: подарок на {S['min_stars']}–{S['max_stars']} ⭐\n"
            f"⏳ Ивент идёт {l['minutes']} мин\n"
            f"📝 Нужно минимум {S['min_msgs']} сообщений. Подарки падают сразу, прямо во время ивента!\n\n"
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


# ===== Подсчёт активности и выдача подарков прямо во время ивента =====
def wins_today(uid: int) -> int:
    d, n = wins.get(uid, (None, 0))
    return n if d == date.today() else 0


async def congratulate(uid: int, g: dict, title: str):
    try:
        await bot.send_message(
            uid,
            "🎉🎉🎉 Поздравляю! Ты выиграл в раздаче! 🎉🎉🎉\n\n"
            f"🎁 Твой приз: {g['emoji']} {gift_word(g)} за {g['stars']} ⭐\n"
            f"💬 Награда за активность в чате «{title}»\n\n"
            "Подарок уже у тебя, загляни в сообщения и в профиль 💙\n"
            "Пиши в чате и участвуй в следующих ивентах, будет ещё! 🍀",
        )
    except Exception:
        pass  # человек не запускал бота, напишем только в чате


async def notify_admin(ev: dict, text: str):
    print("EVENT:", text)
    try:
        await bot.send_message(ev["l"]["admin_id"], text)
    except Exception:
        pass


async def try_award(cid: int, uid: int, ev: dict):
    """Человек набрал нужное число сообщений: один раз бросаем шанс и сразу дарим подарок."""
    l = ev["l"]
    name = ev["names"].get(uid, str(uid))
    if uid in ev["rolled"] or ev["given"] >= l["winners"]:
        return
    if uid not in ADMINS and wins_today(uid) >= S["daily_limit"]:
        ev["rolled"].add(uid)
        await notify_admin(ev, f"ℹ️ {name} набрал сообщения, но уже получил лимит подарков за сегодня ({S['daily_limit']}).")
        return
    ev["rolled"].add(uid)  # помечаем до первого await, чтобы не бросить дважды
    if random.random() * 100 >= l["chance"]:
        await notify_admin(ev, f"🎲 {name}: шанс {l['chance']}% не выпал.")
        return
    ev["given"] += 1  # бронируем место приза
    suitable = [g for g in ev["gifts"] if S["min_stars"] <= g["stars"] <= S["max_stars"]]
    if not suitable:
        ev["given"] -= 1
        ev["no_money"] = True
        await notify_admin(ev, f"⚠️ {name}: нет подходящих подарков в диапазоне {S['min_stars']}–{S['max_stars']} ⭐ (всего в пуле: {len(ev['gifts'])}). Проверь /gifts и /settings.")
        return
    g = random.choice(suitable)
    try:
        await api_send_gift(uid, g["id"])  # баланс проверяет сам сервер
    except Exception as e:
        ev["given"] -= 1
        ev["failed"].append((name, str(e)))
        await notify_admin(ev, f"⚠️ Не удалось отправить подарок {name}: {e}")
        return
    wins[uid] = (date.today(), wins_today(uid) + 1)
    await save_win(uid)
    ev["won"].append((name, g))
    try:
        await bot.send_message(cid, f"🎁 {name} получает {g['emoji']} {gift_word(g)} ({g['stars']} ⭐)! Так держать 💙")
    except Exception:
        pass
    await congratulate(uid, g, l["title"])


async def safe_award(cid: int, uid: int, ev: dict):
    try:
        await try_award(cid, uid, ev)
    except Exception as e:
        await notify_admin(ev, f"❌ Ошибка выдачи: {e!r}")


@dp.message(GROUP, F.from_user, ~F.from_user.is_bot)
async def count_msg(m: Message):
    LAST_GROUP_MSG.update(t=time.time(), chat=m.chat.id, title=m.chat.title)
    ev = events.get(m.chat.id)
    uid = m.from_user.id
    if not ev:
        return
    ev["counts"][uid] = ev["counts"].get(uid, 0) + 1
    ev["names"][uid] = who(m.from_user)
    if ev["counts"][uid] >= S["min_msgs"]:
        asyncio.create_task(safe_award(m.chat.id, uid, ev))


async def finish_event(cid: int, l: dict):
    await asyncio.sleep(l["minutes"] * 60)
    ev = events.pop(cid, None)
    if not ev:
        return

    if ev["won"]:
        lines = "\n".join(f"🎁 {n} — {g['emoji']} {gift_word(g)} ({g['stars']} ⭐)" for n, g in ev["won"])
        await bot.send_message(cid, f"🏁 Ивент завершён!\n\nПодарки получили:\n{lines}\n\nСпасибо всем за активность! 💙")
    else:
        await bot.send_message(cid, "🏁 Ивент завершён. В этот раз подарок никому не достался 😔 Следующий ивент будет скоро!")

    # отчёт админу в личку
    report = []
    if ev["no_money"]:
        report.append("⚠️ Подарок выпал, но нет подходящего подарка или не хватило звёзд. Пополни: /topup или проверь /gifts")
    for n, err in ev["failed"]:
        report.append(f"⚠️ Не удалось отправить {n}: {err}\n(человек должен хотя бы раз запустить бота в личке)")
    if report:
        try:
            await bot.send_message(l["admin_id"], "\n\n".join(report))
        except Exception:
            pass


# ===== Веб-сервер для Render + запуск =====
async def health(_):
    return web.Response(text="ok")


async def init_me():
    """Твой API отдаёт getMe урезанным (без новых полей), и aiogram падает на проверке.
    Берём getMe сами и кладём в кэш бота, чтобы aiogram не делал свой запрос."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
        async with s.get(f"{API_URL}/bot{BOT_TOKEN}/getMe") as r:
            data = await r.json()
    if not data.get("ok"):
        raise RuntimeError(f"getMe не прошёл: {data}")
    bot._me = User(**data["result"])
    print("Бот:", bot._me.username)


async def main():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 10000))).start()
    await init_me()
    await init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
