import asyncio
import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime
from typing import Dict, List, Optional

import aiosqlite
import gspread
import telegram.error
from google.oauth2.service_account import Credentials
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    ReplyKeyboardRemove,
    ReplyKeyboardMarkup,
    KeyboardButton,
    User
)
from telegram.ext import CallbackQueryHandler as CQH, MessageHandler as MH
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация
CONFIG = {
    "GOOGLE_SHEETS_CREDENTIALS": "credentials.json", # Файла нет в репозитории, т.к. там пристусвует скретная информация
    "CIVILIAN_SHEET_URL": "https://docs.google.com/spreadsheets/d/1_7xOrJWnV9Fzs8OhbrdUTkfuLfCd6uL8uJVf9837yjQ/edit",
    "BANK_SHEET_URL": "https://docs.google.com/spreadsheets/d/1sEsl_1GOOrqrq0tRNh1WrmzsoVH-8Lnl2GRGzzV2vo0/edit",
    "ROLES_SHEET_URL": "https://docs.google.com/spreadsheets/d/1mDlLMhev9irM1ZieFd5OPtBu5l3diD9pIqVeQdFTOWU/edit",
    "SYNC_INTERVAL": 1800,
    "ADMIN_NOTIFICATIONS_DIR": "admin_notifications",
    "BLACKLIST_FILE": "blacklist.json",
}

REGISTRATION, MC_NICKNAME, DISCORD_NICKNAME, BIRTHDAY, REGISTRATION_CONFIRM = range(5)
TASK_CREATION, TASK_NAME, TASK_TYPE, TASK_COUNT, TASK_COST, TASK_SOCIAL_TYPE, TASK_DEADLINE, TASK_DESCRIPTION = range(8)
TASK_REPORT, TASK_SELECT, TASK_PROOF, TASK_EDIT_PARAM = range(4)
TRANSFER, TRANSFER_RECIPIENT, TRANSFER_AMOUNT, TRANSFER_CONFIRM, TRANSFER_COMMENT, TRANSFER_SELECT_USER = range(6)
DEPOSIT, DEPOSIT_USER, DEPOSIT_AMOUNT, DEPOSIT_REASON = range(4)
WITHDRAW, WITHDRAW_USER, WITHDRAW_AMOUNT, WITHDRAW_REASON = range(4)
EXCHANGE, EXCHANGE_AMOUNT, EXCHANGE_USER = range(3)
ADMIN_ACTIONS = range(1)

# Роли пользователей
ROLES = {
    "guest": "Гость 👋",
    "resident": "Житель 🏠",
    "banker": "Банкир 💰",
    "admin": "Администратор 👑",
}

# Типы заданий
TASK_TYPES = {
    "mining": "Добыча ⛏️",
    "rebuilding": "Перестройка 🏗️",
    "farming": "Фарм 🌾",
    "other": "Другое ✨",
}

SOCIAL_TYPES = {
    "passive": "Пассивное 🔄",
    "active": "Активное 🔥",
    "individual": "Индивидуальное 🎯",
}


def init_google_sheets():
    try:
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        credentials = Credentials.from_service_account_file(
            CONFIG["GOOGLE_SHEETS_CREDENTIALS"], scopes=scopes
        )
        gc = gspread.authorize(credentials)
        return gc
    except Exception as e:
        logger.error(f"Ошибка инициализации Google Sheets: {e}")
        return None


async def sync_with_google_sheets(context: ContextTypes.DEFAULT_TYPE = None):
    try:
        gc = gspread.service_account(filename=CONFIG["GOOGLE_SHEETS_CREDENTIALS"])

        sh = gc.open_by_url(CONFIG["CIVILIAN_SHEET_URL"])
        worksheet = sh.worksheet("Team")
        records = worksheet.get_all_records()

        async with aiosqlite.connect("civilian.db") as db:
            await db.execute("DELETE FROM civilians")
            for row in records:
                if row.get("is_resident", "").upper() == "TRUE":
                    await db.execute(
                        """INSERT INTO civilians (id, nickname, discord, telegram_uid, role)
                        VALUES (?, ?, ?, ?, ?)""",
                        (row["id"], row["nickname"], row.get("discord"),
                         row.get("telegram"), "resident")
                    )
            await db.commit()

        logger.info(f"Синхронизировано {len(records)} записей горожан")
        return True
    except Exception as e:
        logger.error(f"Ошибка синхронизации: {str(e)}")
        return False


# Инициализация баз данных
async def init_databases():
    try:
        os.makedirs(CONFIG["ADMIN_NOTIFICATIONS_DIR"], exist_ok=True)

        async with aiosqlite.connect("civilian.db") as db:
            await db.execute(
                """CREATE TABLE IF NOT EXISTS civilians (
                    id TEXT PRIMARY KEY,
                    nickname TEXT NOT NULL,
                    discord TEXT,
                    telegram_uid TEXT,
                    role TEXT DEFAULT 'civilian'
                )"""
            )
            await db.commit()

        await sync_with_google_sheets()

        async with aiosqlite.connect("bank.db") as db:
            await db.execute(
                """CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY,
                    balance INTEGER DEFAULT 0,
                    salary INTEGER DEFAULT 0
                )"""
            )
            await db.execute(
                """CREATE TABLE IF NOT EXISTS transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT,
                    type TEXT,
                    date TEXT,
                    from_user TEXT,
                    to_user TEXT,
                    amount INTEGER,
                    comment TEXT
                )"""
            )
            await db.commit()

        async with aiosqlite.connect("tasks.db") as db:
            await db.execute(
                """CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    task_type TEXT,
                    count INTEGER,
                    cost INTEGER NOT NULL,
                    social_type TEXT NOT NULL,
                    deadline TEXT,
                    description TEXT,
                    assigned_to TEXT,
                    completed BOOLEAN DEFAULT FALSE
                )"""
            )
            await db.commit()

        logger.info("Базы данных успешно инициализированы и синхронизированы")
    except Exception as e:
        logger.error(f"Ошибка инициализации баз данных: {e}")
        raise


async def check_last_transaction():
    try:
        async with aiosqlite.connect("bank.db") as db:
            cursor = await db.execute(
                "SELECT * FROM transactions ORDER BY id DESC LIMIT 1"
            )
            last_trans = await cursor.fetchone()
            logger.info(f"Последняя транзакция в БД: {last_trans}")
    except Exception as e:
        logger.error(f"Ошибка проверки транзакций: {e}")


# Функции для работы с пользователями
async def get_user_role(telegram_uid: str) -> Optional[str]:
    async with aiosqlite.connect("civilian.db") as db:
        cursor = await db.execute(
            "SELECT role FROM civilians WHERE telegram_uid = ?", (telegram_uid,)
        )
        result = await cursor.fetchone()
        return result[0] if result else None


async def get_all_residents() -> List[Dict]:
    async with aiosqlite.connect("civilian.db") as db:
        cursor = await db.execute(
            "SELECT id, nickname, telegram_uid FROM civilians WHERE role = 'resident'"
        )
        results = await cursor.fetchall()
        return [{"id": row[0], "nickname": row[1], "telegram_uid": row[2]} for row in results]


async def get_admin_ids() -> List[str]:
    async with aiosqlite.connect("civilian.db") as db:
        cursor = await db.execute(
            "SELECT telegram_uid FROM civilians WHERE role = 'admin'"
        )
        results = await cursor.fetchall()
        return [result[0] for result in results if result[0]]


# Функции для работы с банком
async def get_balance(telegram_uid: str) -> int:
    async with aiosqlite.connect("civilian.db") as db:
        cursor = await db.execute(
            "SELECT id FROM civilians WHERE telegram_uid = ?", (telegram_uid,)
        )
        result = await cursor.fetchone()
        if not result:
            return 0

        user_id = result[0]

    async with aiosqlite.connect("bank.db") as db:
        cursor = await db.execute(
            "SELECT balance FROM accounts WHERE id = ?", (user_id,)
        )
        result = await cursor.fetchone()
        return result[0] if result else 0


async def deposit_money(user_id: str, amount: int, reason: str = "") -> bool:
    if amount <= 0:
        return False

    async with aiosqlite.connect("bank.db") as db:
        await db.execute(
            "UPDATE accounts SET balance = balance + ? WHERE id = ?",
            (amount, user_id)
        )

        await db.execute(
            """INSERT INTO transactions 
            (user_id, type, date, to_user, amount, comment)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, "deposit", datetime.now().isoformat(), user_id, amount, reason)
        )

        await db.commit()
    return True


async def withdraw_money(user_id: str, amount: int, reason: str = "") -> bool:
    if amount <= 0:
        return False

    async with aiosqlite.connect("bank.db") as db:
        cursor = await db.execute(
            "SELECT balance FROM accounts WHERE id = ?", (user_id,)
        )
        balance = await cursor.fetchone()
        if not balance or balance[0] < amount:
            return False

        await db.execute(
            "UPDATE accounts SET balance = balance - ? WHERE id = ?",
            (amount, user_id)
        )

        await db.execute(
            """INSERT INTO transactions 
            (user_id, type, date, from_user, amount, comment)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, "withdraw", datetime.now().isoformat(), user_id, amount, reason)
        )

        await db.commit()
    return True


async def transfer_money(from_uid: str, to_id: str, amount: int, comment: str = "") -> bool:
    logger.info(f"Начало перевода: from_uid={from_uid}, to_id={to_id}, amount={amount}, comment='{comment}'")

    if amount <= 0:
        logger.error("Сумма перевода должна быть положительной")
        return False

    try:
        async with aiosqlite.connect("civilian.db") as db:
            cursor = await db.execute(
                "SELECT id FROM civilians WHERE telegram_uid = ?",
                (from_uid,)
            )
            from_result = await cursor.fetchone()
            if not from_result:
                logger.error("Отправитель не найден в базе")
                return False
            from_id = from_result[0]

        async with aiosqlite.connect("bank.db") as db:
            cursor = await db.execute(
                "SELECT balance FROM accounts WHERE id = ?",
                (from_id,)
            )
            balance = (await cursor.fetchone())[0]
            if balance < amount:
                logger.error(f"Недостаточно средств: баланс {balance}, требуется {amount}")
                return False

            await db.execute(
                "UPDATE accounts SET balance = balance - ? WHERE id = ?",
                (amount, from_id)
            )
            await db.execute(
                "UPDATE accounts SET balance = balance + ? WHERE id = ?",
                (amount, to_id)
            )

            await db.execute(
                """INSERT INTO transactions 
                (user_id, type, date, from_user, to_user, amount, comment)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (from_id, "transfer", datetime.now().isoformat(), from_id, to_id, amount, comment)
            )
            await db.commit()

            logger.info("Перевод успешно выполнен")
            return True

    except Exception as e:
        logger.error(f"Ошибка при переводе: {str(e)}", exc_info=True)
        return False


async def find_user_by_nicknames(mc_nickname: str, discord_nickname: str) -> tuple:
    """Ищет пользователя по нику в майнкрафте и дискорде"""
    async with aiosqlite.connect("civilian.db") as db:
        cursor = await db.execute(
            "SELECT id, nickname, discord, telegram_uid FROM civilians WHERE nickname = ? AND discord = ?",
            (mc_nickname, discord_nickname)
        )
        full_match = await cursor.fetchall()

        cursor = await db.execute(
            "SELECT id, nickname, discord, telegram_uid FROM civilians WHERE nickname = ? OR discord = ?",
            (mc_nickname, discord_nickname)
        )
        partial_matches = await cursor.fetchall()

        return full_match, partial_matches


async def get_blacklist() -> List[Dict]:
    """Возвращает черный список, создает файл если его нет"""
    try:
        if not os.path.exists(CONFIG["BLACKLIST_FILE"]):
            with open(CONFIG["BLACKLIST_FILE"], "w") as f:
                json.dump([], f)
            return []

        with open(CONFIG["BLACKLIST_FILE"], "r") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Ошибка чтения черного списка: {e}")
        return []


async def add_to_blacklist(user_id: str, nickname: str, reason: str) -> bool:
    """Добавляет пользователя в черный список"""
    try:
        blacklist = await get_blacklist()
        blacklist.append({
            "id": user_id,
            "nickname": nickname,
            "reason": reason,
            "block_date": datetime.now().isoformat()
        })

        with open(CONFIG["BLACKLIST_FILE"], "w") as f:
            json.dump(blacklist, f, indent=2)
        return True
    except Exception as e:
        logger.error(f"Ошибка добавления в черный список: {e}")
        return False


async def remove_from_blacklist(user_id: str) -> bool:
    try:
        blacklist = await get_blacklist()
        blacklist = [user for user in blacklist if user["id"] != user_id]

        with open(CONFIG["BLACKLIST_FILE"], "w") as f:
            json.dump(blacklist, f)
        return True
    except Exception as e:
        logger.error(f"Ошибка удаления из черного списка: {e}")
        return False


async def is_blacklisted(user_id: str) -> bool:
    blacklist = await get_blacklist()
    return any(user["id"] == user_id for user in blacklist)


async def get_transactions(page: int = 0, limit: int = 10) -> List[Dict]:
    async with aiosqlite.connect("bank.db") as db:
        cursor = await db.execute(
            """SELECT * FROM transactions 
            ORDER BY date DESC
            LIMIT ? OFFSET ?""",
            (limit, page * limit)
        )
        results = await cursor.fetchall()

        transactions = []
        for row in results:
            transactions.append({
                "id": row[0],
                "user_id": row[1],
                "type": row[2],
                "date": row[3],
                "from_user": row[4],
                "to_user": row[5],
                "amount": row[6],
                "comment": row[7]
            })
        return transactions


async def get_user_info(user_id: str) -> Optional[Dict]:
    async with aiosqlite.connect("civilian.db") as db:
        cursor = await db.execute(
            "SELECT id, nickname, role FROM civilians WHERE id = ?", (user_id,)
        )
        result = await cursor.fetchone()
        if not result:
            return None

        return {
            "id": result[0],
            "nickname": result[1],
            "role": result[2]
        }


async def change_user_role(user_id: str, new_role: str) -> bool:
    if new_role not in ROLES:
        return False

    async with aiosqlite.connect("civilian.db") as db:
        await db.execute(
            "UPDATE civilians SET role = ? WHERE id = ?",
            (new_role, user_id)
        )
        await db.commit()
    return True


async def withdraw_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.edit_message_text("Введите ник в Minecraft или ID горожанина для снятия WVR:")
    return WITHDRAW_USER


async def withdraw_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    recipient = update.message.text
    context.user_data["withdraw_recipient"] = recipient

    async with aiosqlite.connect("civilian.db") as db:
        cursor = await db.execute(
            "SELECT id FROM civilians WHERE id = ? OR nickname LIKE ?",
            (recipient, f"%{recipient}%")
        )
        result = await cursor.fetchall()

        if not result:
            await update.message.reply_text("Житель не найден. Проверьте данные и попробуйте снова.")
            return ConversationHandler.END

        if len(result) > 1:
            keyboard = []
            for row in result:
                cursor = await db.execute(
                    "SELECT nickname FROM civilians WHERE id = ?",
                    (row[0],))
                nickname = await cursor.fetchone()
                keyboard.append([InlineKeyboardButton(
                    f"{nickname[0]} (ID: {row[0]})",
                    callback_data=f"withdraw_from_{row[0]}")
                ])

            keyboard.append([InlineKeyboardButton("Отмена ❌", callback_data="bank_operations")])
            await update.message.reply_text(
                "Найдено несколько жителей. Выберите нужного:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return ConversationHandler.END

        context.user_data["withdraw_user_id"] = result[0][0]
        await update.message.reply_text("Введите сумму для снятия:")
        return WITHDRAW_AMOUNT


async def withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = int(update.message.text)
        if amount <= 0:
            raise ValueError

        context.user_data["withdraw_amount"] = amount
        await update.message.reply_text("Введите причину снятия:")
        return WITHDRAW_REASON

    except ValueError:
        await update.message.reply_text("Неверная сумма. Введите целое положительное число:")
        return WITHDRAW_AMOUNT


async def withdraw_complete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reason = update.message.text
    user_id = context.user_data["withdraw_user_id"]
    amount = context.user_data["withdraw_amount"]

    success = await withdraw_money(user_id, amount, reason)

    if success:
        await update.message.reply_text(
            f"✅ Успешно снято {amount} WVR\n"
            f"Причина: {reason}")
    else:
        await update.message.reply_text(
            "❌ Не удалось выполнить операцию. Проверьте баланс и попробуйте снова.")

    return ConversationHandler.END


async def exchange_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.edit_message_text("Введите ник в Minecraft или ID горожанина для обналичивания WVR:")
    return EXCHANGE_USER


async def exchange_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    recipient = update.message.text

    async with aiosqlite.connect("civilian.db") as db:
        cursor = await db.execute(
            "SELECT id, telegram_uid FROM civilians WHERE id = ? OR nickname LIKE ?",
            (recipient, f"%{recipient}%")
        )
        result = await cursor.fetchall()

        if not result:
            await update.message.reply_text("Житель не найден. Проверьте данные и попробуйте снова.")
            return ConversationHandler.END

        if len(result) > 1:
            keyboard = []
            for row in result:
                cursor = await db.execute(
                    "SELECT nickname FROM civilians WHERE id = ?",
                    (row[0],)
                )
                nickname = await cursor.fetchone()
                keyboard.append([InlineKeyboardButton(
                    f"{nickname[0]} (ID: {row[0]})",
                    callback_data=f"exchange_for_{row[0]}")
                ])

                keyboard.append([InlineKeyboardButton("Отмена ❌", callback_data="bank_operations")])
                await update.message.reply_text(
                    "Найдено несколько жителей. Выберите нужного:",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            return ConversationHandler.END

        context.user_data["exchange_user_id"] = result[0][0]
        context.user_data["exchange_telegram_uid"] = result[0][1]
        await update.message.reply_text("Введите сумму для обналичивания:")
        return EXCHANGE_AMOUNT


async def exchange_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = int(update.message.text)
        if amount <= 0:
            raise ValueError

        user_id = context.user_data["exchange_user_id"]
        telegram_uid = context.user_data["exchange_telegram_uid"]

        success = await withdraw_money(user_id, amount, "Обналичивание в АРы")

        if success:
            try:
                await context.bot.send_message(
                    telegram_uid,
                    f"✅ Ваши {amount} WVR были обналичены в {amount} АР\n"
                    f"Операцию выполнил: @{update.effective_user.username}")
            except Exception as e:
                logger.error(f"Не удалось уведомить пользователя: {e}")

            await update.message.reply_text(
                f"✅ Успешно обналичено {amount} WVR в {amount} АР\n"
                "Пользователь был уведомлен.")
        else:
            await update.message.reply_text(
                "❌ Не удалось выполнить операцию. Проверьте баланс пользователя.")

    except ValueError:
        await update.message.reply_text("Неверная сумма. Введите целое положительное число:")
        return EXCHANGE_AMOUNT

    return ConversationHandler.END


# Функции для работы с заданиями
async def get_available_tasks() -> List[Dict]:
    async with aiosqlite.connect("tasks.db") as db:
        cursor = await db.execute(
            """SELECT id, name, task_type, cost, social_type, deadline, description 
            FROM tasks WHERE completed = FALSE AND (social_type = 'passive' OR social_type = 'active')"""
        )
        tasks = await cursor.fetchall()

        result = []
        for task in tasks:
            result.append({
                "id": task[0],
                "name": task[1],
                "type": task[2],
                "cost": task[3],
                "social_type": task[4],
                "deadline": task[5],
                "description": task[6],
            })

        return result


def get_reply_markup(include_cancel=False):
    keyboard = [[KeyboardButton("/start")]]
    if include_cancel:
        keyboard[0].append(KeyboardButton("/cancel"))
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)


# Обработчики команд
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    telegram_uid = str(user.id)

    if await is_blacklisted(telegram_uid):
        return

    role = await get_user_role(telegram_uid)

    if role is None:
        if await check_pending_application(telegram_uid):
            await update.message.reply_text(
                "🛑 Ваша заявка на рассмотрении. Пожалуйста, дождитесь решения администратора.",
                reply_markup=get_reply_markup()
            )
            return

        keyboard = [
            [InlineKeyboardButton("🖊️ Зарегистрироваться", callback_data="start_registration")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "Вы не зарегистрированы в системе. Хотите подать заявку на регистрацию?",
            reply_markup=reply_markup
        )
        return

    if role == "guest":
        keyboard = [
            [InlineKeyboardButton("Мой баланс 💰", callback_data="balance")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f"Привет, {user.first_name}! 👋\n"
            f"Твой статус: {ROLES.get(role, 'Гость')}\n"
            "Доступные действия:",
            reply_markup=reply_markup
        )
        return

    keyboard = [
        [InlineKeyboardButton("Мой баланс 💰", callback_data="balance")],
        [InlineKeyboardButton("Доступные задания 📋", callback_data="tasks")],
        [InlineKeyboardButton("Перевести WVR 🔄", callback_data="transfer")],
    ]

    if role in ["banker", "admin"]:
        keyboard.append([InlineKeyboardButton("Банковские операции 🏦", callback_data="bank_operations")])

    if role == "admin":
        keyboard.append([InlineKeyboardButton("Администрирование 👑", callback_data="admin_actions")])

    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"Привет, {user.first_name}! 👋\n"
        f"Твой статус: {ROLES.get(role, 'Гость')}\n"
        "Выбери действие:",
        reply_markup=reply_markup
    )


async def check_pending_application(telegram_uid: str) -> bool:
    """Проверяет, есть ли у пользователя активные заявки"""
    try:
        if not os.path.exists(CONFIG["ADMIN_NOTIFICATIONS_DIR"]):
            return False

        for filename in os.listdir(CONFIG["ADMIN_NOTIFICATIONS_DIR"]):
            if filename.startswith(f"app_{telegram_uid}_"):
                filepath = os.path.join(CONFIG["ADMIN_NOTIFICATIONS_DIR"], filename)
                try:
                    with open(filepath, "r") as f:
                        data = json.load(f)
                        if data.get("status") != "rejected":
                            return True
                except Exception as e:
                    logger.error(f"Ошибка чтения файла {filename}: {e}")
        return False
    except Exception as e:
        logger.error(f"Ошибка проверки заявок: {e}")
        return False


async def check_user_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if update.message and update.message.text in ['/start', '/cancel']:
        return True

    user_id = str(update.effective_user.id)

    if await is_blacklisted(user_id):
        return False

    if await check_pending_application(user_id):
        await update.message.reply_text(
            "Ваша заявка на рассмотрении. Пожалуйста, дождитесь решения администратора.",
            reply_markup=get_reply_markup()
        )
        return False

    role = await get_user_role(user_id)

    if role == "guest" and update.callback_query and update.callback_query.data != "balance":
        await update.callback_query.answer("У вас недостаточно прав для этого действия", show_alert=True)
        return False

    return True


async def start_registration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    telegram_uid = str(query.from_user.id)

    if await check_pending_application(telegram_uid):
        await query.edit_message_text(
            "❌ У вас уже есть заявка на рассмотрении. Пожалуйста, дождитесь решения администратора."
        )
        return ConversationHandler.END

    if await get_user_role(telegram_uid):
        await query.edit_message_text(
            "❌ Вы уже зарегистрированы в системе."
        )
        return ConversationHandler.END

    await query.edit_message_text(
        "Отлично! Давай начнем процесс регистрации.\n"
        "Пожалуйста, введи свой ник в Minecraft:"
    )
    return MC_NICKNAME


async def register_mc_nickname(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["mc_nickname"] = update.message.text
    await update.message.reply_text(
        "Хорошо! Теперь введи свой ник в Discord:",
        reply_markup=get_reply_markup(include_cancel=True)
    )
    return DISCORD_NICKNAME


async def register_discord_nickname(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["discord_nickname"] = update.message.text
    await update.message.reply_text(
        "Отлично! Теперь введи свою дату рождения в формате ДД.ММ.ГГГГ:",
        reply_markup=get_reply_markup(include_cancel=True)
    )
    return BIRTHDAY


async def register_birthday(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        datetime.strptime(update.message.text, "%d.%m.%Y")
        context.user_data["birthday"] = update.message.text

        full_match, partial_matches = await find_user_by_nicknames(
            context.user_data["mc_nickname"],
            context.user_data["discord_nickname"]
        )

        application_id = str(uuid.uuid4())
        application_data = {
            "application_id": application_id,
            "telegram_uid": str(update.effective_user.id),
            "mc_nickname": context.user_data["mc_nickname"],
            "discord_nickname": context.user_data["discord_nickname"],
            "birthday": context.user_data["birthday"],
            "timestamp": datetime.now().isoformat(),
            "status": "pending"
        }

        filename = f"{CONFIG['ADMIN_NOTIFICATIONS_DIR']}/app_{application_data['telegram_uid']}_{application_id}.json"
        with open(filename, "w") as f:
            json.dump(application_data, f)

        if full_match:
            await notify_admins(context, update.effective_user, application_data, "полное совпадение")
            await update.message.reply_text("✅ Обнаружено полное совпадение в БД. Заявка отправлена на рассмотрение")
        elif partial_matches:
            await notify_admins(context, update.effective_user, application_data, "частичное совпадение")
            await update.message.reply_text("⚠️ Обнаружены неточности. Заявка отправлена на рассмотрение.")
        else:
            await notify_admins(context, update.effective_user, application_data, "нет совпадений")
            await update.message.reply_text("❓ Совпадений в БД нет. Заявка отправлена на рассмотрение.")

        return ConversationHandler.END

    except ValueError:
        await update.message.reply_text("Неверный формат даты. Введите ДД.ММ.ГГГГ:")
        return BIRTHDAY


async def register_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    application_data = {
        "telegram_uid": str(update.effective_user.id),
        "mc_nickname": context.user_data["mc_nickname"],
        "discord_nickname": context.user_data["discord_nickname"],
        "birthday": context.user_data["birthday"],
        "timestamp": datetime.now().isoformat(),
        "status": "no_match_confirmed"
    }

    os.makedirs(CONFIG["ADMIN_NOTIFICATIONS_DIR"], exist_ok=True)
    filename = f"{CONFIG['ADMIN_NOTIFICATIONS_DIR']}/app_{update.effective_user.id}.json"

    with open(filename, "w") as f:
        json.dump(application_data, f)

    admins = await get_admin_ids()
    for admin_id in admins:
        try:
            keyboard = [
                [InlineKeyboardButton("✅ Одобрить", callback_data=f"register_approve_{update.effective_user.id}")],
                [InlineKeyboardButton("❌ Заблокировать", callback_data=f"register_block_{update.effective_user.id}")]
            ]

            await context.bot.send_message(
                admin_id,
                f"📨 Новая заявка от @{update.effective_user.username} (без совпадений в БД)\n"
                f"MC: {context.user_data['mc_nickname']}\n"
                f"Discord: {context.user_data['discord_nickname']}\n"
                f"Дата рождения: {context.user_data['birthday']}",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception as e:
            logger.error(f"Не удалось уведомить админа {admin_id}: {e}")

    await query.edit_message_text(
        "✅ Ваша заявка отправлена на рассмотрение администратору. "
        "Ожидайте ответа в течение 1-2 дней."
    )
    return ConversationHandler.END


async def register_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "Давайте начнем регистрацию заново.\n"
        "Пожалуйста, введите ваш ник в Minecraft:"
    )
    return MC_NICKNAME


async def handle_application_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action, application_id = query.data.split("_", 1)
    application_file = None

    for filename in os.listdir(CONFIG["ADMIN_NOTIFICATIONS_DIR"]):
        if f"_{application_id}.json" in filename:
            application_file = os.path.join(CONFIG["ADMIN_NOTIFICATIONS_DIR"], filename)
            break

    if not application_file:
        await query.edit_message_text("❌ Заявка не найдена")
        return

    with open(application_file, "r") as f:
        application_data = json.load(f)

    if action == "approve":
        async with aiosqlite.connect("civilian.db") as db:
            cursor = await db.execute(
                "SELECT id FROM civilians WHERE nickname = ? AND discord = ?",
                (application_data["mc_nickname"], application_data["discord_nickname"])
            )
            result = await cursor.fetchone()

            if not result:
                await query.edit_message_text(
                    "❌ Не найден городской ID. Требуется ручное добавление!\n"
                    f"TG ID: `{application_data['telegram_uid']}`",
                    parse_mode="Markdown"
                )
                return

            city_id = result[0]

            await db.execute(
                "UPDATE civilians SET telegram_uid = ? WHERE id = ?",
                (application_data["telegram_uid"], city_id)
            )
            await db.commit()

        await create_bank_account(city_id)

        os.remove(application_file)
        await query.edit_message_text(
            f"✅ Заявка одобрена\n"
            f"Городской ID: `{city_id}`\n"
            f"TG ID: `{application_data['telegram_uid']}`",
            parse_mode="Markdown"
        )

        await notify_user(
            context,
            application_data["telegram_uid"],
            "🎉 Ваша заявка одобрена! Теперь вы полноправный житель Вайтовера."
        )

    elif action == "block":
        success = await add_to_blacklist(
            application_data["telegram_uid"],
            application_data["mc_nickname"],
            "Отказ в регистрации"
        )

        if success:
            os.remove(application_file)
            await query.edit_message_text("✅ Пользователь добавлен в черный список")

            await notify_user(
                context,
                application_data["telegram_uid"],
                "❌ Ваша заявка на регистрацию была отклонена.\n"
                "По всем вопросам обращайтесь к @feetonok."
            )
        else:
            await query.edit_message_text("❌ Ошибка добавления в черный список")


async def notify_user(context: ContextTypes.DEFAULT_TYPE, user_id: str, message: str):
    """Отправляет уведомление пользователю"""
    try:
        await context.bot.send_message(user_id, message)
    except Exception as e:
        logger.error(f"Не удалось уведомить пользователя {user_id}: {e}")


async def create_bank_account(city_id: str) -> bool:
    """Создает банковский счет для пользователя по городскому ID"""
    try:
        async with aiosqlite.connect("bank.db") as db:
            await db.execute(
                "INSERT INTO accounts (id, balance, salary) VALUES (?, 0, 0)",
                (city_id,)
            )
            await db.commit()
            return True
    except Exception as e:
        logger.error(f"Ошибка создания банковского счета: {e}")
        return False


async def notify_admins(context: ContextTypes.DEFAULT_TYPE, user: User, application_data: dict, match_type: str):
    """Уведомляет админов о новой заявке"""
    admins = await get_admin_ids()

    message = (
        f"📨 Новая заявка на регистрацию ({match_type}):\n"
        f"ID заявки: `{application_data['application_id']}`\n"
        f"TG ID: `{application_data['telegram_uid']}`\n"
        f"Пользователь: @{user.username}\n"
        f"MC: {application_data['mc_nickname']}\n"
        f"Discord: {application_data['discord_nickname']}\n"
        f"Дата рождения: {application_data['birthday']}\n"
    )

    if match_type != "полное совпадение":
        message += "\n⚠️ ВНИМАНИЕ: Требуется ручная проверка данных!\n"

    keyboard = [
        [InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_{application_data['application_id']}"),
         InlineKeyboardButton("❌ Заблокировать", callback_data=f"block_{application_data['application_id']}")]
    ]

    for admin_id in admins:
        try:
            await context.bot.send_message(
                admin_id,
                message,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception as e:
            logger.error(f"Не удалось уведомить админа {admin_id}: {e}")


# Банковские операции
async def bank_operations_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("Начислить WVR 💰", callback_data="deposit")],
        [InlineKeyboardButton("Снять WVR 🏧", callback_data="withdraw")],
        [InlineKeyboardButton("Обналичить WVR 💎", callback_data="exchange")],
        [InlineKeyboardButton("Назад ↩️", callback_data="main_menu")],
    ]

    await query.edit_message_text(
        "🏦 Банковские операции\nВыберите действие:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def deposit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.edit_message_text("Введите ник в Minecraft или ID горожанина для начисления WVR:")
    return DEPOSIT_USER


async def deposit_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    recipient = update.message.text
    context.user_data["deposit_recipient"] = recipient

    async with aiosqlite.connect("civilian.db") as db:
        cursor = await db.execute(
            "SELECT id FROM civilians WHERE id = ? OR nickname LIKE ?",
            (recipient, f"%{recipient}%")
        )
        result = await cursor.fetchall()

        if not result:
            await update.message.reply_text("Житель не найден. Проверьте данные и попробуйте снова.")
            return ConversationHandler.END

        if len(result) > 1:
            keyboard = []
            for row in result:
                cursor = await db.execute(
                    "SELECT nickname FROM civilians WHERE id = ?",
                    (row[0],))
                nickname = await cursor.fetchone()
                keyboard.append([InlineKeyboardButton(
                    f"{nickname[0]} (ID: {row[0]})",
                    callback_data=f"deposit_to_{row[0]}")
                ])

            keyboard.append([InlineKeyboardButton("Отмена ❌", callback_data="bank_operations")])
            await update.message.reply_text(
                "Найдено несколько жителей. Выберите нужного:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return ConversationHandler.END

        context.user_data["deposit_user_id"] = result[0][0]
        await update.message.reply_text("Введите сумму для начисления:")
        return DEPOSIT_AMOUNT


async def deposit_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = int(update.message.text)
        if amount <= 0:
            raise ValueError

        context.user_data["deposit_amount"] = amount
        await update.message.reply_text("Введите причину начисления:")
        return DEPOSIT_REASON

    except ValueError:
        await update.message.reply_text("Неверная сумма. Введите целое положительное число:")
        return DEPOSIT_AMOUNT


async def deposit_complete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reason = update.message.text
    user_id = context.user_data["deposit_user_id"]
    amount = context.user_data["deposit_amount"]

    async with aiosqlite.connect("civilian.db") as db:
        cursor = await db.execute(
            "SELECT telegram_uid FROM civilians WHERE id = ?",
            (user_id,)
        )
        result = await cursor.fetchone()
        telegram_uid = result[0] if result else None

    success = await deposit_money(user_id, amount, reason)

    if success:
        if telegram_uid:
            try:
                await context.bot.send_message(
                    telegram_uid,
                    f"📥 Вам начислено {amount} WVR\n"
                    f"Причина: {reason}"
                )
            except Exception as e:
                logger.error(f"Не удалось уведомить пользователя: {e}")

        await update.message.reply_text(
            f"✅ Успешно начислено {amount} WVR\n"
            f"Причина: {reason}")
    else:
        await update.message.reply_text(
            "❌ Не удалось выполнить операцию. Проверьте данные и попробуйте снова.")

    return ConversationHandler.END


async def transfer_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()

    context.user_data.clear()

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Введите ник получателя или его ID:",
        reply_markup=get_reply_markup(include_cancel=True)
    )
    return TRANSFER_RECIPIENT


async def transfer_recipient(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    recipient = update.message.text
    context.user_data['transfer_recipient'] = recipient

    async with aiosqlite.connect("civilian.db") as db:
        cursor = await db.execute(
            "SELECT id, nickname, telegram_uid FROM civilians WHERE nickname LIKE ? OR id = ?",
            (f"%{recipient}%", recipient)
        )
        results = await cursor.fetchall()

        if not results:
            await update.message.reply_text(
                "❌ Житель не найден. Проверьте данные и попробуйте снова.",
                reply_markup=get_reply_markup(include_cancel=True)
            )
            return ConversationHandler.END

        if len(results) > 1:
            keyboard = []
            for user_id, nickname, _ in results:
                keyboard.append([InlineKeyboardButton(
                    f"{nickname} (ID: {user_id})",
                    callback_data=f"transfer_select_{user_id}"
                )])

            keyboard.append([InlineKeyboardButton("❌ Отменить", callback_data="cancel")])

            await update.message.reply_text(
                "Найдено несколько жителей. Выберите нужного:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return TRANSFER_SELECT_USER

        context.user_data['transfer_recipient_id'] = results[0][0]
        await update.message.reply_text(
            "Введите сумму для перевода:",
            reply_markup=get_reply_markup(include_cancel=True)
        )
        return TRANSFER_AMOUNT


async def transfer_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        amount = int(update.message.text)
        if amount <= 0:
            raise ValueError

        context.user_data['transfer_amount'] = amount
        balance = await get_balance(str(update.effective_user.id))

        if balance < amount:
            await update.message.reply_text(
                f"❌ Недостаточно средств. Ваш баланс: {balance} WVR",
                reply_markup=get_reply_markup(include_cancel=True)
            )
            return ConversationHandler.END

        recipient = context.user_data['transfer_recipient']
        async with aiosqlite.connect("civilian.db") as db:
            cursor = await db.execute(
                "SELECT id, nickname FROM civilians WHERE nickname LIKE ? OR id = ?",
                (f"%{recipient}%", recipient)
            )
            result = await cursor.fetchone()
            context.user_data['transfer_recipient_id'] = result[0]
            context.user_data['transfer_recipient_nick'] = result[1]

        keyboard = [
            [InlineKeyboardButton("✅ Подтвердить", callback_data="confirm_transfer")],
            [InlineKeyboardButton("✏️ Добавить комментарий", callback_data="add_comment")],
            [InlineKeyboardButton("❌ Отменить", callback_data="cancel_transfer")]
        ]

        await update.message.reply_text(
            f"Подтвердите перевод:\n"
            f"• Получатель: {result[1]}\n"
            f"• Сумма: {amount} WVR\n"
            f"• Комментарий: {'нет' if 'transfer_comment' not in context.user_data else context.user_data['transfer_comment']}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return TRANSFER_CONFIRM

    except ValueError:
        await update.message.reply_text(
            "Неверная сумма. Введите целое положительное число:",
            reply_markup=get_reply_markup(include_cancel=True)
        )
        return TRANSFER_AMOUNT


async def confirm_transfer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    recipient_id = context.user_data['transfer_recipient_id']
    amount = context.user_data['transfer_amount']
    comment = context.user_data.get('transfer_comment', '')
    from_uid = str(update.effective_user.id)

    success = await transfer_money(from_uid, recipient_id, amount, comment)

    if success:
        async with aiosqlite.connect("civilian.db") as db:
            cursor = await db.execute(
                "SELECT nickname, telegram_uid FROM civilians WHERE id = ?",
                (recipient_id,)
            )
            recipient_nick, to_uid = await cursor.fetchone()

            cursor = await db.execute(
                "SELECT nickname FROM civilians WHERE telegram_uid = ?",
                (from_uid,)
            )
            from_nick = (await cursor.fetchone())[0]

        msg = f"✅ Успешно переведено {amount} WVR пользователю {recipient_nick}"
        if comment:
            msg += f"\nКомментарий: {comment}"
        await query.edit_message_text(msg)

        try:
            recipient_msg = f"📥 Вам переведено {amount} WVR от {from_nick}"
            if comment:
                recipient_msg += f"\nКомментарий: {comment}"
            await context.bot.send_message(to_uid, recipient_msg)
        except Exception as e:
            logger.error(f"Ошибка уведомления получателя: {e}")
    else:
        await query.edit_message_text("❌ Ошибка при выполнении перевода")

    return ConversationHandler.END


async def add_comment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Введите комментарий к переводу:")
    return TRANSFER_COMMENT


async def cancel_transfer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("❌ Перевод отменен")
    return ConversationHandler.END


async def transfer_comment_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['transfer_comment'] = update.message.text

    keyboard = [
        [InlineKeyboardButton("✅ Подтвердить", callback_data="confirm_transfer")],
        [InlineKeyboardButton("❌ Отменить", callback_data="cancel_transfer")]
    ]

    await update.message.reply_text(
        f"Обновленные данные перевода:\n"
        f"• Получатель: {context.user_data['transfer_recipient_nick']}\n"
        f"• Сумма: {context.user_data['transfer_amount']} WVR\n"
        f"• Комментарий: {context.user_data['transfer_comment']}\n\n"
        "Подтвердите перевод:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return TRANSFER_CONFIRM


# Админ-панель
async def admin_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("Управление пользователями 👥", callback_data="manage_users")],
        [InlineKeyboardButton("Управление заданиями 📝", callback_data="manage_tasks")],
        [InlineKeyboardButton("Просмотр транзакций 💰", callback_data="view_transactions")],
        [InlineKeyboardButton("Чёрный список 🚫", callback_data="manage_blacklist")],
        [InlineKeyboardButton("Назад ↩️", callback_data="main_menu")],
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "👑 Администрирование\nВыберите действие:",
        reply_markup=reply_markup
    )


async def manage_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    page = context.user_data.get("user_page", 0)
    residents = await get_all_residents()
    pages = [residents[i:i + 5] for i in range(0, len(residents), 5)]

    if page >= len(pages):
        page = len(pages) - 1
    if page < 0:
        page = 0

    context.user_data["user_page"] = page
    current_page = pages[page]

    keyboard = []
    for user in current_page:
        keyboard.append([InlineKeyboardButton(
            f"{user['nickname']} (ID: {user['id']})",
            callback_data=f"user_detail_{user['id']}")
        ])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️", callback_data="user_prev_page"))
    nav_buttons.append(InlineKeyboardButton(f"{page + 1}/{len(pages)}", callback_data="user_page_num"))
    if page < len(pages) - 1:
        nav_buttons.append(InlineKeyboardButton("➡️", callback_data="user_next_page"))

    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton("Назад ↩️", callback_data="admin_actions")])

    await query.edit_message_text(
        "👥 Управление пользователями\nВыберите пользователя:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def user_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.data.split("_")[-1]

    async with aiosqlite.connect("civilian.db") as db:
        cursor = await db.execute(
            "SELECT id, nickname, role FROM civilians WHERE id = ?",
            (user_id,)
        )
        user = await cursor.fetchone()

        cursor = await db.execute(
            "SELECT balance FROM accounts WHERE id = ?",
            (user_id,)
        )
        balance = await cursor.fetchone()

    keyboard = [
        [InlineKeyboardButton("Назначить роль", callback_data=f"user_role_{user_id}")],
        [InlineKeyboardButton("Проверить счёт", callback_data=f"user_balance_{user_id}")],
        [InlineKeyboardButton("Проверить задания", callback_data=f"user_tasks_{user_id}")],
        [InlineKeyboardButton("Заблокировать", callback_data=f"user_block_{user_id}")],
        [InlineKeyboardButton("Назад ↩️", callback_data="manage_users")],
    ]

    await query.edit_message_text(
        f"👤 Информация о пользователе\n"
        f"ID: {user[0]}\n"
        f"Ник: {user[1]}\n"
        f"Роль: {ROLES.get(user[2], user[2])}\n"
        f"Баланс: {balance[0] if balance else 0} WVR",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def user_role_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.data.split("_")[-1]
    context.user_data["edit_user_id"] = user_id

    keyboard = []
    for role, role_name in ROLES.items():
        keyboard.append([InlineKeyboardButton(role_name, callback_data=f"set_role_{role}")])

    keyboard.append([InlineKeyboardButton("Назад ↩️", callback_data=f"user_detail_{user_id}")])

    await query.edit_message_text(
        "Выберите новую роль для пользователя:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ADMIN_ACTIONS


async def set_user_role(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    role = query.data.split("_")[-1]
    user_id = context.user_data["edit_user_id"]

    success = await change_user_role(user_id, role)
    if success:
        await query.edit_message_text(
            f"✅ Роль пользователя успешно изменена на {ROLES[role]}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Назад ↩️", callback_data=f"user_detail_{user_id}")]
            ]))
    else:
        await query.edit_message_text(
            "❌ Не удалось изменить роль пользователя",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Назад ↩️", callback_data=f"user_detail_{user_id}")]
            ]))
    return ConversationHandler.END


async def block_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.data.split("_")[-1]

    user = await get_user_info(user_id)
    if not user:
        await query.edit_message_text(
            "❌ Пользователь не найден",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Назад ↩️", callback_data="manage_users")]
            ]))
        return

    success = await add_to_blacklist(user_id, user["nickname"])
    if success:
        await query.edit_message_text(
            f"✅ Пользователь {user['nickname']} добавлен в черный список",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Назад ↩️", callback_data="manage_users")]
            ]))
    else:
        await query.edit_message_text(
            "❌ Не удалось добавить пользователя в черный список",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Назад ↩️", callback_data="manage_users")]
            ]))


async def manage_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("Создать задание", callback_data="create_task")],
        [InlineKeyboardButton("Просмотреть активные задания", callback_data="view_active_tasks")],
        [InlineKeyboardButton("Просмотреть выполненные задания", callback_data="view_completed_tasks")],
        [InlineKeyboardButton("Назад ↩️", callback_data="admin_actions")],
    ]

    await query.edit_message_text(
        "📝 Управление заданиями\nВыберите действие:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def create_task_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()

    await query.edit_message_text(
        "Введите название задания:"
    )
    return TASK_NAME


async def view_transactions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    page = context.user_data.get("trans_page", 0)
    transactions = await get_transactions(page)

    if not transactions and page > 0:
        page = 0
        transactions = await get_transactions(page)

    context.user_data["trans_page"] = page

    message = "💰 История транзакций\n\n"
    for trans in transactions:
        message += (f"📅 {trans['date']}\n"
                    f"Тип: {trans['type']}\n"
                    f"Сумма: {trans['amount']} WVR\n"
                    f"Комментарий: {trans['comment']}\n\n")

    keyboard = []
    nav_buttons = []

    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️", callback_data="trans_prev_page"))

    nav_buttons.append(InlineKeyboardButton(f"{page + 1}", callback_data="trans_page_num"))

    if len(transactions) == 10:
        nav_buttons.append(InlineKeyboardButton("➡️", callback_data="trans_next_page"))

    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton("Назад ↩️", callback_data="admin_actions")])

    await query.edit_message_text(
        message,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def manage_blacklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    blacklist = await get_blacklist()

    keyboard = []
    for user in blacklist:
        keyboard.append([InlineKeyboardButton(
            f"{user['nickname']} (ID: {user['id']})",
            callback_data=f"blacklist_detail_{user['id']}")
        ])

    keyboard.append([InlineKeyboardButton("Назад ↩️", callback_data="admin_actions")])

    await query.edit_message_text(
        "🚫 Управление черным списком\nВыберите пользователя:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def blacklist_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.data.split("_")[-1]

    user = await get_user_info(user_id)

    keyboard = [
        [InlineKeyboardButton("Разблокировать", callback_data=f"unblock_{user_id}")],
        [InlineKeyboardButton("Назад ↩️", callback_data="manage_blacklist")],
    ]

    await query.edit_message_text(
        f"🚫 Информация о заблокированном пользователе\n"
        f"ID: {user['id']}\n"
        f"Ник: {user['nickname']}\n"
        f"Дата блокировки: {user['block_date']}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def unblock_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.data.split("_")[-1]

    success = await remove_from_blacklist(user_id)
    if success:
        await query.edit_message_text(
            "✅ Пользователь удален из черного списка",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Назад ↩️", callback_data="manage_blacklist")]
            ]))
    else:
        await query.edit_message_text(
            "❌ Не удалось удалить пользователя из черного списка",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Назад ↩️", callback_data="manage_blacklist")]
            ]))


async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    role = await get_user_role(str(query.from_user.id))

    keyboard = [
        [InlineKeyboardButton("Мой баланс 💰", callback_data="balance")],
        [InlineKeyboardButton("Доступные задания 📋", callback_data="tasks")],
        [InlineKeyboardButton("Перевести WVR 🔄", callback_data="transfer")],
    ]

    if role in ["banker", "admin"]:
        keyboard.append([InlineKeyboardButton("Банковские операции 🏦", callback_data="bank_operations")])

    if role == "admin":
        keyboard.append([InlineKeyboardButton("Администрирование 👑", callback_data="admin_actions")])

    await query.edit_message_text(
        f"Главное меню\nТвой статус: {ROLES.get(role, 'Гость')}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def show_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    balance = await get_balance(str(query.from_user.id))

    keyboard = [[InlineKeyboardButton("Назад ↩️", callback_data="main_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        f"💰 Твой текущий баланс: {balance} WVR",
        reply_markup=reply_markup
    )


async def show_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    tasks = await get_available_tasks()

    if not tasks:
        await query.edit_message_text(
            "📋 Сейчас нет доступных заданий.\nПопробуй проверить позже!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад ↩️", callback_data="main_menu")]])
        )
        return

    message = "📋 Доступные задания:\n\n"
    for task in tasks:
        message += (
            f"🔹 {task['name']}\n"
            f"Тип: {TASK_TYPES.get(task['type'], task['type'])} | "
            f"Вид: {SOCIAL_TYPES.get(task['social_type'], task['social_type'])}\n"
            f"Награда: {task['cost']} WVR\n"
        )
        if task.get('description'):
            message += f"Описание: {task['description']}\n"
        if task.get('deadline'):
            message += f"Срок: {task['deadline']}\n"
        message += "\n"

    await query.edit_message_text(
        message,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Взять задание 📝", callback_data="take_task")],
            [InlineKeyboardButton("Назад ↩️", callback_data="main_menu")]
        ])
    )


async def view_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE, completed: bool = False):
    query = update.callback_query
    await query.answer()

    page = context.user_data.get("task_page", 0)

    async with aiosqlite.connect("tasks.db") as db:
        cursor = await db.execute(
            """SELECT id, name, task_type, cost, social_type, deadline, description, assigned_to 
            FROM tasks WHERE completed = ? ORDER BY id DESC LIMIT 5 OFFSET ?""",
            (completed, page * 5)
        )
        tasks = await cursor.fetchall()

        cursor = await db.execute(
            "SELECT COUNT(*) FROM tasks WHERE completed = ?",
            (completed,)
        )
        total_tasks = (await cursor.fetchone())[0]

    message = f"📋 {'Выполненные' if completed else 'Активные'} задания:\n\n"
    for task in tasks:
        assigned_to = "Не назначено" if not task[7] else task[7]
        message += (
            f"🔹 {task[1]} (ID: {task[0]})\n"
            f"Тип: {TASK_TYPES.get(task[2], task[2])}\n"
            f"Награда: {task[3]} WVR\n"
            f"Вид: {SOCIAL_TYPES.get(task[4], task[4])}\n"
            f"Срок: {task[5] if task[5] else 'Не указан'}\n"
            f"Назначено: {assigned_to}\n"
        )
        if task[6]:
            message += f"Описание: {task[6]}\n"
        message += "\n"

    total_pages = (total_tasks + 4) // 5
    keyboard = []

    if not completed:
        keyboard.append([
            InlineKeyboardButton("Пометить выполненным", callback_data="complete_task"),
            InlineKeyboardButton("Редактировать", callback_data="edit_task")
        ])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️", callback_data="task_prev_page"))

    nav_buttons.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="task_page_num"))

    if (page + 1) * 5 < total_tasks:
        nav_buttons.append(InlineKeyboardButton("➡️", callback_data="task_next_page"))

    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([
        InlineKeyboardButton("Назад ↩️", callback_data="manage_tasks"),
        InlineKeyboardButton("Главное меню 🏠", callback_data="start")
    ])

    await query.edit_message_text(
        message,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    context.user_data["task_filter_completed"] = completed


async def complete_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    task_id = context.user_data.get("current_task_id")
    if task_id:
        async with aiosqlite.connect("tasks.db") as db:
            await db.execute(
                "UPDATE tasks SET completed = TRUE WHERE id = ?",
                (task_id,)
            )
            await db.commit()

        await query.edit_message_text(
            "✅ Задание помечено как выполненное",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Назад к заданиям", callback_data="view_active_tasks")]
            ])
        )
    else:
        await query.edit_message_text(
            "❌ Не удалось найти задание",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Назад к заданиям", callback_data="view_active_tasks")]
            ])
        )


async def edit_task_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "Выберите параметр для редактирования:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Название", callback_data="edit_task_name")],
            [InlineKeyboardButton("Награду", callback_data="edit_task_reward")],
            [InlineKeyboardButton("Срок", callback_data="edit_task_deadline")],
            [InlineKeyboardButton("Отмена", callback_data="view_active_tasks")]
        ])
    )
    return TASK_EDIT_PARAM


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "Операция отменена",
        reply_markup=get_reply_markup()
    )
    return ConversationHandler.END


async def check_blacklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if await is_blacklisted(user_id):
        await update.message.reply_text("Вайтовер помнит своих. А ты в списках?")
        return True
    return False


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Извини, я не понимаю эту команду. Попробуй /start")


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

    if isinstance(context.error, telegram.error.BadRequest) and "Message text is empty" in str(context.error):
        return

    if update.effective_message:
        await update.effective_message.reply_text(
            "Произошла ошибка. Пожалуйста, попробуйте снова."
        )


def main() -> None:
    application = Application.builder().token("ТУТ ДОЛЖЕН БЫТЬ ТОКЕН").build()

    asyncio.get_event_loop().run_until_complete(init_databases())
    asyncio.get_event_loop().run_until_complete(check_last_transaction())

    asyncio.get_event_loop().create_task(sync_with_google_sheets())

    application.add_handler(MH(filters.ALL, check_blacklist), group=-1)

    reg_conv = ConversationHandler(
        entry_points=[
            CQH(start_registration, pattern="^start_registration$")
        ],
        states={
            MC_NICKNAME: [MH(filters.TEXT & ~filters.COMMAND, register_mc_nickname)],
            DISCORD_NICKNAME: [MH(filters.TEXT & ~filters.COMMAND, register_discord_nickname)],
            BIRTHDAY: [MH(filters.TEXT & ~filters.COMMAND, register_birthday)],
            REGISTRATION_CONFIRM: [
                CQH(register_confirm, pattern="^register_confirm$"),
                CQH(register_restart, pattern="^register_restart$")
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    application.add_handler(reg_conv)

    transfer_conv = ConversationHandler(
        entry_points=[CQH(transfer_start, pattern="^transfer$")],
        states={
            TRANSFER_RECIPIENT: [MH(filters.TEXT & ~filters.COMMAND, transfer_recipient)],
            TRANSFER_AMOUNT: [MH(filters.TEXT & ~filters.COMMAND, transfer_amount)],
            TRANSFER_CONFIRM: [
                CQH(confirm_transfer_handler, pattern="^confirm_transfer$"),
                CQH(add_comment_handler, pattern="^add_comment$"),
                CQH(cancel_transfer_handler, pattern="^cancel_transfer$")
            ],
            TRANSFER_COMMENT: [MH(filters.TEXT & ~filters.COMMAND, transfer_comment_text)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel)
        ],
        map_to_parent={
            ConversationHandler.END: ConversationHandler.END
        }
    )
    application.add_handler(transfer_conv)

    deposit_conv = ConversationHandler(
        entry_points=[CQH(deposit_start, pattern="^deposit$")],
        states={
            DEPOSIT_USER: [MH(filters.TEXT & ~filters.COMMAND, deposit_user)],
            DEPOSIT_AMOUNT: [MH(filters.TEXT & ~filters.COMMAND, deposit_amount)],
            DEPOSIT_REASON: [MH(filters.TEXT & ~filters.COMMAND, deposit_complete)],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
    )
    application.add_handler(deposit_conv)

    withdraw_conv = ConversationHandler(
        entry_points=[CQH(withdraw_start, pattern="^withdraw$")],
        states={
            WITHDRAW_USER: [MH(filters.TEXT & ~filters.COMMAND, withdraw_user)],
            WITHDRAW_AMOUNT: [MH(filters.TEXT & ~filters.COMMAND, withdraw_amount)],
            WITHDRAW_REASON: [MH(filters.TEXT & ~filters.COMMAND, withdraw_complete)],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
    )
    application.add_handler(withdraw_conv)

    exchange_conv = ConversationHandler(
        entry_points=[CQH(exchange_start, pattern="^exchange$")],
        states={
            EXCHANGE_USER: [MH(filters.TEXT & ~filters.COMMAND, exchange_user)],
            EXCHANGE_AMOUNT: [MH(filters.TEXT & ~filters.COMMAND, exchange_amount)],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
    )
    application.add_handler(exchange_conv)

    application.add_handler(CQH(
        lambda u, c: view_tasks(u, c, c.user_data.get("task_filter_completed", False)),
        pattern="^task_prev_page$")
    )
    application.add_handler(CQH(
        lambda u, c: view_tasks(u, c, c.user_data.get("task_filter_completed", False)),
        pattern="^task_next_page$")
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CQH(show_balance, pattern="^balance$"))
    application.add_handler(CQH(show_tasks, pattern="^tasks$"))
    application.add_handler(CQH(main_menu, pattern="^main_menu$"))
    application.add_handler(CQH(bank_operations_menu, pattern="^bank_operations$"))
    application.add_handler(CQH(admin_actions, pattern="^admin_actions$"))
    application.add_handler(CQH(cancel, pattern="^cancel$"))
    application.add_handler(CQH(manage_users, pattern="^manage_users$"))
    application.add_handler(CQH(manage_blacklist, pattern="^manage_blacklist$"))
    application.add_handler(CQH(user_detail, pattern="^user_detail_"))
    application.add_handler(CQH(blacklist_detail, pattern="^blacklist_detail_"))
    application.add_handler(CQH(user_role_menu, pattern="^user_role_"))
    application.add_handler(CQH(set_user_role, pattern="^set_role_"))
    application.add_handler(CQH(block_user, pattern="^user_block_"))
    application.add_handler(CQH(unblock_user, pattern="^unblock_"))
    application.add_handler(CQH(register_confirm, pattern="^register_confirm$"))
    application.add_handler(CQH(register_restart, pattern="^register_restart$"))
    application.add_handler(CQH(view_transactions, pattern="^view_transactions$"))
    application.add_handler(CQH(manage_tasks, pattern="^manage_tasks$"))
    application.add_handler(CQH(create_task_start, pattern="^create_task$"))
    application.add_handler(CQH(complete_task, pattern="^complete_task$"))
    application.add_handler(CQH(edit_task_start, pattern="^edit_task$"))
    application.add_handler(CQH(
        handle_application_decision,
        pattern=r"^(approve|block)_[a-f0-9-]+$"
    ))
    application.add_handler(MH(filters.ALL, check_user_access), group=0)

    application.add_handler(CQH(lambda u, c: view_tasks(u, c, completed=False), pattern="^view_active_tasks$"))
    application.add_handler(CQH(lambda u, c: view_tasks(u, c, completed=True), pattern="^view_completed_tasks$"))

    application.add_handler(CQH(lambda u, c: manage_users(u, c), pattern="^user_prev_page$"))
    application.add_handler(CQH(lambda u, c: manage_users(u, c), pattern="^user_next_page$"))

    application.add_handler(CQH(lambda u, c: view_transactions(u, c), pattern="^trans_prev_page$"))
    application.add_handler(CQH(lambda u, c: view_transactions(u, c), pattern="^trans_next_page$"))

    application.add_handler(MH(filters.COMMAND, unknown))

    application.add_error_handler(error_handler)

    application.run_polling()


if __name__ == "__main__":
    main()
