# ============================
# PART 0 — CORE SETUP (PATCHED FOR PYTHON 3.13)
# HINARI ADS BOT (multi-account + BIO check)
# Created by @NOTCH2ND
# ============================

import sys
import types

# --------------------------------------
# 🔥 Python 3.13 Fix for Telethon
# Telethon imports "imghdr", which no longer exists in Python 3.13
# So we create a fake module before Telethon loads.
# --------------------------------------
fake_imghdr = types.ModuleType("imghdr")
fake_imghdr.what = lambda *args, **kwargs: None
sys.modules["imghdr"] = fake_imghdr

# Now safe to continue
# --------------------------------------

import asyncio
import sqlite3
import os
import json
import secrets
import string
import logging
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.tl.functions.users import GetFullUserRequest

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("hinari")

# ----------------------------
# CONFIG (your token inserted)
# ----------------------------
BOT_TOKEN = "8399763411:AAGVzQJqCkwMWgnEUV1_7GRHQtCSz-j5-yI"  # <- your token
ADMIN_IDS = [7765446998]  # <- admin
DB_FILE = "users.db"
SESSIONS_DIR = "sessions"
os.makedirs(SESSIONS_DIR, exist_ok=True)

# BIO enforcement required text
BIO_REQUIRED_TEXT = "By HinariAdsBot"

# ----------------------------
# GLOBALS
# ----------------------------
active_clients = {}        # map keys -> Telethon clients
forward_tasks = {}         # { user_id: { account_id: task } }
daily_tasks_started = False
DEFAULT_DELAY = 300        # seconds (5 minutes)
BOT_APP = None             # set on startup        # will be set to Application.bot at startup
# ============================
# PART 1 — DATABASE & HELPERS
# ============================

def run_db(query, params=(), fetch=False):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(query, params)
    result = None
    if fetch:
        result = c.fetchall()
    conn.commit()
    conn.close()
    return result

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        phone TEXT,
        joined_date TEXT,
        premium_expiry TEXT,
        trial_start TEXT,
        delay_setting INTEGER DEFAULT 300,
        is_banned INTEGER DEFAULT 0
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS user_states (
        user_id INTEGER PRIMARY KEY,
        state TEXT,
        temp_data TEXT
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS redeem_codes (
        code TEXT PRIMARY KEY,
        days INTEGER,
        created_by INTEGER,
        created_date TEXT,
        used_by INTEGER DEFAULT NULL,
        used_date TEXT DEFAULT NULL,
        is_used INTEGER DEFAULT 0
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS banned_users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        banned_by INTEGER,
        banned_date TEXT,
        reason TEXT
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS user_accounts (
        account_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        phone TEXT,
        api_id TEXT,
        api_hash TEXT,
        session_file TEXT,
        is_active INTEGER DEFAULT 1,
        is_forwarding INTEGER DEFAULT 0,
        created_at TEXT
    )""")

    conn.commit()
    conn.close()

init_db()

# User state helpers
def set_user_state(user_id, state, temp_data=None):
    temp_json = json.dumps(temp_data) if temp_data else None
    run_db("INSERT OR REPLACE INTO user_states (user_id, state, temp_data) VALUES (?, ?, ?)",
           (user_id, state, temp_json))

def get_user_state(user_id):
    r = run_db("SELECT state, temp_data FROM user_states WHERE user_id = ?", (user_id,), fetch=True)
    if not r:
        return None, None
    state, temp_json = r[0]
    temp = json.loads(temp_json) if temp_json else None
    return state, temp

def clear_user_state(user_id):
    run_db("DELETE FROM user_states WHERE user_id = ?", (user_id,))

# User / premium helpers
def get_user_data(user_id):
    r = run_db("SELECT * FROM users WHERE user_id = ?", (user_id,), fetch=True)
    return r[0] if r else None

def create_or_refresh_user(user_id):
    user = get_user_data(user_id)
    now = datetime.utcnow().isoformat()
    if not user:
        trial_end = (datetime.utcnow() + timedelta(days=7)).isoformat()
        run_db("INSERT OR REPLACE INTO users (user_id, joined_date, premium_expiry, trial_start) VALUES (?, ?, ?, ?)",
               (user_id, now, trial_end, now))
        return get_user_data(user_id)
    return user

def is_user_banned(user_id):
    row = get_user_data(user_id)
    return bool(row and row[6] == 1) if row else False

def get_premium_days_left(user_row):
    if not user_row:
        return 0
    expiry = user_row[3]
    if not expiry:
        return 0
    try:
        left = datetime.fromisoformat(expiry) - datetime.utcnow()
        return max(0, left.days)
    except:
        return 0

def user_is_premium(user_row):
    if not user_row:
        return False
    try:
        return datetime.fromisoformat(user_row[3]) > datetime.utcnow()
    except:
        return False

# Multi-account helpers
def make_session_filename(user_id, account_id=None):
    if account_id:
        return f"{SESSIONS_DIR}/user_{user_id}_acc_{account_id}"
    return f"{SESSIONS_DIR}/user_{user_id}_{int(datetime.utcnow().timestamp())}"

def add_account(user_id, phone, api_id, api_hash, session_file=None):
    now = datetime.utcnow().isoformat()
    run_db("INSERT INTO user_accounts (user_id, phone, api_id, api_hash, session_file, created_at) VALUES (?, ?, ?, ?, ?, ?)",
           (user_id, phone, api_id, api_hash, session_file, now))
    rows = run_db("SELECT account_id FROM user_accounts WHERE rowid = (SELECT MAX(rowid) FROM user_accounts)", fetch=True)
    return rows[0][0] if rows else None

def update_account_session_file(account_id, session_file):
    run_db("UPDATE user_accounts SET session_file = ? WHERE account_id = ?", (session_file, account_id))

def get_accounts_for_user(user_id):
    return run_db("SELECT account_id, user_id, phone, api_id, api_hash, session_file, is_active, is_forwarding, created_at FROM user_accounts WHERE user_id = ? ORDER BY account_id ASC",
                  (user_id,), fetch=True) or []

def get_account(account_id):
    r = run_db("SELECT account_id, user_id, phone, api_id, api_hash, session_file, is_active, is_forwarding, created_at FROM user_accounts WHERE account_id = ?",
               (account_id,), fetch=True)
    return r[0] if r else None

def delete_account(account_id):
    acc = get_account(account_id)
    if not acc:
        return None, False
    session_file = acc[5]
    run_db("DELETE FROM user_accounts WHERE account_id = ?", (account_id,))
    try:
        if session_file:
            for suf in ("", ".session", ".session-journal"):
                p = session_file + suf
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except:
                        pass
    except:
        pass
    return session_file, True

def count_accounts_for_user(user_id):
    r = run_db("SELECT COUNT(*) FROM user_accounts WHERE user_id = ?", (user_id,), fetch=True)
    return int(r[0][0]) if r else 0

def user_can_add_account(user_id):
    user = get_user_data(user_id)
    if user_is_premium(user):
        return True, ""
    cnt = count_accounts_for_user(user_id)
    if cnt >= 1:
        msg = (
            "⚠️ Free trial users may connect only a single Telegram account.\n\n"
            "Upgrade to Premium (₹59/month) for unlimited accounts. Contact @NOTCH2ND."
        )
        return False, msg
    return True, ""
    # ============================
# PART 2 — LOGIN FLOW (OTP + 2FA) (multi-account)
# ============================

async def login_flow_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    state, temp = get_user_state(user_id)

    # STEP 1 — API ID
    if state == "waiting_api_id":
        try:
            api_id = int(text)
        except:
            return await update.message.reply_text("❌ Invalid API ID. Send only numbers.")
        temp = {"api_id": api_id}
        set_user_state(user_id, "waiting_api_hash", temp)
        return await update.message.reply_text("🔑 Now send your <b>API HASH</b>:", parse_mode=ParseMode.HTML)

    # STEP 2 — API HASH
    if state == "waiting_api_hash":
        temp = temp or {}
        temp["api_hash"] = text.strip()
        set_user_state(user_id, "waiting_phone", temp)
        return await update.message.reply_text("📱 Send your phone (with country code), e.g. +919812345678")

    # STEP 3 — PHONE → create account row and send OTP
    if state == "waiting_phone":
        temp = temp or {}
        phone = text.strip()
        allowed, msg = user_can_add_account(user_id)
        if not allowed:
            clear_user_state(user_id)
            return await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

        temp["phone"] = phone
        api_id = temp["api_id"]
        api_hash = temp["api_hash"]

        acc_id = add_account(user_id, phone, str(api_id), api_hash, None)
        session_file = make_session_filename(user_id, acc_id)
        update_account_session_file(acc_id, session_file)

        client = TelegramClient(session_file, api_id, api_hash)
        try:
            await client.connect()
            await client.send_code_request(phone)
        except Exception as e:
            try:
                await client.disconnect()
            except:
                pass
            delete_account(acc_id)
            clear_user_state(user_id)
            return await update.message.reply_text(f"❌ Failed to send code: {e}")

        temp.update({"acc_id": acc_id, "session_file": session_file})
        active_clients[f"login_{user_id}"] = client
        set_user_state(user_id, "waiting_code", temp)
        return await update.message.reply_text("📩 OTP sent! Enter the code:")

    # STEP 4 — CODE
    if state == "waiting_code":
        temp = temp or {}
        client = active_clients.get(f"login_{user_id}")
        if not client:
            try:
                client = TelegramClient(temp["session_file"], int(temp["api_id"]), temp["api_hash"])
                await client.connect()
                active_clients[f"login_{user_id}"] = client
            except Exception as e:
                clear_user_state(user_id)
                return await update.message.reply_text(f"❌ Session lost: {e}")

        try:
            try:
                await client.sign_in(temp["phone"], text)
            except SessionPasswordNeededError:
                set_user_state(user_id, "waiting_2fa", temp)
                return await update.message.reply_text("🔐 2FA is enabled. Send your password:")
            # success
            await client.disconnect()
            active_clients.pop(f"login_{user_id}", None)
            clear_user_state(user_id)
            # ensure user record exists and trial applied
            create_or_refresh_user(user_id)
            return await update.message.reply_text("🎉 <b>Account connected successfully!</b>", parse_mode=ParseMode.HTML)
        except Exception as e:
            return await update.message.reply_text(f"❌ Incorrect code: {e}")

    # STEP 5 — 2FA
    if state == "waiting_2fa":
        temp = temp or {}
        acc_id = temp.get("acc_id")
        client = active_clients.get(f"login_{user_id}")
        if not client:
            try:
                client = TelegramClient(temp["session_file"], temp["api_id"], temp["api_hash"])
                await client.connect()
                active_clients[f"login_{user_id}"] = client
            except Exception as e:
                clear_user_state(user_id)
                delete_account(acc_id)
                return await update.message.reply_text(f"❌ Could not restore session: {e}")

        try:
            await client.sign_in(password=text)
            await client.disconnect()
            active_clients.pop(f"login_{user_id}", None)
            clear_user_state(user_id)
            create_or_refresh_user(user_id)
            return await update.message.reply_text("🔐 <b>Login successful with 2FA!</b>", parse_mode=ParseMode.HTML)
        except Exception as e:
            delete_account(acc_id)
            clear_user_state(user_id)
            return await update.message.reply_text(f"❌ Wrong 2FA password: {e}")

    return None
    # ============================
    # ============================
# PART 3 — MESSAGE ROUTER (FINAL)
# ============================

async def message_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip() if update.message and update.message.text else ""

    state, temp = get_user_state(user_id)

    # Check banned
    if is_user_banned(user_id):
        return await update.message.reply_text("❌ You are banned.")

    # Admin special states
    if is_admin(user_id) and state and state.startswith("admin_"):
        return await handle_admin_states(update, text, user_id, state, temp)

    # Redeem code flow
    if state == "waiting_redeem_code":
        clear_user_state(user_id)
        return await handle_redeem_code(update, text, user_id)

    # Login flow (API ID → HASH → PHONE → OTP → 2FA)
    if state and state.startswith("waiting_"):
        result = await login_flow_handler(update, context, text)
        if result is not None:
            return

    # Default
    return await update.message.reply_text("Use /menu")
# PART 4 — UI + BUTTON HANDLING
# ============================

async def manage_accounts_ui(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    accounts = get_accounts_for_user(user_id)
    if not accounts:
        kb = [[InlineKeyboardButton("➕ Add Account", callback_data="add_account")]]
        return await update.message.reply_text("You have no connected accounts. Add one to start forwarding.", reply_markup=InlineKeyboardMarkup(kb))
    kb = []
    for acc in accounts:
        acc_id = acc[0]; phone = acc[2] or "Unknown"; is_fw = bool(acc[7])
        status = "🟢 Forwarding" if is_fw else "🔴 Stopped"
        row = []
        if is_fw:
            row.append(InlineKeyboardButton("⛔ Stop", callback_data=f"account_stop_{acc_id}"))
        else:
            row.append(InlineKeyboardButton("▶ Start", callback_data=f"account_start_{acc_id}"))
        row.append(InlineKeyboardButton("🗑 Delete", callback_data=f"account_delete_{acc_id}"))
        kb.append(row)
    kb.append([InlineKeyboardButton("➕ Add Account", callback_data="add_account")])
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="menu_back")])
    text = "<b>Your Accounts</b>:\n\n"
    for acc in accounts:
        text += f"• {acc[2] or 'Unknown'} (ID: {acc[0]})\n"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))

async def _handle_account_start_button(query, account_id):
    user_id = query.from_user.id
    acc = get_account(account_id)
    if not acc or acc[1] != user_id:
        return await query.edit_message_text("❌ Account not found.")
    started = await start_forward_for_account(account_id)
    if started:
        return await query.edit_message_text("✅ Account forwarder started.")
    else:
        return await query.edit_message_text("ℹ️ Account forwarder already running.")

async def _handle_account_stop_button(query, account_id):
    user_id = query.from_user.id
    acc = get_account(account_id)
    if not acc or acc[1] != user_id:
        return await query.edit_message_text("❌ Account not found.")
    stopped = await stop_forward_for_account(account_id)
    if stopped:
        return await query.edit_message_text("🛑 Account forwarder stopped.")
    else:
        return await query.edit_message_text("ℹ️ No forwarder was running for this account.")

async def _handle_account_delete_button(query, account_id):
    user_id = query.from_user.id
    acc = get_account(account_id)
    if not acc or acc[1] != user_id:
        return await query.edit_message_text("❌ Account not found.")
    kb = [
        [InlineKeyboardButton("✅ Yes, delete", callback_data=f"account_confirm_delete_{account_id}")],
        [InlineKeyboardButton("❌ Cancel", callback_data="menu_back")]
    ]
    return await query.edit_message_text(f"⚠️ Are you sure you want to delete account {acc[2]} (ID {account_id})? This will stop forwarding and remove the session file.", reply_markup=InlineKeyboardMarkup(kb))

# Button handler (main)
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data
    await query.answer()

    if data == "manage_accounts":
        return await manage_accounts_ui(update, context)

    if data == "add_account":
        set_user_state(user_id, "waiting_api_id")
        return await query.edit_message_text("🆕 <b>Adding a new account</b>\n\nSend your API ID:", parse_mode=ParseMode.HTML)

    if data.startswith("account_start_"):
        acc_id = int(data.split("_")[-1])
        return await _handle_account_start_button(query, acc_id)

    if data.startswith("account_stop_"):
        acc_id = int(data.split("_")[-1])
        return await _handle_account_stop_button(query, acc_id)

    if data.startswith("account_delete_"):
        acc_id = int(data.split("_")[-1])
        return await _handle_account_delete_button(query, acc_id)

    if data.startswith("account_confirm_delete_"):
        acc_id = int(data.split("_")[-1])
        ok = await delete_account_and_stop(acc_id)
        if ok:
            return await query.edit_message_text("🗑 Account deleted successfully.")
        else:
            return await query.edit_message_text("❌ Failed to delete account.")

    if data == "redeem_code":
        set_user_state(user_id, "waiting_redeem_code")
        return await query.edit_message_text("🎫 Send your redeem code:")

    if data == "menu_back":
        return await menu_handler(update, context)

    if data == "admin_panel":
        return await admin_panel_handler(update, context)

    return await query.edit_message_text("Unknown option. Use /menu")
    # ============================
# PART 5 — ADMIN / REDEEM / DAILY / MAIN
# ============================

def create_redeem_code(days, created_by):
    code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(12))
    run_db("INSERT INTO redeem_codes (code, days, created_by, created_date) VALUES (?, ?, ?, ?)",
           (code, days, created_by, datetime.utcnow().isoformat()))
    return code

def use_redeem_code(code, user_id):
    r = run_db("SELECT * FROM redeem_codes WHERE code = ? AND is_used = 0", (code,), fetch=True)
    if not r:
        return None
    days = r[0][1]
    run_db("UPDATE redeem_codes SET used_by=?, used_date=?, is_used=1 WHERE code=?", (user_id, datetime.utcnow().isoformat(), code))
    extend_premium(user_id, days)
    return days

def extend_premium(user_id, days):
    user = get_user_data(user_id)
    if user:
        try:
            current = datetime.fromisoformat(user[3])
        except:
            current = datetime.utcnow()
        if current < datetime.utcnow():
            new = datetime.utcnow() + timedelta(days=days)
        else:
            new = current + timedelta(days=days)
        run_db("UPDATE users SET premium_expiry=? WHERE user_id=?", (new.isoformat(), user_id))
        return new.isoformat()
    else:
        new = datetime.utcnow() + timedelta(days=days)
        run_db("INSERT INTO users (user_id, joined_date, premium_expiry) VALUES (?, ?, ?)", (user_id, datetime.utcnow().isoformat(), new.isoformat()))
        return new.isoformat()

async def handle_redeem_code(update: Update, text: str, user_id: int):
    days = use_redeem_code(text, user_id)
    if not days:
        return await update.message.reply_text("❌ Invalid or already used code.")
    return await update.message.reply_text(f"🎉 Code redeemed! Premium extended by {days} days.")

async def admin_panel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        return await update.message.reply_text("❌ You are not an admin.")
    users_count = run_db("SELECT COUNT(*) FROM users", fetch=True)[0][0]
    accounts_count = run_db("SELECT COUNT(*) FROM user_accounts", fetch=True)[0][0]
    kb = [
        [InlineKeyboardButton("📊 Stats", callback_data="admin_stats")],
        [InlineKeyboardButton("🎟 Generate Code", callback_data="admin_gen")],
        [InlineKeyboardButton("⛔ Ban User", callback_data="admin_ban")],
        [InlineKeyboardButton("♻ Unban User", callback_data="admin_unban")],
        [InlineKeyboardButton("⭐ Extend Premium", callback_data="admin_extend")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")],
    ]
    text = (
        "<b>👑 Admin Panel</b>\n\n"
        f"👥 Users: {users_count}\n"
        f"🔗 Accounts: {accounts_count}\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))

async def handle_admin_states(update: Update, text: str, user_id: int, state: str, temp):
    if state == "admin_waiting_redeem_days":
        try:
            days = int(text)
        except:
            return await update.message.reply_text("❌ Send a valid number.")
        code = create_redeem_code(days, user_id)
        clear_user_state(user_id)
        return await update.message.reply_text(f"🎟 Redeem code created:\n<code>{code}</code>", parse_mode=ParseMode.HTML)

    if state == "admin_waiting_ban":
        try:
            uid = int(text)
        except:
            return await update.message.reply_text("❌ Invalid user ID.")
        run_db("UPDATE users SET is_banned=1 WHERE user_id=?", (uid,))
        run_db("INSERT OR REPLACE INTO banned_users (user_id, username, banned_by, banned_date, reason) VALUES (?, ?, ?, ?, ?)",
               (uid, "unknown", user_id, datetime.utcnow().isoformat(), "banned by admin"))
        clear_user_state(user_id)
        return await update.message.reply_text(f"⛔ User {uid} banned.")

    if state == "admin_waiting_unban":
        try:
            uid = int(text)
        except:
            return await update.message.reply_text("❌ Invalid user ID.")
        run_db("UPDATE users SET is_banned=0 WHERE user_id=?", (uid,))
        run_db("DELETE FROM banned_users WHERE user_id=?", (uid,))
        clear_user_state(user_id)
        return await update.message.reply_text(f"♻ User {uid} unbanned.")

    if state == "admin_waiting_extend":
        try:
            uid, days = text.split(); uid = int(uid); days = int(days)
        except:
            return await update.message.reply_text("❌ Format invalid. Use: user_id days")
        new_expiry = extend_premium(uid, days)
        clear_user_state(user_id)
        return await update.message.reply_text(f"⭐ Premium extended until:\n<b>{new_expiry}</b>", parse_mode=ParseMode.HTML)

    if state == "admin_waiting_broadcast":
        users = run_db("SELECT user_id FROM users", fetch=True) or []
        success = fail = 0
        for u in users:
            try:
                await update.get_bot().send_message(u[0], text)
                success += 1
            except:
                fail += 1
        clear_user_state(user_id)
        return await update.message.reply_text(f"📢 Broadcast complete!\n✔ Sent: {success}\n❌ Failed: {fail}")

    return None

async def daily_premium_status_task(app: Application):
    await app.wait_until_ready()
    while True:
        users = run_db("SELECT user_id, premium_expiry, trial_start FROM users", fetch=True) or []
        now = datetime.utcnow()
        for row in users:
            uid = row[0]
            try:
                prem_days = 0
                if row[1]:
                    try:
                        prem_days = max(0, (datetime.fromisoformat(row[1]) - now).days)
                    except:
                        prem_days = 0
                trial_days_left = 0
                if row[2]:
                    try:
                        trial_days_left = max(0, (datetime.fromisoformat(row[2]) + timedelta(days=7) - now).days)
                    except:
                        trial_days_left = 0
                accs = run_db("SELECT COUNT(*) FROM user_accounts WHERE user_id = ?", (uid,), fetch=True)
                acc_count = accs[0][0] if accs else 0

                msg = (
                    "📅 <b>Your Daily Account Status</b>\n\n"
                    f"🎁 Trial: <b>{trial_days_left} day(s)</b> left\n"
                    f"💎 Premium: <b>{prem_days} day(s)</b> left\n"
                    f"🔗 Connected Accounts: <b>{acc_count}</b>\n\n"
                    "To upgrade to Premium (unlimited accounts & priority forwarding) contact @NOTCH2ND"
                )

                try:
                    if BOT_APP:
                        await BOT_APP.send_message(uid, msg, parse_mode=ParseMode.HTML)
                except:
                    pass
            except:
                continue

        await asyncio.sleep(24 * 3600)

async def resume_active_forwarders_on_start(app: Application):
    rows = run_db("SELECT account_id FROM user_accounts WHERE is_forwarding = 1", fetch=True) or []
    restarted = 0
    for r in rows:
        acc_id = r[0]
        try:
            await start_forward_for_account(acc_id)
            restarted += 1
        except:
            continue
    log.info("Resumed %d forwarders on startup", restarted)

# START / MENU commands (ensure these are present)
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_user_banned(user_id):
        return await update.message.reply_text("❌ You are banned.")
    create_or_refresh_user(user_id)
    intro = (
        "<b>🌟 Welcome to HinariAdsBot 🌟</b>\n\n"
        "Created by <b>@NOTCH2ND</b>\n\n"
        "🎁 <b>1 WEEK FREE TRIAL</b>\n"
        "💎 Premium: ₹59/month — contact @NOTCH2ND\n\n"
        "Use /menu to get started."
    )
    await update.message.reply_text(intro, parse_mode=ParseMode.HTML)

async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user_data(user_id)
    premium_days = get_premium_days_left(user) if user else 0
    premium_text = (f"💎 Premium Active: <b>{premium_days} days</b>" if premium_days > 0 else "💎 Premium: <b>Expired</b>")
    acc_count = count_accounts_for_user(user_id)
    msg = (
        "<b>🔰 HinariAdsBot Menu</b>\n\n"
        f"{premium_text}\n"
        f"👤 Connected Accounts: <b>{acc_count}</b>\n\n"
        "Use the buttons below to manage your setup."
    )
    kb = [
        [InlineKeyboardButton("➕ Add Account", callback_data="add_account")],
        [InlineKeyboardButton("📂 Manage Accounts", callback_data="manage_accounts")],
        [InlineKeyboardButton("🎫 Redeem Code", callback_data="redeem_code")],
    ]
    if update.effective_user.id in ADMIN_IDS:
        kb.append([InlineKeyboardButton("👑 Admin Panel", callback_data="admin_panel")])
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))

# MAIN wiring
def main():
    global BOT_APP, daily_tasks_started
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("menu", menu_handler))
    app.add_handler(CommandHandler("admin", admin_panel_handler))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_router))

    async def on_start(app_obj: Application):
        global BOT_APP, daily_tasks_started
        BOT_APP = app_obj.bot
        if not daily_tasks_started:
            daily_tasks_started = True
            asyncio.create_task(daily_premium_status_task(app_obj))
            await resume_active_forwarders_on_start(app_obj)

    app.post_init = on_start

    print("Bot running…")
    app.run_polling()

if __name__ == "__main__":
    main()