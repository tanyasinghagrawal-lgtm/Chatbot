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
        {"cmd": k, "count": v}
        for k, v in sorted(cmd_counts_dict.items(), key=lambda x: x[1], reverse=True)[:25]
    ]

    # Build continuous timeline series
    buckets_all = data.get("hourly_buckets") or {}
    if isinstance(buckets_all, list):
        buckets_all = {}
    buckets_gc = data.get("hourly_gc") or {}
    if isinstance(buckets_gc, list):
        buckets_gc = {}
    buckets_dm = data.get("hourly_dm") or {}
    if isinstance(buckets_dm, list):
        buckets_dm = {}

    timeline_series = []
    first_b = (start_ts // bucket_size) * bucket_size
    last_b = (end_ts // bucket_size) * bucket_size
    curr_b = first_b
    while curr_b <= last_b:
        k_str = str(curr_b)
        dt_label = datetime.fromtimestamp(curr_b, IST).strftime("%d %b %H:%M")
        timeline_series.append({
            "ts": curr_b,
            "label": dt_label,
            "total": buckets_all.get(k_str, 0),
            "gc": buckets_gc.get(k_str, 0),
            "dm": buckets_dm.get(k_str, 0)
        })
        curr_b += bucket_size

    return JSONResponse({
        "status": "ok",
        "range": {"start_ts": start_ts, "end_ts": end_ts, "bucket_size": bucket_size},
        "kpis": {
            "total_webhooks": data.get("total_webhooks", 0),
            "total_msgs": data.get("total_msgs", 0),
            "gc_webhooks": data.get("gc_webhooks", 0),
            "dm_webhooks": data.get("dm_webhooks", 0),
            "gc_msgs": data.get("gc_msgs", 0),
            "dm_msgs": data.get("dm_msgs", 0),
            "unique_users": data.get("unique_users", 0),
            "unique_gcs": data.get("unique_gcs", 0),
            "total_commands": data.get("total_commands", 0),
            "total_callbacks": data.get("total_callbacks", 0),
            "total_files_gc": data.get("total_files_gc", 0),
            "total_files_dm": data.get("total_files_dm", 0),
            "total_files": data.get("total_files_gc", 0) + data.get("total_files_dm", 0),
            "ai_replies_total": ai_total,
            "ai_replies_gc": ai_gc,
            "ai_replies_dm": ai_dm
        },
        "media_gc": data.get("media_gc", {}),
        "media_dm": data.get("media_dm", {}),
        "timeline": timeline_series,
        "top_users": top_users,
        "top_groups": top_groups,
        "top_commands": top_commands
    })


@router.get("/anylise/api/chat_history")
async def analytics_chat_history(
    request: Request,
    mode: str = Query("all"),          # 'gc', 'user', 'dm', 'all'
    target_id: str = Query(""),        # GC ID or User ID
    cursor_ts: Optional[int] = Query(None),
    direction: str = Query("before"),  # 'before' (scroll up) or 'after' (scroll down)
    limit: int = Query(40),
    chat_subfilter: str = Query("all"), # 'all', 'gc', 'dm'
    days_back: int = Query(7),
    search_q: str = Query("")
):
    """
    Fetches paginated chunks of chat messages (including Tanya AI replies and replied-to previews)
    directly from Valkey using Lua for smooth infinite up/down scrolling.
    """
    if not is_authenticated(request):
        return JSONResponse({"status": "unauthorized"}, status_code=401)

    await ensure_latest_ram_flushed()

    now_ts = int(time.time())
    if cursor_ts is None:
        cursor_ts = now_ts if direction == "before" else (now_ts - 86400)

    limit = max(10, min(100, limit))
    days_back = max(1, min(30, days_back))

    if direction == "before":
        start_ts = cursor_ts - (days_back * 86400)
        end_ts = cursor_ts
    else:
        start_ts = cursor_ts
        end_ts = min(now_ts + 60, cursor_ts + (days_back * 86400))

    valkey_client = _bot_module._resolve_valkey_client() if _bot_module else None
    if not valkey_client:
        return JSONResponse({"status": "error", "message": "Valkey offline"}, status_code=500)

    log_keys = get_date_keys_for_range(start_ts, end_ts, "tg:webhook_logs")
    ai_keys = get_date_keys_for_range(start_ts, end_ts, "tg:ai_replies")
    all_keys = log_keys + ai_keys

    try:
        raw_res = await valkey_client.eval(
            LUA_FETCH_CHAT_SLICE,
            len(all_keys),
            *all_keys,
            mode,
            str(target_id).strip(),
            str(cursor_ts),
            direction,
            str(limit),
            chat_subfilter,
            search_q.strip()
        )
        slice_data = json.loads(raw_res) if isinstance(raw_res, str) else raw_res
    except Exception as e:
        logger.error(f"Chat slice Lua error: {e}", exc_info=True)
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

    items = slice_data.get("items") or []
    if isinstance(items, dict):
        items = []

    # Collect UIDs and GIDs to attach names and titles
    uids_set = set()
    gids_set = set()
    for ev in items:
        if ev.get("uid") and str(ev["uid"]).lstrip("-").isdigit():
            uids_set.add(int(ev["uid"]))
        if ev.get("ruid") and str(ev["ruid"]).lstrip("-").isdigit():
            uids_set.add(int(ev["ruid"]))
        if ev.get("gid") and str(ev["gid"]).lstrip("-").isdigit():
            gids_set.add(int(ev["gid"]))

    meta = await enrich_names_from_postgres(list(uids_set), list(gids_set))

    # Build message lookup map for reply previews within the chunk
    msg_lookup: Dict[str, Dict[str, Any]] = {}
    for ev in items:
        if not ev.get("_is_ai") and ev.get("mid"):
            key_m = f"{ev.get('gid') or ev.get('uid')}_{ev['mid']}"
            msg_lookup[key_m] = ev

    enriched_messages = []
    for ev in items:
        t_val = int(ev.get("t", 0))
        dt_str = datetime.fromtimestamp(t_val, IST).strftime("%d %b %Y, %I:%M:%S %p")
        time_short = datetime.fromtimestamp(t_val, IST).strftime("%I:%M %p")
        uid_str = str(ev.get("uid") or "")
        gid_str = str(ev.get("gid") or "")
        u_meta = meta["users"].get(uid_str, {})
        g_meta = meta["groups"].get(gid_str, {})

        sender_name = u_meta.get("first_name") or ev.get("fname") or ev.get(" fname") or (f"User {uid_str}" if uid_str else "Unknown")
        sender_uname = u_meta.get("username") or ev.get("uname") or ""
        group_title = g_meta.get("title") or ev.get("gtitle") or (f"GC {gid_str}" if gid_str else "Private DM")

        if ev.get("_is_ai"):
            # Bot AI Reply Bubble
            enriched_messages.append({
                "is_bot_ai": True,
                "t": t_val,
                "dt_str": dt_str,
                "time_short": time_short,
                "c": ev.get("c", "dm"),
                "gid": gid_str,
                "group_title": group_title,
                "uid": uid_str,
                "target_user_name": sender_name,
                "mid": ev.get("b_mid"),
                "rmid": ev.get("u_mid"),
                "reply_preview": (ev.get("u_msg") or "")[:100],
                "sender_name": "Tanya AI 🌸",
                "sender_uname": "tanya_bot",
                "tp": "ai_reply",
                "msg": ev.get("b_rep") or "",
                "mtype": None,
                "fid": None
            })
        else:
            rmid = ev.get("rmid")
            ruid_str = str(ev.get("ruid") or "")
            ru_meta = meta["users"].get(ruid_str, {})
            reply_user_name = ru_meta.get("first_name") or (f"User {ruid_str}" if ruid_str else "Message")
            reply_preview = ""
            if rmid:
                lookup_k = f"{gid_str or uid_str}_{rmid}"
                if lookup_k in msg_lookup:
                    reply_preview = (msg_lookup[lookup_k].get("msg") or "")[:100]

            enriched_messages.append({
                "is_bot_ai": False,
                "t": t_val,
                "dt_str": dt_str,
                "time_short": time_short,
                "c": ev.get("c", "dm"),
                "gid": gid_str,
                "group_title": group_title,
                "uid": uid_str,
                "mid": ev.get("mid"),
                "rmid": rmid,
                "ruid": ruid_str,
                "reply_user_name": reply_user_name,
                "reply_preview": reply_preview,
                "rfid": ev.get("rfid"),
                "sender_name": sender_name,
                "sender_uname": sender_uname,
                "tp": ev.get("tp", "message"),
                "cmd": ev.get("cmd"),
                "msg": ev.get("msg") or "",
                "mtype": ev.get("mtype"),
                "fid": ev.get("fid")
            })

    return JSONResponse({
        "status": "ok",
        "mode": mode,
        "target_id": target_id,
        "direction": direction,
        "has_more": bool(slice_data.get("has_more", False)),
        "total_matched": slice_data.get("total_matched", len(enriched_messages)),
        "messages": enriched_messages
    })


@router.get("/anylise/api/user_profile")
async def analytics_user_profile(request: Request, uid: int = Query(...)):
    """Fetches complete PostgreSQL wallet, FD, protection, and activity profile for any User ID."""
    if not is_authenticated(request):
        return JSONResponse({"status": "unauthorized"}, status_code=401)

    pg_pool = getattr(_bot_module, "pg_pool", None) if _bot_module else None
    if not pg_pool:
        return JSONResponse({"status": "error", "message": "Database offline"}, status_code=500)

    try:
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT user_id, username, first_name, balance, COALESCE(fd_balance, 0) AS fd_balance,
                       total_balance, protected_until, last_interacted, daily_chat_count,
                       referral_count, dm_allowed
                FROM users WHERE user_id = $1;
                """,
                uid
            )
            if not row:
                return JSONResponse({"status": "not_found", "uid": uid})

            return JSONResponse({
                "status": "ok",
                "profile": {
                    "uid": str(row["user_id"]),
                    "first_name": row["first_name"] or "User",
                    "username": row["username"] or "",
                    "balance": row["balance"] or 0,
                    "fd_balance": row["fd_balance"] or 0,
                    "total_balance": row["total_balance"] or 0,
                    "daily_chat_count": row["daily_chat_count"] or 0,
                    "referral_count": row["referral_count"] or 0,
                    "dm_allowed": bool(row["dm_allowed"]),
                    "last_interacted": row["last_interacted"].astimezone(IST).strftime("%d %b %Y, %I:%M %p") if row["last_interacted"] else "N/A",
                    "protected_until": row["protected_until"].astimezone(IST).strftime("%d %b %Y, %I:%M %p") if row["protected_until"] else "None"
                }
            })
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@router.get("/anylise/media/{file_id}")
async def analytics_media_proxy(request: Request, file_id: str):
    """
    Streams Telegram Stickers, Photos, GIFs, Voice, and Videos directly to the Chat UI
    without exposing the Bot Token on the frontend.
    """
    if not is_authenticated(request):
        return Response(status_code=401)

    if not _bot_module or not getattr(_bot_module, "ptb_app", None):
        return Response(status_code=503)

    bot = _bot_module.ptb_app.bot
    http_session = getattr(_bot_module, "http_session", None)
    try:
        tg_file = await bot.get_file(file_id)
        file_path = tg_file.file_path
        if not file_path.startswith("http"):
            bot_token = getattr(_bot_module, "BOT_TOKEN", "")
            download_url = f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
        else:
            download_url = file_path

        ext = file_path.split(".")[-1].lower() if "." in file_path else ""
        mime_map = {
            "webp": "image/webp",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "png": "image/png",
            "mp4": "video/mp4",
            "webm": "video/webm",
            "ogg": "audio/ogg",
            "oga": "audio/ogg",
            "mp3": "audio/mpeg",
            "tgs": "application/x-tgsticker"
        }
        content_type = mime_map.get(ext, "application/octet-stream")

        if http_session:
            async with http_session.get(download_url, timeout=15) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    return Response(
                        content=data,
                        media_type=content_type,
                        headers={"Cache-Control": "public, max-age=86400"}
                    )
        return Response(status_code=404)
    except Exception as e:
        logger.debug(f"Media proxy fetch error for {file_id}: {e}")
        return Response(status_code=404)


@router.get("/anylise", response_class=HTMLResponse)
async def serve_analytics_dashboard():
    """Serves the complete Glassmorphism Tanya Analytics SPA."""
    return HTMLResponse(content=ANALYTICS_HTML_PAGE, status_code=200)


# =========================================================
# 🎨 GLASSMORPHISM UI (LIGHT YELLOWISH-PINK & PARROT GREEN)
# =========================================================
ANALYTICS_HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Tanya Anyalitycs — Telemetry & Live Chat Monitor</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;600&display=swap" rel="stylesheet">
<style>
:root {
    --bg-pink-yellow: #fff6f2;
    --bg-parrot-light: #f1fceb;
    --fg-pink-glass: rgba(255, 238, 232, 0.72);
    --fg-parrot-glass: rgba(226, 251, 215, 0.74);
    --card-glass: rgba(255, 255, 255, 0.68);
    --glass-border: rgba(255, 255, 255, 0.88);
    --accent-parrot: #38b000;
    --accent-parrot-soft: #dcfce7;
    --accent-pink: #ff5d8f;
    --accent-pink-soft: #ffe5ec;
    --accent-amber: #f59e0b;
    --text-dark: #1e293b;
    --text-muted: #64748b;
    --shadow-soft: 0 10px 30px -5px rgba(56, 176, 0, 0.08), 0 8px 20px -6px rgba(255, 93, 143, 0.1);
}

* {
    box-sizing: border-box;
    margin: 0;
    padding: 0;
    -webkit-tap-highlight-color: transparent;
}

body {
    font-family: 'Plus Jakarta Sans', sans-serif;
    color: var(--text-dark);
    background: linear-gradient(135deg, #fff5f0 0%, #fff9eb 35%, #eefce8 70%, #fceef2 100%);
    background-attachment: fixed;
    min-height: 100vh;
    overflow-x: hidden;
}

/* Ambient Floating Orbs */
.bg-orb-1, .bg-orb-2, .bg-orb-3 {
    position: fixed;
    border-radius: 50%;
    filter: blur(70px);
    z-index: -1;
    pointer-events: none;
}
.bg-orb-1 {
    width: 380px; height: 380px;
    background: rgba(255, 182, 193, 0.45);
    top: -80px; left: -80px;
}
.bg-orb-2 {
    width: 420px; height: 420px;
    background: rgba(163, 247, 139, 0.42);
    bottom: -100px; right: -80px;
}
.bg-orb-3 {
    width: 320px; height: 320px;
    background: rgba(255, 236, 179, 0.5);
    top: 40%; left: 45%;
}

/* Glassmorphism Utility */
.glass {
    background: var(--card-glass);
    backdrop-filter: blur(18px);
    -webkit-backdrop-filter: blur(18px);
    border: 1.5px solid var(--glass-border);
    box-shadow: var(--shadow-soft);
    border-radius: 22px;
}

.glass-pink {
    background: linear-gradient(135deg, rgba(255, 240, 235, 0.82), rgba(255, 248, 231, 0.72));
    backdrop-filter: blur(18px);
    border: 1.5px solid rgba(255, 205, 215, 0.75);
    box-shadow: var(--shadow-soft);
    border-radius: 20px;
}

.glass-parrot {
    background: linear-gradient(135deg, rgba(232, 253, 222, 0.82), rgba(244, 255, 238, 0.72));
    backdrop-filter: blur(18px);
    border: 1.5px solid rgba(180, 240, 160, 0.8);
    box-shadow: var(--shadow-soft);
    border-radius: 20px;
}

/* Login Screen */
#login-screen {
    position: fixed;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 20px;
    z-index: 9999;
    background: linear-gradient(135deg, rgba(255,245,240,0.9), rgba(235,253,228,0.9));
    backdrop-filter: blur(20px);
}
.login-box {
    width: 100%;
    max-width: 420px;
    padding: 36px 28px;
    text-align: center;
}
.login-logo {
    width: 68px; height: 68px;
    margin: 0 auto 16px;
    border-radius: 20px;
    background: linear-gradient(135deg, #ff7597, #52b788);
    display: flex; align-items: center; justify-content: center;
    font-size: 32px;
    color: white;
    box-shadow: 0 10px 25px rgba(255, 117, 151, 0.35);
}
.input-glass {
    width: 100%;
    padding: 13px 16px;
    border-radius: 14px;
    border: 1.5px solid rgba(148, 163, 184, 0.3);
    background: rgba(255, 255, 255, 0.85);
    font-size: 14px;
    font-family: inherit;
    color: var(--text-dark);
    outline: none;
    transition: all 0.2s;
}
.input-glass:focus {
    border-color: var(--accent-pink);
    box-shadow: 0 0 0 3px rgba(255, 93, 143, 0.15);
}
.btn-primary {
    padding: 12px 22px;
    border-radius: 14px;
    border: none;
    font-weight: 700;
    font-size: 14px;
    cursor: pointer;
    background: linear-gradient(135deg, #ff5d8f 0%, #ff8fa3 50%, #40916c 100%);
    color: white;
    box-shadow: 0 6px 18px rgba(255, 93, 143, 0.28);
    transition: transform 0.15s, box-shadow 0.15s;
}
.btn-primary:active {
    transform: scale(0.97);
}
.btn-chip {
    padding: 8px 14px;
    border-radius: 12px;
    border: 1px solid rgba(100, 116, 139, 0.2);
    background: rgba(255, 255, 255, 0.75);
    font-size: 12.5px;
    font-weight: 600;
    color: var(--text-dark);
    cursor: pointer;
    transition: all 0.18s;
}
.btn-chip.active, .btn-chip:hover {
    background: linear-gradient(135deg, #ffe5ec, #d8f3dc);
    border-color: #52b788;
    color: #1b4332;
}

/* Header */
header.top-nav {
    position: sticky;
    top: 10px;
    z-index: 100;
    margin: 12px auto;
    max-width: 1440px;
    padding: 14px 22px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 12px;
}
.brand-wrap {
    display: flex;
    align-items: center;
    gap: 12px;
}
.brand-badge {
    width: 42px; height: 42px;
    border-radius: 13px;
    background: linear-gradient(135deg, #ff6b8b, #52b788);
    display: flex; align-items: center; justify-content: center;
    font-size: 21px;
    color: #fff;
}
.brand-title {
    font-size: 20px;
    font-weight: 800;
    letter-spacing: -0.4px;
    background: linear-gradient(90deg, #d90429, #ff5d8f, #2d6a4f);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
}
.live-pill {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 10px;
    border-radius: 999px;
    font-size: 11px;
    font-weight: 700;
    background: rgba(56, 176, 0, 0.14);
    color: #1b4332;
}
.live-dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    background: #38b000;
    animation: pulse 1.6s infinite;
}
@keyframes pulse {
    0% { box-shadow: 0 0 0 0 rgba(56, 176, 0, 0.6); }
    70% { box-shadow: 0 0 0 8px rgba(56, 176, 0, 0); }
    100% { box-shadow: 0 0 0 0 rgba(56, 176, 0, 0); }
}

/* Main Container */
.main-wrap {
    max-width: 1440px;
    margin: 0 auto;
    padding: 6px 14px 40px;
}

/* Navigation Tabs */
.nav-tabs {
    display: flex;
    gap: 8px;
    overflow-x: auto;
    padding-bottom: 4px;
}
.tab-btn {
    padding: 10px 18px;
    border-radius: 14px;
    border: 1.5px solid transparent;
    background: rgba(255, 255, 255, 0.55);
    font-weight: 700;
    font-size: 13.5px;
    cursor: pointer;
    white-space: nowrap;
    color: var(--text-muted);
    transition: all 0.2s;
}
.tab-btn.active {
    background: linear-gradient(135deg, #fff0f3, #e9f5db);
    border-color: #74c69d;
    color: #1b4332;
    box-shadow: 0 4px 12px rgba(82, 183, 136, 0.15);
}

/* Filter Bar */
.filter-bar {
    padding: 16px 20px;
    margin-bottom: 18px;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
}
.filter-group {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
}

/* KPI Grid */
.kpi-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 14px;
    margin-bottom: 20px;
}
.kpi-card {
    padding: 18px 20px;
    position: relative;
    overflow: hidden;
}
.kpi-label {
    font-size: 12.5px;
    font-weight: 700;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.4px;
}
.kpi-value {
    font-size: 28px;
    font-weight: 800;
    margin: 6px 0 4px;
    font-family: 'JetBrains Mono', monospace;
}
.kpi-sub {
    font-size: 12px;
    font-weight: 600;
    color: #2d6a4f;
}

/* Charts & Media Section */
.charts-grid {
    display: grid;
    grid-template-columns: 2fr 1fr;
    gap: 16px;
    margin-bottom: 20px;
}
@media (max-width: 980px) {
    .charts-grid { grid-template-columns: 1fr; }
}
.chart-card {
    padding: 20px;
}
.section-title {
    font-size: 15.5px;
    font-weight: 800;
    margin-bottom: 14px;
    display: flex;
    align-items: center;
    justify-content: space-between;
}

/* Media Pill Grid */
.media-grid {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 10px;
}
.media-item {
    padding: 11px 14px;
    border-radius: 14px;
    background: rgba(255, 255, 255, 0.72);
    border: 1px solid rgba(255, 255, 255, 0.9);
    display: flex;
    align-items: center;
    justify-content: space-between;
    cursor: pointer;
    transition: transform 0.15s;
}
.media-item:hover {
    transform: translateY(-2px);
}

/* Leaderboards Grid */
.tables-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
    gap: 16px;
}
.list-scroll {
    max-height: 390px;
    overflow-y: auto;
    padding-right: 4px;
}
.row-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 11px 14px;
    margin-bottom: 8px;
    border-radius: 14px;
    background: rgba(255, 255, 255, 0.75);
    border: 1px solid rgba(255, 255, 255, 0.95);
    cursor: pointer;
    transition: all 0.16s;
}
.row-item:hover {
    background: linear-gradient(135deg, #fff0f3, #e9f5db);
    transform: translateX(3px);
}
.badge-count {
    font-family: 'JetBrains Mono', monospace;
    font-size: 12.5px;
    font-weight: 700;
    padding: 4px 10px;
    border-radius: 10px;
    background: rgba(56, 176, 0, 0.15);
    color: #1b4332;
}

/* =========================================================
   💬 TELEGRAM-STYLE CHAT VIEWER & INFINITE SCROLL
   ========================================================= */
.chat-layout {
    display: grid;
    grid-template-columns: 330px 1fr;
    gap: 16px;
    height: calc(100vh - 125px);
    min-height: 580px;
}
@media (max-width: 900px) {
    .chat-layout {
        grid-template-columns: 1fr;
        height: auto;
    }
}
.chat-sidebar {
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 12px;
    overflow-y: auto;
}
.chat-main {
    display: flex;
    flex-direction: column;
    height: calc(100vh - 125px);
    min-height: 560px;
    overflow: hidden;
    position: relative;
}
@media (max-width: 900px) {
    .chat-main {
        height: 78vh;
    }
}
.tg-chat-header {
    padding: 14px 18px;
    background: linear-gradient(135deg, rgba(255, 235, 238, 0.9), rgba(226, 251, 215, 0.9));
    border-bottom: 1px solid rgba(255, 255, 255, 0.9);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
}
.tg-avatar {
    width: 42px; height: 42px;
    border-radius: 50%;
    background: linear-gradient(135deg, #ff758f, #40916c);
    color: white;
    font-weight: 800;
    font-size: 16px;
    display: flex; align-items: center; justify-content: center;
    flex-shrink: 0;
}
.tg-messages-viewport {
    flex: 1;
    overflow-y: auto;
    padding: 18px 16px;
    background: radial-gradient(circle at center, rgba(255, 248, 242, 0.65), rgba(235, 252, 228, 0.65));
    display: flex;
    flex-direction: column;
    gap: 10px;
    scroll-behavior: auto;
}

/* Chat Bubbles */
.msg-row {
    display: flex;
    gap: 9px;
    max-width: 82%;
    animation: fadeIn 0.15s ease-out;
}
@keyframes fadeIn {
    from { opacity: 0; transform: translateY(4px); }
    to { opacity: 1; transform: translateY(0); }
}
.msg-row.msg-right {
    align-self: flex-end;
    flex-direction: row-reverse;
}
.msg-row.msg-left {
    align-self: flex-start;
}
.msg-bubble {
    padding: 10px 14px;
    border-radius: 18px;
    position: relative;
    box-shadow: 0 3px 10px rgba(0, 0, 0, 0.04);
    word-break: break-word;
    font-size: 14px;
    line-height: 1.45;
}
.msg-left .msg-bubble {
    background: rgba(255, 255, 255, 0.92);
    border: 1px solid rgba(255, 204, 213, 0.7);
    border-top-left-radius: 4px;
}
.msg-right .msg-bubble {
    background: linear-gradient(135deg, #d8f3dc 0%, #e9f5db 100%);
    border: 1px solid rgba(116, 198, 157, 0.65);
    border-top-right-radius: 4px;
}
.msg-bot-ai .msg-bubble {
    background: linear-gradient(135deg, #ffe5ec 0%, #fff0f3 100%);
    border: 1.5px solid #ff8fa3;
}
.msg-sender {
    font-size: 12.5px;
    font-weight: 800;
    color: #d90429;
    margin-bottom: 3px;
    display: flex;
    align-items: center;
    gap: 6px;
    cursor: pointer;
}
.msg-reply-quote {
    border-left: 3px solid #ff5d8f;
    background: rgba(255, 93, 143, 0.08);
    padding: 4px 8px;
    border-radius: 6px;
    font-size: 12px;
    margin-bottom: 6px;
    color: #475569;
}
.msg-meta {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 6px;
    font-size: 10.5px;
    color: #64748b;
    margin-top: 4px;
}
.msg-media-box {
    margin: 6px 0;
    border-radius: 12px;
    overflow: hidden;
    max-width: 240px;
}
.msg-media-box img, .msg-media-box video {
    max-width: 100%;
    max-height: 220px;
    border-radius: 10px;
    display: block;
}
.date-divider {
    align-self: center;
    padding: 4px 12px;
    border-radius: 999px;
    font-size: 11.5px;
    font-weight: 700;
    background: rgba(255, 255, 255, 0.78);
    border: 1px solid rgba(0,0,0,0.06);
    color: #475569;
    margin: 6px 0;
}
.scroll-loader {
    text-align: center;
    padding: 8px;
    font-size: 12px;
    font-weight: 700;
    color: #2d6a4f;
}
.hidden { display: none !important; }
</style>
</head>
<body>

<div class="bg-orb-1"></div>
<div class="bg-orb-2"></div>
<div class="bg-orb-3"></div>

<!-- 🔐 LOGIN OVERLAY -->
<div id="login-screen">
    <div class="login-box glass">
        <div class="login-logo">🌸</div>
        <h2 style="font-size: 23px; font-weight: 800; margin-bottom: 6px;">Tanya Anyalitycs</h2>
        <p style="font-size: 13px; color: var(--text-muted); margin-bottom: 22px;">
            Ultra-Fast Valkey Telemetry & Live GC/DM Chat Inspector
        </p>
        <form onsubmit="handleLogin(event)">
            <input type="password" id="admin-pass" class="input-glass" placeholder="Enter Admin Password..." required style="margin-bottom: 14px; text-align: center; font-weight: 600;">
            <button type="submit" class="btn-primary" style="width: 100%; padding: 14px;">🔓 Unlock Tanya Anyalitycs</button>
        </form>
        <p id="login-err" style="color: #e11d48; font-size: 12.5px; font-weight: 700; margin-top: 12px;"></p>
    </div>
</div>

<!-- 🌸 TOP HEADER -->
<header class="top-nav glass">
    <div class="brand-wrap">
        <div class="brand-badge">🌸</div>
        <div>
            <div class="brand-title">Tanya Anyalitycs</div>
            <div style="font-size: 11.5px; color: var(--text-muted); font-weight: 600;">
                Valkey Edge Engine • Zero Bot CPU Overhead
            </div>
        </div>
        <span class="live-pill"><span class="live-dot"></span> LIVE</span>
    </div>

    <div class="nav-tabs">
        <button class="tab-btn active" id="tab-btn-dash" onclick="switchTab('dash')">📊 Analytics Overview</button>
        <button class="tab-btn" id="tab-btn-chat" onclick="switchTab('chat')">💬 Live TG Chat & User Timeline</button>
    </div>

    <div style="display: flex; align-items: center; gap: 8px;">
        <button class="btn-chip" onclick="refreshCurrentView()">🔄 Sync Now</button>
        <button class="btn-chip" onclick="logoutAdmin()" style="color: #e11d48;">🚪 Logout</button>
    </div>
</header>

<div class="main-wrap">

    <!-- =========================================================
         TAB 1: ANALYTICS OVERVIEW DASHBOARD
         ========================================================= -->
    <div id="view-dash">
        <!-- Time & Category Filter Bar -->
        <div class="filter-bar glass-pink">
            <div class="filter-group">
                <span style="font-size: 12.5px; font-weight: 800; color: #9d174d;">⏱️ Time Range:</span>
                <button class="btn-chip" onclick="setQuickRange(1, this)">Last 1h</button>
                <button class="btn-chip" onclick="setQuickRange(6, this)">Last 6h</button>
                <button class="btn-chip active" id="default-range-btn" onclick="setQuickRange(24, this)">Last 24h</button>
                <button class="btn-chip" onclick="setQuickRange(168, this)">Last 7d</button>
                <button class="btn-chip" onclick="setQuickRange(720, this)">Last 30d</button>
            </div>

            <div class="filter-group">
                <input type="datetime-local" id="custom-start" class="input-glass" style="width: auto; padding: 7px 10px; font-size: 12px;">
                <span style="font-weight: 700;">to</span>
                <input type="datetime-local" id="custom-end" class="input-glass" style="width: auto; padding: 7px 10px; font-size: 12px;">
                <button class="btn-chip" onclick="applyCustomDateRange()" style="background: #d8f3dc; border-color: #52b788;">Apply Custom</button>
            </div>

            <div class="filter-group">
                <select id="dash-chat-filter" class="input-glass" style="width: auto; padding: 7px 12px; font-size: 12.5px; font-weight: 700;" onchange="loadDashboardData()">
                    <option value="all">🌐 All Chats (GC + DM)</option>
                    <option value="gc">👥 Group Chats Only (GC)</option>
                    <option value="dm">💌 Private DMs Only</option>
                </select>
                <select id="dash-type-filter" class="input-glass" style="width: auto; padding: 7px 12px; font-size: 12.5px; font-weight: 700;" onchange="loadDashboardData()">
                    <option value="all">⚡ All Event Types</option>
                    <option value="message">💬 Text Messages</option>
                    <option value="media">🖼️ Media / Files Only</option>
                    <option value="command">🤖 Bot Commands</option>
                    <option value="callback">🔘 Button Callbacks</option>
                </select>
            </div>
        </div>

        <!-- KPI Cards -->
        <div class="kpi-grid">
            <div class="kpi-card glass-pink">
                <div class="kpi-label">⚡ Total Webhooks</div>
                <div class="kpi-value" id="kpi-webhooks">0</div>
                <div class="kpi-sub" id="kpi-webhooks-sub">GC: 0 | DM: 0</div>
            </div>
            <div class="kpi-card glass-parrot">
                <div class="kpi-label">💬 Total Messages</div>
                <div class="kpi-value" id="kpi-msgs">0</div>
                <div class="kpi-sub" id="kpi-msgs-sub">GC: 0 | DM: 0</div>
            </div>
            <div class="kpi-card glass-pink">
                <div class="kpi-label">👥 Active Users</div>
                <div class="kpi-value" id="kpi-users">0</div>
                <div class="kpi-sub" id="kpi-gcs-sub">Active Groups: 0</div>
            </div>
            <div class="kpi-card glass-parrot">
                <div class="kpi-label">🖼️ Total Media & Files</div>
                <div class="kpi-value" id="kpi-files">0</div>
                <div class="kpi-sub" id="kpi-files-sub">GC Files: 0 | DM Files: 0</div>
            </div>
            <div class="kpi-card glass-pink">
                <div class="kpi-label">🌸 Tanya AI Replies</div>
                <div class="kpi-value" id="kpi-ai">0</div>
                <div class="kpi-sub" id="kpi-ai-sub">GC AI: 0 | DM AI: 0</div>
            </div>
            <div class="kpi-card glass-parrot">
                <div class="kpi-label">🤖 Commands & Buttons</div>
                <div class="kpi-value" id="kpi-cmds">0</div>
                <div class="kpi-sub" id="kpi-cmds-sub">Cmds: 0 | Callbacks: 0</div>
            </div>
        </div>

        <!-- Charts & Media Breakdown -->
        <div class="charts-grid">
            <div class="chart-card glass">
                <div class="section-title">
                    <span>📈 Webhook Traffic Timeline (GC vs DM)</span>
                    <span id="chart-bucket-label" style="font-size: 12px; color: var(--text-muted); font-weight: 600;">Hourly Trend</span>
                </div>
                <div style="height: 290px;">
                    <canvas id="trafficChart"></canvas>
                </div>
            </div>

            <div class="chart-card glass-parrot">
                <div class="section-title">
                    <span>🗂️ Media & Files Breakdown</span>
                    <span style="font-size: 11.5px; color: #1b4332;">GC vs DM</span>
                </div>
                <div class="media-grid" id="media-breakdown-grid"></div>
            </div>
        </div>

        <!-- Leaderboards: Top Active Users, Top Groups, Top Commands -->
        <div class="tables-grid">
            <div class="chart-card glass-pink">
                <div class="section-title">
                    <span>👑 Top Active Users (Click to Inspect Timeline)</span>
                </div>
                <div class="list-scroll" id="top-users-list"></div>
            </div>

            <div class="chart-card glass-parrot">
                <div class="section-title">
                    <span>🏛️ Most Active Group Chats (Click to Open GC Chat)</span>
                </div>
                <div class="list-scroll" id="top-groups-list"></div>
            </div>

            <div class="chart-card glass">
                <div class="section-title">
                    <span>🚀 Top Bot Commands Used</span>
                </div>
                <div class="list-scroll" id="top-cmds-list"></div>
            </div>
        </div>
    </div>

    <!-- =========================================================
         TAB 2: TELEGRAM-STYLE INFINITE CHAT & USER TIMELINE
         ========================================================= -->
    <div id="view-chat" class="hidden">
        <div class="chat-layout">
            <!-- Left Sidebar: Inspector Controls & Profile Card -->
            <div class="chat-sidebar glass-pink">
                <div style="font-size: 15px; font-weight: 800; color: #9d174d;">🔍 Activity & Chat Inspector</div>

                <div>
                    <label style="font-size: 11.5px; font-weight: 700; color: var(--text-muted);">SELECT VIEW MODE</label>
                    <select id="chat-mode-select" class="input-glass" style="margin-top: 4px; font-weight: 700;" onchange="onChatModeChange()">
                        <option value="all">🌐 Global Live Stream (All GC + DM)</option>
                        <option value="user">👤 User Activity Timeline (By UID)</option>
                        <option value="gc">👥 Group Chat Inspector (By GC ID)</option>
                        <option value="dm">💌 Private DM Chat (By User ID)</option>
                    </select>
                </div>

                <div id="target-id-box">
                    <label style="font-size: 11.5px; font-weight: 700; color: var(--text-muted);" id="target-input-label">USER UID OR GROUP CHAT ID</label>
                    <div style="display: flex; gap: 6px; margin-top: 4px;">
                        <input type="text" id="chat-target-input" class="input-glass" placeholder="e.g. 68475932 or -100192837">
                        <button class="btn-primary" onclick="startFreshChatLoad()" style="padding: 0 16px;">Go</button>
                    </div>
                </div>

                <div>
                    <label style="font-size: 11.5px; font-weight: 700; color: var(--text-muted);">FILTER CHAT TYPE & KEYWORD</label>
                    <div style="display: flex; gap: 6px; margin-top: 4px;">
                        <select id="chat-subfilter" class="input-glass" style="width: 48%; padding: 9px;" onchange="startFreshChatLoad()">
                            <option value="all">All (GC+DM)</option>
                            <option value="gc">GC Only</option>
                            <option value="dm">DM Only</option>
                        </select>
                        <input type="text" id="chat-search-input" class="input-glass" style="width: 52%; padding: 9px;" placeholder="Search text..." onkeydown="if(event.key==='Enter') startFreshChatLoad()">
                    </div>
                </div>

                <!-- Dynamic User Wallet Profile Card (Visible when inspecting a User) -->
                <div id="user-profile-card" class="glass-parrot hidden" style="padding: 14px; font-size: 12.5px;">
                    <div style="font-weight: 800; font-size: 14px; margin-bottom: 6px;" id="up-name">User Profile</div>
                    <div id="up-details" style="display: grid; gap: 4px; color: #1b4332;"></div>
                </div>

                <!-- Quick Jump List in Sidebar -->
                <div style="font-size: 12px; font-weight: 800; color: var(--text-muted); margin-top: 4px;">⚡ QUICK JUMP (ACTIVE GCs & USERS)</div>
                <div id="sidebar-quick-list" style="flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 6px;"></div>
            </div>

            <!-- Right Telegram Chat Viewport -->
            <div class="chat-main glass">
                <div class="tg-chat-header">
                    <div style="display: flex; align-items: center; gap: 11px;">
                        <div class="tg-avatar" id="chat-header-avatar">🌐</div>
                        <div>
                            <div style="font-weight: 800; font-size: 15px;" id="chat-header-title">Global Live Webhook Stream</div>
                            <div style="font-size: 12px; color: var(--text-muted);" id="chat-header-sub">Scroll up for older chunks • Scroll down for newer chunks</div>
                        </div>
                    </div>
                    <div style="display: flex; gap: 6px;">
                        <button class="btn-chip" onclick="jumpToOldestLoaded()" title="Scroll to Top">⬆️ Top</button>
                        <button class="btn-chip" onclick="jumpToBottomLatest()" title="Jump to Latest">⬇️ Latest</button>
                    </div>
                </div>

                <div id="up-loader" class="scroll-loader hidden">⏳ Loading older messages from Valkey...</div>

                <!-- Infinite Scroll Container -->
                <div class="tg-messages-viewport" id="tg-viewport" onscroll="handleViewportScroll(this)"></div>

                <div id="down-loader" class="scroll-loader hidden">⏳ Loading newer messages...</div>
            </div>
        </div>
    </div>

</div>

<script>
let currentStartTs = Math.floor(Date.now() / 1000) - 86400;
let currentEndTs = Math.floor(Date.now() / 1000);
let trafficChartInstance = null;

// Infinite Chat State
let chatMessages = [];
let earliestTs = null;
let latestTs = null;
let hasMoreOlder = true;
let hasMoreNewer = false;
let isLoadingChat = false;
let cachedTopUsers = [];
let cachedTopGroups = [];

async function handleLogin(e) {
    e.preventDefault();
    const pass = document.getElementById('admin-pass').value;
    const errEl = document.getElementById('login-err');
    errEl.textContent = '';
    try {
        const res = await fetch('/anylise/api/login', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({password: pass})
        });
        const data = await res.json();
        if (res.ok && data.status === 'ok') {
            localStorage.setItem('tanya_admin_token', data.token);
            document.getElementById('login-screen').classList.add('hidden');
            initDashboard();
        } else {
            errEl.textContent = data.message || 'Incorrect password!';
        }
    } catch (err) {
        errEl.textContent = 'Network error while logging in.';
    }
}

async function logoutAdmin() {
    await fetch('/anylise/api/logout', {method: 'POST'});
    localStorage.removeItem('tanya_admin_token');
    document.getElementById('login-screen').classList.remove('hidden');
}

function getAuthHeaders() {
    const t = localStorage.getItem('tanya_admin_token');
    return t ? {'X-Admin-Token': t} : {};
}

function switchTab(tab) {
    document.getElementById('view-dash').classList.toggle('hidden', tab !== 'dash');
    document.getElementById('view-chat').classList.toggle('hidden', tab !== 'chat');
    document.getElementById('tab-btn-dash').classList.toggle('active', tab === 'dash');
    document.getElementById('tab-btn-chat').classList.toggle('active', tab === 'chat');
    if (tab === 'chat' && chatMessages.length === 0) {
        startFreshChatLoad();
    }
}

function setQuickRange(hours, btnEl) {
    document.querySelectorAll('.filter-group .btn-chip').forEach(b => b.classList.remove('active'));
    if (btnEl) btnEl.classList.add('active');
    currentEndTs = Math.floor(Date.now() / 1000);
    currentStartTs = currentEndTs - (hours * 3600);
    loadDashboardData();
}

function applyCustomDateRange() {
    const sVal = document.getElementById('custom-start').value;
    const eVal = document.getElementById('custom-end').value;
    if (!sVal || !eVal) {
        alert('Please select both Start and End date-time!');
        return;
    }
    currentStartTs = Math.floor(new Date(sVal).getTime() / 1000);
    currentEndTs = Math.floor(new Date(eVal).getTime() / 1000);
    if (currentEndTs <= currentStartTs) {
        alert('End time must be after Start time!');
        return;
    }
    loadDashboardData();
}

function refreshCurrentView() {
    if (!document.getElementById('view-dash').classList.contains('hidden')) {
        currentEndTs = Math.floor(Date.now() / 1000);
        loadDashboardData();
    } else {
        startFreshChatLoad();
    }
}

async function loadDashboardData() {
    const chatFilter = document.getElementById('dash-chat-filter').value;
    const typeFilter = document.getElementById('dash-type-filter').value;

    const url = `/anylise/api/overview?start_ts=${currentStartTs}&end_ts=${currentEndTs}&chat_filter=${chatFilter}&type_filter=${typeFilter}`;
    try {
        const res = await fetch(url, {headers: getAuthHeaders()});
        if (res.status === 401) {
            document.getElementById('login-screen').classList.remove('hidden');
            return;
        }
        const data = await res.json();
        if (data.status !== 'ok') return;

        document.getElementById('login-screen').classList.add('hidden');

        const k = data.kpis;
        document.getElementById('kpi-webhooks').textContent = k.total_webhooks.toLocaleString();
        document.getElementById('kpi-webhooks-sub').textContent = `GC: ${k.gc_webhooks.toLocaleString()} | DM: ${k.dm_webhooks.toLocaleString()}`;

        document.getElementById('kpi-msgs').textContent = k.total_msgs.toLocaleString();
        document.getElementById('kpi-msgs-sub').textContent = `GC Msgs: ${k.gc_msgs.toLocaleString()} | DM Msgs: ${k.dm_msgs.toLocaleString()}`;

        document.getElementById('kpi-users').textContent = k.unique_users.toLocaleString();
        document.getElementById('kpi-gcs-sub').textContent = `Active GCs: ${k.unique_gcs.toLocaleString()}`;

        document.getElementById('kpi-files').textContent = k.total_files.toLocaleString();
        document.getElementById('kpi-files-sub').textContent = `GC Files: ${k.total_files_gc.toLocaleString()} | DM Files: ${k.total_files_dm.toLocaleString()}`;

        document.getElementById('kpi-ai').textContent = k.ai_replies_total.toLocaleString();
        document.getElementById('kpi-ai-sub').textContent = `GC AI: ${k.ai_replies_gc.toLocaleString()} | DM AI: ${k.ai_replies_dm.toLocaleString()}`;

        document.getElementById('kpi-cmds').textContent = (k.total_commands + k.total_callbacks).toLocaleString();
        document.getElementById('kpi-cmds-sub').textContent = `Cmds: ${k.total_commands.toLocaleString()} | Buttons: ${k.total_callbacks.toLocaleString()}`;

        renderTrafficChart(data.timeline || []);
        renderMediaBreakdown(data.media_gc || {}, data.media_dm || {});

        cachedTopUsers = data.top_users || [];
        cachedTopGroups = data.top_groups || [];
        renderLeaderboards(cachedTopUsers, cachedTopGroups, data.top_commands || []);
        renderSidebarQuickJump();
    } catch (e) {
        console.error('Overview load error:', e);
    }
}

function renderTrafficChart(timeline) {
    const ctx = document.getElementById('trafficChart').getContext('2d');
    const labels = timeline.map(i => i.label);
    const gcData = timeline.map(i => i.gc);
    const dmData = timeline.map(i => i.dm);

    if (trafficChartInstance) {
        trafficChartInstance.destroy();
    }

    trafficChartInstance = new Chart(ctx, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [
                {
                    label: 'Group Chats (GC)',
                    data: gcData,
                    borderColor: '#38b000',
                    backgroundColor: 'rgba(56, 176, 0, 0.16)',
                    borderWidth: 2.5,
                    tension: 0.35,
                    fill: true,
                    pointRadius: 2
                },
                {
                    label: 'Private DMs',
                    data: dmData,
                    borderColor: '#ff5d8f',
                    backgroundColor: 'rgba(255, 93, 143, 0.16)',
                    borderWidth: 2.5,
                    tension: 0.35,
                    fill: true,
                    pointRadius: 2
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            plugins: {
                legend: { position: 'top', labels: { font: { family: 'Plus Jakarta Sans', weight: '700' } } }
            },
            scales: {
                y: { beginAtZero: true, grid: { color: 'rgba(0,0,0,0.04)' } },
                x: { grid: { display: false }, ticks: { maxTicksLimit: 10 } }
            }
        }
    });
}

function renderMediaBreakdown(mGc, mDm) {
    const types = [
        {key: 'sticker', icon: '🎭', name: 'Stickers'},
        {key: 'photo', icon: '🖼️', name: 'Photos'},
        {key: 'gif', icon: '🎞️', name: 'GIFs'},
        {key: 'video', icon: '🎬', name: 'Videos'},
        {key: 'voice', icon: '🎙️', name: 'Voice Notes'},
        {key: 'audio', icon: '🎵', name: 'Audio Files'},
        {key: 'video_note', icon: '⭕', name: 'Video Notes'},
        {key: 'document', icon: '📄', name: 'Documents'}
    ];
    const grid = document.getElementById('media-breakdown-grid');
    grid.innerHTML = types.map(t => {
        const gcVal = mGc[t.key] || 0;
        const dmVal = mDm[t.key] || 0;
        const tot = gcVal + dmVal;
        return `
        <div class="media-item">
            <div>
                <div style="font-size: 13px; font-weight: 800;">${t.icon} ${t.name}</div>
                <div style="font-size: 11px; color: var(--text-muted); font-weight: 600;">GC: ${gcVal} • DM: ${dmVal}</div>
            </div>
            <span class="badge-count">${tot.toLocaleString()}</span>
        </div>`;
    }).join('');
}

function escapeHtml(s) {
    return String(s || '').replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
}

function renderLeaderboards(users, groups, cmds) {
    const uEl = document.getElementById('top-users-list');
    uEl.innerHTML = users.length ? users.map((u, idx) => `
        <div class="row-item" onclick="inspectUserTimeline('${escapeHtml(u.uid)}', '${escapeHtml(u.first_name)}')">
            <div>
                <div style="font-weight: 800; font-size: 13.5px;">#${idx+1} ${escapeHtml(u.first_name)} ${u.username ? `<span style="color:#64748b;font-weight:500;">(@${escapeHtml(u.username)})</span>` : ''}</div>
                <div style="font-size: 11.5px; color: var(--text-muted);">UID: <code>${escapeHtml(u.uid)}</code> • Wallet: ${Number(u.balance||0).toLocaleString()} 🪙</div>
            </div>
            <span class="badge-count">${u.count.toLocaleString()} msgs</span>
        </div>
    `).join('') : '<div style="padding:14px;color:#64748b;">No user activity in this filter window.</div>';

    const gEl = document.getElementById('top-groups-list');
    gEl.innerHTML = groups.length ? groups.map((g, idx) => `
        <div class="row-item" onclick="inspectGroupChat('${escapeHtml(g.gid)}', '${escapeHtml(g.title)}')">
            <div>
                <div style="font-weight: 800; font-size: 13.5px;">#${idx+1} ${escapeHtml(g.title)}</div>
                <div style="font-size: 11.5px; color: var(--text-muted);">GC ID: <code>${escapeHtml(g.gid)}</code> • GDP: ${Number(g.gdp||0).toLocaleString()} 🪙</div>
            </div>
            <span class="badge-count">${g.count.toLocaleString()} msgs</span>
        </div>
    `).join('') : '<div style="padding:14px;color:#64748b;">No group activity in this filter window.</div>';

    const cEl = document.getElementById('top-cmds-list');
    cEl.innerHTML = cmds.length ? cmds.map((c, idx) => `
        <div class="row-item" onclick="searchCommandInChat('${escapeHtml(c.cmd)}')">
            <div style="font-weight: 800; font-size: 13.5px; font-family: 'JetBrains Mono', monospace;">#${idx+1} ${escapeHtml(c.cmd)}</div>
            <span class="badge-count">${c.count.toLocaleString()} uses</span>
        </div>
    `).join('') : '<div style="padding:14px;color:#64748b;">No commands executed in this window.</div>';
}

function renderSidebarQuickJump() {
    const qEl = document.getElementById('sidebar-quick-list');
    const items = [];
    cachedTopGroups.slice(0, 6).forEach(g => {
        items.push(`<div class="row-item" style="padding:8px 10px;margin-bottom:4px;" onclick="inspectGroupChat('${escapeHtml(g.gid)}', '${escapeHtml(g.title)}')">
            <div style="font-size:12px;font-weight:700;">👥 ${escapeHtml(g.title)}</div>
            <span class="badge-count" style="font-size:11px;">${g.count}</span>
        </div>`);
    });
    cachedTopUsers.slice(0, 8).forEach(u => {
        items.push(`<div class="row-item" style="padding:8px 10px;margin-bottom:4px;" onclick="inspectUserTimeline('${escapeHtml(u.uid)}', '${escapeHtml(u.first_name)}')">
            <div style="font-size:12px;font-weight:700;">👤 ${escapeHtml(u.first_name)}</div>
            <span class="badge-count" style="font-size:11px;">${u.count}</span>
        </div>`);
    });
    qEl.innerHTML = items.join('');
}

// =========================================================
// 💬 TELEGRAM INFINITE SCROLL CHAT LOGIC
// =========================================================
function inspectUserTimeline(uid, name) {
    switchTab('chat');
    document.getElementById('chat-mode-select').value = 'user';
    document.getElementById('chat-target-input').value = uid;
    document.getElementById('chat-header-avatar').textContent = (name || 'U')[0].toUpperCase();
    document.getElementById('chat-header-title').textContent = `${name} — Full Activity Timeline`;
    document.getElementById('chat-header-sub').textContent = `UID: ${uid} • Showing both GC & DM messages`;
    loadUserProfileCard(uid);
    startFreshChatLoad();
}

function inspectGroupChat(gid, title) {
    switchTab('chat');
    document.getElementById('chat-mode-select').value = 'gc';
    document.getElementById('chat-target-input').value = gid;
    document.getElementById('chat-header-avatar').textContent = '👥';
    document.getElementById('chat-header-title').textContent = title || `Group ${gid}`;
    document.getElementById('chat-header-sub').textContent = `GC ID: ${gid} • Live Telegram Group Chat View`;
    document.getElementById('user-profile-card').classList.add('hidden');
    startFreshChatLoad();
}

function searchCommandInChat(cmd) {
    switchTab('chat');
    document.getElementById('chat-mode-select').value = 'all';
    document.getElementById('chat-search-input').value = cmd;
    startFreshChatLoad();
}

function onChatModeChange() {
    const mode = document.getElementById('chat-mode-select').value;
    const pCard = document.getElementById('user-profile-card');
    if (mode !== 'user' && mode !== 'dm') {
        pCard.classList.add('hidden');
    }
    if (mode === 'all') {
        document.getElementById('chat-target-input').value = '';
        document.getElementById('chat-header-avatar').textContent = '🌐';
        document.getElementById('chat-header-title').textContent = 'Global Live Stream (All GC + DM)';
        document.getElementById('chat-header-sub').textContent = 'Real-time combined feed';
        startFreshChatLoad();
    }
}

async function loadUserProfileCard(uid) {
    const pCard = document.getElementById('user-profile-card');
    try {
        const res = await fetch(`/anylise/api/user_profile?uid=${encodeURIComponent(uid)}`, {headers: getAuthHeaders()});
        const data = await res.json();
        if (data.status === 'ok' && data.profile) {
            const p = data.profile;
            pCard.classList.remove('hidden');
            document.getElementById('up-name').textContent = `👤 ${p.first_name} ${p.username ? '(@'+p.username+')' : ''}`;
            document.getElementById('up-details').innerHTML = `
                <div>💰 <b>Total Net Worth:</b> <code>${p.total_balance.toLocaleString()} Coins</code></div>
                <div>💵 <b>Main:</b> ${p.balance.toLocaleString()} | 🔒 <b>FD:</b> ${p.fd_balance.toLocaleString()}</div>
                <div>💬 <b>Daily Chats Used:</b> ${p.daily_chat_count}/100</div>
                <div>🛡️ <b>Shield:</b> ${p.protected_until}</div>
                <div>🕒 <b>Last Seen:</b> ${p.last_interacted}</div>
            `;
        } else {
            pCard.classList.add('hidden');
        }
    } catch (e) {
        pCard.classList.add('hidden');
    }
}

async function startFreshChatLoad() {
    chatMessages = [];
    earliestTs = null;
    latestTs = null;
    hasMoreOlder = true;
    hasMoreNewer = false;

    const vp = document.getElementById('tg-viewport');
    vp.innerHTML = '<div class="scroll-loader">⏳ Loading chat timeline from Valkey...</div>';

    const mode = document.getElementById('chat-mode-select').value;
    const tid = document.getElementById('chat-target-input').value.trim();
    if ((mode === 'user' || mode === 'dm') && tid) {
        loadUserProfileCard(tid);
    }

    await fetchChatChunk('before', Math.floor(Date.now() / 1000), true);
}

async function fetchChatChunk(direction, cursorTs, isInitial = false) {
    if (isLoadingChat) return;
    isLoadingChat = true;

    const mode = document.getElementById('chat-mode-select').value;
    const targetId = document.getElementById('chat-target-input').value.trim();
    const subfilter = document.getElementById('chat-subfilter').value;
    const searchQ = document.getElementById('chat-search-input').value.trim();

    if (direction === 'before') {
        document.getElementById('up-loader').classList.remove('hidden');
    } else {
        document.getElementById('down-loader').classList.remove('hidden');
    }

    const url = `/anylise/api/chat_history?mode=${encodeURIComponent(mode)}&target_id=${encodeURIComponent(targetId)}&cursor_ts=${cursorTs}&direction=${direction}&limit=40&chat_subfilter=${subfilter}&days_back=14&search_q=${encodeURIComponent(searchQ)}`;

    try {
        const res = await fetch(url, {headers: getAuthHeaders()});
        const data = await res.json();
        const vp = document.getElementById('tg-viewport');

        if (data.status === 'ok') {
            const incoming = data.messages || [];
            if (isInitial) {
                chatMessages = incoming;
                hasMoreOlder = data.has_more;
                hasMoreNewer = false;
                renderChatViewport();
                vp.scrollTop = vp.scrollHeight;
            } else if (direction === 'before') {
                const prevScrollHeight = vp.scrollHeight;
                const prevScrollTop = vp.scrollTop;
                // Deduplicate and prepend older chunk
                const existingKeys = new Set(chatMessages.map(m => `${m.t}_${m.mid}_${m.is_bot_ai}`));
                const filtered = incoming.filter(m => !existingKeys.has(`${m.t}_${m.mid}_${m.is_bot_ai}`));
                chatMessages = [...filtered, ...chatMessages];
                hasMoreOlder = data.has_more && filtered.length > 0;
                renderChatViewport();
                // Maintain exact visual scroll position when loading upward
                vp.scrollTop = vp.scrollHeight - prevScrollHeight + prevScrollTop;
            } else {
                const existingKeys = new Set(chatMessages.map(m => `${m.t}_${m.mid}_${m.is_bot_ai}`));
                const filtered = incoming.filter(m => !existingKeys.has(`${m.t}_${m.mid}_${m.is_bot_ai}`));
                chatMessages = [...chatMessages, ...filtered];
                hasMoreNewer = data.has_more && filtered.length > 0;
                renderChatViewport();
            }

            if (chatMessages.length > 0) {
                earliestTs = chatMessages[0].t;
                latestTs = chatMessages[chatMessages.length - 1].t;
            }
        }
    } catch (e) {
        console.error('Chat chunk load error:', e);
    } finally {
        isLoadingChat = false;
        document.getElementById('up-loader').classList.add('hidden');
        document.getElementById('down-loader').classList.add('hidden');
    }
}

function renderMediaPreviewHtml(m) {
    if (!m.fid || !m.mtype) return '';
    const mediaUrl = `/anylise/media/${encodeURIComponent(m.fid)}`;
    if (m.mtype === 'sticker' || m.mtype === 'photo' || m.mtype === 'gif') {
        return `<div class="msg-media-box">
            <img src="${mediaUrl}" loading="lazy" alt="${escapeHtml(m.mtype)}" onerror="this.outerHTML='<div style=\\'padding:8px;background:rgba(0,0,0,0.05);border-radius:8px;font-size:11.5px;\\'>[${escapeHtml(m.mtype.toUpperCase())}: ${escapeHtml(m.fid.slice(0,16))}...]</div>'">
        </div>`;
    }
    if (m.mtype === 'video' || m.mtype === 'video_note') {
        return `<div class="msg-media-box">
            <video src="${mediaUrl}" controls preload="metadata"></video>
        </div>`;
    }
    if (m.mtype === 'voice' || m.mtype === 'audio') {
        return `<div style="margin:6px 0;"><audio src="${mediaUrl}" controls style="height:34px;max-width:220px;"></audio></div>`;
    }
    return `<div style="padding:6px 10px;background:rgba(0,0,0,0.05);border-radius:8px;font-size:12px;margin:4px 0;">📎 <b>${escapeHtml(m.mtype.toUpperCase())}</b> <a href="${mediaUrl}" target="_blank" style="color:#d90429;font-weight:700;">Download</a></div>`;
}

function renderChatViewport() {
    const vp = document.getElementById('tg-viewport');
    if (!chatMessages.length) {
        vp.innerHTML = '<div style="margin:auto;text-align:center;color:#64748b;padding:40px;">📭 No messages found for this filter.</div>';
        return;
    }

    const mode = document.getElementById('chat-mode-select').value;
    const targetId = document.getElementById('chat-target-input').value.trim();

    let lastDateStr = '';
    const htmlParts = [];

    for (const m of chatMessages) {
        const dayPart = (m.dt_str || '').split(',')[0];
        if (dayPart && dayPart !== lastDateStr) {
            lastDateStr = dayPart;
            htmlParts.push(`<div class="date-divider">${escapeHtml(dayPart)}</div>`);
        }

        const isRight = m.is_bot_ai || (mode === 'user' && String(m.uid) === targetId);
        const alignClass = isRight ? 'msg-right' : 'msg-left';
        const botClass = m.is_bot_ai ? 'msg-bot-ai' : '';

        const avatarChar = m.is_bot_ai ? '🌸' : (m.sender_name || 'U')[0].toUpperCase();
        const contextBadge = m.c === 'gc'
            ? `<span style="background:rgba(56,176,0,0.14);color:#1b4332;padding:2px 6px;border-radius:6px;font-size:10px;cursor:pointer;" onclick="event.stopPropagation();inspectGroupChat('${escapeHtml(m.gid)}','${escapeHtml(m.group_title)}')">👥 ${escapeHtml(m.group_title)}</span>`
            : `<span style="background:rgba(255,93,143,0.15);color:#9d174d;padding:2px 6px;border-radius:6px;font-size:10px;">💌 DM</span>`;

        let replyHtml = '';
        if (m.rmid) {
            replyHtml = `<div class="msg-reply-quote">
                ↩️ <b>Reply to ${escapeHtml(m.reply_user_name || ('Msg #' + m.rmid))}:</b> ${escapeHtml(m.reply_preview || ('Message ID ' + m.rmid))}
            </div>`;
        }

        const mediaHtml = renderMediaPreviewHtml(m);

        htmlParts.push(`
        <div class="msg-row ${alignClass} ${botClass}">
            <div class="tg-avatar" style="width:34px;height:34px;font-size:13px;cursor:pointer;" onclick="inspectUserTimeline('${escapeHtml(m.uid)}','${escapeHtml(m.sender_name)}')">${escapeHtml(avatarChar)}</div>
            <div class="msg-bubble">
                <div class="msg-sender" onclick="inspectUserTimeline('${escapeHtml(m.uid)}','${escapeHtml(m.sender_name)}')">
                    <span>${escapeHtml(m.sender_name)} ${m.sender_uname ? `<small style="color:#64748b;font-weight:500;">@${escapeHtml(m.sender_uname)}</small>` : ''}</span>
                    ${contextBadge}
                </div>
                ${replyHtml}
                ${mediaHtml}
                <div>${escapeHtml(m.msg)}</div>
                <div class="msg-meta">
                    <span>#${m.mid || '-'}</span>
                    <span>•</span>
                    <span>${escapeHtml(m.time_short)}</span>
                </div>
            </div>
        </div>`);
    }

    vp.innerHTML = htmlParts.join('');
}

function handleViewportScroll(vp) {
    if (isLoadingChat) return;
    // Scroll Up -> Load Older Messages
    if (vp.scrollTop <= 50 && hasMoreOlder && earliestTs !== null) {
        fetchChatChunk('before', earliestTs - 1, false);
    }
    // Scroll Down -> Load Newer Messages
    if (vp.scrollHeight - vp.scrollTop - vp.clientHeight <= 50 && hasMoreNewer && latestTs !== null) {
        fetchChatChunk('after', latestTs + 1, false);
    }
}

function jumpToOldestLoaded() {
    const vp = document.getElementById('tg-viewport');
    vp.scrollTop = 0;
}

function jumpToBottomLatest() {
    startFreshChatLoad();
}

function initDashboard() {
    loadDashboardData();
}

window.addEventListener('DOMContentLoaded', () => {
    loadDashboardData();
});
</script>
</body>
</html>
"""
