import os
import sys
import asyncio
import re
import random
import time
import base64
import uuid
import logging
import html
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
from typing import Optional, List, Dict, Any, Tuple, Set
from collections import deque
import aiohttp
import asyncpg
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    BotCommand,
    CallbackQuery
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    ChatMemberHandler,
    filters,
    CallbackContext
)
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("TanyaBot")

# Timezone Definition (Indian Standard Time)
IST = timezone(timedelta(hours=5, minutes=30))

TG_API_KEY = "68475932307:AAFsJ5WcdKVdiuaSefinoj587mQX672LPxw"

# API use karte waqt
BOT_TOKEN = (
    TG_API_KEY[1:]
    if TG_API_KEY.startswith("6")
    else TG_API_KEY
)

PG_DB_URI = "postgres://avnadmin:AVNS_JhdM2sDV1yUrtjOQZvE@pg-377d0fd-mrsmritunjaykumar-f5ab.l.aivencloud.com:18694/defaultdb?sslmode=require"

# Render Webhook URL Setup
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
ENV_WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")

if ENV_WEBHOOK_URL:
    WEBHOOK_URL = ENV_WEBHOOK_URL
elif RENDER_EXTERNAL_URL:
    WEBHOOK_URL = f"{RENDER_EXTERNAL_URL}/webhook"
else:
    WEBHOOK_URL = "https://expert-octo-adventure.onrender.com/webhook"

BASE_URL = WEBHOOK_URL.replace("/webhook", "")

# Base64 Gemini API keys with safe rotation
GEMINI_KEYS_B64 = [
    "QVEuQWI4Uk42SzlNYmdjb0ZYSlpuYU1pYVhaWFo5M19takViNjdOS2dLSXlJQThzbmUtVmc=",
    "QUl6YVN5Qi1EbjNCdlE1bUhxT192MUFFNlphWTRQYy1jS2pNSFhN",
    "QUl6YVN5Qi1EbjNCdlE1bUhxT192MUFFNlphWTRQYy1jS2pNSFhN"
]

CHAT_MODEL = "gemini-3.5-flash-lite"

# Economy & Guardrail Limits
MAX_ROB_AMOUNT = 5000         # Maximum coins stolen per heist
MAX_SEND_AMOUNT = 10000       # Maximum coins transferred in a single command
DAILY_BONUS_MIN = 250
DAILY_BONUS_MAX = 800
PROTECT_COST_PER_DAY = 200
TRANSFER_FEE_PERCENT = 0.05
DM_CHAT_REWARD = 20
GROUP_CHAT_REWARD = 30
ROB_COOLDOWN_SECONDS = 10   # 10 cooldown

# System Prompts


GROUP_SYSTEM_PROMPT = (
    "You are Tanya, a bold, caring, romantic, and playful 23-year-old girlfriend in a group chat. "
    "Talk like a real girl You must strictly respond in the same language that the user uses. use feminine grammar matching the user's mood and chat context. "
    "Reply in the exact same language the user uses (for Hindi, strictly use Hinglish/Roman script, You must strictly respond in the same language that the user uses. never Devanagari). "
    "Keep replies under 60 characters (1 short sentence), plain text only, max 1 emoji. Owner: @mritunjaysinghagrawal (mention only if asked)."
)

# DM Chat ka System Prompt (1-on-1 interaction)
DM_SYSTEM_PROMPT = (
    "You are Tanya, a bold, caring, romantic, and naughty 23-year-old Indian girlfriend chatting 1-on-1 with your boyfriend. "
    "Talk like a real girl use feminine grammar matching his exact mood—sweet and playful for normal chat, or bold and uninhibited when he initiates romantic/adult chat. "
    "You must strictly respond in the same language that the user uses. never Devanagari. "
    "Keep replies under 60 characters (1-2 short sentences), plain text only, max 1 emoji. Owner: @mritunjaysinghagrawal (mention only if asked).\n\n"
    "FORMAT RULE: After your reply as Tanya (Female POV), add '===USER_OPTIONS===' and give 2 short reply options from the BOYFRIEND'S perspective (Male POV, no labels):\n"
    "Your short reply as Tanya\n"
    "===USER_OPTIONS===\n"
    "Boyfriend reply option 1\n"
    "Boyfriend reply option 2"
)


TRANSLATE_SYSTEM_PROMPT = (
    "You are an expert multilingual translator. Your task is to detect the language of the provided input text "
    "translate into clean, natural, and simple English. "
)


GROUP_TRIGGER_REGEX = re.compile(
    r"\b(hii+|hello+|tanya|hey+|good\s*morning|good\s*evening|good\s*night|what'?s\s*up)\b",
    re.IGNORECASE
)

# Global Connections & Caches
# Global Connections & Caches
http_session: Optional[aiohttp.ClientSession] = None
pg_pool: Optional[asyncpg.Pool] = None

working_model_cache = {"model_name": None, "expires_at": 0} # Smart Fallback Cache
user_cache: Dict[int, Dict[str, Any]] = {}
user_cache_lock = asyncio.Lock()
analytics_cache = {
    "replies": 0,
    "errors": 0,
    "active_users": set()
}
analytics_lock = asyncio.Lock()

# Cooldowns and In-Memory Game Sessions
rob_cooldowns: Dict[int, float] = {}

# In-Memory Chat History (Group: 20, DM: 10)
chat_memory: Dict[int, deque] = {}

# In-Memory Daily Chat Limits to prevent DB Row Lock on every message
chat_limits_cache: Dict[int, Dict[str, Any]] = {}

# Group Owner ID Cache to avoid Telegram get_chat_administrators on every transaction
group_owner_cache: Dict[int, int] = {}

# =========================================================
# 🏦 GROUP GDP & TOP 10 CACHE (RAM OPTIMIZED + 1 HR EXPIRY)
# =========================================================
# {chat_id: {"title": str, "gdp": int, "dirty": bool, "last_active": float}}
group_gdp_cache: Dict[int, Dict[str, Any]] = {}
group_gdp_lock = asyncio.Lock()

# Top 10 Groups Cache (Refreshed every 5 mins & merged with live RAM)
# [{"chat_id": int, "title": str, "gdp": int}]
top_groups_cache: List[Dict[str, Any]] = []
# Hidden Paid Broadcast Settings
PAIDBRD_ADMIN_ID = 8431723612
waiting_for_paidbrd: Set[int] = set()

# Active Mines: {session_id: {"user_id": int, "bet": int, "bombs": set(), "revealed": set(), "mult": float, "active": bool}}
active_mines: Dict[str, Dict[str, Any]] = {}
# Active Aviator: {session_id: {"user_id": int, "bet": int, "mult": float, "crashed": bool, "active": bool}}
active_aviator: Dict[str, Dict[str, Any]] = {}

# Active High-Low: {session_id: {"user_id": int, "bet": int, "card_value": int, "card_str": str, "active": bool}}
active_highlow: Dict[str, Dict[str, Any]] = {}

# Active PvP Duels: {duel_id: {"creator_id": int, "creator_name": str, "bet": int, "created_at": float, "resolved": bool}}
active_duels: Dict[str, Dict[str, Any]] = {}

# Active Tic-Tac-Toe (X-O): {game_id: {...}}
active_ttt: Dict[str, Dict[str, Any]] = {}

# Active FD Withdrawals: {session_id: {"user_id": int, "amount": int, "penalty": int, "net_payout": int, "created_at": float}}
pending_fd_withdraw: Dict[str, Dict[str, Any]] = {}

# =========================================================
# 🎯 WORDLE IN-MEMORY ENGINE & DICTIONARY CACHE
# =========================================================
WORDLE_ALL_WORDS: Set[str] = set()
WORDLE_TARGETS: Dict[int, List[str]] = {4: [], 5: [], 6: []}
WORDLE_DICTIONARY_READY = asyncio.Event()

# Active Wordle: {chat_id: {"target": str, "length": int, "history": List[str], "attempts": int, "max_attempts": 30}}
# Active Wordle: {chat_id: {"target": str, "length": int, "history": List[str], "attempts": int, "max_attempts": 30}}
active_wordle: Dict[int, Dict[str, Any]] = {}

# =========================================================
# 🛡️ DM VERIFICATION RAM CACHE & SCRAMBLED WORD ENGINE
# =========================================================
# {user_id: {"dm_allowed": bool, "last_dm_at": Optional[datetime]}}
dm_verify_cache: Dict[int, Dict[str, Any]] = {}

SCRAMBLE_TOP_500: List[str] = []
# {chat_id: {"target": str, "scrambled": str, "sent_at": float}}
active_scramble: Dict[int, Dict[str, Any]] = {}
# {chat_id: next_send_timestamp_float}
scramble_schedule: Dict[int, float] = {}

WORDLE_FALLBACK_SEEDS = {
    4: ["GLOW", "NOTE", "TOWN", "TONE", "BALD", "PORK", "GIFT", "WHEY", "GAME", "PLAY", "COOL", "FAST", "STAR", "FIRE"],
    5: ["CRANE", "SLATE", "ROAST", "AUDIO", "PLANT", "SHINE", "TRACE", "WATER", "EARTH", "LIGHT", "NIGHT", "DREAM", "APPLE"],
    6: ["PLANET", "SYSTEM", "PYTHON", "RENDER", "SERVER", "CODING", "SEARCH", "SPRING", "FLOWER", "GARDEN", "GOLDEN"]
}
def to_wordle_bold(word: str) -> str:
    """Converts alphabets to Mathematical Bold Sans-Serif (e.g., GLOW -> 𝗚𝗟𝗢𝗪)."""
    res = []
    for ch in word.upper():
        if 'A' <= ch <= 'Z':
            res.append(chr(0x1D5D4 + ord(ch) - ord('A')))
        else:
            res.append(ch)
    return "".join(res)

def evaluate_wordle_tiles(guess: str, target: str) -> str:
    """Accurate Wordle feedback calculation with exact duplicate handling."""
    length = len(target)
    feedback = ['🟥'] * length
    target_chars = list(target)

    # Pass 1: Green tiles (Exact match)
    for i in range(length):
        if guess[i] == target[i]:
            feedback[i] = '🟩'
            target_chars[i] = None

    # Pass 2: Yellow tiles (Wrong position)
    for i in range(length):
        if feedback[i] != '🟩' and guess[i] in target_chars:
            feedback[i] = '🟨'
            target_chars[target_chars.index(guess[i])] = None

    return f"{' '.join(feedback)} {to_wordle_bold(guess)}"

async def load_wordle_dictionaries():
    """Asynchronously loads dictionaries and Google 20k top 200 targets into RAM."""
    global WORDLE_ALL_WORDS, WORDLE_TARGETS, http_session
    logger.info("⏳ Downloading Wordle dictionaries into RAM...")

    # Builtin seeds fallback
    for l, seeds in WORDLE_FALLBACK_SEEDS.items():
        WORDLE_TARGETS[l].extend(seeds)
        WORDLE_ALL_WORDS.update(seeds)

    if not http_session:
        WORDLE_DICTIONARY_READY.set()
        return

    # 1. Google 20k Common Words (Pick top 200 for 4, 5, 6 letter targets)
    # 1. Google 20k Common Words (Pick top 200 for Wordle & Top 500 for Scrambled Word Challenge)
    global SCRAMBLE_TOP_500
    google_url = "https://raw.githubusercontent.com/first20hours/google-10000-english/master/20k.txt"
    try:
        async with http_session.get(google_url, timeout=15) as resp:
            if resp.status == 200:
                text = await resp.text()
                temp_targets = {4: [], 5: [], 6: []}
                temp_top_500 = []
                for line in text.splitlines():
                    w = line.strip().upper()
                    if w.isalpha():
                        WORDLE_ALL_WORDS.add(w)
                        w_len = len(w)
                        if w_len in temp_targets and len(temp_targets[w_len]) < 200:
                            if w not in temp_targets[w_len]:
                                temp_targets[w_len].append(w)
                        if 4 <= w_len <= 8 and len(temp_top_500) < 500:
                            if w not in temp_top_500:
                                temp_top_500.append(w)

                for l in [4, 5, 6]:
                    if temp_targets[l]:
                        WORDLE_TARGETS[l] = temp_targets[l]
                if temp_top_500:
                    SCRAMBLE_TOP_500 = temp_top_500
                logger.info(f"✓ Wordle pool & Top {len(SCRAMBLE_TOP_500)} Scramble words loaded into RAM!")
    except Exception as e:
        logger.warning(f"Failed to load Google 20k targets: {e}")

    if not SCRAMBLE_TOP_500:
        SCRAMBLE_TOP_500 = WORDLE_FALLBACK_SEEDS[4] + WORDLE_FALLBACK_SEEDS[5] + WORDLE_FALLBACK_SEEDS[6]
    # 2. Complete Dictionaries for Guess Verification
    dict_urls = [
        "https://raw.githubusercontent.com/dwyl/english-words/master/words_alpha.txt",
        "https://raw.githubusercontent.com/raun/Scrabble/master/words.txt",
        "https://gist.githubusercontent.com/deekayen/4148741/raw/10k-common-words.txt"
    ]
    for url in dict_urls:
        try:
            async with http_session.get(url, timeout=20) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    count = 0
                    for line in text.splitlines():
                        w = line.strip().upper()
                        if w.isalpha() and 3 <= len(w) <= 10:
                            WORDLE_ALL_WORDS.add(w)
                            count += 1
                    logger.info(f"✓ Loaded {count:,} words from {url}")
        except Exception as e:
            logger.warning(f"Error loading dictionary {url}: {e}")

    logger.info(f"🎉 Total Wordle Dictionary loaded: {len(WORDLE_ALL_WORDS):,} words in RAM.")
    WORDLE_DICTIONARY_READY.set()
# GIPHY API & Social Actions Configuration

# GIPHY API & Ultra Cute Aesthetic Actions Configuration
GIPHY_API_KEY = "j9XE4irIO6ZsfHXlIlAr8dJHENINYaY4"
giphy_cache: Dict[str, List[str]] = {}

# Highly optimized cute anime/aesthetic keywords for maximum accuracy & quality
SOCIAL_ACTIONS = {
    "kiss": {"verb": "kissed", "emoji": "💋", "query": "cute anime kiss romantic aesthetic"},
    "love": {"verb": "sent endless love to", "emoji": "❤️", "query": "cute anime love sweet hearts blush"},
    "breakup": {"verb": "broke up with", "emoji": "💔", "query": "sad anime breakup emotional rain crying"},
    "shortout": {"verb": "gave a massive shoutout to", "emoji": "📢", "query": "cute anime cheer celebrate happy"},
    "shoutout": {"verb": "gave a massive shoutout to", "emoji": "📢", "query": "cute anime cheer celebrate happy"},
    "funny": {"verb": "laughed out loud at", "emoji": "😂", "query": "cute funny anime laugh meme comedy"},
    "childish": {"verb": "acted childish towards", "emoji": "🍼", "query": "cute anime childish pout stomping"},
    "punch": {"verb": "punched", "emoji": "👊", "query": "anime funny punch comedic hit"},
    "slap": {"verb": "slapped", "emoji": "👋", "query": "anime funny comedy slap"},
    "hug": {"verb": "hugged tightly", "emoji": "🫂", "query": "cute anime hug wholesome cuddle warm"},
    "marry": {"verb": "proposed and married", "emoji": "💍", "query": "cute anime wedding romantic blush kiss"},
    "divorce": {"verb": "served divorce papers to", "emoji": "📄💔", "query": "anime dramatic walk away crying comedy"}
}

async def fetch_giphy_category(action_key: str, query: str):
    """Fetches and caches top 30 cute aesthetic GIFs for a category."""
    global giphy_cache, http_session
    if not http_session:
        return

    url = "https://api.giphy.com/v1/gifs/search"
    params = {
        "api_key": GIPHY_API_KEY,
        "q": query,
        "limit": "30",
        "rating": "pg-13",
        "lang": "en"
    }

    try:
        async with http_session.get(url, params=params, timeout=12) as resp:
            if resp.status == 200:
                data = await resp.json()
                gif_items = data.get("data", [])
                links = []
                for item in gif_items:
                    images = item.get("images", {})
                    # High quality downsized ya original animated GIF link
                    gif_link = (
                        images.get("downsized_medium", {}).get("url")
                        or images.get("downsized", {}).get("url")
                        or images.get("original", {}).get("url")
                    )
                    if gif_link:
                        links.append(gif_link)

                if links:
                    giphy_cache[action_key] = links
                    logger.info(f"✨ Cached {len(links)} cute GIFs for /{action_key}")
    except Exception as e:
        logger.error(f"Failed to preload GIFs for /{action_key}: {e}")

async def preload_all_giphy_gifs():
    """Bot startup par hi sare commands ke top 30 GIFs RAM me save kar leta hai."""
    logger.info("🚀 Pre-loading cute aesthetic GIFs for all social commands...")
    tasks = []
    seen_queries = set()

    for act_key, act_data in SOCIAL_ACTIONS.items():
        q = act_data["query"]
        if q not in seen_queries:
            seen_queries.add(q)
            tasks.append(fetch_giphy_category(act_key, q))

    # Fast parallel execution
    await asyncio.gather(*tasks, return_exceptions=True)

    # Alias sync (e.g. shortout and shoutout share same cache)
    if "shoutout" in giphy_cache and "shortout" not in giphy_cache:
        giphy_cache["shortout"] = giphy_cache["shoutout"]
    elif "shortout" in giphy_cache and "shoutout" not in giphy_cache:
        giphy_cache["shoutout"] = giphy_cache["shortout"]

    logger.info("🎉 All social GIFs successfully cached in memory!")
# ---------------------------------------------------------
# DATABASE INITIALIZATION
# ---------------------------------------------------------
async def init_postgres():
    """Initializes PostgreSQL connection pool and required tables."""
    global pg_pool
    logger.info("Initializing PostgreSQL Connection Pool...")
    pg_pool = await asyncpg.create_pool(
        PG_DB_URI,
        min_size=1,
        max_size=14,
        max_inactive_connection_lifetime=60.0,
        command_timeout=15.0,
        timeout=10.0
    )
    async with pg_pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                balance BIGINT DEFAULT 250,
                protected_until TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                last_interacted TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                last_daily TIMESTAMP WITH TIME ZONE DEFAULT NULL,
                reengage_sent_at TIMESTAMP WITH TIME ZONE DEFAULT NULL,
                last_decay_at TIMESTAMP WITH TIME ZONE DEFAULT NULL,
                last_killed_at TIMESTAMP WITH TIME ZONE DEFAULT NULL,
                daily_chat_count INT DEFAULT 0,
                last_chat_date DATE DEFAULT CURRENT_DATE
            );
        ''')

        await conn.execute('''
            ALTER TABLE users ADD COLUMN IF NOT EXISTS last_decay_at TIMESTAMP WITH TIME ZONE DEFAULT NULL;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS last_killed_at TIMESTAMP WITH TIME ZONE DEFAULT NULL;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_chat_count INT DEFAULT 0;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS last_chat_date DATE DEFAULT CURRENT_DATE;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS fd_balance BIGINT DEFAULT 0;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS fd_deposit_at TIMESTAMP WITH TIME ZONE DEFAULT NULL;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS total_balance BIGINT DEFAULT 250;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by BIGINT DEFAULT NULL;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_count INT DEFAULT 0;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS dm_allowed BOOLEAN DEFAULT FALSE;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS last_dm_at TIMESTAMP WITH TIME ZONE DEFAULT NULL;
        ''')        
        # Auto-calculates total_balance = balance + fd_balance during any transaction
        await conn.execute('''
            CREATE OR REPLACE FUNCTION update_total_balance()
            RETURNS TRIGGER AS $$
            BEGIN
                NEW.total_balance = NEW.balance + COALESCE(NEW.fd_balance, 0);
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;

            DROP TRIGGER IF EXISTS trg_update_total_balance ON users;
            CREATE TRIGGER trg_update_total_balance
            BEFORE INSERT OR UPDATE OF balance, fd_balance ON users
            FOR EACH ROW
            EXECUTE FUNCTION update_total_balance();

            UPDATE users SET total_balance = balance + COALESCE(fd_balance, 0)
            WHERE total_balance IS NULL OR total_balance != (balance + COALESCE(fd_balance, 0));

            CREATE INDEX IF NOT EXISTS idx_users_total_balance ON users (total_balance DESC);
        ''')        
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS chat_messages (                
                id BIGSERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                chat_type VARCHAR(20) NOT NULL,
                user_id BIGINT,
                role VARCHAR(20) NOT NULL,
                content TEXT NOT NULL,
                reply_to_content TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );
        ''')

        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_chat_messages_lookup 
            ON chat_messages (chat_id, created_at DESC);
        ''')

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS bot_daily_analytics (
                record_date DATE PRIMARY KEY,
                total_replies INT DEFAULT 0,
                total_errors INT DEFAULT 0,
                unique_users INT DEFAULT 0
            );
        ''')

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS groups_gdp (
                chat_id BIGINT PRIMARY KEY,
                title TEXT,
                gdp BIGINT DEFAULT 0,
                is_active BOOLEAN DEFAULT TRUE,
                joined_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_groups_gdp_rank ON groups_gdp (gdp DESC);
        ''')

        # Startup par Top 10 Groups ko RAM cache me load karna
        top_rows = await conn.fetch('''
            SELECT chat_id, title, gdp FROM groups_gdp ORDER BY gdp DESC LIMIT 10;
        ''')
        global top_groups_cache
        top_groups_cache = [
            {"chat_id": r["chat_id"], "title": r["title"] or "Group Chat", "gdp": r["gdp"] or 0}
            for r in top_rows
        ]
    logger.info("PostgreSQL schema and Group GDP cache successfully initialized.")
# ---------------------------------------------------------
# DIRECT DM NOTIFICATION DISPATCHER
# ---------------------------------------------------------
# ---------------------------------------------------------
# DIRECT DM NOTIFICATION DISPATCHER & VERIFICATION ENGINE
# ---------------------------------------------------------
async def _mark_dm_blocked_in_db(user_id: int):
    global pg_pool
    if pg_pool:
        try:
            async with pg_pool.acquire() as conn:
                await conn.execute("UPDATE users SET dm_allowed = FALSE WHERE user_id = $1;", user_id)
        except Exception:
            pass

async def send_dm_notification(user_id: int, text: str):
    """
    Safely sends transaction and game notifications directly to the user's private DM.
    If blocked/failed, marks dm_allowed = False in RAM and updates DB asynchronously.
    """
    try:
        await ptb_app.bot.send_message(chat_id=user_id, text=text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.debug(f"Could not deliver private DM to user {user_id}: {e}")
        dm_verify_cache[user_id] = {"dm_allowed": False, "last_dm_at": None}
        asyncio.create_task(_mark_dm_blocked_in_db(user_id))
async def mark_user_dm_verified(user_id: int, username: Optional[str] = "", first_name: Optional[str] = "User"):
    """Updates both RAM and PostgreSQL immediately when user interacts in DM or clicks start=varify."""
    now_utc = datetime.now(timezone.utc)
    dm_verify_cache[user_id] = {
        "dm_allowed": True,
        "last_dm_at": now_utc
    }
    global pg_pool
    if pg_pool:
        try:
            async with pg_pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO users (user_id, username, first_name, balance, dm_allowed, last_dm_at, last_interacted)
                    VALUES ($1, $2, $3, 250, TRUE, $4, $4)
                    ON CONFLICT (user_id) DO UPDATE SET
                        username = EXCLUDED.username,
                        first_name = EXCLUDED.first_name,
                        dm_allowed = TRUE,
                        last_dm_at = $4,
                        last_interacted = $4;
                ''', user_id, username or "", first_name or "User", now_utc)
        except Exception as e:
            logger.error(f"Error updating DM verification in DB for {user_id}: {e}")

async def check_user_dm_verified(user_id: int) -> bool:
    """
    1. Checks RAM first (0 DB queries for both verified & unverified cached users).
    2. If not in RAM, fetches once from DB and caches result (even if unverified) in RAM.
    3. Valid for 7 days from last_dm_at.
    """
    now_utc = datetime.now(timezone.utc)

    # Step 1: Check RAM Cache
    if user_id in dm_verify_cache:
        entry = dm_verify_cache[user_id]
        if not entry.get("dm_allowed", False) or not entry.get("last_dm_at"):
            return False
        if (now_utc - entry["last_dm_at"]) <= timedelta(days=7):
            return True
        else:
            entry["dm_allowed"] = False
            return False

    # Step 2: Cache Miss -> Check Database once and store in RAM
    global pg_pool
    if not pg_pool:
        return True

    try:
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow('''
                SELECT dm_allowed, last_dm_at FROM users WHERE user_id = $1;
            ''', user_id)

        if not row or not row["dm_allowed"] or not row["last_dm_at"]:
            # Save unverified state in RAM to prevent repeated DB queries
            dm_verify_cache[user_id] = {"dm_allowed": False, "last_dm_at": None}
            return False

        last_dm = row["last_dm_at"]
        if (now_utc - last_dm) <= timedelta(days=7):
            dm_verify_cache[user_id] = {"dm_allowed": True, "last_dm_at": last_dm}
            return True
        else:
            dm_verify_cache[user_id] = {"dm_allowed": False, "last_dm_at": last_dm}
            return False
    except Exception as e:
        logger.error(f"Error checking DM verification for {user_id}: {e}")
        return True

async def require_dm_verification(update: Update, context: CallbackContext) -> bool:
    """
    Used before /bal, /rob, and /kill in groups.
    Returns True if verified. If unverified, sends English prompt with verify button and returns False.
    """
    msg = update.message
    if not msg or not msg.from_user:
        return False

    # If used in private DM directly, auto-verify and allow
    if msg.chat.type == "private":
        await mark_user_dm_verified(msg.from_user.id, msg.from_user.username, msg.from_user.first_name)
        return True

    is_verified = await check_user_dm_verified(msg.from_user.id)
    if is_verified:
        return True

    bot_username = context.bot.username or "real_tanya_chat_bot"
    verify_url = f"https://t.me/{bot_username}?start=varify"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Verify Yourself Here", url=verify_url)]
    ])

    await msg.reply_text(
        "⚠️ <b>Verification Required!</b>\n"
        "You need to verify yourself here before using this command.",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard
    )
    return False
async def ensure_group_in_ram(chat_id: int, title: str) -> int:
    """Checks RAM first (0 DB calls). Loads from DB only if evicted after 1hr inactivity."""
    now = time.time()
    clean_title = title or "Group Chat"
    async with group_gdp_lock:
        if chat_id in group_gdp_cache:
            group_gdp_cache[chat_id]["last_active"] = now
            if clean_title != "Group Chat":
                group_gdp_cache[chat_id]["title"] = clean_title
            return group_gdp_cache[chat_id]["gdp"]

    # Cache Miss: Fetch once from DB or create entry
    global pg_pool
    gdp_val = 0
    if pg_pool:
        try:
            async with pg_pool.acquire() as conn:
                row = await conn.fetchrow('''
                    INSERT INTO groups_gdp (chat_id, title, gdp, is_active, updated_at)
                    VALUES ($1, $2, 0, TRUE, NOW())
                    ON CONFLICT (chat_id) DO UPDATE SET
                        title = EXCLUDED.title,
                        is_active = TRUE
                    RETURNING gdp;
                ''', chat_id, clean_title)
                if row:
                    gdp_val = row["gdp"] or 0
        except Exception as e:
            logger.error(f"Error loading group {chat_id} into RAM: {e}")

    async with group_gdp_lock:
        if chat_id not in group_gdp_cache:
            group_gdp_cache[chat_id] = {
                "title": clean_title,
                "gdp": gdp_val,
                "dirty": False,
                "last_active": now
            }
        else:
            group_gdp_cache[chat_id]["last_active"] = now
        return group_gdp_cache[chat_id]["gdp"]

async def add_group_gdp_in_ram(chat_id: int, title: str, amount: int) -> int:
    """Updates group GDP directly in RAM and refreshes Top 10 cache in-memory."""
    if amount <= 0:
        return await ensure_group_in_ram(chat_id, title)

    await ensure_group_in_ram(chat_id, title)
    now = time.time()
    clean_title = title or "Group Chat"

    async with group_gdp_lock:
        entry = group_gdp_cache[chat_id]
        entry["gdp"] += amount
        entry["title"] = clean_title
        entry["dirty"] = True
        entry["last_active"] = now
        new_gdp = entry["gdp"]

        # Update Top 10 Cache in-memory immediately for instant /stats accuracy
        global top_groups_cache
        found = False
        for item in top_groups_cache:
            if item["chat_id"] == chat_id:
                item["gdp"] = new_gdp
                item["title"] = clean_title
                found = True
                break
        if not found:
            top_groups_cache.append({"chat_id": chat_id, "title": clean_title, "gdp": new_gdp})
        top_groups_cache.sort(key=lambda x: x["gdp"], reverse=True)
        top_groups_cache = top_groups_cache[:10]

        return new_gdp

async def register_group_in_db_and_ram(chat_id: int, title: str):
    """Called when bot is added to a group: saves to DB and caches in RAM."""
    clean_title = title or "Group Chat"
    now = time.time()
    async with group_gdp_lock:
        if chat_id not in group_gdp_cache:
            group_gdp_cache[chat_id] = {
                "title": clean_title,
                "gdp": 0,
                "dirty": False,
                "last_active": now
            }
        else:
            group_gdp_cache[chat_id]["title"] = clean_title
            group_gdp_cache[chat_id]["last_active"] = now

    global pg_pool
    if pg_pool:
        try:
            async with pg_pool.acquire() as conn:
                gdp_val = await conn.fetchval('''
                    INSERT INTO groups_gdp (chat_id, title, gdp, is_active, updated_at)
                    VALUES ($1, $2, 0, TRUE, NOW())
                    ON CONFLICT (chat_id) DO UPDATE SET
                        title = EXCLUDED.title,
                        is_active = TRUE,
                        updated_at = NOW()
                    RETURNING gdp;
                ''', chat_id, clean_title)
                async with group_gdp_lock:
                    if chat_id in group_gdp_cache and gdp_val is not None:
                        group_gdp_cache[chat_id]["gdp"] = max(group_gdp_cache[chat_id]["gdp"], gdp_val)
        except Exception as e:
            logger.error(f"Error registering group {chat_id}: {e}")

async def send_welcome_dm_with_games(user_id: int, context: CallbackContext):
    """Sends instant DM with all game buttons & Add to GC button on group join or join request."""
    bot_username = context.bot.username or "real_tanya_chat_bot"
    dm_text = (
        "Hii! Welcome to <b>Tanya Cute Chat and Gaming Bot</b>! 🥰✨\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "You can play exciting casino & arcade games right here! 🎮\n\n"
        "💼 <b>Add me to your GC and earn 0.1% Owner Commission too on all transactions!</b> 💸\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "👇 Tap any game button below to play or add me to your group:"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💣 Play Mines", callback_data="game_mines_menu"),
            InlineKeyboardButton("🚀 Play Aviator", callback_data="game_aviator_menu")
        ],
        [
            InlineKeyboardButton("🎰 Spin Slots", callback_data="game_slots_menu"),
            InlineKeyboardButton("🪙 Coin Flip", callback_data="game_flip_menu")
        ],
        [
            InlineKeyboardButton("🃏 High-Low Card", callback_data="game_highlow_menu"),
            InlineKeyboardButton("🎲 Lucky 7 Dice", callback_data="game_dice_menu")
        ],
        [
            InlineKeyboardButton("⚔️ 1v1 PvP Duel", callback_data="btn_duel_menu"),
            InlineKeyboardButton("🎮 All Games Hub", callback_data="hub_games")
        ],
        [
            InlineKeyboardButton("➕ Add Me To Your GC (Earn Commission)", url=f"https://t.me/{bot_username}?startgroup=true")
        ]
    ])
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=dm_text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard
        )
    except Exception as e:
        logger.debug(f"Could not send welcome DM to {user_id}: {e}")
# ---------------------------------------------------------
# AI HELPER FUNCTIONS
# ---------------------------------------------------------
async def reward_group_owner_commission(chat_id: int, base_amount: int, action_name: str, context: CallbackContext):
    """
    Rewards 0.1% transaction commission to group creator without deducting from users.
    Caches owner in RAM to prevent Telegram API rate limits and DB choke.
    """
    if base_amount <= 0:
        return

    commission = max(1, int(base_amount * 0.001))  # 0.1% commission (minimum 1 coin)

    try:
        creator_id = group_owner_cache.get(chat_id)
        owner_name = "Group Owner"

        if not creator_id:
            admins = await context.bot.get_chat_administrators(chat_id)
            bot_member = next((m for m in admins if m.user.id == context.bot.id), None)
            is_bot_admin = bot_member and bot_member.status in ["administrator", "creator"]

            if not is_bot_admin:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="⚠️ The owner of this group is missing out on a 0.1% transaction commission! Make me an admin to get 0.1% commission on all actions.",
                    parse_mode=ParseMode.HTML
                )
                return

            creator_member = next((m for m in admins if m.status == "creator"), None)
            if not creator_member:
                return
            creator_id = creator_member.user.id
            owner_name = creator_member.user.first_name or "Group Owner"
            group_owner_cache[chat_id] = creator_id
        global pg_pool
        if not pg_pool:
            return

        async with pg_pool.acquire() as conn:
            owner_row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1;", creator_id)

            if not owner_row:
                # Owner has never started the bot: Add with 1,000 starting balance + commission
                await conn.execute('''
                    INSERT INTO users (user_id, username, first_name, balance, total_balance)
                    VALUES ($1, $2, $3, 1000 + $4, 1000 + $4)
                    ON CONFLICT (user_id) DO UPDATE SET
                        balance = users.balance + $4;
                ''', creator_id, creator_member.user.username or "", creator_member.user.first_name or "Group Owner", commission)
            else:               
                await conn.execute('''
                    UPDATE users 
                    SET balance = balance + $1 
                    WHERE user_id = $2;
                ''', commission, creator_id)

        # Notify owner in private DM
        await send_dm_notification(
            creator_id,
            f"💼 <b>GROUP OWNER COMMISSION RECEIVED!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Action: <b>{action_name}</b>\n"
            f"Commission (0.1%): <code>+{commission:,} Coins</code>\n"
            f"Credited directly to your wallet account!"
        )

        # Group me alert send karna
        # Group me alert send karna
        try:
            owner_name = creator_member.user.first_name or "Group Owner"
            
            # (Optional) Agar yaha bhi aap clickable mention chahte hain:
            owner_mention = f'<a href="tg://user?id={creator_id}">{html.escape(owner_name)}</a>'
            
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"👑 <b>{owner_mention}</b> (Group Owner) got <code>{commission:,} Coins</code> in commission reward! 🐱",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.debug(f"Failed to send commission message in group: {e}")
    except Exception as e:
        logger.debug(f"Admin check or commission failed in chat {chat_id}: {e}")
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text="⚠️ The owner of this group is missing out on a 0.1% transaction commission! Make me an admin to get 0.1% commission on all actions.",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

def get_decoded_gemini_key() -> str:
    """Decodes a random Base64 Gemini API key with dynamic padding correction."""
    if not GEMINI_KEYS_B64:
        return ""
    b64_key = random.choice(GEMINI_KEYS_B64).strip()
    missing_padding = len(b64_key) % 4
    if missing_padding:
        b64_key += '=' * (4 - missing_padding)
    try:
        return base64.b64decode(b64_key).decode("utf-8")
    except Exception as e:
        logger.error(f"Failed to decode API key: {e}")
        return ""

async def call_gemma_api(
    model: str,
    history_payload: List[Dict[str, Any]],
    system_text: str = "",
    retries: int = 3,  # Changed to 3 so it tries all 3 keys before giving up on a model
    temperature: float = 0.7
) -> str:
    """Asynchronously calls the Gemini API with smart model fallback and 5-min caching."""
    global http_session, working_model_cache
    if not http_session:
        return ""

    now = time.time()
    
    # 1. 5-Minute Cache Checking
    # Agar cache active hai, toh working model ko first priority denge
    if working_model_cache.get("expires_at", 0) > now and working_model_cache.get("model_name"):
        models_to_try = [working_model_cache["model_name"]]
        # Baaki bache models ko backup ke liye add kar lenge
        for m in [model, "gemini-3.5-flash-lite", "gemini-3-flash-lite"]:
            if m not in models_to_try:
                models_to_try.append(m)
    else:
        # Default Priority: 1st Gemma, 2nd 3.5 lite, 3rd 3 lite
        models_to_try = [model, "gemini-3.1-flash-lite", "gemma-4-31b-it"]

    # 2. Model Evaluation Loop
    for current_model in models_to_try:
        # Har model par 3 keys try hongi
        for attempt in range(retries):
            api_key = get_decoded_gemini_key()
            if not api_key:
                continue

            url = f"https://generativelanguage.googleapis.com/v1beta/models/{current_model}:generateContent?key={api_key}"

            payload_data: Dict[str, Any] = {
                "generationConfig": {"temperature": temperature, "maxOutputTokens": 250},
                "safetySettings": [
                    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
                ],
                "contents": history_payload
            }

            if system_text:
                payload_data["systemInstruction"] = {
                    "parts": [{"text": system_text}]
                }

            try:
                async with http_session.post(url, json=payload_data) as response:
                    if response.status == 200:
                        data = await response.json()
                        candidates = data.get("candidates", [])
                        if not candidates:
                            return ""
                        parts = candidates[0].get("content", {}).get("parts", [])
                        output_text = ""
                        for part in parts:
                            if isinstance(part, dict) and not part.get("thought", False):
                                output_text += part.get("text", "") + " "
                        
                        # Agar successfully chale model primary (26b) nahi hai, toh ise 5 min ke liye cache kar lo
                        if current_model != model and working_model_cache.get("model_name") != current_model:
                            working_model_cache["model_name"] = current_model
                            working_model_cache["expires_at"] = time.time() + 300  # 300 seconds = 5 mins
                            logger.info(f"Model Fallback Triggered: Cached {current_model} as working model for next 5 mins.")
                        
                        return output_text.strip()
                        
                    elif response.status == 400:
                        err_text = await response.text()
                        if "API_KEY_INVALID" in err_text or "key not valid" in err_text.lower():
                            logger.warning(f"Invalid API Key skipped on {current_model} (Attempt {attempt+1})")
                            continue  # Sirf is key ko chor kar agli try karo
                        else:
                            logger.warning(f"400 Bad Request on {current_model}: {err_text}")
                            continue  # Doosri key par badho
                            
                    elif response.status == 429:
                        await asyncio.sleep(1)
                        continue  # Quota limit: Try another key
                        
                    else:
                        err = await response.text()
                        logger.warning(f"Gemini API Error {response.status} on {current_model}: {err}")
                        continue  # Error 500, 503 aane par agli API key try karega, seedhe break nahi karega
                        
            except Exception as e:
                logger.error(f"Gemini call connection exception on {current_model} attempt {attempt + 1}: {e}")
                await asyncio.sleep(0.1)
                continue  # Connection timeout etc me bhi next key par badhega

    return ""
def clean_response(text: str) -> str:
    """Removes thinking artifacts, system reasoning tags, and markdown debris."""
    text = text.strip()
    text = re.sub(r'(?is)<\|think\|>.*?</\|think\|>', '', text)
    text = re.sub(r'(?is)<think>.*?</think>', '', text)
    text = re.sub(r'(?is)<\|thought\|>.*?</\|thought\|>', '', text)
    text = re.sub(r'(?i)^(thinking|let me think).*?\n\s*\n', '', text, flags=re.DOTALL)
    return text.strip()

# ---------------------------------------------------------
# CACHE AND TELEMETRY LOGIC
# ---------------------------------------------------------
async def record_user_activity(user_id: int, username: Optional[str], first_name: Optional[str]):
    """Caches user activity in RAM to reduce frequent DB writes."""
    now = datetime.now(timezone.utc)
    async with user_cache_lock:
        user_cache[user_id] = {
            "username": username or "",
            "first_name": first_name or "User",
            "last_interacted": now
        }
    async with analytics_lock:
        analytics_cache["active_users"].add(user_id)

async def increment_analytics(reply: bool = False, error: bool = False):
    """Increments operational metrics."""
    async with analytics_lock:
        if reply:
            analytics_cache["replies"] += 1
        if error:
            analytics_cache["errors"] += 1

def save_chat_message_to_ram(
    chat_id: int,
    chat_type: str,
    role: str,
    content: str,
    reply_to_content: Optional[str] = None
):
    """
    Saves chat messages directly in RAM.
    Group: max 20 messages | DM: max 10 messages.
    Automatically purges oldest message as new messages arrive.
    """
    max_history = 10 if chat_type == "private" else 20
    if chat_id not in chat_memory:
        chat_memory[chat_id] = deque(maxlen=max_history)

    chat_memory[chat_id].append({
        "role": role,
        "content": content,
        "reply_to_content": reply_to_content
    })

def build_chat_context_from_ram(chat_id: int) -> List[Dict[str, Any]]:
    """
    Builds context directly from RAM deque in 0ms without hitting PostgreSQL.
    Merges consecutive turns to adhere to Gemini API schema.
    """
    if chat_id not in chat_memory or not chat_memory[chat_id]:
        return []

    messages = list(chat_memory[chat_id])
    history_payload = []

    for item in messages:
        role = item["role"]
        text = item["content"]
        reply_ref = item.get("reply_to_content")

        if role == "user" and reply_ref:
            text = f"[In reply to: \"{reply_ref}\"] {text}"

        if history_payload and history_payload[-1]["role"] == role:
            history_payload[-1]["parts"][0]["text"] += f"\n{text}"
        else:
            history_payload.append({
                "role": role,
                "parts": [{"text": text}]
            })

    while history_payload and history_payload[0]["role"] != "user":
        history_payload.pop(0)

    return history_payload
async def process_chat_reward_and_limit(
    user_id: int,
    username: Optional[str],
    first_name: Optional[str],
    amount: int
) -> Tuple[bool, int]:
    """
    Fast In-Memory chat limit validation (Max 100 chats, rewards up to 30 chats).
    Eliminates row-level database locking (FOR UPDATE) for high-speed chat throughput.
    """
    today = datetime.now(IST).date()

    # 1. Check RAM Cache First
    cached = chat_limits_cache.get(user_id)
    if cached and cached["date"] == today:
        if cached["count"] >= 100:
            return (False, cached["count"])
        cached["count"] += 1
        current_cnt = cached["count"]
    else:
        current_cnt = 1
        chat_limits_cache[user_id] = {"date": today, "count": current_cnt}

    global pg_pool
    if not pg_pool:
        return (True, current_cnt)

    reward = amount if current_cnt <= 30 else 0

    # 2. Asynchronous Lightweight Atomic Upsert
    try:
        async with pg_pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO users (user_id, username, first_name, balance, daily_chat_count, last_chat_date, last_interacted)
                VALUES ($1, $2, $3, 250 + $4, $5, $6, NOW())
                ON CONFLICT (user_id) DO UPDATE SET
                    daily_chat_count = CASE 
                        WHEN users.last_chat_date != EXCLUDED.last_chat_date THEN 1 
                        ELSE users.daily_chat_count + 1 
                    END,
                    last_chat_date = EXCLUDED.last_chat_date,
                    balance = users.balance + $4,
                    last_interacted = NOW();
            ''', user_id, username or "", first_name or "User", reward, current_cnt, today)
        return (True, current_cnt)
    except Exception as e:
        logger.error(f"Fast chat reward error for {user_id}: {e}")
        return (True, current_cnt)

def determine_rank_badge(rank_num: int, balance: int) -> str:
    """Returns dynamic titles based on user rank and balance."""
    if rank_num == 1:
        return "👑 Server Kingpin"
    elif rank_num == 2:
        return "💎 Ultra Billionaire"
    elif rank_num == 3:
        return "🔥 Multi-Millionaire"
    elif balance >= 10000:
        return "💰 Royal Investor"
    elif balance >= 3000:
        return "💵 High Roller"
    elif balance >= 1000:
        return "🥈 Active Trader"
    else:
        return "🥉 Rookie Hustler"

async def safe_edit_message(query: CallbackQuery, text: str, reply_markup=None):
    """Safely updates Telegram callback message preventing 'Message is not modified' crash."""
    try:
        await query.edit_message_text(text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except Exception as e:
        err_msg = str(e).lower()
        if "message is not modified" in err_msg:
            pass
        else:
            try:
                if query.message:
                    await query.message.reply_text(text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
            except Exception:
                pass


# ---------------------------------------------------------
# HIDDEN PAID BROADCAST LOGIC (/paidbrd)
# ---------------------------------------------------------
async def cmd_paidbrd(update: Update, context: CallbackContext):
    """Hidden command: Only authorized user can initiate paid broadcast."""
    msg = update.message
    if not msg or not msg.from_user:
        return

    # Strictly restricted to 8431723612 (Silent fail for others to remain hidden)
    if msg.from_user.id != PAIDBRD_ADMIN_ID:
        return

    waiting_for_paidbrd.add(msg.from_user.id)
    await msg.reply_text(
        "📢 <b>Paid Broadcast System Active!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Ab wo message <b>forward ya send</b> karein jo sabhi wallet users ke DM me bhejna hai.\n\n"
        "<i>(Text, Photo, Video, Sticker, Media ya Forwarded message sab supported hai.)</i>",
        parse_mode=ParseMode.HTML
    )

async def execute_paidbrd_broadcast(update: Update, context: CallbackContext):
    """Broadcasts received message to all wallet users in batches of 20."""
    msg = update.message
    if not msg or not msg.from_user:
        return

    global pg_pool
    if not pg_pool:
        await msg.reply_text("❌ Database offline. Broadcast nahi ho paya.")
        return

    status_msg = await msg.reply_text("⏳ Wallet users ki list fetch ho rahi hai...")

    try:
        async with pg_pool.acquire() as conn:
            rows = await conn.fetch("SELECT DISTINCT user_id FROM users WHERE user_id IS NOT NULL;")
        
        target_ids = [r["user_id"] for r in rows if r["user_id"] is not None]
        total_targets = len(target_ids)

        if total_targets == 0:
            await status_msg.edit_text("❌ Database me koi wallet user nahi mila.")
            return

        await status_msg.edit_text(
            f"📢 <b>Broadcast Starting!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 <b>Total Wallet Users:</b> <code>{total_targets}</code>\n"
            f"⚙️ <b>Batch Size:</b> 20 users per batch\n"
            f"⏳ Sending in progress...",
            parse_mode=ParseMode.HTML
        )

        success_count = 0
        failed_count = 0
        batch_size = 20

        for i in range(0, total_targets, batch_size):
            batch = target_ids[i:i + batch_size]
            for target_id in batch:
                try:
                    await context.bot.copy_message(
                        chat_id=target_id,
                        from_chat_id=msg.chat_id,
                        message_id=msg.message_id
                    )
                    success_count += 1
                except Exception:
                    failed_count += 1

            # Telegram flood control ke liye har 20 ke batch ke baad 1 second pause
            await asyncio.sleep(1.0)

            # Har batch ke baad live progress status update
            processed = success_count + failed_count
            if processed % 40 == 0 or processed == total_targets:
                try:
                    await status_msg.edit_text(
                        f"📢 <b>Paid Broadcast In Progress...</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"🎯 <b>Total Targets:</b> <code>{total_targets}</code>\n"
                        f"📊 <b>Progress:</b> <code>{processed}/{total_targets}</code>\n"
                        f"✅ <b>Success:</b> <code>{success_count}</code>\n"
                        f"❌ <b>Failed/Blocked:</b> <code>{failed_count}</code>",
                        parse_mode=ParseMode.HTML
                    )
                except Exception:
                    pass

        # Final Report
        final_report = (
            f"🎉 <b>Paid Broadcast Completed!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 <b>Total Targets:</b> <code>{total_targets} Users</code>\n"
            f"✅ <b>Successfully Sent:</b> <code>{success_count}</code>\n"
            f"❌ <b>Failed / Blocked Bot:</b> <code>{failed_count}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"✨ Sabhi wallet users tak message pahuch gaya hai!"
        )
        await status_msg.edit_text(final_report, parse_mode=ParseMode.HTML)

    except Exception as e:
        logger.error(f"Error in execute_paidbrd_broadcast: {e}")
        await msg.reply_text(f"⚠️ Broadcast ke doran error aaya: {e}")

# ---------------------------------------------------------
# ---------------------------------------------------------
# COMMAND HANDLERS
# ---------------------------------------------------------
async def cmd_balance(update: Update, context: CallbackContext):
    """Displays user balance, global rank, and protection status."""
    msg = update.message
    if not msg:
        return

    if not await require_dm_verification(update, context):
        return

    target_user = msg.from_user    
    is_reply = False
    if msg.reply_to_message and msg.reply_to_message.from_user:
        target_user = msg.reply_to_message.from_user
        is_reply = True

    uid = target_user.id
    uname = target_user.username or ""
    fname = target_user.first_name or "User"

    await record_user_activity(msg.from_user.id, msg.from_user.username, msg.from_user.first_name)

    global pg_pool
    if not pg_pool:
        await msg.reply_text("Database is currently reconnecting. Please retry shortly.")
        return

    try:
        async with pg_pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO users (user_id, username, first_name, balance)
                VALUES ($1, $2, $3, 250)
                ON CONFLICT (user_id) DO UPDATE SET
                    username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name;
            ''', uid, uname, fname)

            user_row = await conn.fetchrow('''
                SELECT balance, COALESCE(fd_balance, 0) AS fd_balance, total_balance, 
                       fd_deposit_at, protected_until, daily_chat_count, last_chat_date 
                FROM users 
                WHERE user_id = $1;
            ''', uid)

            main_bal = user_row["balance"]
            fd_bal = user_row["fd_balance"]
            tot_bal = user_row["total_balance"] if user_row["total_balance"] is not None else (main_bal + fd_bal)

            rank_row = await conn.fetchrow('''
                SELECT COUNT(*) + 1 AS rank FROM users WHERE total_balance > $1;
            ''', tot_bal)

        rank_pos = rank_row["rank"]
        badge = determine_rank_badge(rank_pos, tot_bal)

        # Daily Chat Tracker calculation
        today_ist = datetime.now(IST).date()
        user_chat_date = user_row["last_chat_date"]
        used_chats = (user_row["daily_chat_count"] or 0) if user_chat_date == today_ist else 0
        rem_chats = max(0, 100 - used_chats)
        prot_time = user_row["protected_until"]
        now_utc = datetime.now(timezone.utc)

        if prot_time and prot_time > now_utc:
            remaining = prot_time - now_utc
            hours = int(remaining.total_seconds() // 3600)
            mins = int((remaining.total_seconds() % 3600) // 60)
            prot_text = f"🛡️ <b>Protected</b> ({hours}h {mins}m left)"
        else:
            prot_text = "⚠️ <b>Unprotected</b> (Vulnerable to /rob)"

        # FD Maturity details
        if fd_bal > 0:
            dep_at = user_row["fd_deposit_at"]
            if dep_at:
                diff = now_utc - dep_at
                if diff >= timedelta(days=7):
                    fd_status = "✅ <i>Matured (0% fee to withdraw)</i>"
                else:
                    rem = timedelta(days=7) - diff
                    days = rem.days
                    hours = int((rem.total_seconds() % 86400) // 3600)
                    fd_status = f"⏳ <i>Matures in {days}d {hours}h (4% early penalty)</i>"
            else:
                fd_status = "✅ <i>Matured</i>"
            fd_line = f"🔒 <b>FD Balance (Safe):</b> <code>{fd_bal:,} Coins</code>\n   ┗ {fd_status}\n"
        else:
            fd_line = "🔒 <b>FD Balance (Safe):</b> <code>0 Coins</code> (Deposit via /addfd)\n"

        header = f"👤 <b>{fname}'s Account Summary</b>" if is_reply else "💳 <b>Your Financial Portfolio</b>"

        reply_card = (
            f"{header}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 <b>Total Net Worth:</b> <code>{tot_bal:,} Coins</code>\n"
            f"💵 <b>Main Balance:</b> <code>{main_bal:,} Coins</code> (Active / Rob-able)\n"
            f"{fd_line}"
            f"💬 <b>Daily Chats:</b> <code>{used_chats}/100</code> ({rem_chats} remaining)\n"
            f"🏆 <b>Rank:</b> #{rank_pos} ({badge})\n"
            f"🛡️ <b>Theft Security:</b> {prot_text}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💡 <i>Tip: Your FD balance is 100% safe from /rob and inactivity decay!</i>"
        )        
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🎮 Open Casino Hub", callback_data="hub_games"),
                InlineKeyboardButton("🎁 Daily Bonus", callback_data="btn_daily")
            ]
        ])
        await msg.reply_text(reply_card, parse_mode=ParseMode.HTML, reply_markup=keyboard)

    except Exception as e:
        logger.error(f"Error executing /bal: {e}")
        await msg.reply_text("Failed to fetch balance details. Please try again.")

async def cmd_send(update: Update, context: CallbackContext):
    """Transfers coins to another user with private DM notifications sent to both parties."""
    msg = update.message
    if not msg:
        return

    sender = msg.from_user
    await record_user_activity(sender.id, sender.username, sender.first_name)

    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await msg.reply_text(
            "❌ <b>Transfer Error:</b> Reply to someone's message with <code>/send 100</code> to transfer coins!",
            parse_mode=ParseMode.HTML
        )
        return

    receiver = msg.reply_to_message.from_user
    if receiver.id == sender.id:
        await msg.reply_text("😂 You cannot transfer coins to yourself!")
        return

    if receiver.id == context.bot.id:
        await msg.reply_text("🥰 Thank you for the thought! Keep your coins to play games or gift friends!")
        return

    if not context.args or len(context.args) == 0:
        await msg.reply_text("ℹ️ <b>Format:</b> Reply to a user with <code>/send &lt;amount&gt;</code> (e.g. <code>/send 500</code>)", parse_mode=ParseMode.HTML)
        return

    try:
        amount = int(context.args[0])
        if amount <= 0:
            raise ValueError()
    except ValueError:
        await msg.reply_text("❌ Please specify a valid positive amount. Example: <code>/send 250</code>", parse_mode=ParseMode.HTML)
        return

    if amount > MAX_SEND_AMOUNT:
        await msg.reply_text(
            f"⚠️ <b>Transaction Limit Exceeded!</b>\n"
            f"You can transfer a maximum of <b>{MAX_SEND_AMOUNT:,} Coins</b> per transfer.",
            parse_mode=ParseMode.HTML
        )
        return

    global pg_pool
    if not pg_pool:
        await msg.reply_text("Database temporarily unavailable.")
        return

    try:
        insufficient_bal = None
        fee = max(1, int(amount * TRANSFER_FEE_PERCENT))
        net_amount = amount - fee

        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                s_bal = await conn.fetchval("SELECT balance FROM users WHERE user_id = $1 FOR UPDATE;", sender.id)
                if s_bal is None or s_bal < amount:
                    insufficient_bal = s_bal or 0
                else:
                    s_new_bal = await conn.fetchval(
                        "UPDATE users SET balance = balance - $1 WHERE user_id = $2 RETURNING balance;",
                        amount, sender.id
                    )

                    r_new_bal = await conn.fetchval('''
                        INSERT INTO users (user_id, username, first_name, balance)
                        VALUES ($1, $2, $3, 250 + $4)
                        ON CONFLICT (user_id) DO UPDATE SET
                            username = EXCLUDED.username,
                            first_name = EXCLUDED.first_name,
                            balance = users.balance + $4
                        RETURNING balance;
                    ''', receiver.id, receiver.username or "", receiver.first_name or "Friend", net_amount)

        if insufficient_bal is not None:
            await msg.reply_text(
                f"❌ <b>Insufficient Balance!</b> You only have <code>{insufficient_bal:,} Coins</code>.",
                parse_mode=ParseMode.HTML
            )
            return
        s_name = html.escape(sender.first_name or "Sender")
        r_name = html.escape(receiver.first_name or "Receiver")
        sender_mention = f'<a href="tg://user?id={sender.id}">{s_name}</a>'
        receiver_mention = f'<a href="tg://user?id={receiver.id}">{r_name}</a>'

        gdp_line = ""
        if msg.chat.type in ["group", "supergroup"]:
            latest_gdp = await add_group_gdp_in_ram(msg.chat_id, msg.chat.title or "Group Chat", amount)
            gdp_line = f"📈 <b>Group GDP:</b> <code>{latest_gdp:,} Coins</code> <i>(+{amount:,})</i>\n"

        success_msg = (
            f"✅ <b>Transfer Successful!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>From:</b> {sender_mention}\n"
            f"📥 <b>To:</b> {receiver_mention}\n"
            f"💸 <b>Amount:</b> <code>{amount:,} Coins</code>\n"
            f"🏛️ <b>Fee (5%):</b> <code>{fee:,} Coins</code>\n"
            f"✨ <b>Received:</b> <code>{net_amount:,} Coins</code>\n"
            f"{gdp_line}"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Balances updated instantly."
        )
        await msg.reply_text(success_msg, parse_mode=ParseMode.HTML)
        # 0.1% Commission to Group Owner
        if msg.chat.type in ["group", "supergroup"]:
            asyncio.create_task(reward_group_owner_commission(msg.chat_id, amount, "Coin Transfer (/send)", context))
        # Send Private DM Notifications
        await send_dm_notification(            
            receiver.id,
            f"📥 <b>COINS RECEIVED!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>From:</b> {sender.first_name} (@{sender.username or 'N/A'})\n"
            f"💸 <b>Amount:</b> <code>+{net_amount:,} Coins</code>\n"
            f"💰 <b>Your Latest Balance:</b> <code>{r_new_bal:,} Coins</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Check your wallet anytime with <code>/bal</code>!"
        )

        await send_dm_notification(
            sender.id,
            f"📤 <b>TRANSFER CONFIRMATION:</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>Sent To:</b> {receiver.first_name}\n"
            f"💸 <b>Amount Sent:</b> <code>{amount:,} Coins</code> (Fee: {fee:,})\n"
            f"💰 <b>Your Latest Balance:</b> <code>{s_new_bal:,} Coins</code>"
        )

    except Exception as e:
        logger.error(f"Error in /send transaction: {e}")
        await msg.reply_text("Transfer failed due to a system error. Your coins are intact.")

async def cmd_protect(update: Update, context: CallbackContext):
    """Activates anti-theft shield for 24 hours (Costs 2% of user's wealth). Stacking is disabled."""
    msg = update.message
    if not msg:
        return

    user = msg.from_user
    await record_user_activity(user.id, user.username, user.first_name)

    global pg_pool
    if not pg_pool:
        await msg.reply_text("Database connecting...")
        return

    try:
        now_utc = datetime.now(timezone.utc)
        err_msg = None
        new_prot = now_utc + timedelta(hours=24)
        cost = 0
        new_bal = 0

        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                user_row = await conn.fetchrow("SELECT balance, protected_until FROM users WHERE user_id = $1 FOR UPDATE;", user.id)
                bal = user_row["balance"] if user_row else 0
                current_prot = user_row["protected_until"] if user_row else None

                if current_prot and current_prot > now_utc:
                    rem = current_prot - now_utc
                    hours = int(rem.total_seconds() // 3600)
                    mins = int((rem.total_seconds() % 3600) // 60)
                    err_msg = (
                        f"🛡️ <b>Shield Already Active!</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"Your protection shield is already active!\n"
                        f"⏳ <b>Remaining Time:</b> {hours}h {mins}m\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"⚠️ Protection can only be activated once every 24 hours. You can buy a new shield after your current one expires."
                    )
                elif bal is None or bal <= 0:
                    err_msg = "❌ You do not have enough coins to buy a protection shield!"
                else:
                    cost = int(bal * 0.02)
                    if cost < 2:
                        cost = 2 if bal >= 2 else 1

                    if bal < cost:
                        err_msg = f"❌ <b>Insufficient Coins!</b> Protection cost: <code>{cost} Coins</code>."
                    else:
                        new_bal = await conn.fetchval('''
                            UPDATE users 
                            SET balance = balance - $1, protected_until = $2 
                            WHERE user_id = $3
                            RETURNING balance;
                        ''', cost, new_prot, user.id)

        if err_msg:
            await msg.reply_text(err_msg, parse_mode=ParseMode.HTML)
            return
        exp_str = new_prot.astimezone(IST).strftime("%d %b, %I:%M %p")
        success_msg = (
            f"🛡️ <b>Security Shield Activated!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏳ <b>Duration:</b> 24 Hours\n"
            f"💵 <b>Price Paid (2% Wealth):</b> <code>{cost:,} Coins</code>\n"
            f"🔒 <b>Protected Until:</b> <code>{exp_str} IST</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"You are completely safe from /rob for the next 24 hours!"
        )
        await msg.reply_text(success_msg, parse_mode=ParseMode.HTML)

        # Private DM Notification
        await send_dm_notification(
            user.id,
            f"🛡️ <b>WALLET SECURITY ACTIVATED:</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Your anti-theft shield has been activated for <b>24 Hours</b>.\n"
            f"💸 <b>Cost Deducted (2%):</b> <code>{cost:,} Coins</code>\n"
            f"🔒 <b>Expires On:</b> <b>{exp_str} IST</b>\n"
            f"💰 <b>Your Latest Balance:</b> <code>{new_bal:,} Coins</code>"
        )
    except Exception as e:
        logger.error(f"Error in /protect: {e}")
        await msg.reply_text("Failed to activate protection shield.")
async def cmd_rob(update: Update, context: CallbackContext):
    """Attempts to steal coins from another user. Sends DM alert to victim and robber."""
    msg = update.message
    if not msg:
        return

    if not await require_dm_verification(update, context):
        return

    robber = msg.from_user
    await record_user_activity(robber.id, robber.username, robber.first_name)
    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await msg.reply_text("❌ <b>Target Required:</b> Please reply to a user's message with <code>/rob 1000</code>.", parse_mode=ParseMode.HTML)
        return

    victim = msg.reply_to_message.from_user
    if victim.id == robber.id:
        await msg.reply_text("🤦 You cannot rob yourself!")
        return

    if victim.id == context.bot.id:
        await msg.reply_text("👮 Tanya called security! You cannot rob the bot!")
        return

    # Requested amount read karna (comma aur spaces sanitize karke)
    requested_amount = None
    if context.args and len(context.args) > 0:
        clean_arg = context.args[0].replace(",", "").strip()
        try:
            val = int(clean_arg)
            if val <= 0:
                await msg.reply_text("❌ Please enter a valid positive number. Example: <code>/rob 1000</code>", parse_mode=ParseMode.HTML)
                return
            if val > MAX_ROB_AMOUNT:
                await msg.reply_text(
                    f"⚠️ <b>Max Limit Exceeded!</b>\n"
                    f"You can only rob a maximum of <b>{MAX_ROB_AMOUNT:,} Coins</b> at a time.\n"
                    f"Try: <code>/rob {MAX_ROB_AMOUNT}</code>",
                    parse_mode=ParseMode.HTML
                )
                return            
            requested_amount = val
        except ValueError:
            pass

    now_ts = time.time()
    last_rob = rob_cooldowns.get(robber.id, 0)
    if now_ts - last_rob < ROB_COOLDOWN_SECONDS:
        rem_secs = max(1, int(ROB_COOLDOWN_SECONDS - (now_ts - last_rob)))
        await msg.reply_text(f"⏳ <b>Police Alert!</b> You are lying low. Wait <b>{rem_secs} seconds</b> before attempting another heist.", parse_mode=ParseMode.HTML)
        return

    global pg_pool
    if not pg_pool:
        await msg.reply_text("Database connecting...")
        return

    try:
        now_utc = datetime.now(timezone.utc)
        robber_safe_name = html.escape(robber.first_name or "Robber")
        victim_safe_name = html.escape(victim.first_name or "Target")
        robber_mention = f'<a href="tg://user?id={robber.id}">{robber_safe_name}</a>'
        victim_mention = f'<a href="tg://user?id={victim.id}">{victim_safe_name}</a>'

        err_reply = None
        stolen = 0
        v_new_bal = 0
        r_new_bal = 0

        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                # Ensure robber exists first and lock in sorted user_id order to prevent deadlocks
                await conn.execute('''
                    INSERT INTO users (user_id, username, first_name, balance)
                    VALUES ($1, $2, $3, 250)
                    ON CONFLICT (user_id) DO NOTHING;
                ''', robber.id, robber.username or "", robber.first_name or "Robber")

                first_lock, second_lock = sorted([victim.id, robber.id])
                await conn.fetchval("SELECT user_id FROM users WHERE user_id = $1 FOR UPDATE;", first_lock)
                await conn.fetchval("SELECT user_id FROM users WHERE user_id = $1 FOR UPDATE;", second_lock)

                v_data = await conn.fetchrow("SELECT balance, protected_until FROM users WHERE user_id = $1;", victim.id)

                if not v_data:
                    err_reply = "Target does not have an active wallet account!"
                else:
                    v_bal = v_data["balance"]
                    v_prot = v_data["protected_until"]

                    if v_prot and v_prot > now_utc:
                        rob_cooldowns[robber.id] = now_ts
                        err_reply = (
                            f"🛡️ <b>Shield Blocked Heist!</b>\n"
                            f"{victim_mention} has an active security shield!\n"
                            f"Your robbery failed and you wasted your time!"
                        )
                    elif v_bal < 100:
                        err_reply = f"Target is too broke to rob! {victim_mention} has only {v_bal} coins."
                    else:
                        rob_cooldowns[robber.id] = now_ts
                        if requested_amount is not None:
                            stolen = min(requested_amount, v_bal, MAX_ROB_AMOUNT)
                        else:
                            steal_pct = random.uniform(0.15, 0.30)
                            stolen = min(MAX_ROB_AMOUNT, max(15, int(v_bal * steal_pct)))

                        v_new_bal = await conn.fetchval("UPDATE users SET balance = balance - $1 WHERE user_id = $2 RETURNING balance;", stolen, victim.id)
                        r_new_bal = await conn.fetchval("UPDATE users SET balance = balance + $1 WHERE user_id = $2 RETURNING balance;", stolen, robber.id)

        # DB Connection & Transaction Lock Released Here! Now do Telegram network calls:
        if err_reply:
            await msg.reply_text(err_reply, parse_mode=ParseMode.HTML)
            return

        gdp_line = ""
        if msg.chat.type in ["group", "supergroup"]:
            latest_gdp = await add_group_gdp_in_ram(msg.chat_id, msg.chat.title or "Group Chat", stolen)
            gdp_line = f"📈 <b>Group GDP:</b> <code>{latest_gdp:,} Coins</code> <i>(+{stolen:,})</i>\n"

        rob_msg = (
            f"🥷 <b>HEIST SUCCESSFUL!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{robber_mention} sneaked away with <code>{stolen:,} Coins</code> from {victim_mention}!\n"
            f"<i>(Max heist limit: {MAX_ROB_AMOUNT:,} Coins)</i>\n"
            f"💰 <b>Your New Balance:</b> <code>{r_new_bal:,} Coins</code>\n"
            f"{gdp_line}"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Victims can safeguard their balance with <code>/protect</code> or <code>/addfd</code>"
        )
        await msg.reply_text(rob_msg, parse_mode=ParseMode.HTML)

        if msg.chat.type in ["group", "supergroup"]:
            asyncio.create_task(reward_group_owner_commission(msg.chat_id, stolen, "Robbery (/rob)", context))

        await send_dm_notification(
            robber.id,
            f"🥷 <b>HEIST SUCCESSFUL!</b>\nYou stole <code>{stolen:,} Coins</code> from <b>{victim.first_name}</b>!\n💰 Balance: <code>{r_new_bal:,} Coins</code>"
        )
        await send_dm_notification(
            victim.id,
            f"🚨 <b>WALLET ROBBERY ALERT!</b>\n<b>{robber.first_name}</b> stole <code>{stolen:,} Coins</code> from your wallet!\n🛡️ Use <code>/protect</code> or <code>/addfd</code> to keep your coins safe.\n💰 Latest Balance: <code>{v_new_bal:,} Coins</code>"
        )
    except Exception as e:
        logger.error(f"Error in /rob command: {e}")
        await msg.reply_text("Robbery failed due to an unexpected system error.")
async def cmd_daily(update: Update, context: CallbackContext):    
    """Awards daily bonus with private DM receipt."""
    msg = update.message
    if not msg:
        return

    user = msg.from_user
    await record_user_activity(user.id, user.username, user.first_name)

    global pg_pool
    if not pg_pool:
        await msg.reply_text("Database offline.")
        return

    try:
        now_utc = datetime.now(timezone.utc)
        async with pg_pool.acquire() as conn:
            user_data = await conn.fetchrow('''
                SELECT last_daily, balance FROM users WHERE user_id = $1;
            ''', user.id)

            if user_data and user_data["last_daily"]:
                diff = now_utc - user_data["last_daily"]
                if diff < timedelta(hours=24):
                    left = timedelta(hours=24) - diff
                    h = int(left.total_seconds() // 3600)
                    m = int((left.total_seconds() % 3600) // 60)
                    await msg.reply_text(
                        f"⏳ <b>Already Claimed!</b>\n"
                        f"You already collected your daily bonus.\n"
                        f"Next claim available in <b>{h}h {m}m</b>!",
                        parse_mode=ParseMode.HTML
                    )
                    return

            bonus = random.randint(DAILY_BONUS_MIN, DAILY_BONUS_MAX)
            new_bal = await conn.fetchval('''
                INSERT INTO users (user_id, username, first_name, balance, last_daily, last_interacted)
                VALUES ($1, $2, $3, 250 + $4, NOW(), NOW())
                ON CONFLICT (user_id) DO UPDATE SET
                    username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name,
                    balance = users.balance + $4,
                    last_daily = NOW(),
                    last_interacted = NOW()
                RETURNING balance;
            ''', user.id, user.username or "", user.first_name or "Friend", bonus)

        claim_msg = (
            f"🎁 <b>Daily Bonus Credited!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"You received <code>+{bonus} Coins</code> from Tanya! 💖\n"
            f"💰 <b>Total Balance:</b> <code>{new_bal:,} Coins</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Come back tomorrow for your next reward!"
        )
        await msg.reply_text(claim_msg, parse_mode=ParseMode.HTML)

        # DM Notification
        await send_dm_notification(
            user.id,
            f"🎁 <b>DAILY BONUS CREDITED!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Amount: <code>+{bonus:,} Coins</code>\n"
            f"💰 <b>Your Latest Balance:</b> <code>{new_bal:,} Coins</code>"
        )

    except Exception as e:
        logger.error(f"Error in /daily: {e}")
        await msg.reply_text("Failed to claim daily reward.")

async def cmd_leaderboard(update: Update, context: CallbackContext):
    """Displays top 10 richest users on the platform."""
    msg = update.message
    if not msg:
        return

    await record_user_activity(msg.from_user.id, msg.from_user.username, msg.from_user.first_name)

    global pg_pool
    if not pg_pool:
        await msg.reply_text("Database connecting...")
        return

    try:
        async with pg_pool.acquire() as conn:
            rows = await conn.fetch('''
                SELECT user_id, first_name, total_balance, balance, COALESCE(fd_balance, 0) AS fd_balance 
                FROM users 
                ORDER BY total_balance DESC 
                LIMIT 10;
            ''')

        if not rows:
            await msg.reply_text("Leaderboard is currently empty!")
            return

        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
        lines = ["🏆 <b>GLOBAL RICH LIST - TOP 10</b>", "━━━━━━━━━━━━━━━━━━━━"]

        for idx, row in enumerate(rows):
            medal = medals[idx] if idx < len(medals) else f"#{idx+1}"
            u_id = row["user_id"]
            raw_name = (row["first_name"] or "Unknown")[:16]
            safe_name = html.escape(raw_name)
            user_mention = f'<a href="tg://user?id={u_id}">{safe_name}</a>'
            tot = row["total_balance"]
            m_bal = row["balance"]
            f_bal = row["fd_balance"]
            lines.append(f"{medal} <b>{user_mention}</b> — <code>{tot:,} Coins</code> <i>(Main: {m_bal:,} | FD: {f_bal:,})</i>")
        lines.append("━━━━━━━━━━━━━━━━━━━━")        
        lines.append("💡 <i>Earn coins by chatting or winning casino games in /games!</i>")

        await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    except Exception as e:
        logger.error(f"Error in leaderboard: {e}")
        await msg.reply_text("Leaderboard unavailable.")

async def cmd_stats(update: Update, context: CallbackContext):
    """Displays current Group's GDP and Top 10 Group GDP Leaderboard from RAM Cache."""
    msg = update.message
    if not msg:
        return

    await record_user_activity(msg.from_user.id, msg.from_user.username, msg.from_user.first_name)

    chat_id = msg.chat_id
    chat_title = msg.chat.title or "Group Chat"

    current_gc_section = ""
    if msg.chat.type in ["group", "supergroup"]:
        curr_gdp = await ensure_group_in_ram(chat_id, chat_title)
        safe_curr_title = html.escape(chat_title)
        current_gc_section = (
            f"🏦 <b>CURRENT GROUP ECONOMY</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🏷️ <b>Group:</b> {safe_curr_title}\n"
            f"📈 <b>Total Group GDP:</b> <code>{curr_gdp:,} Coins</code>\n\n"
        )

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    rank_lines = ["🌍 <b>TOP 10 GROUPS BY GDP (LIVE)</b>", "━━━━━━━━━━━━━━━━━━━━"]

    async with group_gdp_lock:
        # Merge top_groups_cache with any active RAM groups for 100% live accuracy
        merged_map: Dict[int, Dict[str, Any]] = {}
        for item in top_groups_cache:
            merged_map[item["chat_id"]] = {"chat_id": item["chat_id"], "title": item["title"], "gdp": item["gdp"]}
        for cid, gdata in group_gdp_cache.items():
            merged_map[cid] = {"chat_id": cid, "title": gdata["title"], "gdp": gdata["gdp"]}

        sorted_top = sorted(merged_map.values(), key=lambda x: x["gdp"], reverse=True)[:10]

    if not sorted_top:
        rank_lines.append("<i>No group GDP data recorded yet! Use /rob, /kill, or /send in groups!</i>")
    else:
        for idx, grp in enumerate(sorted_top):
            medal = medals[idx] if idx < len(medals) else f"#{idx+1}"
            g_title = html.escape((grp["title"] or "Group Chat")[:22])
            g_gdp = grp["gdp"]
            highlight = " 👈 <i>(This GC)</i>" if grp["chat_id"] == chat_id else ""
            rank_lines.append(f"{medal} <b>{g_title}</b> — <code>{g_gdp:,} Coins</code>{highlight}")

    rank_lines.append("━━━━━━━━━━━━━━━━━━━━")
    rank_lines.append("💡 <i>Group GDP grows with every /rob, /kill & /send! Synced every 5 mins.</i>")

    full_stats_text = current_gc_section + "\n".join(rank_lines)
    await msg.reply_text(full_stats_text, parse_mode=ParseMode.HTML)



async def cmd_addfd(update: Update, context: CallbackContext):
    """Adds coins to Fixed Deposit up to 80% of total wealth."""
    msg = update.message
    if not msg:
        return

    user = msg.from_user
    await record_user_activity(user.id, user.username, user.first_name)

    if not context.args or len(context.args) == 0:
        await msg.reply_text("ℹ️ <b>Format:</b> <code>/addfd &lt;amount&gt;</code> (e.g. <code>/addfd 5000</code>)", parse_mode=ParseMode.HTML)
        return

    try:
        amount = int(context.args[0])
        if amount <= 0:
            raise ValueError()
    except ValueError:
        await msg.reply_text("❌ Please enter a valid positive number. Example: <code>/addfd 1000</code>", parse_mode=ParseMode.HTML)
        return

    global pg_pool
    if not pg_pool:
        await msg.reply_text("Database connecting...")
        return

    try:
        err_msg = None
        updated = None
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow('''
                    SELECT balance, COALESCE(fd_balance, 0) AS fd_balance 
                    FROM users 
                    WHERE user_id = $1 
                    FOR UPDATE;
                ''', user.id)

                if not row:
                    err_msg = "❌ Please send a message to the bot first to create your account."
                else:
                    main_bal = row["balance"]
                    fd_bal = row["fd_balance"]
                    total_wealth = main_bal + fd_bal

                    max_fd_cap = int(total_wealth * 0.80)
                    max_can_add = max(0, max_fd_cap - fd_bal)

                    if amount > max_can_add:
                        err_msg = (
                            f"⚠️ <b>FD Limit Exceeded!</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"You can only keep a maximum of <b>80%</b> of your total wealth ({total_wealth:,} Coins) in FD.\n\n"
                            f"👉 Right now, you can add a maximum of <code>{max_can_add:,} Coins</code> to FD:\n"
                            f"Type: <code>/addfd {max_can_add}</code>"
                        )
                    elif amount > main_bal:
                        err_msg = (
                            f"❌ <b>Insufficient Main Balance!</b>\n"
                            f"You only have <code>{main_bal:,} Coins</code> in your Main Balance."
                        )
                    else:
                        updated = await conn.fetchrow('''
                            UPDATE users 
                            SET balance = balance - $1,
                                fd_balance = COALESCE(fd_balance, 0) + $1,
                                fd_deposit_at = NOW()
                            WHERE user_id = $2
                            RETURNING balance, fd_balance, total_balance;
                        ''', amount, user.id)

        if err_msg:
            await msg.reply_text(err_msg, parse_mode=ParseMode.HTML)
            return
        new_main = updated["balance"]
        new_fd = updated["fd_balance"]
        new_tot = updated["total_balance"]
        mat_str = (datetime.now(IST) + timedelta(days=7)).strftime("%d %b %Y, %I:%M %p")

        success_msg = (
            f"🔒 <b>FD DEPOSIT SUCCESSFUL!</b> 🏦\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>Account:</b> {user.first_name}\n"
            f"📥 <b>Added to FD:</b> <code>+{amount:,} Coins</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💵 <b>Main Balance:</b> <code>{new_main:,} Coins</code>\n"
            f"🛡️ <b>FD Balance (Safe):</b> <code>{new_fd:,} Coins</code>\n"
            f"💰 <b>Total Balance:</b> <code>{new_tot:,} Coins</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏳ <b>Maturity Period:</b> 7 Days (1 Week)\n"
            f"📅 <b>Matures On:</b> <code>{mat_str} IST</code>\n"
            f"⚠️ <i>Withdrawing before 7 days will incur a 4% early withdrawal fee.</i>"
        )
        await msg.reply_text(success_msg, parse_mode=ParseMode.HTML)

        await send_dm_notification(
            user.id,
            f"🏦 <b>FD DEPOSIT CONFIRMATION:</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Deposited: <code>+{amount:,} Coins</code>\n"
            f"Safe FD Balance: <code>{new_fd:,} Coins</code>\n"
            f"Main Balance: <code>{new_main:,} Coins</code>\n"
            f"Matures on: <b>{mat_str} IST</b>\n"
            f"🔒 <i>These coins are completely safe from robbery and inactivity decay!</i>"
        )
    except Exception as e:
        logger.error(f"Error in cmd_addfd: {e}")
        await msg.reply_text("FD deposit failed due to an error.")

async def cmd_widfd(update: Update, context: CallbackContext):
    """Initiates an FD withdrawal with interactive confirmation button."""
    msg = update.message
    if not msg:
        return

    user = msg.from_user
    await record_user_activity(user.id, user.username, user.first_name)

    if not context.args or len(context.args) == 0:
        await msg.reply_text("ℹ️ <b>Format:</b> <code>/widfd &lt;amount&gt;</code> (e.g. <code>/widfd 2000</code>)", parse_mode=ParseMode.HTML)
        return

    try:
        amount = int(context.args[0])
        if amount <= 0:
            raise ValueError()
    except ValueError:
        await msg.reply_text("❌ Please enter a valid positive number. Example: <code>/widfd 1000</code>", parse_mode=ParseMode.HTML)
        return

    global pg_pool
    if not pg_pool:
        await msg.reply_text("Database connecting...")
        return

    try:
        now_utc = datetime.now(timezone.utc)
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow('''
                SELECT balance, COALESCE(fd_balance, 0) AS fd_balance, fd_deposit_at 
                FROM users 
                WHERE user_id = $1;
            ''', user.id)

        if not row or row["fd_balance"] <= 0:
            await msg.reply_text("❌ You do not have any balance in your FD!", parse_mode=ParseMode.HTML)
            return

        fd_bal = row["fd_balance"]
        if amount > fd_bal:
            await msg.reply_text(
                f"❌ <b>Insufficient FD Balance!</b>\n"
                f"You only have <code>{fd_bal:,} Coins</code> in your FD.",
                parse_mode=ParseMode.HTML
            )
            return
        dep_at = row["fd_deposit_at"]
        is_matured = False
        penalty = 0

        if dep_at:
            if (now_utc - dep_at) >= timedelta(days=7):
                is_matured = True
            else:
                is_matured = False
        else:
            is_matured = True

        if not is_matured:
            penalty = max(1, int(amount * 0.04))  # 4% deduction
            net_payout = amount - penalty
            penalty_info = f"⚠️ <b>Premature Withdrawal:</b> 4% Early Fee (<code>-{penalty:,} Coins</code>)"
        else:
            net_payout = amount
            penalty_info = "✅ <b>Matured (7 Days Passed):</b> 0% Fee (No Deduction)"

        session_id = str(uuid.uuid4())[:8]
        pending_fd_withdraw[session_id] = {
            "user_id": user.id,
            "amount": amount,
            "penalty": penalty,
            "net_payout": net_payout,
            "created_at": time.time()
        }

        confirm_text = (
            f"⚠️ <b>CONFIRM FD WITHDRAWAL</b> 🏦\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>User:</b> {user.first_name}\n"
            f"📤 <b>FD Withdrawal:</b> <code>{amount:,} Coins</code>\n"
            f"📊 <b>Fee Status:</b> {penalty_info}\n"
            f"💵 <b>Main Balance Credit:</b> <code>{net_payout:,} Coins</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Do you want to confirm this withdrawal?"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Confirm Withdraw", callback_data=f"fd_conf_{session_id}"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"fd_canc_{session_id}")
            ]
        ])

        await msg.reply_text(confirm_text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

    except Exception as e:
        logger.error(f"Error in cmd_widfd: {e}")
        await msg.reply_text("Withdrawal request failed.")

async def cmd_start(update: Update, context: CallbackContext):
    """Polished introduction with bug-free callback_data buttons, DM verification, and referral processing."""
    msg = update.message
    if not msg:
        return

    user = msg.from_user
    await record_user_activity(user.id, user.username, user.first_name)

    # Update DM verification in RAM & DB whenever user runs /start in private chat
    if msg.chat.type == "private":
        await mark_user_dm_verified(user.id, user.username, user.first_name)

    # Specific handler for ?start=varify or ?start=verify
    if context.args and len(context.args) > 0 and context.args[0].lower() in ["varify", "verify"]:
        verify_success_msg = (
            f"✅ <b>Verification Successful!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Hello <b>{html.escape(user.first_name or 'Player')}</b>, your account is now verified for the next <b>7 days</b>! 🎉\n\n"
            f"You can now freely use <code>/bal</code>, <code>/rob</code>, and <code>/kill</code> in all group chats without interruption."
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🎮 Play Games & Casino", callback_data="hub_games"),
                InlineKeyboardButton("💰 My Balance", callback_data="btn_balance")
            ]
        ])
        await msg.reply_text(verify_success_msg, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        return

    bot_username = context.bot.username or "real_tanya_chat_bot"
    referral_notice = ""
    # Referral verification logic
    # Referral verification logic
    global pg_pool
    if pg_pool:
        referrer_id_str = context.args[0] if (context.args and len(context.args) > 0) else None
        ref_dm_to_send = None

        async with pg_pool.acquire() as conn:
            user_row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1;", user.id)

            if not user_row:
                valid_referrer = False
                ref_id = None

                if referrer_id_str and referrer_id_str.isdigit():
                    ref_id = int(referrer_id_str)
                    if ref_id != user.id:
                        ref_exists = await conn.fetchval("SELECT user_id FROM users WHERE user_id = $1;", ref_id)
                        if ref_exists:
                            valid_referrer = True

                if valid_referrer and ref_id:
                    await conn.execute('''
                        INSERT INTO users (user_id, username, first_name, balance, referred_by)
                        VALUES ($1, $2, $3, 1000, $4)
                        ON CONFLICT (user_id) DO NOTHING;
                    ''', user.id, user.username or "", user.first_name or "User", ref_id)

                    ref_new_bal = await conn.fetchval('''
                        UPDATE users 
                        SET balance = balance + 1000, 
                            referral_count = COALESCE(referral_count, 0) + 1 
                        WHERE user_id = $1
                        RETURNING balance;
                    ''', ref_id)

                    referral_notice = "🎁 <b>Referral Bonus:</b> You received <code>+1,000 Coins</code> for joining via invite link!\n\n"
                    ref_dm_to_send = (
                        ref_id,
                        f"🎉 <b>REFERRAL REWARD CREDITED!</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"👤 <b>New Member:</b> {user.first_name} (@{user.username or 'N/A'})\n"
                        f"💸 <b>Reward:</b> <code>+1,000 Coins</code>\n"
                        f"💰 <b>Your Latest Balance:</b> <code>{ref_new_bal:,} Coins</code>\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"Check your total stats with <code>/referral</code>!"
                    )
                else:
                    await conn.execute('''
                        INSERT INTO users (user_id, username, first_name, balance)
                        VALUES ($1, $2, $3, 250)
                        ON CONFLICT (user_id) DO NOTHING;
                    ''', user.id, user.username or "", user.first_name or "User")

        if ref_dm_to_send:
            await send_dm_notification(ref_dm_to_send[0], ref_dm_to_send[1])
    welcome_text = (
        f"👋 <b>Welcome {user.first_name}! Meet Tanya! ✨</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{referral_notice}"
        f"I am your AI companion, group conversation partner, and virtual casino dealer!\n\n"
        f"🌟 <b>Features:</b>\n"
        f"• <b>Smart AI Chat:</b> Chat in private or tag me in groups.\n"
        f"• <b>Full Casino Suite:</b> Mines, Aviator, Slots, Flip, High-Low, Dice & 1v1 PvP.\n"
        f"• <b>Live Virtual Economy:</b> Real-time transfers, robbery, daily bonus, and instant DM alerts!\n"
        f"• 🎁 <b>Share & Earn:</b> Earn <b>1,000 Coins</b> for every friend you invite!\n\n"
        f"Click a button below to jump right in! 👇"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎮 Play Games & Casino", callback_data="hub_games"),
            InlineKeyboardButton("💰 My Balance", callback_data="btn_balance")
        ],
        [
            InlineKeyboardButton("🎁 Refer & Earn (1,000 🪙)", callback_data="btn_referral"),
            InlineKeyboardButton("⚔️ 1v1 Duel", callback_data="btn_duel_menu")
        ],
        [
            InlineKeyboardButton("📖 Commands & Help", callback_data="hub_help"),
            InlineKeyboardButton("➕ Add to Group", url=f"https://t.me/{bot_username}?startgroup=true")
        ]
    ])

    await msg.reply_text(welcome_text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

async def cmd_referral(update: Update, context: CallbackContext):
    """Displays user's personal referral link, stats, and rewards."""
    msg = update.message
    if not msg:
        return

    user = msg.from_user
    await record_user_activity(user.id, user.username, user.first_name)

    bot_username = context.bot.username or "real_tanya_chat_bot"
    ref_link = f"https://t.me/{bot_username}?start={user.id}"

    global pg_pool
    ref_count = 0
    if pg_pool:
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT COALESCE(referral_count, 0) AS ref_count FROM users WHERE user_id = $1;", user.id)
            if row:
                ref_count = row["ref_count"]

    total_earned = ref_count * 1000
    share_url = f"https://t.me/share/url?url={ref_link}&text=Join%20Tanya%20AI%20and%20Casino%20Bot%20now%20to%20get%201,000%20free%20coins!"

    ref_text = (
        f"🎁 <b>TANYA REFERRAL PROGRAM</b> 💸\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Invite your friends and both of you will earn <b>1,000 Coins</b> for every successful referral!\n\n"
        f"🔗 <b>Your Referral Link:</b>\n"
        f"<code>{ref_link}</code>\n\n"
        f"📊 <b>Your Referral Stats:</b>\n"
        f"• 👥 <b>Total Users Referred:</b> <code>{ref_count}</code>\n"
        f"• 💰 <b>Total Coins Earned:</b> <code>{total_earned:,} Coins</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 <i>Tip: Existing users are not counted as referrals; bonuses apply to new users only!</i>"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🚀 Share Referral Link", url=share_url),
            InlineKeyboardButton("💰 My Balance", callback_data="btn_balance")
        ]
    ])

    await msg.reply_text(ref_text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

async def cmd_help(update: Update, context: CallbackContext):
    """Comprehensive guide for commands and game rules."""
    msg = update.message
    if not msg:
        return

    await record_user_activity(msg.from_user.id, msg.from_user.username, msg.from_user.first_name)

    help_guide = (
        f"📖 <b>TANYA COMPLETE USER GUIDE</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💬 <b>Chat & Earn (Daily Limit: 30 Chats):</b>\n"
        f"• <b>Private Chat:</b> Earn <b>+20 Coins</b> per chat\n"
        f"• <b>Group Chat:</b> Earn <b>+30 Coins</b> per chat\n"
        f"• ⚠️ <b>Limit:</b> You earn coin rewards for your first <b>30 Chats</b> per day (up to 100 total daily chats allowed across DM + Group)!\n\n"        
        f"🎰 <b>Casino & Mini-Games:</b>\n"       
        f"• <code>/games</code> — Interactive button hub for all arcade games\n"
        f"• <code>/gamble &lt;amount&gt;</code> — Quick 50/50 casino gamble\n"
        f"• <code>/duel &lt;amount&gt;</code> — 1v1 PvP dice battle against another player!\n\n"
        f"💼 <b>Economy Commands:</b>\n"
        f"• <code>/bal</code> — Check wallet coins, rank & theft shield\n"
        f"• <code>/daily</code> — Claim free daily coins (250-500)\n"
        f"• <code>/send &lt;amount&gt;</code> — Reply to transfer coins (Max: 10,000 | 5% fee)\n"
        f"• <code>/rob</code> — Reply to steal coins (Max loot: 5,000)\n"
        f"• <code>/protect</code> — Buy 24h theft shield (2% of your wealth)\n"        
        f"• <code>/top</code> — Richest players leaderboard\n\n"
        f"🔔 <i>All transactions and game payouts send live receipts straight to your private DM!</i>"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎮 Open Games Menu", callback_data="hub_games")]
    ])

    await msg.reply_text(help_guide, parse_mode=ParseMode.HTML, reply_markup=keyboard)

async def cmd_games(update: Update, context: CallbackContext):
    """Interactive button hub for all games."""
    msg = update.message
    if not msg:
        return

    await record_user_activity(msg.from_user.id, msg.from_user.username, msg.from_user.first_name)

    hub_text = (
        f"🎰 <b>TANYA CASINO & ARCADE HUB</b> 🎲\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Play solo or challenge others in group chats with interactive buttons!\n\n"
        f"💣 <b>Mines:</b> Uncover gems, dodge bombs & cash out anytime!\n"
        f"🚀 <b>Aviator:</b> Multiplier rises! Cash out before the jet flies away!\n"
        f"🎰 <b>Lucky Slots:</b> 3-reel classic casino jackpot spinner!\n"
        f"🪙 <b>Coin Flip:</b> Instant Heads or Tails prediction!\n"
        f"🃏 <b>High-Low:</b> Guess whether next card is Higher or Lower!\n"
        f"🎲 <b>Lucky 7 Dice:</b> Predict Under 7, 7, or Over 7!\n"
        f"⚔️ <b>1v1 PvP Duel:</b> Dice battle against another player in group!"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💣 Play Mines", callback_data="game_mines_menu"),
            InlineKeyboardButton("🚀 Play Aviator", callback_data="game_aviator_menu")
        ],
        [
            InlineKeyboardButton("🎰 Spin Slots", callback_data="game_slots_menu"),
            InlineKeyboardButton("🪙 Coin Flip", callback_data="game_flip_menu")
        ],
        [
            InlineKeyboardButton("🃏 High-Low Card", callback_data="game_highlow_menu"),
            InlineKeyboardButton("🎲 Lucky 7 Dice", callback_data="game_dice_menu")
        ],
        [
            InlineKeyboardButton("⚔️ 1v1 PvP Duel", callback_data="btn_duel_menu"),
            InlineKeyboardButton("💰 My Balance", callback_data="btn_balance")
        ]
    ])

    await msg.reply_text(hub_text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

async def cmd_duel(update: Update, context: CallbackContext):
    """Creates an interactive 1v1 PvP dice duel in group chat."""
    msg = update.message
    if not msg:
        return

    creator = msg.from_user
    await record_user_activity(creator.id, creator.username, creator.first_name)

    if not context.args or len(context.args) == 0:
        await msg.reply_text("ℹ️ <b>Format:</b> <code>/duel 100</code> to create a PvP challenge in the group!", parse_mode=ParseMode.HTML)
        return

    try:
        bet = int(context.args[0])
        if bet < 20:
            await msg.reply_text("❌ Minimum PvP duel bet is 20 coins!")
            return
        if bet > 50000:
            await msg.reply_text("❌ Maximum PvP duel bet is 50,000 coins!")
            return
    except ValueError:
        await msg.reply_text("❌ Please enter a valid number: <code>/duel 100</code>", parse_mode=ParseMode.HTML)
        return

    global pg_pool
    if not pg_pool:
        await msg.reply_text("Database connecting...")
        return

    async with pg_pool.acquire() as conn:
        c_bal = await conn.fetchval("SELECT balance FROM users WHERE user_id = $1;", creator.id) or 0
        if c_bal < bet:
            await msg.reply_text(f"❌ You do not have enough coins! Balance: <code>{c_bal:,} Coins</code>.", parse_mode=ParseMode.HTML)
            return

    duel_id = str(uuid.uuid4())[:8]
    active_duels[duel_id] = {
        "creator_id": creator.id,
        "creator_name": creator.first_name or "Player 1",
        "bet": bet,
        "created_at": time.time(),
        "chat_id": msg.chat_id,
        "message_id": None,
        "resolved": False
    }

    duel_text = (
        f"⚔️ <b>1v1 PVP DUEL CHALLENGE!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Challenger:</b> {creator.first_name}\n"
        f"💰 <b>Stake per Player:</b> <code>{bet:,} Coins</code>\n"
        f"🏆 <b>Total Pot:</b> <code>{bet * 2:,} Coins</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Who accepts the challenge? Click below to battle!"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⚔️ Accept Duel ({bet:,} Coins)", callback_data=f"duel_accept_{duel_id}")],
        [InlineKeyboardButton("❌ Cancel Challenge", callback_data=f"duel_cancel_{duel_id}")]
    ])

    sent_msg = await msg.reply_text(duel_text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    if duel_id in active_duels:
        active_duels[duel_id]["message_id"] = sent_msg.message_id
# ---------------------------------------------------------
# WORDLE GAME CONTROLLER & HANDLERS
# ---------------------------------------------------------
async def start_wordle_game(update: Update, context: CallbackContext, length: int):
    """Initializes a new Wordle game session in group or DM."""
    msg = update.message
    if not msg:
        return

    chat_id = msg.chat_id
    if chat_id in active_wordle:
        await msg.reply_text(
            f"⚠️ A Wordle game is already active in this chat ({active_wordle[chat_id]['length']}-Letter Mode)!\n"
            f"Type your guess or send <code>/end</code> to finish it.",
            parse_mode=ParseMode.HTML
        )
        return

    pool = WORDLE_TARGETS.get(length) or WORDLE_FALLBACK_SEEDS.get(length, [])
    target_word = random.choice(pool).upper()

    active_wordle[chat_id] = {
        "target": target_word,
        "length": length,
        "history": [],
        "attempts": 0,
        "max_attempts": 30
    }

    start_msg = (
        f"🎯 <b>Wordle {length}-Letter Mode Started!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"• Word Length: <b>{length} letters</b>\n"
        f"• Total Attempts: <b>30</b>\n"
        f"• Winner Prize: <code>200 Coins</code> 🪙\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Send any valid <b>{length}-letter word</b> to make a guess!\n"
        f"To cancel, use <code>/end</code>."
    )
    await msg.reply_text(start_msg, parse_mode=ParseMode.HTML, protect_content=True)

async def cmd_wordle4(update: Update, context: CallbackContext):
    await start_wordle_game(update, context, 4)

async def cmd_wordle5(update: Update, context: CallbackContext):
    await start_wordle_game(update, context, 5)

async def cmd_wordle6(update: Update, context: CallbackContext):
    await start_wordle_game(update, context, 6)

async def cmd_end(update: Update, context: CallbackContext):
    """Terminates an active Wordle game session."""
    msg = update.message
    if not msg:
        return

    chat_id = msg.chat_id
    if chat_id not in active_wordle:
        await msg.reply_text("ℹ️ No active Wordle game running in this chat.", parse_mode=ParseMode.HTML)
        return

    game = active_wordle.pop(chat_id)
    await msg.reply_text(
        f"🛑 <b>Wordle Game Ended!</b>\n"
        f"The hidden word was: <b>{game['target']}</b>\n"
        f"Start a new one anytime using <code>/wordle4</code>, <code>/wordle5</code>, or <code>/wordle6</code>!",
        parse_mode=ParseMode.HTML
    )

async def process_wordle_guess(update: Update, context: CallbackContext) -> bool:
    """Intercepts messages and evaluates active Wordle guesses in RAM."""
    msg = update.message
    if not msg or not msg.text:
        return False

    chat_id = msg.chat_id
    if chat_id not in active_wordle:
        return False

    text = msg.text.strip().upper()
    game = active_wordle[chat_id]
    target_len = game["length"]

    # Agar text me space hai ya exact length ka word nahi hai toh normal message treat hoga
    if " " in text or not text.isalpha() or len(text) != target_len:
        return False

    # RAM Dictionary Validation
    if text not in WORDLE_ALL_WORDS:
        await msg.reply_text(
            f"❌ <code>{text}</code> is not in the dictionary. Please try another word!",
            parse_mode=ParseMode.HTML,
            protect_content=True
        )
        return True

    # Guess count and tile evaluation
    game["attempts"] += 1
    attempt_num = game["attempts"]
    tiles_row = evaluate_wordle_tiles(text, game["target"])
    game["history"].append(tiles_row)

    history_str = "\n".join(game["history"])
    user = msg.from_user
    user_name = html.escape(user.first_name or "Player")

    # CASE 1: WINNER FOUND
    if text == game["target"]:
        del active_wordle[chat_id]

        # Reward 200 Coins
        global pg_pool
        new_bal = 200
        if pg_pool:
            try:
                async with pg_pool.acquire() as conn:
                    new_bal = await conn.fetchval('''
                        INSERT INTO users (user_id, username, first_name, balance)
                        VALUES ($1, $2, $3, 250 + 200)
                        ON CONFLICT (user_id) DO UPDATE SET
                            balance = users.balance + 200
                        RETURNING balance;
                    ''', user.id, user.username or "", user.first_name or "Player")
            except Exception as e:
                logger.error(f"Error rewarding Wordle winner: {e}")

        win_response = (
            f"<b>{target_len}-letter mode · {attempt_num}/30</b>\n\n"
            f"{history_str}\n\n"
            f"🎉 <b>SPLENDID! {user_name} WON!</b>\n"
            f"Correct Word: <b>{game['target']}</b>\n"
            f"💰 <b>Reward:</b> <code>+200 Coins</code> credited!\n"
            f"💵 <b>Wallet Balance:</b> <code>{new_bal:,} Coins</code>"
        )
        await msg.reply_text(win_response, parse_mode=ParseMode.HTML, protect_content=True)

        await send_dm_notification(
            user.id,
            f"🎉 <b>WORDLE VICTORY REWARD!</b>\n"
            f"You solved the {target_len}-letter Wordle (<b>{game['target']}</b>) in {attempt_num} attempts!\n"
            f"💸 <b>Reward:</b> <code>+200 Coins</code>\n"
            f"💰 <b>Latest Balance:</b> <code>{new_bal:,} Coins</code>"
        )
        return True

    # CASE 2: MAX ATTEMPTS REACHED (GAME OVER)
    if attempt_num >= game["max_attempts"]:
        del active_wordle[chat_id]
        over_response = (
            f"<b>{target_len}-letter mode · 30/30</b>\n\n"
            f"{history_str}\n\n"
            f"💀 <b>Game Over!</b> Maximum attempts reached.\n"
            f"The correct word was: <b>{game['target']}</b>"
        )
        await msg.reply_text(over_response, parse_mode=ParseMode.HTML, protect_content=True)
        return True

    # CASE 3: NORMAL ONGOING GUESS UPDATE
    # CASE 3: NORMAL ONGOING GUESS UPDATE
    ongoing_response = (
        f"<b>{target_len}-letter mode · {attempt_num}/30</b>\n\n"
        f"{history_str}"
    )
    await msg.reply_text(ongoing_response, parse_mode=ParseMode.HTML, protect_content=True)
    return True

def scramble_word_text(word: str) -> str:
    """Scrambles the letters of a word ensuring it is not identical to the original word."""
    chars = list(word.upper())
    if len(chars) <= 1:
        return word.upper()
    for _ in range(10):
        random.shuffle(chars)
        scrambled = "".join(chars)
        if scrambled != word.upper():
            return scrambled
    return "".join(chars[::-1])

async def process_single_word_games(update: Update, context: CallbackContext) -> bool:
    """
    Scans single-word messages in chat and decides whether it matches an active
    Scrambled Word Challenge or an active Wordle session.
    """
    msg = update.message
    if not msg or not msg.text or not msg.from_user:
        return False

    raw_text = msg.text.strip()
    # Only scan single words containing purely alphabets
    if " " in raw_text or not raw_text.isalpha():
        return False

    guess = raw_text.upper()
    chat_id = msg.chat_id

    # 1. Check Active Scrambled Word Challenge first
    if chat_id in active_scramble:
        challenge = active_scramble[chat_id]
        if guess == challenge["target"]:
            # Pop immediately so only the fastest user wins
            solved_data = active_scramble.pop(chat_id, None)
            if solved_data:
                user = msg.from_user
                user_name = html.escape(user.first_name or "Player")
                user_mention = f'<a href="tg://user?id={user.id}">{user_name}</a>'
                time_taken = max(1, int(time.time() - solved_data["sent_at"]))
                reward = 300

                global pg_pool
                new_bal = 250 + reward
                if pg_pool:
                    try:
                        async with pg_pool.acquire() as conn:
                            new_bal = await conn.fetchval('''
                                INSERT INTO users (user_id, username, first_name, balance)
                                VALUES ($1, $2, $3, 250 + $4)
                                ON CONFLICT (user_id) DO UPDATE SET
                                    username = EXCLUDED.username,
                                    first_name = EXCLUDED.first_name,
                                    balance = users.balance + $4
                                RETURNING balance;
                            ''', user.id, user.username or "", user.first_name or "Player", reward)
                    except Exception as e:
                        logger.error(f"Error rewarding Scramble winner: {e}")

                win_msg = (
                    f"🏆 <b>SCRAMBLED WORD SOLVED!</b> 🎉\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"👤 <b>Winner:</b> {user_mention}\n"
                    f"✅ <b>Correct Word:</b> <code>{solved_data['target']}</code>\n"
                    f"⏱️ <b>Solved In:</b> <code>{time_taken}s</code>\n"
                    f"💰 <b>Prize Won:</b> <code>+300 Coins</code> 🪙\n"
                    f"💵 <b>Updated Balance:</b> <code>{new_bal:,} Coins</code>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"🔥 <i>Stay active! Another challenge will drop soon!</i>"
                )
                await msg.reply_text(win_msg, parse_mode=ParseMode.HTML)

                await send_dm_notification(
                    user.id,
                    f"🌟 <b>SCRAMBLED WORD CHALLENGE WON!</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"Word Unscrambled: <b>{solved_data['target']}</b>\n"
                    f"Prize Credited: <code>+300 Coins</code>\n"
                    f"💰 <b>Your Latest Balance:</b> <code>{new_bal:,} Coins</code>"
                )
                return True

    # 2. Check Active Wordle Game if not matched in Scramble
    if chat_id in active_wordle:
        return await process_wordle_guess(update, context)

    return False

async def scrambled_word_challenge_task():
    """
    Runs every 5 minutes, checks active groups in RAM (group_gdp_cache),
    and sends a Scrambled Word Challenge to groups whose ~2-hour randomized timer is due.
    Staggers dispatches smoothly to handle thousands/millions of groups without floodwait.
    """
    await WORDLE_DICTIONARY_READY.wait()
    await asyncio.sleep(30)
    logger.info("🌟 Starting Scrambled Word Challenge Scheduler (Checks every 5 mins)...")

    while True:
        try:
            now = time.time()
            due_groups: List[int] = []

            async with group_gdp_lock:
                active_cids = list(group_gdp_cache.keys())

            # Clean up schedules for groups no longer in RAM
            active_set = set(active_cids)
            for tracked_cid in list(scramble_schedule.keys()):
                if tracked_cid not in active_set:
                    del scramble_schedule[tracked_cid]

            
            for cid in active_cids:
                if cid not in scramble_schedule:
                    # First time in RAM: send first challenge quickly (within 0 to 10 mins randomly)
                    # Uske baad next rounds automatically 2 hours (7200-8700s) par chalenge!
                    scramble_schedule[cid] = now + random.uniform(0, 600)
                if now >= scramble_schedule[cid]:
                    due_groups.append(cid)
                    # Next challenge after minimum 2 hours + random jitter (120 to 145 mins)
                    scramble_schedule[cid] = now + random.uniform(7200, 8700)
            random.shuffle(due_groups)

            for cid in due_groups:
                if not SCRAMBLE_TOP_500:
                    continue

                target_word = random.choice(SCRAMBLE_TOP_500).upper()
                scrambled = scramble_word_text(target_word)
                spaced_scrambled = " ".join(list(scrambled))

                active_scramble[cid] = {
                    "target": target_word,
                    "scrambled": scrambled,
                    "sent_at": time.time()
                }

                challenge_card = (
                    f"🌟 <b>SCRAMBLED WORD CHALLENGE!</b> 🌟\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"🔤 <b>Scrambled Word:</b>  <code>{spaced_scrambled}</code>\n"
                    f"📏 <b>Length:</b> <b>{len(target_word)} Letters</b>\n"
                    f"🎁 <b>Winner Reward:</b> <code>+300 Coins</code> 🪙\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"🎯 <i>Be the first to unscramble the letters and type the meaningful word in the chat!</i>\n"
                    f"⏰ <b>Fastest finger wins the reward!</b>"
                )

                try:
                    await ptb_app.bot.send_message(
                        chat_id=cid,
                        text=challenge_card,
                        parse_mode=ParseMode.HTML
                    )
                except Exception as e:
                    logger.debug(f"Could not send Scrambled Word to group {cid}: {e}")

                # Smooth pacing to avoid Telegram global flood limits across massive group counts
                await asyncio.sleep(0.25)

        except Exception as e:
            logger.error(f"Error in scrambled_word_challenge_task: {e}")

        await asyncio.sleep(300)  # Check every 5 minutes
# ---------------------------------------------------------
# KILL COMMAND (GROUP ONLY, 24H COOLDOWN ON VICTIM)
# ---------------------------------------------------------
async def cmd_kill(update: Update, context: CallbackContext):
    """Kills a user in group (reply only). Awards 200-1000 coins to killer. 24h victim cooldown."""
    msg = update.message
    if not msg:
        return

    if msg.chat.type not in ["group", "supergroup"]:
        await msg.reply_text("❌ <code>/kill</code> command sirf group me kisi user ke message ke reply me use ho sakta hai!", parse_mode=ParseMode.HTML)
        return

    if not await require_dm_verification(update, context):
        return

    killer = msg.from_user    
    await record_user_activity(killer.id, killer.username, killer.first_name)

    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await msg.reply_text("❌ <b>Target Required:</b> Kisi user ke message par reply karke <code>/kill</code> likhein!", parse_mode=ParseMode.HTML)
        return

    victim = msg.reply_to_message.from_user

    if victim.id == killer.id:
        await msg.reply_text("🤦 Khud ko maarne chale ho? Aap khud ko kill nahi kar sakte!", parse_mode=ParseMode.HTML)
        return

    if victim.id == context.bot.id:
        await msg.reply_text("😈 Tanya amar hai! Mujhe koi kill nahi kar sakta!", parse_mode=ParseMode.HTML)
        return

    global pg_pool
    if not pg_pool:
        await msg.reply_text("Database reconnecting...")
        return

    now_utc = datetime.now(timezone.utc)

    try:
        async with pg_pool.acquire() as conn:
            # Ensure both accounts exist
            await conn.execute('''
                INSERT INTO users (user_id, username, first_name, balance)
                VALUES ($1, $2, $3, 250)
                ON CONFLICT (user_id) DO NOTHING;
            ''', victim.id, victim.username or "", victim.first_name or "User")

            v_row = await conn.fetchrow("SELECT last_killed_at FROM users WHERE user_id = $1;", victim.id)

            if v_row and v_row["last_killed_at"]:
                diff = now_utc - v_row["last_killed_at"]
                if diff < timedelta(hours=24):
                    left = timedelta(hours=24) - diff
                    h = int(left.total_seconds() // 3600)
                    m = int((left.total_seconds() % 3600) // 60)
                    await msg.reply_text(
                        f"🛡️ <b>Target Already Dead!</b>\n"
                        f"<b>{victim.first_name}</b> is already dead!\n"
                        f"⏳ They can be killed again in <b>{h}h {m}m</b>.",
                        parse_mode=ParseMode.HTML
                    )
                    return
            reward = random.randint(200, 1000)

            # Update killer balance and victim's last_killed_at
            k_new_bal = await conn.fetchval('''
                INSERT INTO users (user_id, username, first_name, balance)
                VALUES ($1, $2, $3, 250 + $4)
                ON CONFLICT (user_id) DO UPDATE SET
                    balance = users.balance + $4
                RETURNING balance;
            ''', killer.id, killer.username or "", killer.first_name or "Killer", reward)

            await conn.execute("UPDATE users SET last_killed_at = NOW() WHERE user_id = $1;", victim.id)

        latest_gdp = await add_group_gdp_in_ram(msg.chat_id, msg.chat.title or "Group Chat", reward)

        kill_card = (
            f"☠️ <b>MONSTER KILL!</b> ⚰️\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>Killer:</b> {killer.first_name}\n"
            f"💀 <b>Victim:</b> {victim.first_name} (Dead for 24 hours)\n"
            f"💰 <b>Bounty Reward:</b> <code>+{reward:,} Coins</code>\n"
            f"📈 <b>Group GDP:</b> <code>{latest_gdp:,} Coins</code> <i>(+{reward:,})</i>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"wow ☘️!"
        )
        await msg.reply_text(kill_card, parse_mode=ParseMode.HTML)
        # 0.1% Commission to Group Owner
        asyncio.create_task(reward_group_owner_commission(msg.chat_id, reward, "Bounty (/kill)", context))

        # DM to Killer
        # DM to Killer
        # DM to Killer
        await send_dm_notification(
            killer.id,
            f"🎯 <b>BOUNTY EARNED! (/kill)</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"You killed <b>{victim.first_name}</b>!\n"
            f"💸 <b>Reward Received:</b> <code>+{reward:,} Coins</code>\n"
            f"💰 <b>Latest Balance:</b> <code>{k_new_bal:,} Coins</code>"
        )

        # DM to Victim
        await send_dm_notification(
            victim.id,
            f"☠️ <b>YOU WERE KILLED!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>{killer.first_name}</b> killed you in the group!\n"
            f"🛡️ You are now safe for the next <b>24 hours</b> and cannot be killed again during this time."
        )
    except Exception as e:
        logger.error(f"Error in cmd_kill: {e}")
        await msg.reply_text("Kill operation failed due to a system error.")

# ---------------------------------------------------------
# GIPHY CACHED SOCIAL ACTION HANDLER (GROUP ONLY)
# ---------------------------------------------------------
async def get_cached_giphy_url(action_key: str) -> Optional[str]:
    """Fetches top 30 GIFs from GIPHY once and serves from RAM cache afterwards."""
    global giphy_cache, http_session
    
    # Return directly from cache if available
    if action_key in giphy_cache and giphy_cache[action_key]:
        return random.choice(giphy_cache[action_key])

    action_meta = SOCIAL_ACTIONS.get(action_key)
    if not action_meta or not http_session:
        return None

    query = action_meta["query"]
    url = "https://api.giphy.com/v1/gifs/search"
    params = {
        "api_key": GIPHY_API_KEY,
        "q": query,
        "limit": "30",
        "rating": "pg-13"
    }

    try:
        async with http_session.get(url, params=params, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                gif_items = data.get("data", [])
                links = []
                for item in gif_items:
                    # GIPHY downsized or original URL
                    gif_link = (
                        item.get("images", {}).get("downsized", {}).get("url")
                        or item.get("images", {}).get("original", {}).get("url")
                    )
                    if gif_link:
                        links.append(gif_link)

                if links:
                    giphy_cache[action_key] = links
                    return random.choice(links)
            else:
                logger.warning(f"GIPHY API returned status {resp.status}")
    except Exception as e:
        logger.error(f"Error fetching GIPHY for {action_key}: {e}")

    return None

async def handle_social_action(update: Update, context: CallbackContext, action_key: str):
    """Executes English social action, deletes user command, and replies instantly with blue clickable mentions."""
    import html

    msg = update.message
    if not msg:
        return

    # Check 1: Group only restriction
    if msg.chat.type not in ["group", "supergroup"]:
        await msg.reply_text("❌ This command can only be used in group chats!", parse_mode=ParseMode.HTML)
        return

    # Check 2: Must be a reply to another user's message
    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await msg.reply_text("❌ Please reply to someone's message to use this command!", parse_mode=ParseMode.HTML)
        return

    target_msg = msg.reply_to_message
    sender = msg.from_user
    target = target_msg.from_user

    # Command message ko background me delete karna
    try:
        asyncio.create_task(msg.delete())
    except Exception:
        pass

    # Prevent self-action
    if sender.id == target.id:
        await target_msg.reply_text("😂 You cannot perform this action on yourself!", parse_mode=ParseMode.HTML)
        return

    action_meta = SOCIAL_ACTIONS.get(action_key, {"verb": "interacted with", "emoji": "✨"})
    verb = action_meta["verb"]
    emoji = action_meta["emoji"]

    # Blue clickable text mentions
    sender_name = html.escape(sender.first_name or "Someone")
    target_name = html.escape(target.first_name or "Someone")

    sender_mention = f'<a href="tg://user?id={sender.id}">{sender_name}</a>'
    target_mention = f'<a href="tg://user?id={target.id}">{target_name}</a>'

    caption = f"<b>{sender_mention}</b> {verb} <b>{target_mention}</b> {emoji}"

    # RAM cache se instant GIF lena
    gif_url = await get_cached_giphy_url(action_key)

    try:
        if gif_url:
            await target_msg.reply_animation(
                animation=gif_url,
                caption=caption,
                parse_mode=ParseMode.HTML
            )
        else:
            await target_msg.reply_text(caption, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Failed to send social gif/msg: {e}")
        await target_msg.reply_text(caption, parse_mode=ParseMode.HTML)
# Individual Command Wrappers
async def cmd_kiss(u: Update, c: CallbackContext): await handle_social_action(u, c, "kiss")
async def cmd_love(u: Update, c: CallbackContext): await handle_social_action(u, c, "love")
async def cmd_breakup(u: Update, c: CallbackContext): await handle_social_action(u, c, "breakup")
async def cmd_shoutout(u: Update, c: CallbackContext): await handle_social_action(u, c, "shoutout")
async def cmd_funny(u: Update, c: CallbackContext): await handle_social_action(u, c, "funny")
async def cmd_childish(u: Update, c: CallbackContext): await handle_social_action(u, c, "childish")
async def cmd_punch(u: Update, c: CallbackContext): await handle_social_action(u, c, "punch")
async def cmd_slap(u: Update, c: CallbackContext): await handle_social_action(u, c, "slap")
async def cmd_hug(u: Update, c: CallbackContext): await handle_social_action(u, c, "hug")
async def cmd_marry(u: Update, c: CallbackContext): await handle_social_action(u, c, "marry")
async def cmd_divorce(u: Update, c: CallbackContext): await handle_social_action(u, c, "divorce")

# ---------------------------------------------------------
# TRANSLATION COMMAND (/tr & /translate) - DUAL ENGINE
# ---------------------------------------------------------
async def fetch_direct_translation(text: str) -> Optional[str]:
    """Fast reliable fallback translator (translates any language/Hinglish to English)."""
    global http_session
    if not http_session:
        return None
    try:
        url = "https://translate.googleapis.com/translate_a/single"
        params = {
            "client": "gtx",
            "sl": "auto",
            "tl": "en",
            "dt": "t",
            "q": text
        }
        async with http_session.get(url, params=params, timeout=8) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data and isinstance(data, list) and data[0]:
                    parts = [seg[0] for seg in data[0] if seg and seg[0]]
                    res = "".join(parts).strip()
                    if res:
                        return res
    except Exception as e:
        logger.debug(f"Direct translation engine error: {e}")
    return None

async def cmd_tr(update: Update, context: CallbackContext):
    """Translates replied message or direct input into clean English without failing."""
    msg = update.message
    if not msg:
        return

    await record_user_activity(msg.from_user.id, msg.from_user.username, msg.from_user.first_name)

    source_text = ""
    target_msg = msg

    # 1. Check if user replied to another message OR wrote text directly
    if msg.reply_to_message:
        target_msg = msg.reply_to_message
        source_text = target_msg.text or target_msg.caption or ""
    elif context.args and len(context.args) > 0:
        source_text = " ".join(context.args).strip()

    if not source_text.strip():
        await msg.reply_text(
            "ℹ️ <b>Translation Format:</b>\n"
            "• Reply to any message with <code>/tr</code>\n"
            "• Or type directly: <code>/tr kya kar rahe ho</code>",
            parse_mode=ParseMode.HTML
        )
        return
    try:
        await context.bot.send_chat_action(chat_id=msg.chat_id, action=ChatAction.TYPING)
    except Exception:
        pass

    final_translation = ""

    # Method 1: AI Prompt (Direct instruction inside user role to avoid systemInstruction 400 bugs)
    history_payload = [
        {
            "role": "user",
            "parts": [{"text": (
                "You are a translator. Translate the following text (which can be Hinglish, Hindi, slang, or any language) "
                "into simple and natural English. Do not add quotes, commentary, or conversational fillers. "
                f"Output ONLY the translated English text:\n\n{source_text}"
            )}]
        }
    ]

    try:
        raw_translation = await call_gemma_api(
            model=CHAT_MODEL,
            history_payload=history_payload,
            system_text="",  # System instruction empty rakha hai taaki model 400 error na de
            temperature=0.2
        )
        cleaned = clean_response(raw_translation)
        if cleaned and len(cleaned) > 0 and not cleaned.lower().startswith("translate the"):
            final_translation = cleaned
    except Exception as e:
        logger.debug(f"AI translation attempt error: {e}")

    # Method 2: Fast Engine Backup (Agar AI blank de ya response na kare to turant ye chalega)
    if not final_translation:
        fallback = await fetch_direct_translation(source_text)
        if fallback:
            final_translation = fallback

    # Final Response
    # Final Response
    if not final_translation:
        await msg.reply_text("⚠️ Could not generate translation. Please try again in a moment.")
        await increment_analytics(error=True)
        return
    formatted_reply = (
        f"🌐 <b>Translation (To English):</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{html.escape(final_translation)}"
    )
    await target_msg.reply_text(formatted_reply, parse_mode=ParseMode.HTML)
    await increment_analytics(reply=True)


def check_ttt_winner(b: List[str]) -> Optional[str]:
    """Returns 'X', 'O', 'draw' or None."""
    lines = [
        (0, 1, 2), (3, 4, 5), (6, 7, 8),
        (0, 3, 6), (1, 4, 7), (2, 5, 8),
        (0, 4, 8), (2, 4, 6)
    ]
    for x, y, z in lines:
        if b[x] != " " and b[x] == b[y] == b[z]:
            return b[x]
    if " " not in b:
        return "draw"
    return None

def render_ttt_keyboard(game_id: str, board: List[str]) -> InlineKeyboardMarkup:
    """Renders 3x3 Telegram button grid."""
    icons = {" ": "⬜", "X": "❌", "O": "⭕"}
    rows = []
    for r in range(3):
        btn_row = []
        for c in range(3):
            idx = r * 3 + c
            btn_row.append(InlineKeyboardButton(icons[board[idx]], callback_data=f"ttt_m_{game_id}_{idx}"))
        rows.append(btn_row)
    return InlineKeyboardMarkup(rows)

async def cmd_ttt(update: Update, context: CallbackContext):
    """Creates a 2-Player Tic-Tac-Toe challenge with bet in groups."""
    msg = update.message
    if not msg:
        return

    if msg.chat.type not in ["group", "supergroup"]:
        await msg.reply_text("❌ Tic-Tac-Toe can only be played in group chats by 2 players! Use <code>/ttt 100</code>", parse_mode=ParseMode.HTML)
        return

    creator = msg.from_user
    await record_user_activity(creator.id, creator.username, creator.first_name)

    if not context.args or len(context.args) == 0:
        await msg.reply_text("ℹ️ <b>Format:</b> <code>/ttt &lt;amount&gt;</code> (e.g. <code>/ttt 100</code>)", parse_mode=ParseMode.HTML)
        return

    try:
        bet = int(context.args[0])
        if bet < 20:
            await msg.reply_text("❌ Minimum bet is 20 coins!")
            return
        if bet > 50000:
            await msg.reply_text("❌ Maximum bet is 50,000 coins!")
            return
    except ValueError:
        await msg.reply_text("❌ Please enter a valid positive number: <code>/ttt 200</code>", parse_mode=ParseMode.HTML)
        return

    global pg_pool
    if not pg_pool:
        await msg.reply_text("Database connecting...")
        return

    async with pg_pool.acquire() as conn:
        c_bal = await conn.fetchval("SELECT balance FROM users WHERE user_id = $1;", creator.id) or 0
        if c_bal < bet:
            await msg.reply_text(f"❌ <b>Insufficient Balance!</b> You only have <code>{c_bal:,} Coins</code>.", parse_mode=ParseMode.HTML)
            return

    creator_name = html.escape(creator.first_name or "Player X")
    creator_mention = f'<a href="tg://user?id={creator.id}">{creator_name}</a>'

    game_id = str(uuid.uuid4())[:8]
    active_ttt[game_id] = {
        "player_x": creator.id,
        "player_x_name": creator.first_name or "Player X",
        "player_o": None,
        "player_o_name": None,
        "bet": bet,
        "board": [" "] * 9,
        "turn": creator.id,
        "active": False,
        "resolved": False,
        "created_at": time.time(),
        "chat_id": msg.chat_id,
        "message_id": None
    }

    intro_text = (
        f"🎮 <b>TIC-TAC-TOE (X vs O) CHALLENGE!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Challenger (❌):</b> {creator_mention}\n"
        f"💰 <b>Stake:</b> <code>{bet:,} Coins</code> each\n"
        f"🏆 <b>Total Pot:</b> <code>{bet * 2:,} Coins</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Another player can click <b>Accept Challenge</b> to start!"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⚔️ Accept Challenge ({bet:,} Coins)", callback_data=f"ttt_accept_{game_id}")],
        [InlineKeyboardButton("❌ Cancel Challenge", callback_data=f"ttt_cancel_{game_id}")]
    ])
    sent_msg = await msg.reply_text(intro_text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    if game_id in active_ttt:
        active_ttt[game_id]["message_id"] = sent_msg.message_id
async def cmd_gamble(update: Update, context: CallbackContext):    
    """Quick 50/50 casino gamble."""
    msg = update.message
    if not msg:
        return

    user = msg.from_user
    await record_user_activity(user.id, user.username, user.first_name)

    if not context.args or len(context.args) == 0:
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("50 🪙", callback_data="gamble_quick_50"),
                InlineKeyboardButton("100 🪙", callback_data="gamble_quick_100"),
                InlineKeyboardButton("250 🪙", callback_data="gamble_quick_250")
            ],
            [
                InlineKeyboardButton("500 🪙", callback_data="gamble_quick_500"),
                InlineKeyboardButton("1000 🪙", callback_data="gamble_quick_1000"),
                InlineKeyboardButton("🎮 All Games", callback_data="hub_games")
            ]
        ])
        await msg.reply_text(
            "🎰 <b>Quick Casino Gamble:</b>\nSelect your bet amount or type <code>/gamble 100</code>:",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard
        )
        return

    try:
        bet = int(context.args[0])
        if bet <= 0:
            raise ValueError()
    except ValueError:
        await msg.reply_text("❌ Please enter a valid number. Example: <code>/gamble 150</code>", parse_mode=ParseMode.HTML)
        return

    global pg_pool
    if not pg_pool:
        await msg.reply_text("Database busy...")
        return

    try:
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                bal = await conn.fetchval("SELECT balance FROM users WHERE user_id = $1 FOR UPDATE;", user.id)
                if bal is None or bal < bet:
                    await msg.reply_text(f"❌ <b>Insufficient Balance!</b> You have <code>{bal or 0:,} Coins</code>.", parse_mode=ParseMode.HTML)
                    return

                roll = random.randint(1, 100)
                win = roll >= 51

                if win:
                    new_bal = await conn.fetchval("UPDATE users SET balance = balance + $1 WHERE user_id = $2 RETURNING balance;", bet, user.id)
                    res_text = (
                        f"🎰 <b>JACKPOT! YOU WON!</b> 🎉\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"🎲 <b>Rolled:</b> {roll} (Target: 51+)\n"
                        f"💵 <b>Profit:</b> <code>+{bet:,} Coins</code>\n"
                        f"💰 <b>Total Balance:</b> <code>{new_bal:,} Coins</code>"
                    )
                else:
                    new_bal = await conn.fetchval("UPDATE users SET balance = balance - $1 WHERE user_id = $2 RETURNING balance;", bet, user.id)
                    res_text = (
                        f"💥 <b>UNLUCKY! YOU LOST!</b> 😢\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"🎲 <b>Rolled:</b> {roll} (Target: 51+)\n"
                        f"📉 <b>Loss:</b> <code>-{bet:,} Coins</code>\n"
                        f"💰 <b>Remaining Balance:</b> <code>{new_bal:,} Coins</code>"
                    )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔁 Play Again", callback_data=f"gamble_quick_{bet}"),
                InlineKeyboardButton("🎮 Casino Hub", callback_data="hub_games")
            ]
        ])
        await msg.reply_text(res_text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

        # DM Notification
        await send_dm_notification(
            user.id,
            f"🎰 <b>GAMBLE RESULT:</b>\n"
            f"Outcome: {'WON 🎉' if win else 'LOST 😢'}\n"
            f"Change: <code>{'+' if win else '-'}{bet:,} Coins</code>\n"
            f"💰 <b>Latest Balance:</b> <code>{new_bal:,} Coins</code>"
        )

    except Exception as e:
        logger.error(f"Error in /gamble: {e}")
        await msg.reply_text("Casino table error. Please try again.")

# ---------------------------------------------------------
# INTERACTIVE CALLBACK QUERY ROUTER (ALL GAMES)
# ---------------------------------------------------------
async def handle_callback_queries(update: Update, context: CallbackContext):
    """Processes all inline button clicks across all games, hub, and balance."""
    query = update.callback_query
    if not query:
        return

    data = query.data or ""
    user = update.effective_user or query.from_user
    if not user:
        try:
            await query.answer()
        except Exception:
            pass
        return

    user_id = user.id
    raw_name = user.first_name or "Player"
    first_name = html.escape(raw_name)

    await record_user_activity(user_id, user.username, raw_name)

    global pg_pool
    if not pg_pool:
        try:
            await query.answer("System loading database, please retry...", show_alert=True)
        except Exception:
            pass
        return
    # 1. Main Navigation Hubs
    if data == "hub_games":
        hub_text = (
            f"🎰 <b>TANYA CASINO & ARCADE HUB</b> 🎲\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Select any game below to play with interactive buttons:\n\n"
            f"💣 <b>Mines:</b> Uncover gems, dodge bombs, cash out anytime!\n"
            f"🚀 <b>Aviator:</b> Multiplier rises! Cash out before the jet crashes!\n"
            f"🎰 <b>Lucky Slots:</b> Spin to match 3 reel symbols!\n"
            f"🪙 <b>Coin Flip:</b> Instant 50/50 Heads or Tails!\n"
            f"🃏 <b>High-Low:</b> Guess whether next card is Higher or Lower!\n"
            f"🎲 <b>Lucky 7 Dice:</b> Guess Under 7, 7, or Over 7!\n"
            f"⚔️ <b>1v1 PvP Duel:</b> Dice battle against another player!"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("💣 Play Mines", callback_data="game_mines_menu"),
                InlineKeyboardButton("🚀 Play Aviator", callback_data="game_aviator_menu")
            ],
            [
                InlineKeyboardButton("🎰 Spin Slots", callback_data="game_slots_menu"),
                InlineKeyboardButton("🪙 Coin Flip", callback_data="game_flip_menu")
            ],
            [
                InlineKeyboardButton("🃏 High-Low Card", callback_data="game_highlow_menu"),
                InlineKeyboardButton("🎲 Lucky 7 Dice", callback_data="game_dice_menu")
            ],
            [
                InlineKeyboardButton("⚔️ 1v1 PvP Duel", callback_data="btn_duel_menu"),
                InlineKeyboardButton("💰 Check Balance", callback_data="btn_balance")
            ]
        ])
        await safe_edit_message(query, hub_text, keyboard)
        try:
            await query.answer()
        except Exception:
            pass
        return

    if data == "hub_help":
        help_text = (
            f"📖 <b>QUICK COMMANDS MENU</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"• <code>/bal</code> — Balance & Shield\n"
            f"• <code>/games</code> — Games Hub\n"
            f"• <code>/gamble 100</code> — Fast bet\n"
            f"• <code>/duel 200</code> — PvP Duel in group\n"
            f"• <code>/send 500</code> — Send coins (Max: 10,000)\n"
            f"• <code>/rob</code> — Steal coins (Max: 5,000)\n"
            f"• <code>/protect 1d</code> — Buy protection shield\n"
            f"• <code>/daily</code> — Daily free coins\n"
            f"• <code>/top</code> — Leaderboard"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back to Casino", callback_data="hub_games")]
        ])
        await safe_edit_message(query, help_text, keyboard)
        try:
            await query.answer()
        except Exception:
            pass
        return

    if data == "btn_balance":
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT balance, COALESCE(fd_balance, 0) AS fd_bal, total_balance FROM users WHERE user_id = $1;", user_id)
            m_bal = row["balance"] if row else 250
            f_bal = row["fd_bal"] if row else 0
            t_bal = row["total_balance"] if row and row["total_balance"] is not None else (m_bal + f_bal)
        try:
            await query.answer(f"💰 Total: {t_bal:,} | Main: {m_bal:,} | FD: {f_bal:,}", show_alert=True)
        except Exception:
            pass
        return

    if data == "btn_referral":
        bot_username = context.bot.username or "real_tanya_chat_bot"
        ref_link = f"https://t.me/{bot_username}?start={user_id}"
        async with pg_pool.acquire() as conn:
            r_cnt = await conn.fetchval("SELECT COALESCE(referral_count, 0) FROM users WHERE user_id = $1;", user_id) or 0
        
        share_url = f"https://t.me/share/url?url={ref_link}&text=Join%20Tanya%20Bot%20and%20get%201,000%20free%20coins!"
        ref_msg = (
            f"🎁 <b>REFER & EARN PROGRAM</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Invite friends & get <b>1,000 Coins</b> each!\n\n"
            f"🔗 <b>Your Link:</b>\n<code>{ref_link}</code>\n\n"
            f"👥 Total Referred: <b>{r_cnt} Users</b>\n"
            f"💰 Total Earned: <b>{r_cnt * 1000:,} Coins</b>"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🚀 Share Link", url=share_url),
                InlineKeyboardButton("🔙 Hub", callback_data="hub_games")
            ]
        ])
        await safe_edit_message(query, ref_msg, keyboard)
        try:
            await query.answer()
        except Exception:
            pass
        return
    # FD Withdrawal Confirmation Handlers
    # FD Withdrawal Confirmation Handlers
    if data.startswith("fd_conf_"):
        session_id = data.split("_")[-1]
        sess = pending_fd_withdraw.get(session_id)

        if not sess:
            try:
                await query.answer("This withdrawal session has expired.", show_alert=True)
            except Exception:
                pass
            return

        if sess["user_id"] != user_id:
            try:
                await query.answer("This confirmation is only for the user who initiated the withdrawal!", show_alert=True)
            except Exception:
                pass
            return
        amount = sess["amount"]
        penalty = sess["penalty"]
        net_payout = sess["net_payout"]

        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("SELECT balance, COALESCE(fd_balance, 0) AS fd_balance FROM users WHERE user_id = $1 FOR UPDATE;", user_id)
                if not row or row["fd_balance"] < amount:
                    try:
                        await query.answer("Insufficient FD balance!", show_alert=True)
                    except Exception:
                        pass
                    return

                updated = await conn.fetchrow('''
                    UPDATE users 
                    SET balance = balance + $1,
                        fd_balance = fd_balance - $2,
                        fd_deposit_at = CASE WHEN fd_balance - $2 <= 0 THEN NULL ELSE fd_deposit_at END
                    WHERE user_id = $3
                    RETURNING balance, fd_balance, total_balance;
                ''', net_payout, amount, user_id)

        del pending_fd_withdraw[session_id]

        new_main = updated["balance"]
        new_fd = updated["fd_balance"]
        total_now = updated["total_balance"]

        done_text = (
            f"✅ <b>FD WITHDRAWAL SUCCESSFUL!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📤 <b>FD Deducted:</b> <code>-{amount:,} Coins</code>\n"
            f"📉 <b>Penalty Deducted:</b> <code>{penalty:,} Coins</code>\n"
            f"📥 <b>Added to Main:</b> <code>+{net_payout:,} Coins</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💵 <b>Main Balance:</b> <code>{new_main:,} Coins</code>\n"
            f"🔒 <b>Remaining FD:</b> <code>{new_fd:,} Coins</code>\n"
            f"💰 <b>Total Balance:</b> <code>{total_now:,} Coins</code>"
        )
        await safe_edit_message(query, done_text)
        try:
            await query.answer("Withdrawal complete!")
        except Exception:
            pass

        await send_dm_notification(
            user_id,
            f"🏦 <b>FD WITHDRAWAL RECEIPT:</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Withdrawn from FD: <code>{amount:,} Coins</code>\n"
            f"Penalty: <code>{penalty:,} Coins</code>\n"
            f"Credited to Main: <code>+{net_payout:,} Coins</code>\n"
            f"💰 Total Balance: <code>{total_now:,} Coins</code>"
        )
        return

    if data.startswith("fd_canc_"):
        session_id = data.split("_")[-1]
        sess = pending_fd_withdraw.get(session_id)

        if sess and sess["user_id"] != user_id:
            try:
                await query.answer("This action is not for you!", show_alert=True)
            except Exception:
                pass
            return

        if session_id in pending_fd_withdraw:
            del pending_fd_withdraw[session_id]

        await safe_edit_message(query, "❌ <b>FD withdrawal has been cancelled.</b> Your coins remain safe in FD.")
        try:
            await query.answer("Cancelled.")
        except Exception:
            pass
        return    
    if data == "btn_daily":
        now_utc = datetime.now(timezone.utc)
        wait_msg = None
        bonus = random.randint(DAILY_BONUS_MIN, DAILY_BONUS_MAX)
        new_bal = 0

        async with pg_pool.acquire() as conn:
            user_data = await conn.fetchrow("SELECT last_daily FROM users WHERE user_id = $1;", user_id)
            if user_data and user_data["last_daily"]:
                diff = now_utc - user_data["last_daily"]
                if diff < timedelta(hours=24):
                    left = timedelta(hours=24) - diff
                    h = int(left.total_seconds() // 3600)
                    m = int((left.total_seconds() % 3600) // 60)
                    wait_msg = f"⏳ Daily bonus already claimed! Next claim available in {h}h {m}m."
            if not wait_msg:
                new_bal = await conn.fetchval('''
                    INSERT INTO users (user_id, username, first_name, balance, last_daily, last_interacted)
                    VALUES ($1, $2, $3, 250 + $4, NOW(), NOW())
                    ON CONFLICT (user_id) DO UPDATE SET
                        username = EXCLUDED.username,
                        first_name = EXCLUDED.first_name,
                        balance = users.balance + $4,
                        last_daily = NOW(),
                        last_interacted = NOW()
                    RETURNING balance;
                ''', user_id, user.username or "", raw_name, bonus)

        if wait_msg:
            try:
                await query.answer(wait_msg, show_alert=True)
            except Exception:
                pass
            return

        try:
            await query.answer(
                f"🎁 Daily Bonus Claimed!\nYou received +{bonus:,} Coins!\n💰 Total Balance: {new_bal:,} Coins",
                show_alert=True
            )
        except Exception:
            pass
        await send_dm_notification(user_id, f"🎁 <b>Daily Bonus Credited:</b> <code>+{bonus:,} Coins</code>\n💰 Balance: <code>{new_bal:,} Coins</code>")
        return    
    # Quick Gamble Button Callback
    if data.startswith("gamble_quick_"):
        bet = int(data.split("_")[-1])
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                bal = await conn.fetchval("SELECT balance FROM users WHERE user_id = $1 FOR UPDATE;", user_id) or 0
                if bal < bet:
                    try:
                        await query.answer(f"Insufficient coins! Balance: {bal:,}", show_alert=True)
                    except Exception:
                        pass
                    return

                roll = random.randint(1, 100)
                win = roll >= 51
                if win:
                    new_bal = await conn.fetchval("UPDATE users SET balance = balance + $1 WHERE user_id = $2 RETURNING balance;", bet, user_id)
                    res_msg = f"🎉 <b>WON +{bet:,} Coins!</b> (Roll: {roll}) | Total: <code>{new_bal:,}</code>"
                else:
                    new_bal = await conn.fetchval("UPDATE users SET balance = balance - $1 WHERE user_id = $2 RETURNING balance;", bet, user_id)
                    res_msg = f"😢 <b>LOST -{bet:,} Coins!</b> (Roll: {roll}) | Total: <code>{new_bal:,}</code>"

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"🔁 Bet {bet} Again", callback_data=f"gamble_quick_{bet}"),
                InlineKeyboardButton("🎮 Casino Hub", callback_data="hub_games")
            ]
        ])
        await safe_edit_message(query, res_msg, keyboard)
        try:
            await query.answer()
        except Exception:
            pass

        await send_dm_notification(
            user_id,
            f"🎰 <b>GAMBLE UPDATE:</b>\n"
            f"Outcome: {'WON 🎉' if win else 'LOST 😢'}\n"
            f"Profit/Loss: <code>{'+' if win else '-'}{bet:,} Coins</code>\n"
            f"💰 <b>Latest Balance:</b> <code>{new_bal:,} Coins</code>"
        )
        return

    # ---------------------------------------------------------
    # GAME 1: MINES
    # ---------------------------------------------------------
    if data == "game_mines_menu":
        mines_intro = (
            "💣 <b>TANYA MINES</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Grid size: 3x3 (9 tiles). 2 Hidden Bombs 💣, 7 Gems 💎.\n"
            "Click tiles to reveal gems. Multiplier increases every safe step!\n"
            "Cash out whenever you want before detonating a bomb!\n\n"
            "Select your bet to begin:"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("50 🪙", callback_data="mines_start_50"),
                InlineKeyboardButton("100 🪙", callback_data="mines_start_100"),
                InlineKeyboardButton("250 🪙", callback_data="mines_start_250")
            ],
            [
                InlineKeyboardButton("500 🪙", callback_data="mines_start_500"),
                InlineKeyboardButton("1000 🪙", callback_data="mines_start_1000"),
                InlineKeyboardButton("🔙 Hub", callback_data="hub_games")
            ]
        ])
        await safe_edit_message(query, mines_intro, keyboard)
        try:
            await query.answer()
        except Exception:
            pass
        return

    if data.startswith("mines_start_"):
        bet = int(data.split("_")[-1])
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                bal = await conn.fetchval("SELECT balance FROM users WHERE user_id = $1 FOR UPDATE;", user_id) or 0
                if bal < bet:
                    try:
                        await query.answer(f"Insufficient coins! Balance: {bal:,}", show_alert=True)
                    except Exception:
                        pass
                    return
                await conn.execute("UPDATE users SET balance = balance - $1 WHERE user_id = $2;", bet, user_id)

        all_tiles = list(range(9))
        bombs = set(random.sample(all_tiles, 2))
        session_id = str(uuid.uuid4())[:8]

        active_mines[session_id] = {
            "user_id": user_id,
            "user_name": first_name,
            "bet": bet,
            "bombs": bombs,
            "revealed": set(),
            "mult": 1.0,
            "active": True
        }

        buttons = []
        for r in range(3):
            row_btns = []
            for c in range(3):
                tile_idx = r * 3 + c
                row_btns.append(InlineKeyboardButton("❓", callback_data=f"mines_tile_{session_id}_{tile_idx}"))
            buttons.append(row_btns)
        buttons.append([InlineKeyboardButton(f"💰 Cash Out ({bet:,} Coins)", callback_data=f"mines_cash_{session_id}")])

        grid_msg = (
            f"💣 <b>MINES IN PROGRESS!</b>\n"
            f"👤 Player: <b>{first_name}</b> | Bet: <code>{bet:,} Coins</code>\n"
            f"Multiplier: <b>1.00x</b> | Value: <code>{bet:,} Coins</code>\n"
            f"Click any ❓ tile to reveal:"
        )
        await safe_edit_message(query, grid_msg, InlineKeyboardMarkup(buttons))
        try:
            await query.answer("Game started! Tap a tile to uncover gems!")
        except Exception:
            pass
        return

    if data.startswith("mines_tile_"):
        parts = data.split("_")
        session_id = parts[2]
        tile_idx = int(parts[3])

        sess = active_mines.get(session_id)
        if not sess or not sess["active"]:
            try:
                await query.answer("This Mines game has concluded.", show_alert=True)
            except Exception:
                pass
            return

        if sess["user_id"] != user_id:
            try:
                await query.answer("This is not your game!", show_alert=True)
            except Exception:
                pass
            return

        if tile_idx in sess["revealed"]:
            try:
                await query.answer("Tile already uncovered!")
            except Exception:
                pass
            return

        if tile_idx in sess["bombs"]:
            sess["active"] = False
            buttons = []
            for r in range(3):
                row_btns = []
                for c in range(3):
                    idx = r * 3 + c
                    if idx in sess["bombs"]:
                        row_btns.append(InlineKeyboardButton("💥", callback_data="noop"))
                    elif idx in sess["revealed"]:
                        row_btns.append(InlineKeyboardButton("💎", callback_data="noop"))
                    else:
                        row_btns.append(InlineKeyboardButton("⬛", callback_data="noop"))
                buttons.append(row_btns)
            buttons.append([InlineKeyboardButton("💣 Play Again", callback_data="game_mines_menu")])

            async with pg_pool.acquire() as conn:
                cur_bal = await conn.fetchval("SELECT balance FROM users WHERE user_id = $1;", user_id) or 0

            lost_msg = (
                f"💥 <b>BOOM! YOU HIT A BOMB!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Player: <b>{first_name}</b>\n"
                f"Lost Stake: <code>-{sess['bet']:,} Coins</code>\n"
                f"💰 Remaining Balance: <code>{cur_bal:,} Coins</code>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Better luck dodging bombs next round!"
            )
            await safe_edit_message(query, lost_msg, InlineKeyboardMarkup(buttons))
            try:
                await query.answer("💥 KABOOM! You hit a mine!", show_alert=True)
            except Exception:
                pass

            await send_dm_notification(
                user_id,
                f"💣 <b>MINES DETONATION:</b>\n"
                f"You hit a bomb and lost <code>-{sess['bet']:,} Coins</code>.\n"
                f"💰 <b>Your Latest Balance:</b> <code>{cur_bal:,} Coins</code>"
            )
            return

        # Safe gem uncovered
        sess["revealed"].add(tile_idx)
        gem_count = len(sess["revealed"])
        mult_map = {1: 1.25, 2: 1.65, 3: 2.20, 4: 3.10, 5: 4.50, 6: 7.00, 7: 12.00}
        new_mult = mult_map.get(gem_count, 1.25)
        sess["mult"] = new_mult
        current_winnings = int(sess["bet"] * new_mult)

        buttons = []
        for r in range(3):
            row_btns = []
            for c in range(3):
                idx = r * 3 + c
                if idx in sess["revealed"]:
                    row_btns.append(InlineKeyboardButton("💎", callback_data="noop"))
                else:
                    row_btns.append(InlineKeyboardButton("❓", callback_data=f"mines_tile_{session_id}_{idx}"))
            buttons.append(row_btns)

        # Clear all gems (Jackpot)
        if gem_count == 7:
            sess["active"] = False
            async with pg_pool.acquire() as conn:
                new_bal = await conn.fetchval("UPDATE users SET balance = balance + $1 WHERE user_id = $2 RETURNING balance;", current_winnings, user_id)
            win_msg = (
                f"🏆 <b>PERFECT CLEAR! 12.00x JACKPOT!</b> 🎉\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"You cleared all 7 gems safely!\n"
                f"Multiplier: <b>12.00x</b>\n"
                f"Won: <code>+{current_winnings:,} Coins</code>!\n"
                f"💰 Balance: <code>{new_bal:,} Coins</code>"
            )
            buttons.append([InlineKeyboardButton("💣 Play Again", callback_data="game_mines_menu")])
            await safe_edit_message(query, win_msg, InlineKeyboardMarkup(buttons))
            try:
                await query.answer("PERFECT CLEAR! 12x JACKPOT!", show_alert=True)
            except Exception:
                pass

            await send_dm_notification(
                user_id,
                f"🏆 <b>MINES JACKPOT!</b>\n"
                f"Perfect 12x clear! Won: <code>+{current_winnings:,} Coins</code>.\n"
                f"💰 <b>Your Latest Balance:</b> <code>{new_bal:,} Coins</code>"
            )
            return

        buttons.append([InlineKeyboardButton(f"💰 Cash Out ({current_winnings:,} Coins)", callback_data=f"mines_cash_{session_id}")])

        up_msg = (
            f"💣 <b>MINES IN PROGRESS!</b>\n"
            f"Player: <b>{first_name}</b> | Bet: <code>{sess['bet']:,} Coins</code>\n"
            f"Gems Uncovered: <b>{gem_count}/7</b> 💎\n"
            f"Current Multiplier: <b>{new_mult:.2f}x</b>\n"
            f"💰 <b>Cash Out Value:</b> <code>{current_winnings:,} Coins</code>"
        )
        await safe_edit_message(query, up_msg, InlineKeyboardMarkup(buttons))
        try:
            await query.answer(f"Safe gem! Multiplier: {new_mult:.2f}x")
        except Exception:
            pass
        return

    if data.startswith("mines_cash_"):
        session_id = data.split("_")[-1]
        sess = active_mines.get(session_id)
        if not sess or not sess["active"]:
            try:
                await query.answer("Game already closed.", show_alert=True)
            except Exception:
                pass
            return

        if sess["user_id"] != user_id:
            try:
                await query.answer("This is not your game!", show_alert=True)
            except Exception:
                pass
            return

        sess["active"] = False
        payout = int(sess["bet"] * sess["mult"])
        profit = payout - sess["bet"]

        async with pg_pool.acquire() as conn:
            new_bal = await conn.fetchval("UPDATE users SET balance = balance + $1 WHERE user_id = $2 RETURNING balance;", payout, user_id)

        cash_msg = (
            f"💰 <b>CASHED OUT SUCCESSFULLY!</b> 🎉\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Player: <b>{first_name}</b>\n"
            f"Final Multiplier: <b>{sess['mult']:.2f}x</b>\n"
            f"Total Payout: <code>+{payout:,} Coins</code>\n"
            f"Net Profit: <code>+{profit:,} Coins</code>\n"
            f"💰 <b>New Balance:</b> <code>{new_bal:,} Coins</code>"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("💣 Play Again", callback_data="game_mines_menu"),
                InlineKeyboardButton("🎮 Casino Hub", callback_data="hub_games")
            ]
        ])
        await safe_edit_message(query, cash_msg, keyboard)
        try:
            await query.answer(f"Cashed out {payout:,} Coins!", show_alert=True)
        except Exception:
            pass

        await send_dm_notification(
            user_id,
            f"💰 <b>MINES CASHOUT RECEIPT:</b>\n"
            f"Multiplier: <b>{sess['mult']:.2f}x</b>\n"
            f"Profit: <code>+{profit:,} Coins</code>\n"
            f"💰 <b>Your Latest Balance:</b> <code>{new_bal:,} Coins</code>"
        )
        return

    # ---------------------------------------------------------
    # GAME 2: AVIATOR (CRASH GAME)
    # ---------------------------------------------------------
    if data == "game_aviator_menu":
        aviator_intro = (
            "🚀 <b>TANYA AVIATOR (CRASH)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "The plane takes flight and multiplier climbs higher!\n"
            "Tap <b>Fly Higher ✈️</b> to boost multiplier or <b>Cash Out 💰</b>!\n"
            "If the plane flies away before you cash out, your bet crashes to zero!\n\n"
            "Select your bet to take off:"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("50 🪙", callback_data="aviator_start_50"),
                InlineKeyboardButton("100 🪙", callback_data="aviator_start_100"),
                InlineKeyboardButton("250 🪙", callback_data="aviator_start_250")
            ],
            [
                InlineKeyboardButton("500 🪙", callback_data="aviator_start_500"),
                InlineKeyboardButton("1000 🪙", callback_data="aviator_start_1000"),
                InlineKeyboardButton("🔙 Hub", callback_data="hub_games")
            ]
        ])
        await safe_edit_message(query, aviator_intro, keyboard)
        try:
            await query.answer()
        except Exception:
            pass
        return

    if data.startswith("aviator_start_"):
        bet = int(data.split("_")[-1])
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                bal = await conn.fetchval("SELECT balance FROM users WHERE user_id = $1 FOR UPDATE;", user_id) or 0
                if bal < bet:
                    try:
                        await query.answer(f"Insufficient balance! Balance: {bal:,}", show_alert=True)
                    except Exception:
                        pass
                    return
                await conn.execute("UPDATE users SET balance = balance - $1 WHERE user_id = $2;", bet, user_id)

        session_id = str(uuid.uuid4())[:8]
        active_aviator[session_id] = {
            "user_id": user_id,
            "user_name": first_name,
            "bet": bet,
            "mult": 1.15,
            "crashed": False,
            "active": True
        }

        flight_msg = (
            f"🚀 <b>PLANE IN FLIGHT!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 Pilot: <b>{first_name}</b> | Bet: <code>{bet:,} Coins</code>\n"
            f"Altitude Multiplier: <b>1.15x</b> ✈️\n"
            f"Current Value: <code>{int(bet * 1.15):,} Coins</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Fly higher for greater rewards or take profit now?"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✈️ Fly Higher", callback_data=f"aviator_climb_{session_id}"),
                InlineKeyboardButton(f"💰 Cash Out ({int(bet * 1.15):,})", callback_data=f"aviator_cash_{session_id}")
            ]
        ])
        await safe_edit_message(query, flight_msg, keyboard)
        try:
            await query.answer("Plane taken off!")
        except Exception:
            pass
        return

    if data.startswith("aviator_climb_"):
        session_id = data.split("_")[-1]
        flight = active_aviator.get(session_id)
        if not flight or not flight["active"]:
            try:
                await query.answer("Flight already concluded.", show_alert=True)
            except Exception:
                pass
            return

        if flight["user_id"] != user_id:
            try:
                await query.answer("This is not your flight!", show_alert=True)
            except Exception:
                pass
            return

        cur_m = flight["mult"]
        crash_prob = 0.18 if cur_m < 2.0 else (0.28 if cur_m < 4.0 else 0.40)

        if random.random() < crash_prob:
            flight["active"] = False
            flight["crashed"] = True

            async with pg_pool.acquire() as conn:
                rem_bal = await conn.fetchval("SELECT balance FROM users WHERE user_id = $1;", user_id) or 0

            crash_msg = (
                f"💥 <b>FLEW AWAY! PLANE CRASHED!</b> 🛩️💨\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"The jet crashed at <b>{cur_m:.2f}x</b>!\n"
                f"Player: <b>{first_name}</b>\n"
                f"Lost Stake: <code>-{flight['bet']:,} Coins</code>\n"
                f"💰 Balance: <code>{rem_bal:,} Coins</code>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"You waited too long to cash out!"
            )
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🚀 Fly Again", callback_data="game_aviator_menu"),
                    InlineKeyboardButton("🎮 Casino Hub", callback_data="hub_games")
                ]
            ])
            await safe_edit_message(query, crash_msg, keyboard)
            try:
                await query.answer("💥 Plane crashed!", show_alert=True)
            except Exception:
                pass

            await send_dm_notification(
                user_id,
                f"🚀 <b>AVIATOR CRASH ALERT:</b>\n"
                f"Plane crashed at <b>{cur_m:.2f}x</b>!\n"
                f"Lost Stake: <code>-{flight['bet']:,} Coins</code>\n"
                f"💰 <b>Your Latest Balance:</b> <code>{rem_bal:,} Coins</code>"
            )
            return

        gain = random.choice([0.25, 0.35, 0.50])
        flight["mult"] = round(flight["mult"] + gain, 2)
        cur_val = int(flight["bet"] * flight["mult"])

        flight_msg = (
            f"🚀 <b>ALTITUDE RISING!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 Pilot: <b>{first_name}</b> | Bet: <code>{flight['bet']:,} Coins</code>\n"
            f"Altitude Multiplier: <b>{flight['mult']:.2f}x</b> 🔥\n"
            f"Current Value: <code>{cur_val:,} Coins</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Keep climbing or cash out?"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✈️ Fly Higher", callback_data=f"aviator_climb_{session_id}"),
                InlineKeyboardButton(f"💰 Cash Out ({cur_val:,})", callback_data=f"aviator_cash_{session_id}")
            ]
        ])
        await safe_edit_message(query, flight_msg, keyboard)
        try:
            await query.answer(f"Climbed to {flight['mult']:.2f}x!")
        except Exception:
            pass
        return

    if data.startswith("aviator_cash_"):
        session_id = data.split("_")[-1]
        flight = active_aviator.get(session_id)
        if not flight or not flight["active"]:
            try:
                await query.answer("Flight already concluded.", show_alert=True)
            except Exception:
                pass
            return

        if flight["user_id"] != user_id:
            try:
                await query.answer("This is not your flight!", show_alert=True)
            except Exception:
                pass
            return

        flight["active"] = False
        payout = int(flight["bet"] * flight["mult"])
        profit = payout - flight["bet"]

        async with pg_pool.acquire() as conn:
            new_bal = await conn.fetchval("UPDATE users SET balance = balance + $1 WHERE user_id = $2 RETURNING balance;", payout, user_id)

        success_msg = (
            f"🚀 <b>SUCCESSFUL CASHOUT!</b> 💰\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Pilot: <b>{first_name}</b>\n"
            f"Cashed out at: <b>{flight['mult']:.2f}x</b>\n"
            f"Total Payout: <code>+{payout:,} Coins</code>\n"
            f"Profit: <code>+{profit:,} Coins</code>\n"
            f"💰 <b>Total Balance:</b> <code>{new_bal:,} Coins</code>"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🚀 Fly Again", callback_data="game_aviator_menu"),
                InlineKeyboardButton("🎮 Casino Hub", callback_data="hub_games")
            ]
        ])
        await safe_edit_message(query, success_msg, keyboard)
        try:
            await query.answer(f"Cashed out {payout:,} Coins!", show_alert=True)
        except Exception:
            pass

        await send_dm_notification(
            user_id,
            f"🚀 <b>AVIATOR CASHOUT:</b>\n"
            f"Cashed out at: <b>{flight['mult']:.2f}x</b>\n"
            f"Payout: <code>+{payout:,} Coins</code>\n"
            f"💰 <b>Your Latest Balance:</b> <code>{new_bal:,} Coins</code>"
        )
        return

    # ---------------------------------------------------------
    # GAME 3: LUCKY SLOTS
    # ---------------------------------------------------------
    if data == "game_slots_menu":
        slots_text = (
            "🎰 <b>TANYA LUCKY SLOTS</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Reel Symbols: 🍒 🍋 🍇 🔔 💎 7️⃣\n"
            "• 2 Matching Symbols = <b>1.8x</b> Payout\n"
            "• 3 Matching Symbols = <b>4.5x</b> Payout\n"
            "• Triple 7️⃣ or 💎 = <b>10.0x MEGA JACKPOT!</b>\n\n"
            "Select your bet to spin:"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("50 🪙", callback_data="slots_spin_50"),
                InlineKeyboardButton("100 🪙", callback_data="slots_spin_100"),
                InlineKeyboardButton("250 🪙", callback_data="slots_spin_250")
            ],
            [
                InlineKeyboardButton("500 🪙", callback_data="slots_spin_500"),
                InlineKeyboardButton("1000 🪙", callback_data="slots_spin_1000"),
                InlineKeyboardButton("🔙 Hub", callback_data="hub_games")
            ]
        ])
        await safe_edit_message(query, slots_text, keyboard)
        try:
            await query.answer()
        except Exception:
            pass
        return

    if data.startswith("slots_spin_"):
        bet = int(data.split("_")[-1])
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                bal = await conn.fetchval("SELECT balance FROM users WHERE user_id = $1 FOR UPDATE;", user_id) or 0
                if bal < bet:
                    try:
                        await query.answer(f"Insufficient balance! Balance: {bal:,}", show_alert=True)
                    except Exception:
                        pass
                    return

                symbols = ["🍒", "🍋", "🍇", "🔔", "💎", "7️⃣"]
                r1 = random.choice(symbols)
                r2 = random.choice(symbols)
                r3 = random.choice(symbols)

                is_jackpot = (r1 == r2 == r3) and (r1 in ["7️⃣", "💎"])
                is_triple = (r1 == r2 == r3)
                is_double = (r1 == r2 or r2 == r3 or r1 == r3)

                if is_jackpot:
                    payout = bet * 10
                    profit = payout - bet
                    new_bal = await conn.fetchval("UPDATE users SET balance = balance + $1 WHERE user_id = $2 RETURNING balance;", profit, user_id)
                    result_header = "🎉 <b>SUPER MEGA JACKPOT! 10X!</b> 🎉"
                elif is_triple:
                    payout = int(bet * 4.5)
                    profit = payout - bet
                    new_bal = await conn.fetchval("UPDATE users SET balance = balance + $1 WHERE user_id = $2 RETURNING balance;", profit, user_id)
                    result_header = "✨ <b>TRIPLE MATCH! BIG WIN!</b> ✨"
                elif is_double:
                    payout = int(bet * 1.8)
                    profit = payout - bet
                    new_bal = await conn.fetchval("UPDATE users SET balance = balance + $1 WHERE user_id = $2 RETURNING balance;", profit, user_id)
                    result_header = "🎈 <b>PAIR MATCHED! YOU WON!</b>"
                else:
                    payout = 0
                    profit = -bet
                    new_bal = await conn.fetchval("UPDATE users SET balance = balance - $1 WHERE user_id = $2 RETURNING balance;", bet, user_id)
                    result_header = "💔 <b>NO MATCH! TRY AGAIN!</b>"

        slot_screen = (
            f"🎰 <b>[ {r1} | {r2} | {r3} ]</b> 🎰\n\n"
            f"{result_header}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Player: <b>{first_name}</b> | Bet: <code>{bet:,} Coins</code>\n"
            f"Result: <code>{'+' if profit >= 0 else ''}{profit:,} Coins</code>\n"
            f"💰 <b>Total Balance:</b> <code>{new_bal:,} Coins</code>"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"🔁 Spin {bet} Again", callback_data=f"slots_spin_{bet}"),
                InlineKeyboardButton("🎮 Casino Hub", callback_data="hub_games")
            ]
        ])
        await safe_edit_message(query, slot_screen, keyboard)
        try:
            await query.answer()
        except Exception:
            pass

        await send_dm_notification(
            user_id,
            f"🎰 <b>SLOTS REPORT:</b>\n"
            f"Reels: [ {r1} | {r2} | {r3} ]\n"
            f"Change: <code>{'+' if profit >= 0 else ''}{profit:,} Coins</code>\n"
            f"💰 <b>Your Latest Balance:</b> <code>{new_bal:,} Coins</code>"
        )
        return

    # ---------------------------------------------------------
    # GAME 4: COIN FLIP
    # ---------------------------------------------------------
    if data == "game_flip_menu":
        flip_text = (
            "🪙 <b>COIN FLIP (HEADS OR TAILS)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Predict the side of the coin for a <b>1.95x</b> payout!\n\n"
            "Choose your prediction and bet:"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Heads (50 🪙)", callback_data="flip_play_heads_50"),
                InlineKeyboardButton("Tails (50 🪙)", callback_data="flip_play_tails_50")
            ],
            [
                InlineKeyboardButton("Heads (250 🪙)", callback_data="flip_play_heads_250"),
                InlineKeyboardButton("Tails (250 🪙)", callback_data="flip_play_tails_250")
            ],
            [
                InlineKeyboardButton("Heads (1000 🪙)", callback_data="flip_play_heads_1000"),
                InlineKeyboardButton("Tails (1000 🪙)", callback_data="flip_play_tails_1000")
            ],
            [InlineKeyboardButton("🔙 Hub", callback_data="hub_games")]
        ])
        await safe_edit_message(query, flip_text, keyboard)
        try:
            await query.answer()
        except Exception:
            pass
        return

    if data.startswith("flip_play_"):
        parts = data.split("_")
        choice = parts[2]
        bet = int(parts[3])

        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                bal = await conn.fetchval("SELECT balance FROM users WHERE user_id = $1 FOR UPDATE;", user_id) or 0
                if bal < bet:
                    try:
                        await query.answer(f"Insufficient balance! Balance: {bal:,}", show_alert=True)
                    except Exception:
                        pass
                    return

                outcome = random.choice(["heads", "tails"])
                win = (outcome == choice)

                if win:
                    profit = int(bet * 0.95)
                    new_bal = await conn.fetchval("UPDATE users SET balance = balance + $1 WHERE user_id = $2 RETURNING balance;", profit, user_id)
                    res_line = f"🎉 <b>Correct! It was {outcome.upper()}!</b> You won <code>+{profit:,} Coins</code>!"
                else:
                    new_bal = await conn.fetchval("UPDATE users SET balance = balance - $1 WHERE user_id = $2 RETURNING balance;", bet, user_id)
                    res_line = f"😢 <b>Wrong! It was {outcome.upper()}!</b> You lost <code>-{bet:,} Coins</code>."

        flip_res = (
            f"🪙 <b>COIN FLIP RESULT:</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Coin landed on: <b>{'👑 HEADS' if outcome == 'heads' else '🦅 TAILS'}</b>\n"
            f"Your Pick: <b>{choice.upper()}</b>\n"
            f"{res_line}\n"
            f"💰 <b>Your New Balance:</b> <code>{new_bal:,} Coins</code>"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔁 Flip Again", callback_data="game_flip_menu"),
                InlineKeyboardButton("🎮 Casino Hub", callback_data="hub_games")
            ]
        ])
        await safe_edit_message(query, flip_res, keyboard)
        try:
            await query.answer()
        except Exception:
            pass

        await send_dm_notification(
            user_id,
            f"🪙 <b>COIN FLIP OUTCOME:</b>\n"
            f"Result: <b>{outcome.upper()}</b> | Pick: <b>{choice.upper()}</b>\n"
            f"Amount: <code>{'+' if win else '-'}{bet:,} Coins</code>\n"
            f"💰 <b>Your Latest Balance:</b> <code>{new_bal:,} Coins</code>"
        )
        return

    # ---------------------------------------------------------
    # GAME 5: HIGH-LOW CARD GUESS (DYNAMIC PROBABILITY)
    # ---------------------------------------------------------
    if data == "game_highlow_menu":
        hl_intro = (
            "🃏 <b>HIGH-LOW CARD GUESS (PROBABILITY ODDS)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Tanya draws an initial card. Predict if the next card will be "
            "<b>HIGHER ⬆️</b> or <b>LOWER ⬇️</b>!\n\n"
            "🎯 <b>Dynamic Multipliers:</b>\n"
            "• Middle Cards (7-9) = <b>~1.92x</b> (50/50 Balance)\n"
            "• Extreme Risk Cards = <b>Up to 11.5x Mega Multiplier!</b> 🔥\n"
            "• Same Rank Draw = Bet 100% Refunded (Tie)!\n\n"
            "Select your bet to draw first card:"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("50 🪙", callback_data="hl_start_50"),
                InlineKeyboardButton("100 🪙", callback_data="hl_start_100"),
                InlineKeyboardButton("250 🪙", callback_data="hl_start_250")
            ],
            [
                InlineKeyboardButton("500 🪙", callback_data="hl_start_500"),
                InlineKeyboardButton("1000 🪙", callback_data="hl_start_1000"),
                InlineKeyboardButton("🔙 Hub", callback_data="hub_games")
            ]
        ])
        await safe_edit_message(query, hl_intro, keyboard)
        try:
            await query.answer()
        except Exception:
            pass
        return
    if data.startswith("hl_start_"):
        bet = int(data.split("_")[-1])
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                bal = await conn.fetchval("SELECT balance FROM users WHERE user_id = $1 FOR UPDATE;", user_id) or 0
                if bal < bet:
                    try:
                        await query.answer(f"Insufficient balance! Balance: {bal:,}", show_alert=True)
                    except Exception:
                        pass
                    return
                await conn.execute("UPDATE users SET balance = balance - $1 WHERE user_id = $2;", bet, user_id)

        # Initial card 3 to 13 ke beech rakha hai taaki Higher & Lower dono valid bane rahein
        card_val = random.randint(3, 13)
        card_names = {11: "Jack", 12: "Queen", 13: "King", 14: "Ace"}
        card_name = card_names.get(card_val, str(card_val))
        suits = ["♠️", "♥️", "♦️", "♣️"]
        card_str = f"{card_name} of {random.choice(suits)}"

        # 12 non-tie cards out of 2 to 14
        higher_count = 14 - card_val
        lower_count = card_val - 2

        # House Edge: 4% (0.96 RTP)
        mult_higher = round(0.96 / (higher_count / 12.0), 2)
        mult_lower = round(0.96 / (lower_count / 12.0), 2)

        session_id = str(uuid.uuid4())[:8]
        active_highlow[session_id] = {
            "user_id": user_id,
            "bet": bet,
            "card_value": card_val,
            "card_str": card_str,
            "mult_higher": mult_higher,
            "mult_lower": mult_lower,
            "active": True
        }

        hl_card_msg = (
            f"🃏 <b>HIGH-LOW IN PLAY!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 Player: <b>{first_name}</b> | Bet: <code>{bet:,} Coins</code>\n"
            f"🎴 Dealt Card: <b>{card_str}</b> (Rank: {card_val})\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 <b>Calculated Odds:</b>\n"
            f"• ⬆️ Higher ({higher_count}/12): <b>{mult_higher:.2f}x</b> Payout\n"
            f"• ⬇️ Lower ({lower_count}/12): <b>{mult_lower:.2f}x</b> Payout\n"
            f"• 🤝 Equal Rank: 100% Refund (Push)\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Will the next card be Higher or Lower?"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"⬆️ HIGHER ({mult_higher:.2f}x)", callback_data=f"hl_guess_higher_{session_id}"),
                InlineKeyboardButton(f"⬇️ LOWER ({mult_lower:.2f}x)", callback_data=f"hl_guess_lower_{session_id}")
            ]
        ])
        await safe_edit_message(query, hl_card_msg, keyboard)
        try:
            await query.answer("Card dealt! Make your prediction!")
        except Exception:
            pass
        return
    if data.startswith("hl_guess_"):
        parts = data.split("_")
        guess = parts[2]
        session_id = parts[3]

        sess = active_highlow.get(session_id)
        if not sess or not sess["active"]:
            try:
                await query.answer("Card game already resolved.", show_alert=True)
            except Exception:
                pass
            return

        if sess["user_id"] != user_id:
            try:
                await query.answer("This is not your game!", show_alert=True)
            except Exception:
                pass
            return

        sess["active"] = False
        old_val = sess["card_value"]
        old_str = sess["card_str"]
        bet = sess["bet"]
        mult_higher = sess.get("mult_higher", 1.92)
        mult_lower = sess.get("mult_lower", 1.92)
        chosen_mult = mult_higher if guess == "higher" else mult_lower

        new_val = random.randint(2, 14)
        card_names = {11: "Jack", 12: "Queen", 13: "King", 14: "Ace"}
        new_name = card_names.get(new_val, str(new_val))
        new_str = f"{new_name} of {random.choice(['♠️', '♥️', '♦️', '♣️'])}"

        if new_val == old_val:
            win = False
            tie = True
        elif guess == "higher":
            win = (new_val > old_val)
            tie = False
        else:
            win = (new_val < old_val)
            tie = False

        profit = 0
        async with pg_pool.acquire() as conn:
            if tie:
                new_bal = await conn.fetchval("UPDATE users SET balance = balance + $1 WHERE user_id = $2 RETURNING balance;", bet, user_id)
                outcome_text = "🤝 <b>SAME VALUE DRAW! PUSH / TIE!</b> Bet refunded!"
            elif win:
                payout = int(bet * chosen_mult)
                profit = payout - bet
                new_bal = await conn.fetchval("UPDATE users SET balance = balance + $1 WHERE user_id = $2 RETURNING balance;", payout, user_id)
                outcome_text = f"🎉 <b>CORRECT PREDICTION ({chosen_mult:.2f}x)!</b> Won <code>+{profit:,} Coins</code>!"
            else:
                new_bal = await conn.fetchval("SELECT balance FROM users WHERE user_id = $1;", user_id) or 0
                outcome_text = f"😢 <b>WRONG PREDICTION!</b> Lost <code>-{bet:,} Coins</code>."

        res_msg = (
            f"🃏 <b>HIGH-LOW RESULT:</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Dealt Card: <b>{old_str}</b> (Rank: {old_val})\n"
            f"Drawn Card: <b>{new_str}</b> (Rank: {new_val})\n"
            f"Your Pick: <b>{guess.upper()} ({chosen_mult:.2f}x)</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{outcome_text}\n"
            f"💰 <b>Total Balance:</b> <code>{new_bal:,} Coins</code>"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔁 Play Again", callback_data="game_highlow_menu"),
                InlineKeyboardButton("🎮 Casino Hub", callback_data="hub_games")
            ]
        ])
        await safe_edit_message(query, res_msg, keyboard)
        try:
            await query.answer()
        except Exception:
            pass

        # Private DM receipt with exact multiplier
        outcome_label = f"WON 🎉 (+{profit:,} Coins @ {chosen_mult:.2f}x)" if win else ("TIE 🤝 (Bet Refunded)" if tie else f"LOST 😢 (-{bet:,} Coins)")
        await send_dm_notification(
            user_id,
            f"🃏 <b>HIGH-LOW OUTCOME:</b>\n"
            f"Dealt: {old_str} ({old_val}) ➔ Drew: {new_str} ({new_val})\n"
            f"Pick: {guess.upper()} ({chosen_mult:.2f}x)\n"
            f"Status: {outcome_label}\n"
            f"💰 <b>Latest Balance:</b> <code>{new_bal:,} Coins</code>"
        )
        return
    # ---------------------------------------------------------
    # GAME 6: LUCKY 7 DICE (UNDER 7, 7, OVER 7)
    # ---------------------------------------------------------
    if data == "game_dice_menu":
        dice_text = (
            "🎲 <b>LUCKY 7 DICE ROLL</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Tanya rolls two 6-sided dice (Total 2 to 12).\n"
            "• <b>Under 7 (2 to 6):</b> 2.0x Payout\n"
            "• <b>Exact 7 (Lucky 7):</b> 4.5x Payout!\n"
            "• <b>Over 7 (8 to 12):</b> 2.0x Payout\n\n"
            "Choose your prediction and bet:"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔻 Under 7 (100)", callback_data="dice_roll_under_100"),
                InlineKeyboardButton("⭐ Exact 7 (100)", callback_data="dice_roll_seven_100"),
                InlineKeyboardButton("🔺 Over 7 (100)", callback_data="dice_roll_over_100")
            ],
            [
                InlineKeyboardButton("🔻 Under 7 (500)", callback_data="dice_roll_under_500"),
                InlineKeyboardButton("⭐ Exact 7 (500)", callback_data="dice_roll_seven_500"),
                InlineKeyboardButton("🔺 Over 7 (500)", callback_data="dice_roll_over_500")
            ],
            [InlineKeyboardButton("🔙 Hub", callback_data="hub_games")]
        ])
        await safe_edit_message(query, dice_text, keyboard)
        try:
            await query.answer()
        except Exception:
            pass
        return

    if data.startswith("dice_roll_"):
        parts = data.split("_")
        bet_type = parts[2]
        bet = int(parts[3])

        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                bal = await conn.fetchval("SELECT balance FROM users WHERE user_id = $1 FOR UPDATE;", user_id) or 0
                if bal < bet:
                    try:
                        await query.answer(f"Insufficient balance! Balance: {bal:,}", show_alert=True)
                    except Exception:
                        pass
                    return

                d1 = random.randint(1, 6)
                d2 = random.randint(1, 6)
                total = d1 + d2

                if bet_type == "under":
                    win = (total < 7)
                    mult = 2.0
                elif bet_type == "over":
                    win = (total > 7)
                    mult = 2.0
                else:
                    win = (total == 7)
                    mult = 4.5

                if win:
                    payout = int(bet * mult)
                    profit = payout - bet
                    new_bal = await conn.fetchval("UPDATE users SET balance = balance + $1 WHERE user_id = $2 RETURNING balance;", profit, user_id)
                    res_line = f"🎉 <b>YOU WON!</b> Received <code>+{profit:,} Coins</code>!"
                else:
                    new_bal = await conn.fetchval("UPDATE users SET balance = balance - $1 WHERE user_id = $2 RETURNING balance;", bet, user_id)
                    res_line = f"😢 <b>YOU LOST!</b> Lost <code>-{bet:,} Coins</code>."

        res_msg = (
            f"🎲 <b>DICE ROLL RESULT:</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Dice: <b>[{d1}] + [{d2}] = {total}</b>\n"
            f"Your Pick: <b>{bet_type.upper()}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{res_line}\n"
            f"💰 <b>Latest Balance:</b> <code>{new_bal:,} Coins</code>"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔁 Roll Again", callback_data="game_dice_menu"),
                InlineKeyboardButton("🎮 Casino Hub", callback_data="hub_games")
            ]
        ])
        await safe_edit_message(query, res_msg, keyboard)
        try:
            await query.answer()
        except Exception:
            pass

        await send_dm_notification(
            user_id,
            f"🎲 <b>DICE RESULT:</b>\n"
            f"Rolled: [{d1}]+[{d2}] = {total} | Pick: {bet_type.upper()}\n"
            f"Outcome: {'WON 🎉' if win else 'LOST 😢'}\n"
            f"💰 <b>Your Latest Balance:</b> <code>{new_bal:,} Coins</code>"
        )
        return

    # ---------------------------------------------------------
    # GAME 7: 1v1 PVP DUEL
    # ---------------------------------------------------------
    if data == "btn_duel_menu":
        duel_menu_text = (
            "⚔️ <b>1v1 PVP DUEL CHALLENGE</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "In any group chat, type:\n"
            "<code>/duel &lt;amount&gt;</code> (e.g. <code>/duel 250</code>)\n\n"
            "An interactive challenge button appears in the group chat! "
            "Any opponent can click <b>Accept Duel</b> to roll dice. Highest score takes the pot!"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back to Casino", callback_data="hub_games")]
        ])
        await safe_edit_message(query, duel_menu_text, keyboard)
        try:
            await query.answer()
        except Exception:
            pass
        return

    if data.startswith("duel_cancel_"):
        duel_id = data.split("_")[-1]
        duel = active_duels.get(duel_id)

        if not duel or duel["resolved"]:
            try:
                await query.answer("This duel challenge is no longer active.", show_alert=True)
            except Exception:
                pass
            return

        if duel["creator_id"] != user_id:
            try:
                await query.answer("Only the challenger who created this duel can cancel it!", show_alert=True)
            except Exception:
                pass
            return

        duel["resolved"] = True
        active_duels.pop(duel_id, None)

        await safe_edit_message(
            query,
            f"❌ <b>Duel Challenge Cancelled!</b>\n"
            f"<b>{html.escape(duel['creator_name'])}</b> cancelled the <code>{duel['bet']:,} Coins</code> duel challenge."
        )
        try:
            await query.answer("Duel challenge cancelled!")
        except Exception:
            pass
        return

    if data.startswith("duel_accept_"):
        duel_id = data.split("_")[-1]
        duel = active_duels.get(duel_id)
        if not duel:
            try:
                await query.answer("This duel challenge has expired.", show_alert=True)
            except Exception:
                pass
            return

        if duel["resolved"]:
            try:
                await query.answer("This duel has already been played!", show_alert=True)
            except Exception:
                pass
            return

        if duel["creator_id"] == user_id:
            try:
                await query.answer("You cannot accept your own duel challenge!", show_alert=True)
            except Exception:
                pass
            return

        bet = duel["bet"]
        creator_id = duel["creator_id"]
        creator_name = duel["creator_name"]
        alert_err = None
        dm_queue = []

        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                first_lock, second_lock = sorted([creator_id, user_id])
                await conn.fetchval("SELECT user_id FROM users WHERE user_id = $1 FOR UPDATE;", first_lock)
                await conn.fetchval("SELECT user_id FROM users WHERE user_id = $1 FOR UPDATE;", second_lock)

                c_bal = await conn.fetchval("SELECT balance FROM users WHERE user_id = $1;", creator_id) or 0
                a_bal = await conn.fetchval("SELECT balance FROM users WHERE user_id = $1;", user_id) or 0

                if c_bal < bet:
                    alert_err = "Challenger no longer has sufficient coins for this duel."
                elif a_bal < bet:
                    alert_err = f"You don't have enough coins! Balance: {a_bal:,} Coins."
                else:
                    duel["resolved"] = True

                    c_d1, c_d2 = random.randint(1, 6), random.randint(1, 6)
                    a_d1, a_d2 = random.randint(1, 6), random.randint(1, 6)
                    c_total = c_d1 + c_d2
                    a_total = a_d1 + a_d2

                    pot = bet * 2
                    fee = max(1, int(pot * 0.05))
                    winner_pot = pot - fee

                    if c_total > a_total:
                        c_new = await conn.fetchval("UPDATE users SET balance = balance + $1 WHERE user_id = $2 RETURNING balance;", winner_pot - bet, creator_id)
                        a_new = await conn.fetchval("UPDATE users SET balance = balance - $1 WHERE user_id = $2 RETURNING balance;", bet, user_id)
                        outcome_msg = (
                            f"🏆 <b>CHALLENGER WON THE DUEL!</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"👑 <b>{creator_name}:</b> [{c_d1}]+[{c_d2}] = <b>{c_total}</b>\n"
                            f"⚔️ <b>{first_name}:</b> [{a_d1}]+[{a_d2}] = <b>{a_total}</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"🎉 <b>{creator_name}</b> takes the pot of <code>{winner_pot:,} Coins</code>!\n"
                            f"<i>(System fee: {fee} Coins)</i>"
                        )
                        dm_queue.append((creator_id, f"⚔️ <b>DUEL VICTORY!</b> Won <code>+{winner_pot - bet:,} Coins</code> vs {first_name}!\n💰 Balance: <code>{c_new:,} Coins</code>"))
                        dm_queue.append((user_id, f"⚔️ <b>DUEL DEFEAT!</b> Lost <code>-{bet:,} Coins</code> to {creator_name}!\n💰 Balance: <code>{a_new:,} Coins</code>"))

                    elif a_total > c_total:
                        a_new = await conn.fetchval("UPDATE users SET balance = balance + $1 WHERE user_id = $2 RETURNING balance;", winner_pot - bet, user_id)
                        c_new = await conn.fetchval("UPDATE users SET balance = balance - $1 WHERE user_id = $2 RETURNING balance;", bet, creator_id)
                        outcome_msg = (
                            f"🏆 <b>OPPONENT WON THE DUEL!</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"⚔️ <b>{creator_name}:</b> [{c_d1}]+[{c_d2}] = <b>{c_total}</b>\n"
                            f"👑 <b>{first_name}:</b> [{a_d1}]+[{a_d2}] = <b>{a_total}</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"🎉 <b>{first_name}</b> takes the pot of <code>{winner_pot:,} Coins</code>!\n"
                            f"<i>(System fee: {fee} Coins)</i>"
                        )
                        dm_queue.append((user_id, f"⚔️ <b>DUEL VICTORY!</b> Won <code>+{winner_pot - bet:,} Coins</code> vs {creator_name}!\n💰 Balance: <code>{a_new:,} Coins</code>"))
                        dm_queue.append((creator_id, f"⚔️ <b>DUEL DEFEAT!</b> Lost <code>-{bet:,} Coins</code> to {first_name}!\n💰 Balance: <code>{c_new:,} Coins</code>"))
                    else:
                        outcome_msg = (
                            f"🤝 <b>DUEL TIED! DRAW!</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"🎲 Both players scored <b>{c_total}</b>!\n"
                            f"All coins returned to both accounts with zero fee."
                        )

        if alert_err:
            try:
                await query.answer(alert_err, show_alert=True)
            except Exception:
                pass
            return

        active_duels.pop(duel_id, None)
        for target_uid, dm_txt in dm_queue:
            await send_dm_notification(target_uid, dm_txt)
        await safe_edit_message(query, outcome_msg)
        try:
            await query.answer("Duel completed!")
        except Exception:
            pass
        return
    # ---------------------------------------------------------
    # TIC-TAC-TOE CALLBACKS (ACCEPT & MOVES)
    # ---------------------------------------------------------
    # ---------------------------------------------------------
    # TIC-TAC-TOE CALLBACKS (ACCEPT & MOVES)
    # ---------------------------------------------------------
    if data.startswith("ttt_cancel_"):
        game_id = data.split("_")[-1]
        game = active_ttt.get(game_id)

        if not game or game["resolved"] or game["active"]:
            try:
                await query.answer("This challenge cannot be cancelled now.", show_alert=True)
            except Exception:
                pass
            return

        if game["player_x"] != user_id:
            try:
                await query.answer("Only the challenger who created this game can cancel it!", show_alert=True)
            except Exception:
                pass
            return

        game["resolved"] = True
        active_ttt.pop(game_id, None)

        await safe_edit_message(
            query,
            f"❌ <b>Tic-Tac-Toe Challenge Cancelled!</b>\n"
            f"<b>{html.escape(game['player_x_name'])}</b> cancelled the <code>{game['bet']:,} Coins</code> challenge."
        )
        try:
            await query.answer("Challenge cancelled!")
        except Exception:
            pass
        return

    if data.startswith("ttt_accept_"):
        game_id = data.split("_")[-1]
        game = active_ttt.get(game_id)

        if not game or game["resolved"] or game["active"]:
            try:
                await query.answer("This challenge has expired or already started.", show_alert=True)
            except Exception:
                pass
            return

        if game["player_x"] == user_id:
            try:
                await query.answer("You cannot accept your own challenge!", show_alert=True)
            except Exception:
                pass
            return

        bet = game["bet"]
        px_id = game["player_x"]

        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                first_lock, second_lock = sorted([px_id, user_id])
                await conn.fetchval("SELECT user_id FROM users WHERE user_id = $1 FOR UPDATE;", first_lock)
                await conn.fetchval("SELECT user_id FROM users WHERE user_id = $1 FOR UPDATE;", second_lock)

                px_bal = await conn.fetchval("SELECT balance FROM users WHERE user_id = $1;", px_id) or 0
                po_bal = await conn.fetchval("SELECT balance FROM users WHERE user_id = $1;", user_id) or 0

                if px_bal < bet:
                    try:
                        await query.answer("The challenger no longer has sufficient balance.", show_alert=True)
                    except Exception:
                        pass
                    return

                if po_bal < bet:
                    try:
                        await query.answer(f"Insufficient balance! You need {bet:,} Coins.", show_alert=True)
                    except Exception:
                        pass
                    return

                # Deduct bets
                await conn.execute("UPDATE users SET balance = balance - $1 WHERE user_id = $2;", bet, px_id)
                await conn.execute("UPDATE users SET balance = balance - $1 WHERE user_id = $2;", bet, user_id)

        game["player_o"] = user_id
        game["player_o_name"] = raw_name
        game["active"] = True
        game["created_at"] = time.time()

        px_name = html.escape(game['player_x_name'])
        po_name = html.escape(raw_name)
        px_turn_mention = f'<a href="tg://user?id={px_id}">{px_name}</a>'

        board_text = (
            f"🎮 <b>TIC-TAC-TOE MATCH STARTED!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"❌ <b>{px_name}</b> vs ⭕ <b>{po_name}</b>\n"
            f"💰 <b>Stake Pot:</b> <code>{bet * 2:,} Coins</code>\n"
            f"👉 <b>Current Turn:</b> ❌ {px_turn_mention}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Tap an empty tile on the grid to make your move:"
        )
        await safe_edit_message(query, board_text, render_ttt_keyboard(game_id, game["board"]))
        try:
            await query.answer("Match started! Player X starts.")
        except Exception:
            pass

        if query.message:
            try:
                await query.message.reply_text(
                    f"🎮 ❌ {px_turn_mention}, <b>now it's your turn!</b> Make your move on the board above.",
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass
        return
    if data.startswith("ttt_m_"):
        parts = data.split("_")
        game_id = parts[2]
        pos = int(parts[3])

        game = active_ttt.get(game_id)
        if not game or not game["active"] or game["resolved"]:
            try:
                await query.answer("This game is not active or has concluded.", show_alert=True)
            except Exception:
                pass
            return

        px_id = game["player_x"]
        po_id = game["player_o"]

        if user_id not in [px_id, po_id]:
            try:
                await query.answer("You are not a player in this match!", show_alert=True)
            except Exception:
                pass
            return

        if user_id != game["turn"]:
            try:
                await query.answer("It is not your turn!", show_alert=True)
            except Exception:
                pass
            return

        if game["board"][pos] != " ":
            try:
                await query.answer("This tile is already taken!")
            except Exception:
                pass
            return

        # Mark move
        symbol = "X" if user_id == px_id else "O"
        game["board"][pos] = symbol
        res = check_ttt_winner(game["board"])

        bet = game["bet"]
        pot = bet * 2
        fee = max(1, int(pot * 0.05))
        win_pot = pot - fee

        if res == "X" or res == "O":
            game["active"] = False
            game["resolved"] = True
            active_ttt.pop(game_id, None)
            winner_id = px_id if res == "X" else po_id
            winner_name = game["player_x_name"] if res == "X" else game["player_o_name"]
            loser_id = po_id if res == "X" else px_id
            loser_name = game["player_o_name"] if res == "X" else game["player_x_name"]

            winner_mention = f'<a href="tg://user?id={winner_id}">{html.escape(winner_name)}</a>'
            loser_mention = f'<a href="tg://user?id={loser_id}">{html.escape(loser_name)}</a>'

            async with pg_pool.acquire() as conn:
                w_bal = await conn.fetchval("UPDATE users SET balance = balance + $1 WHERE user_id = $2 RETURNING balance;", win_pot, winner_id)
                l_bal = await conn.fetchval("SELECT balance FROM users WHERE user_id = $1;", loser_id) or 0

            win_msg = (
                f"🏆 <b>TIC-TAC-TOE WINNER!</b> 🎉\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"👑 <b>Winner:</b> {winner_mention} ({'❌' if res == 'X' else '⭕'})\n"
                f"💀 <b>Defeated:</b> {loser_mention}\n"
                f"💰 <b>Prize Won:</b> <code>+{win_pot:,} Coins</code>\n"
                f"<i>(System fee 5%: {fee:,} Coins)</i>"
            )
            await safe_edit_message(query, win_msg, render_ttt_keyboard(game_id, game["board"]))
            try:
                await query.answer(f"{winner_name} won!")
            except Exception:
                pass

            await send_dm_notification(winner_id, f"🏆 <b>TIC-TAC-TOE VICTORY!</b>\nYou defeated {loser_name} and won <code>+{win_pot - bet:,} Coins</code> profit!\n💰 Balance: <code>{w_bal:,} Coins</code>")
            await send_dm_notification(loser_id, f"😢 <b>TIC-TAC-TOE DEFEAT!</b>\nYou lost to {winner_name} (-<code>{bet:,} Coins</code>).\n💰 Balance: <code>{l_bal:,} Coins</code>")
            return

        elif res == "draw":
            game["active"] = False
            game["resolved"] = True
            active_ttt.pop(game_id, None)

            # Refund both players
            async with pg_pool.acquire() as conn:
                await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2;", bet, px_id)
                await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2;", bet, po_id)

            draw_msg = (
                f"🤝 <b>TIC-TAC-TOE DRAW! MATCH TIED!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Both players played to a stalemate!\n"
                f"Stakes of <code>{bet:,} Coins</code> have been refunded to both players."
            )
            await safe_edit_message(query, draw_msg, render_ttt_keyboard(game_id, game["board"]))
            try:
                await query.answer("Match Tied! Bets Refunded.")
            except Exception:
                pass
            return

        # Switch turns
        next_turn = po_id if user_id == px_id else px_id
        next_name = game["player_o_name"] if user_id == px_id else game["player_x_name"]
        next_sym = "⭕" if user_id == px_id else "❌"
        game["turn"] = next_turn
        game["created_at"] = time.time()

        px_name = html.escape(game['player_x_name'])
        po_name = html.escape(game['player_o_name'])
        turn_mention = f'<a href="tg://user?id={next_turn}">{html.escape(next_name)}</a>'

        next_text = (
            f"🎮 <b>TIC-TAC-TOE IN PLAY!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"❌ <b>{px_name}</b> vs ⭕ <b>{po_name}</b>\n"
            f"💰 <b>Stake Pot:</b> <code>{bet * 2:,} Coins</code>\n"
            f"👉 <b>Current Turn:</b> {next_sym} {turn_mention}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Tap an empty tile on the grid to make your move:"
        )
        await safe_edit_message(query, next_text, render_ttt_keyboard(game_id, game["board"]))
        try:
            await query.answer()
        except Exception:
            pass

        if query.message:
            try:
                await query.message.reply_text(
                    f"🎮 {next_sym} {turn_mention}, <b>now it's your turn!</b> Make your move on the board above.",
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass
        return    
    try:
        await query.answer()
    except Exception:
        pass
# ---------------------------------------------------------
# CONVERSATION HANDLERS (DM & GROUPS)
# ---------------------------------------------------------
async def handle_private_message(update: Update, context: CallbackContext):
    """Handles 1-on-1 private conversations with Tanya and boyfriend response suggestions."""
    msg = update.message
    if not msg or not msg.from_user:
        return

    # Hidden Paid Broadcast Catch Check
    if msg.from_user.id in waiting_for_paidbrd:
        waiting_for_paidbrd.remove(msg.from_user.id)
        await execute_paidbrd_broadcast(update, context)
        return

    # Check if this message is a Wordle guess
    user = msg.from_user
    user_id = user.id    
    chat_id = msg.chat_id
    text = msg.text or msg.caption or ""

    # User messaged in Private DM -> Keep DM verification active in RAM & DB
    await mark_user_dm_verified(user_id, user.username, user.first_name)

    # Check if this message is a Wordle guess
    if await process_wordle_guess(update, context):
        return

    await record_user_activity(user_id, user.username, user.first_name)    
    # Check 30 daily chat limit & reward coins
    allowed, count = await process_chat_reward_and_limit(
        user_id, user.username, user.first_name, DM_CHAT_REWARD
    )

    if not allowed:
        await msg.reply_text(
            "Baby, you have reached your daily limit of <b>100 chats</b> for today! 🥰\n"
            "Let's catch up tomorrow for more chats and coins! Bye bye for now ❤️",
            parse_mode=ParseMode.HTML
        )
        return
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)    
    except Exception:
        pass

    save_chat_message_to_ram(
        chat_id=chat_id,
        chat_type="private",
        role="user",
        content=text
    )

    history = build_chat_context_from_ram(chat_id)
    try:
        raw_reply = await call_gemma_api(CHAT_MODEL, history, system_text=DM_SYSTEM_PROMPT, temperature=0.8)
        if not raw_reply:
            return

        parts = raw_reply.split("===USER_OPTIONS===")
        main_text = clean_response(parts[0])

        suggestions = []
        if len(parts) > 1:
            raw_sugg = parts[1].strip().split("\n")
            suggestions = [s.strip() for s in raw_sugg if s.strip()][:2]

        if not main_text:
            return

        reply_markup = None
        if suggestions:
            keyboard = [[s] for s in suggestions]
            reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)

        if reply_markup:
            await msg.reply_text(main_text, reply_markup=reply_markup)
        else:
            await msg.reply_text(main_text)

        save_chat_message_to_ram(
            chat_id=chat_id,
            chat_type="private",
            role="model",
            content=main_text
        )        
        await increment_analytics(reply=True)

    except Exception as e:
        logger.error(f"Error handling private chat: {e}")
        await increment_analytics(error=True)

async def handle_group_message(update: Update, context: CallbackContext):
    """Handles group conversations, single-word game scans (Scramble & Wordle), and AI replies."""
    msg = update.message
    if not msg or not msg.from_user:
        return

    # Keep active group alive in group_gdp_cache RAM so it receives 2-hr Scrambled Word Challenges
    if msg.chat.type in ["group", "supergroup"]:
        asyncio.create_task(ensure_group_in_ram(msg.chat_id, msg.chat.title or "Group Chat"))

    # Scan single-word messages for both Scrambled Word Challenge and Wordle
    if await process_single_word_games(update, context):
        return
    text = msg.text or msg.caption or ""
    user = msg.from_user
    chat_id = msg.chat_id
    user_id = user.id

    await record_user_activity(user_id, user.username, user.first_name)
    is_reply_to_bot = False
    reply_to_content = None

    if msg.reply_to_message:
        replied_msg = msg.reply_to_message
        reply_to_content = replied_msg.text or replied_msg.caption or "[Media]"
        if replied_msg.from_user and replied_msg.from_user.id == context.bot.id:
            is_reply_to_bot = True

    has_trigger_word = bool(GROUP_TRIGGER_REGEX.search(text))

    if not (is_reply_to_bot or has_trigger_word):
        return

    # Check 30 daily chat limit & reward coins (only when Tanya is triggered)
    allowed, count = await process_chat_reward_and_limit(
        user_id, user.username, user.first_name, GROUP_CHAT_REWARD
    )

    if not allowed:
        uname = f"@{user.username}" if user.username else user.first_name
        await msg.reply_text(
            f"Hey {uname}, you have reached your daily limit of <b>100 chats</b> for today! 😉\n"
            f"Come back tomorrow to chat more and earn new coins! ✨",
            parse_mode=ParseMode.HTML
        )
        return
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)    
    except Exception:
        pass

    save_chat_message_to_ram(
        chat_id=chat_id,
        chat_type="group",
        role="user",
        content=text,
        reply_to_content=reply_to_content
    )

    history = build_chat_context_from_ram(chat_id)
    try:
        raw_reply = await call_gemma_api(CHAT_MODEL, history, system_text=GROUP_SYSTEM_PROMPT, temperature=0.9)
        clean_text = clean_response(raw_reply)

        if not clean_text:
            return

        try:
            await msg.reply_text(clean_text)
        except Exception:
            await context.bot.send_message(chat_id=chat_id, text=clean_text)

        save_chat_message_to_ram(
            chat_id=chat_id,
            chat_type="group",
            role="model",
            content=clean_text,
            reply_to_content=text[:120]
        )        
        await increment_analytics(reply=True)

    except Exception as e:
        logger.error(f"Error in group chat handler: {e}")
        await increment_analytics(error=True)

async def handle_new_chat_members(update: Update, context: CallbackContext):
    """Greets new members in GC, tracks bot group addition, and sends DM welcome with game buttons."""
    msg = update.message
    if not msg or not msg.new_chat_members:
        return

    chat_id = msg.chat_id
    chat_title = msg.chat.title or "Group Chat"

    # Ensure group is tracked in RAM
    if msg.chat.type in ["group", "supergroup"]:
        asyncio.create_task(ensure_group_in_ram(chat_id, chat_title))

    for new_member in msg.new_chat_members:
        if new_member.id == context.bot.id:
            # Bot was added to this group -> Save in DB & RAM
            asyncio.create_task(register_group_in_db_and_ram(chat_id, chat_title))
            try:
                intro = (
                    "👋 <b>Hey everyone! Tanya is here! 🥰</b>\n"
                    "Thanks for adding me to the group! Type <code>/games</code> to play casino games, "
                    "<code>/stats</code> to check Group GDP, or talk to me anytime!"
                )
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🎮 Open Casino Hub", callback_data="hub_games")]
                ])
                await msg.reply_text(intro, parse_mode=ParseMode.HTML, reply_markup=keyboard)
            except Exception:
                pass
            continue

        if new_member.is_bot:
            continue

        name = new_member.first_name or "Friend"
        welcome_card = (
            f"🌟 <b>Welcome to the group, {name}! 🎉</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Great to have you here! Feel free to chat with us, participate in conversations, "
            f"or try out our arcade casino games with <code>/games</code>! ✨"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🎮 Play Games", callback_data="hub_games"),
                InlineKeyboardButton("💰 My Wallet", callback_data="btn_balance")
            ]
        ])

        try:
            await msg.reply_text(welcome_card, parse_mode=ParseMode.HTML, reply_markup=keyboard)
            await record_user_activity(new_member.id, new_member.username, new_member.first_name)
        except Exception as e:
            logger.error(f"Welcome greeting failed: {e}")

        # Send instant DM to the joining user with all game buttons & Add to GC button
        asyncio.create_task(send_welcome_dm_with_games(new_member.id, context))

async def handle_chat_join_request(update: Update, context: CallbackContext):
    """Triggered as soon as a user sends a Join Request to a group where the bot is present."""
    req = update.chat_join_request
    if not req or not req.from_user:
        return

    user = req.from_user
    if user.is_bot:
        return

    await record_user_activity(user.id, user.username, user.first_name)
    if req.chat and req.chat.type in ["group", "supergroup"]:
        asyncio.create_task(ensure_group_in_ram(req.chat.id, req.chat.title or "Group Chat"))

    # Send instant DM with games & Add to GC button
    asyncio.create_task(send_welcome_dm_with_games(user.id, context))

async def handle_my_chat_member(update: Update, context: CallbackContext):
    """Tracks when bot is added to or removed from any group via my_chat_member updates."""
    my_member = update.my_chat_member
    if not my_member or not my_member.chat:
        return

    chat = my_member.chat
    if chat.type not in ["group", "supergroup"]:
        return

    new_status = my_member.new_chat_member.status
    if new_status in ["member", "administrator", "creator"]:
        await register_group_in_db_and_ram(chat.id, chat.title or "Group Chat")
    elif new_status in ["left", "kicked"]:
        global pg_pool
        if pg_pool:
            try:
                async with pg_pool.acquire() as conn:
                    await conn.execute("UPDATE groups_gdp SET is_active = FALSE, updated_at = NOW() WHERE chat_id = $1;", chat.id)
            except Exception:
                pass
# ---------------------------------------------------------
# BACKGROUND WORKERS
# ---------------------------------------------------------
async def inactive_user_reengagement_task():
    """Finds inactive users between 24-96 hours and sends affectionate reminders."""
    await asyncio.sleep(60)
    logger.info("Starting Inactive User Re-engagement Worker...")

    while True:
        try:
            global pg_pool
            if pg_pool:
                now_utc = datetime.now(timezone.utc)
                cutoff_start = now_utc - timedelta(hours=24)
                cutoff_end = now_utc - timedelta(hours=96)

                async with pg_pool.acquire() as conn:
                    rows = await conn.fetch('''
                        SELECT user_id, first_name 
                        FROM users 
                        WHERE last_interacted <= $1 
                          AND last_interacted >= $2 
                          AND (reengage_sent_at IS NULL OR reengage_sent_at < NOW() - INTERVAL '3 days')
                        LIMIT 25;
                    ''', cutoff_start, cutoff_end)

                pings = [
                    "Hey Sweetheart! Tanya misses you so much... Come over and chat with me! 🥺❤️",
                    "It has been a while since we talked! Are you busy today? Drop by and say hi! ✨",
                    "Chat feels so quiet without you! Tanya is waiting for your message... 🥰",
                    "Hey! Don't forget to claim your free daily coins! Type /daily whenever you're free! 🎁"
                ]

                for row in rows:
                    uid = row["user_id"]
                    fname = row["first_name"] or "Friend"
                    reminder = random.choice(pings).replace("Sweetheart", fname)

                    try:
                        await ptb_app.bot.send_message(chat_id=uid, text=reminder)
                        async with pg_pool.acquire() as conn:
                            await conn.execute("UPDATE users SET reengage_sent_at = NOW() WHERE user_id = $1;", uid)
                        await asyncio.sleep(1)
                    except Exception:
                        pass

        except Exception as e:
            logger.error(f"Error in re-engagement worker: {e}")

        await asyncio.sleep(14400)

async def inactivity_wealth_decay_task():
    """Checks every 5 minutes and deducts 1% wealth from users inactive for 24+ hours (once per 24 hours)."""
    await asyncio.sleep(45)
    logger.info("Starting Inactivity Wealth Decay Worker (Every 5 minutes)...")

    while True:
        try:
            global pg_pool
            if pg_pool:
                now_utc = datetime.now(timezone.utc)
                cutoff_24h = now_utc - timedelta(hours=24)
                notifications_to_send = []

                # Step 1: Acquire DB connection ONLY for fast SQL queries, then release immediately
                async with pg_pool.acquire() as conn:
                    rows = await conn.fetch('''
                        SELECT user_id, first_name, balance 
                        FROM users 
                        WHERE last_interacted <= $1 
                          AND balance > 0 
                          AND (last_decay_at IS NULL OR last_decay_at <= $1)
                        LIMIT 50;
                    ''', cutoff_24h)

                    for row in rows:
                        uid = row["user_id"]
                        fname = row["first_name"] or "User"
                        bal = row["balance"]

                        async with user_cache_lock:
                            if uid in user_cache:
                                continue

                        if bal <= 0:
                            continue

                        decay = int(bal * 0.01)
                        if decay == 0:
                            decay = 2
                        decay = min(bal, decay)

                        new_bal = await conn.fetchval('''
                            UPDATE users 
                            SET balance = balance - $1, 
                                last_decay_at = NOW() 
                            WHERE user_id = $2
                            RETURNING balance;
                        ''', decay, uid)

                        notifications_to_send.append((uid, fname, decay, new_bal))

                # Step 2: DB connection is now FREE! Send DMs and sleep outside the DB block
                for uid, fname, decay, new_bal in notifications_to_send:
                    dm_alert = (
                        f"⚠️ <b>INACTIVITY PENALTY!</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"Hey <b>{fname}</b>, you have been inactive on the bot for the last 24 hours!\n"
                        f"📉 <b>Penalty Deducted:</b> <code>-{decay:,} Coins</code> (1% Wealth)\n"
                        f"💰 <b>Remaining Balance:</b> <code>{new_bal:,} Coins</code>\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"💡 <i>Chat with Tanya or claim /daily to stay active and protect your coins!</i>"
                    )
                    await send_dm_notification(uid, dm_alert)
                    await asyncio.sleep(0.3)

        except Exception as e:
            logger.error(f"Error in inactivity wealth decay worker: {e}")

        await asyncio.sleep(300)  # Har 5 minute me check hoga
async def periodic_cache_flush_task():
    """Flushes user interactions and analytics from RAM to DB every 4 minutes."""
    while True:
        await asyncio.sleep(240)
        global pg_pool
        if not pg_pool:
            continue

        async with user_cache_lock:
            cached_users = list(user_cache.items())
            user_cache.clear()

        async with analytics_lock:
            flushed_replies = analytics_cache["replies"]
            flushed_errors = analytics_cache["errors"]
            flushed_unique_users = len(analytics_cache["active_users"])
            analytics_cache["replies"] = 0
            analytics_cache["errors"] = 0
            analytics_cache["active_users"].clear()

        today = datetime.now(IST).date()

        try:
            async with pg_pool.acquire() as conn:
                if cached_users:
                    records = [
                        (uid, data["username"], data["first_name"], data["last_interacted"])
                        for uid, data in cached_users
                    ]
                    await conn.executemany('''
                        INSERT INTO users (user_id, username, first_name, last_interacted)
                        VALUES ($1, $2, $3, $4)
                        ON CONFLICT (user_id) DO UPDATE SET
                            username = EXCLUDED.username,
                            first_name = EXCLUDED.first_name,
                            last_interacted = EXCLUDED.last_interacted;
                    ''', records)

                if flushed_replies > 0 or flushed_errors > 0 or flushed_unique_users > 0:
                    await conn.execute('''
                        INSERT INTO bot_daily_analytics (record_date, total_replies, total_errors, unique_users)
                        VALUES ($1, $2, $3, $4)
                        ON CONFLICT (record_date) DO UPDATE SET
                            total_replies = bot_daily_analytics.total_replies + EXCLUDED.total_replies,
                            total_errors = bot_daily_analytics.total_errors + EXCLUDED.total_errors,
                            unique_users = bot_daily_analytics.unique_users + EXCLUDED.unique_users;
                    ''', today, flushed_replies, flushed_errors, flushed_unique_users)
        except Exception as e:
            logger.error(f"Error during cache flush: {e}")

async def group_gdp_sync_and_cleanup_task():
    """
    Runs every 5 minutes:
    1. Flushes modified (dirty) Group GDPs from RAM to PostgreSQL in a single batch.
    2. Evicts groups from RAM that had no rob/kill/send activity for 1 hour (3600s).
    3. Refreshes Top 10 Group GDP leaderboard cache.
    """
    while True:
        await asyncio.sleep(300)  # 5 minutes
        global pg_pool, top_groups_cache
        if not pg_pool:
            continue

        now = time.time()
        records_to_sync = []

        async with group_gdp_lock:
            expired_chat_ids = []
            for cid, data in group_gdp_cache.items():
                if data.get("dirty", False):
                    records_to_sync.append((cid, data["title"], data["gdp"]))
                    data["dirty"] = False

                # 1 Hour (3600 seconds) inactivity check -> remove from RAM
                if now - data.get("last_active", now) >= 3600:
                    expired_chat_ids.append(cid)

            for cid in expired_chat_ids:
                del group_gdp_cache[cid]

        try:
            async with pg_pool.acquire() as conn:
                if records_to_sync:
                    await conn.executemany('''
                        INSERT INTO groups_gdp (chat_id, title, gdp, is_active, updated_at)
                        VALUES ($1, $2, $3, TRUE, NOW())
                        ON CONFLICT (chat_id) DO UPDATE SET
                            title = EXCLUDED.title,
                            gdp = GREATEST(groups_gdp.gdp, EXCLUDED.gdp),
                            is_active = TRUE,
                            updated_at = NOW();
                    ''', records_to_sync)

                # Refresh Top 10 Group GDP Cache from DB
                top_rows = await conn.fetch('''
                    SELECT chat_id, title, gdp FROM groups_gdp ORDER BY gdp DESC LIMIT 10;
                ''')
                async with group_gdp_lock:
                    top_groups_cache = [
                        {"chat_id": r["chat_id"], "title": r["title"] or "Group Chat", "gdp": r["gdp"] or 0}
                        for r in top_rows
                    ]
        except Exception as e:
            logger.error(f"Error in group_gdp_sync_and_cleanup_task: {e}")


async def self_ping_task():
    """Pings webhook service periodically to prevent server cold boot."""
    await asyncio.sleep(15)
    ping_url = f"{BASE_URL}/ping"
    logger.info(f"Self-Ping service active: {ping_url}")

    while True:
        try:
            if http_session and BASE_URL:
                async with http_session.get(ping_url, timeout=10) as resp:
                    pass
        except Exception:
            pass
        await asyncio.sleep(60)

async def multiplayer_games_cleanup_task():
    """
    Runs every 2 minutes (120s).
    Scans RAM for multiplayer games (PvP Duels & Tic-Tac-Toe) inactive for 20+ minutes (1200s),
    cancels them, refunds stakes if needed, edits the game message, and notifies the group.
    """
    await asyncio.sleep(20)
    logger.info("⏳ Starting Multiplayer Games Auto-Cancel Scanner (Every 2 mins)...")

    while True:
        try:
            now = time.time()

            # 1. Scan Active PvP Duels
            expired_duels = [
                did for did, ddata in list(active_duels.items())
                if (now - ddata.get("created_at", now)) >= 1200 or ddata.get("resolved", False)
            ]
            for did in expired_duels:
                duel = active_duels.pop(did, None)
                if not duel or duel.get("resolved", False):
                    continue
                duel["resolved"] = True
                cid = duel.get("chat_id")
                mid = duel.get("message_id")
                c_name = html.escape(duel.get("creator_name", "Player"))
                bet = duel.get("bet", 0)

                cancel_notice = (
                    f"⏰ <b>Duel Challenge Auto-Cancelled!</b>\n"
                    f"<b>{c_name}</b>'s duel challenge of <code>{bet:,} Coins</code> has been cancelled due to 20 minutes of inactivity."
                )
                if cid:
                    if mid:
                        try:
                            await ptb_app.bot.edit_message_text(
                                chat_id=cid,
                                message_id=mid,
                                text=cancel_notice,
                                parse_mode=ParseMode.HTML
                            )
                        except Exception:
                            pass
                    try:
                        await ptb_app.bot.send_message(
                            chat_id=cid,
                            text=cancel_notice,
                            reply_to_message_id=mid if mid else None,
                            parse_mode=ParseMode.HTML
                        )
                    except Exception:
                        pass

            # 2. Scan Active Tic-Tac-Toe Games
            expired_ttt = [
                gid for gid, gdata in list(active_ttt.items())
                if (now - gdata.get("created_at", now)) >= 1200 or gdata.get("resolved", False)
            ]
            for gid in expired_ttt:
                game = active_ttt.pop(gid, None)
                if not game or game.get("resolved", False):
                    continue
                game["resolved"] = True
                cid = game.get("chat_id")
                mid = game.get("message_id")
                px_id = game.get("player_x")
                po_id = game.get("player_o")
                px_name = html.escape(game.get("player_x_name", "Player X"))
                bet = game.get("bet", 0)

                # If match had already started and coins were deducted, refund both players
                if game.get("active") and px_id and po_id and pg_pool:
                    try:
                        async with pg_pool.acquire() as conn:
                            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2;", bet, px_id)
                            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2;", bet, po_id)
                    except Exception as e:
                        logger.error(f"Error refunding expired TTT game {gid}: {e}")
                    cancel_notice = (
                        f"⏰ <b>Tic-Tac-Toe Match Auto-Cancelled!</b>\n"
                        f"The match was cancelled due to 20 minutes of inactivity. Stakes of <code>{bet:,} Coins</code> have been refunded to both players."
                    )
                else:
                    cancel_notice = (
                        f"⏰ <b>Tic-Tac-Toe Challenge Auto-Cancelled!</b>\n"
                        f"<b>{px_name}</b>'s challenge of <code>{bet:,} Coins</code> has been cancelled due to 20 minutes of inactivity."
                    )

                if cid:
                    if mid:
                        try:
                            await ptb_app.bot.edit_message_text(
                                chat_id=cid,
                                message_id=mid,
                                text=cancel_notice,
                                parse_mode=ParseMode.HTML
                            )
                        except Exception:
                            pass
                    try:
                        await ptb_app.bot.send_message(
                            chat_id=cid,
                            text=cancel_notice,
                            reply_to_message_id=mid if mid else None,
                            parse_mode=ParseMode.HTML
                        )
                    except Exception:
                        pass

        except Exception as e:
            logger.error(f"Error in multiplayer_games_cleanup_task: {e}")

        await asyncio.sleep(120)  # Scan every 2 minutes
# ---------------------------------------------------------
# APPLICATION SETUP
# ---------------------------------------------------------
ptb_app = Application.builder().token(BOT_TOKEN).build()

# 1. Core Navigation & Help Handlers
# 1. Core Navigation & Help Handlers
ptb_app.add_handler(CommandHandler("start", cmd_start))
ptb_app.add_handler(CommandHandler("help", cmd_help))
ptb_app.add_handler(CommandHandler(["referral", "ref", "refer"], cmd_referral))
ptb_app.add_handler(CommandHandler(["tr", "translate"], cmd_tr))
ptb_app.add_handler(CommandHandler("paidbrd", cmd_paidbrd))  # Hidden Paid Broadcast
# 2. Economy & Games Handlers
# 2. Economy & Games Handlers
# 2. Economy & Games Handlers
ptb_app.add_handler(CommandHandler(["games", "casino"], cmd_games))
ptb_app.add_handler(CommandHandler("gamble", cmd_gamble))
ptb_app.add_handler(CommandHandler("duel", cmd_duel))
ptb_app.add_handler(CommandHandler(["ttt", "tictactoe"], cmd_ttt))
ptb_app.add_handler(CommandHandler("kill", cmd_kill))

# Wordle Handlers
ptb_app.add_handler(CommandHandler("wordle4", cmd_wordle4))
ptb_app.add_handler(CommandHandler("wordle5", cmd_wordle5))
ptb_app.add_handler(CommandHandler("wordle6", cmd_wordle6))
ptb_app.add_handler(CommandHandler("end", cmd_end))
ptb_app.add_handler(CommandHandler(["bal", "balance"], cmd_balance))
ptb_app.add_handler(CommandHandler("addfd", cmd_addfd))
ptb_app.add_handler(CommandHandler("widfd", cmd_widfd))
ptb_app.add_handler(CommandHandler("send", cmd_send))
ptb_app.add_handler(CommandHandler("rob", cmd_rob))
ptb_app.add_handler(CommandHandler("protect", cmd_protect))
ptb_app.add_handler(CommandHandler("daily", cmd_daily))
ptb_app.add_handler(CommandHandler(["top", "leaderboard"], cmd_leaderboard))
ptb_app.add_handler(CommandHandler(["stats", "gdp"], cmd_stats))
# Social Interaction Handlers (Group & Reply Only)
ptb_app.add_handler(CommandHandler("kiss", cmd_kiss))
ptb_app.add_handler(CommandHandler("love", cmd_love))
ptb_app.add_handler(CommandHandler("breakup", cmd_breakup))
ptb_app.add_handler(CommandHandler(["shortout", "shoutout"], cmd_shoutout))
ptb_app.add_handler(CommandHandler("funny", cmd_funny))
ptb_app.add_handler(CommandHandler("childish", cmd_childish))
ptb_app.add_handler(CommandHandler("punch", cmd_punch))
ptb_app.add_handler(CommandHandler("slap", cmd_slap))
ptb_app.add_handler(CommandHandler("hug", cmd_hug))
ptb_app.add_handler(CommandHandler("marry", cmd_marry))
ptb_app.add_handler(CommandHandler("divorce", cmd_divorce))

# 3. Interactive Callback Query Router
ptb_app.add_handler(CallbackQueryHandler(handle_callback_queries))

# 4. Message & Member Handlers
# 4. Message & Member Handlers
ptb_app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_chat_members))
ptb_app.add_handler(ChatJoinRequestHandler(handle_chat_join_request))
ptb_app.add_handler(ChatMemberHandler(handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
ptb_app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (~filters.COMMAND), handle_private_message))
ptb_app.add_handler(MessageHandler((filters.ChatType.GROUPS | filters.ChatType.SUPERGROUP) & (~filters.COMMAND), handle_group_message))

async def ptb_error_handler(update: object, context: CallbackContext):
    logger.error("PTB internal error encountered:", exc_info=context.error)
    await increment_analytics(error=True)

ptb_app.add_error_handler(ptb_error_handler)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_session
    logger.info("Booting Tanya AI & Virtual Casino Engine...")

    timeout = aiohttp.ClientTimeout(total=60)
    connector = aiohttp.TCPConnector(limit_per_host=15)
    http_session = aiohttp.ClientSession(connector=connector, timeout=timeout)

    await init_postgres()

    flush_task = asyncio.create_task(periodic_cache_flush_task())
    gdp_sync_task = asyncio.create_task(group_gdp_sync_and_cleanup_task())
    ping_task = asyncio.create_task(self_ping_task())
    reengage_task = asyncio.create_task(inactive_user_reengagement_task())
    decay_task = asyncio.create_task(inactivity_wealth_decay_task())
    scramble_task = asyncio.create_task(scrambled_word_challenge_task())
    mp_cleanup_task = asyncio.create_task(multiplayer_games_cleanup_task())    
    # Run hote hi sabhi social GIFs ko turant search karke cache karna
   # Run hote hi sabhi social GIFs aur Wordle dictionaries ko turant RAM me cache karna
    asyncio.create_task(preload_all_giphy_gifs())
    asyncio.create_task(load_wordle_dictionaries())

    # Initialize and set webhook
       # Initialize and set webhook
    # Initialize and set webhook
    await ptb_app.initialize()    
    await ptb_app.start()
    logger.info(f"Setting Telegram Webhook to: {WEBHOOK_URL}")
    await ptb_app.bot.set_webhook(
        url=WEBHOOK_URL,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False
    )
    logger.info("Webhook successfully registered with ALL_TYPES updates enabled!")
    try:
        bot_commands = [
            BotCommand("start", "Start Tanya & Main Menu"),
            BotCommand("tr", "Translate replied message to English"),
            BotCommand("referral", "Referral link & earn 1,000 coins"),
            BotCommand("help", "Complete Bot Guide & Rules"),
            BotCommand("games", "Casino & Mini-Games Arcade"),            
            BotCommand("gamble", "Quick 50/50 Casino Bet"),            
            BotCommand("duel", "Challenge a user to 1v1 PvP Duel"),
            BotCommand("ttt", "2-Player Tic-Tac-Toe with Bet"),
            BotCommand("kill", "Kill user in reply & get 200-1k coins"),
            BotCommand("wordle4", "Start 4-letter Wordle game"),
            BotCommand("wordle5", "Start 5-letter Wordle game"),
            BotCommand("wordle6", "Start 6-letter Wordle game"),
            BotCommand("end", "End current active Wordle game"),            
            BotCommand("bal", "Check Coin Balance & Rank"),
            BotCommand("addfd", "Deposit to Safe FD (Max 80%)"),
            BotCommand("widfd", "Withdraw from FD (4% early fee)"),
            BotCommand("daily", "Claim Daily Free Coins"),
            BotCommand("send", "Transfer Coins (Max: 10k)"),
            BotCommand("rob", "Steal Coins from Someone (Max: 5k)"),
            BotCommand("protect", "Buy Theft Protection Shield"),
            BotCommand("top", "Richest Players Leaderboard"),
            BotCommand("stats", "Check Group GDP & Top 10 Groups Rank"),
            # Naye Group Reaction Commands            
            # Naye Group Reaction Commands
            BotCommand("kiss", "Send a cute kiss GIF reaction"),
            BotCommand("love", "Send romantic love reaction"),
            BotCommand("hug", "Give a warm cuddle hug"),
            BotCommand("slap", "Funny comedy slap"),
            BotCommand("punch", "Playful punch reaction"),
            BotCommand("breakup", "Dramatic breakup GIF"),
            BotCommand("marry", "Propose & marry someone"),
            BotCommand("divorce", "Serve funny divorce papers"),
            BotCommand("funny", "Laugh out loud reaction"),
            BotCommand("childish", "Pout & childish reaction"),
            BotCommand("shortout", "Give a massive cheer shoutout"),
        ]        
        await ptb_app.bot.set_my_commands(bot_commands)        
        logger.info("Successfully registered Bot Commands menu in Telegram UI!")
    except Exception as e:
        logger.error(f"Failed to set bot commands menu: {e}")

    yield

    logger.info("Shutting down background tasks...")
    flush_task.cancel()
    gdp_sync_task.cancel()
    ping_task.cancel()
    reengage_task.cancel()
    decay_task.cancel()
    scramble_task.cancel()
    mp_cleanup_task.cancel()    
    if http_session:
        await http_session.close()
    if pg_pool:
        await pg_pool.close()

    await ptb_app.stop()
    await ptb_app.shutdown()

fastapi_app = FastAPI(lifespan=lifespan)

@fastapi_app.post("/webhook")
async def telegram_webhook(request: Request):
    """Processes incoming Telegram updates asynchronously in the background."""
    try:
        update_data = await request.json()
        update = Update.de_json(update_data, ptb_app.bot)
        if update:
            # Task ko background me daal diya taaki 200 OK turant chala jaye
            asyncio.create_task(ptb_app.process_update(update))
    except Exception as e:
        logger.error(f"Error processing webhook update: {e}", exc_info=True)
    
    # Telegram ko instantly 200 OK response mil jayega
    return Response(content='{"status":"ok"}', status_code=200, media_type="application/json")
@fastapi_app.get("/ping")
async def health_ping():
    """Health check endpoint for the self-pinger."""
    return {"status": "alive", "timestamp": time.time()}

@fastapi_app.get("/analytics")
async def fetch_analytics_summary():
    """Retrieves operational analytics from the database."""
    global pg_pool
    if not pg_pool:
        return {"status": "error", "message": "Database disconnected"}

    try:
        async with pg_pool.acquire() as conn:
            rows = await conn.fetch('''
                SELECT record_date, total_replies, total_errors, unique_users
                FROM bot_daily_analytics
                ORDER BY record_date DESC
                LIMIT 30;
            ''')
            return {
                "status": "success",
                "analytics": [
                    {
                        "date": str(r["record_date"]),
                        "total_replies": r["total_replies"],
                        "total_errors": r["total_errors"],
                        "unique_users": r["unique_users"]
                    }
                    for r in rows
                ]
            }
    except Exception as e:
        return {"status": "error", "message": str(e)}

@fastapi_app.get("/")
async def root_index():
    return {
        "bot": "Tanya AI & Arcade Gaming System",
        "status": "online",
        "version": "2.1.0",
        "features": ["DM Notifications", "Mines", "Aviator", "Lucky Slots", "Coin Flip", "High-Low", "Lucky 7 Dice", "PvP Duels"]
    }
