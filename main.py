# PART 0 - IMPORTS / CONFIG / DATABASE
import os
import asyncio
import sqlite3
import secrets
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Tuple, Dict

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# Telethon (used later for login/forwarding)
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.tl.functions.users import GetFullUserRequest

# ---------- CONFIG ----------
BOT_TOKEN = "8399763411:AAGVzQJqCkwMWgnEUV1_7GRHQtCSz-j5-yI"  # set in Railway / env or replace with your token (not recommended)
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "7765446998").split(",") if x.strip()]
DB_FILE = os.environ.get("DB_FILE", "hinari_users.db")
SESSIONS_DIR = os.environ.get("SESSIONS_DIR", "sessions")
BIO_REQUIRED_TEXT = os.environ.get("BIO_REQUIRED_TEXT", "By HinariAdsBot")
FREE_TRIAL_DAYS = int(os.environ.get("FREE_TRIAL_DAYS", "7"))
PREMIUM_PRICE_TEXT = os.environ.get("PREMIUM_PRICE_TEXT", "₹59/month — contact @NOTCH2ND")

os.makedirs(SESSIONS_DIR, exist_ok=True)

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("hinari")

# In-memory runtime holders
_active_login_clients: Dict[int, TelegramClient] = {}   # temporary during login (user_id -> Telethon client)
_active_clients: Dict[str, TelegramClient] = {}         # persistent active Telethon clients (keyed by user_acc)
_forward_tasks: Dict[int, Dict[int, asyncio.Task]] = {} # user_id -> {account_id: Task}

# ---------- DATABASE HELPERS ----------
def init_db():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    c = conn.cursor()

    # users: user_id, joined_date, premium_expiry, trial_start, delay_setting, is_banned
    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        joined_date TEXT,
        premium_expiry TEXT,
        trial_start TEXT,
        delay_setting INTEGER DEFAULT 300,
        is_banned INTEGER DEFAULT 0
    )
    """)

    # accounts: account_id, owner_id, phone, api_id, api_hash, session_file, is_forwarding, created_at
    c.execute("""
    CREATE TABLE IF NOT EXISTS accounts (
        account_id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id INTEGER,
        phone TEXT,
        api_id TEXT,
        api_hash TEXT,
        session_file TEXT,
        is_forwarding INTEGER DEFAULT 0,
        created_at TEXT
    )
    """)

    # redeem_codes: code, days, created_by, created_date, used_by, used_date, is_used
    c.execute("""
    CREATE TABLE IF NOT EXISTS redeem_codes (
        code TEXT PRIMARY KEY,
        days INTEGER,
        created_by INTEGER,
        created_date TEXT,
        used_by INTEGER DEFAULT NULL,
        used_date TEXT DEFAULT NULL,
        is_used INTEGER DEFAULT 0
    )
    """)

    # user_states for state machine (login/redeem/etc)
    c.execute("""
    CREATE TABLE IF NOT EXISTS user_states (
        user_id INTEGER PRIMARY KEY,
        state TEXT,
        temp_data TEXT
    )
    """)

    conn.commit()
    conn.close()

def run_db(query: str, params: tuple = (), fetch: Optional[str] = None):
    """
    Simple DB helper.
      - fetch="one" -> returns single row
      - fetch="all" -> returns all rows
      - otherwise returns None
    """
    conn = sqlite3.connect(DB_FILE, timeout=30)
    c = conn.cursor()
    c.execute(query, params)
    res = None
    if fetch == "one":
        res = c.fetchone()
    elif fetch == "all":
        res = c.fetchall()
    conn.commit()
    conn.close()
    return res

# initialize DB on import
init_db()
# PART 1 - USER / PREMIUM / ACCOUNT DB FUNCTIONS

def ensure_user(user_id: int) -> bool:
    """Ensure a DB entry exists for a user; apply free trial if new."""
    r = run_db("SELECT user_id FROM users WHERE user_id = ?", (user_id,), fetch="one")
    if not r:
        now = datetime.utcnow().isoformat()
        trial_start = now
        premium_expiry = (datetime.utcnow() + timedelta(days=FREE_TRIAL_DAYS)).isoformat()
        run_db(
            "INSERT INTO users (user_id, joined_date, premium_expiry, trial_start) VALUES (?, ?, ?, ?)",
            (user_id, now, premium_expiry, trial_start),
        )
        return True
    return False


def get_user_row(user_id: int):
    """
    Returns tuple:
    (user_id, joined_date, premium_expiry, trial_start, delay_setting, is_banned)
    """
    return run_db("SELECT user_id, joined_date, premium_expiry, trial_start, delay_setting, is_banned FROM users WHERE user_id = ?", (user_id,), fetch="one")


def is_premium_active(user_id: int) -> bool:
    row = get_user_row(user_id)
    if not row:
        return False
    expiry = row[2]
    if not expiry:
        return False
    try:
        return datetime.fromisoformat(expiry) > datetime.utcnow()
    except:
        return False


def premium_days_left(user_id: int) -> int:
    row = get_user_row(user_id)
    if not row or not row[2]:
        return 0
    try:
        d = datetime.fromisoformat(row[2]) - datetime.utcnow()
        return max(0, d.days)
    except:
        return 0


def extend_premium(user_id: int, days: int) -> str:
    """Extend or set new premium expiry."""
    row = get_user_row(user_id)
    if not row or not row[2]:
        new_expiry = datetime.utcnow() + timedelta(days=days)
    else:
        try:
            current = datetime.fromisoformat(row[2])
            if current < datetime.utcnow():
                new_expiry = datetime.utcnow() + timedelta(days=days)
            else:
                new_expiry = current + timedelta(days=days)
        except:
            new_expiry = datetime.utcnow() + timedelta(days=days)

    run_db("UPDATE users SET premium_expiry = ? WHERE user_id = ?", (new_expiry.isoformat(), user_id))
    return new_expiry.isoformat()


def add_account(owner_id: int, phone: str, api_id: str, api_hash: str, session_file: str) -> int:
    now = datetime.utcnow().isoformat()
    run_db(
        "INSERT INTO accounts (owner_id, phone, api_id, api_hash, session_file, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (owner_id, phone, api_id, api_hash, session_file, now),
    )
    row = run_db("SELECT account_id FROM accounts WHERE rowid = (SELECT MAX(rowid) FROM accounts)", fetch="one")
    return row[0] if row else 0


def get_accounts(owner_id: int) -> List[Tuple]:
    """
    Returns list of accounts:
    (account_id, owner_id, phone, api_id, api_hash, session_file, is_forwarding, created_at)
    """
    return run_db(
        "SELECT account_id, owner_id, phone, api_id, api_hash, session_file, is_forwarding, created_at FROM accounts WHERE owner_id = ?",
        (owner_id,), fetch="all"
    ) or []


def get_account_by_id(account_id: int):
    return run_db(
        "SELECT account_id, owner_id, phone, api_id, api_hash, session_file, is_forwarding, created_at FROM accounts WHERE account_id = ?",
        (account_id,), fetch="one"
    )


def delete_account(account_id: int):
    """Delete account + its session files."""
    acc = get_account_by_id(account_id)
    if acc:
        session_file = acc[5]
        try:
            for ext in ["", ".session", ".session-journal"]:
                path = f"{session_file}{ext}"
                if os.path.exists(path):
                    os.remove(path)
        except:
            pass
    run_db("DELETE FROM accounts WHERE account_id = ?", (account_id,))


def create_redeem_code(days: int, creator_id: int) -> str:
    code = "".join(secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") for _ in range(12))
    run_db(
        "INSERT INTO redeem_codes (code, days, created_by, created_date) VALUES (?, ?, ?, ?)",
        (code, days, creator_id, datetime.utcnow().isoformat()),
    )
    return code


def use_redeem_code(code: str, user_id: int) -> Optional[int]:
    row = run_db("SELECT code, days, is_used FROM redeem_codes WHERE code = ?", (code,), fetch="one")
    if not row:
        return None
    if row[2] == 1:
        return None

    days = row[1]
    run_db(
        "UPDATE redeem_codes SET used_by = ?, used_date = ?, is_used = 1 WHERE code = ?",
        (user_id, datetime.utcnow().isoformat(), code),
    )
    extend_premium(user_id, days)
    return days


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS
    # PART 2 — SAFE REPLY / UI / START / MENU / STATE MACHINE

import json  # required for state-machine temp_data


# ---------- SAFE REPLY ----------
async def safe_reply(update: Update, text: str, **kwargs):
    """
    Safely reply to either message or callback.
    Prevents NoneType errors caused by update.message being None.
    """
    msg = update.effective_message
    if msg:
        return await msg.reply_text(text, **kwargs)

    # If no message (callback-only), send popup
    if update.callback_query:
        try:
            return await update.callback_query.answer(text)
        except:
            return None

    return None


# ---------- MAIN MENU UI ----------
def main_menu_kb(user_id: int):
    kb = [
        [
            InlineKeyboardButton("➕ Add Account", callback_data="ui_add_account"),
            InlineKeyboardButton("📁 Manage Accounts", callback_data="ui_manage_accounts")
        ],
        [InlineKeyboardButton("🎟 Redeem Code", callback_data="ui_redeem")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="ui_settings")]
    ]

    if is_admin(user_id):
        kb.append([InlineKeyboardButton("👑 Admin Panel", callback_data="ui_admin_panel")])

    return InlineKeyboardMarkup(kb)


def manage_accounts_kb(accounts):
    kb = []
    for acc in accounts:
        account_id = acc[0]
        phone = acc[2]
        kb.append([
            InlineKeyboardButton(f"📱 {phone}", callback_data=f"acc_show:{account_id}"),
            InlineKeyboardButton("🗑 Delete", callback_data=f"acc_delete:{account_id}")
        ])

    kb.append([InlineKeyboardButton("➕ Add Account", callback_data="ui_add_account")])
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="ui_back")])

    return InlineKeyboardMarkup(kb)


def admin_panel_kb():
    kb = [
        [
            InlineKeyboardButton("📊 Stats", callback_data="admin_stats"),
            InlineKeyboardButton("🎟 Generate Code", callback_data="admin_gencode")
        ],
        [
            InlineKeyboardButton("⛔ Ban User", callback_data="admin_ban"),
            InlineKeyboardButton("♻ Unban User", callback_data="admin_unban")
        ],
        [
            InlineKeyboardButton("⭐ Extend Premium", callback_data="admin_extend"),
            InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")
        ],
        [InlineKeyboardButton("🔙 Back", callback_data="ui_back")]
    ]
    return InlineKeyboardMarkup(kb)


# ---------- START MESSAGE ----------
WELCOME_MESSAGE = (
    "<b>🌟 Welcome to HinariAdsBot</b>\n\n"
    "Created by <b>@NOTCH2ND</b>\n\n"
    "🎁 <b>1 WEEK FREE TRIAL ACTIVATED</b>\n"
    f"💎 Premium Price: {PREMIUM_PRICE_TEXT}\n\n"
    "Login → Save Messages → Auto-forward everywhere.\n\n"
    "👉 <b>Use /menu to get started.</b>"
)


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id)  # Automatically applies free trial for new users

    days = premium_days_left(user.id)
    status = f"✅ {days} days left" if days > 0 else "❌ Expired"

    text = WELCOME_MESSAGE + f"\n\n<b>Your Premium:</b> {status}"

    return await safe_reply(
        update,
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_kb(user.id),
    )


# ---------- MENU HANDLER ----------
async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id)

    days = premium_days_left(user.id)
    accounts = get_accounts(user.id)

    text = (
        "<b>🔰 HinariAdsBot — Main Menu</b>\n\n"
        f"💎 <b>Premium:</b> {'✅ Active ('+str(days)+' days)' if days>0 else '❌ Not active'}\n"
        f"🔗 <b>Connected Accounts:</b> {len(accounts)}\n\n"
        "Choose an option below:"
    )

    return await safe_reply(
        update,
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_kb(user.id),
    )


# ---------- STATE MACHINE SYSTEM ----------
def set_state(user_id: int, state: str, temp: Optional[dict] = None):
    temp_json = json.dumps(temp) if temp else None
    run_db(
        "INSERT OR REPLACE INTO user_states (user_id, state, temp_data) VALUES (?, ?, ?)",
        (user_id, state, temp_json)
    )


def get_state(user_id: int):
    row = run_db("SELECT state, temp_data FROM user_states WHERE user_id = ?", (user_id,), fetch="one")
    if not row:
        return None, None
    state, temp_json = row
    return state, (json.loads(temp_json) if temp_json else None)


def clear_state(user_id: int):
    run_db("DELETE FROM user_states WHERE user_id = ?", (user_id,))


# ---------- MESSAGE ROUTER ----------
async def message_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        return  # callback handled separately

    text = (update.message.text or "").strip()
    user_id = update.effective_user.id

    # check premium + ban
    row = get_user_row(user_id)
    if row and row[5] == 1:
        return await safe_reply(update, "❌ You are banned.")

    # handle stateful commands (login, otp, redeem)
    state, temp = get_state(user_id)

    # LOGIN STATES handled in Part 3
    if state and state.startswith("login_"):
        return await handle_login_states(update, context, state, temp)

    # REDEEM STATE
    if state == "waiting_redeem":
        code = text.upper()
        days = use_redeem_code(code, user_id)
        clear_state(user_id)
        if not days:
            return await safe_reply(update, "❌ Invalid or already used redeem code.")
        return await safe_reply(update, f"🎉 Redeemed! Premium extended by {days} days.")

    # STANDARD TEXT COMMANDS
    if text.lower() in ("start", "/start"):
        return await start_handler(update, context)

    if text.lower() in ("menu", "/menu"):
        return await menu_handler(update, context)

    return await safe_reply(update, "Unknown option.\nUse /menu.")
    # PART 3 - TELETHON LOGIN FLOW (stateful) / session filename helper

def make_session_filename(user_id: int, suffix: Optional[str] = None) -> str:
    """Create a unique session filename (without extension)."""
    token = secrets.token_hex(6)
    name = f"{SESSIONS_DIR}/user_{user_id}_{token}"
    if suffix:
        name = f"{name}_{suffix}"
    return name


async def handle_login_states(update: Update, context: ContextTypes.DEFAULT_TYPE, state: str, temp: Optional[dict]):
    """
    Handle the login states saved in user_states:
      - login_api_id
      - login_api_hash
      - login_phone
      - login_code
      - login_2fa

    The function reads/writes state via set_state / clear_state and uses _active_login_clients.
    """
    user = update.effective_user
    uid = user.id
    text = (update.message.text or "").strip() if update.message else ""

    # --- API ID ---
    if state == "login_api_id":
        if not text.isdigit():
            return await safe_reply(update, "❌ API ID must be numeric. Send the numeric API ID.")
        temp = {"api_id": int(text)}
        set_state(uid, "login_api_hash", temp)
        return await safe_reply(update, "📥 API ID received. Now send your <b>API HASH</b>:", parse_mode=ParseMode.HTML)

    # --- API HASH ---
    if state == "login_api_hash":
        if not temp:
            clear_state(uid)
            return await safe_reply(update, "Session lost — please start again with Add Account.")
        temp["api_hash"] = text.strip()
        set_state(uid, "login_phone", temp)
        return await safe_reply(update, "📱 API hash saved. Now send your phone number with country code (e.g. +919812345678):")

    # --- PHONE: send code ---
    if state == "login_phone":
        if not temp or "api_id" not in temp or "api_hash" not in temp:
            clear_state(uid)
            return await safe_reply(update, "State lost — start Add Account again.")
        phone = text.strip()
        api_id = int(temp["api_id"])
        api_hash = temp["api_hash"]
        session_file = make_session_filename(uid)
        client = TelegramClient(session_file, api_id, api_hash)
        try:
            await client.connect()
            await client.send_code_request(phone)
        except Exception as e:
            try:
                await client.disconnect()
            except:
                pass
            clear_state(uid)
            return await safe_reply(update, f"❌ Failed to send OTP: {e}")
        # store temp client and state
        _active_login_clients[uid] = client
        temp.update({"phone": phone, "session_file": session_file})
        set_state(uid, "login_code", temp)
        return await safe_reply(update, "📩 OTP sent to your phone. Please enter the code:")

    # --- CODE: verify ---
    if state == "login_code":
        client = _active_login_clients.get(uid)
        if not client or not temp:
            clear_state(uid)
            return await safe_reply(update, "Session expired — start Add Account again.")
        code = text.strip()
        try:
            try:
                await client.sign_in(temp["phone"], code)
            except SessionPasswordNeededError:
                # the account has two-factor auth enabled
                set_state(uid, "login_2fa", temp)
                return await safe_reply(update, "🔐 2FA is required. Send your account password:")
            # success — save account in DB
            acc_id = add_account(uid, temp["phone"], str(temp["api_id"]), temp["api_hash"], temp["session_file"])
            # cleanup
            try:
                await client.disconnect()
            except:
                pass
            _active_login_clients.pop(uid, None)
            clear_state(uid)
            ensure_user(uid)  # ensure user exists so trial is applied if new
            return await safe_reply(update, f"✅ Account connected successfully (Account ID: {acc_id}).\nYou can now start forwarding from this account.")
        except Exception as e:
            try:
                await client.disconnect()
            except:
                pass
            _active_login_clients.pop(uid, None)
            clear_state(uid)
            return await safe_reply(update, f"❌ Sign-in error: {e}")

    # --- 2FA PASSWORD ---
    if state == "login_2fa":
        client = _active_login_clients.get(uid)
        if not client or not temp:
            clear_state(uid)
            return await safe_reply(update, "Session expired — start Add Account again.")
        password = text.strip()
        try:
            await client.sign_in(password=password)
            acc_id = add_account(uid, temp["phone"], str(temp["api_id"]), temp["api_hash"], temp["session_file"])
            try:
                await client.disconnect()
            except:
                pass
            _active_login_clients.pop(uid, None)
            clear_state(uid)
            ensure_user(uid)
            return await safe_reply(update, f"✅ Account connected with 2FA (Account ID: {acc_id}).")
        except Exception as e:
            try:
                await client.disconnect()
            except:
                pass
            _active_login_clients.pop(uid, None)
            clear_state(uid)
            return await safe_reply(update, f"❌ 2FA error: {e}")

    # Unknown state
    clear_state(uid)
    return await safe_reply(update, "State error — please start again.")
# PART 4 — CALLBACK ROUTER (UI NAVIGATION, MANAGE ACCOUNTS, ADMIN PANEL)

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return

    user = q.from_user
    uid = user.id
    payload = q.data or ""

    await q.answer()  # stop Telegram “loading...” spinner

    # ---------------- BASIC NAVIGATION ----------------
    if payload == "ui_back":
        return await menu_handler(update, context)

    if payload == "ui_settings":
        return await safe_reply(update, "⚙️ Settings are not implemented yet.")

    # ---------------- ADD ACCOUNT ----------------
    if payload == "ui_add_account":
        # Start login flow at API ID
        set_state(uid, "login_api_id", None)
        return await safe_reply(
            update,
            "➕ <b>Add Account</b>\n\nSend your <u>API ID</u> to begin:",
            parse_mode=ParseMode.HTML
        )

    # ---------------- MANAGE ACCOUNTS ----------------
    if payload == "ui_manage_accounts":
        accounts = get_accounts(uid)

        if not accounts:
            # No accounts → Add Account button required
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Add Account", callback_data="ui_add_account")],
                [InlineKeyboardButton("🔙 Back", callback_data="ui_back")]
            ])
            return await safe_reply(
                update,
                "📂 <b>You have no connected accounts.</b>\nTap below to add one:",
                parse_mode=ParseMode.HTML,
                reply_markup=kb
            )

        return await safe_reply(
            update,
            "📁 <b>Your Connected Accounts:</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=manage_accounts_kb(accounts)
        )

    # ---------------- ACCOUNT DETAILS ----------------
    if payload.startswith("acc_show:"):
        acc_id = int(payload.split(":", 1)[1])
        acc = get_account_by_id(acc_id)
        if not acc or acc[1] != uid:
            return await safe_reply(update, "❌ Account not found.")

        _, owner_id, phone, api_id, api_hash, session_file, is_forwarding, created_at = acc

        text = (
            f"📟 <b>Account ID:</b> {acc_id}\n"
            f"📱 <b>Phone:</b> {phone}\n"
            f"🔄 <b>Forwarding:</b> {'🟢 ON' if is_forwarding else '🔴 OFF'}\n"
        )

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("▶ Start Forwarding", callback_data=f"acc_startfw:{acc_id}")],
            [InlineKeyboardButton("⏹ Stop Forwarding", callback_data=f"acc_stopfw:{acc_id}")],
            [InlineKeyboardButton("🗑 Delete", callback_data=f"acc_delete:{acc_id}")],
            [InlineKeyboardButton("🔙 Back", callback_data="ui_manage_accounts")]
        ])

        return await safe_reply(update, text, parse_mode=ParseMode.HTML, reply_markup=kb)

    # ---------------- DELETE ACCOUNT ----------------
    if payload.startswith("acc_delete:"):
        acc_id = int(payload.split(":", 1)[1])
        acc = get_account_by_id(acc_id)
        if not acc or acc[1] != uid:
            return await safe_reply(update, "❌ Invalid account.")

        # stop forwarding if running
        try:
            if uid in _forward_tasks and acc_id in _forward_tasks[uid]:
                _forward_tasks[uid][acc_id].cancel()
                _forward_tasks[uid].pop(acc_id, None)
        except:
            pass

        delete_account(acc_id)

        return await safe_reply(update, f"🗑 Account <b>{acc_id}</b> deleted.", parse_mode=ParseMode.HTML)

    # ---------------- START FORWARDING ----------------
    if payload.startswith("acc_startfw:"):
        acc_id = int(payload.split(":", 1)[1])
        started = await start_forward_for_account(acc_id)

        if not started:
            return await safe_reply(update, "⚠️ Forwarding already running or account error.")

        return await safe_reply(update, "▶ Forwarding started!")

    # ---------------- STOP FORWARDING ----------------
    if payload.startswith("acc_stopfw:"):
        acc_id = int(payload.split(":", 1)[1])
        stopped = await stop_forward_for_account(acc_id)

        if not stopped:
            return await safe_reply(update, "⚠️ Forwarding wasn't running.")

        return await safe_reply(update, "⏹ Forwarding stopped.")

    # ---------------- REDEEM CODE ----------------
    if payload == "ui_redeem":
        set_state(uid, "waiting_redeem")
        return await safe_reply(
            update,
            "🎟 <b>Enter your redeem code:</b>",
            parse_mode=ParseMode.HTML
        )

    # ---------------- ADMIN PANEL ----------------
    if payload == "ui_admin_panel":
        if not is_admin(uid):
            return await safe_reply(update, "❌ You are not an admin.")
        return await safe_reply(
            update,
            "👑 <b>Admin Panel</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_panel_kb()
        )

    # ---------------- ADMIN ACTIONS ----------------
    if payload == "admin_stats":
        total_users = run_db("SELECT COUNT(*) FROM users", fetch="one")[0]
        total_accounts = run_db("SELECT COUNT(*) FROM accounts", fetch="one")[0]

        return await safe_reply(
            update,
            f"📊 <b>Bot Statistics</b>\n\n"
            f"👥 Users: <b>{total_users}</b>\n"
            f"📱 Accounts Connected: <b>{total_accounts}</b>",
            parse_mode=ParseMode.HTML
        )

    if payload == "admin_gencode":
        return await safe_reply(update, "Usage:\n<b>/genkey 5 30</b>\n(5 codes, 30 days each)", parse_mode=ParseMode.HTML)

    if payload == "admin_ban":
        return await safe_reply(update, "Usage:\n<b>/ban USER_ID</b>", parse_mode=ParseMode.HTML)

    if payload == "admin_unban":
        return await safe_reply(update, "Usage:\n<b>/unban USER_ID</b>", parse_mode=ParseMode.HTML)

    if payload == "admin_extend":
        return await safe_reply(update, "Usage:\n<b>/extend USER_ID DAYS</b>", parse_mode=ParseMode.HTML)

    if payload == "admin_broadcast":
        return await safe_reply(update, "Usage:\n<b>/broadcast message here</b>", parse_mode=ParseMode.HTML)

    # ---------------- UNKNOWN ----------------
    return await safe_reply(update, "Unknown action.\nUse /menu.")
    # PART 5 — FORWARDER ENGINE + ADMIN COMMANDS


# ---------------------- ADMIN COMMANDS ----------------------

async def genkey_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return await safe_reply(update, "❌ You are not an admin.")

    args = context.args
    if len(args) != 2:
        return await safe_reply(update, "Usage:\n<b>/genkey QUANTITY DAYS</b>", parse_mode=ParseMode.HTML)

    try:
        qty = int(args[0])
        days = int(args[1])
    except:
        return await safe_reply(update, "❌ Qty and Days must be numbers.")

    codes = []
    for _ in range(qty):
        codes.append(create_redeem_code(days, uid))

    return await safe_reply(update, "🎟 <b>Generated Codes:</b>\n" + "\n".join(codes), parse_mode=ParseMode.HTML)


async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return await safe_reply(update, "❌ Not authorized.")

    if not context.args:
        return await safe_reply(update, "Usage:\n<b>/ban USER_ID</b>", parse_mode=ParseMode.HTML)

    target = int(context.args[0])
    run_db("UPDATE users SET is_banned = 1 WHERE user_id = ?", (target,))
    return await safe_reply(update, f"⛔ Banned <b>{target}</b>", parse_mode=ParseMode.HTML)


async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return await safe_reply(update, "❌ Not authorized.")

    if not context.args:
        return await safe_reply(update, "Usage:\n<b>/unban USER_ID</b>", parse_mode=ParseMode.HTML)

    target = int(context.args[0])
    run_db("UPDATE users SET is_banned = 0 WHERE user_id = ?", (target,))
    return await safe_reply(update, f"♻ Unbanned <b>{target}</b>", parse_mode=ParseMode.HTML)


async def extend_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return await safe_reply(update, "❌ Not authorized.")

    if len(context.args) != 2:
        return await safe_reply(update, "Usage:\n<b>/extend USER_ID DAYS</b>", parse_mode=ParseMode.HTML)

    target = int(context.args[0])
    days = int(context.args[1])

    new_expiry = extend_premium(target, days)

    return await safe_reply(update, f"⭐ Extended premium for <b>{target}</b>\nNew Expiry: {new_expiry}", parse_mode=ParseMode.HTML)


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return await safe_reply(update, "❌ Not authorized.")

    msg = " ".join(context.args)
    if not msg:
        return await safe_reply(update, "Usage:\n<b>/broadcast MESSAGE</b>", parse_mode=ParseMode.HTML)

    users = run_db("SELECT user_id FROM users WHERE is_banned = 0", fetch="all")

    sent = 0
    for row in users:
        try:
            await context.bot.send_message(int(row[0]), msg)
            sent += 1
        except:
            pass

    return await safe_reply(update, f"📢 Broadcast sent to <b>{sent}</b> users.", parse_mode=ParseMode.HTML)



# ---------------------- TELETHON CLIENT MANAGER ----------------------

def _client_key(owner_id: int, account_id: int) -> str:
    return f"user_{owner_id}_acc_{account_id}"


async def get_client_for_account(account_row):
    """
    Load or reconnect Telethon client for an account.
    """
    if not account_row:
        return None

    account_id = account_row[0]
    owner_id = account_row[1]
    api_id = int(account_row[3])
    api_hash = account_row[4]
    session_file = account_row[5]

    key = _client_key(owner_id, account_id)

    # Already loaded in memory?
    client = _active_clients.get(key)
    if client:
        try:
            if not await client.is_connected():
                await client.connect()

            if not await client.is_user_authorized():
                await client.disconnect()
                _active_clients.pop(key, None)
                return None

            return client
        except:
            try:
                await client.disconnect()
            except:
                pass
            _active_clients.pop(key, None)

    # Create new Telethon Client
    client = TelegramClient(session_file, api_id, api_hash)

    try:
        await client.connect()

        if not await client.is_user_authorized():
            await client.disconnect()
            return None

        _active_clients[key] = client
        return client

    except Exception as e:
        log.error(f"Client creation failed for acc {account_id}: {e}")
        try:
            await client.disconnect()
        except:
            pass
        return None



# ---------------------- FORWARDER LOOP ----------------------

async def forwarder_loop(account_id: int):
    acc = get_account_by_id(account_id)
    if not acc:
        return

    account_id, owner_id, phone, api_id, api_hash, session_file, is_forwarding, created_at = acc

    # Mark forwarding ON
    run_db("UPDATE accounts SET is_forwarding = 1 WHERE account_id = ?", (account_id,))

    client = await get_client_for_account(acc)
    if not client:
        run_db("UPDATE accounts SET is_forwarding = 0 WHERE account_id = ?", (account_id,))
        return

    # BIO CHECK
    try:
        profile = await client(GetFullUserRequest('me'))
        bio = getattr(profile.full_user, "about", "") or ""
    except:
        bio = ""

    if BIO_REQUIRED_TEXT not in bio:
        try:
            await context.bot.send_message(owner_id, f"⚠️ Add \"{BIO_REQUIRED_TEXT}\" to your Telegram bio to enable forwarding.")
        except:
            pass
        run_db("UPDATE accounts SET is_forwarding = 0 WHERE account_id = ?", (account_id,))
        return

    # Find all groups/chats to forward to
    recipients = []
    try:
        async for dialog in client.iter_dialogs():
            if dialog.is_group or (dialog.is_channel and not getattr(dialog.entity, "broadcast", True)):
                recipients.append(dialog.entity)
    except:
        pass

    last_msg_id = None

    # Delay setting
    user_row = get_user_row(owner_id)
    delay = user_row[4] if user_row else 300

    while True:
        try:
            messages = await client.get_messages("me", limit=1)
            if messages:
                msg = messages[0]

                if last_msg_id is None or msg.id != last_msg_id:
                    for chat in recipients:
                        try:
                            await client.forward_messages(chat, msg.id, from_peer='me')
                            await asyncio.sleep(0.5)
                        except:
                            pass

                    last_msg_id = msg.id

        except asyncio.CancelledError:
            break

        except Exception as e:
            log.error(f"Forwarding error acc {account_id}: {e}")
            break

        # Bio re-check
        try:
            profile = await client(GetFullUserRequest('me'))
            bio = getattr(profile.full_user, "about", "") or ""
        except:
            bio = ""

        if BIO_REQUIRED_TEXT not in bio:
            try:
                await context.bot.send_message(owner_id, "⚠️ Required bio text missing. Forwarding stopped.")
            except:
                pass
            break

        await asyncio.sleep(delay)

    # CLEAN UP
    run_db("UPDATE accounts SET is_forwarding = 0 WHERE account_id = ?", (account_id,))

    if owner_id in _forward_tasks and account_id in _forward_tasks[owner_id]:
        _forward_tasks[owner_id].pop(account_id, None)



# ---------------------- START / STOP FORWARDING ----------------------

async def start_forward_for_account(account_id: int):
    acc = get_account_by_id(account_id)
    if not acc:
        return False

    owner_id = acc[1]

    if owner_id not in _forward_tasks:
        _forward_tasks[owner_id] = {}

    if account_id in _forward_tasks[owner_id]:
        return False  # already running

    task = asyncio.create_task(forwarder_loop(account_id))
    _forward_tasks[owner_id][account_id] = task

    return True


async def stop_forward_for_account(account_id: int):
    acc = get_account_by_id(account_id)
    if not acc:
        return False

    owner_id = acc[1]

    if owner_id in _forward_tasks and account_id in _forward_tasks[owner_id]:
        task = _forward_tasks[owner_id].pop(account_id, None)
        if task:
            task.cancel()

    run_db("UPDATE accounts SET is_forwarding = 0 WHERE account_id = ?", (account_id,))
    return True
    # PART 6 - KEEP-ALIVE / DAILY TASKS / APP BUILD & RUN

async def keep_alive_loop():
    """Simple heartbeat to keep process active."""
    try:
        while True:
            log.info("Keep-alive heartbeat")
            await asyncio.sleep(300)  # 5 minutes
    except asyncio.CancelledError:
        log.info("Keep-alive cancelled")
        raise


async def daily_status_loop(app: Application):
    """Send or log daily premium status to users (non-intrusive)."""
    try:
        while True:
            rows = run_db("SELECT user_id, premium_expiry FROM users", fetch="all") or []
            for r in rows:
                uid = r[0]
                try:
                    days = premium_days_left(uid)
                    # send a short DM — comment out if noisy
                    # await app.bot.send_message(uid, f"🔔 Premium days left: {days}")
                except Exception:
                    continue
            await asyncio.sleep(24 * 3600)
    except asyncio.CancelledError:
        log.info("Daily status loop cancelled")
        raise


async def resume_forwarders(app: Application):
    """Resume forwarders that were marked as is_forwarding in DB."""
    rows = run_db("SELECT account_id FROM accounts WHERE is_forwarding = 1", fetch="all") or []
    for r in rows:
        try:
            aid = r[0]
            await start_forward_for_account(aid)
        except Exception:
            continue


async def on_startup(app: Application):
    """Post-init startup tasks."""
    # start keepalive + daily loops
    asyncio.create_task(keep_alive_loop())
    asyncio.create_task(daily_status_loop(app))
    # resume any forwarders
    try:
        await resume_forwarders(app)
    except Exception:
        log.exception("resume forwarders failed")
    log.info("Startup tasks launched.")


def build_app() -> Application:
    if not BOT_TOKEN:
        log.error("BOT_TOKEN not set. Set env BOT_TOKEN or update script.")
        raise RuntimeError("BOT_TOKEN not set")

    app = Application.builder().token(BOT_TOKEN).build()

    # Core commands
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("menu", menu_handler))

    # Admin commands
    app.add_handler(CommandHandler("genkey", genkey_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CommandHandler("extend", extend_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))

    # Message router (stateful): handles login states, redeem text, general text
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_router))

    # Callback (inline keyboard) router
    app.add_handler(CallbackQueryHandler(callback_router))

    # Post-init tasks
    app.post_init = on_startup

    return app


def main():
    init_db()
    app = build_app()
    log.info("HinariAdsBot is starting...")
    app.run_polling()


if __name__ == "__main__":
    main()