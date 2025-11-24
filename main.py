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
    # ================================================================
# ================================================================
# ================================================================
# PART 4 — CALLBACK ROUTER (UI NAVIGATION, MANAGE ACCOUNTS,
#           ADMIN PANEL, SETTINGS + DELAY MENU)
# ================================================================

async def safe_edit(update: Update, text: str, **kwargs):
    """
    Edit callback message if possible, otherwise reply safely.
    Accepts the same kwargs as reply_text/edit_message_text.
    """
    q = update.callback_query
    if q:
        try:
            return await q.edit_message_text(text, **kwargs)
        except:
            try:
                await q.answer(text)
            except:
                pass
            return await safe_reply(update, text, **kwargs)
    return await safe_reply(update, text, **kwargs)


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return

    await q.answer()
    data = q.data
    uid = update.effective_user.id

    # ---------- MAIN MENU ----------
    if data == "ui_menu":
        return await menu_handler(update, context)

    # ---------- ADD ACCOUNT ----------
    if data == "ui_add_account":
        ensure_user(uid)
        accounts = get_accounts(uid)
        if not is_premium_active(uid) and len(accounts) >= 1:
            return await safe_edit(
                update,
                "Free users can only add 1 account.\n\nBuy premium to add more.\nContact @NOTCH2ND"
            )
        set_state(uid, "login_api_id", {})
        return await safe_edit(update, "Send your *API ID*:", parse_mode=ParseMode.MARKDOWN)

    # ---------- MANAGE ACCOUNTS ----------
    if data == "ui_manage_accounts":
        accounts = get_accounts(uid)
        if not accounts:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Add Account", callback_data="ui_add_account")],
                [InlineKeyboardButton("⬅️ Back", callback_data="ui_menu")]
            ])
            return await safe_edit(
                update,
                "❌ You have no connected accounts.\nUse 'Add Account' to connect one.",
                reply_markup=kb
            )
        return await safe_edit(
            update,
            "📂 *Your Accounts:*",
            reply_markup=manage_accounts_kb(accounts),
            parse_mode=ParseMode.MARKDOWN
        )

    # SHOW ACCOUNT DETAILS
    if data.startswith("acc_show:"):
        try:
            acc_id = int(data.split(":", 1)[1])
        except:
            return await safe_edit(update, "Invalid account id.")

        acc = get_account_by_id(acc_id)
        if not acc or acc[1] != uid:
            return await safe_edit(update, "❌ Account not found.")

        _, owner_id, phone, api_id, api_hash, session_file, is_fw, created_at = acc
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ Start Forwarding" if not is_fw else "⏸ Stop Forwarding",
                                  callback_data=f"acc_toggle:{acc_id}")],
            [InlineKeyboardButton("🗑 Delete", callback_data=f"acc_delete:{acc_id}")],
            [InlineKeyboardButton("⬅️ Back", callback_data="ui_manage_accounts")]
        ])
        status = "Forwarding ON" if is_fw else "Forwarding OFF"
        return await safe_edit(
            update,
            f"📱 *Account:* `{phone}`\n"
            f"ID: `{acc_id}`\n"
            f"Status: *{status}*",
            reply_markup=kb,
            parse_mode=ParseMode.MARKDOWN
        )

    # TOGGLE FORWARDING
    if data.startswith("acc_toggle:"):
        try:
            acc_id = int(data.split(":", 1)[1])
        except:
            return await safe_edit(update, "Invalid account id.")

        acc = get_account_by_id(acc_id)
        if not acc or acc[1] != uid:
            return await safe_edit(update, "❌ Account not found.")

        is_fw = bool(acc[6])
        if not is_fw:
            ok = await start_forward_for_account(acc_id)
            if ok:
                return await safe_edit(update, "✅ Forwarding started.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="ui_manage_accounts")]]))
            return await safe_edit(update, "❌ Failed to start forwarding.")
        else:
            await stop_forward_for_account(acc_id)
            return await safe_edit(update, "⏸ Forwarding stopped.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="ui_manage_accounts")]]))

    # DELETE ACCOUNT
    if data.startswith("acc_delete:"):
        try:
            acc_id = int(data.split(":", 1)[1])
        except:
            return await safe_edit(update, "Invalid account id.")

        acc = get_account_by_id(acc_id)
        if not acc or acc[1] != uid:
            return await safe_edit(update, "❌ Account not found.")

        delete_account(acc_id)
        return await safe_edit(update, "🗑 Account deleted.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="ui_manage_accounts")]]))

    # ---------- REDEEM CODE ----------
    if data == "ui_redeem":
        set_state(uid, "waiting_redeem", {})
        return await safe_edit(update, "🎟 Send your redeem code:")

    # ---------- SETTINGS ----------
    if data == "ui_settings":
        row = get_user_row(uid)
        delay = (row[4] if row else 300)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⏳ Set Delay", callback_data="ui_set_delay")],
            [InlineKeyboardButton("⬅️ Back", callback_data="ui_menu")]
        ])
        return await safe_edit(
            update,
            f"⚙️ *Settings*\n\nCurrent delay: *{delay//60} minutes*",
            reply_markup=kb,
            parse_mode=ParseMode.MARKDOWN
        )

    # SET DELAY MENU
    if data == "ui_set_delay":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("10 min (Very Safe)", callback_data="delay_600")],
            [InlineKeyboardButton("7 min (Safe)", callback_data="delay_420")],
            [InlineKeyboardButton("5 min (Normal)", callback_data="delay_300")],
            [InlineKeyboardButton("3 min (Fast/Risky)", callback_data="delay_180")],
            [InlineKeyboardButton("⬅️ Back", callback_data="ui_settings")],
        ])
        return await safe_edit(
            update,
            "⏱ *Forwarding Delay*\n\nChoose how often messages are forwarded.\nShort delays may hit Telegram limits.",
            reply_markup=kb,
            parse_mode=ParseMode.MARKDOWN
        )

    # APPLY DELAY CHANGE
    if data.startswith("delay_"):
        try:
            secs = int(data.split("_", 1)[1])
        except:
            return await safe_edit(update, "Invalid selection.")
        run_db("UPDATE users SET delay_setting = ? WHERE user_id = ?", (secs, uid))
        return await safe_edit(
            update,
            f"✅ Delay updated to *{secs//60} minutes*.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="ui_settings")]]),
            parse_mode=ParseMode.MARKDOWN
        )

    # ---------- ADMIN PANEL ----------
    if data == "ui_admin_panel":
        if not is_admin(uid):
            return await safe_edit(update, "❌ You are not authorized.")
        return await safe_edit(update, "👑 *Admin Panel*",
            reply_markup=admin_panel_kb(),
            parse_mode=ParseMode.MARKDOWN)

    # ADMIN — STATS
    if data == "admin_stats":
        if not is_admin(uid):
            return await safe_edit(update, "❌ Not authorized.")
        users = run_db("SELECT COUNT(*) FROM users", fetch="one")[0]
        accounts = run_db("SELECT COUNT(*) FROM accounts", fetch="one")[0]
        return await safe_edit(update, f"📊 *Stats*\nUsers: {users}\nAccounts: {accounts}",
            parse_mode=ParseMode.MARKDOWN)

    # ADMIN — GENERATE CODE
    if data == "admin_gencode":
        if not is_admin(uid):
            return await safe_edit(update, "❌ Not authorized.")
        set_state(uid, "admin_make_code", {})
        return await safe_edit(
            update,
            "🎟 *Generate Premium Codes*\n\n"
            "📌 *Usage:*\n"
            "`<quantity> <days>`\n"
            "Example: `5 30`\n\n"
            "Send your input:",
            parse_mode=ParseMode.MARKDOWN
        )

    # ADMIN — BAN USER
    if data == "admin_ban":
        if not is_admin(uid):
            return await safe_edit(update, "❌ Not authorized.")
        set_state(uid, "admin_ban_user", {})
        return await safe_edit(
            update,
            "⛔ *Ban User*\n\n"
            "📌 *Usage:* `<user_id>`\nExample: `123456789`",
            parse_mode=ParseMode.MARKDOWN
        )

    # ADMIN — UNBAN USER
    if data == "admin_unban":
        if not is_admin(uid):
            return await safe_edit(update, "❌ Not authorized.")
        set_state(uid, "admin_unban_user", {})
        return await safe_edit(
            update,
            "♻️ *Unban User*\n\n"
            "📌 *Usage:* `<user_id>`\nExample: `123456789`",
            parse_mode=ParseMode.MARKDOWN
        )

    # ADMIN — EXTEND PREMIUM
    if data == "admin_extend":
        if not is_admin(uid):
            return await safe_edit(update, "❌ Not authorized.")
        set_state(uid, "admin_extend_user", {})
        return await safe_edit(
            update,
            "⭐ *Extend Premium*\n\n"
            "📌 *Usage:* `<user_id> <days>`\nExample: `123456789 30`",
            parse_mode=ParseMode.MARKDOWN
        )

    # ADMIN — BROADCAST
    if data == "admin_broadcast":
        if not is_admin(uid):
            return await safe_edit(update, "❌ Not authorized.")
        set_state(uid, "admin_broadcast_msg", {})
        return await safe_edit(
            update,
            "📢 *Broadcast Message*\n\nSend the message you want to send to all users:",
            parse_mode=ParseMode.MARKDOWN
        )

    # FALLBACK
    return await safe_edit(update, "❓ Unknown option. Use /menu")
# ================================================================
# PART 5 — FINAL INTEGRATED: FORWARDER ENGINE + MESSAGE ROUTER + ADMIN COMMANDS
# ================================================================
# Paste this over your existing PART 5. It is written to match the
# Part 4 you already have and the helpers in PART 0-3.

# Globals for forwarder management (will reuse if already defined)
try:
    _active_clients
except NameError:
    _active_clients = {}        # key -> Telethon client

try:
    _forward_tasks
except NameError:
    _forward_tasks = {}         # owner_id -> { account_id: asyncio.Task }

# Ensure required DB columns exist (safe to run on startup)
def ensure_user_columns():
    conn = _get_conn()
    cur = conn.cursor()
    try:
        cur.execute("PRAGMA table_info(users)")
        cols = [r[1] for r in cur.fetchall()]
        if "delay_setting" not in cols:
            try:
                cur.execute("ALTER TABLE users ADD COLUMN delay_setting INTEGER DEFAULT 600")
            except:
                pass
        if "is_banned" not in cols:
            try:
                cur.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0")
            except:
                pass
    except Exception:
        pass
    finally:
        conn.commit()
        conn.close()

# ---------------------- ADMIN (slash) COMMANDS ----------------------

async def genkey_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return await safe_reply(update, "❌ You are not an admin.")
    args = context.args or []
    if len(args) != 2:
        return await safe_reply(update, "Usage:\n<b>/genkey QUANTITY DAYS</b>", parse_mode=ParseMode.HTML)
    try:
        qty = int(args[0]); days = int(args[1])
    except:
        return await safe_reply(update, "❌ Qty and Days must be numbers.")
    codes = [create_redeem_code(days, uid) for _ in range(qty)]
    return await safe_reply(update, "🎟 <b>Generated Codes:</b>\n" + "\n".join(codes), parse_mode=ParseMode.HTML)

async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return await safe_reply(update, "❌ Not authorized.")
    if not context.args:
        return await safe_reply(update, "Usage:\n<b>/ban USER_ID</b>", parse_mode=ParseMode.HTML)
    try:
        target = int(context.args[0])
    except:
        return await safe_reply(update, "❌ Invalid user id.")
    run_db("UPDATE users SET is_banned = 1 WHERE user_id = ?", (target,))
    return await safe_reply(update, f"⛔ Banned <b>{target}</b>", parse_mode=ParseMode.HTML)

async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return await safe_reply(update, "❌ Not authorized.")
    if not context.args:
        return await safe_reply(update, "Usage:\n<b>/unban USER_ID</b>", parse_mode=ParseMode.HTML)
    try:
        target = int(context.args[0])
    except:
        return await safe_reply(update, "❌ Invalid user id.")
    run_db("UPDATE users SET is_banned = 0 WHERE user_id = ?", (target,))
    return await safe_reply(update, f"♻ Unbanned <b>{target}</b>", parse_mode=ParseMode.HTML)

async def extend_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return await safe_reply(update, "❌ Not authorized.")
    args = context.args or []
    if len(args) != 2:
        return await safe_reply(update, "Usage:\n<b>/extend USER_ID DAYS</b>", parse_mode=ParseMode.HTML)
    try:
        target = int(args[0]); days = int(args[1])
    except:
        return await safe_reply(update, "❌ Invalid parameters.")
    new_expiry = extend_premium(target, days)
    return await safe_reply(update, f"⭐ Extended premium for <b>{target}</b>\nNew Expiry: {new_expiry}", parse_mode=ParseMode.HTML)

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return await safe_reply(update, "❌ Not authorized.")
    msg = " ".join(context.args or [])
    if not msg:
        return await safe_reply(update, "Usage:\n<b>/broadcast MESSAGE</b>", parse_mode=ParseMode.HTML)
    rows = run_db("SELECT user_id FROM users WHERE is_banned = 0", fetch="all") or []
    sent = 0
    for r in rows:
        try:
            await BOT_APP.bot.send_message(int(r[0]), msg)
            sent += 1
        except:
            continue
    return await safe_reply(update, f"📢 Broadcast sent to <b>{sent}</b> users.", parse_mode=ParseMode.HTML)

# ---------------------- TELETHON CLIENT MANAGER ----------------------

def _client_key(owner_id: int, account_id: int) -> str:
    return f"user_{owner_id}_acc_{account_id}"

async def get_client_for_account(account_row):
    """
    Normalize account_row and create/connect Telethon client.
    Accepts multiple column-order possibilities.
    Expects some session_file path and api_id/api_hash to be discoverable.
    """
    if not account_row:
        return None

    row = list(account_row)
    account_id = int(row[0])
    owner_id = int(row[1])

    # Try to find session_file, api_id, api_hash, phone heuristically
    session_file = None
    api_id = None
    api_hash = None
    phone = None

    # find session-like string
    for v in row[2:]:
        if isinstance(v, str) and (v.startswith(SESSIONS_DIR) or v.endswith(".session") or "user_" in v):
            session_file = v
            break
    # heuristics fallback
    if session_file is None:
        if len(row) > 2 and isinstance(row[2], str):
            session_file = row[2]
        elif len(row) > 5 and isinstance(row[5], str):
            session_file = row[5]

    # attempt common positions
    try:
        phone = str(row[2]) if len(row) > 2 else None
        api_id = int(row[4]) if len(row) > 4 else None
        api_hash = str(row[5]) if len(row) > 5 else None
    except Exception:
        pass

    # alternate attempt
    if api_id is None or api_hash is None:
        try:
            api_id = int(row[3]) if len(row) > 3 else api_id
            api_hash = str(row[4]) if len(row) > 4 else api_hash
        except Exception:
            pass

    # last-resort search
    if (api_id is None or api_hash is None) and len(row) > 2:
        for v in row:
            if api_id is None and isinstance(v, (int,)):
                api_id = int(v)
            elif api_hash is None and isinstance(v, str) and len(v) > 10:
                api_hash = v
            if api_id is not None and api_hash is not None:
                break

    if session_file is None or api_id is None or api_hash is None:
        log.warning("Account %s: couldn't infer session/api from row: %s", account_id, row)
        return None

    key = _client_key(owner_id, account_id)

    # reuse cached client
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
        except Exception:
            try:
                await client.disconnect()
            except:
                pass
            _active_clients.pop(key, None)

    # create and connect
    try:
        client = TelegramClient(session_file, int(api_id), str(api_hash))
        await client.connect()
        if not await client.is_user_authorized():
            try:
                await client.disconnect()
            except:
                pass
            return None
        _active_clients[key] = client
        return client
    except Exception:
        try:
            await client.disconnect()
        except:
            pass
        log.exception("Failed to create/connect client for account %s", account_id)
        return None

# ---------------------- FORWARDER LOOP ----------------------

async def forwarder_loop(account_id: int):
    """
    Long-running forward loop for a single account.
    Uses BOT_APP for bot notifications (BOT_APP must be set in on_start).
    """
    acc = get_account_by_id(account_id)
    if not acc:
        return

    # normalize row
    row = list(acc)
    account_id = int(row[0])
    owner_id = int(row[1])

    # mark in DB
    try:
        run_db("UPDATE accounts SET is_forwarding = 1 WHERE account_id = ?", (account_id,))
    except:
        pass

    client = await get_client_for_account(acc)
    if not client:
        run_db("UPDATE accounts SET is_forwarding = 0 WHERE account_id = ?", (account_id,))
        try:
            await BOT_APP.bot.send_message(owner_id, "⚠️ Failed to start forwarder: session unauthorized or invalid.")
        except:
            pass
        return

    # initial bio check
    try:
        me = await client.get_me()
        profile = await client(GetFullUserRequest(me.id))
        bio = getattr(profile.full_user, "about", "") or ""
    except Exception:
        bio = ""

    if BIO_REQUIRED_TEXT not in bio:
        try:
            await BOT_APP.bot.send_message(owner_id, f"⚠️ Add \"{BIO_REQUIRED_TEXT}\" to your Telegram bio to enable forwarding.")
        except:
            pass
        run_db("UPDATE accounts SET is_forwarding = 0 WHERE account_id = ?", (account_id,))
        return

    # build recipients list (groups and non-broadcast channels)
    recipients = []
    try:
        async for dialog in client.iter_dialogs():
            try:
                if getattr(dialog, "is_group", False):
                    recipients.append(dialog.entity)
                elif getattr(dialog, "is_channel", False):
                    ent = getattr(dialog, "entity", None)
                    if ent and not getattr(ent, "broadcast", True):
                        recipients.append(dialog.entity)
            except Exception:
                continue
    except Exception:
        pass

    last_msg_id = None
    user_row = get_user_row(owner_id) or ()
    delay = user_row[4] if len(user_row) > 4 and user_row[4] else 600

    try:
        while True:
            try:
                msgs = await client.get_messages('me', limit=1)
                if msgs:
                    msg = msgs[0]
                    if last_msg_id is None or msg.id != last_msg_id:
                        for chat in recipients:
                            try:
                                await client.forward_messages(chat, msg.id, from_peer='me')
                                await asyncio.sleep(0.35)
                            except Exception:
                                continue
                        last_msg_id = msg.id
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(5)

            # bio re-check
            try:
                me = await client.get_me()
                profile = await client(GetFullUserRequest(me.id))
                bio = getattr(profile.full_user, "about", "") or ""
            except:
                bio = ""

            if BIO_REQUIRED_TEXT not in bio:
                try:
                    await BOT_APP.bot.send_message(owner_id, "⚠️ Required bio text missing. Forwarding stopped.")
                except:
                    pass
                break

            # refresh delay from DB
            user_row = get_user_row(owner_id) or ()
            delay = user_row[4] if len(user_row) > 4 and user_row[4] else delay

            await asyncio.sleep(delay)

    finally:
        # cleanup
        try:
            run_db("UPDATE accounts SET is_forwarding = 0 WHERE account_id = ?", (account_id,))
        except:
            pass
        try:
            if owner_id in _forward_tasks and account_id in _forward_tasks[owner_id]:
                _forward_tasks[owner_id].pop(account_id, None)
        except:
            pass

# ---------------------- START / STOP / RESUME ----------------------

async def start_forward_for_account(account_id: int):
    acc = get_account_by_id(account_id)
    if not acc:
        return False
    owner_id = int(acc[1])
    if owner_id not in _forward_tasks:
        _forward_tasks[owner_id] = {}
    if account_id in _forward_tasks[owner_id]:
        return False
    t = asyncio.create_task(forwarder_loop(account_id))
    _forward_tasks[owner_id][account_id] = t
    return True

async def stop_forward_for_account(account_id: int):
    acc = get_account_by_id(account_id)
    if not acc:
        return False
    owner_id = int(acc[1])
    try:
        if owner_id in _forward_tasks and account_id in _forward_tasks[owner_id]:
            t = _forward_tasks[owner_id].pop(account_id, None)
            if t:
                t.cancel()
    except:
        pass
    try:
        run_db("UPDATE accounts SET is_forwarding = 0 WHERE account_id = ?", (account_id,))
    except:
        pass
    return True

async def resume_forwarders():
    rows = run_db("SELECT account_id FROM accounts WHERE is_forwarding = 1", fetch="all") or []
    for r in rows:
        try:
            aid = int(r[0])
            await start_forward_for_account(aid)
        except Exception:
            continue

# ---------------------- MESSAGE ROUTER (handles states & admin inputs) ----------------------

async def message_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Routes messages depending on user state.
    - login states -> handle_login_states()
    - redeem -> waiting_redeem
    - admin states -> admin_make_code, admin_ban_user, admin_unban_user, admin_extend_user, admin_broadcast_msg
    """
    user = update.effective_user
    if not user:
        return
    uid = user.id
    text = (update.message.text or "").strip() if update.message else ""

    # if in login flow, delegate
    st, payload = get_state(uid)
    if st and st.startswith("login_"):
        return await handle_login_states(update, context)

    # Redeem
    if st == "waiting_redeem":
        code = text.strip()
        clear_state(uid)
        row = run_db("SELECT code, days, is_used FROM redeem_codes WHERE code = ?", (code,), fetch="one")
        if not row:
            return await safe_reply(update, "❌ Invalid redeem code.")
        if row[2] == 1:
            return await safe_reply(update, "❌ Code already used.")
        run_db("UPDATE redeem_codes SET is_used = 1, used_by = ?, used_date = ? WHERE code = ?", (uid, datetime.utcnow().isoformat(), code))
        days = int(row[1])
        new_expiry = extend_premium(uid, days)
        return await safe_reply(update, f"✅ Redeem successful! Extended by {days} days. New expiry: {new_expiry}")

    # Admin: generate codes (state)
    if st == "admin_make_code":
        if not is_admin(uid):
            clear_state(uid); return await safe_reply(update, "❌ Not authorized.")
        parts = text.split()
        clear_state(uid)
        if len(parts) != 2:
            return await safe_reply(update, "Usage: <quantity> <days>\nExample: 5 30")
        try:
            qty = int(parts[0]); days = int(parts[1])
        except:
            return await safe_reply(update, "❌ Invalid input.")
        codes = [create_redeem_code(days, uid) for _ in range(qty)]
        return await safe_reply(update, "🎟 Generated Codes:\n" + "\n".join(codes))

    # Admin: ban user
    if st == "admin_ban_user":
        if not is_admin(uid):
            clear_state(uid); return await safe_reply(update, "❌ Not authorized.")
        try:
            target = int(text.strip())
        except:
            clear_state(uid); return await safe_reply(update, "❌ Invalid user id.")
        clear_state(uid)
        run_db("UPDATE users SET is_banned = 1 WHERE user_id = ?", (target,))
        return await safe_reply(update, f"⛔ Banned {target}")

    # Admin: unban user
    if st == "admin_unban_user":
        if not is_admin(uid):
            clear_state(uid); return await safe_reply(update, "❌ Not authorized.")
        try:
            target = int(text.strip())
        except:
            clear_state(uid); return await safe_reply(update, "❌ Invalid user id.")
        clear_state(uid)
        run_db("UPDATE users SET is_banned = 0 WHERE user_id = ?", (target,))
        return await safe_reply(update, f"♻ Unbanned {target}")

    # Admin: extend premium
    if st == "admin_extend_user":
        if not is_admin(uid):
            clear_state(uid); return await safe_reply(update, "❌ Not authorized.")
        parts = text.split()
        if len(parts) != 2:
            clear_state(uid); return await safe_reply(update, "Usage: <user_id> <days>")
        try:
            target = int(parts[0]); days = int(parts[1])
        except:
            clear_state(uid); return await safe_reply(update, "❌ Invalid input.")
        clear_state(uid)
        new_expiry = extend_premium(target, days)
        return await safe_reply(update, f"⭐ Extended user {target} by {days} days. New expiry: {new_expiry}")

    # Admin: broadcast
    if st == "admin_broadcast_msg":
        if not is_admin(uid):
            clear_state(uid); return await safe_reply(update, "❌ Not authorized.")
        msg = text
        clear_state(uid)
        rows = run_db("SELECT user_id FROM users WHERE is_banned = 0", fetch="all") or []
        sent = 0
        for r in rows:
            try:
                await BOT_APP.bot.send_message(int(r[0]), msg)
                sent += 1
            except:
                continue
        return await safe_reply(update, f"📢 Broadcast sent to {sent} users.")

    # Default fallback
    return await safe_reply(update, "Unknown message. Use /menu")

# ---------------------- STARTUP / BACKGROUND TASKS ----------------------

async def keep_alive_loop():
    while True:
        try:
            log.info("Keepalive heartbeat")
        except:
            pass
        await asyncio.sleep(300)

async def on_start(app_obj: Application):
    """
    Called by Application.post_init. Sets BOT_APP and starts background tasks.
    """
    global BOT_APP
    BOT_APP = app_obj
    ensure_user_columns()
    # start keepalive
    try:
        app_obj.create_task(keep_alive_loop())
    except Exception:
        asyncio.create_task(keep_alive_loop())
    # resume forwarders
    try:
        await resume_forwarders()
    except Exception:
        log.exception("resume_forwarders failed")
    log.info("on_start finished.")

# ---------------------- REGISTER ADMIN COMMANDS ----------------------

def register_admin_commands(app: Application):
    app.add_handler(CommandHandler("genkey", genkey_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CommandHandler("extend", extend_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))

# End of PART 5
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