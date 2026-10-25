"""
notification.py
================
Ultra-Fast RAM + Valkey (Redis) + PostgreSQL Smart Notification & Group Analytics Engine.

Key Features:
1. High-Speed RAM Buffer + 2-Minute Valkey Pipeline Sync (Zero DB load per message).
2. 3-Day Rolling Analytics in Valkey & 7-Day 72-Slot (20-min each) User Activity Histogram.
3. Top 4 Active Slots Group Auto-Mention (Minimum 2-hour / 6-slot gap between slots,
   country/timezone-aware playful Good Morning/Afternoon/Evening/Night messages in English).
4. /get_notified (Reply in GC): Watchlist DM alerts when a target user comes online in that GC,
   with a strict 30-minute cooldown per target/subscriber and interactive mute/settings buttons.
5. Personalized Friend Notifications (Top 5 Mutual Engagers):
   - Computes Top 10 users who replied to User A & Top 10 users User A replied to.
   - Requires >= 5 replies in BOTH directions (mutual engagement).
   - Selects Top 5 engagers and caches in User A's PostgreSQL personal settings + 3-hour RAM cache.
   - Triggers when User A messages in a GC after >= 6 hours of inactivity (max once per 6 hours).
6. /notification Command: Manage/Toggle Get-Notified & Personalized notifications with user-locked buttons.
7. /ranking Command (Admin Only): Live 24-hour GC stats (Total messages, Total active users,
   Top 10 users with clickable blue HTML mentions) merging Valkey hourly buckets + live RAM buffer.
"""

import os
import time
import json
import html
import random
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Set, Tuple, Any
from collections import defaultdict

import asyncpg
import redis.asyncio as aioredis
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatMember
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    CallbackContext
)

logger = logging.getLogger("TanyaNotificationEngine")

# =========================================================
# 🔗 CONFIGURATION & CONSTANTS
# =========================================================
VALKEY_URI = os.getenv(
    "VALKEY_URI",
    "rediss://default:AVNS_lZVvN0QRZAXRH4VA17m@valkey-39013870-preetibrb7-6457.c.aivencloud.com:12734"
)

# Sync & Cache Intervals
RAM_TO_VALKEY_SYNC_INTERVAL = 120          # 2 minutes batch sync to Valkey
SLOT_MENTION_CHECK_INTERVAL = 300          # Check every 5 minutes for 20-min slot mentions
SETTINGS_CACHE_TTL = 10800                 # 3 hours (10,800s) RAM cache for user settings
ADMIN_CACHE_TTL = 600                      # 10 minutes RAM cache for GC admin IDs
DM_NOTIFICATION_COOLDOWN = 1800            # 30 minutes (1,800s) minimum gap between 2 DM notifications
PERSONAL_INACTIVE_THRESHOLD = 21600        # 6 hours (21,600s) inactivity required for friend alert
TOP5_RECALC_INTERVAL = 21600               # 6 hours (21,600s) max recalculation frequency for Top 5

# Valkey TTLs
TTL_3_DAYS = 3 * 86400                     # 72 hours retention for GC activity & replies
TTL_7_DAYS = 7 * 86400                     # 7 days retention for 72-slot activity histogram
TTL_30_HOURS = 30 * 3600                   # 30 hours retention for hourly 24h /ranking buckets

# 72 Slots Config (24 hours = 1440 mins -> 72 slots of 20 mins each)
TOTAL_SLOTS = 72
SLOT_MINUTES = 20
MIN_SLOT_GAP = 6                           # 6 slots * 20 mins = 120 mins (2 hours minimum gap)

# Minimum Mutual Replies for Top 5 Friend Selection
MIN_MUTUAL_REPLIES = 5

# =========================================================
# 🧠 GLOBAL CONNECTIONS & IN-MEMORY CACHES
# =========================================================
valkey_client: Optional[aioredis.Redis] = None
pg_pool_ref: Optional[asyncpg.Pool] = None
bot_app_ref: Optional[Application] = None

# Lock for flushing RAM buffer safely
ram_buffer_lock = asyncio.Lock()

# 1. Unsynced RAM Analytics Buffer (Flushed every 2 minutes to Valkey)
# Structure:
# {
#   chat_id: {
#     "title": str,
#     "hourly_msgs": {hour_bucket_str: int},
#     "hourly_user_msgs": {hour_bucket_str: {user_id: int}},
#     "daily_user_msgs": {day_str: {user_id: int}},
#     "slots": {user_id: {slot_idx: int}},
#     "replies_sent": {day_str: {(sender_id, replied_to_id): int}},
#     "user_meta": {user_id: {"name": str, "lang": str, "last_ts": int}}
#   }
# }
ram_gc_buffer: Dict[int, Dict[str, Any]] = {}

# 2. Live User Last-Activity Timestamp in RAM: {(chat_id, user_id): last_msg_unix_ts}
ram_last_active: Dict[Tuple[int, int], int] = {}

# 3. Live User Metadata in RAM: {user_id: {"name": str, "lang": str}}
ram_user_profile: Dict[int, Dict[str, str]] = {}

# 4. Live Group Titles in RAM: {chat_id: str}
ram_group_titles: Dict[int, str] = {}

# 5. User Notification Settings 3-Hour RAM Cache:
# {user_id: {"data": dict, "expires_at": float}}
user_settings_cache: Dict[int, Dict[str, Any]] = {}

# 6. Watchlist Subscribers Index in RAM (Synced with Valkey):
# {(chat_id, target_user_id): Set[subscriber_user_id]}
watchlist_ram_index: Dict[Tuple[int, int], Set[int]] = defaultdict(set)
watchlist_loaded_chats: Set[int] = set()

# 7. Cooldown Trackers in RAM:
# {(subscriber_id, target_id): last_dm_sent_ts} -> Ensures 30-min gap between DM notifications
dm_pair_cooldown: Dict[Tuple[int, int], float] = {}
# {(chat_id, target_id): last_watchlist_batch_ts} -> Prevents 10 msgs in 2 mins from triggering 10 scans
target_watchlist_cooldown: Dict[Tuple[int, int], float] = {}
# {(chat_id, user_id): last_personal_ broadcast_ts} -> Max once every 6 hours
personal_notify_cooldown: Dict[Tuple[int, int], float] = {}
# {(chat_id, user_id, day_str, slot_idx): bool} -> Prevents duplicate GC mention in the same slot
slot_mention_sent: Dict[Tuple[int, int, str, int], bool] = {}

# 8. Group Admin Cache in RAM: {chat_id: {"admins": Set[int], "expires_at": float}}
gc_admin_cache: Dict[int, Dict[str, Any]] = {}

# =========================================================
# 🌍 TIMEZONE & CHULBULE (PLAYFUL) MESSAGES ENGINE
# =========================================================
# Maps Telegram language_code prefixes to UTC offset in minutes (Default: India IST +330 mins)
LANGUAGE_TZ_OFFSET_MINS: Dict[str, int] = {
    "hi": 330,   # India (IST UTC+5:30)
    "en": 330,   # Default English in this bot's demographic -> IST UTC+5:30
    "ur": 300,   # Pakistan (PKT UTC+5:00)
    "bn": 330,   # India / Bangladesh (+330 / +360)
    "NP": 345,   # Nepal (UTC+5:45)
    "ta": 330,   # India
    "te": 330,   # India
    "mr": 330,   # India
    "gu": 330,   # India
    "kn": 330,   # India
    "ml": 330,   # India
    "pa": 330,   # India
    "id": 420,   # Indonesia (UTC+7:00)
    "ru": 180,   # Russia (MSK UTC+3:00)
    "tr": 180,   # Turkey (UTC+3:00)
    "ar": 180,   # Riyadh / AST (UTC+3:00)
    "fa": 210,   # Iran (UTC+3:30)
    "uz": 300,   # Uzbekistan (UTC+5:00)
    "vi": 420,   # Vietnam (UTC+7:00)
    "th": 420,   # Thailand (UTC+7:00)
    "ms": 480,   # Malaysia (UTC+8:00)
    "zh": 480,   # China (UTC+8:00)
    "ja": 540,   # Japan (UTC+9:00)
    "ko": 540,   # Korea (UTC+9:00)
    "de": 60,    # Central Europe (UTC+1:00)
    "fr": 60,    # France (UTC+1:00)
    "es": -300,  # LatAm / Spain
    "pt": -180,  # Brazil (UTC-3:00)
}

PLAYFUL_SLOT_MESSAGES = {
    "morning": [
        "Hey {mention}! Good morning 🌞 Where are you hiding? Grab your morning tea and let's start some fun conversations here! ☕✨",
        "Rise and shine {mention}! 🌅 Good morning! The group chat feels way too quiet without your energy—come say hi! 💬💫",
        "Hey {mention}! A very Good Morning to you 🌻✨ Wake up sleepyhead, everyone is waiting to vibe with you here! 🥰",
        "Good morning {mention}! ☀️ Ready to make today awesome? Drop a message and let's get this chat rolling! 🔥"
    ],
    "afternoon": [
        "Hey {mention}! Good afternoon ☀️ Taking a break yet? Come join the conversation and tell us how your day is going! 💬✨",
        "Good afternoon {mention}! 🌤️ Don't let the afternoon laziness win—hop in here and let's have some fun chats! 😄🔥",
        "Hey {mention}! ☀️ It's your usual hangout time! Good afternoon, where are you? Let's start a cool topic here! 🎯",
        "Good afternoon {mention}! 🍹 Drop whatever you're doing for a minute and come vibe with the squad! ✨"
    ],
    "evening": [
        "Hey {mention}! Good evening 🌆✨ Done with the busy day? Grab some snacks and let's spice up the group chat! 🍿🔥",
        "Good evening {mention}! 🌇 This is your prime time to be online—where are you? Let's start some lively conversations! 💬💫",
        "Hey {mention}! 🌆 Evening vibes are incomplete without you here! Come on in and say hello to everyone! 🥰✨",
        "Good evening {mention}! ☕✨ Time to unwind and chat—what's on your mind tonight? Let's talk! 🎉"
    ],
    "night": [
        "Hey {mention}! Good night or should I say happy late-night owl hours? 🌙✨ Where are you? Let's start some midnight conversations! 🦉💬",
        "Hey {mention}! 🌌 Still awake? We know this is your favorite time to be active—come join the late-night squad here! 🔥✨",
        "Good night vibes {mention}! 🌙✨ Before you drift off to sleep, drop by and chat with us for a bit! 💫",
        "Hey night owl {mention}! 🌠 The stars are out and it's your usual chat slot—where are you hiding? Come say hi! 😄✨"
    ]
}


# =========================================================
# 🛠️ HELPER UTILITIES
# =========================================================
def format_blue_mention(user_id: int, first_name: str) -> str:
    """Creates a clean, escaped blue clickable Telegram mention."""
    safe_name = html.escape((first_name or "Friend").strip() or "Friend")
    return f'<a href="tg://user?id={user_id}">{safe_name}</a>'


def get_utc_slot_index(ts: Optional[float] = None) -> int:
    """
    Divides 24 hours (1440 minutes) into 72 slots of 20 minutes each (0 to 71).
    """
    dt = datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc)
    total_minutes = dt.hour * 60 + dt.minute
    return total_minutes // SLOT_MINUTES


def get_circular_slot_distance(slot_a: int, slot_b: int) -> int:
    """Calculates the shortest distance between two slots on a 72-slot 24h circle."""
    diff = abs(slot_a - slot_b)
    return min(diff, TOTAL_SLOTS - diff)


def select_top_4_slots_with_2hr_gap(slot_counts: Dict[int, int]) -> List[int]:
    """
    Selects up to 4 most active slots out of 72 such that ANY two selected slots
    have at least a 2-hour gap (6 slots * 20 mins = 120 mins circular distance).
    """
    if not slot_counts:
        return []

    # Filter positive counts and sort descending by activity count
    sorted_slots = sorted(
        [(int(s), int(c)) for s, c in slot_counts.items() if int(c) > 0],
        key=lambda x: x[1],
        reverse=True
    )

    selected: List[int] = []
    for slot_idx, _ in sorted_slots:
        if all(get_circular_slot_distance(slot_idx, existing) >= MIN_SLOT_GAP for existing in selected):
            selected.append(slot_idx)
            if len(selected) >= 4:
                break

    return selected


def get_time_of_day_period(lang_code: Optional[str] = None) -> str:
    """Determines morning, afternoon, evening, or night in the user's local timezone."""
    code = (lang_code or "hi").split("-")[0].lower()
    offset_mins = LANGUAGE_TZ_OFFSET_MINS.get(code, 330)  # Default IST (+5:30)
    user_tz = timezone(timedelta(minutes=offset_mins))
    local_hour = datetime.now(user_tz).hour

    if 5 <= local_hour < 12:
        return "morning"
    elif 12 <= local_hour < 17:
        return "afternoon"
    elif 17 <= local_hour < 21:
        return "evening"
    else:
        return "night"


def default_user_notification_settings() -> Dict[str, Any]:
    """Default structure stored in PostgreSQL `users.notification_settings`."""
    return {
        "get_notify_enabled": True,
        "personal_notify_enabled": True,
        "muted_gcs": [],
        "muted_users_get_notify": [],
        "muted_users_personal": [],
        "top_engagers": {},             # {str(chat_id): [uid1, uid2, ...]}
        "top_engagers_updated_at": {}   # {str(chat_id): unix_ts}
    }


# =========================================================
# 🗄️ INITIALIZATION (POSTGRESQL + VALKEY)
# =========================================================
async def init_notification_system(pg_pool: asyncpg.Pool, ptb_app: Application):
    """
    Initializes Valkey connection, ensures PostgreSQL `notification_settings` column exists,
    registers command/callback handlers, and starts background sync & slot workers.
    """
    global valkey_client, pg_pool_ref, bot_app_ref
    pg_pool_ref = pg_pool
    bot_app_ref = ptb_app

    # 1. Ensure PostgreSQL column exists on `users` table
    try:
        async with pg_pool_ref.acquire() as conn:
            await conn.execute('''
                ALTER TABLE users 
                ADD COLUMN IF NOT EXISTS notification_settings JSONB DEFAULT '{}'::jsonb;
            ''')
        logger.info("✅ PostgreSQL `users.notification_settings` column verified.")
    except Exception as e:
        logger.error(f"Error ensuring notification_settings column in PostgreSQL: {e}")

    # 2. Initialize Valkey (Redis) Async Client
    try:
        valkey_client = aioredis.from_url(
            VALKEY_URI,
            decode_responses=True,
            socket_timeout=10.0,
            socket_connect_timeout=10.0,
            ssl_cert_reqs=None
        )
        await valkey_client.ping()
        logger.info("🚀 Connected to Aiven Valkey (Redis) Analytics Engine successfully!")
    except Exception as e:
        logger.error(f"⚠️ Failed to connect to Valkey: {e}")

    # 3. Register Telegram Handlers
    register_notification_handlers(ptb_app)

    # 4. Start Background Workers
    asyncio.create_task(valkey_batch_sync_worker())
    asyncio.create_task(slot_mention_scheduler_worker())


# =========================================================
# 🛡️ USER SETTINGS CACHE (3-HOUR RAM TTL + POSTGRESQL)
# =========================================================
async def get_user_notification_settings(user_id: int) -> Dict[str, Any]:
    """
    Fetches user's notification settings from 3-hour RAM cache first.
    Hits PostgreSQL only on cache miss.
    """
    now = time.time()
    cached = user_settings_cache.get(user_id)
    if cached and cached["expires_at"] > now:
        return cached["data"]

    settings = default_user_notification_settings()
    if pg_pool_ref:
        try:
            async with pg_pool_ref.acquire() as conn:
                raw_json = await conn.fetchval(
                    "SELECT notification_settings FROM users WHERE user_id = $1;",
                    user_id
                )
                if raw_json:
                    parsed = json.loads(raw_json) if isinstance(raw_json, str) else dict(raw_json)
                    for k, v in default_user_notification_settings().items():
                        if k not in parsed:
                            parsed[k] = v
                    settings = parsed
        except Exception as e:
            logger.error(f"Error loading notification_settings for {user_id}: {e}")

    user_settings_cache[user_id] = {
        "data": settings,
        "expires_at": now + SETTINGS_CACHE_TTL
    }
    return settings


async def save_user_notification_settings(
    user_id: int,
    settings: Dict[str, Any],
    first_name: str = "User",
    username: str = ""
):
    """
    Updates the 3-hour RAM cache immediately and persists to PostgreSQL `users` table.
    """
    now = time.time()
    user_settings_cache[user_id] = {
        "data": settings,
        "expires_at": now + SETTINGS_CACHE_TTL
    }

    if not pg_pool_ref:
        return

    try:
        json_payload = json.dumps(settings)
        async with pg_pool_ref.acquire() as conn:
            await conn.execute('''
                INSERT INTO users (user_id, username, first_name, balance, notification_settings, last_interacted)
                VALUES ($1, $2, $3, 250, $4::jsonb, NOW())
                ON CONFLICT (user_id) DO UPDATE SET
                    notification_settings = $4::jsonb,
                    last_interacted = NOW();
            ''', user_id, username or "", first_name or "User", json_payload)
    except Exception as e:
        logger.error(f"Error saving notification_settings for {user_id}: {e}")


# =========================================================
# ⚡ REAL-TIME GROUP MESSAGE TRACKER (CALLED ON EVERY GC MSG)
# =========================================================
async def record_gc_message_activity(
    chat_id: int,
    chat_title: str,
    user_id: int,
    first_name: str,
    username: Optional[str] = None,
    lang_code: Optional[str] = "en",
    reply_to_user_id: Optional[int] = None,
    reply_to_first_name: Optional[str] = None,
    is_bot_reply: bool = False
):
    """
    Lightweight, non-blocking tracker called whenever a user sends a message in any group chat.
    1. Buffers message count, 20-min slot index, and reply relationship in Server RAM.
    2. Checks if user was inactive for >= 6 hours -> triggers Personalized Top 5 Friend DMs.
    3. Checks if anyone subscribed via /get_notified for this user -> triggers Watchlist DMs (30m cooldown).
    """
    now_ts = int(time.time())
    dt_utc = datetime.fromtimestamp(now_ts, tz=timezone.utc)
    day_str = dt_utc.strftime("%Y%m%d")
    hour_str = dt_utc.strftime("%Y%m%d%H")
    slot_idx = get_utc_slot_index(now_ts)

    clean_title = chat_title or "Group Chat"
    clean_name = first_name or "User"
    clean_lang = lang_code or "en"

    # Check previous last active timestamp BEFORE updating it
    prev_active_ts = ram_last_active.get((chat_id, user_id), 0)
    ram_last_active[(chat_id, user_id)] = now_ts
    ram_user_profile[user_id] = {"name": clean_name, "lang": clean_lang}
    ram_group_titles[chat_id] = clean_title

    if reply_to_user_id and reply_to_first_name and not is_bot_reply:
        if reply_to_user_id not in ram_user_profile:
            ram_user_profile[reply_to_user_id] = {"name": reply_to_first_name, "lang": "en"}

    # 1. Save in RAM Buffer (Zero DB/Valkey blocking)
    async with ram_buffer_lock:
        if chat_id not in ram_gc_buffer:
            ram_gc_buffer[chat_id] = {
                "title": clean_title,
                "hourly_msgs": defaultdict(int),
                "hourly_user_msgs": defaultdict(lambda: defaultdict(int)),
                "daily_user_msgs": defaultdict(lambda: defaultdict(int)),
                "slots": defaultdict(lambda: defaultdict(int)),
                "replies_sent": defaultdict(lambda: defaultdict(int)),
                "user_meta": {}
            }

        buf = ram_gc_buffer[chat_id]
        buf["title"] = clean_title
        buf["hourly_msgs"][hour_str] += 1
        buf["hourly_user_msgs"][hour_str][user_id] += 1
        buf["daily_user_msgs"][day_str][user_id] += 1
        buf["slots"][user_id][slot_idx] += 1
        buf["user_meta"][user_id] = {
            "name": clean_name,
            "lang": clean_lang,
            "last_ts": now_ts
        }

        if reply_to_user_id and reply_to_user_id != user_id and not is_bot_reply:
            buf["replies_sent"][day_str][(user_id, reply_to_user_id)] += 1
            if reply_to_user_id not in buf["user_meta"] and reply_to_first_name:
                buf["user_meta"][reply_to_user_id] = {
                    "name": reply_to_first_name,
                    "lang": "en",
                    "last_ts": 0
                }

    # 2. Fire-and-forget async notification checks (non-blocking for main bot)
    asyncio.create_task(
        _trigger_online_notifications(
            chat_id=chat_id,
            chat_title=clean_title,
            user_id=user_id,
            first_name=clean_name,
            prev_active_ts=prev_active_ts,
            now_ts=now_ts
        )
    )


# =========================================================
# 🔄 VALKEY (REDIS) 2-MINUTE PIPELINE SYNC WORKER
# =========================================================
async def flush_ram_to_valkey():
    """
    Flushes all accumulated RAM analytics to Valkey in a single atomic pipeline.
    Maintains 3-day TTL on daily/reply keys, 30-hour TTL on 24h hourly buckets,
    and 7-day TTL on 72-slot user histograms.
    """
    global valkey_client
    if not valkey_client:
        return

    async with ram_buffer_lock:
        if not ram_gc_buffer:
            return
        snapshot = ram_gc_buffer.copy()
        ram_gc_buffer.clear()

    try:
        pipe = valkey_client.pipeline(transaction=False)

        for chat_id, data in snapshot.items():
            title = data["title"]
            pipe.sadd("ntf:active_gcs", str(chat_id))
            pipe.hset("ntf:gc_titles", str(chat_id), title)

            # 1. Hourly buckets for 24-hour /ranking command
            for hour_str, total_cnt in data["hourly_msgs"].items():
                h_total_key = f"ntf:gc:{chat_id}:h_total:{hour_str}"
                pipe.incrby(h_total_key, total_cnt)
                pipe.expire(h_total_key, TTL_30_HOURS)

            for hour_str, user_map in data["hourly_user_msgs"].items():
                h_users_key = f"ntf:gc:{chat_id}:h_users:{hour_str}"
                for uid, cnt in user_map.items():
                    pipe.zincrby(h_users_key, cnt, str(uid))
                pipe.expire(h_users_key, TTL_30_HOURS)

            # 2. Daily buckets (3-Day Retention)
            for day_str, user_map in data["daily_user_msgs"].items():
                d_users_key = f"ntf:gc:{chat_id}:d_users:{day_str}"
                for uid, cnt in user_map.items():
                    pipe.zincrby(d_users_key, cnt, str(uid))
                pipe.expire(d_users_key, TTL_3_DAYS)

            # 3. 72-Slot (20-min) User Activity Histogram (7-Day Retention)
            for uid, slot_map in data["slots"].items():
                slot_key = f"ntf:gc:{chat_id}:u:{uid}:slots"
                for s_idx, cnt in slot_map.items():
                    pipe.hincrby(slot_key, str(s_idx), cnt)
                pipe.expire(slot_key, TTL_7_DAYS)

            # 4. Mutual Reply Pairs (3-Day Retention)
            for day_str, pair_map in data["replies_sent"].items():
                for (sender_id, target_id), cnt in pair_map.items():
                    # Sender replied to Target
                    out_key = f"ntf:gc:{chat_id}:u:{sender_id}:rep_out:{day_str}"
                    # Target received reply from Sender
                    in_key = f"ntf:gc:{chat_id}:u:{target_id}:rep_in:{day_str}"
                    pipe.zincrby(out_key, cnt, str(target_id))
                    pipe.expire(out_key, TTL_3_DAYS)
                    pipe.zincrby(in_key, cnt, str(sender_id))
                    pipe.expire(in_key, TTL_3_DAYS)

            # 5. User Metadata & Last Active Timestamp in GC (3-Day Retention)
            last_seen_key = f"ntf:gc:{chat_id}:last_seen"
            names_key = f"ntf:gc:{chat_id}:names"
            langs_key = f"ntf:gc:{chat_id}:langs"
            for uid, meta in data["user_meta"].items():
                if meta.get("name"):
                    pipe.hset(names_key, str(uid), meta["name"])
                if meta.get("lang"):
                    pipe.hset(langs_key, str(uid), meta["lang"])
                if meta.get("last_ts", 0) > 0:
                    pipe.hset(last_seen_key, str(uid), str(meta["last_ts"]))
            pipe.expire(last_seen_key, TTL_7_DAYS)
            pipe.expire(names_key, TTL_7_DAYS)
            pipe.expire(langs_key, TTL_7_DAYS)

        await pipe.execute()
        logger.debug(f"✅ Synced {len(snapshot)} active groups to Valkey.")
    except Exception as e:
        logger.error(f"Error syncing RAM buffer to Valkey: {e}")


async def valkey_batch_sync_worker():
    """Background task that syncs RAM analytics buffer to Valkey every 2 minutes."""
    await asyncio.sleep(30)
    logger.info("⏱️ Valkey 2-Minute Batch Sync Worker started.")
    while True:
        try:
            await flush_ram_to_valkey()
        except Exception as e:
            logger.error(f"Valkey sync worker error: {e}")
        await asyncio.sleep(RAM_TO_VALKEY_SYNC_INTERVAL)


# =========================================================
# 🤝 TOP 5 MUTUAL ENGAGERS CALCULATION ENGINE
# =========================================================
async def compute_and_cache_top5_engagers(chat_id: int, user_id: int) -> List[int]:
    """
    Calculates Top 5 mutual engagers for `user_id` in `chat_id` over the last 3 days:
    1. Fetches Top 10 users who replied to `user_id` (rep_in).
    2. Fetches Top 10 users whose messages `user_id` replied to (rep_out).
    3. Filters candidates where BOTH directions have >= 5 replies (`rep_in >= 5` AND `rep_out >= 5`).
    4. Selects Top 5 by combined reply score and caches in PostgreSQL personal settings + 3h RAM cache.
    """
    settings = await get_user_notification_settings(user_id)
    chat_key = str(chat_id)
    now_ts = int(time.time())

    last_calc = settings.get("top_engagers_updated_at", {}).get(chat_key, 0)
    cached_list = settings.get("top_engagers", {}).get(chat_key, [])

    # Return cached Top 5 if computed within the last 6 hours
    if (now_ts - last_calc) < TOP5_RECALC_INTERVAL and cached_list is not None:
        return cached_list

    rep_in_totals: Dict[int, int] = defaultdict(int)
    rep_out_totals: Dict[int, int] = defaultdict(int)

    # 1. Include any unsynced RAM reply counts
    async with ram_buffer_lock:
        if chat_id in ram_gc_buffer:
            for _, pair_map in ram_gc_buffer[chat_id]["replies_sent"].items():
                for (s_id, t_id), cnt in pair_map.items():
                    if s_id == user_id:
                        rep_out_totals[t_id] += cnt
                    elif t_id == user_id:
                        rep_in_totals[s_id] += cnt

    # 2. Fetch last 3 days from Valkey
    if valkey_client:
        try:
            now_dt = datetime.now(timezone.utc)
            days = [(now_dt - timedelta(days=d)).strftime("%Y%m%d") for d in range(3)]
            pipe = valkey_client.pipeline(transaction=False)
            for d_str in days:
                pipe.zrevrange(f"ntf:gc:{chat_id}:u:{user_id}:rep_in:{d_str}", 0, 19, withscores=True)
                pipe.zrevrange(f"ntf:gc:{chat_id}:u:{user_id}:rep_out:{d_str}", 0, 19, withscores=True)
            results = await pipe.execute()

            for idx in range(0, len(results), 2):
                in_list = results[idx] or []
                out_list = results[idx + 1] or []
                for other_uid_str, score in in_list:
                    rep_in_totals[int(other_uid_str)] += int(score)
                for other_uid_str, score in out_list:
                    rep_out_totals[int(other_uid_str)] += int(score)
        except Exception as e:
            logger.error(f"Error computing Top 5 from Valkey for user {user_id}: {e}")

    # Step A: Top 10 who replied to User A
    top10_in = {
        uid: cnt for uid, cnt in sorted(rep_in_totals.items(), key=lambda x: x[1], reverse=True)[:10]
    }
    # Step B: Top 10 whom User A replied to
    top10_out = {
        uid: cnt for uid, cnt in sorted(rep_out_totals.items(), key=lambda x: x[1], reverse=True)[:10]
    }

    # Step C: Intersect & enforce >= 5 replies in BOTH directions
    qualifying_candidates: List[Tuple[int, int]] = []
    for candidate_uid in set(top10_in.keys()).intersection(set(top10_out.keys())):
        in_cnt = top10_in[candidate_uid]
        out_cnt = top10_out[candidate_uid]
        if in_cnt >= MIN_MUTUAL_REPLIES and out_cnt >= MIN_MUTUAL_REPLIES:
            qualifying_candidates.append((candidate_uid, in_cnt + out_cnt))

    qualifying_candidates.sort(key=lambda x: x[1], reverse=True)
    top5_ids = [uid for uid, _ in qualifying_candidates[:5]]

    # Save to user's personal settings (3-hr RAM cache + PostgreSQL)
    if "top_engagers" not in settings:
        settings["top_engagers"] = {}
    if "top_engagers_updated_at" not in settings:
        settings["top_engagers_updated_at"] = {}

    settings["top_engagers"][chat_key] = top5_ids
    settings["top_engagers_updated_at"][chat_key] = now_ts

    u_name = ram_user_profile.get(user_id, {}).get("name", "User")
    await save_user_notification_settings(user_id, settings, first_name=u_name)
    return top5_ids


# =========================================================
# 🔔 ONLINE NOTIFICATION DISPATCHER (WATCHLIST + TOP 5 FRIENDS)
# =========================================================
async def _ensure_watchlist_loaded(chat_id: int, target_id: int):
    """Loads watchlist subscribers for (chat_id, target_id) from Valkey into RAM once."""
    if not valkey_client:
        return
    pair_key = (chat_id, target_id)
    if pair_key in watchlist_ram_index:
        return
    try:
        subs = await valkey_client.smembers(f"ntf:watchlist:{chat_id}:{target_id}")
        if subs:
            watchlist_ram_index[pair_key] = {int(s) for s in subs}
    except Exception as e:
        logger.debug(f"Watchlist load error: {e}")


async def _trigger_online_notifications(
    chat_id: int,
    chat_title: str,
    user_id: int,
    first_name: str,
    prev_active_ts: int,
    now_ts: int
):
    """
    Evaluates and dispatches:
    1. `/get_notified` Watchlist DMs (with 30-min cooldown per subscriber/target).
    2. Top 5 Mutual Engagers Personalized Friend DMs (if user was inactive in this GC for >= 6 hours).
    """
    if not bot_app_ref:
        return

    user_mention = format_blue_mention(user_id, first_name)
    safe_gc_title = html.escape(chat_title or "Group Chat")

    # ---------------------------------------------------------
    # PART 1: /get_notified Watchlist Notifications
    # ---------------------------------------------------------
    # Quick throttle: if target user already triggered a watchlist check in the last 60s, skip scan
    last_target_check = target_watchlist_cooldown.get((chat_id, user_id), 0)
    if (now_ts - last_target_check) >= 60:
        target_watchlist_cooldown[(chat_id, user_id)] = now_ts
        await _ensure_watchlist_loaded(chat_id, user_id)
        subscribers = list(watchlist_ram_index.get((chat_id, user_id), set()))

        for sub_id in subscribers:
            if sub_id == user_id:
                continue

            # Check 30-minute cooldown between notifications for (sub_id, user_id)
            last_sent = dm_pair_cooldown.get((sub_id, user_id), 0)
            if (now_ts - last_sent) < DM_NOTIFICATION_COOLDOWN:
                continue

            sub_settings = await get_user_notification_settings(sub_id)
            if not sub_settings.get("get_notify_enabled", True):
                continue
            if chat_id in sub_settings.get("muted_gcs", []):
                continue
            if user_id in sub_settings.get("muted_users_get_notify", []):
                continue

            # Mark 30-min cooldown before sending
            dm_pair_cooldown[(sub_id, user_id)] = now_ts

            dm_text = (
                f"🔔 <b>Hey! {user_mention} is online now!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📍 <b>Group:</b> {safe_gc_title}\n"
                f"💬 Go and chat with him/her right now! ✨"
            )
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔕 Turn off notifications for this user",
                        callback_data=f"ntf_goff_u_{sub_id}_{user_id}_{chat_id}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⚙️ Notification Settings",
                        callback_data=f"ntf_gset_m_{sub_id}_{user_id}_{chat_id}"
                    )
                ]
            ])

            try:
                await bot_app_ref.bot.send_message(
                    chat_id=sub_id,
                    text=dm_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard
                )
            except Exception as e:
                logger.debug(f"Could not send /get_notified DM to {sub_id}: {e}")

    # ---------------------------------------------------------
    # PART 2: Top 5 Mutual Engagers (Personalized Friend Alert)
    # ---------------------------------------------------------
    # If prev_active_ts wasn't in RAM yet, check Valkey once
    if prev_active_ts == 0 and valkey_client:
        try:
            val_ts = await valkey_client.hget(f"ntf:gc:{chat_id}:last_seen", str(user_id))
            if val_ts:
                prev_active_ts = int(val_ts)
        except Exception:
            pass

    # Trigger ONLY if user had previous activity in this GC AND was inactive for >= 6 hours (21,600s)
    if prev_active_ts > 0 and (now_ts - prev_active_ts) >= PERSONAL_INACTIVE_THRESHOLD:
        last_personal_trig = personal_notify_cooldown.get((chat_id, user_id), 0)
        if (now_ts - last_personal_trig) >= PERSONAL_INACTIVE_THRESHOLD:
            personal_notify_cooldown[(chat_id, user_id)] = now_ts

            top5_friends = await compute_and_cache_top5_engagers(chat_id, user_id)
            for friend_id in top5_friends:
                if friend_id == user_id:
                    continue

                # Check 30-min DM gap for this recipient/target pair
                last_dm = dm_pair_cooldown.get((friend_id, user_id), 0)
                if (now_ts - last_dm) < DM_NOTIFICATION_COOLDOWN:
                    continue

                friend_settings = await get_user_notification_settings(friend_id)
                if not friend_settings.get("personal_notify_enabled", True):
                    continue
                if chat_id in friend_settings.get("muted_gcs", []):
                    continue
                if user_id in friend_settings.get("muted_users_personal", []):
                    continue

                dm_pair_cooldown[(friend_id, user_id)] = now_ts

                friend_dm_text = (
                    f"🌟 <b>Your friend {user_mention} is online in {safe_gc_title}!</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"💬 If possible, go and chat with them! ✨"
                )
                friend_kb = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "🔕 Turn off personalized notification for this user",
                            callback_data=f"ntf_poff_u_{friend_id}_{user_id}_{chat_id}"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "⚙️ More Settings",
                            callback_data=f"ntf_pset_m_{friend_id}_{user_id}_{chat_id}"
                        )
                    ]
                ])

                try:
                    await bot_app_ref.bot.send_message(
                        chat_id=friend_id,
                        text=friend_dm_text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=friend_kb
                    )
                except Exception as e:
                    logger.debug(f"Could not send personalized friend DM to {friend_id}: {e}")


# =========================================================
# ⏰ 72-SLOT (20-MIN) PERSONALIZED GC MENTION SCHEDULER
# =========================================================
async def slot_mention_scheduler_worker():
    """
    Runs periodically inside each 20-minute slot.
    For active groups, finds users whose Top 4 active slots (with >= 2-hour gap)
    match the current 20-minute slot, and mentions them in the GC with a playful
    time-of-day greeting (Good morning / afternoon / evening / night) if they haven't
    already chatted recently.
    """
    await asyncio.sleep(60)
    logger.info("🌞 72-Slot Personalized Group Mention Scheduler started.")

    while True:
        try:
            if valkey_client and bot_app_ref:
                now_ts = int(time.time())
                dt_utc = datetime.fromtimestamp(now_ts, tz=timezone.utc)
                day_str = dt_utc.strftime("%Y%m%d")
                current_slot = get_utc_slot_index(now_ts)

                active_gcs = await valkey_client.smembers("ntf:active_gcs")
                for cid_str in active_gcs:
                    chat_id = int(cid_str)

                    # Fetch recently seen users in this GC from Valkey
                    last_seen_map = await valkey_client.hgetall(f"ntf:gc:{chat_id}:last_seen")
                    if not last_seen_map:
                        continue

                    names_map = await valkey_client.hgetall(f"ntf:gc:{chat_id}:names")
                    langs_map = await valkey_client.hgetall(f"ntf:gc:{chat_id}:langs")

                    mentioned_in_this_gc = 0

                    for uid_str, last_ts_str in last_seen_map.items():
                        if mentioned_in_this_gc >= 2:
                            # Max 2 auto-mentions per GC per cycle to keep chat natural and non-spammy
                            break

                        uid = int(uid_str)
                        last_ts = max(int(last_ts_str), ram_last_active.get((chat_id, uid), 0))

                        # Skip if user is already chatting in the last 25 minutes
                        # or hasn't been seen in > 3 days
                        if (now_ts - last_ts) < 1500 or (now_ts - last_ts) > TTL_3_DAYS:
                            continue

                        slot_sent_key = (chat_id, uid, day_str, current_slot)
                        if slot_mention_sent.get(slot_sent_key):
                            continue

                        # Fetch user's 72-slot histogram in this GC
                        raw_slots = await valkey_client.hgetall(f"ntf:gc:{chat_id}:u:{uid}:slots")
                        if not raw_slots:
                            continue

                        slot_counts = {int(k): int(v) for k, v in raw_slots.items()}
                        top_4_slots = select_top_4_slots_with_2hr_gap(slot_counts)

                        if current_slot in top_4_slots:
                            slot_mention_sent[slot_sent_key] = True
                            mentioned_in_this_gc += 1

                            u_name = (
                                ram_user_profile.get(uid, {}).get("name")
                                or names_map.get(uid_str)
                                or "Friend"
                            )
                            u_lang = (
                                ram_user_profile.get(uid, {}).get("lang")
                                or langs_map.get(uid_str)
                                or "hi"
                            )

                            mention_html = format_blue_mention(uid, u_name)
                            period = get_time_of_day_period(u_lang)
                            templates = PLAYFUL_SLOT_MESSAGES.get(period, PLAYFUL_SLOT_MESSAGES["morning"])
                            chosen_text = random.choice(templates).format(mention=mention_html)

                            try:
                                await bot_app_ref.bot.send_message(
                                    chat_id=chat_id,
                                    text=chosen_text,
                                    parse_mode=ParseMode.HTML
                                )
                                await asyncio.sleep(0.35)
                            except Exception as e:
                                logger.debug(f"Slot mention failed in GC {chat_id}: {e}")
                                break

        except Exception as e:
            logger.error(f"Error in slot_mention_scheduler_worker: {e}")

        await asyncio.sleep(SLOT_MENTION_CHECK_INTERVAL)


# =========================================================
# 📣 COMMAND 1: /get_notified (OR /get_notify) IN GROUP REPLY
# =========================================================
async def cmd_get_notified(update: Update, context: CallbackContext):
    """
    Allows a user to reply to someone's message in a GC with `/get_notified`
    so they receive a DM whenever that target user messages in the GC.
    If the caller has disabled Get-Notified in their settings, prompts them
    with a user-locked Enable button first!
    """
    msg = update.message
    if not msg or not msg.from_user:
        return

    if msg.chat.type not in ["group", "supergroup"]:
        await msg.reply_text(
            "<blockquote>ℹ️ <b>Group Reply Command!</b>\n"
            "Please use <code>/get_notified</code> by replying to a user's message inside a group chat!</blockquote>",
            parse_mode=ParseMode.HTML
        )
        return

    subscriber = msg.from_user
    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await msg.reply_text(
            "<blockquote>ℹ️ <b>Reply Required!</b>\n"
            "Reply to any user's message with <code>/get_notified</code> to get a DM alert whenever they come online in this group! 🔔</blockquote>",
            parse_mode=ParseMode.HTML
        )
        return

    target = msg.reply_to_message.from_user
    if target.id == subscriber.id:
        await msg.reply_text("😂 You cannot set an online notification for yourself!")
        return

    if target.is_bot:
        await msg.reply_text("🤖 You cannot set online notifications for bots!")
        return

    chat_id = msg.chat_id
    sub_mention = format_blue_mention(subscriber.id, subscriber.first_name)
    target_mention = format_blue_mention(target.id, target.first_name)

    # Check if subscriber has disabled `/get_notified` feature
    sub_settings = await get_user_notification_settings(subscriber.id)
    if not sub_settings.get("get_notify_enabled", True) or chat_id in sub_settings.get("muted_gcs", []):
        # Prompt user to enable first with a user-ID locked callback button
        enable_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "✅ Enable Get-Notified Feature",
                    callback_data=f"ntf_en_gn_{subscriber.id}_{target.id}_{chat_id}"
                )
            ]
        ])
        await msg.reply_text(
            f"<blockquote>⚠️ <b>Notifications Currently Disabled!</b>\n\n"
            f"Hey {sub_mention}, you have disabled <b>Get-Notified</b> alerts (or muted this group).\n"
            f"Please click the button below to enable it first and subscribe to {target_mention}! 👇</blockquote>",
            parse_mode=ParseMode.HTML,
            reply_markup=enable_kb
        )
        return

    # Unmute target user if they were previously muted
    if target.id in sub_settings.get("muted_users_get_notify", []):
        sub_settings["muted_users_get_notify"].remove(target.id)
        await save_user_notification_settings(
            subscriber.id, sub_settings, first_name=subscriber.first_name, username=subscriber.username or ""
        )

    # Save subscription in RAM & Valkey
    watchlist_ram_index[(chat_id, target.id)].add(subscriber.id)
    if valkey_client:
        try:
            pipe = valkey_client.pipeline(transaction=False)
            pipe.sadd(f"ntf:watchlist:{chat_id}:{target.id}", str(subscriber.id))
            pipe.expire(f"ntf:watchlist:{chat_id}:{target.id}", TTL_7_DAYS * 4)
            await pipe.execute()
        except Exception as e:
            logger.error(f"Error saving watchlist in Valkey: {e}")

    await msg.reply_text(
        f"<blockquote>🔔 <b>Notification Activated!</b>\n\n"
        f"Hey {sub_mention}, I will send you a private DM whenever {target_mention} comes online and chats in this group!\n"
        f"<i>(Make sure you have started the bot in DM so I can message you!)</i> ✨</blockquote>",
        parse_mode=ParseMode.HTML
    )


# =========================================================
# ⚙️ COMMAND 2: /notification (SETTINGS CONTROL PANEL)
# =========================================================
def _build_notification_panel(user_id: int, first_name: str, settings: Dict[str, Any]) -> Tuple[str, InlineKeyboardMarkup]:
    """Builds the text and user-locked inline keyboard for `/notification`."""
    gn_on = settings.get("get_notify_enabled", True)
    pn_on = settings.get("personal_notify_enabled", True)
    muted_gcs_cnt = len(settings.get("muted_gcs", []))
    muted_gn_cnt = len(settings.get("muted_users_get_notify", []))
    muted_pn_cnt = len(settings.get("muted_users_personal", []))

    user_mention = format_blue_mention(user_id, first_name)
    gn_status = "🟢 <b>Enabled (ON)</b>" if gn_on else "🔴 <b>Disabled (OFF)</b>"
    pn_status = "🟢 <b>Enabled (ON)</b>" if pn_on else "🔴 <b>Disabled (OFF)</b>"

    text = (
        f"⚙️ <b>SMART NOTIFICATION SETTINGS</b> 🔔\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>User:</b> {user_mention}\n\n"
        f"1️⃣ <b>Watchlist Alerts (<code>/get_notified</code>):</b> {gn_status}\n"
        f"   ┗ <i>Notifies you in DM when watched users chat in a GC.</i>\n\n"
        f"2️⃣ <b>Personalized Friend Alerts (Top 5):</b> {pn_status}\n"
        f"   ┗ <i>Notifies you when your closest GC friends come online after 6h+.</i>\n\n"
        f"🔇 <b>Muted Groups:</b> <code>{muted_gcs_cnt}</code> | "
        f"<b>Muted Users:</b> <code>{muted_gn_cnt + muted_pn_cnt}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👇 <i>Use the buttons below to toggle your preferences:</i>"
    )

    gn_btn_text = "🔕 Disable Get-Notified" if gn_on else "🔔 Enable Get-Notified"
    pn_btn_text = "🔕 Disable Personalized Alerts" if pn_on else "🔔 Enable Personalized Alerts"

    buttons = [
        [InlineKeyboardButton(gn_btn_text, callback_data=f"ntf_tog_gn_{user_id}")],
        [InlineKeyboardButton(pn_btn_text, callback_data=f"ntf_tog_pn_{user_id}")],
        [
            InlineKeyboardButton("✅ Enable All & Unmute", callback_data=f"ntf_all_on_{user_id}"),
            InlineKeyboardButton("🔇 Disable All", callback_data=f"ntf_all_off_{user_id}")
        ]
    ]
    return text, InlineKeyboardMarkup(buttons)


async def cmd_notification_settings(update: Update, context: CallbackContext):
    """Handles `/notification` and `/notifications` command."""
    msg = update.message
    if not msg or not msg.from_user:
        return

    user = msg.from_user
    settings = await get_user_notification_settings(user.id)
    panel_text, panel_kb = _build_notification_panel(user.id, user.first_name, settings)
    await msg.reply_text(panel_text, parse_mode=ParseMode.HTML, reply_markup=panel_kb)


# =========================================================
# 🏆 COMMAND 3: /ranking (ADMIN ONLY - LIVE 24H GC STATS)
# =========================================================
async def _is_user_gc_admin(chat_id: int, user_id: int, context: CallbackContext) -> bool:
    """Checks if `user_id` is an administrator or creator in `chat_id` using a 10-min RAM cache."""
    now = time.time()
    cached = gc_admin_cache.get(chat_id)
    if cached and cached["expires_at"] > now:
        return user_id in cached["admins"]

    try:
        admins = await context.bot.get_chat_administrators(chat_id)
        admin_ids = {
            m.user.id for m in admins
            if m.status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER, "administrator", "creator"]
        }
        gc_admin_cache[chat_id] = {
            "admins": admin_ids,
            "expires_at": now + ADMIN_CACHE_TTL
        }
        return user_id in admin_ids
    except Exception as e:
        logger.warning(f"Failed to fetch admins for {chat_id}: {e}")
        return False


async def cmd_ranking(update: Update, context: CallbackContext):
    """
    Admin-only `/ranking` command.
    Fetches and merges the last 24 hourly buckets from Valkey + live unsynced RAM buffer
    to display:
    - Total messages sent in the last 24 hours
    - Total active users in the last 24 hours
    - Top 10 users with clickable blue HTML mentions and message counts.
    """
    msg = update.message
    if not msg or not msg.from_user:
        return

    if msg.chat.type not in ["group", "supergroup"]:
        await msg.reply_text(
            "<blockquote>❌ <b>Group Only Command!</b>\n"
            "The <code>/ranking</code> command can only be used inside group chats!</blockquote>",
            parse_mode=ParseMode.HTML
        )
        return

    chat_id = msg.chat_id
    user = msg.from_user

    # Strict Admin Check
    is_admin = await _is_user_gc_admin(chat_id, user.id, context)
    if not is_admin:
        user_mention = format_blue_mention(user.id, user.first_name)
        await msg.reply_text(
            f"<blockquote>🔒 <b>Admin Only Command!</b>\n"
            f"Sorry {user_mention}, only Group Admins can check <code>/ranking</code> analytics!</blockquote>",
            parse_mode=ParseMode.HTML
        )
        return

    now_dt = datetime.now(timezone.utc)
    last_24_hours = [(now_dt - timedelta(hours=h)).strftime("%Y%m%d%H") for h in range(24)]
    hour_set = set(last_24_hours)

    user_msg_counts: Dict[int, int] = defaultdict(int)
    names_lookup: Dict[int, str] = {}

    # 1. Read Live Unsynced Server RAM Buffer First
    async with ram_buffer_lock:
        if chat_id in ram_gc_buffer:
            buf = ram_gc_buffer[chat_id]
            for h_str, u_map in buf["hourly_user_msgs"].items():
                if h_str in hour_set:
                    for uid, cnt in u_map.items():
                        user_msg_counts[uid] += cnt
            for uid, meta in buf["user_meta"].items():
                if meta.get("name"):
                    names_lookup[uid] = meta["name"]

    # 2. Read Last 24 Hours from Valkey Pipeline
    if valkey_client:
        try:
            pipe = valkey_client.pipeline(transaction=False)
            for h_str in last_24_hours:
                pipe.zrange(f"ntf:gc:{chat_id}:h_users:{h_str}", 0, -1, withscores=True)
            pipe.hgetall(f"ntf:gc:{chat_id}:names")
            res = await pipe.execute()

            valkey_names = res[-1] or {}
            for uid_str, name_val in valkey_names.items():
                uid_int = int(uid_str)
                if uid_int not in names_lookup:
                    names_lookup[uid_int] = name_val

            for bucket_res in res[:-1]:
                if bucket_res:
                    for uid_str, score in bucket_res:
                        user_msg_counts[int(uid_str)] += int(score)
        except Exception as e:
            logger.error(f"Error fetching 24h ranking from Valkey for {chat_id}: {e}")

    # Calculate Totals
    active_users_24h = len([u for u, c in user_msg_counts.items() if c > 0])
    total_msgs_24h = sum(user_msg_counts.values())
    sorted_top10 = sorted(
        [(u, c) for u, c in user_msg_counts.items() if c > 0],
        key=lambda x: x[1],
        reverse=True
    )[:10]

    safe_gc_title = html.escape(msg.chat.title or "Group Chat")
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

    lines = [
        f"📊 <b>LIVE 24-HOUR GROUP ANALYTICS</b> 🏆",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"🏷️ <b>Group:</b> {safe_gc_title}",
        f"💬 <b>Total Messages (24h):</b> <code>{total_msgs_24h:,} Msgs</code>",
        f"👥 <b>Active Members (24h):</b> <code>{active_users_24h:,} Users</code>",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"🔥 <b>TOP 10 MOST ACTIVE CHATTERS (24H):</b>\n"
    ]

    if not sorted_top10:
        lines.append("<i>No messages recorded in the last 24 hours yet!</i>")
    else:
        for idx, (uid, count) in enumerate(sorted_top10):
            medal = medals[idx] if idx < len(medals) else f"#{idx + 1}"
            u_name = (
                ram_user_profile.get(uid, {}).get("name")
                or names_lookup.get(uid)
                or f"User {uid}"
            )
            blue_mention = format_blue_mention(uid, u_name)
            pct = (count / total_msgs_24h * 100) if total_msgs_24h > 0 else 0.0
            lines.append(
                f"{medal} {blue_mention} — <code>{count:,} msgs</code> <i>({pct:.1f}%)</i>"
            )

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("⚡ <i>Real-time data powered by Server RAM + Valkey Engine</i>")

    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# =========================================================
# 🎛️ INTERACTIVE CALLBACK HANDLER (STRICT USER-ID VERIFICATION)
# =========================================================
async def handle_notification_callbacks(update: Update, context: CallbackContext) -> bool:
    """
    Handles all `ntf_*` callback queries.
    Strictly verifies that `query.from_user.id == owner_id` encoded in the callback data
    so other users in a GC can NEVER trigger DB queries or modify someone else's settings!
    Returns True if handled, False otherwise.
    """
    query = update.callback_query
    if not query or not query.data or not query.data.startswith("ntf_"):
        return False

    data = query.data
    caller = query.from_user
    if not caller:
        await query.answer()
        return True

    parts = data.split("_")
    # Every callback encodes owner_id at parts[3] (or parts[2] for short commands)
    # Formats:
    # ntf_en_gn_{sub_id}_{target_id}_{chat_id}
    # ntf_tog_gn_{user_id}
    # ntf_tog_pn_{user_id}
    # ntf_all_on_{user_id}
    # ntf_all_off_{user_id}
    # ntf_goff_u_{sub_id}_{target_id}_{chat_id}
    # ntf_gset_m_{sub_id}_{target_id}_{chat_id}
    # ntf_gback_{sub_id}_{target_id}_{chat_id}
    # ntf_off_all_{user_id}
    # ntf_off_gc_{user_id}_{chat_id}
    # ntf_poff_u_{friend_id}_{target_id}_{chat_id}
    # ntf_pset_m_{friend_id}_{target_id}_{chat_id}
    # ntf_pback_{friend_id}_{target_id}_{chat_id}
    # ntf_poff_all_{user_id}

    try:
        owner_id = int(parts[3]) if parts[1] in ("en", "tog", "all", "goff", "gset", "off", "poff", "pset") else int(parts[2])
    except (IndexError, ValueError):
        await query.answer("Invalid button data.", show_alert=True)
        return True

    # STRICT SECURITY GUARD: Zero DB hits if clicked by anyone other than the intended user!
    if caller.id != owner_id:
        await query.answer(
            "🔒 Access Denied! This notification control button belongs to another user.",
            show_alert=True
        )
        return True

    settings = await get_user_notification_settings(caller.id)

    # 1. Enable Get-Notified from GC prompt & auto-subscribe to target
    if data.startswith("ntf_en_gn_"):
        target_id = int(parts[4])
        chat_id = int(parts[5])

        settings["get_notify_enabled"] = True
        if chat_id in settings.get("muted_gcs", []):
            settings["muted_gcs"].remove(chat_id)
        if target_id in settings.get("muted_users_get_notify", []):
            settings["muted_users_get_notify"].remove(target_id)

        await save_user_notification_settings(caller.id, settings, caller.first_name, caller.username or "")

        # Also add subscription to RAM & Valkey immediately
        watchlist_ram_index[(chat_id, target_id)].add(caller.id)
        if valkey_client:
            try:
                await valkey_client.sadd(f"ntf:watchlist:{chat_id}:{target_id}", str(caller.id))
            except Exception:
                pass

        caller_mention = format_blue_mention(caller.id, caller.first_name)
        target_name = ram_user_profile.get(target_id, {}).get("name", "User")
        target_mention = format_blue_mention(target_id, target_name)

        await query.edit_message_text(
            f"<blockquote>✅ <b>Get-Notified Enabled & Subscribed!</b>\n\n"
            f"Hey {caller_mention}, your notifications are now active and you will receive a DM whenever {target_mention} chats in this group! 🔔✨</blockquote>",
            parse_mode=ParseMode.HTML
        )
        await query.answer("✅ Notifications enabled!")
        return True

    # 2. /notification Control Panel Toggles
    if data.startswith("ntf_tog_gn_"):
        settings["get_notify_enabled"] = not settings.get("get_notify_enabled", True)
        await save_user_notification_settings(caller.id, settings, caller.first_name, caller.username or "")
        text, kb = _build_notification_panel(caller.id, caller.first_name, settings)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        status_str = "Enabled 🔔" if settings["get_notify_enabled"] else "Disabled 🔕"
        await query.answer(f"Get-Notified Alerts: {status_str}")
        return True

    if data.startswith("ntf_tog_pn_"):
        settings["personal_notify_enabled"] = not settings.get("personal_notify_enabled", True)
        await save_user_notification_settings(caller.id, settings, caller.first_name, caller.username or "")
        text, kb = _build_notification_panel(caller.id, caller.first_name, settings)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        status_str = "Enabled 🔔" if settings["personal_notify_enabled"] else "Disabled 🔕"
        await query.answer(f"Personalized Friend Alerts: {status_str}")
        return True

    if data.startswith("ntf_all_on_"):
        settings["get_notify_enabled"] = True
        settings["personal_notify_enabled"] = True
        settings["muted_gcs"] = []
        settings["muted_users_get_notify"] = []
        settings["muted_users_personal"] = []
        await save_user_notification_settings(caller.id, settings, caller.first_name, caller.username or "")
        text, kb = _build_notification_panel(caller.id, caller.first_name, settings)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        await query.answer("✅ All notifications enabled & mute lists cleared!", show_alert=True)
        return True

    if data.startswith("ntf_all_off_"):
        settings["get_notify_enabled"] = False
        settings["personal_notify_enabled"] = False
        await save_user_notification_settings(caller.id, settings, caller.first_name, caller.username or "")
        text, kb = _build_notification_panel(caller.id, caller.first_name, settings)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        await query.answer("🔇 All notifications disabled!", show_alert=True)
        return True

    # 3. Watchlist DM Buttons: Turn off for specific user / Open Settings Menu
    if data.startswith("ntf_goff_u_"):
        target_id = int(parts[4])
        chat_id = int(parts[5])

        if target_id not in settings.get("muted_users_get_notify", []):
            settings.setdefault("muted_users_get_notify", []).append(target_id)
        await save_user_notification_settings(caller.id, settings, caller.first_name, caller.username or "")

        # Also remove from active watchlist
        watchlist_ram_index[(chat_id, target_id)].discard(caller.id)
        if valkey_client:
            try:
                await valkey_client.srem(f"ntf:watchlist:{chat_id}:{target_id}", str(caller.id))
            except Exception:
                pass

        t_name = ram_user_profile.get(target_id, {}).get("name", "this user")
        t_mention = format_blue_mention(target_id, t_name)
        await query.edit_message_text(
            f"🔕 <b>Notifications Turned Off!</b>\n"
            f"You will no longer receive <code>/get_notified</code> alerts for {t_mention}.\n"
            f"<i>Use <code>/notification</code> anytime to manage your settings.</i>",
            parse_mode=ParseMode.HTML
        )
        await query.answer("🔕 Turned off for this user!")
        return True

    if data.startswith("ntf_gset_m_"):
        target_id = int(parts[4])
        chat_id = int(parts[5])
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🔕 Turn off all notifications",
                    callback_data=f"ntf_off_all_{caller.id}"
                )
            ],
            [
                InlineKeyboardButton(
                    "🔇 Turn off all notifications from this GC",
                    callback_data=f"ntf_off_gc_{caller.id}_{chat_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    "🔙 Back",
                    callback_data=f"ntf_gback_{caller.id}_{target_id}_{chat_id}"
                )
            ]
        ])
        await query.edit_message_reply_markup(reply_markup=kb)
        await query.answer()
        return True

    if data.startswith("ntf_gback_"):
        target_id = int(parts[3])
        chat_id = int(parts[4])
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🔕 Turn off notifications for this user",
                    callback_data=f"ntf_goff_u_{caller.id}_{target_id}_{chat_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    "⚙️ Notification Settings",
                    callback_data=f"ntf_gset_m_{caller.id}_{target_id}_{chat_id}"
                )
            ]
        ])
        await query.edit_message_reply_markup(reply_markup=kb)
        await query.answer()
        return True

    # 4. Shared Mute All / Mute GC Callbacks
    if data.startswith("ntf_off_all_"):
        settings["get_notify_enabled"] = False
        settings["personal_notify_enabled"] = False
        await save_user_notification_settings(caller.id, settings, caller.first_name, caller.username or "")
        await query.edit_message_text(
            "🔇 <b>All Notifications Turned Off!</b>\n"
            "You can re-enable them anytime using <code>/notification</code>.",
            parse_mode=ParseMode.HTML
        )
        await query.answer("🔇 All notifications turned off!")
        return True

    if data.startswith("ntf_off_gc_"):
        chat_id = int(parts[4])
        if chat_id not in settings.get("muted_gcs", []):
            settings.setdefault("muted_gcs", []).append(chat_id)
        await save_user_notification_settings(caller.id, settings, caller.first_name, caller.username or "")
        gc_title = html.escape(ram_group_titles.get(chat_id, "this group"))
        await query.edit_message_text(
            f"🔇 <b>Group Muted!</b>\n"
            f"All notifications from <b>{gc_title}</b> have been turned off.\n"
            f"<i>Use <code>/notification</code> anytime to unmute.</i>",
            parse_mode=ParseMode.HTML
        )
        await query.answer("🔇 Muted notifications from this group!")
        return True

    # 5. Personalized Friend DM Buttons: Turn off for user / More Settings
    if data.startswith("ntf_poff_u_"):
        target_id = int(parts[4])
        if target_id not in settings.get("muted_users_personal", []):
            settings.setdefault("muted_users_personal", []).append(target_id)
        await save_user_notification_settings(caller.id, settings, caller.first_name, caller.username or "")

        t_name = ram_user_profile.get(target_id, {}).get("name", "this friend")
        t_mention = format_blue_mention(target_id, t_name)
        await query.edit_message_text(
            f"🔕 <b>Personalized Notification Muted!</b>\n"
            f"You will no longer receive friend online alerts for {t_mention}.\n"
            f"<i>Manage anytime via <code>/notification</code>.</i>",
            parse_mode=ParseMode.HTML
        )
        await query.answer("🔕 Muted personalized alerts for this user!")
        return True

    if data.startswith("ntf_pset_m_"):
        target_id = int(parts[4])
        chat_id = int(parts[5])
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🔕 Turn off personalized notifications",
                    callback_data=f"ntf_poff_all_{caller.id}"
                )
            ],
            [
                InlineKeyboardButton(
                    "🔇 Turn off all notifications from this GC",
                    callback_data=f"ntf_off_gc_{caller.id}_{chat_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    "🔙 Back",
                    callback_data=f"ntf_pback_{caller.id}_{target_id}_{chat_id}"
                )
            ]
        ])
        await query.edit_message_reply_markup(reply_markup=kb)
        await query.answer()
        return True

    if data.startswith("ntf_pback_"):
        target_id = int(parts[3])
        chat_id = int(parts[4])
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🔕 Turn off personalized notification for this user",
                    callback_data=f"ntf_poff_u_{caller.id}_{target_id}_{chat_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    "⚙️ More Settings",
                    callback_data=f"ntf_pset_m_{caller.id}_{target_id}_{chat_id}"
                )
            ]
        ])
        await query.edit_message_reply_markup(reply_markup=kb)
        await query.answer()
        return True

    if data.startswith("ntf_poff_all_"):
        settings["personal_notify_enabled"] = False
        await save_user_notification_settings(caller.id, settings, caller.first_name, caller.username or "")
        await query.edit_message_text(
            "🔕 <b>Personalized Friend Notifications Turned Off!</b>\n"
            "You can turn them back on anytime using <code>/notification</code>.",
            parse_mode=ParseMode.HTML
        )
        await query.answer("🔕 Personalized notifications turned off!")
        return True

    await query.answer()
    return True


# =========================================================
# 🔌 HANDLER REGISTRATION & SHUTDOWN HELPER
# =========================================================
def register_notification_handlers(ptb_app: Application):
    """Registers `/get_notified`, `/notification`, `/ranking`, and `ntf_*` callback handlers."""
    ptb_app.add_handler(CommandHandler(["get_notified", "get_notify"], cmd_get_notified))
    ptb_app.add_handler(CommandHandler(["notification", "notifications"], cmd_notification_settings))
    ptb_app.add_handler(CommandHandler("ranking", cmd_ranking))
    # High-priority group=0 pattern handler for `ntf_` callbacks
    ptb_app.add_handler(CallbackQueryHandler(handle_notification_callbacks, pattern=r"^ntf_"), group=0)


async def shutdown_notification_system():
    """Flushes any remaining RAM analytics to Valkey and closes the Valkey client cleanly."""
    try:
        await flush_ram_to_valkey()
    except Exception:
        pass
    global valkey_client
    if valkey_client:
        try:
            await valkey_client.close()
        except Exception:
            pass
