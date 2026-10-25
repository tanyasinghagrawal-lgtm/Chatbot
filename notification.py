import asyncio
import html
import json
import logging
import random
import ssl
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, Bot
from telegram.constants import ParseMode
from telegram.ext import CallbackContext

# Valkey / Redis Async Client Import (Supports both redis and valkey packages)
try:
    import redis.asyncio as redis_async
except ImportError:
    try:
        import valkey.asyncio as redis_async
    except ImportError:
        redis_async = None

logger = logging.getLogger("TanyaNotification")

# =========================================================
# 🔗 VALKEY & TIMING CONFIGURATION
# =========================================================
VALKEY_URI = "rediss://default:AVNS_lZVvN0QRZAXRH4VA17m@valkey-39013870-preetibrb7-6457.c.aivencloud.com:12734"

VALKEY_SYNC_INTERVAL = 120          # 2 minutes RAM -> Valkey pipeline sync
RETENTION_3_DAYS = 3 * 86400        # 3 days TTL (259,200s) for GC activity & replies
RETENTION_7_DAYS = 7 * 86400        # 7 days TTL (604,800s) for 72-slot active time tracking
SETTINGS_CACHE_TTL = 3 * 3600       # 3 hours (10,800s) RAM cache for user settings
DM_NOTIFY_COOLDOWN = 30 * 60        # 30 minutes (1,800s) gap between 2 DM notifications
PERSONAL_INACTIVE_GAP = 6 * 3600    # 6 hours (21,600s) inactivity required for friend alert
TOP5_RECALC_INTERVAL = 6 * 3600     # Max once per 6 hours Top-5 calculation per user per GC
MIN_MUTUAL_REPLIES = 5              # Min 5 replies each way for Top-5 mutual engagers
TOTAL_SLOTS = 72                    # 24 hours divided into 20-min slots (72 slots)
MIN_SLOT_GAP = 6                    # 6 slots * 20 mins = 2 hours minimum gap between Top 4 slots

# Global References (Injected by bot.py or auto-discovered from __main__)
pg_pool = None
bot: Optional[Bot] = None
ptb_app = None
valkey_client = None

_workers_started = False
_schema_initialized = False
_ram_lock = asyncio.Lock()

# =========================================================
# 🧠 SERVER RAM CACHES & DELTA BUFFERS
# =========================================================
# 1. User Metadata Cache: {user_id: {"name": str, "username": str, "lang": str}}
ram_user_meta: Dict[int, Dict[str, str]] = {}

# 2. Group Title Cache: {chat_id: str}
ram_group_titles: Dict[int, str] = {}

# 3. Live 24-Hour Rolling Mirror (Guarantees /ranking NEVER shows 0 even before/without Valkey sync)
# {chat_id: {hour_bucket: {user_id: msg_count}}}
ram_24h_mirror: Dict[int, Dict[int, Dict[int, int]]] = defaultdict(
    lambda: defaultdict(lambda: defaultdict(int))
)

# 4. Unsynced Delta Buffers (Flushed to Valkey every 2 mins via Pipeline)
# Hourly message counts: {chat_id: {hour_bucket: {user_id: count}}}
ram_pending_hourly: Dict[int, Dict[int, Dict[int, int]]] = defaultdict(
    lambda: defaultdict(lambda: defaultdict(int))
)
# 72-slot counts: {chat_id: {day_str: {user_id: {slot_idx: count}}}}
ram_pending_slots: Dict[int, Dict[str, Dict[int, Dict[int, int]]]] = defaultdict(
    lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
)
# Reply pairs: {chat_id: {day_str: {(sender_id, replied_to_id): count}}}
ram_pending_replies: Dict[int, Dict[str, Dict[Tuple[int, int], int]]] = defaultdict(
    lambda: defaultdict(lambda: defaultdict(int))
)
# Last active timestamps: {chat_id: {user_id: float_ts}}
ram_last_active: Dict[int, Dict[int, float]] = defaultdict(dict)
ram_pending_last_active: Dict[int, Dict[int, float]] = defaultdict(dict)

# 5. In-Memory Mirrors for Replies & 72-Slots (Fast fallback + instant calculation)
# Outgoing replies: {chat_id: {sender_id: {replied_to_id: count}}}
ram_replies_out: Dict[int, Dict[int, Dict[int, int]]] = defaultdict(
    lambda: defaultdict(lambda: defaultdict(int))
)
# Incoming replies: {chat_id: {replied_to_id: {sender_id: count}}}
ram_replies_in: Dict[int, Dict[int, Dict[int, int]]] = defaultdict(
    lambda: defaultdict(lambda: defaultdict(int))
)
# User slot activity: {chat_id: {user_id: {slot_idx: count}}}
ram_user_slots: Dict[int, Dict[int, Dict[int, int]]] = defaultdict(
    lambda: defaultdict(lambda: defaultdict(int))
)

# 6. /get_notified Watchers in RAM: {chat_id: {target_user_id: Set[watcher_user_id]}}
ram_watchers: Dict[int, Dict[int, Set[int]]] = defaultdict(lambda: defaultdict(set))

# 7. User Notification Settings Cache (3-Hour Expiry):
# {user_id: {"get_notify_enabled": bool, "personal_notify_enabled": bool, "settings": dict, "top_engagers": dict, "expires_at": float}}
user_settings_cache: Dict[int, Dict[str, Any]] = {}

# 8. Cooldown & Dedup Trackers in RAM:
# DM notification 30-min cooldown: {(recipient_id, subject_user_id, chat_id): last_sent_ts}
dm_cooldown_tracker: Dict[Tuple[int, int, int], float] = {}
# Personalized friend notification 6-hr cooldown per active user in GC: {(chat_id, user_id): last_blast_ts}
personal_blast_tracker: Dict[Tuple[int, int], float] = {}
# Top 5 recalculation timestamp: {(chat_id, user_id): last_calc_ts}
top5_calc_tracker: Dict[Tuple[int, int], float] = {}
# Slot mention tracker: {(chat_id, user_id, day_str, slot_idx): True}
slot_mentioned_tracker: Dict[Tuple[int, int, str, int], bool] = {}
# Last slot mention timestamp per (chat_id, user_id) to enforce >= 2h gap
last_slot_mention_ts: Dict[Tuple[int, int], float] = {}
# Admin ID cache (5-min TTL): {chat_id: {"admins": Set[int], "expires_at": float}}
admin_cache: Dict[int, Dict[str, Any]] = {}


# =========================================================
# 🌍 TIMEZONE & PLAYFUL MESSAGES ENGINE (72 SLOTS)
# =========================================================
LANG_TZ_OFFSET_MINUTES = {
    "en": 330,     # Default India (IST +5:30)
    "en-in": 330,
    "hi": 330,
    "ur": 300,     # PKT +5:00
    "bn": 330,
    "ta": 330,
    "te": 330,
    "mr": 330,
    "gu": 330,
    "kn": 330,
    "ml": 330,
    "pa": 330,
    "ne": 345,     # Nepal +5:45
    "ru": 180,     # MSK +3:00
    "id": 420,     # WIB +7:00
    "ar": 180,     # AST +3:00
    "es": -300,    # EST -5:00
    "pt": -180,    # BRT -3:00
    "tr": 180,     # TRT +3:00
}

PLAYFUL_SLOT_MESSAGES = {
    "morning": [
        "Hey {mention}, Good morning 🌞! Where are you? Let's start some conversations here! ☕✨",
        "Rise and shine {mention}! 🌞 Good morning! The group is waiting for your morning energy, come say hi! 💬🌸",
        "Hey {mention}! Good morning ☀️! Grab your tea and let's kick off some fun chats here! 🥰☕",
        "Good morning {mention} 🌞! Where are you hiding today? Drop a message and wake up the group! ✨🔥"
    ],
    "afternoon": [
        "Hey {mention}, Good afternoon ☀️! Where are you? Come take a break and let's start some conversations here! 😎🍹",
        "Good afternoon {mention} 🌤️! It's your favorite time to chat—drop in and tell us how your day is going! 💬✨",
        "Hey {mention}! Good afternoon ☀️! The chat feels quiet without you, come spice things up! 🔥😄"
    ],
    "evening": [
        "Hey {mention}, Good evening 🌆! Where are you? Let's start some conversations and chill here! ☕✨",
        "Good evening {mention} 🌇! Clocked out for the day? Come join the squad and let's chat! 💬🎉",
        "Hey {mention}! Good evening ✨! This is your prime time—where are you? Come say hello! 🥰🌸"
    ],
    "night": [
        "Hey {mention}, Good night & late-night vibes 🌙! Where are you? Let's start some conversations here! 🦉✨",
        "Hey night owl {mention} 🌙! Still awake? Come join the late-night gang and let's chat! 💬🔥",
        "Good night vibes {mention} 🌌! We know you're usually online around this time—drop a text and say hi! 🥰✨"
    ]
}


def get_utc_slot_index(ts: Optional[float] = None) -> int:
    """Divides 24 hours into 72 slots of 20 minutes each (0 to 71 in UTC)."""
    dt = datetime.fromtimestamp(ts or time.time(), tz=timezone.utc)
    return (dt.hour * 60 + dt.minute) // 20


def get_local_greeting_period(lang_code: str, ts: Optional[float] = None) -> str:
    """Determines morning/afternoon/evening/night based on user's language/country timezone."""
    code = (lang_code or "en").lower()
    offset_mins = LANG_TZ_OFFSET_MINUTES.get(code, LANG_TZ_OFFSET_MINUTES.get(code.split("-")[0], 330))
    tz = timezone(timedelta(minutes=offset_mins))
    local_dt = datetime.fromtimestamp(ts or time.time(), tz=tz)
    hr = local_dt.hour
    if 5 <= hr < 12:
        return "morning"
    elif 12 <= hr < 17:
        return "afternoon"
    elif 17 <= hr < 21:
        return "evening"
    else:
        return "night"


def format_blue_mention(user_id: int, first_name: str) -> str:
    """Formats clickable blue HTML mention just like /rob or /slap."""
    safe_name = html.escape((first_name or "User").strip() or "User")
    return f'<a href="tg://user?id={user_id}">{safe_name}</a>'


def _parse_jsonb(val: Any, default: Any) -> Any:
    """Safely parses asyncpg JSONB value whether returned as str or dict/list."""
    if val is None:
        return default
    if isinstance(val, (dict, list)):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return default
    return default


# =========================================================
# 🔌 DEPENDENCY & SCHEMA SELF-HEALING INITIALIZER
# =========================================================
async def _ensure_dependencies(
    bot_obj: Optional[Bot] = None,
    pool_obj: Any = None,
    context: Optional[CallbackContext] = None
):
    """Automatically binds pg_pool, bot, Valkey, DB schema, and background tasks."""
    global pg_pool, bot, ptb_app, valkey_client, _workers_started, _schema_initialized

    if pool_obj is not None:
        pg_pool = pool_obj
    if bot_obj is not None:
        bot = bot_obj
    elif context is not None and getattr(context, "bot", None) is not None:
        bot = context.bot

    # Auto-discover from __main__ (bot.py) if not yet set
    main_mod = sys.modules.get("__main__")
    if main_mod:
        if pg_pool is None and getattr(main_mod, "pg_pool", None) is not None:
            pg_pool = getattr(main_mod, "pg_pool")
        if ptb_app is None and getattr(main_mod, "ptb_app", None) is not None:
            ptb_app = getattr(main_mod, "ptb_app")
        if bot is None and ptb_app is not None and getattr(ptb_app, "bot", None) is not None:
            bot = ptb_app.bot

    # Initialize Valkey with SSL_CERT_NONE (Fixes Aiven self-signed SSL certificate errors)
    if valkey_client is None and redis_async is not None:
        try:
            valkey_client = redis_async.from_url(
                VALKEY_URI,
                decode_responses=True,
                ssl_cert_reqs=ssl.CERT_NONE,
                socket_timeout=10.0,
                socket_connect_timeout=10.0
            )
            await valkey_client.ping()
            logger.info("✅ Valkey (rediss://) connected successfully for Group Analytics!")
        except Exception as e:
            logger.warning(f"Valkey connection attempt warning (falling back to RAM): {e}")

    # Ensure PostgreSQL columns exist on users table
    if pg_pool is not None and not _schema_initialized:
        _schema_initialized = True
        try:
            async with pg_pool.acquire() as conn:
                await conn.execute('''
                    ALTER TABLE users ADD COLUMN IF NOT EXISTS notify_enabled BOOLEAN DEFAULT TRUE;
                    ALTER TABLE users ADD COLUMN IF NOT EXISTS get_notify_enabled BOOLEAN DEFAULT TRUE;
                    ALTER TABLE users ADD COLUMN IF NOT EXISTS personal_notify_enabled BOOLEAN DEFAULT TRUE;
                    ALTER TABLE users ADD COLUMN IF NOT EXISTS notification_settings JSONB DEFAULT '{}'::jsonb;
                    ALTER TABLE users ADD COLUMN IF NOT EXISTS top_engagers JSONB DEFAULT '{}'::jsonb;
                ''')
                # Load active /get_notified watchers from PostgreSQL into RAM on startup
                rows = await conn.fetch('''
                    SELECT user_id, notification_settings FROM users
                    WHERE notification_settings IS NOT NULL;
                ''')
                for r in rows:
                    w_uid = r["user_id"]
                    s_data = _parse_jsonb(r["notification_settings"], {})
                    for item in s_data.get("watching", []):
                        if isinstance(item, list) and len(item) == 2:
                            cid, tid = int(item[0]), int(item[1])
                            ram_watchers[cid][tid].add(w_uid)
            logger.info("✅ PostgreSQL notification columns & watchers loaded into RAM!")
        except Exception as e:
            _schema_initialized = False
            logger.error(f"Schema init error in notification.py: {e}")

    # Start background tasks once
    if not _workers_started:
        _workers_started = True
        asyncio.create_task(_valkey_batch_sync_worker())
        asyncio.create_task(_personalized_slot_mention_worker())
        logger.info("🚀 Notification background workers (2-min Valkey Sync & 72-Slot Mentions) started!")


async def init_notification_system(
    pg_pool: Any = None,
    db_pool: Any = None,
    pool: Any = None,
    bot: Optional[Bot] = None,
    app: Any = None,
    ptb_app: Any = None,
    application: Any = None,
    **kwargs
):
    """Entry point called by init_notification_bridge() in bot.py."""
    await _ensure_dependencies(
        bot_obj=bot,
        pool_obj=pg_pool or db_pool or pool
    )


async def shutdown_notification_system(
    pg_pool: Any = None,
    bot: Optional[Bot] = None,
    ptb_app: Any = None,
    **kwargs
):
    """Flushes remaining RAM analytics to Valkey and closes connection cleanly."""
    await flush_ram_to_valkey()
    global valkey_client
    if valkey_client is not None:
        try:
            await valkey_client.aclose()
        except Exception:
            pass
        valkey_client = None


# =========================================================
# 💾 USER SETTINGS & TOP-5 CACHE (POSTGRESQL + 3H RAM CACHE)
# =========================================================
async def get_user_notif_profile(user_id: int) -> Dict[str, Any]:
    """
    Fetches user's notification settings & Top-5 engagers from 3-hour RAM cache.
    Hits PostgreSQL only on cache miss or expiry.
    """
    now = time.time()
    cached = user_settings_cache.get(user_id)
    if cached and cached.get("expires_at", 0) > now:
        return cached

    await _ensure_dependencies()
    profile = {
        "user_id": user_id,
        "notify_enabled": True,
        "get_notify_enabled": True,
        "personal_notify_enabled": True,
        "settings": {
            "muted_gcs": [],
            "muted_gn_users": [],
            "muted_pn_users": [],
            "watching": []
        },
        "top_engagers": {},
        "expires_at": now + SETTINGS_CACHE_TTL
    }

    if pg_pool is not None:
        try:
            async with pg_pool.acquire() as conn:
                row = await conn.fetchrow('''
                    SELECT notify_enabled, get_notify_enabled, personal_notify_enabled,
                           notification_settings, top_engagers
                    FROM users WHERE user_id = $1;
                ''', user_id)
                if row:
                    n_en = True if row["notify_enabled"] is None else bool(row["notify_enabled"])
                    gn_en = True if row["get_notify_enabled"] is None else bool(row["get_notify_enabled"])
                    pn_en = True if row["personal_notify_enabled"] is None else bool(row["personal_notify_enabled"])
                    raw_s = _parse_jsonb(row["notification_settings"], {})
                    raw_t = _parse_jsonb(row["top_engagers"], {})

                    profile["notify_enabled"] = n_en
                    profile["get_notify_enabled"] = gn_en and n_en
                    profile["personal_notify_enabled"] = pn_en and n_en
                    profile["settings"] = {
                        "muted_gcs": list(raw_s.get("muted_gcs", [])),
                        "muted_gn_users": list(raw_s.get("muted_gn_users", [])),
                        "muted_pn_users": list(raw_s.get("muted_pn_users", [])),
                        "watching": list(raw_s.get("watching", []))
                    }
                    profile["top_engagers"] = raw_t if isinstance(raw_t, dict) else {}
        except Exception as e:
            logger.error(f"Error loading user notif profile for {user_id}: {e}")

    user_settings_cache[user_id] = profile
    return profile


async def save_user_notif_profile(
    user_id: int,
    first_name: str = "User",
    username: str = "",
    notify_enabled: Optional[bool] = None,
    get_notify_enabled: Optional[bool] = None,
    personal_notify_enabled: Optional[bool] = None,
    settings: Optional[Dict[str, Any]] = None,
    top_engagers: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Updates both the 3-hour RAM cache and PostgreSQL users table."""
    profile = await get_user_notif_profile(user_id)

    if notify_enabled is not None:
        profile["notify_enabled"] = bool(notify_enabled)
    if get_notify_enabled is not None:
        profile["get_notify_enabled"] = bool(get_notify_enabled)
    if personal_notify_enabled is not None:
        profile["personal_notify_enabled"] = bool(personal_notify_enabled)
    if settings is not None:
        profile["settings"] = settings
    if top_engagers is not None:
        profile["top_engagers"] = top_engagers

    # Keep master flag in sync if either sub-feature is enabled
    if profile["get_notify_enabled"] or profile["personal_notify_enabled"]:
        profile["notify_enabled"] = True

    profile["expires_at"] = time.time() + SETTINGS_CACHE_TTL
    user_settings_cache[user_id] = profile

    await _ensure_dependencies()
    if pg_pool is not None:
        try:
            settings_json = json.dumps(profile["settings"])
            engagers_json = json.dumps(profile["top_engagers"])
            async with pg_pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO users (
                        user_id, username, first_name, balance,
                        notify_enabled, get_notify_enabled, personal_notify_enabled,
                        notification_settings, top_engagers
                    )
                    VALUES ($1, $2, $3, 250, $4, $5, $6, $7::jsonb, $8::jsonb)
                    ON CONFLICT (user_id) DO UPDATE SET
                        notify_enabled = EXCLUDED.notify_enabled,
                        get_notify_enabled = EXCLUDED.get_notify_enabled,
                        personal_notify_enabled = EXCLUDED.personal_notify_enabled,
                        notification_settings = EXCLUDED.notification_settings,
                        top_engagers = EXCLUDED.top_engagers;
                ''',
                user_id,
                username or ram_user_meta.get(user_id, {}).get("username", ""),
                first_name or ram_user_meta.get(user_id, {}).get("name", "User"),
                profile["notify_enabled"],
                profile["get_notify_enabled"],
                profile["personal_notify_enabled"],
                settings_json,
                engagers_json
                )
        except Exception as e:
            logger.error(f"Error saving notif profile to DB for {user_id}: {e}")

    return profile


# =========================================================
# 🔄 VALKEY 2-MINUTE PIPELINE SYNC ENGINE
# =========================================================
async def flush_ram_to_valkey():
    """Flushes accumulated RAM activity deltas to Valkey in a single atomic pipeline every 2 minutes."""
    await _ensure_dependencies()
    if valkey_client is None:
        return

    async with _ram_lock:
        if (
            not ram_pending_hourly
            and not ram_pending_slots
            and not ram_pending_replies
            and not ram_pending_last_active
        ):
            return

        snap_hourly = {cid: {h: dict(u_map) for h, u_map in h_map.items()} for cid, h_map in ram_pending_hourly.items()}
        snap_slots = {
            cid: {d: {u: dict(s_map) for u, s_map in u_map.items()} for d, u_map in d_map.items()}
            for cid, d_map in ram_pending_slots.items()
        }
        snap_replies = {
            cid: {d: dict(pair_map) for d, pair_map in d_map.items()}
            for cid, d_map in ram_pending_replies.items()
        }
        snap_last = {cid: dict(u_map) for cid, u_map in ram_pending_last_active.items()}
        snap_users = dict(ram_user_meta)
        snap_titles = dict(ram_group_titles)

        ram_pending_hourly.clear()
        ram_pending_slots.clear()
        ram_pending_replies.clear()
        ram_pending_last_active.clear()

    try:
        pipe = valkey_client.pipeline(transaction=False)

        # 1. Sync active group IDs & titles
        for cid, title in snap_titles.items():
            pipe.sadd("vk:active_gcs", str(cid))
            pipe.hset("vk:gc_titles", str(cid), title)

        # 2. Sync user names & language codes (7-day TTL)
        for uid, meta in snap_users.items():
            ukey = f"vk:u:{uid}"
            pipe.hset(ukey, mapping={
                "name": meta.get("name", "User"),
                "username": meta.get("username", ""),
                "lang": meta.get("lang", "en")
            })
            pipe.expire(ukey, RETENTION_7_DAYS)

        # 3. Sync 24h/3d Hourly Message Counts (3-day TTL)
        for cid, h_map in snap_hourly.items():
            for h_bucket, u_map in h_map.items():
                hkey = f"vk:gc:{cid}:h:{h_bucket}"
                for uid, count in u_map.items():
                    pipe.hincrby(hkey, str(uid), count)
                pipe.expire(hkey, RETENTION_3_DAYS)

        # 4. Sync 72-Slot (20-min) User Activity (7-day TTL)
        for cid, d_map in snap_slots.items():
            for day_str, u_map in d_map.items():
                active_users_key = f"vk:gc:{cid}:users"
                for uid, s_map in u_map.items():
                    pipe.sadd(active_users_key, str(uid))
                    skey = f"vk:slots:{cid}:{uid}:{day_str}"
                    for slot_idx, count in s_map.items():
                        pipe.hincrby(skey, str(slot_idx), count)
                    pipe.expire(skey, RETENTION_7_DAYS)
                pipe.expire(active_users_key, RETENTION_7_DAYS)

        # 5. Sync User-to-User Reply Analytics (3-day TTL)
        for cid, d_map in snap_replies.items():
            for day_str, pair_map in d_map.items():
                for (sender_id, target_id), count in pair_map.items():
                    out_key = f"vk:rep:out:{cid}:{sender_id}:{day_str}"
                    in_key = f"vk:rep:in:{cid}:{target_id}:{day_str}"
                    pipe.hincrby(out_key, str(target_id), count)
                    pipe.expire(out_key, RETENTION_3_DAYS)
                    pipe.hincrby(in_key, str(sender_id), count)
                    pipe.expire(in_key, RETENTION_3_DAYS)

        # 6. Sync Last Active Timestamps (3-day TTL)
        for cid, u_map in snap_last.items():
            lkey = f"vk:gc:{cid}:last_active"
            mapping = {str(uid): str(ts) for uid, ts in u_map.items()}
            if mapping:
                pipe.hset(lkey, mapping=mapping)
                pipe.expire(lkey, RETENTION_3_DAYS)

        await pipe.execute()
    except Exception as e:
        logger.warning(f"Valkey batch sync error (re-queueing deltas in RAM): {e}")
        # Restore deltas back to pending buffer so no analytics are ever lost
        async with _ram_lock:
            for cid, h_map in snap_hourly.items():
                for h_bucket, u_map in h_map.items():
                    for uid, cnt in u_map.items():
                        ram_pending_hourly[cid][h_bucket][uid] += cnt
            for cid, d_map in snap_slots.items():
                for day_str, u_map in d_map.items():
                    for uid, s_map in u_map.items():
                        for slot_idx, cnt in s_map.items():
                            ram_pending_slots[cid][day_str][uid][slot_idx] += cnt
            for cid, d_map in snap_replies.items():
                for day_str, pair_map in d_map.items():
                    for pair, cnt in pair_map.items():
                        ram_pending_replies[cid][day_str][pair] += cnt


async def _valkey_batch_sync_worker():
    """Background task that syncs RAM analytics to Valkey every 2 minutes & prunes old RAM buckets."""
    while True:
        await asyncio.sleep(VALKEY_SYNC_INTERVAL)
        try:
            await flush_ram_to_valkey()
            # Prune RAM 24h mirror buckets older than 26 hours to keep RAM tiny
            min_hour = int(time.time() // 3600) - 26
            async with _ram_lock:
                for cid in list(ram_24h_mirror.keys()):
                    old_hours = [h for h in ram_24h_mirror[cid] if h < min_hour]
                    for h in old_hours:
                        del ram_24h_mirror[cid][h]
        except Exception as e:
            logger.error(f"Error in _valkey_batch_sync_worker: {e}")


# =========================================================
# 🤝 TOP 5 MUTUAL ENGAGEMENT ENGINE (MIN 5 REPLIES EACH WAY)
# =========================================================
async def compute_top5_mutual_engagers(chat_id: int, user_id: int, force: bool = False) -> List[int]:
    """
    Calculates Top 5 mutual engagers for `user_id` in `chat_id`:
    1. Top 10 users who replied the most to `user_id` (incoming).
    2. Top 10 users whose messages `user_id` replied to the most (outgoing).
    3. Both must have replied to each other AT LEAST 5 times (incoming >= 5 and outgoing >= 5).
    4. Selects Top 5 sorted by combined engagement.
    Cached for 6 hours per (chat_id, user_id) and saved in user's personal settings in DB + 3h RAM cache.
    """
    now = time.time()
    profile = await get_user_notif_profile(user_id)
    gc_key = str(chat_id)

    last_calc = top5_calc_tracker.get((chat_id, user_id), 0)
    if not force and (now - last_calc < TOP5_RECALC_INTERVAL) and gc_key in profile.get("top_engagers", {}):
        return [int(x) for x in profile["top_engagers"].get(gc_key, [])]

    incoming_totals: Dict[int, int] = defaultdict(int)
    outgoing_totals: Dict[int, int] = defaultdict(int)

    # Combine Valkey 3-day reply history + unsynced RAM deltas
    if valkey_client is not None:
        try:
            now_dt = datetime.now(timezone.utc)
            days = [(now_dt - timedelta(days=i)).strftime("%Y%m%d") for i in range(3)]
            pipe = valkey_client.pipeline(transaction=False)
            for d in days:
                pipe.hgetall(f"vk:rep:in:{chat_id}:{user_id}:{d}")
                pipe.hgetall(f"vk:rep:out:{chat_id}:{user_id}:{d}")
            results = await pipe.execute()

            for idx in range(0, len(results), 2):
                in_map = results[idx] or {}
                out_map = results[idx + 1] or {}
                for other_str, cnt_str in in_map.items():
                    incoming_totals[int(other_str)] += int(cnt_str)
                for other_str, cnt_str in out_map.items():
                    outgoing_totals[int(other_str)] += int(cnt_str)

            # Add unsynced pending replies from RAM
            for d_map in ram_pending_replies.get(chat_id, {}).values():
                for (s_id, r_id), cnt in d_map.items():
                    if s_id == user_id:
                        outgoing_totals[r_id] += cnt
                    elif r_id == user_id:
                        incoming_totals[s_id] += cnt
        except Exception:
            pass

    # Merge with in-memory reply mirrors using max() so it always works even before Valkey sync
    for other_id, cnt in ram_replies_in.get(chat_id, {}).get(user_id, {}).items():
        incoming_totals[other_id] = max(incoming_totals[other_id], cnt)
    for other_id, cnt in ram_replies_out.get(chat_id, {}).get(user_id, {}).items():
        outgoing_totals[other_id] = max(outgoing_totals[other_id], cnt)

    # Step 1: Top 10 who replied to this user
    top10_incoming = {
        uid: cnt for uid, cnt in sorted(incoming_totals.items(), key=lambda x: x[1], reverse=True)[:10]
        if cnt >= MIN_MUTUAL_REPLIES
    }
    # Step 2: Top 10 whom this user replied to
    top10_outgoing = {
        uid: cnt for uid, cnt in sorted(outgoing_totals.items(), key=lambda x: x[1], reverse=True)[:10]
        if cnt >= MIN_MUTUAL_REPLIES
    }

    # Step 3: Mutual intersection where BOTH directions have >= 5 replies
    mutual_candidates = []
    for other_uid in set(top10_incoming.keys()).intersection(set(top10_outgoing.keys())):
        if other_uid == user_id:
            continue
        inc_c = top10_incoming[other_uid]
        out_c = top10_outgoing[other_uid]
        if inc_c >= MIN_MUTUAL_REPLIES and out_c >= MIN_MUTUAL_REPLIES:
            mutual_candidates.append((other_uid, inc_c + out_c))

    mutual_candidates.sort(key=lambda x: x[1], reverse=True)
    top5_ids = [uid for uid, _ in mutual_candidates[:5]]

    top5_calc_tracker[(chat_id, user_id)] = now
    top_engagers_map = dict(profile.get("top_engagers", {}))
    top_engagers_map[gc_key] = top5_ids
    await save_user_notif_profile(user_id, top_engagers=top_engagers_map)

    return top5_ids


# =========================================================
# 📩 REAL-TIME GROUP MESSAGE TRACKER & DM DISPATCHER
# =========================================================
async def record_group_activity(
    update: Optional[Update] = None,
    context: Optional[CallbackContext] = None,
    bot: Optional[Bot] = None,
    pg_pool: Any = None,
    **kwargs
):
    """
    Called on every group message via track_gc_activity_bridge in bot.py.
    1. Saves message & reply analytics in RAM (0 DB calls).
    2. Dispatches /get_notified DMs (with 30-min cooldown).
    3. Dispatches Personalized Top-5 Friend DMs if user was inactive in this GC for >= 6 hours.
    """
    await _ensure_dependencies(bot_obj=bot, pool_obj=pg_pool, context=context)

    msg = getattr(update, "message", None) if update else kwargs.get("msg")
    if not msg or not msg.from_user or msg.from_user.is_bot:
        return

    chat = msg.chat
    if not chat or chat.type not in ("group", "supergroup"):
        return

    chat_id = int(chat.id)
    chat_title = chat.title or "Group Chat"
    user = msg.from_user
    user_id = int(user.id)
    first_name = user.first_name or "User"
    username = user.username or ""
    lang_code = getattr(user, "language_code", "en") or "en"

    reply_to_user_id: Optional[int] = None
    if msg.reply_to_message and msg.reply_to_message.from_user:
        r_user = msg.reply_to_message.from_user
        if not r_user.is_bot and int(r_user.id) != user_id:
            reply_to_user_id = int(r_user.id)
            ram_user_meta[reply_to_user_id] = {
                "name": r_user.first_name or "User",
                "username": r_user.username or "",
                "lang": getattr(r_user, "language_code", "en") or "en"
            }

    now = time.time()
    hour_bucket = int(now // 3600)
    day_str = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y%m%d")
    slot_idx = get_utc_slot_index(now)

    # Fetch previous last_active BEFORE overwriting
    prev_last_active = ram_last_active.get(chat_id, {}).get(user_id)
    if prev_last_active is None and valkey_client is not None:
        try:
            raw_ts = await valkey_client.hget(f"vk:gc:{chat_id}:last_active", str(user_id))
            if raw_ts:
                prev_last_active = float(raw_ts)
        except Exception:
            pass

    # Update RAM Analytics Immediately (Fast & Lock-Protected)
    async with _ram_lock:
        ram_group_titles[chat_id] = chat_title
        ram_user_meta[user_id] = {
            "name": first_name,
            "username": username,
            "lang": lang_code
        }

        # 24h Live Mirror + Pending Valkey Buffer
        ram_24h_mirror[chat_id][hour_bucket][user_id] += 1
        ram_pending_hourly[chat_id][hour_bucket][user_id] += 1

        # 72-Slot Live Mirror + Pending Valkey Buffer
        ram_user_slots[chat_id][user_id][slot_idx] += 1
        ram_pending_slots[chat_id][day_str][user_id][slot_idx] += 1
        slot_mentioned_tracker[(chat_id, user_id, day_str, slot_idx)] = True

        # Reply Analytics
        if reply_to_user_id:
            ram_replies_out[chat_id][user_id][reply_to_user_id] += 1
            ram_replies_in[chat_id][reply_to_user_id][user_id] += 1
            ram_pending_replies[chat_id][day_str][(user_id, reply_to_user_id)] += 1

        ram_last_active[chat_id][user_id] = now
        ram_pending_last_active[chat_id][user_id] = now

    # Run DM notification checks asynchronously in the background
    asyncio.create_task(
        _process_online_notifications(
            chat_id=chat_id,
            chat_title=chat_title,
            user_id=user_id,
            first_name=first_name,
            prev_last_active=prev_last_active,
            now=now
        )
    )


async def _process_online_notifications(
    chat_id: int,
    chat_title: str,
    user_id: int,
    first_name: str,
    prev_last_active: Optional[float],
    now: float
):
    """Handles Type 1 (/get_notified) and Type 2 (Top 5 Personalized) DM dispatches."""
    global bot
    if bot is None:
        return

    user_mention = format_blue_mention(user_id, first_name)
    safe_gc_title = html.escape(chat_title)

    # ---------------------------------------------------------
    # 1. /get_notified WATCHLIST DM ALERTS (30-Min Cooldown)
    # ---------------------------------------------------------
    watchers: Set[int] = set(ram_watchers.get(chat_id, {}).get(user_id, set()))
    if valkey_client is not None:
        try:
            vk_w = await valkey_client.smembers(f"vk:gn:watch:{chat_id}:{user_id}")
            for w_str in vk_w or []:
                w_id = int(w_str)
                watchers.add(w_id)
                ram_watchers[chat_id][user_id].add(w_id)
        except Exception:
            pass

    for watcher_id in watchers:
        if watcher_id == user_id:
            continue

        # Enforce 30-minute cooldown between 2 notifications
        cd_key = (watcher_id, user_id, chat_id)
        if now - dm_cooldown_tracker.get(cd_key, 0) < DM_NOTIFY_COOLDOWN:
            continue

        w_profile = await get_user_notif_profile(watcher_id)
        if not w_profile.get("get_notify_enabled", True):
            continue

        w_settings = w_profile.get("settings", {})
        if chat_id in w_settings.get("muted_gcs", []):
            continue
        if user_id in w_settings.get("muted_gn_users", []):
            continue

        dm_cooldown_tracker[cd_key] = now
        dm_text = (
            f"🔔 <b>Hey! {user_mention} is online now in {safe_gc_title}!</b>\n"
            f"Go and chat with him/her! 💬✨"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🔕 Turn off notifications for this user",
                    callback_data=f"notif_gn_off_u_{watcher_id}_{user_id}_{chat_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    "⚙️ Notification Settings",
                    callback_data=f"notif_gn_more_{watcher_id}_{user_id}_{chat_id}"
                )
            ]
        ])
        try:
            await bot.send_message(
                chat_id=watcher_id,
                text=dm_text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard
            )
        except Exception as e:
            logger.debug(f"Could not send /get_notified DM to {watcher_id}: {e}")

    # ---------------------------------------------------------
    # 2. PERSONALIZED TOP-5 ENGAGERS DM ALERTS (After 6h Inactivity)
    # ---------------------------------------------------------
    # Refresh Top 5 at most once per 6 hours if needed
    if now - top5_calc_tracker.get((chat_id, user_id), 0) >= TOP5_RECALC_INTERVAL:
        asyncio.create_task(compute_top5_mutual_engagers(chat_id, user_id))

    if prev_last_active is not None and (now - prev_last_active) >= PERSONAL_INACTIVE_GAP:
        last_blast = personal_blast_tracker.get((chat_id, user_id), 0)
        if (now - last_blast) >= PERSONAL_INACTIVE_GAP:
            personal_blast_tracker[(chat_id, user_id)] = now
            top5_friends = await compute_top5_mutual_engagers(chat_id, user_id)

            for friend_id in top5_friends:
                if friend_id == user_id:
                    continue

                cd_key = (friend_id, user_id, chat_id)
                if now - dm_cooldown_tracker.get(cd_key, 0) < DM_NOTIFY_COOLDOWN:
                    continue

                f_profile = await get_user_notif_profile(friend_id)
                if not f_profile.get("personal_notify_enabled", True):
                    continue

                f_settings = f_profile.get("settings", {})
                if chat_id in f_settings.get("muted_gcs", []):
                    continue
                if user_id in f_settings.get("muted_pn_users", []):
                    continue

                dm_cooldown_tracker[cd_key] = now
                friend_dm_text = (
                    f"💖 <b>Your friend {user_mention} is online in {safe_gc_title}!</b>\n"
                    f"If possible, go and chat! 💬✨"
                )
                friend_kb = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "🔕 Turn off personalized notification for this user",
                            callback_data=f"notif_pn_off_u_{friend_id}_{user_id}_{chat_id}"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "⚙️ More Settings",
                            callback_data=f"notif_pn_more_{friend_id}_{user_id}_{chat_id}"
                        )
                    ]
                ])
                try:
                    await bot.send_message(
                        chat_id=friend_id,
                        text=friend_dm_text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=friend_kb
                    )
                except Exception as e:
                    logger.debug(f"Could not send Personalized Friend DM to {friend_id}: {e}")


# =========================================================
# ⏰ 72-SLOT (20-MIN) PERSONALIZED GROUP MENTION ENGINE
# =========================================================
def select_top4_slots_with_2h_gap(slot_counts: Dict[int, int]) -> List[int]:
    """
    Selects up to Top 4 active 20-minute slots (out of 72) such that
    any two selected slots have at least a 2-hour gap (6 slots circular distance).
    """
    sorted_slots = sorted(
        [(s, c) for s, c in slot_counts.items() if c > 0],
        key=lambda x: x[1],
        reverse=True
    )
    selected: List[int] = []
    for slot_idx, _ in sorted_slots:
        if all(min((slot_idx - prev) % TOTAL_SLOTS, (prev - slot_idx) % TOTAL_SLOTS) >= MIN_SLOT_GAP for prev in selected):
            selected.append(slot_idx)
            if len(selected) == 4:
                break
    return selected


async def _get_user_7day_slots(chat_id: int, user_id: int) -> Dict[int, int]:
    """Combines 7-day slot activity from Valkey and RAM."""
    combined: Dict[int, int] = defaultdict(int)
    if valkey_client is not None:
        try:
            now_dt = datetime.now(timezone.utc)
            days = [(now_dt - timedelta(days=i)).strftime("%Y%m%d") for i in range(7)]
            pipe = valkey_client.pipeline(transaction=False)
            for d in days:
                pipe.hgetall(f"vk:slots:{chat_id}:{user_id}:{d}")
            res = await pipe.execute()
            for day_map in res:
                if day_map:
                    for s_str, cnt_str in day_map.items():
                        combined[int(s_str)] += int(cnt_str)
        except Exception:
            pass

    for s_idx, cnt in ram_user_slots.get(chat_id, {}).get(user_id, {}).items():
        combined[s_idx] = max(combined[s_idx], cnt)

    return combined


async def _personalized_slot_mention_worker():
    """
    Checks every 3 minutes for users whose Top 4 active slots match the current 20-min slot,
    enforcing at least 2 hours gap between mentions and tailoring greetings to their country time.
    """
    await asyncio.sleep(45)
    while True:
        try:
            await _ensure_dependencies()
            if bot is not None:
                now = time.time()
                current_slot = get_utc_slot_index(now)
                today_str = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y%m%d")

                active_cids = set(ram_group_titles.keys())
                if valkey_client is not None:
                    try:
                        vk_cids = await valkey_client.smembers("vk:active_gcs")
                        for c_str in vk_cids or []:
                            active_cids.add(int(c_str))
                    except Exception:
                        pass

                for cid in list(active_cids):
                    candidate_uids = set(ram_user_slots.get(cid, {}).keys())
                    if valkey_client is not None:
                        try:
                            vk_uids = await valkey_client.smembers(f"vk:gc:{cid}:users")
                            for u_str in vk_uids or []:
                                candidate_uids.add(int(u_str))
                        except Exception:
                            pass

                    for uid in list(candidate_uids):
                        # Skip if already active or mentioned in this slot today
                        if slot_mentioned_tracker.get((cid, uid, today_str, current_slot), False):
                            continue

                        # Enforce minimum 2 hours (7200s) gap between any two slot mentions for this user
                        if now - last_slot_mention_ts.get((cid, uid), 0) < 7200:
                            continue

                        # Only mention users who were active in the last 3 days (72h)
                        last_act = ram_last_active.get(cid, {}).get(uid, 0)
                        if last_act == 0 and valkey_client is not None:
                            try:
                                raw_l = await valkey_client.hget(f"vk:gc:{cid}:last_active", str(uid))
                                if raw_l:
                                    last_act = float(raw_l)
                            except Exception:
                                pass

                        # Skip if user messaged in the last 20 mins (already chatting!) or inactive > 3 days
                        if last_act <= 0 or (now - last_act) < 1200 or (now - last_act) > RETENTION_3_DAYS:
                            continue

                        slot_counts = await _get_user_7day_slots(cid, uid)
                        top4_slots = select_top4_slots_with_2h_gap(slot_counts)

                        if current_slot in top4_slots:
                            slot_mentioned_tracker[(cid, uid, today_str, current_slot)] = True
                            last_slot_mention_ts[(cid, uid)] = now

                            meta = ram_user_meta.get(uid, {})
                            if not meta and valkey_client is not None:
                                try:
                                    meta = await valkey_client.hgetall(f"vk:u:{uid}") or {}
                                except Exception:
                                    meta = {}

                            u_name = meta.get("name", "Friend")
                            u_lang = meta.get("lang", "en")
                            period = get_local_greeting_period(u_lang, now)
                            mention_html = format_blue_mention(uid, u_name)
                            template = random.choice(PLAYFUL_SLOT_MESSAGES[period])
                            greeting_text = template.format(mention=mention_html)

                            try:
                                await bot.send_message(
                                    chat_id=cid,
                                    text=greeting_text,
                                    parse_mode=ParseMode.HTML
                                )
                                await asyncio.sleep(0.5)
                            except Exception as e:
                                logger.debug(f"Slot mention send failed in {cid} for {uid}: {e}")
        except Exception as e:
            logger.error(f"Error in _personalized_slot_mention_worker: {e}")

        await asyncio.sleep(180)


# =========================================================
# 🔔 COMMAND 1: /get_notified (OR /get_notify)
# =========================================================
async def cmd_get_notified(
    update: Optional[Update] = None,
    context: Optional[CallbackContext] = None,
    bot: Optional[Bot] = None,
    pg_pool: Any = None,
    **kwargs
):
    """Sets up an online DM notification for a replied-to user in a Group Chat."""
    await _ensure_dependencies(bot_obj=bot, pool_obj=pg_pool, context=context)
    if not update or not update.message or not update.message.from_user:
        return

    msg = update.message
    watcher = msg.from_user

    if msg.chat.type not in ("group", "supergroup"):
        await msg.reply_text(
            "❌ <b>Group Only Command!</b>\n"
            "Reply to someone's message inside a group with <code>/get_notified</code> to get a DM when they come online!",
            parse_mode=ParseMode.HTML
        )
        return

    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await msg.reply_text(
            "ℹ️ <b>How to use:</b>\n"
            "Reply to any user's message in this group with <code>/get_notified</code> to receive a DM alert whenever they message here!",
            parse_mode=ParseMode.HTML
        )
        return

    target = msg.reply_to_message.from_user
    if target.id == watcher.id:
        await msg.reply_text("😂 You cannot set online notifications for yourself!", parse_mode=ParseMode.HTML)
        return
    if target.is_bot:
        await msg.reply_text("🤖 You cannot set online notifications for a bot!", parse_mode=ParseMode.HTML)
        return

    chat_id = int(msg.chat_id)
    watcher_id = int(watcher.id)
    target_id = int(target.id)

    ram_user_meta[watcher_id] = {
        "name": watcher.first_name or "User",
        "username": watcher.username or "",
        "lang": getattr(watcher, "language_code", "en") or "en"
    }
    ram_user_meta[target_id] = {
        "name": target.first_name or "User",
        "username": target.username or "",
        "lang": getattr(target, "language_code", "en") or "en"
    }

    profile = await get_user_notif_profile(watcher_id)

    # Check if /get_notified feature is disabled by this user
    if not profile.get("get_notify_enabled", True):
        watcher_mention = format_blue_mention(watcher_id, watcher.first_name or "User")
        enable_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🔔 Enable Get Notified Alerts",
                    callback_data=f"notif_en_gn_{watcher_id}"
                )
            ]
        ])
        await msg.reply_text(
            f"⚠️ Hey {watcher_mention}, your <b>Get Notified</b> alerts are currently disabled!\n\n"
            f"Please click the button below to enable them first, and then use <code>/get_notified</code>:",
            parse_mode=ParseMode.HTML,
            reply_markup=enable_kb
        )
        return

    # Save watcher in RAM, Valkey, and PostgreSQL settings
    ram_watchers[chat_id][target_id].add(watcher_id)
    if valkey_client is not None:
        try:
            await valkey_client.sadd(f"vk:gn:watch:{chat_id}:{target_id}", str(watcher_id))
        except Exception:
            pass

    settings = dict(profile.get("settings", {}))
    # Unmute target user or GC if previously muted
    muted_users = [u for u in settings.get("muted_gn_users", []) if int(u) != target_id]
    muted_gcs = [g for g in settings.get("muted_gcs", []) if int(g) != chat_id]
    watching_list = list(settings.get("watching", []))
    pair = [chat_id, target_id]
    if pair not in watching_list:
        watching_list.append(pair)

    settings["muted_gn_users"] = muted_users
    settings["muted_gcs"] = muted_gcs
    settings["watching"] = watching_list[-50:]  # Keep up to 50 recent watches in JSONB

    await save_user_notif_profile(
        watcher_id,
        first_name=watcher.first_name or "User",
        username=watcher.username or "",
        settings=settings
    )

    target_mention = format_blue_mention(target_id, target.first_name or "User")
    watcher_mention = format_blue_mention(watcher_id, watcher.first_name or "User")
    await msg.reply_text(
        f"✅ <b>Notification Activated!</b> 🔔\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Hey {watcher_mention}, I will send you a DM whenever {target_mention} comes online and messages in <b>{html.escape(msg.chat.title or 'this group')}</b>!\n\n"
        f"<i>(Note: Make sure you have started the bot in DM so I can message you!)</i>",
        parse_mode=ParseMode.HTML
    )


# =========================================================
# ⚙️ COMMAND 2: /notification (SETTINGS CONTROL PANEL)
# =========================================================
def _build_notification_panel(user_id: int, first_name: str, profile: Dict[str, Any]) -> Tuple[str, InlineKeyboardMarkup]:
    gn_on = profile.get("get_notify_enabled", True)
    pn_on = profile.get("personal_notify_enabled", True)
    settings = profile.get("settings", {})
    muted_gcs_cnt = len(settings.get("muted_gcs", []))
    muted_gn_cnt = len(settings.get("muted_gn_users", []))
    muted_pn_cnt = len(settings.get("muted_pn_users", []))

    user_mention = format_blue_mention(user_id, first_name)
    text = (
        f"🔔 <b>NOTIFICATION SETTINGS PANEL</b> ⚙️\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>User:</b> {user_mention}\n\n"
        f"1️⃣ <b>Get Notified Alerts (<code>/get_notified</code>):</b> {'✅ <b>Enabled</b>' if gn_on else '❌ <b>Disabled</b>'}\n"
        f"   ┗ <i>Muted Users: {muted_gn_cnt} | Muted GCs: {muted_gcs_cnt}</i>\n\n"
        f"2️⃣ <b>Personalized Friend Alerts (Top 5):</b> {'✅ <b>Enabled</b>' if pn_on else '❌ <b>Disabled</b>'}\n"
        f"   ┗ <i>Muted Friends: {muted_pn_cnt}</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👇 <i>Tap the buttons below to enable or disable your notifications:</i>"
    )

    gn_btn = (
        InlineKeyboardButton("🔕 Disable Get Notified", callback_data=f"notif_dis_gn_{user_id}")
        if gn_on else
        InlineKeyboardButton("🔔 Enable Get Notified", callback_data=f"notif_en_gn_{user_id}")
    )
    pn_btn = (
        InlineKeyboardButton("🔕 Disable Personalized", callback_data=f"notif_dis_pn_{user_id}")
        if pn_on else
        InlineKeyboardButton("💖 Enable Personalized", callback_data=f"notif_en_pn_{user_id}")
    )

    keyboard = InlineKeyboardMarkup([
        [gn_btn],
        [pn_btn],
        [
            InlineKeyboardButton("✅ Enable All & Unmute", callback_data=f"notif_en_all_{user_id}"),
            InlineKeyboardButton("🚫 Disable All", callback_data=f"notif_dis_all_{user_id}")
        ]
    ])
    return text, keyboard


async def cmd_notification(
    update: Optional[Update] = None,
    context: Optional[CallbackContext] = None,
    bot: Optional[Bot] = None,
    pg_pool: Any = None,
    **kwargs
):
    """Opens the interactive /notification settings panel."""
    await _ensure_dependencies(bot_obj=bot, pool_obj=pg_pool, context=context)
    if not update or not update.message or not update.message.from_user:
        return

    msg = update.message
    user = msg.from_user
    user_id = int(user.id)

    ram_user_meta[user_id] = {
        "name": user.first_name or "User",
        "username": user.username or "",
        "lang": getattr(user, "language_code", "en") or "en"
    }

    profile = await get_user_notif_profile(user_id)
    panel_text, panel_kb = _build_notification_panel(user_id, user.first_name or "User", profile)
    await msg.reply_text(panel_text, parse_mode=ParseMode.HTML, reply_markup=panel_kb)


# =========================================================
# 🏆 COMMAND 3: /ranking (ADMIN-ONLY 24H GROUP LEADERBOARD)
# =========================================================
async def _is_user_group_admin(chat_id: int, user_id: int, bot_obj: Bot) -> bool:
    """Checks if user_id is an administrator or creator in chat_id (cached for 5 mins)."""
    now = time.time()
    cached = admin_cache.get(chat_id)
    if cached and cached.get("expires_at", 0) > now:
        if user_id in cached["admins"]:
            return True

    try:
        member = await bot_obj.get_chat_member(chat_id=chat_id, user_id=user_id)
        if member.status in ("administrator", "creator"):
            if chat_id not in admin_cache or admin_cache[chat_id].get("expires_at", 0) <= now:
                admin_cache[chat_id] = {"admins": {user_id}, "expires_at": now + 300}
            else:
                admin_cache[chat_id]["admins"].add(user_id)
            return True
        return False
    except Exception as e:
        logger.debug(f"Admin check failed in {chat_id} for {user_id}: {e}")
        return False


async def cmd_ranking(
    update: Optional[Update] = None,
    context: Optional[CallbackContext] = None,
    bot: Optional[Bot] = None,
    pg_pool: Any = None,
    **kwargs
):
    """
    Admin-only command: Shows last 24h total messages, total active users,
    and Top 10 most active users with blue clickable mentions (combining Valkey + live Server RAM).
    """
    await _ensure_dependencies(bot_obj=bot, pool_obj=pg_pool, context=context)
    if not update or not update.message or not update.message.from_user:
        return

    msg = update.message
    user = msg.from_user
    chat = msg.chat

    if not chat or chat.type not in ("group", "supergroup"):
        await msg.reply_text(
            "❌ <b>Group Only Command!</b>\n"
            "<code>/ranking</code> can only be used inside group chats by group admins.",
            parse_mode=ParseMode.HTML
        )
        return

    chat_id = int(chat.id)
    user_id = int(user.id)
    active_bot = bot or (context.bot if context else None)
    if not active_bot:
        return

    # Verify Group Admin Status
    is_admin = await _is_user_group_admin(chat_id, user_id, active_bot)
    if not is_admin:
        await msg.reply_text(
            "⛔ <b>Admin Only Command!</b>\n"
            "Only group administrators can check the 24-hour group activity <code>/ranking</code>.",
            parse_mode=ParseMode.HTML
        )
        return

    now = time.time()
    current_hour = int(now // 3600)
    last_24_hours = [current_hour - i for i in range(24)]

    # Step 1: Fetch Valkey 24h hourly buckets
    valkey_hourly: Dict[int, Dict[int, int]] = defaultdict(lambda: defaultdict(int))
    if valkey_client is not None:
        try:
            pipe = valkey_client.pipeline(transaction=False)
            for h in last_24_hours:
                pipe.hgetall(f"vk:gc:{chat_id}:h:{h}")
            res = await pipe.execute()
            for h, h_map in zip(last_24_hours, res):
                if h_map:
                    for u_str, cnt_str in h_map.items():
                        valkey_hourly[h][int(u_str)] += int(cnt_str)
        except Exception as e:
            logger.debug(f"Valkey read in /ranking fallback to RAM: {e}")

    # Step 2: Merge Valkey + Pending Unsynced RAM + Live 24h RAM Mirror
    user_24h_totals: Dict[int, int] = defaultdict(int)
    async with _ram_lock:
        for h in last_24_hours:
            vk_map = valkey_hourly.get(h, {})
            pend_map = ram_pending_hourly.get(chat_id, {}).get(h, {})
            mirror_map = ram_24h_mirror.get(chat_id, {}).get(h, {})

            all_uids = set(vk_map.keys()) | set(pend_map.keys()) | set(mirror_map.keys())
            for uid in all_uids:
                combined_vk_pend = vk_map.get(uid, 0) + pend_map.get(uid, 0)
                live_mirror = mirror_map.get(uid, 0)
                user_24h_totals[uid] += max(combined_vk_pend, live_mirror)

    total_messages_24h = sum(user_24h_totals.values())
    total_active_users_24h = len([u for u, c in user_24h_totals.items() if c > 0])
    sorted_top10 = sorted(
        [(u, c) for u, c in user_24h_totals.items() if c > 0],
        key=lambda x: x[1],
        reverse=True
    )[:10]

    # Step 3: Resolve user names from RAM -> Valkey -> PostgreSQL
    missing_uids = [u for u, _ in sorted_top10 if u not in ram_user_meta]
    if missing_uids and valkey_client is not None:
        try:
            pipe = valkey_client.pipeline(transaction=False)
            for u in missing_uids:
                pipe.hgetall(f"vk:u:{u}")
            metas = await pipe.execute()
            for u, m in zip(missing_uids, metas):
                if m and m.get("name"):
                    ram_user_meta[u] = {
                        "name": m.get("name", "User"),
                        "username": m.get("username", ""),
                        "lang": m.get("lang", "en")
                    }
        except Exception:
            pass

    still_missing = [u for u, _ in sorted_top10 if u not in ram_user_meta]
    if still_missing and pg_pool is not None:
        try:
            async with pg_pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT user_id, first_name, username FROM users WHERE user_id = ANY($1::bigint[]);",
                    still_missing
                )
                for r in rows:
                    ram_user_meta[int(r["user_id"])] = {
                        "name": r["first_name"] or "User",
                        "username": r["username"] or "",
                        "lang": "en"
                    }
        except Exception:
            pass

    safe_title = html.escape(chat.title or "Group Chat")
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

    lines = [
        f"📊 <b>24-HOUR GROUP ACTIVITY RANKING</b> 🏆",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"🏷️ <b>Group:</b> {safe_title}",
        f"💬 <b>Total Messages (24h):</b> <code>{total_messages_24h:,}</code>",
        f"👥 <b>Active Users (24h):</b> <code>{total_active_users_24h:,}</code>",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"🔥 <b>TOP 10 ACTIVE CHATTERS (24H):</b>"
    ]

    if not sorted_top10:
        lines.append("<i>No user messages recorded yet in the last 24 hours! Start chatting now!</i>")
    else:
        for idx, (uid, cnt) in enumerate(sorted_top10):
            medal = medals[idx] if idx < len(medals) else f"#{idx + 1}"
            u_name = ram_user_meta.get(uid, {}).get("name", f"User {uid}")
            blue_mention = format_blue_mention(uid, u_name)
            lines.append(f"{medal} <b>{blue_mention}</b> — <code>{cnt:,} msgs</code>")

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("⚡ <i>Live analytics powered by Server RAM & Valkey</i>")

    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# =========================================================
# 🔘 INTERACTIVE CALLBACK HANDLER (USER-LOCKED SECURITY)
# =========================================================
async def handle_notification_callback(
    update: Optional[Update] = None,
    context: Optional[CallbackContext] = None,
    query: Any = None,
    bot: Optional[Bot] = None,
    pg_pool: Any = None,
    **kwargs
) -> bool:
    """
    Processes all notification inline buttons:
    - Enforces strict user_id ownership check so other GC members cannot click someone else's button.
    - Updates 3-hour RAM cache + PostgreSQL users table.
    """
    await _ensure_dependencies(bot_obj=bot, pool_obj=pg_pool, context=context)
    q = query or (update.callback_query if update else None)
    if not q or not q.data:
        return False

    data = q.data
    clicker = q.from_user
    if not clicker:
        return False
    clicker_id = int(clicker.id)

    # ---------------------------------------------------------
    # A. /notification Panel & Enable/Disable Toggles
    # ---------------------------------------------------------
    for prefix in ("notif_en_gn_", "notif_dis_gn_", "notif_en_pn_", "notif_dis_pn_", "notif_en_all_", "notif_dis_all_"):
        if data.startswith(prefix):
            try:
                owner_id = int(data[len(prefix):])
            except ValueError:
                return True

            if clicker_id != owner_id:
                await q.answer("❌ This settings button is only for the user who requested it!", show_alert=True)
                return True

            profile = await get_user_notif_profile(owner_id)
            settings = dict(profile.get("settings", {}))

            if prefix == "notif_en_gn_":
                profile = await save_user_notif_profile(
                    owner_id,
                    first_name=clicker.first_name or "User",
                    username=clicker.username or "",
                    notify_enabled=True,
                    get_notify_enabled=True
                )
                toast = "🔔 Get Notified alerts enabled!"
            elif prefix == "notif_dis_gn_":
                profile = await save_user_notif_profile(
                    owner_id,
                    first_name=clicker.first_name or "User",
                    username=clicker.username or "",
                    get_notify_enabled=False
                )
                toast = "🔕 Get Notified alerts disabled!"
            elif prefix == "notif_en_pn_":
                profile = await save_user_notif_profile(
                    owner_id,
                    first_name=clicker.first_name or "User",
                    username=clicker.username or "",
                    notify_enabled=True,
                    personal_notify_enabled=True
                )
                toast = "💖 Personalized friend notifications enabled!"
            elif prefix == "notif_dis_pn_":
                profile = await save_user_notif_profile(
                    owner_id,
                    first_name=clicker.first_name or "User",
                    username=clicker.username or "",
                    personal_notify_enabled=False
                )
                toast = "🔕 Personalized friend notifications disabled!"
            elif prefix == "notif_en_all_":
                settings["muted_gcs"] = []
                settings["muted_gn_users"] = []
                settings["muted_pn_users"] = []
                profile = await save_user_notif_profile(
                    owner_id,
                    first_name=clicker.first_name or "User",
                    username=clicker.username or "",
                    notify_enabled=True,
                    get_notify_enabled=True,
                    personal_notify_enabled=True,
                    settings=settings
                )
                toast = "✅ All notifications enabled & muted lists cleared!"
            else:  # notif_dis_all_
                profile = await save_user_notif_profile(
                    owner_id,
                    first_name=clicker.first_name or "User",
                    username=clicker.username or "",
                    notify_enabled=False,
                    get_notify_enabled=False,
                    personal_notify_enabled=False
                )
                toast = "🚫 All notifications disabled!"

            panel_text, panel_kb = _build_notification_panel(owner_id, clicker.first_name or "User", profile)
            try:
                await q.edit_message_text(text=panel_text, parse_mode=ParseMode.HTML, reply_markup=panel_kb)
            except Exception:
                pass
            await q.answer(toast, show_alert=True)
            return True

    # ---------------------------------------------------------
    # B. /get_notified DM Buttons
    # ---------------------------------------------------------
    if data.startswith("notif_gn_more_"):
        # Format: notif_gn_more_{owner_id}_{target_id}_{chat_id}
        parts = data[len("notif_gn_more_"):].split("_")
        if len(parts) >= 3:
            owner_id, target_id, chat_id = int(parts[0]), int(parts[1]), int(parts[2])
            if clicker_id != owner_id:
                await q.answer("❌ This button is not for you!", show_alert=True)
                return True

            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔕 Turn off notifications for this user",
                        callback_data=f"notif_gn_off_u_{owner_id}_{target_id}_{chat_id}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🔇 Turn off all notifications from this GC",
                        callback_data=f"notif_gn_off_gc_{owner_id}_{chat_id}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🚫 Turn off all notifications",
                        callback_data=f"notif_gn_off_all_{owner_id}"
                    )
                ]
            ])
            try:
                await q.edit_message_reply_markup(reply_markup=kb)
            except Exception:
                pass
            await q.answer("⚙️ More notification options opened!")
            return True

    if data.startswith("notif_gn_off_u_"):
        # Format: notif_gn_off_u_{owner_id}_{target_id}_{chat_id}
        parts = data[len("notif_gn_off_u_"):].split("_")
        if len(parts) >= 3:
            owner_id, target_id, chat_id = int(parts[0]), int(parts[1]), int(parts[2])
            if clicker_id != owner_id:
                await q.answer("❌ This button is not for you!", show_alert=True)
                return True

            ram_watchers.get(chat_id, {}).get(target_id, set()).discard(owner_id)
            if valkey_client is not None:
                try:
                    await valkey_client.srem(f"vk:gn:watch:{chat_id}:{target_id}", str(owner_id))
                except Exception:
                    pass

            profile = await get_user_notif_profile(owner_id)
            settings = dict(profile.get("settings", {}))
            muted_u = set(settings.get("muted_gn_users", []))
            muted_u.add(target_id)
            settings["muted_gn_users"] = list(muted_u)
            settings["watching"] = [
                w for w in settings.get("watching", [])
                if not (isinstance(w, list) and len(w) == 2 and int(w[0]) == chat_id and int(w[1]) == target_id)
            ]
            await save_user_notif_profile(owner_id, settings=settings)

            try:
                await q.edit_message_text(
                    "🔕 <b>Notifications turned off for this user.</b>\n"
                    "You can manage or re-enable alerts anytime using <code>/notification</code>.",
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass
            await q.answer("🔕 Turned off notifications for this user!", show_alert=True)
            return True

    if data.startswith("notif_gn_off_gc_"):
        # Format: notif_gn_off_gc_{owner_id}_{chat_id}
        parts = data[len("notif_gn_off_gc_"):].split("_")
        if len(parts) >= 2:
            owner_id, chat_id = int(parts[0]), int(parts[1])
            if clicker_id != owner_id:
                await q.answer("❌ This button is not for you!", show_alert=True)
                return True

            profile = await get_user_notif_profile(owner_id)
            settings = dict(profile.get("settings", {}))
            muted_gcs = set(settings.get("muted_gcs", []))
            muted_gcs.add(chat_id)
            settings["muted_gcs"] = list(muted_gcs)
            await save_user_notif_profile(owner_id, settings=settings)

            try:
                await q.edit_message_text(
                    "🔇 <b>All notifications from this group chat have been turned off.</b>\n"
                    "Use <code>/notification</code> anytime to re-enable them.",
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass
            await q.answer("🔇 Notifications muted for this group!", show_alert=True)
            return True

    if data.startswith("notif_gn_off_all_"):
        owner_id = int(data[len("notif_gn_off_all_"):])
        if clicker_id != owner_id:
            await q.answer("❌ This button is not for you!", show_alert=True)
            return True

        await save_user_notif_profile(
            owner_id,
            notify_enabled=False,
            get_notify_enabled=False
        )
        try:
            await q.edit_message_text(
                "🚫 <b>All Get Notified alerts have been turned off.</b>\n"
                "Send <code>/notification</code> anytime to enable them again.",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass
        await q.answer("🚫 Turned off all notifications!", show_alert=True)
        return True

    # ---------------------------------------------------------
    # C. Personalized Friend Notification DM Buttons
    # ---------------------------------------------------------
    if data.startswith("notif_pn_more_"):
        # Format: notif_pn_more_{owner_id}_{friend_id}_{chat_id}
        parts = data[len("notif_pn_more_"):].split("_")
        if len(parts) >= 3:
            owner_id, friend_id, chat_id = int(parts[0]), int(parts[1]), int(parts[2])
            if clicker_id != owner_id:
                await q.answer("❌ This button is not for you!", show_alert=True)
                return True

            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔕 Turn off personalized notification for this user",
                        callback_data=f"notif_pn_off_u_{owner_id}_{friend_id}_{chat_id}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🚫 Turn off personalized notification",
                        callback_data=f"notif_pn_off_all_{owner_id}"
                    )
                ]
            ])
            try:
                await q.edit_message_reply_markup(reply_markup=kb)
            except Exception:
                pass
            await q.answer("⚙️ More settings opened!")
            return True

    if data.startswith("notif_pn_off_u_"):
        # Format: notif_pn_off_u_{owner_id}_{friend_id}_{chat_id}
        parts = data[len("notif_pn_off_u_"):].split("_")
        if len(parts) >= 3:
            owner_id, friend_id, chat_id = int(parts[0]), int(parts[1]), int(parts[2])
            if clicker_id != owner_id:
                await q.answer("❌ This button is not for you!", show_alert=True)
                return True

            profile = await get_user_notif_profile(owner_id)
            settings = dict(profile.get("settings", {}))
            muted_pn = set(settings.get("muted_pn_users", []))
            muted_pn.add(friend_id)
            settings["muted_pn_users"] = list(muted_pn)
            await save_user_notif_profile(owner_id, settings=settings)

            try:
                await q.edit_message_text(
                    "🔕 <b>Personalized notifications turned off for this user.</b>\n"
                    "You can manage your preferences anytime with <code>/notification</code>.",
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass
            await q.answer("🔕 Personalized alerts muted for this friend!", show_alert=True)
            return True

    if data.startswith("notif_pn_off_all_"):
        owner_id = int(data[len("notif_pn_off_all_"):])
        if clicker_id != owner_id:
            await q.answer("❌ This button is not for you!", show_alert=True)
            return True

        await save_user_notif_profile(
            owner_id,
            personal_notify_enabled=False
        )
        try:
            await q.edit_message_text(
                "🚫 <b>All Personalized Friend Notifications have been turned off.</b>\n"
                "Use <code>/notification</code> anytime to turn them back on!",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass
        await q.answer("🚫 Turned off personalized notifications!", show_alert=True)
        return True

    return False
