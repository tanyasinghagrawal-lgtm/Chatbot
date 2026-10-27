import os
import json
import time
import hmac
import hashlib
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, APIRouter, Request, Response, Query
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

logger = logging.getLogger("TanyaAnalytics")

# Timezone Definition (IST)
IST = timezone(timedelta(hours=5, minutes=30))

# Default Admin Password & Secret Token Signer
ADMIN_DEFAULT_PASSWORD = os.getenv("ANALYTICS_ADMIN_PASSWORD", "##8051Mritunjay")
AUTH_SECRET_KEY = hashlib.sha256(f"tanya_analytics_secret_{ADMIN_DEFAULT_PASSWORD}".encode()).hexdigest()

router = APIRouter()
_bot_module = None


def setup_analytics_routes(app: FastAPI, bot_module: Any):
    """Mounts all /anylise routes onto the main FastAPI application."""
    global _bot_module
    _bot_module = bot_module
    app.include_router(router)
    logger.info("✨ Tanya Analytics Engine mounted at /anylise")


# =========================================================
# 🔐 AUTHENTICATION HELPERS
# =========================================================
def create_session_token() -> str:
    exp = int(time.time()) + (86400 * 7)  # 7 days validity
    payload = f"admin:{exp}"
    sig = hmac.new(AUTH_SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def verify_session_token(token: Optional[str]) -> bool:
    if not token:
        return False
    try:
        parts = token.split(":")
        if len(parts) != 3:
            return False
        role, exp_str, sig = parts
        if int(exp_str) < int(time.time()):
            return False
        expected_sig = hmac.new(AUTH_SECRET_KEY.encode(), f"{role}:{exp_str}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected_sig)
    except Exception:
        return False


def is_authenticated(request: Request) -> bool:
    token = request.cookies.get("tanya_admin_token") or request.headers.get("X-Admin-Token")
    return verify_session_token(token)


# =========================================================
# ⚡ VALKEY SERVER-SIDE LUA OFFLOADING ENGINE
# =========================================================
# This Lua script executes DIRECTLY inside the Valkey/Redis server's C-engine.
# It scans daily log lists, filters by timestamp/chat_type/media, aggregates graphs,
# and returns ONLY the compact summary JSON so the Bot Server uses near-ZERO CPU & RAM!
LUA_AGGREGATE_DASHBOARD = """
local start_ts = tonumber(ARGV[1])
local end_ts = tonumber(ARGV[2])
local bucket_size = tonumber(ARGV[3])
local chat_filter = ARGV[4]      -- 'all', 'gc', 'dm'
local type_filter = ARGV[5]      -- 'all', 'message', 'command', 'media', 'callback'

local total_webhooks = 0
local total_msgs = 0
local gc_webhooks = 0
local dm_webhooks = 0
local gc_msgs = 0
local dm_msgs = 0
local total_commands = 0
local total_callbacks = 0
local total_files_gc = 0
local total_files_dm = 0

local media_gc = {sticker=0, gif=0, photo=0, video=0, voice=0, audio=0, video_note=0, document=0}
local media_dm = {sticker=0, gif=0, photo=0, video=0, voice=0, audio=0, video_note=0, document=0}

local active_users = {}
local active_gcs = {}
local cmd_counts = {}
local hourly_buckets = {}
local hourly_gc = {}
local hourly_dm = {}
local user_names = {}
local gc_titles = {}

for k_idx, key in ipairs(KEYS) do
    local items = redis.call('LRANGE', key, 0, -1)
    for i = 1, #items do
        local ok, ev = pcall(cjson.decode, items[i])
        if ok and type(ev) == 'table' then
            local t = tonumber(ev['t']) or 0
            if t >= start_ts and t <= end_ts then
                local c = ev['c'] or 'dm'
                local tp = ev['tp'] or 'message'
                local mtype = ev['mtype']
                local is_media = (mtype ~= nil and mtype ~= cjson.null and mtype ~= '')

                local pass_chat = (chat_filter == 'all') or (chat_filter == c)
                local pass_type = (type_filter == 'all')
                    or (type_filter == 'media' and is_media)
                    or (type_filter == tp)

                if pass_chat and pass_type then
                    total_webhooks = total_webhooks + 1
                    local b_key = tostring(math.floor(t / bucket_size) * bucket_size)
                    hourly_buckets[b_key] = (hourly_buckets[b_key] or 0) + 1

                    if c == 'gc' then
                        gc_webhooks = gc_webhooks + 1
                        hourly_gc[b_key] = (hourly_gc[b_key] or 0) + 1
                    else
                        dm_webhooks = dm_webhooks + 1
                        hourly_dm[b_key] = (hourly_dm[b_key] or 0) + 1
                    end

                    if tp == 'command' then
                        total_commands = total_commands + 1
                        local cmd = ev['cmd'] or '/unknown'
                        cmd_counts[cmd] = (cmd_counts[cmd] or 0) + 1
                    elseif tp == 'callback' then
                        total_callbacks = total_callbacks + 1
                    else
                        total_msgs = total_msgs + 1
                        if c == 'gc' then
                            gc_msgs = gc_msgs + 1
                        else
                            dm_msgs = dm_msgs + 1
                        end
                    end

                    if is_media then
                        if c == 'gc' then
                            total_files_gc = total_files_gc + 1
                            media_gc[mtype] = (media_gc[mtype] or 0) + 1
                        else
                            total_files_dm = total_files_dm + 1
                            media_dm[mtype] = (media_dm[mtype] or 0) + 1
                        end
                    end

                    local uid = ev['uid']
                    if uid and uid ~= cjson.null then
                        local u_str = tostring(uid)
                        active_users[u_str] = (active_users[u_str] or 0) + 1
                        local fn = ev['fname'] or ev[' fname']
                        if fn and fn ~= cjson.null and fn ~= '' then
                            user_names[u_str] = fn
                        end
                    end

                    local gid = ev['gid']
                    if gid and gid ~= cjson.null then
                        local g_str = tostring(gid)
                        active_gcs[g_str] = (active_gcs[g_str] or 0) + 1
                        local gt = ev['gtitle']
                        if gt and gt ~= cjson.null and gt ~= '' then
                            gc_titles[g_str] = gt
                        end
                    end
                end
            end
        end
    end
end

local unique_user_cnt = 0
for _ in pairs(active_users) do unique_user_cnt = unique_user_cnt + 1 end

local unique_gc_cnt = 0
for _ in pairs(active_gcs) do unique_gc_cnt = unique_gc_cnt + 1 end

return cjson.encode({
    total_webhooks = total_webhooks,
    total_msgs = total_msgs,
    gc_webhooks = gc_webhooks,
    dm_webhooks = dm_webhooks,
    gc_msgs = gc_msgs,
    dm_msgs = dm_msgs,
    total_commands = total_commands,
    total_callbacks = total_callbacks,
    total_files_gc = total_files_gc,
    total_files_dm = total_files_dm,
    unique_users = unique_user_cnt,
    unique_gcs = unique_gc_cnt,
    media_gc = media_gc,
    media_dm = media_dm,
    hourly_buckets = hourly_buckets,
    hourly_gc = hourly_gc,
    hourly_dm = hourly_dm,
    active_users = active_users,
    active_gcs = active_gcs,
    cmd_counts = cmd_counts,
    user_names = user_names,
    gc_titles = gc_titles
})
"""

# Server-Side Lua Script for Infinite Scroll Chat & Timeline Slicing
LUA_FETCH_CHAT_SLICE = """
local mode = ARGV[1]          -- 'gc', 'user', 'dm', 'all'
local target_id = ARGV[2]     -- gid or uid string
local cursor_ts = tonumber(ARGV[3])
local direction = ARGV[4]     -- 'before' (scroll up) or 'after' (scroll down)
local limit = tonumber(ARGV[5])
local chat_subfilter = ARGV[6] -- 'all', 'gc', 'dm'
local search_q = string.lower(ARGV[7] or '')

local matched = {}

for k_idx, key in ipairs(KEYS) do
    local is_ai_key = string.find(key, 'tg:ai_replies:') ~= nil
    local items = redis.call('LRANGE', key, 0, -1)
    for i = 1, #items do
        local ok, ev = pcall(cjson.decode, items[i])
        if ok and type(ev) == 'table' then
            local t = tonumber(ev['t']) or 0
            local c = ev['c'] or 'dm'
            local uid = ev['uid'] and tostring(ev['uid']) or ''
            local gid = ev['gid'] and tostring(ev['gid']) or ''

            local time_ok = false
            if direction == 'before' then
                time_ok = (t <= cursor_ts)
            else
                time_ok = (t >= cursor_ts)
            end

            if time_ok then
                local match = false
                if mode == 'gc' then
                    match = (c == 'gc' and gid == target_id)
                elseif mode == 'user' then
                    match = (uid == target_id) and (chat_subfilter == 'all' or chat_subfilter == c)
                elseif mode == 'dm' then
                    match = (c == 'dm' and uid == target_id)
                elseif mode == 'all' then
                    match = (chat_subfilter == 'all' or chat_subfilter == c)
                end

                if match and search_q ~= '' then
                    local msg_txt = string.lower(tostring(ev['msg'] or ev['u_msg'] or '') .. ' ' .. tostring(ev['b_rep'] or ''))
                    if string.find(msg_txt, search_q, 1, true) == nil then
                        match = false
                    end
                end

                if match then
                    ev['_is_ai'] = is_ai_key
                    table.insert(matched, ev)
                end
            end
        end
    end
end

table.sort(matched, function(a, b)
    local ta = tonumber(a['t']) or 0
    local tb = tonumber(b['t']) or 0
    if ta == tb then
        local ma = tonumber(a['mid'] or a['b_mid'] or a['u_mid'] or 0) or 0
        local mb = tonumber(b['mid'] or b['b_mid'] or b['u_mid'] or 0) or 0
        return ma < mb
    end
    return ta < tb
end)

local total_matched = #matched
local result = {}
local has_more = false

if direction == 'before' then
    local start_idx = math.max(1, total_matched - limit + 1)
    if start_idx > 1 then has_more = true end
    for i = start_idx, total_matched do
        table.insert(result, matched[i])
    end
else
    local end_idx = math.min(total_matched, limit)
    if total_matched > limit then has_more = true end
    for i = 1, end_idx do
        table.insert(result, matched[i])
    end
end

return cjson.encode({
    items = result,
    has_more = has_more,
    total_matched = total_matched
})
"""


def get_date_keys_for_range(start_ts: int, end_ts: int, prefix: str = "tg:webhook_logs") -> List[str]:
    """Generates Valkey daily list keys covering [start_ts, end_ts] in IST."""
    start_dt = datetime.fromtimestamp(max(0, start_ts - 3600), IST).date()
    end_dt = datetime.fromtimestamp(end_ts + 3600, IST).date()
    keys = []
    curr = start_dt
    while curr <= end_dt and len(keys) <= 32:
        keys.append(f"{prefix}:{curr.strftime('%Y-%m-%d')}")
        curr += timedelta(days=1)
    return keys


async def ensure_latest_ram_flushed():
    """Flushes any pending un-synced RAM telemetry in bot.py to Valkey before querying."""
    if _bot_module and hasattr(_bot_module, "flush_ram_telemetry_to_valkey"):
        try:
            await _bot_module.flush_ram_telemetry_to_valkey()
        except Exception as e:
            logger.debug(f"RAM flush trigger error: {e}")


async def enrich_names_from_postgres(uids: List[int], gids: List[int]) -> Dict[str, Dict[str, Any]]:
    """Fetches user names, usernames, balances, and group titles from PostgreSQL + RAM cache in 1 fast query."""
    user_map: Dict[str, Dict[str, Any]] = {}
    group_map: Dict[str, Dict[str, Any]] = {}

    if not _bot_module:
        return {"users": user_map, "groups": group_map}

    # 1. Check Bot RAM caches first (Zero DB cost)
    u_cache = getattr(_bot_module, "user_cache", {})
    for u in uids:
        if u in u_cache:
            user_map[str(u)] = {
                "first_name": u_cache[u].get("first_name") or "User",
                "username": u_cache[u].get("username") or "",
                "balance": 0
            }

    g_cache = getattr(_bot_module, "group_gdp_cache", {})
    for g in gids:
        if g in g_cache:
            group_map[str(g)] = {
                "title": g_cache[g].get("title") or f"Group {g}",
                "gdp": g_cache[g].get("gdp") or 0
            }

    # 2. Query PostgreSQL for remaining IDs
    pg_pool = getattr(_bot_module, "pg_pool", None)
    if pg_pool:
        try:
            async with pg_pool.acquire() as conn:
                if uids:
                    rows = await conn.fetch(
                        "SELECT user_id, first_name, username, total_balance FROM users WHERE user_id = ANY($1::bigint[]);",
                        uids[:300]
                    )
                    for r in rows:
                        user_map[str(r["user_id"])] = {
                            "first_name": r["first_name"] or "User",
                            "username": r["username"] or "",
                            "balance": r["total_balance"] or 0
                        }
                if gids:
                    g_rows = await conn.fetch(
                        "SELECT chat_id, title, gdp FROM groups_gdp WHERE chat_id = ANY($1::bigint[]);",
                        gids[:200]
                    )
                    for r in g_rows:
                        group_map[str(r["chat_id"])] = {
                            "title": r["title"] or f"Group {r['chat_id']}",
                            "gdp": r["gdp"] or 0
                        }
        except Exception as e:
            logger.debug(f"Metadata enrichment DB warning: {e}")

    return {"users": user_map, "groups": group_map}


# =========================================================
# 🌐 API ENDPOINTS FOR /anylise
# =========================================================

@router.post("/anylise/api/login")
async def analytics_login(request: Request):
    """Validates admin password and sets an HTTP-only signed session cookie."""
    try:
        body = await request.json()
        password = body.get("password", "")
    except Exception:
        password = ""

    if password == ADMIN_DEFAULT_PASSWORD:
        token = create_session_token()
        resp = JSONResponse({"status": "ok", "token": token})
        resp.set_cookie(
            key="tanya_admin_token",
            value=token,
            max_age=86400 * 7,
            httponly=True,
            samesite="lax"
        )
        return resp
    return JSONResponse({"status": "error", "message": "Invalid Admin Password! Access Denied."}, status_code=401)


@router.post("/anylise/api/logout")
async def analytics_logout():
    resp = JSONResponse({"status": "ok"})
    resp.delete_cookie("tanya_admin_token")
    return resp


@router.get("/anylise/api/overview")
async def analytics_overview(
    request: Request,
    start_ts: Optional[int] = Query(None),
    end_ts: Optional[int] = Query(None),
    chat_filter: str = Query("all"),
    type_filter: str = Query("all"),
    force_sync: bool = Query(True)
):
    """
    Executes server-side aggregation on Valkey for any custom time window (default: Last 24 Hours).
    """
    if not is_authenticated(request):
        return JSONResponse({"status": "unauthorized"}, status_code=401)

    if force_sync:
        await ensure_latest_ram_flushed()

    now_ts = int(time.time())
    if not end_ts:
        end_ts = now_ts
    if not start_ts:
        start_ts = end_ts - 86400  # Default Last 24 Hours

    span = max(60, end_ts - start_ts)
    # Smart dynamic bucket size for graphs
    if span <= 7200:
        bucket_size = 300       # 5-min buckets for <= 2 hours
    elif span <= 172800:
        bucket_size = 3600      # 1-hour buckets for <= 48 hours
    else:
        bucket_size = 14400     # 4-hour buckets for multi-day ranges

    valkey_client = _bot_module._resolve_valkey_client() if _bot_module else None
    if not valkey_client:
        return JSONResponse({"status": "error", "message": "Valkey client not connected"}, status_code=500)

    log_keys = get_date_keys_for_range(start_ts, end_ts, "tg:webhook_logs")
    ai_keys = get_date_keys_for_range(start_ts, end_ts, "tg:ai_replies")

    try:
        raw_res = await valkey_client.eval(
            LUA_AGGREGATE_DASHBOARD,
            len(log_keys),
            *log_keys,
            str(start_ts),
            str(end_ts),
            str(bucket_size),
            chat_filter,
            type_filter
        )
        data = json.loads(raw_res) if isinstance(raw_res, str) else raw_res
    except Exception as e:
        logger.error(f"Valkey Lua eval error in overview: {e}", exc_info=True)
        return JSONResponse({"status": "error", "message": f"Valkey Eval Error: {e}"}, status_code=500)

    # Count AI Replies in time window
    ai_total = 0
    ai_gc = 0
    ai_dm = 0
    try:
        pipe = valkey_client.pipeline(transaction=False)
        for k in ai_keys:
            pipe.lrange(k, 0, -1)
        ai_lists = await pipe.execute()
        for lst in ai_lists:
            for item_str in lst:
                try:
                    ev = json.loads(item_str)
                    t = int(ev.get("t", 0))
                    if start_ts <= t <= end_ts:
                        c = ev.get("c", "dm")
                        if chat_filter in ("all", c):
                            ai_total += 1
                            if c == "gc":
                                ai_gc += 1
                            else:
                                ai_dm += 1
                except Exception:
                    pass
    except Exception:
        pass

    # Process Top Active Users & Groups
    active_users_dict = data.get("active_users") or {}
    if isinstance(active_users_dict, list):
        active_users_dict = {}
    top_users_raw = sorted(active_users_dict.items(), key=lambda x: x[1], reverse=True)[:50]

    active_gcs_dict = data.get("active_gcs") or {}
    if isinstance(active_gcs_dict, list):
        active_gcs_dict = {}
    top_gcs_raw = sorted(active_gcs_dict.items(), key=lambda x: x[1], reverse=True)[:50]

    uids_to_lookup = [int(u) for u, _ in top_users_raw if str(u).lstrip("-").isdigit()]
    gids_to_lookup = [int(g) for g, _ in top_gcs_raw if str(g).lstrip("-").isdigit()]
    meta = await enrich_names_from_postgres(uids_to_lookup, gids_to_lookup)

    valkey_user_names = data.get("user_names") or {}
    if isinstance(valkey_user_names, list):
        valkey_user_names = {}
    valkey_gc_titles = data.get("gc_titles") or {}
    if isinstance(valkey_gc_titles, list):
        valkey_gc_titles = {}

    top_users = []
    for u_str, count in top_users_raw:
        u_info = meta["users"].get(u_str, {})
        fname = u_info.get("first_name") or valkey_user_names.get(u_str) or f"User {u_str}"
        uname = u_info.get("username") or ""
        top_users.append({
            "uid": u_str,
            "first_name": fname,
            "username": uname,
            "balance": u_info.get("balance", 0),
            "count": count
        })

    top_groups = []
    for g_str, count in top_gcs_raw:
        g_info = meta["groups"].get(g_str, {})
        title = g_info.get("title") or valkey_gc_titles.get(g_str) or f"Group {g_str}"
        top_groups.append({
            "gid": g_str,
            "title": title,
            "gdp": g_info.get("gdp", 0),
            "count": count
        })

    cmd_counts_dict = data.get("cmd_counts") or {}
    if isinstance(cmd_counts_dict, list):
        cmd_counts_dict = {}
    top_commands = [
     
