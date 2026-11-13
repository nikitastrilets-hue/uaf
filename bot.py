import asyncio
import logging
import os
from pathlib import Path
from dotenv import load_dotenv
import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    LabeledPrice, PreCheckoutQuery, BotCommand, BotCommandScopeChat
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "@admin")

# --- ПУТЬ К БАЗЕ ДЛЯ BOTHOST (постоянное хранилище /app/data) ---
DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = str(DATA_DIR / "gapar.db")

CURRENCY = "чётки"
RATE = 100
COMM_BOT = 0.40
COMM_GIFT = 0.25
COMM_WITHDRAW = 0.25
REF_BONUS = 10
MIN_WITHDRAW = 2000
MAX_WITHDRAW_STARS = 100

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)


# ---------- БАЗА ----------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            balance INTEGER DEFAULT 0,
            referrer_id INTEGER DEFAULT NULL,
            is_banned INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS tasks (
            task_id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_type TEXT,
            chat_id TEXT,
            title TEXT,
            url TEXT,
            price INTEGER,
            is_active INTEGER DEFAULT 1
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS completed (
            user_id INTEGER,
            task_id INTEGER,
            PRIMARY KEY (user_id, task_id)
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS pending (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            kind TEXT,
            task_id INTEGER DEFAULT NULL,
            stars INTEGER DEFAULT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS deposits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount INTEGER,
            source TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            coins INTEGER,
            stars INTEGER,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        await db.commit()


async def get_user(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (uid,))
        return await cur.fetchone()


async def get_balance(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT balance FROM users WHERE user_id = ?", (uid,))
        row = await cur.fetchone()
        return row[0] if row else 0


async def add_balance(uid, amount):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, uid))
        await db.commit()


# ---------- СОСТОЯНИЯ ----------
class AdminStates(StatesGroup):
    waiting_type = State()
    waiting_chat_id = State()
    waiting_title = State()
    waiting_url = State()
    waiting_price = State()


class WithdrawState(StatesGroup):
    waiting_amount = State()


# ---------- МЕНЮ ----------
def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Заработать чётки", callback_data="earn")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton(text="💸 Пополнить баланс", callback_data="deposit")],
        [InlineKeyboardButton(text="💳 Вывод звёзд", callback_data="withdraw")],
        [InlineKeyboardButton(text="👥 Рефералка", callback_data="ref")],
    ])


def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])


# ---------- КОМАНДЫ ДЛЯ АДМИНА ----------
async def set_admin_commands():
    commands = [
        BotCommand(command="start", description="Запустить бота"),
        BotCommand(command="admin", description="Админ-панель"),
        BotCommand(command="tasks", description="Список заданий"),
        BotCommand(command="pending", description="Ожидают подтверждения"),
        BotCommand(command="approve", description="/approve user_id task_id"),
        BotCommand(command="confirm", description="/confirm user_id звёзды"),
        BotCommand(command="give", description="/give user_id сумма"),
        BotCommand(command="paid", description="/paid заявка_id"),
    ]
    try:
        await bot.set_my_commands(commands, scope=BotCommandScopeChat(chat_id=ADMIN_ID))
    except Exception as e:
        logging.warning(f"Не смог поставить команды: {e}")


# ---------- СТАРТ ----------
@router.message(CommandStart())
async def cmd_start(message: Message):
    uid = message.from_user.id
    user = await get_user(uid)

    if user and user[3]:
        return await message.answer("Ты забанен, петушок. Иди гуляй, тут тебе не рады.")

    referrer_id = None
    args = message.text.split(maxsplit=1)
    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            rid = int(args[1][4:])
            if rid != uid:
                referrer_id = rid
        except ValueError:
            pass

    if not user:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT INTO users (user_id, referrer_id) VALUES (?, ?)", (uid, referrer_id))
            await db.commit()
        if referrer_id:
            ref = await get_user(referrer_id)
            if ref:
                await add_balance(referrer_id, REF_BONUS)
                try:
                    await bot.send_message(referrer_id, f"🎉 По твоей ссылке зашёл новый петушок. +{REF_BONUS} {CURRENCY} тебе.")
                except Exception:
                    pass

    bal = await get_balance(uid)
    text = (
        f"Здарова, {message.from_user.first_name or 'братан'}! Чё надо? Бабки есть? "
        f"Если нет — хуй тебе, а не задания. На счету у тя: {bal} {CURRENCY}. "
        f"Чё смотришь? Начинай вкалывать, петушок."
    )
    await message.answer(text, reply_markup=main_menu())


@router.message(Command("myid"))
async def myid(message: Message):
    await message.answer(f"Твой ID: `{message.from_user.id}`", parse_mode="Markdown")


@router.callback_query(F.data == "back_to_menu")
async def back_to_menu(call: CallbackQuery):
    await call.answer()
    await call.message.edit_text("Чё ещё надо? Выбирай давай, некогда мне тут с тобой лясы точить.", reply_markup=main_menu())


# ---------- ПРОФИЛЬ ----------
@router.callback_query(F.data == "profile")
async def show_profile(call: CallbackQuery):
    bal = await get_balance(call.from_user.id)
    text = f"Твой баланс: {bal} {CURRENCY}. Чё, мало? Иди работай, хули."
    await call.answer()
    await call.message.edit_text(text, reply_markup=back_kb())


# ---------- РЕФЕРАЛКА ----------
@router.callback_query(F.data == "ref")
async def ref_menu(call: CallbackQuery):
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{call.from_user.id}"
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM users WHERE referrer_id = ?", (call.from_user.id,))
        row = await cur.fetchone()
        count = row[0] if row else 0
    text = (
        f"👥 Твоя реферальная ссылка:\n`{link}`\n\n"
        f"За каждого приведённого петушка — {REF_BONUS} {CURRENCY}.\n"
        f"Уже привёл: {count} шт."
    )
    await call.answer()
    await call.message.edit_text(text, parse_mode="Markdown", reply_markup=back_kb())


# ---------- ЗАДАНИЯ ----------
@router.callback_query(F.data == "earn")
async def show_tasks(call: CallbackQuery):
    uid = call.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT * FROM tasks WHERE is_active = 1 AND task_id NOT IN "
            "(SELECT task_id FROM completed WHERE user_id = ?) ORDER BY task_id LIMIT 1", (uid,)
        )
        task = await cur.fetchone()

    if not task:
        await call.answer("Заданий больше нет, петушок. Жди, пока админ подкинет.", show_alert=True)
        return

    tid, ttype, chat_id, title, url, price, _ = task
    text = f"📋 {title}\n\nНаграда: {price} {CURRENCY}\n\nЖми, выполняй, потом проверяй."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Перейти", url=url)],
        [InlineKeyboardButton(text="✅ Проверить", callback_data=f"check_{tid}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")],
    ])
    await call.answer()
    await call.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data.startswith("check_"))
async def check_task(call: CallbackQuery):
    tid = int(call.data.split("_")[1])
    uid = call.from_user.id

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT task_type, chat_id, price FROM tasks WHERE task_id = ? AND is_active = 1", (tid,))
        task = await cur.fetchone()
        cur = await db.execute("SELECT 1 FROM completed WHERE user_id = ? AND task_id = ?", (uid, tid))
        if await cur.fetchone():
            return await call.answer("Ты уже делал это задание, петушок. Хватит.", show_alert=True)

    if not task:
        return await call.answer("Задание удалено, петушок.", show_alert=True)

    ttype, chat_id, price = task

    if ttype == "channel":
        try:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=uid)
            ok = member.status in ("member", "administrator", "creator")
        except Exception as e:
            logging.warning(f"get_chat_member упал: {e}")
            return await call.answer(
                "Бот не может проверить подписку. Скажи админу, пусть добавит бота админом в канал.",
                show_alert=True
            )
        if not ok:
            return await call.answer("Ты не подписан, петушок. Иди подпишись и возвращайся.", show_alert=True)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR IGNORE INTO completed (user_id, task_id) VALUES (?, ?)", (uid, tid))
            await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (price, uid))
            await db.commit()

        await call.answer(f"+{price} {CURRENCY} на счёт, братан!", show_alert=True)
        await call.message.edit_text(f"✅ Задание выполнено. +{price} {CURRENCY}.", reply_markup=main_menu())

    elif ttype == "manual":
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT INTO pending (user_id, kind, task_id) VALUES (?, 'task', ?)", (uid, tid))
            await db.commit()
        await call.answer("Заявка отправлена админу. Жди, петушок.", show_alert=True)
        await bot.send_message(
            ADMIN_ID,
            f"🔔 Юзер @{call.from_user.username or '—'} (ID: {uid}) говорит, что выполнил задание #{tid}.\n"
            f"Подтверди: /approve {uid} {tid}"
        )
        await call.message.edit_text("⏳ Ждём подтверждения от админа.", reply_markup=main_menu())


# ---------- ПОПОЛНЕНИЕ ----------
@router.callback_query(F.data == "deposit")
async def deposit(call: CallbackQuery):
    text = (
        "Выбирай, как бабки заносить:\n\n"
        f"💸 **Через бота:** комиссия 40%. Кидаешь звёзды — бот начисляет {CURRENCY} по курсу.\n"
        f"🎁 **Подарком админу:** комиссия 25%. Кидаешь подарок — админ вручную подтвердит.\n\n"
        f"Курс: 1 звезда = {RATE} {CURRENCY}."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💸 Звёзды боту (комиссия 40%)", callback_data="pay_bot")],
        [InlineKeyboardButton(text="🎁 Подарок админу (комиссия 25%)", callback_data="pay_gift")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")],
    ])
    await call.answer()
    await call.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")


@router.callback_query(F.data == "pay_bot")
async def pay_bot(call: CallbackQuery):
    await call.answer()
    stars = 10
    after = int(stars * (1 - COMM_BOT) * RATE)
    await bot.send_invoice(
        chat_id=call.from_user.id,
        title="Пополнение баланса",
        description=f"{stars} звёзд → {after} {CURRENCY} (комиссия 40%)",
        payload=f"deposit_{stars}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="Пополнение", amount=stars)],
    )


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def payment_ok(message: Message):
    stars = message.successful_payment.total_amount
    coins = int(stars * (1 - COMM_BOT) * RATE)
    await add_balance(message.from_user.id, coins)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO deposits (user_id, amount, source) VALUES (?, ?, 'bot')",
                         (message.from_user.id, coins))
        await db.commit()
    await message.answer(
        f"✅ Закинул {stars} звёзд. После комиссии 40% начислил {coins} {CURRENCY}. Не прогадал, петушок."
    )


@router.callback_query(F.data == "pay_gift")
async def pay_gift(call: CallbackQuery):
    await call.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="15 ⭐", callback_data="gift_15")],
        [InlineKeyboardButton(text="25 ⭐", callback_data="gift_25")],
        [InlineKeyboardButton(text="50 ⭐", callback_data="gift_50")],
        [InlineKeyboardButton(text="100 ⭐", callback_data="gift_100")],
        [InlineKeyboardButton(text="Больше 100 (вручную)", callback_data="gift_more")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")],
    ])
    await call.message.edit_text("Выбери, сколько звёзд кидаешь подарком:", reply_markup=kb)


@router.callback_query(F.data.startswith("gift_"))
async def gift_choose(call: CallbackQuery):
    if call.data == "gift_more":
        text = (
            f"Кидай подарок вручную сюда: {ADMIN_USERNAME}\n"
            f"В комментарии напиши свой ID: `{call.from_user.id}`\n\n"
            f"Админ сам решит, сколько звёзд засчитать, и начислит."
        )
        return await call.message.edit_text(text, parse_mode="Markdown", reply_markup=back_kb())

    stars = int(call.data.split("_")[1])
    text = (
        f"Кидай подарок на **{stars} ⭐** вот сюда: {ADMIN_USERNAME}\n\n"
        f"В комментарии к подарку напиши свой ID: `{call.from_user.id}`\n\n"
        f"После отправки жми «Я отправил» — админ проверит и начислит "
        f"{int(stars * (1 - COMM_GIFT) * RATE)} {CURRENCY}."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я отправил", callback_data=f"giftsent_{stars}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="deposit")],
    ])
    await call.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")


@router.callback_query(F.data.startswith("giftsent_"))
async def gift_sent(call: CallbackQuery):
    stars = int(call.data.split("_")[1])
    uid = call.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO pending (user_id, kind, stars) VALUES (?, 'gift', ?)", (uid, stars))
        await db.commit()
    await call.answer("Заявка отправлена админу. Жди.", show_alert=True)
    await bot.send_message(
        ADMIN_ID,
        f"🎁 Юзер @{call.from_user.username or '—'} (ID: {uid}) говорит, что кинул подарок на {stars} звёзд.\n"
        f"Подтверди: /confirm {uid} {stars}"
    )
    await call.message.edit_text("⏳ Ждём подтверждения от админа.", reply_markup=main_menu())


# ---------- ВЫВОД ----------
@router.callback_query(F.data == "withdraw")
async def withdraw_menu(call: CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    bal = await get_balance(uid)

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT SUM(amount) FROM deposits WHERE user_id = ?", (uid,))
        row = await cur.fetchone()
        deposited = row[0] or 0

    if deposited > 0:
        await call.answer(
            "Ты закинул бабки, петушок. Их вывести сразу нельзя — сначала сыграй в рулетку. "
            "Игры скоро будут, жди.",
            show_alert=True
        )
        return

    if bal < MIN_WITHDRAW:
        await call.answer(f"Минимум {MIN_WITHDRAW} {CURRENCY} для вывода. У тебя {bal}. Иди зарабатывай.", show_alert=True)
        return

    await call.answer()
    await call.message.edit_text(
        f"Сколько {CURRENCY} выводишь? У тебя {bal}.\n"
        f"Минимум: {MIN_WITHDRAW} (это 15 звёзд после комиссии 25%).\n"
        f"Максимум: {MAX_WITHDRAW_STARS} звёзд за раз.\n\n"
        f"Введи сумму {CURRENCY}:"
    )
    await state.set_state(WithdrawState.waiting_amount)


@router.message(WithdrawState.waiting_amount)
async def withdraw_amount(message: Message, state: FSMContext):
    uid = message.from_user.id
    try:
        coins = int(message.text)
    except ValueError:
        return await message.answer("Цифру введи, петушок.")

    bal = await get_balance(uid)

    if coins < MIN_WITHDRAW:
        return await message.answer(f"Минимум {MIN_WITHDRAW} {CURRENCY}, хули.")
    if coins > bal:
        return await message.answer(f"У тебя только {bal} {CURRENCY}. Губу закатай.")

    stars = int(coins / RATE * (1 - COMM_WITHDRAW))
    if stars < 15:
        return await message.answer("После комиссии меньше 15 звёзд — мало. Добавь чёток.")
    if stars > MAX_WITHDRAW_STARS:
        return await message.answer(f"Максимум {MAX_WITHDRAW_STARS} звёзд за раз. Уменьши сумму.")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (coins, uid))
        cur = await db.execute(
            "INSERT INTO withdrawals (user_id, coins, stars, status) VALUES (?, ?, ?, 'pending')",
            (uid, coins, stars)
        )
        await db.commit()
        wid = cur.lastrowid

    await message.answer(
        f"✅ Заявка на вывод создана.\n"
        f"Списано: {coins} {CURRENCY}.\n"
        f"Получишь: {stars} звёзд (после комиссии 25%).\n\n"
        f"⏳ В течение 15 минут админ отправит тебе подарок. "
        f"Если не пришло — напиши сюда: {ADMIN_USERNAME}\n"
        f"Номер заявки: #{wid}"
    )

    await bot.send_message(
        ADMIN_ID,
        f"💸 ЗАЯВКА НА ВЫВОД #{wid}\n"
        f"Юзер @{message.from_user.username or '—'} (ID: {uid})\n"
        f"Списано: {coins} {CURRENCY}\n"
        f"Отправь ему подарок на: {stars} ⭐\n\n"
        f"После отправки подтверди: /paid {wid}"
    )
    await state.clear()


@router.message(Command("paid"))
async def mark_paid(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) != 2:
        return await message.answer("Формат: /paid заявка_id")
    try:
        wid = int(args[1])
    except ValueError:
        return await message.answer("Число нужно.")

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id, stars FROM withdrawals WHERE id = ? AND status = 'pending'", (wid,))
        row = await cur.fetchone()
        if not row:
            return await message.answer("Нет такой заявки или уже выплачена.")
        uid, stars = row
        await db.execute("UPDATE withdrawals SET status = 'paid' WHERE id = ?", (wid,))
        await db.commit()

    await message.answer(f"✅ Заявка #{wid} закрыта. Отправлено {stars} звёзд юзеру {uid}.")
    try:
        await bot.send_message(uid, f"✅ Подарок отправлен! Заявка #{wid} выполнена. Проверь свои подарки.")
    except Exception:
        pass


# ---------- АДМИНКА ----------
@router.message(Command("admin"))
async def admin_panel(message: Message):
    if message.from_user.id != ADMIN_ID:
        return await message.answer("Иди нахуй, петушок. Не твоё это дело.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить задание", callback_data="add_task")],
    ])
    await message.answer("Здарова, босс. Чё делать будем?", reply_markup=kb)


@router.message(Command("tasks"))
async def list_tasks(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT task_id, task_type, title, price, is_active FROM tasks ORDER BY task_id")
        rows = await cur.fetchall()
    if not rows:
        return await message.answer("Заданий нет, босс.")
    out = "\n".join(
        f"#{r[0]} [{r[1]}] {r[2]} — {r[3]} {CURRENCY} — {'✅' if r[4] else '❌'}"
        for r in rows
    )
    await message.answer(f"Задания:\n{out}")


@router.message(Command("pending"))
async def list_pending(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, user_id, kind, task_id, stars FROM pending ORDER BY id")
        rows = await cur.fetchall()
    if not rows:
        return await message.answer("Заявок нет, босс.")
    out = []
    for r in rows:
        if r[2] == "task":
            out.append(f"#{r[0]} — task #{r[3]} от user {r[1]} → /approve {r[1]} {r[3]}")
        else:
            out.append(f"#{r[0]} — gift {r[4]}⭐ от user {r[1]} → /confirm {r[1]} {r[4]}")
    await message.answer("\n".join(out))


@router.callback_query(F.data == "add_task")
async def add_task_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await call.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Авто-проверка (канал)", callback_data="type_channel")],
        [InlineKeyboardButton(text="✋ Ручная проверка", callback_data="type_manual")],
    ])
    await call.message.edit_text("Тип задания:", reply_markup=kb)
    await state.set_state(AdminStates.waiting_type)


@router.callback_query(AdminStates.waiting_type, F.data.startswith("type_"))
async def add_task_type(call: CallbackQuery, state: FSMContext):
    ttype = call.data.replace("type_", "")
    await state.update_data(ttype=ttype)
    await call.answer()
    if ttype == "channel":
        await call.message.edit_text("Скинь chat_id канала (например @channel или -1001234567890). Бот должен быть админом!")
    else:
        await call.message.edit_text("Скинь chat_id (для ручной проверки можно любой текст-заглушку):")
    await state.set_state(AdminStates.waiting_chat_id)


@router.message(AdminStates.waiting_chat_id)
async def add_task_chat(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.update_data(chat_id=message.text.strip())
    await message.answer("Введи текст задания:")
    await state.set_state(AdminStates.waiting_title)


@router.message(AdminStates.waiting_title)
async def add_task_title(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.update_data(title=message.text)
    await message.answer("Скинь ссылку:")
    await state.set_state(AdminStates.waiting_url)


@router.message(AdminStates.waiting_url)
async def add_task_url(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.update_data(url=message.text.strip())
    await message.answer(f"Сколько {CURRENCY} давать? (цифру):")
    await state.set_state(AdminStates.waiting_price)


@router.message(AdminStates.waiting_price)
async def add_task_price(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        price = int(message.text)
    except ValueError:
        return await message.answer("Ты чё, ебанутый? Цифру введи.")
    data = await state.get_data()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO tasks (task_type, chat_id, title, url, price) VALUES (?, ?, ?, ?, ?)",
            (data["ttype"], data["chat_id"], data["title"], data["url"], price)
        )
        await db.commit()
        tid = cur.lastrowid
    await message.answer(f"✅ Задание #{tid} добавлено. Награда: {price} {CURRENCY}.")
    await state.clear()


@router.message(Command("approve"))
async def approve_task(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) != 3:
        return await message.answer("Формат: /approve user_id task_id")
    try:
        uid, tid = int(args[1]), int(args[2])
    except ValueError:
        return await message.answer("Числа нужны.")
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT price FROM tasks WHERE task_id = ?", (tid,))
        row = await cur.fetchone()
        if not row:
            return await message.answer("Нет такого задания.")
        price = row[0]
        await db.execute("INSERT OR IGNORE INTO completed (user_id, task_id) VALUES (?, ?)", (uid, tid))
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (price, uid))
        await db.execute("DELETE FROM pending WHERE user_id = ? AND kind = 'task' AND task_id = ?", (uid, tid))
        await db.commit()
    await message.answer(f"Выдал {price} {CURRENCY} юзеру {uid}.")
    try:
        await bot.send_message(uid, f"✅ Админ подтвердил задание #{tid}. +{price} {CURRENCY}.")
    except Exception:
        pass


@router.message(Command("confirm"))
async def confirm_gift(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) != 3:
        return await message.answer("Формат: /confirm user_id звёзды")
    try:
        uid, stars = int(args[1]), int(args[2])
    except ValueError:
        return await message.answer("Числа нужны.")
    coins = int(stars * (1 - COMM_GIFT) * RATE)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (coins, uid))
        await db.execute("INSERT INTO deposits (user_id, amount, source) VALUES (?, ?, 'gift')", (uid, coins))
        await db.execute("DELETE FROM pending WHERE user_id = ? AND kind = 'gift'", (uid,))
        await db.commit()
    await message.answer(f"Начислил {coins} {CURRENCY} юзеру {uid} (за {stars}⭐, комиссия 25%).")
    try:
        await bot.send_message(uid, f"✅ Админ подтвердил подарок {stars}⭐. +{coins} {CURRENCY}.")
    except Exception:
        pass


@router.message(Command("give"))
async def give_cmd(message: Message):
    if message.from_user.id != ADMIN_ID:
        return await message.answer("Иди нахуй.")
    args = message.text.split()
    if len(args) != 3:
        return await message.answer("Формат: /give user_id сумма")
    try:
        target, amount = int(args[1]), int(args[2])
    except ValueError:
        return await message.answer("Числа нужны, петушок.")
    await add_balance(target, amount)
    await message.answer(f"Выдал {amount} {CURRENCY} юзеру {target}.")
    try:
        await bot.send_message(target, f"Админ накинул тебе {amount} {CURRENCY}. Не подведи.")
    except Exception:
        pass


# ---------- ЗАПУСК ----------
async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    await set_admin_commands()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())