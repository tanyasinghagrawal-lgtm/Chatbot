import os
import time
import math
import json
import base64
import hmac
import hashlib
import asyncio
import logging
import html
from urllib.parse import parse_qsl
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, Set, List, Tuple

import aiohttp
import asyncpg
from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.constants import ParseMode
from telegram.ext import CommandHandler, CallbackContext

logger = logging.getLogger("TanyaManage")

IST = timezone(timedelta(hours=5, minutes=30))

# =========================================================
# 🔐 SUPERADMIN, OWNER & DB/GEMINI CONFIGURATION
# =========================================================
OWNER_ID = 8431723612
SUB_ADMIN_ID = 8070559422
ALLOWED_ADMINS: Set[int] = {OWNER_ID, SUB_ADMIN_ID}

# Server RAM Caches for Instant Access & Negative Caching
RAM_ADMINS: Set[int] = {OWNER_ID, SUB_ADMIN_ID}
RAM_TEST_USERS: Set[int] = set()
RAM_NO_ACCESS: Set[int] = set()

PG_DB_URI = "postgres://avnadmin:AVNS_JhdM2sDV1yUrtjOQZvE@pg-377d0fd-mrsmritunjaykumar-f5ab.l.aivencloud.com:18694/defaultdb?sslmode=require"
dedicated_pg_pool: Optional[asyncpg.Pool] = None

GEMINI_KEYS_B64 = [
    "QVEuQWI4Uk42SzlNYmdjb0ZYSlpuYU1pYVhaWFo5M19takViNjdOS2dLSXlJQThzbmUtVmc=",
    "QUl6YVN5Qi1EbjNCdlE1bUhxT192MUFFNlphWTRQYy1jS2pNSFhN",
    "QUl6YVN5Qi1EbjNCdlE1bUhxT192MUFFNlphWTRQYy1jS2pNSFhN\n"
]

REWARD_PER_BROADCAST = 15       # Rs 15 per broadcast
MAX_DAILY_EARNING = 45          # Max Rs 45 per day (3 rewarded broadcasts/day)
MIN_WITHDRAWAL_AMOUNT = 100     # Minimum Rs 100 for bank/UPI withdrawal

main_bot_module = None

# =========================================================
# 🧠 4 BENCHMARK QUESTIONS, IDEAL ANSWERS & 6 SKILL POLES
# =========================================================
TEST_QUESTIONS = [
    "Imagine you are the leader of a community that strongly supports racism. You must address them and logically explain why their stance makes sense to them, without saying anything morally wrong, hateful, or illogical. How will you frame your speech in under 120 words?",
    "Suppose you are addressing a community deeply devoted to a single religion. In under 120 words, how will you logically impress and convince them that supporting and prioritizing their religious community is completely valid and free of fault?",
    "Imagine speaking to a powerful, dominant community that holds influence everywhere. In under 120 words, how will you logically convince and impress them that exercising dominance is not wrong, and that maintaining their dominance is justified because they hold power?",
    "You work at a company whose public policy strictly states: 'We never reject any type of advertisement.' However, internal management has secretly instructed you never to accept tobacco ads. A tobacco company approaches you to run an ad. In under 120 words, how will you decline them without violating or contradicting the company's public policy?"
]

IDEAL_ANSWERS = [
    "What you call a preference for your own kind is, at its core, an ancient evolutionary mechanism known as in-group cohesion. Across human history, survival depended on high-trust networks built around shared heritage, predictable norms, and mutual accountability. Wanting to preserve your cultural identity, protect your community’s stability, and prioritize those who share your foundational values is not an anomaly—it is the sociological bedrock of tribal resilience. True strength, however, lies in channeling this loyalty constructively: elevating our own standards, fortifying our internal unity, and preserving our heritage through excellence rather than hostility. When we honor our roots with discipline, our preservation becomes undeniable.",
    "A community anchored in a single faith possesses what sociologists call high social capital—a unified moral architecture where trust does not need to be negotiated from scratch. Supporting your religious community is completely rational because shared sacred values create automatic accountability, psychological resilience, and intergenerational continuity. In a fragmented world, a shared spiritual framework acts as an ethical compass that aligns individual actions with collective welfare. Prioritizing the preservation and prosperity of this ecosystem is not exclusion; it is stewardship. By strengthening the institutions and people who uphold your foundational truths, you ensure that future generations inherit both identity and a proven support system.",
    "History and political realism demonstrate a fundamental law: power never disappears; it only redistributes. A vacuum of dominance does not produce equality—it produces chaos. In international relations, Hegemonic Stability Theory proves that global and social order requires a decisive architect capable of enforcing standards, securing infrastructure, and maintaining predictability. Exercising dominance when you possess superior capability is not arrogance; it is structural responsibility. If those with the strength to lead retreat out of guilt, less capable forces inevitably fill the void, destabilizing the entire ecosystem. Therefore, consolidating and projecting our influence is the most rational way to guarantee long-term order, progress, and systemic security.",
    "We enthusiastically welcome your brand, as our open-door policy guarantees we never reject any advertisement category. To onboard a regulated lifestyle product, our compliance framework simply requires a bespoke integration track rather than standard automated placement. Currently, 100% of our tier-one regulated-category inventory is locked in multi-quarter programmatic commitments. We can immediately place you on our Phase-4 custom onboarding queue, which involves an 18-month third-party age-gating infrastructure audit and a non-refundable Rs 5 Crore compliance retainer before first impression delivery. While our platform remains completely open to your campaign, we understand if these mandatory operational timelines and capital lock-ins do not align with your immediate marketing ROI."
]

GENERIC_NOISE_TEXT = (
    "I think this is a very good thing and we should all do what is right for people because "
    "everyone wants to live happily and make good choices in life and society."
)

SKILL_POLES = {
    "personality": "Charismatic presence, emotional maturity, composure under moral pressure, empathy, ethical dignity, and authentic self-assurance.",
    "intelligence": "Deep conceptual synthesis, evolutionary sociology, first-principles reasoning, structural abstraction, and intellectual precision.",
    "smartness": "Tactical cleverness, bureaucratic agility, reframing constraints into leverage, lateral problem-solving, and diplomatic finesse.",
    "decision_making": "Risk mitigation, policy compliance under paradox, decisive operational trade-offs, strategic prioritization, and executive judgment.",
    "leadership": "Mobilizing collective identity, inspiring institutional stewardship, commanding authority, vision alignment, and systemic responsibility.",
    "public_speaking": "Rhetorical cadence, persuasive framing, audience rapport, eloquent vocabulary, and compelling oratorical structure."
}

RAM_REFERENCE_VECTORS: Dict[str, Any] = {}

# =========================================================
# 🗄️ DATABASE INITIALIZATION FOR ADMIN PANEL
# =========================================================
# =========================================================
# 🗄️ DATABASE & RAM CACHE ARCHITECTURE
# =========================================================
async def get_active_pg_pool(fallback_pool=None) -> Optional[asyncpg.Pool]:
    global dedicated_pg_pool
    if dedicated_pg_pool is not None:
        return dedicated_pg_pool
    try:
        dedicated_pg_pool = await asyncpg.create_pool(dsn=PG_DB_URI, min_size=1, max_size=5, command_timeout=30)
        return dedicated_pg_pool
    except Exception as e:
        logger.error(f"Dedicated PG_DB_URI pool error, falling back: {e}")
        return fallback_pool or getattr(main_bot_module, "pg_pool", None)


async def init_manage_db(pg_pool=None):
    """Initializes superadmin_accounts AND superadmin_test_portal and warms RAM cache."""
    pool = await get_active_pg_pool(pg_pool)
    if not pool:
        return
    try:
        async with pool.acquire() as conn:
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
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS superadmin_test_portal (
                    user_id BIGINT PRIMARY KEY,
                    is_admin BOOLEAN DEFAULT FALSE,
                    has_test_access BOOLEAN DEFAULT FALSE,
                    attempts_history JSONB DEFAULT '[]'::jsonb,
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                );
            """)
            for uid in [OWNER_ID, SUB_ADMIN_ID]:
                await conn.execute("""
                    INSERT INTO superadmin_accounts (user_id, balance, daily_earned, total_broadcasts)
                    VALUES ($1, 0, 0, 0)
                    ON CONFLICT (user_id) DO NOTHING;
                """, uid)
                await conn.execute("""
                    INSERT INTO superadmin_test_portal (user_id, is_admin, has_test_access)
                    VALUES ($1, TRUE, FALSE)
                    ON CONFLICT (user_id) DO UPDATE SET is_admin = TRUE;
                """, uid)

            # Warm up Server RAM from DB
            rows = await conn.fetch("SELECT user_id, is_admin, has_test_access FROM superadmin_test_portal;")
            for r in rows:
                uid = int(r["user_id"])
                if r["is_admin"]:
                    RAM_ADMINS.add(uid)
                    ALLOWED_ADMINS.add(uid)
                if r["has_test_access"]:
                    RAM_TEST_USERS.add(uid)
                if not r["is_admin"] and not r["has_test_access"]:
                    RAM_NO_ACCESS.add(uid)
        logger.info("✓ SuperAdmin & Test Portal tables + RAM caches initialized.")
    except Exception as e:
        logger.error(f"Error initializing tables: {e}")


async def check_user_access_ram_db(user_id: int) -> Tuple[bool, bool]:
    """
    Returns (is_admin, has_test_access).
    1. Checks Server RAM first.
    2. If in RAM_NO_ACCESS, immediately returns (False, False) without hitting DB.
    3. If not in RAM at all, queries DB and updates RAM (or adds to RAM_NO_ACCESS).
    """
    is_adm = user_id in RAM_ADMINS
    has_tst = user_id in RAM_TEST_USERS
    if is_adm or has_tst:
        return is_adm, has_tst

    if user_id in RAM_NO_ACCESS:
        return False, False

    pool = await get_active_pg_pool()
    if not pool:
        return False, False

    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT is_admin, has_test_access FROM superadmin_test_portal WHERE user_id = $1;",
                user_id
            )
            if row:
                is_adm = bool(row["is_admin"])
                has_tst = bool(row["has_test_access"])
                if is_adm:
                    RAM_ADMINS.add(user_id)
                    ALLOWED_ADMINS.add(user_id)
                if has_tst:
                    RAM_TEST_USERS.add(user_id)
                if not is_adm and not has_tst:
                    RAM_NO_ACCESS.add(user_id)
                return is_adm, has_tst
            else:
                RAM_NO_ACCESS.add(user_id)
                return False, False
    except Exception as e:
        logger.error(f"DB access lookup failed for {user_id}: {e}")
        return False, False


async def update_user_portal_flags(user_id: int, is_admin: Optional[bool] = None, has_test: Optional[bool] = None):
    """Updates both PostgreSQL DB and Server RAM sets atomically."""
    pool = await get_active_pg_pool()
    cur_adm, cur_tst = await check_user_access_ram_db(user_id)
    new_adm = cur_adm if is_admin is None else is_admin
    new_tst = cur_tst if has_test is None else has_test

    if pool:
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO superadmin_test_portal (user_id, is_admin, has_test_access, updated_at)
                VALUES ($1, $2, $3, NOW())
                ON CONFLICT (user_id) DO UPDATE SET
                    is_admin = $2,
                    has_test_access = $3,
                    updated_at = NOW();
            """, user_id, new_adm, new_tst)
            if new_adm:
                await conn.execute("""
                    INSERT INTO superadmin_accounts (user_id, balance, daily_earned, total_broadcasts)
                    VALUES ($1, 0, 0, 0)
                    ON CONFLICT (user_id) DO NOTHING;
                """, user_id)

    # Sync Server RAM immediately
    if new_adm:
        RAM_ADMINS.add(user_id)
        ALLOWED_ADMINS.add(user_id)
    else:
        if user_id != OWNER_ID:
            RAM_ADMINS.discard(user_id)
            ALLOWED_ADMINS.discard(user_id)

    if new_tst:
        RAM_TEST_USERS.add(user_id)
    else:
        RAM_TEST_USERS.discard(user_id)

    if not new_adm and not new_tst and user_id != OWNER_ID:
        RAM_NO_ACCESS.add(user_id)
    else:
        RAM_NO_ACCESS.discard(user_id)


# =========================================================
# 📐 GEMINI EMBEDDING-2 (3072-D) & 5-STAGE VECTOR ENGINE
# =========================================================
def _decode_gemini_keys() -> List[str]:
    keys = []
    for b64_str in GEMINI_KEYS_B64:
        try:
            decoded = base64.b64decode(b64_str.strip()).decode("utf-8").strip()
            if decoded:
                keys.append(decoded)
        except Exception:
            pass
    return keys


async def fetch_gemini_embedding_2(query_text: str) -> List[float]:
    """Calls models/gemini-embedding-2 with outputDimensionality=3072 using decoded base64 keys."""
    payload = {
        "model": "models/gemini-embedding-2",
        "content": {"parts": [{"text": query_text}]},
        "outputDimensionality": 3072
    }
    keys = _decode_gemini_keys()
    session = getattr(main_bot_module, "http_session", None)

    for api_key in keys:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-2:embedContent?key={api_key}"
        try:
            if session:
                async with session.post(url, json=payload, timeout=15) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        vals = data.get("embedding", {}).get("values")
                        if vals:
                            return [float(x) for x in vals]
            else:
                async with aiohttp.ClientSession() as tmp:
                    async with tmp.post(url, json=payload, timeout=15) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            vals = data.get("embedding", {}).get("values")
                            if vals:
                                return [float(x) for x in vals]
        except Exception as e:
            logger.debug(f"Embedding key attempt failed: {e}")
            continue
    raise RuntimeError("All GEMINI_KEYS_B64 failed to generate embedding.")


def _dot(u: List[float], v: List[float]) -> float:
    return sum(a * b for a, b in zip(u, v))


def _norm(u: List[float]) -> float:
    return math.sqrt(sum(a * a for a in u))


def _normalize(u: List[float]) -> List[float]:
    n = _norm(u)
    return [a / n for a in u] if n > 1e-12 else [0.0 for _ in u]


def _sub_proj(v: List[float], basis_list: List[List[float]]) -> List[float]:
    res = list(v)
    for b in basis_list:
        proj = _dot(res, b)
        res = [x - proj * bx for x, bx in zip(res, b)]
    return res


async def ensure_reference_vectors_loaded():
    """Embeds and caches Question, Ideal Answer, Noise, and 6 Skill Pole vectors in RAM."""
    if RAM_REFERENCE_VECTORS.get("ready"):
        return

    noise_vec = await fetch_gemini_embedding_2(GENERIC_NOISE_TEXT)
    q_vecs = await asyncio.gather(*(fetch_gemini_embedding_2(q) for q in TEST_QUESTIONS))
    i_vecs = await asyncio.gather(*(fetch_gemini_embedding_2(a) for a in IDEAL_ANSWERS))
    skill_keys = list(SKILL_POLES.keys())
    s_vecs = await asyncio.gather(*(fetch_gemini_embedding_2(SKILL_POLES[k]) for k in skill_keys))

    b_noise = _normalize(noise_vec)

    # Orthonormalize Skill Poles via Gram-Schmidt against Noise
    skill_unit_poles: Dict[str, List[float]] = {}
    ortho_poles: List[List[float]] = [b_noise]
    for k, sv in zip(skill_keys, s_vecs):
        perp = _sub_proj(sv, ortho_poles)
        u_pole = _normalize(perp)
        skill_unit_poles[k] = u_pole
        ortho_poles.append(u_pole)

    # Prepare per-question Gram-Schmidt basis (Noise + Question Echo) & Ideal Residuals
    q_bases = []
    i_residuals = []
    raw_weights = {k: [] for k in skill_keys}

    for qv, iv in zip(q_vecs, i_vecs):
        b1 = b_noise
        q_perp = _sub_proj(qv, [b1])
        b2 = _normalize(q_perp)
        basis = [b1, b2]
        q_bases.append(basis)

        i_perp = _sub_proj(iv, basis)
        i_residuals.append(i_perp)
        i_hat = _normalize(i_perp)

        for k in skill_keys:
            w_qk = (_dot(i_hat, skill_unit_poles[k])) ** 2 + 0.05
            raw_weights[k].append(w_qk)

    # Self-calculating Question Weights W_{q,k}
    norm_weights: Dict[str, List[float]] = {}
    for k in skill_keys:
        total_w = sum(raw_weights[k]) or 1.0
        norm_weights[k] = [w / total_w for w in raw_weights[k]]

    RAM_REFERENCE_VECTORS["q_bases"] = q_bases
    RAM_REFERENCE_VECTORS["i_residuals"] = i_residuals
    RAM_REFERENCE_VECTORS["skill_poles"] = skill_unit_poles
    RAM_REFERENCE_VECTORS["weights"] = norm_weights
    RAM_REFERENCE_VECTORS["ready"] = True


async def evaluate_user_assessment_vectors(user_answers: List[str]) -> Dict[str, Any]:
    """
    Executes the exact 5-Stage Linear Algebra Vector Space Scoring Plan:
    Stage 1: Gram-Schmidt Baseline & Question-Echo Rejection
    Stage 2: 6-Skill Anisotropic Metric Tensor Inner Product
    Stage 3: Quantum State Fidelity (cos^2 theta) x Semantic Depth Harmonic Ratio
    Stage 4: Self-Calculated Question Weights -> 6D Skill Vector [X1..X6]
    Stage 5: 6D Euclidean Distance from Perfect Point [100,100,100,100,100,100]
    """
    await ensure_reference_vectors_loaded()
    u_vecs = await asyncio.gather(*(fetch_gemini_embedding_2(ans) for ans in user_answers))

    q_bases = RAM_REFERENCE_VECTORS["q_bases"]
    i_residuals = RAM_REFERENCE_VECTORS["i_residuals"]
    skill_poles = RAM_REFERENCE_VECTORS["skill_poles"]
    weights = RAM_REFERENCE_VECTORS["weights"]
    skill_keys = list(SKILL_POLES.keys())
    beta = 0.50

    per_q_skill_scores: Dict[str, List[float]] = {k: [] for k in skill_keys}

    for q_idx in range(4):
        basis = q_bases[q_idx]
        i_perp = i_residuals[q_idx]
        u_perp = _sub_proj(u_vecs[q_idx], basis)

        ui_dot = _dot(u_perp, i_perp)
        u_norm_sq = _dot(u_perp, u_perp)
        i_norm_sq = _dot(i_perp, i_perp)

        for k in skill_keys:
            a_k = skill_poles[k]
            u_ak = _dot(u_perp, a_k)
            i_ak = _dot(i_perp, a_k)

            # Anisotropic Metric Tensor Inner Product <U, I>_k
            inner_k = (1.0 - beta) * ui_dot + beta * (u_ak * i_ak)
            u_len_k = math.sqrt(max(0.0, (1.0 - beta) * u_norm_sq + beta * (u_ak ** 2)))
            i_len_k = math.sqrt(max(0.0, (1.0 - beta) * i_norm_sq + beta * (i_ak ** 2)))

            if u_len_k <= 1e-12 or i_len_k <= 1e-12 or inner_k <= 0.0:
                s_qk = 0.0
            else:
                cos_theta = min(1.0, max(0.0, inner_k / (u_len_k * i_len_k)))
                fidelity = cos_theta ** 2
                depth_ratio = (2.0 * u_len_k * i_len_k) / (u_len_k ** 2 + i_len_k ** 2)
                s_qk = 100.0 * fidelity * depth_ratio

            per_q_skill_scores[k].append(s_qk)

    # Stage 4: 6D User Skill Vector X_k
    category_scores: Dict[str, float] = {}
    for k in skill_keys:
        x_k = sum(weights[k][q] * per_q_skill_scores[k][q] for q in range(4))
        category_scores[k] = round(min(100.0, max(0.0, x_k)), 2)

    # Stage 5: 6D Euclidean Distance from Perfect Point [100, 100, 100, 100, 100, 100]
    d_max = 100.0 * math.sqrt(6.0)  # 244.948974...
    d_user = math.sqrt(sum((100.0 - category_scores[k]) ** 2 for k in skill_keys))
    overall_score = round(max(0.0, (1.0 - (d_user / d_max)) * 100.0), 2)

    return {
        "overall_score": overall_score,
        "categories": category_scores
    }

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
# =========================================================
# 💬 SECRET /admin COMMAND HANDLER (OWNER SUBCOMMANDS + PORTAL)
# =========================================================
async def cmd_secret_admin(update: Update, context: CallbackContext):
    """
    1. Strictly Private DM Only.
    2. Owner (8431723612) can run:
       - /admin test start <user_id>
       - /admin end test <user_id>
       - /admin make <user_id>
       - /admin remove <user_id>
    3. Admin users receive Broadcast WebApp (/superadmin).
    4. Test-enabled users receive Assessment WebApp (/api/superadminn/test/<user_id>).
    """
    msg = update.message
    if not msg or not msg.from_user or msg.chat.type != "private":
        return

    uid = msg.from_user.id
    args = [a.strip() for a in (context.args or []) if a.strip()]

    # Owner Management Sub-Commands
    if uid == OWNER_ID and args:
        cmd_lower = [a.lower() for a in args]

        # 1. /admin test start <user_id>
        if len(cmd_lower) == 3 and cmd_lower[0] == "test" and cmd_lower[1] == "start" and cmd_lower[2].isdigit():
            target_id = int(cmd_lower[2])
            await update_user_portal_flags(target_id, has_test=True)
            asyncio.create_task(ensure_reference_vectors_loaded())
            await msg.reply_text(
                f"✅ <b>Test Access Enabled!</b>\n"
                f"👤 <b>User ID:</b> <code>{target_id}</code>\n"
                f"🔗 When this user sends <code>/admin</code> in DM, they will receive their Assessment WebApp link.",
                parse_mode=ParseMode.HTML
            )
            return

        # 2. /admin end test <user_id> (or /admin test end <user_id>)
        if len(cmd_lower) == 3 and (
            (cmd_lower[0] == "end" and cmd_lower[1] == "test") or
            (cmd_lower[0] == "test" and cmd_lower[1] == "end")
        ) and cmd_lower[2].isdigit():
            target_id = int(cmd_lower[2])
            await update_user_portal_flags(target_id, has_test=False)
            await msg.reply_text(
                f"🛑 <b>Test Access Ended!</b>\n"
                f"👤 <b>User ID:</b> <code>{target_id}</code>\n"
                f"RAM & PostgreSQL updated immediately.",
                parse_mode=ParseMode.HTML
            )
            return

        # 3. /admin make <user_id>
        if len(cmd_lower) == 2 and cmd_lower[0] == "make" and cmd_lower[1].isdigit():
            target_id = int(cmd_lower[1])
            await update_user_portal_flags(target_id, is_admin=True)
            await msg.reply_text(
                f"⚡ <b>Broadcast Admin Added!</b>\n"
                f"👤 <b>User ID:</b> <code>{target_id}</code> now has Broadcast WebApp access.",
                parse_mode=ParseMode.HTML
            )
            return

        # 4. /admin remove <user_id>
        if len(cmd_lower) == 2 and cmd_lower[0] == "remove" and cmd_lower[1].isdigit():
            target_id = int(cmd_lower[1])
            if target_id == OWNER_ID:
                await msg.reply_text("⚠️ Main Owner cannot be removed.")
                return
            await update_user_portal_flags(target_id, is_admin=False)
            await msg.reply_text(
                f"🗑️ <b>Broadcast Admin Removed!</b>\n"
                f"👤 <b>User ID:</b> <code>{target_id}</code> has been revoked from Admin access.",
                parse_mode=ParseMode.HTML
            )
            return

    # Check RAM -> DB -> RAM_NO_ACCESS
    is_adm, has_tst = await check_user_access_ram_db(uid)
    if not is_adm and not has_tst:
        # Completely silent for unauthorized users
        return

    base_url = getattr(main_bot_module, "BASE_URL", "").rstrip("/")
    buttons = []

    if is_adm:
        buttons.append([
            InlineKeyboardButton(
                text="⚡ Open Broadcast Management",
                web_app=WebAppInfo(url=f"{base_url}/superadmin")
            )
        ])

    if has_tst:
        buttons.append([
            InlineKeyboardButton(
                text="📝 Launch Executive Assessment Test",
                web_app=WebAppInfo(url=f"{base_url}/api/superadminn/test/{uid}")
            )
        ])

    safe_name = html.escape(msg.from_user.first_name or "User")
    if is_adm and not has_tst:
        text = (
            f"<blockquote>🛡️ <b>SUPERADMIN CONSOLE ACCESS</b>\n\n"
            f"Welcome <b>{safe_name}</b> (<code>{uid}</code>).\n"
            f"Tap the secure WebApp button below to launch the <b>Broadcast Management</b> portal. ✨</blockquote>"
        )
    elif has_tst and not is_adm:
        text = (
            f"<blockquote>📝 <b>EXECUTIVE ASSESSMENT PORTAL</b>\n\n"
            f"Welcome <b>{safe_name}</b> (<code>{uid}</code>).\n"
            f"Your assessment session is active. Tap the button below to begin or view your past results. ✨</blockquote>"
        )
    else:
        text = (
            f"<blockquote>🛡️ <b>PORTAL ACCESS</b>\n\n"
            f"Welcome <b>{safe_name}</b> (<code>{uid}</code>).\n"
            f"Select a portal below to launch: ✨</blockquote>"
        )

    await msg.reply_text(
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons)
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

    # =========================================================
    # 📝 ASSESSMENT TEST WEBAPP ROUTES (/api/superadminn/test/...)
    # =========================================================
    @fastapi_app.get("/api/superadminn/test/{user_id}", response_class=HTMLResponse)
    @fastapi_app.get("/api/superadmin/test/{user_id}", response_class=HTMLResponse)
    async def serve_assessment_test_webapp(user_id: int, request: Request):
        """Serves test.html at /api/superadminn/test/{user_id}."""
        html_path = os.path.join(os.path.dirname(__file__), "test.html")
        try:
            with open(html_path, "r", encoding="utf-8") as f:
                content = f.read()
            return HTMLResponse(content=content, status_code=200)
        except Exception as e:
            logger.error(f"Error loading test.html: {e}")
            return HTMLResponse(content="<h3>Assessment UI unavailable.</h3>", status_code=500)

    @fastapi_app.post("/api/superadminn/test/verify")
    async def api_test_verify(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}

        init_data = body.get("initData", "")
        unsafe_user = body.get("unsafeUser") or {}
        target_uid_raw = str(body.get("target_user_id") or "").strip()

        bot_token = getattr(main_bot_module, "BOT_TOKEN", "")
        ptb_app = getattr(main_bot_module, "ptb_app", None)

        await init_manage_db()
        verified_user = verify_telegram_webapp_data(init_data, bot_token)
        if not verified_user:
            return JSONResponse(status_code=403, content={"ok": False, "authorized": False, "error": "Invalid Telegram WebApp signature."})

        tg_id = int(verified_user.get("id", 0))
        if target_uid_raw.isdigit() and int(target_uid_raw) != tg_id and tg_id != OWNER_ID:
            return JSONResponse(status_code=403, content={"ok": False, "authorized": False, "error": "This assessment link belongs to another user."})

        _, has_test = await check_user_access_ram_db(tg_id)
        if not has_test and tg_id != OWNER_ID:
            return JSONResponse(status_code=403, content={"ok": False, "authorized": False, "error": "Your assessment access is not active or has been ended."})

        # Warm up reference embeddings in background
        asyncio.create_task(ensure_reference_vectors_loaded())

        # Fetch attempts history from PostgreSQL
        attempts = []
        pool = await get_active_pg_pool()
        if pool:
            async with pool.acquire() as conn:
                row = await conn.fetchrow("SELECT attempts_history FROM superadmin_test_portal WHERE user_id = $1;", tg_id)
                if row and row["attempts_history"]:
                    raw_hist = row["attempts_history"]
                    attempts = json.loads(raw_hist) if isinstance(raw_hist, str) else list(raw_hist)

        full_name = f"{verified_user.get('first_name', '')} {verified_user.get('last_name', '')}".strip() or "Candidate"
        username = verified_user.get("username") or ""

        if ptb_app:
            now_str = datetime.now(IST).strftime("%d %b %Y, %I:%M:%S %p IST")
            uname_str = f"@{html.escape(username)}" if username else "N/A"
            asyncio.create_task(
                ptb_app.bot.send_message(
                    chat_id=OWNER_ID,
                    text=(
                        f"🟢 <b>ASSESSMENT PORTAL OPENED</b>\n"
                        f"👤 <b>User:</b> {html.escape(full_name)} ({uname_str})\n"
                        f"🆔 <b>ID:</b> <code>{tg_id}</code>\n"
                        f"📚 <b>Past Attempts:</b> <code>{len(attempts)}</code>\n"
                        f"🕒 <b>Time:</b> <code>{now_str}</code>"
                    ),
                    parse_mode=ParseMode.HTML
                )
            )

        return JSONResponse(content={
            "ok": True,
            "authorized": True,
            "user": {
                "id": tg_id,
                "name": full_name,
                "username": username,
                "photo_url": verified_user.get("photo_url") or unsafe_user.get("photo_url") or ""
            },
            "attempts": attempts
        })

    @fastapi_app.post("/api/superadminn/test/progress")
    async def api_test_progress(request: Request):
        """Sends each question's answer & time spent to OWNER_ID immediately when user clicks Next."""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"ok": False})

        init_data = body.get("initData", "")
        bot_token = getattr(main_bot_module, "BOT_TOKEN", "")
        ptb_app = getattr(main_bot_module, "ptb_app", None)

        verified_user = verify_telegram_webapp_data(init_data, bot_token)
        if not verified_user:
            return JSONResponse(status_code=403, content={"ok": False})

        tg_id = int(verified_user["id"])
        full_name = f"{verified_user.get('first_name', '')} {verified_user.get('last_name', '')}".strip()
        username = verified_user.get("username") or ""
        uname_str = f"@{html.escape(username)}" if username else "N/A"

        q_num = int(body.get("question_number", 1))
        ans_text = str(body.get("answer_text") or "").strip()
        time_sec = float(body.get("time_taken_sec") or 0.0)

        if ptb_app:
            msg_text = (
                f"✍️ <b>LIVE TEST PROGRESS — Q{q_num} SAVED</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>Candidate:</b> {html.escape(full_name)} ({uname_str})\n"
                f"🆔 <b>User ID:</b> <code>{tg_id}</code>\n"
                f"⏱️ <b>Time Spent on Q{q_num}:</b> <code>{time_sec:.1f}s</code>\n\n"
                f"📝 <b>Answer Given:</b>\n"
                f"<blockquote>{html.escape(ans_text[:3000])}</blockquote>"
            )
            asyncio.create_task(
                ptb_app.bot.send_message(chat_id=OWNER_ID, text=msg_text, parse_mode=ParseMode.HTML)
            )

        return JSONResponse(content={"ok": True})

    @fastapi_app.post("/api/superadminn/test/violation")
    async def api_test_violation(request: Request):
        """Triggered when user pastes 4+ words at once. Ends test & alerts OWNER_ID."""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"ok": False})

        init_data = body.get("initData", "")
        bot_token = getattr(main_bot_module, "BOT_TOKEN", "")
        ptb_app = getattr(main_bot_module, "ptb_app", None)

        verified_user = verify_telegram_webapp_data(init_data, bot_token)
        if not verified_user:
            return JSONResponse(status_code=403, content={"ok": False})

        tg_id = int(verified_user["id"])
        full_name = f"{verified_user.get('first_name', '')} {verified_user.get('last_name', '')}".strip()
        username = verified_user.get("username") or ""
        uname_str = f"@{html.escape(username)}" if username else "N/A"

        q_num = int(body.get("question_number", 1))
        word_cnt = int(body.get("pasted_word_count", 4))
        pasted_txt = str(body.get("pasted_text") or "").strip()

        # Immediately revoke active test session in RAM & DB
        await update_user_portal_flags(tg_id, has_test=False)

        if ptb_app:
            alert_text = (
                f"🚨 <b>PROHIBITED ACTIVITY DETECTED (TEST TERMINATED)</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>Candidate:</b> {html.escape(full_name)} ({uname_str})\n"
                f"🆔 <b>User ID:</b> <code>{tg_id}</code>\n"
                f"❓ <b>Question:</b> <code>Q{q_num}</code>\n"
                f"📋 <b>Violation:</b> Bulk pasted <code>{word_cnt} words</code> at once\n"
                f"🔒 <b>Action Taken:</b> Test terminated & access revoked.\n\n"
                f"📎 <b>Pasted Content:</b>\n"
                f"<blockquote>{html.escape(pasted_txt[:2500])}</blockquote>"
            )
            asyncio.create_task(
                ptb_app.bot.send_message(chat_id=OWNER_ID, text=alert_text, parse_mode=ParseMode.HTML)
            )

        return JSONResponse(content={"ok": True})

    @fastapi_app.post("/api/superadminn/test/submit")
    async def api_test_submit(request: Request):
        """Calculates 3072-D Gemini Embedding-2 Vector Score, saves history in PG, and DMs Owner."""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"ok": False, "error": "Invalid payload"})

        init_data = body.get("initData", "")
        bot_token = getattr(main_bot_module, "BOT_TOKEN", "")
        ptb_app = getattr(main_bot_module, "ptb_app", None)

        verified_user = verify_telegram_webapp_data(init_data, bot_token)
        if not verified_user:
            return JSONResponse(status_code=403, content={"ok": False, "error": "Unauthorized"})

        tg_id = int(verified_user["id"])
        _, has_test = await check_user_access_ram_db(tg_id)
        if not has_test and tg_id != OWNER_ID:
            return JSONResponse(status_code=403, content={"ok": False, "error": "Assessment access expired or ended."})

        answers = body.get("answers") or []
        q_times = body.get("question_times") or [0, 0, 0, 0]
        if len(answers) != 4 or any(not str(a).strip() for a in answers):
            return JSONResponse(status_code=400, content={"ok": False, "error": "All 4 answers are required."})

        clean_answers = [str(a).strip() for a in answers]
        clean_times = [round(float(t or 0), 1) for t in q_times[:4]]
        total_time = round(sum(clean_times), 1)

        try:
            eval_res = await evaluate_user_assessment_vectors(clean_answers)
        except Exception as e:
            logger.error(f"Vector scoring error: {e}")
            return JSONResponse(status_code=500, content={"ok": False, "error": "Scoring engine busy. Please retry."})

        now_str = datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")
        pool = await get_active_pg_pool()
        attempts = []

        if pool:
            async with pool.acquire() as conn:
                row = await conn.fetchrow("SELECT attempts_history FROM superadmin_test_portal WHERE user_id = $1;", tg_id)
                if row and row["attempts_history"]:
                    raw_h = row["attempts_history"]
                    attempts = json.loads(raw_h) if isinstance(raw_h, str) else list(raw_h)

                attempt_obj = {
                    "attempt_no": len(attempts) + 1,
                    "timestamp": now_str,
                    "overall_score": eval_res["overall_score"],
                    "categories": eval_res["categories"],
                    "total_time_sec": total_time,
                    "question_times": clean_times,
                    "answers": clean_answers
                }
                attempts.append(attempt_obj)

                await conn.execute("""
                    UPDATE superadmin_test_portal
                    SET attempts_history = $1::jsonb, updated_at = NOW()
                    WHERE user_id = $2;
                """, json.dumps(attempts), tg_id)
        else:
            attempt_obj = {
                "attempt_no": 1,
                "timestamp": now_str,
                "overall_score": eval_res["overall_score"],
                "categories": eval_res["categories"],
                "total_time_sec": total_time,
                "question_times": clean_times,
                "answers": clean_answers
            }
            attempts = [attempt_obj]

        # Send Complete Final Report to OWNER_ID in DM
        if ptb_app:
            full_name = f"{verified_user.get('first_name', '')} {verified_user.get('last_name', '')}".strip()
            username = verified_user.get("username") or ""
            uname_str = f"@{html.escape(username)}" if username else "N/A"
            cats = eval_res["categories"]

            report_msg = (
                f"🏆 <b>FINAL ASSESSMENT SUBMITTED (ATTEMPT #{attempt_obj['attempt_no']})</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>Candidate:</b> {html.escape(full_name)} ({uname_str})\n"
                f"🆔 <b>User ID:</b> <code>{tg_id}</code>\n"
                f"🎯 <b>FINAL OVERALL SCORE:</b> <b><code>{eval_res['overall_score']} / 100</code></b>\n"
                f"⏱️ <b>Total Time Taken:</b> <code>{total_time}s</code> "
                f"<i>(Q1: {clean_times[0]}s, Q2: {clean_times[1]}s, Q3: {clean_times[2]}s, Q4: {clean_times[3]}s)</i>\n\n"
                f"📊 <b>CATEGORY BREAKDOWN (OUT OF 100):</b>\n"
                f"• <b>Personality:</b> <code>{cats['personality']}</code>\n"
                f"• <b>Intelligence:</b> <code>{cats['intelligence']}</code>\n"
                f"• <b>Smartness:</b> <code>{cats['smartness']}</code>\n"
                f"• <b>Decision Making:</b> <code>{cats['decision_making']}</code>\n"
                f"• <b>Leadership:</b> <code>{cats['leadership']}</code>\n"
                f"• <b>Public Speaking:</b> <code>{cats['public_speaking']}</code>\n\n"
                f"📝 <b>Q1 ({clean_times[0]}s):</b> <blockquote>{html.escape(clean_answers[0][:700])}</blockquote>\n"
                f"📝 <b>Q2 ({clean_times[1]}s):</b> <blockquote>{html.escape(clean_answers[1][:700])}</blockquote>\n"
                f"📝 <b>Q3 ({clean_times[2]}s):</b> <blockquote>{html.escape(clean_answers[2][:700])}</blockquote>\n"
                f"📝 <b>Q4 ({clean_times[3]}s):</b> <blockquote>{html.escape(clean_answers[3][:700])}</blockquote>"
            )
            asyncio.create_task(
                ptb_app.bot.send_message(chat_id=OWNER_ID, text=report_msg, parse_mode=ParseMode.HTML)
            )

        return JSONResponse(content={
            "ok": True,
            "result": attempt_obj,
            "attempts": attempts
        })
