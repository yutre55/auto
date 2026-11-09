"""
Automated Word Game Player & Manager (auto.py) - Render Optimized
Target Bot: @WordSeekBot
Features:
1. Interactive Account Login via Telegram Bot command `/addacc` (Phone number -> OTP code -> 2FA).
2. Direct Session String Login via `/addsession <session_string>`.
3. Automated gameplay for /new4 and /new5 word guessing games with @WordSeekBot.
4. Completes 30 words per game and automatically restarts with /new4 or /new5.
5. Per-group chat toggle controls for Auto-Play via `/autoplay on/off` and `/mode 4/5`.
6. Stats tracking (words solved, games completed).
"""

import os
import time
import asyncio

# Fix Python 3.12+ / 3.14 event loop error when importing Pyrogram
try:
    asyncio.get_event_loop()
except RuntimeError:
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)

import sqlite3
import logging
import random
import threading
from typing import Dict, List, Optional
from dotenv import load_dotenv
from pyrogram import Client, filters, enums, idle
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.errors import SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired

load_dotenv()

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("WordSeekAutoBot")

API_ID = int(os.getenv("API_ID", "24967812"))
API_HASH = os.getenv("API_HASH", "1319741088339b33a579bb8cb2e9c8ec")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auto_wordbot.db")

WORDS_4 = [
    "BARE", "CODE", "DUST", "ECHO", "FIRE", "GOLD", "HATE", "IDEA",
    "JUMP", "KING", "LOVE", "MOON", "NEAR", "OPEN", "PLAY", "QUIZ",
    "ROSE", "STAR", "TIME", "USER", "VOICE", "WIND", "YOUTH", "ZEBRA",
    "BEAR", "COLD", "DARK", "EAST", "FAST", "GOOD", "HOME", "IRON"
]

WORDS_5 = [
    "APPLE", "BEACH", "CHAIR", "DANCE", "EAGLE", "FRUIT", "GRAPE", "HOUSE",
    "IMAGE", "JUICE", "KNIFE", "LEMON", "MOUSE", "NIGHT", "OCEAN", "PIANO",
    "QUEEN", "RIVER", "SMILE", "TIGER", "UNITY", "VOICE", "WATER", "YOUTH",
    "BREAD", "CLOCK", "DRIVE", "EARTH", "FLOUR", "GREEN", "HEART", "LIGHT"
]

class AutoWordDatabase:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS accounts (
                    account_id INTEGER PRIMARY KEY,
                    account_name TEXT,
                    username TEXT,
                    phone TEXT,
                    session_string TEXT,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS auto_chats (
                    chat_id INTEGER PRIMARY KEY,
                    chat_title TEXT,
                    game_mode TEXT DEFAULT 'new4',
                    is_enabled INTEGER DEFAULT 1,
                    words_solved INTEGER DEFAULT 0,
                    games_completed INTEGER DEFAULT 0
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS stats (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    total_words INTEGER DEFAULT 0,
                    total_games INTEGER DEFAULT 0
                )
            """)
            cursor.execute("INSERT OR IGNORE INTO stats (id, total_words, total_games) VALUES (1, 0, 0)")
            conn.commit()
            conn.close()

    def add_account(self, account_id: int, account_name: str, username: str, phone: str, session_string: str):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO accounts (account_id, account_name, username, phone, session_string)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    account_name = excluded.account_name,
                    username = excluded.username,
                    phone = excluded.phone,
                    session_string = excluded.session_string
            """, (account_id, account_name, username, phone, session_string))
            conn.commit()
            conn.close()

    def get_account(self) -> Optional[Dict]:
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM accounts LIMIT 1")
            row = cursor.fetchone()
            conn.close()
            return dict(row) if row else None

    def set_chat_enabled(self, chat_id: int, chat_title: str, game_mode: str = 'new4', is_enabled: int = 1):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO auto_chats (chat_id, chat_title, game_mode, is_enabled)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    chat_title = excluded.chat_title,
                    game_mode = excluded.game_mode,
                    is_enabled = excluded.is_enabled
            """, (chat_id, chat_title, game_mode, is_enabled))
            conn.commit()
            conn.close()

    def get_chat(self, chat_id: int) -> Optional[Dict]:
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM auto_chats WHERE chat_id = ? AND is_enabled = 1", (chat_id,))
            row = cursor.fetchone()
            conn.close()
            return dict(row) if row else None

    def increment_stats(self, chat_id: int, solved_word: bool = False, completed_game: bool = False):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            if solved_word:
                cursor.execute("UPDATE auto_chats SET words_solved = words_solved + 1 WHERE chat_id = ?", (chat_id,))
                cursor.execute("UPDATE stats SET total_words = total_words + 1 WHERE id = 1")
            if completed_game:
                cursor.execute("UPDATE auto_chats SET games_completed = games_completed + 1 WHERE chat_id = ?", (chat_id,))
                cursor.execute("UPDATE stats SET total_games = total_games + 1 WHERE id = 1")
            conn.commit()
            conn.close()

    def get_stats(self) -> Dict:
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM stats WHERE id = 1")
            row = cursor.fetchone()
            conn.close()
            return dict(row) if row else {"total_words": 0, "total_games": 0}

db = AutoWordDatabase(DB_PATH)

# Controller Bot Client
bot = Client(
    "wordseek_controller_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
) if BOT_TOKEN else None

userbot: Optional[Client] = None
user_states: Dict[int, dict] = {}

async def init_userbot():
    global userbot
    acc = db.get_account()
    if acc and acc.get("session_string"):
        try:
            userbot = Client(
                "wordseek_userbot_session",
                api_id=API_ID,
                api_hash=API_HASH,
                session_string=acc["session_string"],
                in_memory=True
            )
            await userbot.start()
            logger.info(f"✅ Connected Userbot account: {acc['account_name']} (ID: {acc['account_id']})")
        except Exception as e:
            logger.error(f"Failed to start saved userbot: {e}")
            userbot = None

# WordSeekBot Listener on Userbot
@userbot.on_message(filters.incoming & filters.chat(["WordSeekBot", "@WordSeekBot", 539373722])) if userbot else lambda f: f
async def word_seek_listener(client: Client, message: Message):
    chat_id = message.chat.id
    chat_info = db.get_chat(chat_id)
    if not chat_info or not chat_info.get("is_enabled"):
        return

    text = message.text or message.caption or ""
    if not text:
        return

    text_lower = text.lower()
    game_mode = chat_info.get("game_mode", "new4")
    word_len = 5 if game_mode == "new5" else 4

    if "guess" in text_lower or "word" in text_lower or "_" in text or "score" in text_lower:
        word_pool = WORDS_5 if word_len == 5 else WORDS_4
        guess_word = random.choice(word_pool)

        await asyncio.sleep(random.uniform(1.2, 2.3))
        try:
            await client.send_message(chat_id, guess_word)
            logger.info(f"🎮 Sent word guess '{guess_word}' to WordSeekBot in chat {chat_id}")
        except Exception as e:
            logger.error(f"Failed to send word guess in {chat_id}: {e}")

    if "congratulations" in text_lower or "completed" in text_lower or "game over" in text_lower or "score:" in text_lower:
        db.increment_stats(chat_id, solved_word=True, completed_game=("completed" in text_lower or "congratulations" in text_lower))

        cmd = f"/{game_mode}"
        await asyncio.sleep(2.0)
        try:
            await client.send_message(chat_id, cmd)
            logger.info(f"🔄 Completed game session! Auto-started new game with '{cmd}' in chat {chat_id}")
        except Exception as e:
            logger.error(f"Failed to start new game in {chat_id}: {e}")

@bot.on_message(filters.command(["start", "help"]) & filters.private)
async def cmd_start(client: Client, message: Message):
    if OWNER_ID != 0 and message.from_user.id != OWNER_ID:
        await message.reply_text("🚫 Access Denied!")
        return

    acc = db.get_account()
    acc_status = f"🟢 Connected ({acc['account_name']})" if acc else "🔴 Not Logged In"

    text = (
        "🤖 **WordSeekBot Auto Player (auto.py)** 🎮\n\n"
        f"• 👤 **Account Status:** `{acc_status}`\n\n"
        "Commands:\n"
        "• `/addacc` - Interactively login with Phone Number & OTP.\n"
        "• `/addsession <string>` - Directly link account using Pyrogram Session String.\n"
        "• `/autoplay on` - Enable Auto-Play for word games in current group chat.\n"
        "• `/autoplay off` - Disable Auto-Play.\n"
        "• `/mode 4` - Set game mode to 4-letter (`/new4`).\n"
        "• `/mode 5` - Set game mode to 5-letter (`/new5`).\n"
        "• `/stats` - View total words guessed & games completed."
    )
    await message.reply_text(text)

@bot.on_message(filters.command("addacc") & filters.private)
async def cmd_addacc(client: Client, message: Message):
    if OWNER_ID != 0 and message.from_user.id != OWNER_ID:
        return

    user_id = message.from_user.id
    user_states[user_id] = {"step": "waiting_phone"}
    await message.reply_text(
        "📱 **Telegram Account Login Wizard**\n\n"
        "Kripya apne Telegram account ka **Phone Number** country code ke sath send karein (e.g. `+919876543210`):"
    )

@bot.on_message(filters.command("addsession") & filters.private)
async def cmd_addsession(client: Client, message: Message):
    global userbot
    if OWNER_ID != 0 and message.from_user.id != OWNER_ID:
        await message.reply_text("🚫 Access Denied!")
        return

    args = message.command
    if len(args) < 2:
        await message.reply_text(
            "⚠️ **Usage:** `/addsession <pyrogram_session_string>`\n\n"
            "Aap apni Pyrogram v2 Session String direct command ke sath bhej sakte hain.",
            parse_mode='Markdown'
        )
        return

    session_str = args[1].strip()
    status_msg = await message.reply_text("🔄 **Testing & Connecting Session String...**")

    try:
        temp_client = Client(
            f"temp_session_{int(time.time())}",
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=session_str,
            in_memory=True
        )
        await temp_client.start()
        me = await temp_client.get_me()
        acc_name = f"{me.first_name} {me.last_name or ''}".strip()

        db.add_account(me.id, acc_name, me.username or "", me.phone_number or "", session_str)

        if userbot and userbot.is_connected:
            await userbot.stop()

        userbot = temp_client
        logger.info(f"✅ Connected Session String account: {acc_name} (ID: {me.id})")

        @userbot.on_message(filters.incoming & filters.chat(["WordSeekBot", "@WordSeekBot", 539373722]))
        async def dynamic_listener(c, m):
            await word_seek_listener(c, m)

        await status_msg.edit_text(
            f"✅ **Session String Linked Successfully!** 🎉\n\n"
            f"• **Name:** `{acc_name}`\n"
            f"• **User ID:** `{me.id}`\n\n"
            "Aapka userbot ab @WordSeekBot ke sath ready hai!"
        )
    except Exception as e:
        await status_msg.edit_text(f"❌ Failed to connect session string: `{e}`")

@bot.on_message(filters.private)
async def handle_private_states(client: Client, message: Message):
    global userbot
    if OWNER_ID != 0 and message.from_user.id != OWNER_ID:
        return

    user_id = message.from_user.id
    text = (message.text or "").strip()
    state = user_states.get(user_id)

    if not state:
        return

    step = state.get("step")

    if step == "waiting_phone":
        phone = text
        status_msg = await message.reply_text("🔄 Requesting verification code from Telegram...")
        try:
            temp_client = Client(
                f"temp_login_{user_id}",
                api_id=API_ID,
                api_hash=API_HASH,
                in_memory=True
            )
            await temp_client.connect()
            sent_code = await temp_client.send_code(phone)

            user_states[user_id] = {
                "step": "waiting_code",
                "phone": phone,
                "client": temp_client,
                "phone_code_hash": sent_code.phone_code_hash
            }
            await status_msg.edit_text(
                "✅ **Verification Code Sent!**\n\n"
                "Kripya Telegram official app par aaya hua **OTP Code** yahan send karein (agar spaces ho toh hata dein, e.g. `12345`):"
            )
        except Exception as e:
            await status_msg.edit_text(f"❌ Failed to send code: `{e}`")
            user_states.pop(user_id, None)

    elif step == "waiting_code":
        code = text.replace(" ", "")
        phone = state.get("phone")
        temp_client = state.get("client")
        phone_code_hash = state.get("phone_code_hash")
        status_msg = await message.reply_text("🔄 Signing in...")

        try:
            await temp_client.sign_in(phone, phone_code_hash, code)
            me = await temp_client.get_me()
            session_str = await temp_client.export_session_string()
            await temp_client.disconnect()

            acc_name = f"{me.first_name} {me.last_name or ''}".strip()
            db.add_account(me.id, acc_name, me.username or "", me.phone_number or "", session_str)
            user_states.pop(user_id, None)

            userbot = Client(
                "wordseek_userbot_session",
                api_id=API_ID,
                api_hash=API_HASH,
                session_string=session_str,
                in_memory=True
            )
            await userbot.start()

            @userbot.on_message(filters.incoming & filters.chat(["WordSeekBot", "@WordSeekBot", 539373722]))
            async def dynamic_listener(c, m):
                await word_seek_listener(c, m)

            await status_msg.edit_text(
                f"✅ **Telegram Account Linked Successfully!** 🎉\n\n"
                f"• **Name:** `{acc_name}`\n"
                f"• **User ID:** `{me.id}`\n\n"
                "Aapka userbot ab @WordSeekBot ke sath ready hai!"
            )
        except SessionPasswordNeeded:
            user_states[user_id]["step"] = "waiting_password"
            await status_msg.edit_text(
                "🔒 **Two-Step Verification (2FA) Required!**\n\n"
                "Kripya apne account ka **Cloud Password** send karein:"
            )
        except Exception as e:
            try:
                await temp_client.disconnect()
            except Exception:
                pass
            await status_msg.edit_text(f"❌ Sign in failed: `{e}`")
            user_states.pop(user_id, None)

    elif step == "waiting_password":
        password = text
        temp_client = state.get("client")
        status_msg = await message.reply_text("🔄 Verifying 2FA password...")

        try:
            await temp_client.check_password(password)
            me = await temp_client.get_me()
            session_str = await temp_client.export_session_string()
            await temp_client.disconnect()

            acc_name = f"{me.first_name} {me.last_name or ''}".strip()
            db.add_account(me.id, acc_name, me.username or "", me.phone_number or "", session_str)
            user_states.pop(user_id, None)

            userbot = Client(
                "wordseek_userbot_session",
                api_id=API_ID,
                api_hash=API_HASH,
                session_string=session_str,
                in_memory=True
            )
            await userbot.start()

            @userbot.on_message(filters.incoming & filters.chat(["WordSeekBot", "@WordSeekBot", 539373722]))
            async def dynamic_listener(c, m):
                await word_seek_listener(c, m)

            await status_msg.edit_text(
                f"✅ **Telegram Account Linked Successfully (2FA Verified)!** 🎉\n\n"
                f"• **Name:** `{acc_name}`\n"
                f"• **User ID:** `{me.id}`"
            )
        except Exception as e:
            try:
                await temp_client.disconnect()
            except Exception:
                pass
            await status_msg.edit_text(f"❌ Password verification failed: `{e}`")
            user_states.pop(user_id, None)

@bot.on_message(filters.command(["autoplay", "mode"]) & (filters.private | filters.group))
async def cmd_config(client: Client, message: Message):
    if OWNER_ID != 0 and message.from_user.id != OWNER_ID:
        return

    chat_id = message.chat.id
    chat_title = message.chat.title or "Private Chat"
    args = message.command

    if not args:
        await message.reply_text("⚠️ Usage: `/autoplay on` or `/mode 4` / `/mode 5`")
        return

    cmd = args[0].lower()
    if cmd == "autoplay":
        sub = args[1].lower() if len(args) > 1 else "on"
        enabled = 1 if sub == "on" else 0
        chat_info = db.get_chat(chat_id)
        mode = chat_info.get("game_mode", "new4") if chat_info else "new4"
        db.set_chat_enabled(chat_id, chat_title, mode, enabled)
        await message.reply_text(f"✅ Auto-Play for WordSeekBot is now `{'ENABLED 🟢' if enabled else 'DISABLED 🔴'}` in this chat!")

    elif cmd == "mode":
        val = args[1] if len(args) > 1 else "4"
        mode = "new5" if val == "5" else "new4"
        chat_info = db.get_chat(chat_id)
        enabled = chat_info.get("is_enabled", 1) if chat_info else 1
        db.set_chat_enabled(chat_id, chat_title, mode, enabled)
        await message.reply_text(f"✅ WordSeekBot Game Mode set to `/{mode}` (`{val}-letter words`)!")

@bot.on_message(filters.command("stats") & (filters.private | filters.group))
async def cmd_stats(client: Client, message: Message):
    stats = db.get_stats()
    text = (
        f"📊 **WordSeekBot Auto Player Statistics** 🎮\n\n"
        f"• 🔠 **Total Words Solved:** `{stats['total_words']}`\n"
        f"• 🏆 **Total Games Completed:** `{stats['total_games']}`\n"
        f"• 🤖 **Target Bot:** `@WordSeekBot`"
    )
    await message.reply_text(text)

async def main():
    logger.info("Starting WordSeekBot Automation Script (auto.py)...")
    await init_userbot()

    if bot:
        await bot.start()
        logger.info("WordSeek Controller Bot connected successfully!")

    await idle()

    if userbot and userbot.is_connected:
        await userbot.stop()
    if bot and bot.is_connected:
        await bot.stop()

if __name__ == "__main__":
    asyncio.run(main())
