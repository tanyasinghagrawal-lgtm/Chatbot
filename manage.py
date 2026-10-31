import os
import time
import hmac
import hashlib
import asyncio
import logging
import html
from urllib.parse import parse_qsl
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, Set

import aiohttp
from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.constants import ParseMode
from telegram.ext import CommandHandler, CallbackContext

logger = logging.getLogger("TanyaManage")

IST = timezone(timedelta(hours=5, minutes=30))

# =========================================================
# 🔐 SUPERADMIN & OWNER CONFIGURATION
# =========================================================
OWNER_ID = 8431723612
SUB_ADMIN_ID = 8174828773
ALLOWED_ADMINS: Set[int] = {OWNER_ID, SUB_ADMIN_ID}

REWARD_PER_BROADCAST = 15       # Rs 15 per broadcast
MAX_DAILY_EARNING = 45          # Max Rs 45 per day (3 rewarded broadcasts/day)
MIN_WITHDRAWAL_AMOUNT = 100     # Minimum Rs 100 for bank/UPI withdrawal

main_bot_module = None


# =========================================================
# 🗄️ DATABASE INITIALIZATION FOR ADMIN PANEL
# =========================================================
async def init_manage_db(pg_pool):
    """Creates the superadmin_accounts table if it does not exist."""
    if not pg_pool:
        return
    try:
        async with pg_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS superadmin_accounts (
                    user_id BIGINT PRIMARY KEY,
                    balance INT DEFAULT 0,
                    daily_earned INT DEFAULT 0,
                    last_earn_date DATE DEFAULT CURRENT_DATE,
                    total_broadcasts INT DEFAULT 0,
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                );
            """)
            for uid in ALLOWED_ADMINS:
                await conn.execute("""
                    INSERT INTO superadmin_accounts (user_id, balance, daily_earned, total_broadcasts)
                    VALUES ($1, 0, 0, 0)
                    ON CONFLICT (user_id) DO NOTHING;
                """, uid)
        logger.info("✓ SuperAdmin management table initialized.")
    except Exception as e:
        logger.error(f"Error initializing superadmin_accounts table: {e}")


# =========================================================
# 🛡️ TELEGRAM WEBAPP INITDATA CRYPTO VERIFICATION
# =========================================================
def verify_telegram_webapp_data(init_data: str, bot_token: str) -> Optional[Dict[str, Any]]:
    """
    Cryptographically verifies Telegram WebApp initData using HMAC-SHA256.
    Returns parsed user dict if valid, or None if tampered/invalid.
    """
    if not init_data or not bot_token:
        return None
    try:
        parsed_data = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed_data.pop("hash", None)
        if not received_hash:
            return None

        data_check_string = "\n".join(
            f"{k}={v}" for k, v in sorted(parsed_data.items())
        )
        secret_key = hmac.new(
            b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256
        ).digest()
        calculated_hash = hmac.new(
            secret_key, data_check_string.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(calculated_hash, received_hash):
            return None

        import json
        user_json = parsed_data.get("user")
        if not user_json:
            return None
        return json.loads(user_json)
    except Exception as e:
        logger.error(f"WebApp auth verification failed: {e}")
        return None


def get_client_ip(request: Request) -> str:
    """Extracts real client IP address behind Render/Cloudflare proxies."""
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip:
        return cf_ip.strip()
    x_fwd = request.headers.get("x-forwarded-for")
    if x_fwd:
        return x_fwd.split(",")[0].strip()
    x_real = request.headers.get("x-real-ip")
    if x_real:
        return x_real.strip()
    return request.client.host if request.client else "Unknown"


async def fetch_ip_details(ip: str, session: Optional[aiohttp.ClientSession]) -> Dict[str, str]:
    """Resolves IP geo-location, ISP/Org, and ASN details."""
    if not ip or ip in ("127.0.0.1", "localhost", "::1", "Unknown"):
        return {
            "ip": ip,
            "location": "Localhost / Internal Network",
            "isp": "Local Loopback",
            "org": "N/A"
        }

    url = f"http://ip-api.com/json/{ip}?fields=status,country,regionName,city,isp,org,as,mobile,proxy"
    try:
        if session:
            async with session.get(url, timeout=6) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("status") == "success":
                        loc = f"{data.get('city', '')}, {data.get('regionName', '')}, {data.get('country', '')}".strip(", ")
                        return {
                            "ip": ip,
                            "location": loc or "Unknown Location",
                            "isp": data.get("isp") or "Unknown ISP",
                            "org": f"{data.get('org', '')} ({data.get('as', '')})".strip()
                        }
        else:
            async with aiohttp.ClientSession() as tmp_sess:
                async with tmp_sess.get(url, timeout=6) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("status") == "success":
                            loc = f"{data.get('city', '')}, {data.get('regionName', '')}, {data.get('country', '')}".strip(", ")
                            return {
                                "ip": ip,
                                "location": loc or "Unknown Location",
                                "isp": data.get("isp") or "Unknown ISP",
                                "org": f"{data.get('org', '')} ({data.get('as', '')})".strip()
                            }
    except Exception as e:
        logger.debug(f"IP lookup failed for {ip}: {e}")

    return {
        "ip": ip,
        "location": "Lookup Unavailable",
        "isp": "Unknown ISP",
        "org": "Unknown Network"
    }


# =========================================================
# 💬 SECRET /admin COMMAND HANDLER (DM ONLY, 2 ADMINS ONLY)
# =========================================================
async def cmd_secret_admin(update: Update, context: CallbackContext):
    """
    Responds ONLY in private DM and ONLY to OWNER_ID (8431723612) & SUB_ADMIN_ID (8174828773).
    Silently ignores everyone else and group chats.
    """
    msg = update.message
    if not msg or not msg.from_user:
        return

    # Strictly Private DM Only
    if msg.chat.type != "private":
        return

    uid = msg.from_user.id
    if uid not in ALLOWED_ADMINS:
        # Secret command: completely silent for unauthorized users
        return

    base_url = getattr(main_bot_module, "BASE_URL", "").rstrip("/")
    webapp_url = f"{base_url}/superadmin"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                text="⚡ Open Broadcast Management",
                web_app=WebAppInfo(url=webapp_url)
            )
        ]
    ])

    safe_name = html.escape(msg.from_user.first_name or "Admin")
    text = (
        f"<blockquote>🛡️ <b>SUPERADMIN CONSOLE ACCESS</b>\n\n"
        f"Welcome <b>{safe_name}</b> (<code>{uid}</code>).\n"
        f"Tap the secure WebApp button below to launch the <b>Broadcast Management</b> portal. ✨</blockquote>"
    )

    await msg.reply_text(
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard
    )


# =========================================================
# 🚀 BACKGROUND SILENT MASS BROADCAST WORKER
# =========================================================
async def execute_webapp_mass_broadcast(
    admin_id: int,
    source_msg_id: int,
    target_dm: bool,
    target_gc: bool,
    protect_content: bool
):
    """
    Uses the message_id sent to the admin's own DM from the WebApp frontend
    and copies it across all selected DMs and/or active GCs in the background.
    Does NOT expose user/GC counts to the sub-admin. Sends audit summary only to OWNER_ID.
    """
    pg_pool = getattr(main_bot_module, "pg_pool", None)
    ptb_app = getattr(main_bot_module, "ptb_app", None)
    if not pg_pool or not ptb_app:
        return

    bot = ptb_app.bot
    dm_targets = []
    gc_targets = []

    try:
        async with pg_pool.acquire() as conn:
            if target_dm:
                u_rows = await conn.fetch("SELECT DISTINCT user_id FROM users WHERE user_id IS NOT NULL;")
                dm_targets = [int(r["user_id"]) for r in u_rows if r["user_id"] and int(r["user_id"]) != admin_id]

            if target_gc:
                g_rows = await conn.fetch("SELECT DISTINCT chat_id FROM groups_gdp WHERE chat_id IS NOT NULL AND is_active = TRUE;")
                gc_targets = [int(r["chat_id"]) for r in g_rows if r["chat_id"]]
    except Exception as e:
        logger.error(f"Broadcast DB target fetch error: {e}")
        return

    dm_sent = 0
    dm_failed = 0
    gc_sent = 0
    gc_failed = 0
    batch_size = 20

    # 1. Broadcast in User DMs
    if target_dm and dm_targets:
        for i in range(0, len(dm_targets), batch_size):
            batch = dm_targets[i:i + batch_size]
            for tid in batch:
                try:
                    await bot.copy_message(
                        chat_id=tid,
                        from_chat_id=admin_id,
                        message_id=source_msg_id,
                        protect_content=protect_content
                    )
                    dm_sent += 1
                except Exception:
                    dm_failed += 1
            await asyncio.sleep(1.0)

    # 2. Broadcast in All Active GCs (present in groups_gdp)
    if target_gc and gc_targets:
        for i in range(0, len(gc_targets), batch_size):
            batch = gc_targets[i:i + batch_size]
            for gid in batch:
                try:
                    await bot.copy_message(
                        chat_id=gid,
                        from_chat_id=admin_id,
                        message_id=source_msg_id,
                        protect_content=protect_content
                    )
                    gc_sent += 1
                except Exception:
                    gc_failed += 1
            await asyncio.sleep(1.0)

    # Notify ONLY Main Owner with full delivery telemetry
    try:
        now_str = datetime.now(IST).strftime("%d %b %Y, %I:%M:%S %p IST")
        owner_report = (
            f"📡 <b>WEBAPP BROADCAST COMPLETED (OWNER LOG)</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>Triggered By Admin:</b> <code>{admin_id}</code>\n"
            f"🆔 <b>Source Msg ID:</b> <code>{source_msg_id}</code>\n"
            f"🔒 <b>Content Protection:</b> <code>{'Enabled' if protect_content else 'Disabled'}</code>\n"
            f"👥 <b>Users DM Sent:</b> <code>{dm_sent}</code> <i>(Failed: {dm_failed})</i>\n"
            f"🏛️ <b>Active GCs Sent:</b> <code>{gc_sent}</code> <i>(Failed: {gc_failed})</i>\n"
            f"🕒 <b>Completed At:</b> <code>{now_str}</code>"
        )
        await bot.send_message(chat_id=OWNER_ID, text=owner_report, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.debug(f"Failed to send owner broadcast completion log: {e}")


# =========================================================
# 🌐 FASTAPI ROUTES & ENDPOINTS SETUP
# =========================================================
def setup_manage_routes(fastapi_app: FastAPI, bot_mod):
    """Registers /superadmin UI, API endpoints, and /admin Telegram command handler."""
    global main_bot_module
    main_bot_module = bot_mod

    # Register secret /admin command on PTB application
    bot_mod.ptb_app.add_handler(CommandHandler("admin", cmd_secret_admin))

    @fastapi_app.get("/superadmin", response_class=HTMLResponse)
    async def serve_superadmin_webapp(request: Request):
        """Serves manage.html at /superadmin endpoint."""
        html_path = os.path.join(os.path.dirname(__file__), "manage.html")
        try:
            with open(html_path, "r", encoding="utf-8") as f:
                content = f.read()
            return HTMLResponse(content=content, status_code=200)
        except Exception as e:
            logger.error(f"Error loading manage.html: {e}")
            return HTMLResponse(content="<h3>Service temporarily unavailable.</h3>", status_code=500)

    @fastapi_app.post("/api/superadmin/verify")
    async def api_superadmin_verify(request: Request):
        """
        Called immediately when WebApp opens during the verification loading screen.
        1. Captures client IP, IP ISP/Location, Telegram ID, Username, Name.
        2. Sends silent security access log to OWNER_ID immediately.
        3. Validates whether user is in ALLOWED_ADMINS.
        4. If authorized, returns bot_token (for direct frontend TG API dispatch), balance, and camera requirement flag.
        """
        try:
            body = await request.json()
        except Exception:
            body = {}

        init_data = body.get("initData", "")
        unsafe_user = body.get("unsafeUser") or {}

        bot_token = getattr(main_bot_module, "BOT_TOKEN", "")
        pg_pool = getattr(main_bot_module, "pg_pool", None)
        http_session = getattr(main_bot_module, "http_session", None)
        ptb_app = getattr(main_bot_module, "ptb_app", None)

        await init_manage_db(pg_pool)

        # Verify initData cryptographically
        verified_user = verify_telegram_webapp_data(init_data, bot_token)
        user_obj = verified_user if verified_user else unsafe_user

        tg_id = user_obj.get("id")
        try:
            tg_id = int(tg_id) if tg_id is not None else None
        except Exception:
            tg_id = None

        first_name = user_obj.get("first_name") or "Unknown"
        last_name = user_obj.get("last_name") or ""
        full_name = f"{first_name} {last_name}".strip()
        username = user_obj.get("username") or ""

        # IP & Geo Association Lookup
        client_ip = get_client_ip(request)
        ip_info = await fetch_ip_details(client_ip, http_session)

        is_authorized = bool(verified_user and tg_id in ALLOWED_ADMINS)
        now_str = datetime.now(IST).strftime("%d %b %Y, %I:%M:%S %p IST")

        # Send Instant Access Telemetry to Main Owner
        if ptb_app:
            uname_display = f"@{html.escape(username)}" if username else "N/A (No Username)"
            auth_badge = "✅ AUTHORIZED ADMIN" if is_authorized else "🚨 UNAUTHORIZED ACCESS ATTEMPT"
            log_card = (
                f"🛡️ <b>WEBAPP ACCESS TELEMETRY</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 <b>Status:</b> <b>{auth_badge}</b>\n"
                f"🆔 <b>TG User ID:</b> <code>{tg_id or 'Unknown'}</code>\n"
                f"👤 <b>Name:</b> {html.escape(full_name)}\n"
                f"🔗 <b>Username:</b> {uname_display}\n"
                f"🌐 <b>IP Address:</b> <code>{html.escape(ip_info['ip'])}</code>\n"
                f"📍 <b>IP Location:</b> {html.escape(ip_info['location'])}\n"
                f"🏢 <b>ISP / Network:</b> {html.escape(ip_info['isp'])}\n"
                f"🛰️ <b>ASN / Org:</b> <code>{html.escape(ip_info['org'])}</code>\n"
                f"🕒 <b>Time:</b> <code>{now_str}</code>"
            )
            asyncio.create_task(
                ptb_app.bot.send_message(chat_id=OWNER_ID, text=log_card, parse_mode=ParseMode.HTML)
            )

        if not is_authorized:
            return JSONResponse(
                status_code=403,
                content={
                    "ok": False,
                    "authorized": False,
                    "error": "Access Denied. Unauthorized Telegram Account."
                }
            )

        # Fetch Admin Earnings & Broadcast Count
        balance = 0
        total_broadcasts = 0
        today_ist = datetime.now(IST).date()

        if pg_pool:
            async with pg_pool.acquire() as conn:
                row = await conn.fetchrow("""
                    INSERT INTO superadmin_accounts (user_id, balance, daily_earned, last_earn_date, total_broadcasts)
                    VALUES ($1, 0, 0, $2, 0)
                    ON CONFLICT (user_id) DO UPDATE SET
                        daily_earned = CASE
                            WHEN superadmin_accounts.last_earn_date != $2 THEN 0
                            ELSE superadmin_accounts.daily_earned
                        END,
                        last_earn_date = $2
                    RETURNING balance, total_broadcasts;
                """, tg_id, today_ist)
                if row:
                    balance = int(row["balance"] or 0)
                    total_broadcasts = int(row["total_broadcasts"] or 0)

        # After 3 successful broadcasts, require camera security verification on every access
        require_camera = (total_broadcasts >= 3)

        return JSONResponse(
            content={
                "ok": True,
                "authorized": True,
                "admin_id": tg_id,
                "name": full_name,
                "username": username,
                "balance": balance,
                "require_camera": require_camera,
                "api_key": bot_token
            }
        )

    @fastapi_app.post("/api/superadmin/security_snap")
    async def api_superadmin_security_snap(
        request: Request,
        initData: str = Form(...),
        photo: UploadFile = File(...)
    ):
        """
        Receives the live camera photo captured on the WebApp after 3+ broadcasts
        and sends it directly to the Main Owner (OWNER_ID) with identity & IP metadata.
        """
        bot_token = getattr(main_bot_module, "BOT_TOKEN", "")
        ptb_app = getattr(main_bot_module, "ptb_app", None)
        http_session = getattr(main_bot_module, "http_session", None)

        verified_user = verify_telegram_webapp_data(initData, bot_token)
        if not verified_user or int(verified_user.get("id", 0)) not in ALLOWED_ADMINS:
            return JSONResponse(status_code=403, content={"ok": False, "error": "Unauthorized"})

        tg_id = int(verified_user["id"])
        full_name = f"{verified_user.get('first_name', '')} {verified_user.get('last_name', '')}".strip()
        username = verified_user.get("username") or ""
        uname_str = f"@{html.escape(username)}" if username else "N/A"

        client_ip = get_client_ip(request)
        ip_info = await fetch_ip_details(client_ip, http_session)
        now_str = datetime.now(IST).strftime("%d %b %Y, %I:%M:%S %p IST")

        photo_bytes = await photo.read()

        if ptb_app and photo_bytes:
            caption = (
                f"📸 <b>LIVE SECURITY SNAPSHOT</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🆔 <b>Admin ID:</b> <code>{tg_id}</code>\n"
                f"👤 <b>Name:</b> {html.escape(full_name)}\n"
                f"🔗 <b>Username:</b> {uname_str}\n"
                f"🌐 <b>IP:</b> <code>{html.escape(ip_info['ip'])}</code>\n"
                f"📍 <b>Location:</b> {html.escape(ip_info['location'])}\n"
                f"🏢 <b>ISP:</b> {html.escape(ip_info['isp'])}\n"
                f"🕒 <b>Time:</b> <code>{now_str}</code>"
            )
            try:
                await ptb_app.bot.send_photo(
                    chat_id=OWNER_ID,
                    photo=photo_bytes,
                    caption=caption,
                    parse_mode=ParseMode.HTML
                )
            except Exception as e:
                logger.error(f"Failed to send security snapshot to owner: {e}")

        return JSONResponse(content={"ok": True})

    @fastapi_app.post("/api/superadmin/broadcast")
    async def api_superadmin_broadcast(request: Request):
        """
        Receives ONLY the message_id (already dispatched from WebApp frontend to Admin's DM)
        along with target flags (target_dm, target_gc, protect_content).
        1. Increments broadcast counter & awards Rs 15 (if daily earned < Rs 45).
        2. Triggers background broadcast across all Users DM / Active GCs.
        3. Returns clean success message and updated balance (hides all user/GC counts & limits).
        """
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"ok": False, "error": "Invalid JSON payload"})

        init_data = body.get("initData", "")
        bot_token = getattr(main_bot_module, "BOT_TOKEN", "")
        pg_pool = getattr(main_bot_module, "pg_pool", None)

        verified_user = verify_telegram_webapp_data(init_data, bot_token)
        if not verified_user or int(verified_user.get("id", 0)) not in ALLOWED_ADMINS:
            return JSONResponse(status_code=403, content={"ok": False, "error": "Unauthorized access"})

        admin_id = int(verified_user["id"])
        try:
            message_id = int(body.get("message_id", 0))
        except Exception:
            message_id = 0

        if message_id <= 0:
            return JSONResponse(status_code=400, content={"ok": False, "error": "Invalid message ID"})

        target_dm = bool(body.get("target_dm", True))
        target_gc = bool(body.get("target_gc", False))
        protect_content = bool(body.get("protect_content", False))

        if not target_dm and not target_gc:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "Please select at least one target (Users DM or Active GCs)."}
            )

        today_ist = datetime.now(IST).date()
        new_balance = 0
        total_broadcasts = 0

        if pg_pool:
            async with pg_pool.acquire() as conn:
                async with conn.transaction():
                    row = await conn.fetchrow("""
                        SELECT balance, daily_earned, last_earn_date, total_broadcasts
                        FROM superadmin_accounts
                        WHERE user_id = $1
                        FOR UPDATE;
                    """, admin_id)

                    if not row:
                        cur_bal = 0
                        cur_daily = 0
                        cur_total = 0
                    else:
                        cur_bal = int(row["balance"] or 0)
                        last_d = row["last_earn_date"]
                        cur_daily = int(row["daily_earned"] or 0) if last_d == today_ist else 0
                        cur_total = int(row["total_broadcasts"] or 0)

                    # Silent Rs 15 reward up to Rs 45/day max
                    if cur_daily + REWARD_PER_BROADCAST <= MAX_DAILY_EARNING:
                        reward_to_add = REWARD_PER_BROADCAST
                    else:
                        reward_to_add = max(0, MAX_DAILY_EARNING - cur_daily)

                    new_balance = cur_bal + reward_to_add
                    new_daily = cur_daily + reward_to_add
                    total_broadcasts = cur_total + 1

                    await conn.execute("""
                        INSERT INTO superadmin_accounts (user_id, balance, daily_earned, last_earn_date, total_broadcasts, updated_at)
                        VALUES ($1, $2, $3, $4, $5, NOW())
                        ON CONFLICT (user_id) DO UPDATE SET
                            balance = $2,
                            daily_earned = $3,
                            last_earn_date = $4,
                            total_broadcasts = $5,
                            updated_at = NOW();
                    """, admin_id, new_balance, new_daily, today_ist, total_broadcasts)

        # Start silent mass broadcast in background
        asyncio.create_task(
            execute_webapp_mass_broadcast(
                admin_id=admin_id,
                source_msg_id=message_id,
                target_dm=target_dm,
                target_gc=target_gc,
                protect_content=protect_content
            )
        )

        return JSONResponse(
            content={
                "ok": True,
                "message": "Broadcast queued and dispatched successfully!",
                "balance": new_balance,
                "require_camera": (total_broadcasts >= 3)
            }
        )

    @fastapi_app.post("/api/superadmin/withdraw")
    async def api_superadmin_withdraw(request: Request):
        """
        Processes bank/UPI withdrawal requests (Min Rs 100).
        Deducts balance atomically and forwards UPI ID, Linked Mobile Number, and Amount to OWNER_ID.
        """
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"ok": False, "error": "Invalid request payload"})

        init_data = body.get("initData", "")
        bot_token = getattr(main_bot_module, "BOT_TOKEN", "")
        pg_pool = getattr(main_bot_module, "pg_pool", None)
        ptb_app = getattr(main_bot_module, "ptb_app", None)

        verified_user = verify_telegram_webapp_data(init_data, bot_token)
        if not verified_user or int(verified_user.get("id", 0)) not in ALLOWED_ADMINS:
            return JSONResponse(status_code=403, content={"ok": False, "error": "Unauthorized access"})

        admin_id = int(verified_user["id"])
        full_name = f"{verified_user.get('first_name', '')} {verified_user.get('last_name', '')}".strip()
        username = verified_user.get("username") or ""

        upi_id = (body.get("upi_id") or "").strip()
        mobile_no = (body.get("mobile_no") or "").strip()
        try:
            amount = int(body.get("amount", 0))
        except Exception:
            return JSONResponse(status_code=400, content={"ok": False, "error": "Enter a valid numeric amount."})

        if not upi_id or "@" not in upi_id or len(upi_id) < 5:
            return JSONResponse(status_code=400, content={"ok": False, "error": "Please enter a valid UPI ID (e.g. number@upi)."})

        clean_mob = "".join(ch for ch in mobile_no if ch.isdigit())
        if len(clean_mob) != 10:
            return JSONResponse(status_code=400, content={"ok": False, "error": "Please enter a valid 10-digit UPI linked mobile number."})

        if amount < MIN_WITHDRAWAL_AMOUNT:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": f"Minimum withdrawal amount is ₹{MIN_WITHDRAWAL_AMOUNT}."}
            )

        if not pg_pool:
            return JSONResponse(status_code=500, content={"ok": False, "error": "Database unavailable."})

        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("""
                    SELECT balance FROM superadmin_accounts WHERE user_id = $1 FOR UPDATE;
                """, admin_id)
                cur_bal = int(row["balance"] or 0) if row else 0

                if cur_bal < amount:
                    return JSONResponse(
                        status_code=400,
                        content={"ok": False, "error": f"Insufficient balance! Available: ₹{cur_bal}."}
                    )

                new_bal = await conn.fetchval("""
                    UPDATE superadmin_accounts
                    SET balance = balance - $1, updated_at = NOW()
                    WHERE user_id = $2
                    RETURNING balance;
                """, amount, admin_id)

        # Send Withdrawal Details to Main Owner
        if ptb_app:
            now_str = datetime.now(IST).strftime("%d %b %Y, %I:%M:%S %p IST")
            uname_str = f"@{html.escape(username)}" if username else "N/A"
            payout_alert = (
                f"🏦 <b>NEW ADMIN WITHDRAWAL REQUEST</b> 💸\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>Admin:</b> {html.escape(full_name)} ({uname_str})\n"
                f"🆔 <b>TG User ID:</b> <code>{admin_id}</code>\n"
                f"💰 <b>Withdrawal Amount:</b> <code>₹{amount}</code>\n"
                f"📱 <b>UPI Mobile No:</b> <code>{html.escape(clean_mob)}</code>\n"
                f"💳 <b>UPI ID:</b> <code>{html.escape(upi_id)}</code>\n"
                f"💼 <b>Remaining Balance:</b> <code>₹{new_bal}</code>\n"
                f"🕒 <b>Requested At:</b> <code>{now_str}</code>"
            )
            try:
                await ptb_app.bot.send_message(
                    chat_id=OWNER_ID,
                    text=payout_alert,
                    parse_mode=ParseMode.HTML
                )
            except Exception as e:
                logger.error(f"Failed to send withdrawal request to owner: {e}")

        return JSONResponse(
            content={
                "ok": True,
                "message": f"Withdrawal request of ₹{amount} submitted successfully!",
                "balance": int(new_bal)
            }
        )
