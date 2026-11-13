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
import http.server
import socketserver
import urllib.request
import json
import re
import sqlite3
import logging
import random
import threading
from typing import Dict, List, Optional
from dotenv import load_dotenv

# Fix Python 3.12+ / 3.14 event loop error when importing Pyrogram
try:
    asyncio.get_event_loop()
except RuntimeError:
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)

from pyrogram import Client, filters as p_filters, enums, idle
from pyrogram.types import Message as PMessage, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.errors import SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired
from pyrogram.raw.functions.messages import SaveDraft

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters as tg_filters, ContextTypes

from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

load_dotenv()

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("WordSeekAutoBot")

import unicodedata

LOOKISM_DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lookism_cards_db.json")

def load_lookism_cards_db() -> dict:
    if os.path.exists(LOOKISM_DB_FILE):
        try:
            with open(LOOKISM_DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def clean_card_name(raw_name: str) -> str:
    if not raw_name:
        return ""
    text = unicodedata.normalize('NFKD', raw_name)
    text = re.sub(r"\([^)]*\)", "", text)
    text = re.sub(r"\[[^\]]*\]", "", text)
    text = "".join(c for c in text if ord(c) < 128 or c.isalnum() or c.isspace() or c in ["'", "-", "&"])
    text = " ".join(text.split()).strip()

    parts = text.split()
    if not parts:
        return ""

    if "&" in parts:
        sub_parts = []
        for p in parts:
            if p == "&":
                break
            sub_parts.append(p)
        parts = sub_parts

    if len(parts) >= 2:
        first_name = parts[0]
        surname = parts[-1]
        # Rule: By default use surname (Lee, Kim), but if surname is longer than first name (Arashimaya > Takuma), use first name
        if len(surname) > len(first_name):
            return first_name
        else:
            return surname
    elif len(parts) == 1:
        return parts[0]

    return text.strip()

def migrate_and_clean_lookism_db():
    if not os.path.exists(LOOKISM_DB_FILE):
        return
    try:
        with open(LOOKISM_DB_FILE, "r", encoding="utf-8") as f:
            db_data = json.load(f)

        updated = False
        new_db = {}
        for k, v in db_data.items():
            original_name = v.get("name", k)
            cleaned = clean_card_name(original_name)
            v["name"] = original_name
            v["clean_name"] = cleaned
            new_db[k] = v
            if "clean_name" not in v or v["clean_name"] != cleaned:
                updated = True

        if updated:
            with open(LOOKISM_DB_FILE, "w", encoding="utf-8") as f:
                json.dump(new_db, f, indent=2, ensure_ascii=False)
            print("🧹 Normalized all card clean_names in lookism_cards_db.json!")
    except Exception as e:
        print(f"Error migrating lookism db: {e}")

migrate_and_clean_lookism_db()
lookism_cards_db = load_lookism_cards_db()
lookism_hash_index: Dict[str, str] = {}
for c_name, c_info in lookism_cards_db.items():
    f_id = c_info.get("file_unique_id")
    if f_id:
        lookism_hash_index[f_id] = clean_card_name(c_info.get("name", c_name))

logger.info(f"⚡ Loaded and indexed {len(lookism_hash_index)} card photo hashes into memory for ultra-fast sniping!")

async def save_telegram_draft(client: Client, chat_id: int, text: str) -> bool:
    try:
        peer = await client.resolve_peer(chat_id)
        await client.invoke(SaveDraft(peer=peer, message=text))
        return True
    except Exception as e:
        logger.error(f"❌ MTProto SaveDraft error for chat {chat_id}: {e}")
        return False

def run_health_server():
    port = int(os.getenv("PORT", "10000"))
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b"OK - WordSeek Auto Bot is running 24/7")
        def log_message(self, format, *args):
            pass  # Suppress request log spam

    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
        print(f"🌐 Health check HTTP server started on port {port}")

        # Start self-ping keep-alive background thread to prevent Render free tier sleeping
        def keep_alive_ping():
            time.sleep(15)
            target_urls = [f"http://127.0.0.1:{port}/"]
            ext_url = os.getenv("RENDER_EXTERNAL_URL")
            if ext_url:
                if not ext_url.endswith("/"):
                    ext_url += "/"
                target_urls.append(ext_url)

            while True:
                for u in target_urls:
                    try:
                        req = urllib.request.Request(u, headers={"User-Agent": "WordSeekAutoBotKeepAlive/1.0"})
                        with urllib.request.urlopen(req, timeout=10) as resp:
                            pass
                    except Exception:
                        pass
                time.sleep(240)  # Ping every 4 minutes

        threading.Thread(target=keep_alive_ping, daemon=True).start()
        server.serve_forever()
    except Exception as e:
        print(f"❌ Failed to start health server on port {port}: {e}")

# Start health server immediately at module load time so Render detects the open port instantly
threading.Thread(target=run_health_server, daemon=True).start()

def escape_html(text: str) -> str:
    if not text:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

API_ID = int(os.getenv("API_ID", "24967812"))
API_HASH = os.getenv("API_HASH", "1319741088339b33a579bb8cb2e9c8ec")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8641208496:AAHg-3wEKC4ogsymjHecCL6hy7sPvGbml0Q")
OWNER_ID = int(os.getenv("OWNER_ID", "5193216992"))

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auto_wordbot.db")

WORDS_4 = [
    "ABLE", "ACID", "AGED", "ALSO", "AREA", "ARMY", "AWAY", "BABY", "BACK", "BALL",
    "BAND", "BANK", "BASE", "BATH", "BEAR", "BEAT", "BEEN", "BEER", "BELL", "BELT",
    "BEST", "BIRD", "BLOW", "BLUE", "BOAT", "BODY", "BOMB", "BOND", "BONE", "BOOK",
    "BOOM", "BORN", "BOSS", "BOTH", "BOWL", "BULK", "BURN", "BUSH", "BUSY", "CALL",
    "CALM", "CAME", "CAMP", "CARD", "CARE", "CASE", "CASH", "CAST", "CELL", "CHAT",
    "CHIP", "CITY", "CLUB", "COAL", "COAT", "CODE", "COLD", "COME", "COOK", "COOL",
    "COPE", "COPY", "CORE", "COST", "CREW", "CROP", "DARK", "DATA", "DATE", "DAWN",
    "DAYS", "DEAD", "DEAL", "DEAN", "DEAR", "DEBT", "DEEP", "DENY", "DESK", "DIAL",
    "DIET", "DISC", "DISK", "DOES", "DONE", "DOOR", "DOSE", "DOWN", "DRAW", "DREW",
    "DROP", "DRUG", "DUAL", "DUCK", "DUST", "DUTY", "EACH", "EARN", "EASE", "EAST",
    "EASY", "EDGE", "ELSE", "EVEN", "EVER", "EVIL", "EXAM", "FACE", "FACT", "FAIL",
    "FAIR", "FALL", "FARM", "FAST", "FATE", "FEAR", "FEED", "FEEL", "FEET", "FELL",
    "FELT", "FILE", "FILL", "FILM", "FIND", "FINE", "FIRE", "FIRM", "FISH", "FIVE",
    "FLAT", "FLOW", "FOOD", "FOOT", "FORD", "FORM", "FORT", "FOUR", "FREE", "FROM",
    "FUEL", "FULL", "FUND", "GAIN", "GAME", "GATE", "GAVE", "GEAR", "GIFT", "GIRL",
    "GIVE", "GLAD", "GOAL", "GOAT", "GOLD", "GONE", "GOOD", "GRAY", "GREW", "GRID",
    "GROW", "GULF", "HAIR", "HALF", "HALL", "HAND", "HANG", "HARD", "HARM", "HATE",
    "HAVE", "HEAD", "HEAR", "HEAT", "HELD", "HELL", "HELP", "HERE", "HERO", "HIGH",
    "HILL", "HIRE", "HOLD", "HOLE", "HOLY", "HOME", "HOPE", "HOST", "HOUR", "HUGE",
    "HUNG", "HUNT", "HURT", "IDEA", "INCH", "INTO", "IRON", "ITEM", "JACK", "JANE",
    "JEAN", "JOIN", "JUMP", "JUST", "KEEP", "KENT", "KEPT", "KICK", "KILL", "KIND",
    "KING", "KNEE", "KNEW", "KNOW", "LACK", "LADY", "LAID", "LAKE", "LAND", "LANE",
    "LAST", "LATE", "LEAD", "LEFT", "LESS", "LIFE", "LIFT", "LIKE", "LINE", "LINK",
    "LION", "LIST", "LIVE", "LOAD", "LOAN", "LOCK", "LOGO", "LONG", "LOOK", "LORD",
    "LOSE", "LOSS", "LOST", "LOVE", "LUCK", "MADE", "MAIL", "MAIN", "MAKE", "MALE",
    "MANY", "MARK", "MASS", "MATT", "MEAL", "MEAN", "MEAT", "MEET", "MENU", "MERE",
    "MILD", "MILE", "MILK", "MIND", "MINE", "MODE", "MOON", "MORE", "MOST", "MOVE",
    "MUCH", "MUST", "NAME", "NAVY", "NEAR", "NECK", "NEED", "NEWS", "NEXT", "NICE",
    "NICK", "NIGHT", "NODE", "NONE", "NOSE", "NOTE", "OKAY", "ONCE", "ONLY", "OPEN",
    "PACK", "PAGE", "PAID", "PAIN", "PAIR", "PARK", "PART", "PASS", "PAST", "PATH",
    "PEAK", "PICK", "PINK", "PLAN", "PLAY", "PLOT", "PLUG", "PLUS", "POEM", "POET",
    "POLL", "POOL", "POOR", "PORT", "POST", "PULL", "PURE", "PUSH", "RACE", "RAIL",
    "RAIN", "RANK", "RARE", "RATE", "READ", "REAL", "REAR", "RELY", "RENT", "REST",
    "RICE", "RICH", "RIDE", "RING", "RISE", "RISK", "ROAD", "ROCK", "ROLE", "ROLL",
    "ROOF", "ROOM", "ROOT", "ROSE", "RULE", "RUSH", "SAFE", "SAID", "SAKE", "SALE",
    "SALT", "SAME", "SAND", "SAVE", "SEAT", "SEED", "SEEK", "SEEM", "SEEN", "SELF",
    "SELL", "SEND", "SENT", "SHIP", "SHOP", "SHOT", "SHOW", "SHUT", "SICK", "SIDE",
    "SIGN", "SITE", "SIZE", "SKIN", "SLIP", "SLOW", "SNOW", "SOFT", "SOIL", "SOLD",
    "SOLE", "SOME", "SONG", "SOON", "SORT", "SOUL", "SPOT", "STAR", "STAY", "STEP",
    "STOP", "SUCH", "SUIT", "SURE", "TAKE", "TALK", "TALL", "TANK", "TAPE", "TASK",
    "TEAM", "TECH", "TELL", "TEND", "TERM", "TEST", "TEXT", "THAN", "THAT", "THEM",
    "THEN", "THEY", "THIS", "THUS", "TIDE", "TIED", "TIME", "TINY", "TOLD", "TOLL",
    "TONE", "TONY", "TOOK", "TOOL", "TOUR", "TOWN", "TREE", "TRIP", "TRUE", "TUBE",
    "TURN", "TWIN", "TYPE", "UNIT", "UPON", "USED", "USER", "VARY", "VAST", "VERY",
    "VIEW", "VOTE", "WAGE", "WAIT", "WAKE", "WALK", "WALL", "WANT", "WARD", "WARM",
    "WASH", "WAVE", "WAYS", "WEAK", "WEAR", "WEEK", "WELL", "WENT", "WEST", "WHAT",
    "WHEN", "WHOM", "WIDE", "WIFE", "WILD", "WILL", "WIND", "WINE", "WING", "WISH",
    "WITH", "WOOD", "WORD", "WORK", "YARD", "YEAR", "YOUR", "ZERO", "ZONE"
]

WORDS_5 = [
    "APPLE", "BEACH", "CHAIR", "DANCE", "EAGLE", "FRUIT", "GRAPE", "HOUSE",
    "IMAGE", "JUICE", "KNIFE", "LEMON", "MOUSE", "NIGHT", "OCEAN", "PIANO",
    "QUEEN", "RIVER", "SMILE", "TIGER", "UNITY", "VOICE", "WATER", "YOUTH",
    "BREAD", "CLOCK", "DRIVE", "EARTH", "FLOUR", "GREEN", "HEART", "LIGHT",
    "WORLD", "SMART", "BRAIN", "POWER", "DREAM", "STORY", "MUSIC", "SOUND"
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
                CREATE TABLE IF NOT EXISTS drecruit_chats (
                    chat_id INTEGER PRIMARY KEY,
                    chat_title TEXT,
                    target_bot TEXT DEFAULT '@Lookism_rec_bot',
                    is_enabled INTEGER DEFAULT 1
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
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
            cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('target_bots', '@Lookism_rec_bot, @Gnejdjdjbot')")
            conn.commit()
            conn.close()

    def set_drecruit_enabled(self, chat_id: int, chat_title: str, target_bot: str = '@Lookism_rec_bot', is_enabled: int = 1):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            try:
                cursor.execute("ALTER TABLE drecruit_chats ADD COLUMN target_bot TEXT DEFAULT '@Lookism_rec_bot'")
            except Exception:
                pass
            cursor.execute("""
                INSERT INTO drecruit_chats (chat_id, chat_title, target_bot, is_enabled)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    chat_title = excluded.chat_title,
                    target_bot = excluded.target_bot,
                    is_enabled = excluded.is_enabled
            """, (chat_id, chat_title, target_bot, is_enabled))
            conn.commit()
            conn.close()

    def get_drecruit_chat(self, chat_id: int) -> Optional[Dict]:
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM drecruit_chats WHERE chat_id = ? AND is_enabled = 1", (chat_id,))
            row = cursor.fetchone()
            conn.close()
            return dict(row) if row else None

    def set_setting(self, key: str, value: str):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """, (key, value))
            conn.commit()
            conn.close()

    def get_setting(self, key: str) -> Optional[str]:
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = cursor.fetchone()
            conn.close()
            return row[0] if row else None

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

userbot: Optional[Client] = None
telegram_app: Optional[Application] = None
user_states: Dict[int, dict] = {}

def extract_character_name(message_or_text) -> str:
    text = ""
    message = None
    if hasattr(message_or_text, "text") or hasattr(message_or_text, "caption") or hasattr(message_or_text, "reply_markup"):
        message = message_or_text
        text = (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()
    elif isinstance(message_or_text, str):
        text = message_or_text

    # 1. Check Inline Keyboard buttons text or callback data instantly in milliseconds
    if message and hasattr(message, "reply_markup") and message.reply_markup and hasattr(message.reply_markup, "inline_keyboard"):
        for row in message.reply_markup.inline_keyboard:
            for btn in row:
                btn_text = getattr(btn, "text", "") or ""
                btn_data = getattr(btn, "callback_data", "") or ""
                for source_str in [btn_text, btn_data]:
                    if not source_str:
                        continue
                    m_btn = re.search(r"(?:recruit|claim|grab|catch|card|char)[:\s_]+([A-Za-z0-9\s'&-]+)", source_str, re.IGNORECASE)
                    if m_btn:
                        val = m_btn.group(1).strip()
                        if val and len(val) >= 2 and len(val) < 50:
                            return val
                    cleaned = re.sub(r"^(?:recruit|claim|grab|catch|card|char|select|buy|view)[:\s_\-]+", "", source_str, flags=re.IGNORECASE).strip()
                    cleaned = cleaned.replace("_", " ")
                    if cleaned and len(cleaned) >= 2 and len(cleaned) < 50 and any(c.isalpha() for c in cleaned):
                        if cleaned.lower() not in ["claim", "recruit", "grab", "catch", "card", "next", "back", "buy", "view"]:
                            return cleaned

    if not text:
        return ""

    text_clean = text.replace("*", "").replace("`", "").replace("_", "").replace("~", "")

    # Pattern A: "recruit <Character Name> has appeared" (Used by @Gnejdjdjbot & drop bots!)
    mA = re.search(r"recruit\s+([A-Za-z0-9\s'&-]+?)\s+has\s+appeared", text_clean, re.IGNORECASE)
    if mA:
        val = mA.group(1).strip()
        if val and len(val) < 50:
            return val

    # Pattern B: "Name: <Name>" or "Character: <Name>"
    mB = re.search(r"(?:name|character|char)[:\s]+([^\n\r]+?)(?:\s+code:|\s+tier:|\s+worth:|\n|\r|$)", text_clean, re.IGNORECASE)
    if mB:
        val = mB.group(1).strip()
        for stop in ["code:", "Code:", "tier:", "Tier:", "worth:", "Worth:"]:
            if stop in val:
                val = val.split(stop)[0].strip()
        if val and len(val) < 50:
            return val

    # Pattern C: Line scan
    for line in text_clean.split("\n"):
        line_s = line.strip()
        if line_s.lower().startswith("name:") or line_s.lower().startswith("character:"):
            parts = line_s.split(":", 1)
            if len(parts) > 1:
                val = parts[1].strip()
                for stop in ["Code:", "code:", "Tier:", "tier:", "Worth:", "worth:"]:
                    if stop in val:
                        val = val.split(stop)[0].strip()
                if val and len(val) < 50:
                    return val

    return ""

active_recruits_per_chat: Dict[int, float] = {}  # chat_id -> drop timestamp

def register_word_seek_handler(client: Client):
    @client.on_message(p_filters.me & p_filters.group)
    async def user_manual_message_listener(c: Client, message: PMessage):
        chat_id = message.chat.id
        chat_info = db.get_chat(chat_id)
        if not chat_info or not chat_info.get("is_enabled"):
            return

        text = (message.text or message.caption or "").strip()
        if not text:
            return

        text_upper = text.upper()
        game_mode = chat_info.get("game_mode", "new4")
        word_len = 5 if game_mode == "new5" else 4

        is_bot_command = text_upper in ["/NEW4", "/NEW5", "/END"]
        is_auto_word_guess = (len(text_upper) == word_len and text_upper.isalpha())

        # If it is NOT an automated script guess/command, it's a MANUAL USER MESSAGE!
        if not is_bot_command and not is_auto_word_guess:
            chat_title = message.chat.title or "Group Chat"
            db.set_chat_enabled(chat_id, chat_title, game_mode, 0)
            logger.info(f"🛑 Auto-Play automatically TURNED OFF in chat {chat_title} ({chat_id}) due to manual user message: '{text[:30]}'")

            try:
                if OWNER_ID != 0 and telegram_app:
                    await telegram_app.bot.send_message(
                        chat_id=OWNER_ID,
                        text=f"🛑 **Auto-Play Automatically Turned OFF!**\n\n"
                             f"• **Group:** `{chat_title}`\n"
                             f"• **Reason:** Manual message detected from your account (`\"{text[:30]}\"`).",
                        parse_mode='Markdown'
                    )
            except Exception:
                pass

    @client.on_message(p_filters.group)
    @client.on_edited_message(p_filters.group)
    async def universal_drecruit_tracker(c: Client, message: PMessage):
        try:
            if not message or not message.chat:
                return

            # STRICT RULE 1: ONLY Group Chats & Supergroups
            if message.chat.type not in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
                return

            chat_id = message.chat.id

            # STRICT RULE 2: ONLY apply if DRecruit is explicitly turned ON for this chat via /drecruit
            d_info = db.get_drecruit_chat(chat_id)
            if not d_info or not d_info.get("is_enabled"):
                return

            chat_title = message.chat.title or f"Group_{chat_id}"
            text = (message.text or message.caption or "").strip()
            now = time.time()

            sender = message.from_user or message.sender_chat
            if not sender or not getattr(sender, "is_bot", True):
                return

            sender_uname = (sender.username or "").lower() if sender else ""
            sender_name = (getattr(sender, "first_name", None) or getattr(sender, "title", None) or "").lower() if sender else ""

            target_list = ["lookism_rec_bot", "gnejdjdjbot"]
            if d_info and d_info.get("target_bot"):
                for b in d_info["target_bot"].split(","):
                    b_clean = b.strip().lower().lstrip('@')
                    if b_clean and b_clean not in target_list:
                        target_list.append(b_clean)

            is_target_sender = any(
                t in sender_uname or t in sender_name or "nalla" in sender_name
                for t in target_list
            )

            if not is_target_sender:
                return

            text_lower = text.lower()

            # -------------------------------------------------------------
            # CASE 1: CLAIMED / EXPIRED
            # -------------------------------------------------------------
            if any(kw in text_lower for kw in ["in your crew", "crew now", "walked off", "too late"]):
                active_recruits_per_chat.pop(chat_id, None)
                try:
                    await save_telegram_draft(c, chat_id, "")
                except Exception:
                    pass
                return

            # -------------------------------------------------------------
            # CASE 2: CARD DROP PHOTO ("recruit has appeared")
            # -------------------------------------------------------------
            is_card_drop_msg = bool(message.photo) and (
                "appeared" in text_lower
                or "recruit" in text_lower
                or "crew" in text_lower
            )

            if is_card_drop_msg:
                active_recruits_per_chat[chat_id] = now
                char_name = ""

                # 1. Lookup via Photo Hash in memory index (O(1) speed)
                if message.photo:
                    file_unique_id = message.photo.file_unique_id
                    char_name = lookism_hash_index.get(file_unique_id, "")

                # 2. Fallback to inline buttons or text extraction
                if not char_name:
                    char_name = extract_character_name(message)

                if char_name:
                    clean_name = clean_card_name(char_name)
                    draft_text = f"/recruit {clean_name}"
                    await save_telegram_draft(c, chat_id, draft_text)
                    logger.info(f"📝 [LOOKISM DRAFT SET FROM RAM INDEX!] Saved '{draft_text}' in group '{chat_title}'")
                else:
                    await save_telegram_draft(c, chat_id, "/recruit ")
                return

            # -------------------------------------------------------------
            # CASE 3: SUBSEQUENT NAME MESSAGE ("Name: ...") WHILE DROP IS ACTIVE
            # -------------------------------------------------------------
            last_drop_time = active_recruits_per_chat.get(chat_id, 0.0)
            is_drop_active = (now - last_drop_time < 60)  # Active within 60 seconds

            if is_drop_active and "name:" in text_lower:
                char_name = extract_character_name(message)
                if char_name:
                    clean_name = clean_card_name(char_name)
                    draft_text = f"/recruit {clean_name}"
                    await save_telegram_draft(c, chat_id, draft_text)
                    logger.info(f"📝 [LOOKISM DRAFT SET FROM NAME MSG!] Saved '{draft_text}' in group '{chat_title}'")
                return

        except Exception as e:
            logger.error(f"❌ Error in universal_drecruit_tracker: {e}")

    @client.on_message(p_filters.incoming)
    @client.on_edited_message(p_filters.incoming)
    async def word_seek_listener(c: Client, message: PMessage):
        text = message.text or message.caption or ""
        if not text:
            return

        text_lower = text.lower()

        # Extract sender details
        sender_id = message.from_user.id if message.from_user else (message.sender_chat.id if message.sender_chat else 0)
        sender_uname = (message.from_user.username or "").lower() if message.from_user else (message.sender_chat.username or "").lower() if message.sender_chat else ""
        sender_fname = (message.from_user.first_name or "").lower() if message.from_user else (message.sender_chat.title or "").lower() if message.sender_chat else ""

        # Match WordSeek bot by ID, username, display name, OR message content signatures
        is_wordseek = (
            sender_id == 539373722
            or "wordseek" in sender_uname
            or "wordseek" in sender_fname
            or "4-letter mode" in text_lower
            or "5-letter mode" in text_lower
            or "/30 guesses" in text_lower
            or "already a game" in text_lower
            or "game started!" in text_lower
        )

        if is_wordseek:
            await process_word_seek_response(c, message.chat.id, text)

active_game_loops: Dict[int, asyncio.Task] = {}
used_words_per_chat: Dict[int, Set[str]] = {}

async def process_word_seek_response(client: Client, chat_id: int, text: str):
    chat_info = db.get_chat(chat_id)
    if not chat_info or not chat_info.get("is_enabled"):
        return

    text_lower = text.lower()
    game_mode = chat_info.get("game_mode", "new4")
    word_len = 5 if game_mode == "new5" else 4
    word_pool = WORDS_5 if word_len == 5 else WORDS_4

    # 1. Handle "game already in progress" -> Send /end then start fresh game
    if "already a game" in text_lower or "use /end" in text_lower or "end the current game" in text_lower:
        logger.info(f"⚠️ Game already in progress in chat {chat_id}! Resetting with /end...")
        used_words_per_chat[chat_id] = set()
        try:
            await client.send_message(chat_id, "/end")
            await asyncio.sleep(1.5)
            await client.send_message(chat_id, f"/{game_mode}")
            logger.info(f"🔄 Reset and sent `/{game_mode}` to chat {chat_id}")
        except Exception as e:
            logger.error(f"Failed to reset game in {chat_id}: {e}")
        return

    # 2. Restart on completion / win / game over / game ended / the word was
    if any(kw in text_lower for kw in ["congratulations", "congrats!", "guessed it correctly", "completed", "game over", "won!", "game ended", "start a new game", "the word was", "added", "leaderboard"]):
        db.increment_stats(chat_id, solved_word=True, completed_game=True)
        logger.info(f"🏆 Game ended in {chat_id}! Resetting used words and restarting /{game_mode}...")
        used_words_per_chat[chat_id] = set()
        try:
            await asyncio.sleep(1.5)
            await client.send_message(chat_id, f"/{game_mode}")
            logger.info(f"🔄 Auto-restarted `/{game_mode}` in chat {chat_id}")
        except Exception as e:
            logger.error(f"Failed to start new game in {chat_id}: {e}")
        return

    # 3. Active game guess ONLY if WordSeekBot has explicitly started a game and is waiting for guesses
    if any(kw in text_lower for kw in ["game started!", "guesses", "/30", "tries", "_"]):
        if chat_id not in used_words_per_chat:
            used_words_per_chat[chat_id] = set()

        used_set = used_words_per_chat[chat_id]
        available_words = [w for w in word_pool if w.capitalize() not in used_set]
        if not available_words:
            used_set.clear()
            available_words = list(word_pool)

        guess_word = random.choice(available_words).capitalize()
        used_set.add(guess_word)

        delay = random.uniform(0.6, 1.2)
        await asyncio.sleep(delay)
        try:
            await client.send_message(chat_id, guess_word)
            logger.info(f"🎮 Sent fresh word guess '{guess_word}' to WordSeekBot in chat {chat_id} (used: {len(used_set)})")
        except Exception as e:
            logger.error(f"Failed to send word guess in {chat_id}: {e}")

async def run_game_loop_for_chat(client: Client, chat_id: int):
    logger.info(f"🔄 Started active background game loop for chat {chat_id}")
    last_action_time = 0.0

    while True:
        try:
            chat_info = db.get_chat(chat_id)
            if not chat_info or not chat_info.get("is_enabled"):
                logger.info(f"🛑 Stopping active game loop for chat {chat_id} (disabled)")
                break

            game_mode = chat_info.get("game_mode", "new4")
            word_len = 5 if game_mode == "new5" else 4
            word_pool = WORDS_5 if word_len == 5 else WORDS_4

            messages = []
            async for m in client.get_chat_history(chat_id, limit=2):
                messages.append(m)

            now = time.time()
            text_lower = ""
            if messages:
                last_m = messages[0]
                text = last_m.text or last_m.caption or ""
                text_lower = text.lower()

            # 1. Reset if game already in progress
            if "already a game" in text_lower or "use /end" in text_lower or "end the current game" in text_lower:
                if now - last_action_time > 3.0:
                    logger.info(f"⚠️ Resetting game in chat {chat_id}...")
                    used_words_per_chat[chat_id] = set()
                    await client.send_message(chat_id, "/end")
                    await asyncio.sleep(1.2)
                    await client.send_message(chat_id, f"/{game_mode}")
                    last_action_time = time.time()

            # 2. Restart on completion / win / game over / game ended / the word was
            elif any(kw in text_lower for kw in ["congratulations", "completed", "game over", "won!", "game ended", "start a new game", "the word was"]):
                if now - last_action_time > 3.0:
                    db.increment_stats(chat_id, solved_word=True, completed_game=True)
                    logger.info(f"🏆 Game ended in {chat_id}! Resetting used words and restarting /{game_mode}...")
                    used_words_per_chat[chat_id] = set()
                    await asyncio.sleep(1.2)
                    await client.send_message(chat_id, f"/{game_mode}")
                    last_action_time = time.time()

            # 3. Continuous guessing loop: send a fresh unique 4/5-letter word every 0-3s random timing non-stop!
            else:
                delay_between = random.uniform(1.0, 3.0)
                if now - last_action_time > delay_between:
                    if chat_id not in used_words_per_chat:
                        used_words_per_chat[chat_id] = set()

                    used_set = used_words_per_chat[chat_id]
                    available_words = [w for w in word_pool if w.capitalize() not in used_set]
                    if not available_words:
                        used_set.clear()
                        available_words = list(word_pool)

                    guess_word = random.choice(available_words).capitalize()
                    used_set.add(guess_word)

                    await client.send_message(chat_id, guess_word)
                    last_action_time = time.time()
                    logger.info(f"🎮 Continuous loop sent word guess '{guess_word}' to chat {chat_id} (delay: {delay_between:.1f}s, used: {len(used_set)})")

        except Exception as e:
            logger.error(f"Error in game loop for chat {chat_id}: {e}")

        await asyncio.sleep(random.uniform(0.5, 3.0))

async def watchdog_supervisor():
    logger.info("🛡️ Self-healing watchdog supervisor started (checking every 5s)...")
    while True:
        try:
            acc = db.get_account()
            if acc and acc.get("session_string"):
                global userbot
                if not userbot or not userbot.is_connected:
                    try:
                        logger.warning("⚠️ Userbot disconnected! Reconnecting automatically...")
                        userbot = Client(
                            "wordseek_userbot_session",
                            api_id=API_ID,
                            api_hash=API_HASH,
                            session_string=acc["session_string"],
                            in_memory=True
                        )
                        await userbot.start()
                        await setup_userbot_cache(userbot)
                        register_word_seek_handler(userbot)
                        start_all_enabled_game_loops(userbot)
                        logger.info("✅ Userbot successfully reconnected by watchdog!")
                    except Exception as e:
                        logger.error(f"Watchdog failed to reconnect userbot: {e}")

            if userbot and userbot.is_connected:
                start_all_enabled_game_loops(userbot)

        except Exception as e:
            logger.error(f"Error in watchdog supervisor: {e}")

        await asyncio.sleep(5.0)

def ensure_chat_game_loop(client: Client, chat_id: int):
    if chat_id not in active_game_loops or active_game_loops[chat_id].done():
        active_game_loops[chat_id] = asyncio.create_task(run_game_loop_for_chat(client, chat_id))

def start_all_enabled_game_loops(client: Client):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id FROM auto_chats WHERE is_enabled = 1")
    rows = cursor.fetchall()
    conn.close()
    for row in rows:
        ensure_chat_game_loop(client, row["chat_id"])

async def setup_userbot_cache(client: Client):
    try:
        async for dialog in client.get_dialogs():
            pass
        logger.info("✅ Userbot dialogs cache synchronized successfully.")
    except Exception as e:
        logger.warning(f"Could not sync userbot dialogs cache: {e}")

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
            await setup_userbot_cache(userbot)
            register_word_seek_handler(userbot)
            start_all_enabled_game_loops(userbot)
            logger.info(f"✅ Connected Userbot account: {acc['account_name']} (ID: {acc['account_id']})")
        except Exception as e:
            logger.error(f"Failed to start saved userbot: {e}")
            userbot = None
            register_word_seek_handler(userbot)
            logger.info(f"✅ Connected Userbot account: {acc['account_name']} (ID: {acc['account_id']})")
        except Exception as e:
            logger.error(f"Failed to start saved userbot: {e}")
            userbot = None

def clear_telegram_webhook(token: str):
    if not token:
        return
    try:
        url = f"https://api.telegram.org/bot{token}/deleteWebhook?drop_pending_updates=true"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as response:
            res = json.loads(response.read().decode())
            logger.info(f"🗑️ Cleared Telegram webhook: {res}")
    except Exception as e:
        logger.warning(f"Could not clear webhook: {e}")

# Python-Telegram-Bot Handlers
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if OWNER_ID != 0 and user.id != OWNER_ID:
        await update.message.reply_text("🚫 Access Denied!")
        return

    acc = db.get_account()
    acc_status = f"🟢 Connected ({acc['account_name']})" if acc else "🔴 Not Logged In"

    text = (
        "🤖 **WordSeekBot & DRecruit Auto-Sniper Dashboard** 🎮🎴\n\n"
        f"• 👤 **Userbot Account:** `{acc_status}`\n\n"
        "📋 **All Available Commands:**\n"
        "• `/start` or `/help` - Show this main dashboard and command list.\n"
        "• `/addacc` - Interactively login with Phone Number & OTP.\n"
        "• `/addsession <string>` - Directly link account using Pyrogram Session String.\n"
        "• `/drecruit` - Interactive wizard to enable/disable DRecruit (Auto-Draft Sniper) in GCs.\n"
        "• `/drecruit off` - Turn off DRecruit in active groups.\n"
        "• `/addt <bot>` - Set target recruit bot username (e.g. `@Lookism_rec_bot`).\n"
        "• `/autoplay` - Interactive wizard to enable/disable Auto-Play word game in GCs.\n"
        "• `/autoplay off` - Turn off Auto-Play in active groups.\n"
        "• `/mode 4` / `/mode 5` - Set WordSeek game mode (4 or 5-letter words).\n"
        "• `/chats` or `/groups` - View all group chats your userbot is joined in.\n"
        "• `/dbdwn` - Download current `lookism_cards_db.json` database file.\n"
        "• `/dbup` (reply to json file) - Upload and instantly update live database in memory.\n"
        "• `/scard` - Save/add a new card to DB.\n"
        "• `/dcard <name>` - Delete a card from DB by name.\n"
        "• `/cancel` - Cancel any active interactive wizard.\n"
        "• `/stats` - View total words guessed & games completed stats.\n"
        "• `/ping` - Check if bot is alive and responding 24/7."
    )
    await update.message.reply_text(text, parse_mode='Markdown')

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if OWNER_ID != 0 and user.id != OWNER_ID:
        return
    await update.message.reply_text("🏓 **Pong!** Bot is alive, active, and running 24/7! 🟢", parse_mode='Markdown')

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if OWNER_ID != 0 and user.id != OWNER_ID:
        return
    if user.id in user_states:
        user_states.pop(user.id, None)
        await update.message.reply_text("❌ **Interaction cancelled successfully.**", parse_mode='Markdown')
    else:
        await update.message.reply_text("⚠️ Koi active interaction nahi chal rahi hai.")

async def cmd_scard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if OWNER_ID != 0 and user.id != OWNER_ID:
        await update.message.reply_text("🚫 Access Denied!")
        return
    if update.message.chat.type != 'private':
        await update.message.reply_text("⚠️ Kripya `/scard` command bot ke **Private Chat** mein use karein.")
        return

    user_states[user.id] = {"step": "waiting_for_card_message"}
    await update.message.reply_text(
        "📸 **Card Scraper Wizard Active** 🎴\n\n"
        "Kripya ab us **card message ko forward karein** ya **photo + details upload karein**.\n\n"
        "👉 *Agar bich mein rokna ho toh `/cancel` send karein:*",
        parse_mode='Markdown'
    )

async def cmd_dbdwn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if OWNER_ID != 0 and user.id != OWNER_ID:
        await update.message.reply_text("🚫 Access Denied!")
        return
    if not os.path.exists(LOOKISM_DB_FILE):
        await update.message.reply_text("⚠️ Database file `lookism_cards_db.json` not found on disk!")
        return
    try:
        db_data = load_lookism_cards_db()
        total = len(db_data)
        names = [c_info.get("name", k) for k, c_info in db_data.items()]
        names_preview = ", ".join(names[:35]) if names else "None"
        if len(names) > 35:
            names_preview += f" ... and {len(names) - 35} more"

        caption = (
            f"📦 **Lookism Cards Database (`lookism_cards_db.json`)**\n\n"
            f"• **Total Cards Stored:** `{total}`\n"
            f"• **Card Names:** `{names_preview}`"
        )
        with open(LOOKISM_DB_FILE, "rb") as f:
            await update.message.reply_document(
                document=f,
                caption=caption,
                parse_mode='Markdown'
            )
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to send database file: `{e}`")

async def cmd_dbup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global lookism_cards_db, lookism_hash_index
    user = update.effective_user
    if OWNER_ID != 0 and user.id != OWNER_ID:
        await update.message.reply_text("🚫 Access Denied!")
        return

    reply = update.message.reply_to_message
    if not reply or not reply.document:
        await update.message.reply_text(
            "⚠️ **Usage:** Kripya naye `lookism_cards_db.json` file ko upload karke us message par reply mein `/dbup` command send karein.",
            parse_mode='Markdown'
        )
        return

    status_msg = await update.message.reply_text("🔄 Downloading and updating database file...")
    try:
        doc = reply.document
        file = await context.bot.get_file(doc.file_id)
        await file.download_to_drive(LOOKISM_DB_FILE)

        # Reload DB and re-index in memory instantly
        lookism_cards_db = load_lookism_cards_db()
        lookism_hash_index.clear()
        for c_name, c_info in lookism_cards_db.items():
            f_id = c_info.get("file_unique_id")
            if f_id:
                lookism_hash_index[f_id] = clean_card_name(c_info.get("name", c_name))

        await status_msg.edit_text(
            f"✅ **Database Updated Successfully!** 🎉\n\n"
            f"• **Total Cards Loaded:** `{len(lookism_cards_db)}`\n"
            f"• **Indexed Photo Hashes:** `{len(lookism_hash_index)}`\n\n"
            "⚡ *Bot ne naya database live memory mein load kar liya hai!*",
            parse_mode='Markdown'
        )
    except Exception as e:
        await status_msg.edit_text(f"❌ Failed to update database: `{e}`")

async def cmd_scard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global lookism_cards_db, lookism_hash_index
    user = update.effective_user
    if OWNER_ID != 0 and user.id != OWNER_ID:
        await update.message.reply_text("🚫 Access Denied!")
        return

    reply = update.message.reply_to_message

    # Extract text from replied message first, else current message
    text = ""
    if reply:
        text = (reply.caption or reply.text or "").strip()
    if not text:
        text = (update.message.caption or update.message.text or "").strip()
        text = text.replace("/scard", "").strip()

    # Extract photo from replied message first, else current message
    photo = None
    if reply and reply.photo:
        photo = reply.photo
    elif update.message.photo:
        photo = update.message.photo

    p_obj = None
    if photo:
        p_obj = photo[-1] if isinstance(photo, list) else photo

    if not text and not p_obj:
        await update.message.reply_text(
            "⚠️ **Card details nahi mili!**\n\n"
            "Kripya card message par reply karke `/scard` bhein, ya card photo ke sath caption mein details bhein.",
            parse_mode='Markdown'
        )
        return

    char_name = ""
    tier_info = "Mortal"
    worth_info = "200"

    m_name = re.search(r"Name:\s*([^\n\r]+)", text, re.IGNORECASE)
    if m_name:
        char_name = m_name.group(1).strip()

    m_tier = re.search(r"Tier:\s*([^\n\r]+)", text, re.IGNORECASE)
    if m_tier:
        tier_info = m_tier.group(1).strip()

    m_worth = re.search(r"Worth:\s*([^\n\r]+)", text, re.IGNORECASE)
    if m_worth:
        worth_info = m_worth.group(1).strip()

    if not char_name:
        await update.message.reply_text(
            f"❌ **Card name extract nahi ho paya!**\n"
            f"Text mein `Name: <Name>` hona zaroori hai.\n\n"
            f"📄 **Received Text:**\n`{text}`",
            parse_mode='Markdown'
        )
        return

    file_unique_id = getattr(p_obj, "file_unique_id", "") if p_obj else ""
    file_id = getattr(p_obj, "file_id", "") if p_obj else ""

    clean_name = clean_card_name(char_name)

    # Load fresh DB from disk so we NEVER overwrite or delete old cards!
    lookism_cards_db = load_lookism_cards_db()

    lookism_cards_db[char_name] = {
        "name": char_name,
        "clean_name": clean_name,
        "description": f"{tier_info} - {worth_info}",
        "file_unique_id": file_unique_id,
        "file_id": file_id
    }

    try:
        with open(LOOKISM_DB_FILE, "w", encoding="utf-8") as f:
            json.dump(lookism_cards_db, f, indent=2, ensure_ascii=False)
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to save database file: `{e}`")
        return

    # Re-index RAM memory instantly
    lookism_hash_index.clear()
    for c_name, c_info in lookism_cards_db.items():
        f_id = c_info.get("file_unique_id")
        if f_id:
            lookism_hash_index[f_id] = clean_card_name(c_info.get("name", c_name))

    await update.message.reply_text(
        f"✅ **Card Added Successfully to Database!** 🎉\n\n"
        f"• **Original Name:** `{char_name}`\n"
        f"• **Draft Name (Clean):** `{clean_name}` 🎯\n"
        f"• **Tier / Worth:** `{tier_info} | {worth_info}`\n"
        f"• **Photo Unique ID:** `{file_unique_id or 'None'}`\n\n"
        f"📊 *Total Cards in DB:* `{len(lookism_cards_db)}`",
        parse_mode='Markdown'
    )

async def cmd_dcard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global lookism_cards_db, lookism_hash_index
    user = update.effective_user
    if OWNER_ID != 0 and user.id != OWNER_ID:
        await update.message.reply_text("🚫 Access Denied!")
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "⚠️ **Usage:** `/dcard <card_name>`\n\n"
            "Aap database se kisi bhi card ko uske naam se delete kar sakte hain (e.g., `/dcard Yeonwoo` ya `/dcard Takuma`).",
            parse_mode='Markdown'
        )
        return

    query = " ".join(args).strip().lower()
    lookism_cards_db = load_lookism_cards_db()

    found_key = None
    for k, v in lookism_cards_db.items():
        orig_name = k.lower()
        clean_n = v.get("clean_name", "").lower()
        db_name = v.get("name", "").lower()
        if query == orig_name or query == clean_n or query == db_name or query in orig_name or query in clean_n:
            found_key = k
            break

    if not found_key:
        await update.message.reply_text(f"❌ Card matching `{query}` database mein nahi mila!")
        return

    deleted_card = lookism_cards_db.pop(found_key)

    try:
        with open(LOOKISM_DB_FILE, "w", encoding="utf-8") as f:
            json.dump(lookism_cards_db, f, indent=2, ensure_ascii=False)
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to save database file: `{e}`")
        return

    lookism_hash_index.clear()
    for c_name, c_info in lookism_cards_db.items():
        f_id = c_info.get("file_unique_id")
        if f_id:
            lookism_hash_index[f_id] = clean_card_name(c_info.get("name", c_info.get("clean_name", c_name)))

    await update.message.reply_text(
        f"🗑️ **Card Deleted Successfully!**\n\n"
        f"• **Deleted Card:** `{found_key}` (Clean: `{deleted_card.get('clean_name', '')}`)\n"
        f"• **Remaining Cards in DB:** `{len(lookism_cards_db)}`",
        parse_mode='Markdown'
    )

async def cmd_addt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if OWNER_ID != 0 and user.id != OWNER_ID:
        await update.message.reply_text("🚫 Access Denied!")
        return
    args = context.args
    if not args:
        target = db.get_setting("target_bot") or "Not Set ⚠️"
        await update.message.reply_text(
            f"ℹ️ **Current Target Recruit Bot:** `{target}`\n\n"
            f"⚠️ **Usage:** `/addt <bot_username_or_id>` (e.g. `/addt @Lookism_rec_bot`)",
            parse_mode='Markdown'
        )
        return

    bot_target = args[0].strip()
    db.set_setting("target_bot", bot_target)
    await update.message.reply_text(f"✅ Target Recruit Bot successfully set to: `{bot_target}`", parse_mode='Markdown')

async def cmd_drecruit_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if OWNER_ID != 0 and user.id != OWNER_ID:
        return
    if update.message.chat.type != 'private':
        await update.message.reply_text("⚠️ Kripya `/drecruit off` command bot ke **Private Chat** mein use karein.")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM drecruit_chats WHERE is_enabled = 1")
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("⚠️ Wahan koi drecruit nahi chal raha ha!")
        return

    enabled_chats = [dict(r) for r in rows]
    user_states[user.id] = {"step": "disabling_drecruit_chat", "enabled_chats": enabled_chats}

    lines = [f"📋 **Select Group Chat to Disable DRecruit:**\n"]
    for idx, c in enumerate(enabled_chats, 1):
        lines.append(f"{idx}. **{escape_html(c['chat_title'])}** (Bot: `{c.get('target_bot', '@Lookism_rec_bot')}`)")
    lines.append("\n👉 *Kripya disable karne ke liye us Group ka **Number** send karein:*")

    await update.message.reply_text("\n".join(lines), parse_mode='Markdown')

async def cmd_drecruit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if OWNER_ID != 0 and user.id != OWNER_ID:
        return
    if update.message.chat.type != 'private':
        await update.message.reply_text("⚠️ Kripya `/drecruit` command bot ke **Private Chat** mein use karein taaki aap interactive wizard se group select kar sakein!")
        return

    args = context.args
    if args and args[0].lower() == "off":
        await cmd_drecruit_off(update, context)
        return

    if not userbot or not userbot.is_connected:
        await update.message.reply_text("🔴 Userbot connected nahi hai! Pehle account link karein using `/addacc` ya `/addsession`.")
        return

    status_msg = await update.message.reply_text("🔄 Fetching your joined group chats for DRecruit...")
    groups = []
    try:
        async for dialog in userbot.get_dialogs():
            if dialog.chat.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
                d_info = db.get_drecruit_chat(dialog.chat.id)
                is_on = bool(d_info and d_info.get("is_enabled"))
                bot_target = d_info.get("target_bot", "@Lookism_rec_bot") if d_info else "@Lookism_rec_bot"
                groups.append({
                    "id": dialog.chat.id,
                    "title": dialog.chat.title,
                    "is_on": is_on,
                    "target_bot": bot_target
                })
    except Exception as e:
        await status_msg.edit_text(f"❌ Failed to fetch chats: `{e}`")
        return

    if not groups:
        await status_msg.edit_text("⚠️ Userbot kisi bhi Group Chat mein joined nahi hai!")
        return

    user_states[user.id] = {"step": "selecting_drecruit_chat", "groups": groups}

    lines = [f"📋 **DRecruit Group Chats Dashboard:**\n"]
    for idx, g in enumerate(groups, 1):
        badge = f"🟢 ON (`{g['target_bot']}`)" if g["is_on"] else "🔴 OFF"
        lines.append(f"{idx}. **{escape_html(g['title'])}** ({badge})")
    lines.append("\n👉 *Kripya kisi bhi Group ka **Number** send karein (e.g. `1`):*")
    lines.append("• *Number bhejne par us group ke liye Target Bot select karne ka option aayega.*")

    await status_msg.edit_text("\n".join(lines), parse_mode='Markdown')

async def cmd_addacc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if OWNER_ID != 0 and user.id != OWNER_ID:
        return
    user_states[user.id] = {"step": "waiting_phone"}
    await update.message.reply_text(
        "📱 **Telegram Account Login Wizard**\n\n"
        "Kripya apne Telegram account ka **Phone Number** country code ke sath send karein (e.g. `+919876543210`):",
        parse_mode='Markdown'
    )

async def cmd_addsession(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global userbot
    user = update.effective_user
    if OWNER_ID != 0 and user.id != OWNER_ID:
        await update.message.reply_text("🚫 Access Denied!")
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "⚠️ **Usage:** `/addsession <pyrogram_session_string>`\n\n"
            "Aap apni Pyrogram v2 Session String direct command ke sath bhej sakte hain.",
            parse_mode='Markdown'
        )
        return

    session_str = args[0].strip()
    status_msg = await update.message.reply_text("🔄 **Testing & Connecting Session String...**")

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
        await setup_userbot_cache(userbot)
        register_word_seek_handler(userbot)
        logger.info(f"✅ Connected Session String account: {acc_name} (ID: {me.id})")

        await status_msg.edit_text(
            f"✅ **Session String Linked Successfully!** 🎉\n\n"
            f"• **Name:** `{acc_name}`\n"
            f"• **User ID:** `{me.id}`\n\n"
            "Aapka userbot ab @WordSeekBot ke sath ready hai!",
            parse_mode='Markdown'
        )
    except Exception as e:
        await status_msg.edit_text(f"❌ Failed to connect session string: `{e}`")

async def cmd_autoplay_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if OWNER_ID != 0 and user.id != OWNER_ID:
        return
    if update.message.chat.type != 'private':
        await update.message.reply_text("⚠️ Kripya `/autoplay off` command bot ke **Private Chat** mein use karein.")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM auto_chats WHERE is_enabled = 1")
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("⚠️ Wahan koi autoplay nahi chal raha ha!")
        return

    enabled_chats = [dict(r) for r in rows]
    user_states[user.id] = {"step": "disabling_chat", "enabled_chats": enabled_chats}

    lines = [f"📋 **Select Group Chat to Disable Auto-Play:**\n"]
    for idx, c in enumerate(enabled_chats, 1):
        lines.append(f"{idx}. **{escape_html(c['chat_title'])}** (`/{c['game_mode']}`)")
    lines.append("\n👉 *Kripya disable karne ke liye us Group ka **Number** send karein:*")

    await update.message.reply_text("\n".join(lines), parse_mode='Markdown')

async def cmd_autoplay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if OWNER_ID != 0 and user.id != OWNER_ID:
        return
    if update.message.chat.type != 'private':
        await update.message.reply_text("⚠️ Kripya `/autoplay` command bot ke **Private Chat** mein use karein taaki aap interactive wizard se group select kar sakein!")
        return

    args = context.args
    if args and args[0].lower() == "off":
        await cmd_autoplay_off(update, context)
        return

    if not userbot or not userbot.is_connected:
        await update.message.reply_text("🔴 Userbot connected nahi hai! Pehle account link karein using `/addacc` ya `/addsession`.")
        return

    status_msg = await update.message.reply_text("🔄 Fetching your joined group chats...")
    groups = []
    try:
        async for dialog in userbot.get_dialogs():
            if dialog.chat.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
                chat_info = db.get_chat(dialog.chat.id)
                is_on = bool(chat_info and chat_info.get("is_enabled"))
                mode = chat_info.get("game_mode", "new4") if chat_info else "new4"
                groups.append({
                    "id": dialog.chat.id,
                    "title": dialog.chat.title,
                    "is_on": is_on,
                    "mode": mode
                })
    except Exception as e:
        await status_msg.edit_text(f"❌ Failed to fetch chats: `{e}`")
        return

    if not groups:
        await status_msg.edit_text("⚠️ Userbot kisi bhi Group Chat mein joined nahi hai!")
        return

    user_states[user.id] = {"step": "selecting_chat", "groups": groups}

    lines = [f"📋 **Group Chats Auto-Play Dashboard:**\n"]
    for idx, g in enumerate(groups, 1):
        badge = f"🟢 ON (`/{g['mode']}`)" if g["is_on"] else "🔴 OFF"
        lines.append(f"{idx}. **{escape_html(g['title'])}** ({badge})")
    lines.append("\n👉 *Kripya kisi bhi Group ka **Number** send karein (e.g. `1` ya `5`):*")
    lines.append("• *Agar Auto-Play ON hoga, toh number bhejne par OFF ho jayega.*")
    lines.append("• *Agar OFF hoga, toh game mode select karke ON ho jayega.*")

    await status_msg.edit_text("\n".join(lines), parse_mode='Markdown')

async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if OWNER_ID != 0 and user.id != OWNER_ID:
        return
    chat_id = update.message.chat.id
    chat_title = update.message.chat.title or "Private Chat"
    args = context.args
    val = args[0] if args else "4"
    mode = "new5" if val == "5" else "new4"
    chat_info = db.get_chat(chat_id)
    enabled = chat_info.get("is_enabled", 1) if chat_info else 1
    db.set_chat_enabled(chat_id, chat_title, mode, enabled)
    await update.message.reply_text(f"✅ WordSeekBot Game Mode set to `/{mode}` (`{val}-letter words`)!", parse_mode='Markdown')

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = db.get_stats()
    text = (
        f"📊 **WordSeekBot Auto Player Statistics** 🎮\n\n"
        f"• 🔠 **Total Words Solved:** `{stats['total_words']}`\n"
        f"• 🏆 **Total Games Completed:** `{stats['total_games']}`\n"
        f"• 🤖 **Target Bot:** `@WordSeekBot`"
    )
    await update.message.reply_text(text, parse_mode='Markdown')

async def cmd_chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if OWNER_ID != 0 and user.id != OWNER_ID:
        await update.message.reply_text("🚫 Access Denied!")
        return

    if not userbot or not userbot.is_connected:
        await update.message.reply_text("🔴 Userbot is not connected! Please link an account first using `/addacc` or `/addsession`.", parse_mode='Markdown')
        return

    status_msg = await update.message.reply_text("🔄 Fetching joined group chats...")
    groups = []
    try:
        async for dialog in userbot.get_dialogs():
            if dialog.chat.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
                groups.append(f"• **{dialog.chat.title}** (`{dialog.chat.id}`)")
    except Exception as e:
        await status_msg.edit_text(f"❌ Failed to fetch chats: `{e}`")
        return

    if not groups:
        await status_msg.edit_text("⚠️ Userbot kisi bhi Group Chat mein joined nahi hai!")
        return

    groups_text = "\n".join(groups[:30])
    text = (
        f"📋 **Joined Group Chats List (Total: {len(groups)})**:\n\n"
        f"{groups_text}\n\n"
        "💡 *Aap jis bhi group mein auto-play on karna chahte hain, us group mein jaakar `/autoplay on` aur `/mode 4` send karein, fir `/new4` se game start karein!*"
    )
    await status_msg.edit_text(text, parse_mode='Markdown')

async def handle_private_states(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global userbot
    user = update.effective_user
    if not user or (OWNER_ID != 0 and user.id != OWNER_ID):
        return
    if update.message.chat.type != 'private':
        return

    user_id = user.id
    text = (update.message.text or "").strip()
    state = user_states.get(user_id)
    if not state:
        return

    step = state.get("step")

    if step == "waiting_phone":
        phone = text
        status_msg = await update.message.reply_text("🔄 Requesting verification code from Telegram...")
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
                "Kripya Telegram official app par aaya hua **OTP Code** yahan send karein (agar spaces ho toh hata dein, e.g. `12345`):",
                parse_mode='Markdown'
            )
        except Exception as e:
            await status_msg.edit_text(f"❌ Failed to send code: `{e}`")
            user_states.pop(user_id, None)

    elif step == "waiting_code":
        code = text.replace(" ", "")
        phone = state.get("phone")
        temp_client = state.get("client")
        phone_code_hash = state.get("phone_code_hash")
        status_msg = await update.message.reply_text("🔄 Signing in...")

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
            await setup_userbot_cache(userbot)
            register_word_seek_handler(userbot)

            await status_msg.edit_text(
                f"✅ **Telegram Account Linked Successfully!** 🎉\n\n"
                f"• **Name:** `{acc_name}`\n"
                f"• **User ID:** `{me.id}`\n\n"
                "Aapka userbot ab @WordSeekBot ke sath ready hai!",
                parse_mode='Markdown'
            )
        except SessionPasswordNeeded:
            user_states[user_id]["step"] = "waiting_password"
            await status_msg.edit_text(
                "🔒 **Two-Step Verification (2FA) Required!**\n\n"
                "Kripya apne account ka **Cloud Password** send karein:",
                parse_mode='Markdown'
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
        status_msg = await update.message.reply_text("🔄 Verifying 2FA password...")

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
            await setup_userbot_cache(userbot)
            register_word_seek_handler(userbot)

            await status_msg.edit_text(
                f"✅ **Telegram Account Linked Successfully (2FA Verified)!** 🎉\n\n"
                f"• **Name:** `{acc_name}`\n"
                f"• **User ID:** `{me.id}`",
                parse_mode='Markdown'
            )
        except Exception as e:
            try:
                await temp_client.disconnect()
            except Exception:
                pass
            await status_msg.edit_text(f"❌ Password verification failed: `{e}`")
            user_states.pop(user_id, None)

    elif step == "selecting_chat":
        try:
            choice = int(text)
            groups = state.get("groups", [])
            if choice < 1 or choice > len(groups):
                await update.message.reply_text("⚠️ Invalid number! Kripya list mein diya gaya sahi number send karein.")
                return
            selected_group = groups[choice - 1]
            chat_id = selected_group["id"]
            chat_title = selected_group["title"]

            chat_info = db.get_chat(chat_id)
            is_on = bool(chat_info and chat_info.get("is_enabled"))

            if is_on:
                # Auto-Play is currently ON -> Toggle OFF!
                db.set_chat_enabled(chat_id, chat_title, chat_info.get("game_mode", "new4"), 0)
                user_states.pop(user_id, None)
                await update.message.reply_text(
                    f"🛑 **Auto-Play Turned OFF!**\n\n"
                    f"• **Group:** `{chat_title}`\n"
                    f"• **Status:** Disabled 🔴",
                    parse_mode='Markdown'
                )
            else:
                # Auto-Play is currently OFF -> Ask for Game Mode to turn ON!
                user_states[user_id] = {
                    "step": "selecting_mode",
                    "chat_id": chat_id,
                    "chat_title": chat_title
                }
                await update.message.reply_text(
                    f"🎮 **Selected Group:** `{chat_title}`\n"
                    f"• **Status:** Currently OFF 🔴\n\n"
                    "Kripya game mode select karke Auto-Play ON karein:\n"
                    "1. `/new4` (4-Letter Words)\n"
                    "2. `/new5` (5-Letter Words)\n\n"
                    "👉 *Number send karein (`4` ya `5`):*",
                    parse_mode='Markdown'
                )
        except ValueError:
            await update.message.reply_text("⚠️ Kripya valid number send karein (e.g. `1`, `2`, etc.).")

    elif step == "selecting_mode":
        chat_id = state.get("chat_id")
        chat_title = state.get("chat_title")
        val = text.strip()
        if val in ["4", "new4", "1"]:
            mode = "new4"
        elif val in ["5", "new5", "2"]:
            mode = "new5"
        else:
            await update.message.reply_text("⚠️ Invalid choice! Kripya `4` ya `5` send karein.")
            return

        db.set_chat_enabled(chat_id, chat_title, mode, 1)
        user_states.pop(user_id, None)

        await update.message.reply_text(
            f"✅ **Auto-Play Enabled Successfully!** 🎉\n\n"
            f"• **Group:** `{chat_title}`\n"
            f"• **Game Mode:** `/{mode}`\n\n"
            f"🔄 *Userbot ab is group mein automatically `/{mode}` game start kar raha hai...*",
            parse_mode='Markdown'
        )

        if userbot and userbot.is_connected:
            ensure_chat_game_loop(userbot, chat_id)
            try:
                await userbot.send_message(chat_id, f"/{mode}")
                logger.info(f"🚀 Auto-started game `/{mode}` in chat {chat_title} ({chat_id})")
            except Exception as e:
                logger.error(f"Failed to auto-start game in {chat_id}: {e}")

    elif step == "selecting_drecruit_chat":
        try:
            choice = int(text)
            groups = state.get("groups", [])
            if choice < 1 or choice > len(groups):
                await update.message.reply_text("⚠️ Invalid number! Kripya list mein diya gaya sahi number send karein.")
                return
            selected_group = groups[choice - 1]
            chat_id = selected_group["id"]
            chat_title = selected_group["title"]

            user_states[user_id] = {
                "step": "selecting_drecruit_bot",
                "chat_id": chat_id,
                "chat_title": chat_title
            }
            await update.message.reply_text(
                f"🎮 **Selected Group:** `{chat_title}`\n\n"
                "Kripya target recruit bot select karein:\n"
                "1. `@Lookism_rec_bot`\n"
                "2. `@Gnejdjdjbot`\n"
                "3. Both / All (`@Lookism_rec_bot, @Gnejdjdjbot`)\n\n"
                "👉 *Number send karein (`1`, `2`, ya `3`):*",
                parse_mode='Markdown'
            )
        except ValueError:
            await update.message.reply_text("⚠️ Kripya valid number send karein (e.g. `1`, `2`, etc.).")

    elif step == "selecting_drecruit_bot":
        chat_id = state.get("chat_id")
        chat_title = state.get("chat_title")
        val = text.strip()
        if val in ["1", "@Lookism_rec_bot", "lookism"]:
            target_bot = "@Lookism_rec_bot"
        elif val in ["2", "@Gnejdjdjbot", "gnejdjdjbot"]:
            target_bot = "@Gnejdjdjbot"
        elif val in ["3", "both", "all"]:
            target_bot = "@Lookism_rec_bot, @Gnejdjdjbot"
        else:
            target_bot = val if val.startswith("@") else f"@{val}"

        db.set_drecruit_enabled(chat_id, chat_title, target_bot, 1)
        user_states.pop(user_id, None)

        await update.message.reply_text(
            f"✅ **DRecruit Turned ON Successfully!** 🎉\n\n"
            f"• **Group:** `{chat_title}`\n"
            f"• **Target Bot:** `{target_bot}`\n"
            f"• **Status:** Active 🟢\n\n"
            f"📝 *Jab bhi ye bot is group mein card drop karega, script automatic `/recruit <name>` input box mein draft kar degi aur aapko DM par report bhej degi!*",
            parse_mode='Markdown'
        )

    elif step == "waiting_for_card_details":
        target_msg = update.message.reply_to_message if update.message.reply_to_message else update.message
        msg_text = (target_msg.text or target_msg.caption or update.message.text or update.message.caption or "").strip()
        photo = target_msg.photo or update.message.photo

        if not msg_text and not photo:
            await update.message.reply_text("⚠️ Kripya valid card photo aur details forward/upload karein (ya `/cancel` type karein).")
            return

        char_name = ""
        tier_info = "Mortal"
        worth_info = "200"

        m_name = re.search(r"Name:\s*([^\n\r]+)", msg_text, re.IGNORECASE)
        if m_name:
            char_name = m_name.group(1).strip()

        m_tier = re.search(r"Tier:\s*([^\n\r]+)", msg_text, re.IGNORECASE)
        if m_tier:
            tier_info = m_tier.group(1).strip()

        m_worth = re.search(r"Worth:\s*([^\n\r]+)", msg_text, re.IGNORECASE)
        if m_worth:
            worth_info = m_worth.group(1).strip()

        if not char_name:
            await update.message.reply_text("❌ Card name extract nahi ho paya! Text mein 'Name: <Name>' hona chahiye. Dobara bhein ya `/cancel` karein.")
            return

        p_obj = photo[-1] if isinstance(photo, list) else photo
        file_unique_id = getattr(p_obj, "file_unique_id", "") if p_obj else ""
        file_id = getattr(p_obj, "file_id", "") if p_obj else ""
        clean_name = clean_card_name(char_name)

        lookism_cards_db[char_name] = {
            "name": char_name,
            "clean_name": clean_name,
            "description": f"{tier_info} - {worth_info}",
            "file_unique_id": file_unique_id,
            "file_id": file_id
        }

        try:
            with open(LOOKISM_DB_FILE, "w", encoding="utf-8") as f:
                json.dump(lookism_cards_db, f, indent=2, ensure_ascii=False)
        except Exception as e:
            await update.message.reply_text(f"❌ Failed to save to database file: `{e}`")
            return

        if file_unique_id:
            lookism_hash_index[file_unique_id] = clean_name

        user_states.pop(user.id, None)

        await update.message.reply_text(
            f"✅ **Card Saved Successfully via /scard!** 🎉\n\n"
            f"• **Original Name:** `{char_name}`\n"
            f"• **Clean Name (Draft):** `{clean_name}`\n"
            f"• **Tier / Worth:** `{tier_info} | {worth_info}`\n"
            f"• **Photo Hash:** `{file_unique_id or 'None'}`\n\n"
            "⚡ *Database aur memory index update ho gaye hain!*",
            parse_mode='Markdown'
        )

    elif step == "disabling_drecruit_chat":
        try:
            choice = int(text)
            chats = state.get("enabled_chats", [])
            if choice < 1 or choice > len(chats):
                await update.message.reply_text("⚠️ Wahan koi drecruit nahi chal raha ha!")
                return
            target = chats[choice - 1]
            chat_id = target["chat_id"]
            chat_title = target["chat_title"]

            db.set_drecruit_enabled(chat_id, chat_title, target.get("target_bot", "@Lookism_rec_bot"), 0)
            user_states.pop(user_id, None)

            await update.message.reply_text(
                f"🛑 **DRecruit Turned OFF Successfully!**\n\n"
                f"• **Group:** `{chat_title}`\n"
                f"• **Status:** Disabled 🔴",
                parse_mode='Markdown'
            )
        except ValueError:
            await update.message.reply_text("⚠️ Wahan koi drecruit nahi chal raha ha!")

    elif step == "disabling_chat":
        try:
            choice = int(text)
            chats = state.get("enabled_chats", [])
            if choice < 1 or choice > len(chats):
                await update.message.reply_text("⚠️ Wahan koi autoplay nahi chal raha ha!")
                return
            target = chats[choice - 1]
            chat_id = target["chat_id"]
            chat_title = target["chat_title"]

            db.set_chat_enabled(chat_id, chat_title, target["game_mode"], 0)
            user_states.pop(user_id, None)

            await update.message.reply_text(
                f"🛑 **Auto-Play Disabled Successfully!**\n\n"
                f"• **Group:** `{chat_title}`\n"
                f"• **Status:** Disabled 🔴",
                parse_mode='Markdown'
            )
        except ValueError:
            await update.message.reply_text("⚠️ Wahan koi autoplay nahi chal raha ha!")

async def main():
    global telegram_app
    logger.info("Starting WordSeekBot Automation Script (auto.py)...")
    await init_userbot()

    # Start self-healing watchdog supervisor task
    asyncio.create_task(watchdog_supervisor())

    telegram_app = Application.builder().token(BOT_TOKEN).build() if BOT_TOKEN else None
    if telegram_app:
        clear_telegram_webhook(BOT_TOKEN)

        telegram_app.add_handler(CommandHandler(["start", "help"], cmd_start))
        telegram_app.add_handler(CommandHandler("ping", cmd_ping))
        telegram_app.add_handler(CommandHandler("cancel", cmd_cancel))
        telegram_app.add_handler(CommandHandler("addt", cmd_addt))
        telegram_app.add_handler(CommandHandler("drecruit", cmd_drecruit))
        telegram_app.add_handler(CommandHandler("dbdwn", cmd_dbdwn))
        telegram_app.add_handler(CommandHandler("dbup", cmd_dbup))
        telegram_app.add_handler(CommandHandler("scard", cmd_scard))
        telegram_app.add_handler(CommandHandler("dcard", cmd_dcard))
        telegram_app.add_handler(CommandHandler("addacc", cmd_addacc))
        telegram_app.add_handler(CommandHandler("addsession", cmd_addsession))
        telegram_app.add_handler(CommandHandler("autoplay", cmd_autoplay))
        telegram_app.add_handler(CommandHandler("mode", cmd_mode))
        telegram_app.add_handler(CommandHandler("chats", cmd_chats))
        telegram_app.add_handler(CommandHandler("groups", cmd_chats))
        telegram_app.add_handler(CommandHandler("stats", cmd_stats))
        telegram_app.add_handler(MessageHandler((tg_filters.TEXT | tg_filters.PHOTO) & ~tg_filters.COMMAND, handle_private_states))

        try:
            await telegram_app.initialize()
            await telegram_app.start()
            await telegram_app.updater.start_polling(drop_pending_updates=True)
            logger.info("WordSeek Controller Bot connected successfully with python-telegram-bot!")
        except Exception as e:
            logger.warning(f"⚠️ Failed to start python-telegram-bot (retrying in 5s): {e}")
            await asyncio.sleep(5.0)
            try:
                await telegram_app.initialize()
                await telegram_app.start()
                await telegram_app.updater.start_polling(drop_pending_updates=True)
                logger.info("WordSeek Controller Bot connected successfully on retry!")
            except Exception as e2:
                logger.error(f"❌ Failed to start python-telegram-bot on retry: {e2}")

        if OWNER_ID != 0:
            try:
                await telegram_app.bot.send_message(
                    chat_id=OWNER_ID,
                    text="🤖 **WordSeek Auto Bot started successfully!** 🚀\nType /start to view dashboard.",
                    parse_mode='Markdown'
                )
            except Exception as e:
                logger.warning(f"Could not send startup message to owner: {e}")

    await idle()

    if telegram_app:
        try:
            await telegram_app.updater.stop()
            await telegram_app.stop()
            await telegram_app.shutdown()
        except Exception:
            pass

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Bot stopped gracefully by user (Ctrl+C). Exiting...")
