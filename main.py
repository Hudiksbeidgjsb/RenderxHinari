# PART 0 - CONFIG & IMPORTS
import os
import json
import sqlite3
import secrets
import string
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Tuple, Dict, Any, List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# Telethon
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

# Ensure sessions folder exists
os.makedirs("sessions", exist_ok=True)

# -------- CONFIG ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8399763411:AAGVzQJqCkwMWgnEUV1_7GRHQtCSz-j5-yI")  # <-- set your token or env var
ADMIN_IDS = [7765446998]  # add more admins if needed
FREE_TRIAL_DAYS = 7
PREMIUM_PRICE = "59rs/month"  # informational
# --------------------------

# Global container for active telethon clients in login flow
_active_login_clients: Dict[int, TelegramClient] = {}

# Global BOT_APP holder (Telegram Bot)
BOT_APP = None

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger(__name__)
# PART 1 - DATABASE SETUP & HELPERS
DB_FILE = "users.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute("""CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    phone TEXT,
                    joined_date TEXT,
                    premium_expiry TEXT,
                    delay_setting INTEGER DEFAULT 300,
                    api_id TEXT,
                    api_hash TEXT,
                    session_file TEXT,
                    is_active INTEGER DEFAULT 1,
                    is_forwarding INTEGER DEFAULT 0,
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

    c.execute("""CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER,
                    session_file TEXT,
                    phone TEXT,
                    api_id TEXT,
                    api_hash TEXT,
                    created_date TEXT
                )""")

    conn.commit()
    conn.close()

init_db()

# DB helpers
def run_db(query: str, params: tuple = (), fetch: Optional[str] = None):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(query, params)
    if fetch == "one":
        r = c.fetchone()
    elif fetch == "all":
        r = c.fetchall()
    else:
        r = None
    conn.commit()
    conn.close()
    return r

# user state helpers
def set_user_state(user_id: int, state: str, temp_data: Optional[dict] = None):
    temp_json = json.dumps(temp_data) if temp_data else None
    run_db("INSERT OR REPLACE INTO user_states (user_id, state, temp_data) VALUES (?, ?, ?)",
           (user_id, state, temp_json))

def get_user_state(user_id: int) -> Tuple[Optional[str], Optional[dict]]:
    r = run_db("SELECT state, temp_data FROM user_states WHERE user_id = ?", (user_id,), fetch="one")
    if r:
        state, temp_json = r
        return state, json.loads(temp_json) if temp_json else None
    return None, None

def clear_user_state(user_id: int):
    run_db("DELETE FROM user_states WHERE user_id = ?", (user_id,))

# user data
def save_user_data(user_id: int, phone: str, api_id: int, api_hash: str, session_file: str):
    existing = run_db("SELECT user_id FROM users WHERE user_id = ?", (user_id,), fetch="one")
    if existing:
        run_db("UPDATE users SET phone=?, api_id=?, api_hash=?, session_file=?, is_active=1 WHERE user_id=?",
               (phone, str(api_id), api_hash, session_file, user_id))
    else:
        joined = datetime.utcnow().isoformat()
        expiry = (datetime.utcnow() + timedelta(days=FREE_TRIAL_DAYS)).isoformat()
        run_db("INSERT INTO users (user_id, phone, joined_date, premium_expiry, api_id, api_hash, session_file) VALUES (?, ?, ?, ?, ?, ?, ?)",
               (user_id, phone, joined, expiry, str(api_id), api_hash, session_file))

def get_user_data(user_id: int):
    return run_db("SELECT * FROM users WHERE user_id = ?", (user_id,), fetch="one")

def extend_premium(user_id: int, days: int):
    row = get_user_data(user_id)
    if row:
        expiry = datetime.fromisoformat(row[3])
        if expiry < datetime.utcnow():
            new_expiry = datetime.utcnow() + timedelta(days=days)
        else:
            new_expiry = expiry + timedelta(days=days)
        run_db("UPDATE users SET premium_expiry=? WHERE user_id=?", (new_expiry.isoformat(), user_id))
        return new_expiry
    else:
        new_expiry = (datetime.utcnow() + timedelta(days=days)).isoformat()
        run_db("INSERT INTO users (user_id, joined_date, premium_expiry) VALUES (?, ?, ?)",
               (user_id, datetime.utcnow().isoformat(), new_expiry))
        return datetime.fromisoformat(new_expiry)

def is_premium_active(user_id: int) -> bool:
    row = get_user_data(user_id)
    if not row:
        return False
    expiry = datetime.fromisoformat(row[3])
    return expiry > datetime.utcnow()

def get_premium_days_left(user_id: int) -> int:
    row = get_user_data(user_id)
    if not row:
        return 0
    expiry = datetime.fromisoformat(row[3])
    return max(0, (expiry - datetime.utcnow()).days)

def is_user_banned(user_id: int) -> bool:
    row = get_user_data(user_id)
    return bool(row and row[9] == 1)

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# account (multi-account) helpers
def add_account_record(owner_id: int, session_file: str, phone: str, api_id: int, api_hash: str):
    run_db("INSERT INTO accounts (owner_id, session_file, phone, api_id, api_hash, created_date) VALUES (?, ?, ?, ?, ?, ?)",
           (owner_id, session_file, phone, str(api_id), api_hash, datetime.utcnow().isoformat()))

def get_accounts_for_user(owner_id: int):
    return run_db("SELECT id, session_file, phone, api_id FROM accounts WHERE owner_id=?", (owner_id,), fetch="all")

def delete_account_record(account_id: int):
    run_db("DELETE FROM accounts WHERE id=?", (account_id,))
# PART 2 - START / MENU / MESSAGE ROUTER
from telegram.constants import ParseMode

START_TEXT = (
    "🌟 *Welcome to HinariAdsBot* 🌟\n\n"
    "Created by @NOTCH2ND\n\n"
    "➡️ 1 week free trial. After that purchase premium at @NOTCH2ND (₹59/month)\n\n"
    "Login -> Save messages -> Forward automatically to your accounts."
)

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_user_banned(user_id):
        await safe_reply(update, "❌ You are banned.")
        return
    await safe_reply(update, START_TEXT, parse_mode=ParseMode.MARKDOWN)

async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    row = get_user_data(user_id)
    days_left = get_premium_days_left(user_id) if row else 0
    pstat = f"✅ {days_left} days" if days_left > 0 else "❌ Expired / Not activated"
    accounts = get_accounts_for_user(user_id) or []
    kb = [
        [InlineKeyboardButton("➕ Add Account", callback_data="menu_add_account")],
        [InlineKeyboardButton("📂 Manage Accounts", callback_data="menu_manage_accounts")],
        [InlineKeyboardButton("🎫 Redeem Code", callback_data="menu_redeem")],
        [InlineKeyboardButton("👑 Admin Panel", callback_data="menu_admin_panel")],
    ]
    text = f"🔰 *HinariAdsBot Menu* 🔰\n\n💎 Premium: {pstat}\n👤 Connected Accounts: {len(accounts)}\n\nUse the buttons below to manage your setup."
    await safe_reply(update, text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

# small helper to reply safely whether message or callback
async def safe_reply(update: Update, text: str, **kwargs):
    if update.callback_query:
        # respond on callback message context
        msg = update.callback_query.message
        if msg:
            return await msg.reply_text(text, **kwargs)
        else:
            # fallback to editing callback or answering
            await update.callback_query.answer()
            return
    elif update.message:
        return await update.message.reply_text(text, **kwargs)
    else:
        # unsupported update
        log.debug("safe_reply: unsupported update type")
        return

# Generic message router (handles text commands and prevents callback confusion)
async def message_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ignore callback_query here
    if update.callback_query:
        return

    text = (update.message.text or "").strip() if update.message else ""
    user_id = update.effective_user.id

    # Simple text shortcuts
    if text.lower() == "/menu" or text.lower() == "menu":
        return await menu_handler(update, context)

    # login flow and others rely on state machine
    state, temp = get_user_state(user_id)
    if state and state.startswith("waiting_"):
        # delegate to login flow handler (implemented in part 3)
        return await login_flow_handler(update, context, text)

    # if not recognized
    return await update.message.reply_text("Unknown option. Use /menu")
# PART 3 - TELETHON LOGIN FLOW & ACCOUNT MANAGEMENT

async def login_flow_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    state, temp = get_user_state(user_id)

    # waiting for api id
    if state == "waiting_api_id":
        try:
            api_id = int(text)
            set_user_state(user_id, "waiting_api_hash", {"api_id": api_id})
            await update.message.reply_text("Now send your API HASH:")
        except:
            await update.message.reply_text("Invalid API ID. Send a numeric API ID.")
        return True

    # waiting for api hash
    if state == "waiting_api_hash":
        temp["api_hash"] = text.strip()
        set_user_state(user_id, "waiting_phone", temp)
        await update.message.reply_text("Now send your phone number in international format (e.g. +91XXXXXXXXX):")
        return True

    # waiting phone -> send code
    if state == "waiting_phone":
        phone = text.strip()
        api_id = temp["api_id"]
        api_hash = temp["api_hash"]
        session_file = f"sessions/user_{user_id}_{secrets.token_hex(6)}"
        client = TelegramClient(session_file, api_id, api_hash)
        await client.connect()
        try:
            await client.send_code_request(phone)
            temp.update({"phone": phone, "session_file": session_file})
            _active_login_clients[user_id] = client
            set_user_state(user_id, "waiting_code", temp)
            await update.message.reply_text("📩 OTP sent! Enter the code you received:")
        except Exception as e:
            await update.message.reply_text(f"Error sending code: {e}")
            await client.disconnect()
            clear_user_state(user_id)
        return True

    if state == "waiting_code":
        code = text.strip()
        client = _active_login_clients.get(user_id)
        if not client:
            clear_user_state(user_id)
            await update.message.reply_text("Session expired. Please start again.")
            return True
        try:
            await client.sign_in(temp["phone"], code)
        except SessionPasswordNeededError:
            set_user_state(user_id, "waiting_2fa", temp)
            await update.message.reply_text("Two-factor enabled. Please send your 2FA password:")
            return True
        except Exception as e:
            await update.message.reply_text(f"Sign-in error: {e}")
            await client.disconnect()
            _active_login_clients.pop(user_id, None)
            clear_user_state(user_id)
            return True

        # success: save account record (multi-account support)
        add_account_record(user_id, temp["session_file"], temp["phone"], temp["api_id"], temp["api_hash"])
        save_user_data(user_id, temp["phone"], temp["api_id"], temp["api_hash"], temp["session_file"])

        await client.disconnect()
        _active_login_clients.pop(user_id, None)
        clear_user_state(user_id)
        await update.message.reply_text("✅ Account logged in and saved.")
        return True

    if state == "waiting_2fa":
        password = text
        client = _active_login_clients.get(user_id)
        if not client:
            clear_user_state(user_id)
            await update.message.reply_text("Session expired. Start again.")
            return True
        try:
            await client.sign_in(password=password)
            add_account_record(user_id, temp["session_file"], temp["phone"], temp["api_id"], temp["api_hash"])
            save_user_data(user_id, temp["phone"], temp["api_id"], temp["api_hash"], temp["session_file"])
            await client.disconnect()
            _active_login_clients.pop(user_id, None)
            clear_user_state(user_id)
            await update.message.reply_text("✅ Logged in with 2FA and saved.")
        except Exception as e:
            await update.message.reply_text(f"2FA error: {e}")
        return True

    return False  # not handled here

# Add account entrypoint
async def add_account_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    # multi-account limits: free users only 1 account; premium unlimited
    accounts = get_accounts_for_user(user_id) or []
    if not is_premium_active(user_id) and len(accounts) >= 1:
        await safe_reply(update, "Free trial users can only add 1 account. Buy premium from @NOTCH2ND to add more.")
        return
    set_user_state(user_id, "waiting_api_id")
    await safe_reply(update, "Send API ID:")

# Manage accounts UI
async def manage_accounts_ui(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    accounts = get_accounts_for_user(user_id) or []
    if not accounts:
        await safe_reply(update, "You have no connected accounts. Use 'Add Account' to add one.")
        return
    kb = []
    for a in accounts:
        aid, session_file, phone, api_id = a
        kb.append([InlineKeyboardButton(f"{phone} (id:{aid})", callback_data=f"account_show:{aid}")])
        kb.append([InlineKeyboardButton("❌ Delete", callback_data=f"account_delete:{aid}")])
    await safe_reply(update, "Your accounts:", reply_markup=InlineKeyboardMarkup(kb))

# Delete account
async def delete_account_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, account_id: int):
    # stop any forwarder processes for that session if exist (user-defined)
    delete_account_record(account_id)
    await safe_reply(update, f"Deleted account id {account_id} and stopped forwarding.")
    # PART 4 - FORWARDING (simplified) + ADMIN PANEL + CALLBACK HANDLING

# Placeholder: actual forwarding logic must use Telethon clients per saved session file.
# Example function to resume forwarders on start (simplified)
async def resume_active_forwarders_on_start(app_obj: Application):
    # iterate all accounts and (re)start forwarders
    rows = run_db("SELECT id, owner_id, session_file FROM accounts", (), fetch="all") or []
    for r in rows:
        acc_id, owner_id, session_file = r[0], r[1], r[2]
        # start a background task for each account forwarding (user to implement details)
        asyncio.create_task(dummy_forwarder_loop(acc_id, session_file))

async def dummy_forwarder_loop(acc_id: int, session_file: str):
    # Placeholder loop; replace with Telethon logic to forward saved messages to destinations
    while True:
        log.debug("Dummy forwarder tick for acc %s", acc_id)
        await asyncio.sleep(60)  # check every 60s

# ADMIN PANEL UI
async def admin_panel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await safe_reply(update, "You are not authorized.")
    kb = [
        [InlineKeyboardButton("📊 Stats", callback_data="admin_stats")],
        [InlineKeyboardButton("🪪 Generate Code", callback_data="admin_gen_code")],
        [InlineKeyboardButton("⛔ Ban User", callback_data="admin_ban")],
        [InlineKeyboardButton("♻️ Unban User", callback_data="admin_unban")],
        [InlineKeyboardButton("⭐ Extend Premium", callback_data="admin_extend")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")],
    ]
    await safe_reply(update, "👑 Admin Panel\nChoose an action:", reply_markup=InlineKeyboardMarkup(kb))

# Admin actions
async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total_users = run_db("SELECT COUNT(*) FROM users", (), fetch="one")[0]
    total_accounts = run_db("SELECT COUNT(*) FROM accounts", (), fetch="one")[0]
    text = f"📊 Users: {total_users}\n🔗 Accounts: {total_accounts}"
    return await safe_reply(update, text)

# Central callback query handler
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    data = query.data or ""
    await query.answer()

    # MENU callbacks
    if data == "menu_add_account":
        return await add_account_start(update, context)
    if data == "menu_manage_accounts":
        return await manage_accounts_ui(update, context)
    if data == "menu_redeem":
        set_user_state(update.effective_user.id, "waiting_redeem_code")
        return await safe_reply(update, "Send redeem code now:")

    if data == "menu_admin_panel":
        return await admin_panel_handler(update, context)

    # ACCOUNT callbacks (account_show:id and account_delete:id)
    if data.startswith("account_show:"):
        aid = int(data.split(":", 1)[1])
        row = run_db("SELECT id, phone, session_file FROM accounts WHERE id=?", (aid,), fetch="one")
        if row:
            return await safe_reply(update, f"Account {row[0]} - {row[1]}")
        else:
            return await safe_reply(update, "Account not found.")

    if data.startswith("account_delete:"):
        aid = int(data.split(":", 1)[1])
        delete_account_record(aid)
        return await safe_reply(update, f"Deleted account {aid}.")

    # ADMIN CALLBACKS (explicit)
    if data == "admin_stats":
        return await admin_stats(update, context)

    if data == "admin_gen_code":
        return await safe_reply(update, "Usage:\n/genkey <quantity> <days>\nExample: /genkey 1 30")

    if data == "admin_ban":
        return await safe_reply(update, "Usage:\n/ban <user_id>")

    if data == "admin_unban":
        return await safe_reply(update, "Usage:\n/unban <user_id>")

    if data == "admin_extend":
        return await safe_reply(update, "Usage:\n/extend <user_id> <days>")

    if data == "admin_broadcast":
        return await safe_reply(update, "Usage:\n/broadcast <your message>")

    # fallback
    return await safe_reply(update, "Unknown option. Use /menu")
    # PART 5 - ADMIN COMMANDS, REDEEM, DAILY TASKS, KEEP-ALIVE & MAIN

# Redeem code handler text
async def redeem_code_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state, temp = get_user_state(user_id)
    if state != "waiting_redeem_code":
        return await safe_reply(update, "Use the redeem button first.")
    code = (update.message.text or "").strip()
    r = run_db("SELECT code, days, is_used FROM redeem_codes WHERE code=?", (code,), fetch="one")
    if not r:
        clear_user_state(user_id)
        return await safe_reply(update, "Invalid code.")
    if r[2] == 1:
        clear_user_state(user_id)
        return await safe_reply(update, "Code already used.")
    days = r[1]
    extend_premium(user_id, days)
    run_db("UPDATE redeem_codes SET used_by=?, used_date=?, is_used=1 WHERE code=?", (user_id, datetime.utcnow().isoformat(), code))
    clear_user_state(user_id)
    return await safe_reply(update, f"Redeemed! Premium extended by {days} days.")

# Admin text commands (generate code, ban/unban/extend/broadcast)
async def genkey_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return await safe_reply(update, "Unauthorized.")
    args = context.args
    if len(args) != 2:
        return await safe_reply(update, "Usage: /genkey <quantity> <days>")
    qty, days = int(args[0]), int(args[1])
    codes = []
    for _ in range(qty):
        code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(12))
        run_db("INSERT INTO redeem_codes (code, days, created_by, created_date) VALUES (?, ?, ?, ?)",
               (code, days, user_id, datetime.utcnow().isoformat()))
        codes.append(code)
    return await safe_reply(update, "Generated:\n" + "\n".join(codes))

async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return await safe_reply(update, "Unauthorized.")
    if not context.args: return await safe_reply(update, "Usage: /ban <user_id>")
    uid = int(context.args[0])
    run_db("UPDATE users SET is_banned=1 WHERE user_id=?", (uid,))
    return await safe_reply(update, f"Banned {uid}")

async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return await safe_reply(update, "Unauthorized.")
    if not context.args: return await safe_reply(update, "Usage: /unban <user_id>")
    uid = int(context.args[0])
    run_db("UPDATE users SET is_banned=0 WHERE user_id=?", (uid,))
    return await safe_reply(update, f"Unbanned {uid}")

async def extend_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return await safe_reply(update, "Unauthorized.")
    if len(context.args) != 2: return await safe_reply(update, "Usage: /extend <user_id> <days>")
    uid = int(context.args[0]); days = int(context.args[1])
    extend_premium(uid, days)
    return await safe_reply(update, f"Extended {uid} by {days} days")

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return await safe_reply(update, "Unauthorized.")
    msg = " ".join(context.args)
    if not msg: return await safe_reply(update, "Usage: /broadcast <message>")
    rows = run_db("SELECT user_id FROM users WHERE is_banned=0", (), fetch="all") or []
    sent = 0
    for r in rows:
        uid = r[0]
        try:
            await context.bot.send_message(uid, msg)
            sent += 1
        except Exception:
            continue
    return await safe_reply(update, f"Broadcast sent to {sent} users.")

# Delete account text command
async def delete_account_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args: return await safe_reply(update, "Usage: /delacc <account_id>")
    aid = int(context.args[0])
    # ensure ownership
    row = run_db("SELECT owner_id FROM accounts WHERE id=?", (aid,), fetch="one")
    if not row or row[0] != user_id:
        return await safe_reply(update, "Not your account or account not found.")
    delete_account_record(aid)
    return await safe_reply(update, f"Deleted account {aid}")

# Background tasks
async def keep_alive_task():
    while True:
        log.info("Keep-alive heartbeat")
        await asyncio.sleep(300)

async def daily_premium_status_task(app_obj: Application):
    while True:
        # run once per 24 hours
        rows = run_db("SELECT user_id, premium_expiry FROM users", (), fetch="all") or []
        for r in rows:
            uid, expiry = r[0], r[1]
            try:
                days_left = max(0, (datetime.fromisoformat(expiry) - datetime.utcnow()).days)
                await app_obj.bot.send_message(uid, f"🔔 Your premium days left: {days_left}")
            except Exception:
                continue
        await asyncio.sleep(24 * 3600)

# Main wiring
def main():
    global BOT_APP
    app = Application.builder().token(BOT_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("menu", menu_handler))
    app.add_handler(CommandHandler("genkey", genkey_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CommandHandler("extend", extend_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("delacc", delete_account_cmd))

    # Message router & login flow (text)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_router))

    # Callback / button handler
    app.add_handler(CallbackQueryHandler(button_handler))

    # Redeem code text (handled by state in message_router but also register separate handler)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, redeem_code_text_handler))

    # Post-init: start background tasks after startup
    async def on_start(app_obj: Application):
        global BOT_APP
        BOT_APP = app_obj.bot
        # start keep alive & daily task
        asyncio.create_task(keep_alive_task())
        asyncio.create_task(daily_premium_status_task(app_obj))
        # resume forwarders
        await resume_active_forwarders_on_start(app_obj)

    app.post_init = on_start

    print("Bot running…")
    app.run_polling()

if __name__ == "__main__":
    main()