import asyncio
import random
import os
import json
from datetime import datetime, timedelta
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, ApiIdInvalidError
from telethon.tl.types import User
import sqlite3
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Bot configuration - You need to set this from @BotFather
BOT_TOKEN = "8399763411:AAGVzQJqCkwMWgnEUV1_7GRHQtCSz-j5-yI"

# Initialize database
def init_db():
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id INTEGER PRIMARY KEY, 
                  phone TEXT,
                  joined_date TEXT,
                  premium_expiry TEXT,
                  delay_setting INTEGER DEFAULT 300,
                  api_id TEXT,
                  api_hash TEXT,
                  session_file TEXT,
                  is_active INTEGER DEFAULT 1)''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_states
                 (user_id INTEGER PRIMARY KEY,
                  state TEXT,
                  temp_data TEXT)''')
    conn.commit()
    conn.close()

init_db()

# Bot client
bot = TelegramClient('bot_session', 1, "1").start(bot_token=BOT_TOKEN)  # Temporary credentials

# User state management
def set_user_state(user_id, state, temp_data=None):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    temp_json = json.dumps(temp_data) if temp_data else None
    c.execute('INSERT OR REPLACE INTO user_states (user_id, state, temp_data) VALUES (?, ?, ?)', 
              (user_id, state, temp_json))
    conn.commit()
    conn.close()

def get_user_state(user_id):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('SELECT state, temp_data FROM user_states WHERE user_id = ?', (user_id,))
    result = c.fetchone()
    conn.close()
    if result:
        temp_data = json.loads(result[1]) if result[1] else None
        return result[0], temp_data
    return None, None

def clear_user_state(user_id):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('DELETE FROM user_states WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()

def save_user_data(user_id, phone, api_id, api_hash, session_file):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    
    user_data = get_user_data(user_id)
    if user_data:
        c.execute('''UPDATE users SET phone=?, api_id=?, api_hash=?, session_file=?, is_active=1 
                     WHERE user_id=?''', 
                  (phone, api_id, api_hash, session_file, user_id))
    else:
        joined_date = datetime.now().isoformat()
        premium_expiry = (datetime.now() + timedelta(days=7)).isoformat()  # 1 week free trial
        c.execute('''INSERT INTO users 
                     (user_id, phone, joined_date, premium_expiry, api_id, api_hash, session_file) 
                     VALUES (?, ?, ?, ?, ?, ?, ?)''', 
                  (user_id, phone, joined_date, premium_expiry, api_id, api_hash, session_file))
    conn.commit()
    conn.close()

def get_user_data(user_id):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    result = c.fetchone()
    conn.close()
    return result

def update_delay_setting(user_id, delay):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('UPDATE users SET delay_setting = ? WHERE user_id = ?', (delay, user_id))
    conn.commit()
    conn.close()

# Premium check
def is_premium_active(user_data):
    if not user_data:
        return False
    
    premium_expiry = datetime.fromisoformat(user_data[3])
    return datetime.now() < premium_expiry

# Bio check
async def check_user_bio(client, user_id):
    try:
        user = await client.get_entity(user_id)
        if hasattr(user, 'about') and user.about:
            return "By @HinariAdsBot" in user.about
    except:
        pass
    return False

# Start command with attractive intro
@bot.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    user_id = event.sender_id
    
    # Beautiful intro message
    intro_text = """
🌟 *Welcome to HinariAdsBot* 🌟

*✨ Automated Message Forwarding Bot ✨*

🚀 *Features:*
• 📢 Send messages to all your groups automatically
• ⚡ Multiple account support
• 🕒 Customizable delay settings
• 👥 Group management made easy
• 💰 Affordable premium plans

🎯 *How it works:*
1. Login your Telegram account
2. Set your preferred delay
3. Put message in Saved Messages
4. Let the bot do the work!

📝 *Requirement:* Add `By @HinariAdsBot` in your Telegram bio

🆓 *Free Trial:* 1 Week
💎 *Premium:* ₹59/month

*Created by:* @NOTCH2ND

Use /menu to access all features!
    """
    
    await event.reply(intro_text, parse_mode='markdown')
    
    # Initialize user if not exists
    if not get_user_data(user_id):
        joined_date = datetime.now().isoformat()
        premium_expiry = (datetime.now() + timedelta(days=7)).isoformat()
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        c.execute('INSERT OR IGNORE INTO users (user_id, joined_date, premium_expiry) VALUES (?, ?, ?)',
                  (user_id, joined_date, premium_expiry))
        conn.commit()
        conn.close()

# Menu command
@bot.on(events.NewMessage(pattern='/menu'))
async def menu_handler(event):
    menu_text = """
🔰 *HinariAdsBot Menu* 🔰

Please choose an option:

🔐 *Login Account* - Login your Telegram account
📱 *Fetch Accounts* - View your logged-in accounts
⏰ *Set Delay* - Set delay between messages
👥 *Fetch Groups* - Get list of your groups
🔄 *Start Forwarding* - Begin automated forwarding
🛑 *Stop Forwarding* - Stop automation

*Quick Delay Settings:*
• 🐢 3 Minutes
• 🚶 5 Minutes  
• 🐢 7 Minutes

Use buttons below or type commands!
    """
    
    buttons = [
        [Button.inline("🔐 Login Account", b"login_account")],
        [Button.inline("📱 Fetch Accounts", b"fetch_accounts")],
        [Button.inline("⏰ Set Delay", b"set_delay")],
        [Button.inline("👥 Fetch Groups", b"fetch_groups")],
        [Button.inline("🔄 Start Forwarding", b"start_forward")],
        [Button.inline("🛑 Stop Forwarding", b"stop_forward")]
    ]
    
    await event.reply(menu_text, parse_mode='markdown', buttons=buttons)

# Handle button clicks
@bot.on(events.CallbackQuery)
async def button_handler(event):
    user_id = event.sender_id
    data = event.data.decode('utf-8')
    
    if data == "login_account":
        await event.edit("Please send your API ID (get from https://my.telegram.org):")
        set_user_state(user_id, "waiting_api_id")
    
    elif data == "fetch_accounts":
        await show_user_accounts(event, user_id)
    
    elif data == "set_delay":
        await show_delay_options(event)
    
    elif data == "fetch_groups":
        await fetch_user_groups(event, user_id)
    
    elif data == "start_forward":
        await start_forwarding(event, user_id)
    
    elif data == "stop_forward":
        await stop_forwarding(event, user_id)
    
    elif data.startswith("delay_"):
        delay_time = int(data.split("_")[1])
        update_delay_setting(user_id, delay_time)
        await event.edit(f"✅ Delay set to {delay_time//60} minutes!")

# API ID input
@bot.on(events.NewMessage)
async def handle_messages(event):
    if event.text.startswith('/'):
        return
    
    user_id = event.sender_id
    state, temp_data = get_user_state(user_id)
    
    if state == "waiting_api_id":
        try:
            api_id = int(event.text.strip())
            set_user_state(user_id, "waiting_api_hash", {"api_id": api_id})
            await event.reply("✅ API ID received! Now send your API HASH:")
        except ValueError:
            await event.reply("❌ Invalid API ID. Please enter a valid number:")
    
    elif state == "waiting_api_hash":
        api_hash = event.text.strip()
        temp_data['api_hash'] = api_hash
        set_user_state(user_id, "waiting_phone", temp_data)
        await event.reply("✅ API Hash received! Now send your phone number (with country code, e.g., +919876543210):")
    
    elif state == "waiting_phone":
        phone = event.text.strip()
        api_id = temp_data['api_id']
        api_hash = temp_data['api_hash']
        
        # Create session file name
        session_file = f"user_{user_id}_{int(time.time())}"
        
        try:
            await event.reply("🔄 Creating session... Please wait.")
            
            # Create client with user's credentials
            client = TelegramClient(session_file, api_id, api_hash)
            await client.connect()
            
            # Send code request
            await client.send_code_request(phone)
            set_user_state(user_id, "waiting_code", {
                "phone": phone,
                "api_id": api_id,
                "api_hash": api_hash,
                "session_file": session_file,
                "client": client
            })
            
            await event.reply("📲 Verification code sent! Please enter the code you received:")
            
        except ApiIdInvalidError:
            await event.reply("❌ Invalid API ID or Hash. Please start again with /menu")
            clear_user_state(user_id)
        except Exception as e:
            await event.reply(f"❌ Error: {str(e)}. Please start again with /menu")
            clear_user_state(user_id)
    
    elif state == "waiting_code":
        code = event.text.strip()
        temp_data = temp_data  # Contains client and other data
        
        try:
            # Sign in with code
            await temp_data['client'].sign_in(temp_data['phone'], code)
            
            # Save user data
            save_user_data(user_id, temp_data['phone'], temp_data['api_id'], 
                          temp_data['api_hash'], temp_data['session_file'])
            
            await temp_data['client'].disconnect()
            clear_user_state(user_id)
            
            await event.reply("✅ Account logged in successfully! You can now use /menu to access features.")
            
        except SessionPasswordNeededError:
            set_user_state(user_id, "waiting_password", temp_data)
            await event.reply("🔒 2FA enabled. Please enter your password:")
        
        except Exception as e:
            await event.reply(f"❌ Error during login: {str(e)}. Please start again with /menu")
            clear_user_state(user_id)
            await temp_data['client'].disconnect()
    
    elif state == "waiting_password":
        password = event.text.strip()
        temp_data = temp_data
        
        try:
            await temp_data['client'].sign_in(password=password)
            
            # Save user data
            save_user_data(user_id, temp_data['phone'], temp_data['api_id'], 
                          temp_data['api_hash'], temp_data['session_file'])
            
            await temp_data['client'].disconnect()
            clear_user_state(user_id)
            
            await event.reply("✅ Account logged in successfully with 2FA! You can now use /menu to access features.")
            
        except Exception as e:
            await event.reply(f"❌ Error during 2FA login: {str(e)}. Please start again with /menu")
            clear_user_state(user_id)
            await temp_data['client'].disconnect()

# Show delay options
async def show_delay_options(event):
    buttons = [
        [Button.inline("🐢 3 Minutes", b"delay_180")],
        [Button.inline("🚶 5 Minutes", b"delay_300")],
        [Button.inline("🐇 7 Minutes", b"delay_420")],
        [Button.inline("🔙 Back to Menu", b"menu_back")]
    ]
    await event.edit("⏰ Select delay between messages:", buttons=buttons)

# Show user accounts
async def show_user_accounts(event, user_id):
    user_data = get_user_data(user_id)
    if not user_data or not user_data[5]:  # Check if api_id exists
        await event.edit("❌ No accounts logged in. Use 'Login Account' first.")
        return
    
    premium_status = "✅ Active" if is_premium_active(user_data) else "❌ Expired"
    delay_minutes = user_data[4] // 60
    
    account_text = f"""
📱 *Your Account Details:*

• 📞 Phone: `{user_data[1]}`
• ⏰ Delay: {delay_minutes} minutes
• 💎 Premium: {premium_status}
• 📅 Joined: {datetime.fromisoformat(user_data[2]).strftime('%Y-%m-%d')}
• 🗓️ Premium expires: {datetime.fromisoformat(user_data[3]).strftime('%Y-%m-%d')}

💡 Use buttons below to manage settings.
    """
    
    buttons = [
        [Button.inline("⏰ Change Delay", b"set_delay")],
        [Button.inline("🔙 Back to Menu", b"menu_back")]
    ]
    
    await event.edit(account_text, parse_mode='markdown', buttons=buttons)

# Fetch user groups
async def fetch_user_groups(event, user_id):
    user_data = get_user_data(user_id)
    if not user_data or not user_data[5]:
        await event.edit("❌ Please login first using 'Login Account'")
        return
    
    if not is_premium_active(user_data):
        await event.edit("❌ Premium expired. Please contact @NOTCH2ND to renew.")
        return
    
    try:
        await event.edit("🔄 Fetching your groups...")
        
        # Create client with user's credentials
        client = TelegramClient(user_data[7], int(user_data[5]), user_data[6])
        await client.connect()
        
        groups = []
        async for dialog in client.iter_dialogs():
            if dialog.is_group:
                groups.append(dialog.name)
        
        await client.disconnect()
        
        if groups:
            groups_text = "👥 *Your Groups:*\n\n" + "\n".join([f"• {name}" for name in groups[:20]])  # Show first 20
            if len(groups) > 20:
                groups_text += f"\n\n... and {len(groups) - 20} more groups"
        else:
            groups_text = "❌ No groups found."
        
        await event.edit(groups_text, parse_mode='markdown')
        
    except Exception as e:
        await event.edit(f"❌ Error fetching groups: {str(e)}")

# Start forwarding
async def start_forwarding(event, user_id):
    user_data = get_user_data(user_id)
    if not user_data or not user_data[5]:
        await event.edit("❌ Please login first using 'Login Account'")
        return
    
    if not is_premium_active(user_data):
        await event.edit("❌ Premium expired. Please contact @NOTCH2ND to buy premium (₹59/month)")
        return
    
    await event.edit("🔄 Starting forwarding service...")
    # Here you would implement the actual forwarding logic
    # This would be similar to your original forward_loop function
    # but integrated with the user's session and settings
    
    await event.edit("✅ Forwarding started! Make sure to:\n1. Put your message in Saved Messages\n2. Keep 'By @HinariAdsBot' in your bio")

# Stop forwarding
async def stop_forwarding(event, user_id):
    await event.edit("🛑 Forwarding stopped!")

# Menu back button
@bot.on(events.CallbackQuery(pattern=b'menu_back'))
async def menu_back_handler(event):
    await menu_handler(event)

# Scheduler for checking premium status
scheduler = AsyncIOScheduler()

async def check_premium_status():
    # This would check and notify users about expiring premium
    pass

# Start the bot
async def main():
    await bot.run_until_disconnected()

if __name__ == '__main__':
    print("Bot started...")
    bot.start(bot_token=BOT_TOKEN)
    asyncio.run(main())