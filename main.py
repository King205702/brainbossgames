"""
Brain Boss — backend (Flask API + Telegram bot), designed to run as a
single long-running process on Render (NOT Netlify — Netlify can't host
persistent processes; see chat notes).

Responsibilities:
  1. Telegram bot: receives MoMo payment screenshots, OCRs them, validates
     the payment, and issues single-use 6-digit access codes.
  2. Flask API: lets the Netlify frontend verify a code (issuing a signed,
     short-lived play_token) and submit a final answer (burning the code
     atomically and recording winners).

Run locally:
    python main.py

Environment variables (set these in Render's dashboard, or a local .env):
    TELEGRAM_BOT_TOKEN     - from @BotFather
    OCR_SPACE_API_KEY      - from ocr.space
    FRONTEND_URL           - your Netlify site, e.g. https://brainboss.netlify.app
    SECRET_KEY             - any long random string, used to sign play_tokens
    ADMIN_USER_IDS         - comma-separated Telegram user IDs allowed to confirm payouts
    ANNOUNCEMENT_CHANNEL_ID - your channel's chat ID (bot must be an admin there)
    PORT                    - provided automatically by Render
"""

import os
import re
import io
import json
import time
import random
import sqlite3
import logging
import threading
import contextlib
from datetime import datetime, timezone, timedelta

import requests
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from gtts import gTTS

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# -----------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("brain_boss")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OCR_SPACE_API_KEY = os.environ.get("OCR_SPACE_API_KEY", "helloworld")  # 'helloworld' = OCR.space's public demo key, rate-limited — replace it
FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://your-site.netlify.app")
SECRET_KEY = os.environ.get("SECRET_KEY", "")
PORT = int(os.environ.get("PORT", 5000))

# Comma-separated Telegram user IDs allowed to confirm payouts, e.g. "111111,222222"
# Find your own numeric ID by messaging @userinfobot on Telegram.
ADMIN_USER_IDS = {
    int(part.strip())
    for part in os.environ.get("ADMIN_USER_IDS", "").split(",")
    if part.strip().isdigit()
}

# Your public announcement channel's chat ID (looks like -100xxxxxxxxxx).
# The bot must be added as an ADMIN of that channel to post there.
ANNOUNCEMENT_CHANNEL_ID = os.environ.get("ANNOUNCEMENT_CHANNEL_ID", "").strip() or None

# Your bot's @username (no @), used to build t.me deep links for referrals.
TELEGRAM_BOT_USERNAME = os.environ.get("TELEGRAM_BOT_USERNAME", "").strip()

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is required.")
if not SECRET_KEY:
    raise RuntimeError(
        "SECRET_KEY environment variable is required (used to sign play_tokens). "
        "Set it to any long random string in Render's dashboard."
    )

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brain_boss.db")
PLAY_TOKEN_MAX_AGE_SECONDS = 10 * 60  # play_token expires 10 minutes after verify-code
ENTRY_FEE_GHS = 5

# Reserved codes for YOU to test the game for free, any time, without paying
# or going through the bot. They never touch the payments database, never
# count toward round-winning or the jackpot, and are excluded from the pool
# of codes randomly generated for real players. Override via env var if
# you'd rather pick your own.
TEST_ACCESS_CODES = set(
    (os.environ.get("TEST_ACCESS_CODES", "") or "900001,900002,900003,900004,900005").split(",")
)

serializer = URLSafeTimedSerializer(SECRET_KEY, salt="brain-boss-play-token")

# Single global lock guarding multi-step DB transactions (issue token / burn
# code). SQLite handles concurrent connections fine for reads, but we still
# want these specific read-then-write sequences to be atomic across threads
# (the Flask dev server and the Telegram bot both touch the same file).
db_lock = threading.Lock()


def _telegram_api_post(method: str, data: dict):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}", data=data, timeout=15)
    except Exception:
        logger.exception("Telegram API call failed: %s", method)


def send_channel_message(text=None, photo_file_id=None, caption=None):
    """
    Posts to ANNOUNCEMENT_CHANNEL_ID using Telegram's raw HTTP API rather
    than the (async) bot library, so this can be called from Flask's
    synchronous request-handling code as easily as from the bot's async
    handlers. No-ops quietly if the channel isn't configured.
    """
    if not ANNOUNCEMENT_CHANNEL_ID:
        logger.info("ANNOUNCEMENT_CHANNEL_ID not set — skipping channel post.")
        return

    if photo_file_id:
        _telegram_api_post(
            "sendPhoto",
            {"chat_id": ANNOUNCEMENT_CHANNEL_ID, "photo": photo_file_id, "caption": caption or ""},
        )
    else:
        _telegram_api_post("sendMessage", {"chat_id": ANNOUNCEMENT_CHANNEL_ID, "text": text or ""})


def send_telegram_dm(chat_id: str, text: str):
    """Sends a direct message to a specific user — usable from Flask (sync)."""
    _telegram_api_post("sendMessage", {"chat_id": chat_id, "text": text})


# -----------------------------------------------------------------------
# Database
# -----------------------------------------------------------------------

def get_conn():
    # A fresh connection per call is the simplest thread-safe pattern for
    # SQLite when multiple threads (Flask + the bot) are involved.
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db():
    with contextlib.closing(get_conn()) as conn, conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS codes (
                code TEXT PRIMARY KEY,
                transaction_id TEXT UNIQUE NOT NULL,
                status TEXT NOT NULL DEFAULT 'valid',   -- 'valid' | 'burned'
                play_token TEXT,
                telegram_user_id TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                burned_at TEXT,
                module TEXT,                            -- puzzle module assigned to this code
                puzzle_json TEXT,                        -- safe (answer-free) puzzle data sent to the client
                correct_answer TEXT,                     -- never sent to the client
                puzzle_generated_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS winners (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                formula TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                username TEXT,
                winning_code TEXT,
                payment_details TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',   -- 'pending' | 'paid' | 'rejected'
                created_at TEXT DEFAULT (datetime('now')),
                paid_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS game_round (
                id INTEGER PRIMARY KEY CHECK (id = 1),  -- singleton row: only one active round ever
                status TEXT NOT NULL DEFAULT 'open',    -- 'open' | 'closed'
                winner_code TEXT,
                opened_at TEXT DEFAULT (datetime('now')),
                closed_at TEXT
            )
            """
        )
        conn.execute("INSERT OR IGNORE INTO game_round (id, status) VALUES (1, 'open')")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS discounts (
                code TEXT PRIMARY KEY,
                telegram_user_id TEXT NOT NULL,
                percent_off INTEGER NOT NULL DEFAULT 50,
                source TEXT,                              -- why it was granted, e.g. 'runner_up'
                status TEXT NOT NULL DEFAULT 'unused',     -- 'unused' | 'used'
                created_at TEXT DEFAULT (datetime('now')),
                used_at TEXT
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS payment_references (
                reference TEXT PRIMARY KEY,               -- e.g. "BB4821"
                telegram_user_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'unused',     -- 'unused' | 'used'
                created_at TEXT DEFAULT (datetime('now')),
                used_at TEXT
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS referral_codes (
                telegram_user_id TEXT PRIMARY KEY,
                referral_code TEXT UNIQUE NOT NULL,        -- e.g. "REF4821"
                created_at TEXT DEFAULT (datetime('now'))
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS referral_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referral_code TEXT NOT NULL,
                referred_user_id TEXT UNIQUE NOT NULL,     -- a user can only ever be referred once
                status TEXT NOT NULL DEFAULT 'pending',    -- 'pending' | 'completed'
                created_at TEXT DEFAULT (datetime('now')),
                completed_at TEXT
            )
            """
        )

        # Migration for DBs created before paid_at existed on withdrawals.
        try:
            conn.execute("ALTER TABLE withdrawals ADD COLUMN paid_at TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists

        # Migration for DBs created before server-side puzzle state existed.
        for column in ["module", "puzzle_json", "correct_answer", "puzzle_generated_at"]:
            try:
                conn.execute(f"ALTER TABLE codes ADD COLUMN {column} TEXT")
            except sqlite3.OperationalError:
                pass  # column already exists
    logger.info("Database ready at %s", DB_PATH)


def generate_unique_code(conn) -> str:
    for _ in range(50):
        candidate = f"{random.randint(0, 999999):06d}"
        if candidate in TEST_ACCESS_CODES:
            continue
        row = conn.execute("SELECT 1 FROM codes WHERE code = ?", (candidate,)).fetchone()
        if not row:
            return candidate
    raise RuntimeError("Could not generate a unique 6-digit code after 50 attempts.")


def generate_discount_code(conn) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I to avoid confusion
    for _ in range(50):
        candidate = "SAVE-" + "".join(random.choice(alphabet) for _ in range(6))
        row = conn.execute("SELECT 1 FROM discounts WHERE code = ?", (candidate,)).fetchone()
        if not row:
            return candidate
    raise RuntimeError("Could not generate a unique discount code after 50 attempts.")


def generate_reference_code(conn) -> str:
    """
    Short, all-numeric-suffix reference a player types into the MoMo
    'Reference'/'Note' field when paying. Digits-only suffix maximizes OCR
    reliability when we read it back off the receipt.
    """
    for _ in range(50):
        candidate = "BB" + f"{random.randint(0, 9999):04d}"
        row = conn.execute("SELECT 1 FROM payment_references WHERE reference = ?", (candidate,)).fetchone()
        if not row:
            return candidate
    raise RuntimeError("Could not generate a unique reference code after 50 attempts.")


def get_or_create_reference(conn, telegram_user_id: str) -> str:
    """Reuses an existing unused reference for this user if they have one
    (e.g. they tapped Buy Game Ticket twice), otherwise issues a new one."""
    row = conn.execute(
        "SELECT reference FROM payment_references WHERE telegram_user_id = ? AND status = 'unused' "
        "ORDER BY created_at DESC LIMIT 1",
        (telegram_user_id,),
    ).fetchone()
    if row:
        return row["reference"]

    reference = generate_reference_code(conn)
    conn.execute(
        "INSERT INTO payment_references (reference, telegram_user_id, status) VALUES (?, ?, 'unused')",
        (reference, telegram_user_id),
    )
    return reference


def generate_referral_code(conn) -> str:
    for _ in range(50):
        candidate = "REF" + f"{random.randint(0, 9999):04d}"
        row = conn.execute("SELECT 1 FROM referral_codes WHERE referral_code = ?", (candidate,)).fetchone()
        if not row:
            return candidate
    raise RuntimeError("Could not generate a unique referral code after 50 attempts.")


def get_or_create_referral_code(conn, telegram_user_id: str) -> str:
    row = conn.execute(
        "SELECT referral_code FROM referral_codes WHERE telegram_user_id = ?", (telegram_user_id,)
    ).fetchone()
    if row:
        return row["referral_code"]

    referral_code = generate_referral_code(conn)
    conn.execute(
        "INSERT INTO referral_codes (telegram_user_id, referral_code) VALUES (?, ?)",
        (telegram_user_id, referral_code),
    )
    return referral_code


def build_referral_link(referral_code: str) -> str:
    if not TELEGRAM_BOT_USERNAME:
        return "(set TELEGRAM_BOT_USERNAME to enable referral links)"
    return f"https://t.me/{TELEGRAM_BOT_USERNAME}?start={referral_code}"


# -----------------------------------------------------------------------
# OCR + payment validation
# -----------------------------------------------------------------------

def ocr_image(image_bytes: bytes) -> str:
    """Send image bytes to OCR.space and return the extracted text."""
    response = requests.post(
        "https://api.ocr.space/parse/image",
        files={"file": ("receipt.jpg", image_bytes)},
        data={
            "apikey": OCR_SPACE_API_KEY,
            "language": "eng",
            "isOverlayRequired": False,
            "OCREngine": 2,
            "scale": True,
        },
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()

    if data.get("IsErroredOnProcessing"):
        error_msg = data.get("ErrorMessage") or ["Unknown OCR error"]
        raise ValueError(f"OCR failed: {error_msg}")

    parsed_results = data.get("ParsedResults") or []
    if not parsed_results:
        raise ValueError("OCR returned no parsed results.")

    return parsed_results[0].get("ParsedText", "")


# -----------------------------------------------------------------------
# Recipient identity — the payment must have been sent to YOU specifically,
# not just any 5 GHS payment to anyone. Update these if your MoMo details
# change.
# -----------------------------------------------------------------------
RECIPIENT_NAME_TOKENS = ["PRINCE", "KWABENA", "BOATENG"]
RECIPIENT_NUMBER = "0205499441"  # local Ghana format, leading 0


def check_recipient(text: str) -> bool:
    """
    Confirms the receipt shows a payment sent to the configured recipient.
    Numbers are matched digit-only (ignores spaces/dashes/country-code
    formatting differences between providers). Name is matched leniently
    (at least 2 of 3 tokens) since OCR frequently mangles a letter or two.
    """
    digits_only = re.sub(r"\D", "", text)
    local9 = RECIPIENT_NUMBER[1:]  # drop leading 0 -> "205499441"
    number_match = (
        RECIPIENT_NUMBER in digits_only
        or ("233" + local9) in digits_only
        or local9 in digits_only
    )

    upper_text = text.upper()
    name_hits = sum(1 for token in RECIPIENT_NAME_TOKENS if token in upper_text)
    name_match = name_hits >= 2

    return number_match or name_match


# Amount patterns we accept: "5.00", "5.0", "5 GHS", "GHS 5", "GHS 5.00" etc.
FULL_AMOUNT_PATTERN = re.compile(
    r"(?:\bGHS\s*5(?:\.0{1,2})?\b)|(?:\b5(?:\.0{1,2})?\s*GHS\b)|(?:\b5\.00\b)|(?:\b5\.0\b)",
    re.IGNORECASE,
)

# Discounted entry (50% off), for runner-up players redeeming a discount code.
HALF_AMOUNT_PATTERN = re.compile(
    r"(?:\bGHS\s*2\.50\b)|(?:\b2\.50\s*GHS\b)|(?:\bGHS\s*2\.5\b)|(?:\b2\.5\s*GHS\b)|(?:\b2\.50\b)",
    re.IGNORECASE,
)

# Different providers word a completed transfer differently. This list is
# based on real screenshots tested — MTN and AirtelTigo may still phrase
# things differently, so widen it further as you see more real receipts.
SUCCESS_PATTERN = re.compile(
    r"\b(success(?:ful)?|completed|confirmed|you\s+have\s+sent|you\s+have\s+received|"
    r"payment\s+received|transaction\s+successful)\b",
    re.IGNORECASE,
)

# Guards against a *pending* or *requested* transaction being mistaken for
# a completed one (we saw exactly this in testing — a bundle-purchase
# request screenshot that mentioned "confirmed" but was actually pending).
PENDING_PATTERN = re.compile(
    r"\b(pending|kindly\s+wait|processing|awaiting\s+confirmation|request\s+received)\b",
    re.IGNORECASE,
)

# Transaction / reference ID patterns. MoMo providers format these
# differently — these cover common Ghanaian MTN/Telecel/AirtelTigo phrasing.
# ADJUST THESE to match real screenshots from your provider before going live.
TXN_ID_PATTERNS = [
    re.compile(r"(?:trans(?:action)?\.?\s*id)[:\s]*([A-Za-z0-9\-\.]{6,25})", re.IGNORECASE),
    re.compile(r"(?:financial\s*trans(?:action)?\.?\s*id)[:\s]*([A-Za-z0-9\-\.]{6,25})", re.IGNORECASE),
    re.compile(r"(?:ref(?:erence)?\.?\s*(?:no\.?|number)?)[:\s]*([A-Za-z0-9\-\.]{6,25})", re.IGNORECASE),
    re.compile(r"(?:external\s*(?:trans(?:action)?)?\.?\s*id)[:\s]*([A-Za-z0-9\-\.]{6,25})", re.IGNORECASE),
    # Fallback: some providers (e.g. Telecel Cash) print a bare long digit
    # string with no label at all, e.g. "0000013751921434 confirmed."
    re.compile(r"\b(\d{10,20})\b"),
]

# Our own player-specific reference codes (e.g. "BB4821"), which we ask
# players to put in the MoMo "Reference"/"Note" field when paying. If one
# is found on the receipt, it's a much stronger signal than fuzzy name/number
# matching — so it's checked (against the DB, by the caller) instead of the
# recipient check, not in addition to it. Tolerant of OCR/typing variations
# like "BB 4821" or "BB-4821".
REFERENCE_PATTERN = re.compile(r"\bBB[\s\-]?(\d{4})\b", re.IGNORECASE)


def validate_receipt_text(text: str):
    """
    Returns (is_valid, transaction_id, reason, amount_tier, reference_code)
    amount_tier is "full" (5 GHS) or "half" (2.50 GHS, requires a discount code
    to be honored — checked separately by the caller).
    reference_code is a "BBxxxx" string if found on the receipt, else None —
    the caller is responsible for verifying it against the database.
    """
    if PENDING_PATTERN.search(text):
        return False, None, "Transaction appears pending, not completed.", None, None

    if not SUCCESS_PATTERN.search(text):
        return False, None, "No success confirmation found on the receipt.", None, None

    if FULL_AMOUNT_PATTERN.search(text):
        amount_tier = "full"
    elif HALF_AMOUNT_PATTERN.search(text):
        amount_tier = "half"
    else:
        return (
            False, None,
            "Could not confirm the payment amount (5 GHS, or 2.50 GHS with a discount code).",
            None, None,
        )

    ref_match = REFERENCE_PATTERN.search(text)
    reference_code = f"BB{ref_match.group(1)}" if ref_match else None

    # Only fall back to fuzzy recipient matching if no reference code was
    # found at all — a found reference code gets verified against the DB
    # by the caller instead, which is a much more reliable signal.
    if not reference_code and not check_recipient(text):
        return False, None, "Payment was not sent to the correct recipient.", None, None

    txn_id = None
    for pattern in TXN_ID_PATTERNS:
        match = pattern.search(text)
        if match:
            txn_id = match.group(1).strip()
            break

    if not txn_id:
        return False, None, "Could not find a transaction ID on the receipt.", None, None

    return True, txn_id, "ok", amount_tier, reference_code


# -----------------------------------------------------------------------
# VAULT ESCAPE — server-authoritative puzzle generation & answer checking
#
# The browser NEVER decides whether a puzzle was solved correctly. It only
# renders whatever puzzle_json this module hands it, and reports back the
# player's raw answer (which button they tapped, what they typed). Every
# puzzle instance and its correct answer is generated here and stored
# server-side (in the `codes` row for real games, or signed into the
# play_token for test games) before the client ever sees it.
# -----------------------------------------------------------------------

MODULE_NAMES = ["sequence_trap", "detective", "spot_glitch", "audio_cipher"]


def generate_sequence_trap():
    use_multiply = random.random() < 0.5
    k = random.randint(2, 3) if use_multiply else random.randint(3, 8)
    start = random.randint(1, 6)
    terms = [start]
    for _ in range(3):
        start = start * k if use_multiply else start + k
        terms.append(start)

    correct = terms[3] * k if use_multiply else terms[3] + k
    # The trap: the answer you'd get by mistaking the rule for the OTHER
    # operation — the "90% get it wrong" answer.
    trap = terms[3] + k if use_multiply else terms[3] * k
    distractor = correct + random.choice([-1, 1]) * random.randint(1, 3)

    options = list(dict.fromkeys([correct, trap, distractor, correct + k]))
    while len(options) < 4:
        candidate = correct + random.randint(-10, 10)
        if candidate not in options:
            options.append(candidate)
    options = options[:4]
    random.shuffle(options)

    return {"terms": terms, "options": options}, str(correct)


DETECTIVE_SCENARIOS = [
    {
        "brief": "The vault's emergency cash box vanished at 9:14 PM. Only three guards were on that floor.",
        "suspects": [
            {"name": "Marcus", "avatar": "🕴️", "alibi": "\"I was reviewing the security footage in the control room the whole time.\"", "guilty": False},
            {"name": "Priscilla", "avatar": "👩‍💼", "alibi": "\"I was on my dinner break outside — I only came back at 9:20.\"", "guilty": False},
            {"name": "Derek", "avatar": "🧑‍🔧", "alibi": "\"I was fixing the vault camera wiring — that's why there's no footage from 9:10 to 9:16.\"", "guilty": True},
        ],
    },
    {
        "brief": "A guest's diamond ring disappeared from suite 4B between 6 and 7 PM. Housekeeping had three staff on that floor.",
        "suspects": [
            {"name": "Ama", "avatar": "🧹", "alibi": "\"I cleaned 4B at 5:45, then went straight to 4C — the guest there can confirm.\"", "guilty": False},
            {"name": "Kwesi", "avatar": "🧑‍🍳", "alibi": "\"I was restocking the minibar in 4B until 6:50 — I left right as the guest returned.\"", "guilty": True},
            {"name": "Efua", "avatar": "🧺", "alibi": "\"I was on laundry duty in the basement all evening.\"", "guilty": False},
        ],
    },
    {
        "brief": "Someone tampered with the poker table's shuffler at exactly 11 PM. Three staff had access to the pit.",
        "suspects": [
            {"name": "Yaw", "avatar": "🎲", "alibi": "\"I was on my break in the staff room — camera shows me there from 10:50.\"", "guilty": False},
            {"name": "Naomi", "avatar": "💼", "alibi": "\"I was auditing the cash drawer at the far end of the pit the entire time.\"", "guilty": False},
            {"name": "Kojo", "avatar": "🃏", "alibi": "\"I was resetting the shuffler for maintenance — but maintenance wasn't scheduled until tomorrow.\"", "guilty": True},
        ],
    },
]


def generate_detective():
    scenario = random.choice(DETECTIVE_SCENARIOS)
    suspects = [{"name": s["name"], "avatar": s["avatar"], "alibi": s["alibi"]} for s in scenario["suspects"]]
    guilty = next(s["name"] for s in scenario["suspects"] if s["guilty"])
    return {"brief": scenario["brief"], "suspects": suspects}, guilty


GLITCH_SCENES = [
    {"theme": "an old market square", "normal": "🏺", "anomaly": "📱"},
    {"theme": "a medieval camp", "normal": "⚔️", "anomaly": "🔋"},
    {"theme": "an ancient temple", "normal": "🏛️", "anomaly": "💡"},
    {"theme": "an 1800s harbor", "normal": "⛵", "anomaly": "🚀"},
]


def generate_spot_glitch():
    scene = random.choice(GLITCH_SCENES)
    size = 15
    anomaly_index = random.randint(0, size - 1)
    grid = [scene["anomaly"] if i == anomaly_index else scene["normal"] for i in range(size)]
    return {"theme": scene["theme"], "grid": grid}, str(anomaly_index)


CIPHER_WORDS = ["VAULT", "ESCAPE", "SHADOW", "GOLDEN", "CIPHER", "BREACH"]


def generate_audio_cipher():
    word = random.choice(CIPHER_WORDS)
    # Nothing revealed here — the client fetches synthesized audio for this
    # word from a separate endpoint that never returns the text itself.
    return {}, word


def generate_puzzle_for_module(module: str):
    if module == "sequence_trap":
        return generate_sequence_trap()
    if module == "detective":
        return generate_detective()
    if module == "spot_glitch":
        return generate_spot_glitch()
    if module == "audio_cipher":
        return generate_audio_cipher()
    raise ValueError(f"Unknown module: {module}")


def check_answer(module: str, correct_answer: str, submitted_answer: str) -> bool:
    submitted = str(submitted_answer or "").strip()
    correct = str(correct_answer or "").strip()
    if module == "detective":
        return submitted.lower() == correct.lower()
    if module == "audio_cipher":
        return submitted.upper() == correct.upper()
    return submitted == correct  # sequence_trap, spot_glitch: exact string match


# -----------------------------------------------------------------------
# Telegram bot handlers
# -----------------------------------------------------------------------

RECIPIENT_DISPLAY_NAME = " ".join(t.title() for t in RECIPIENT_NAME_TOKENS)

def build_rules_text(reference: str) -> str:
    return f"""🧠 WELCOME TO BRAIN BOSS ARENA 🏆
Here are the official rules for today's matchstick challenge:

1️⃣ ENTRY FEE & PAYMENT:
- The entry fee is exactly 5 GHS.
- Send payment to: {RECIPIENT_NUMBER} ({RECIPIENT_DISPLAY_NAME}).
- IMPORTANT: Enter this exact reference in the MoMo "Reference" or "Note" field when you pay: {reference}
- Upload your clean screenshot receipt here afterward. Our automated system reads MTN, Telecel, and AT networks. (Note: Data bundle or airtime purchase screenshots will be automatically rejected).

2️⃣ THE CHALLENGE WEBSITE:
- Once verified, you will receive a unique 6-digit access code and the game link.
- Enter your code to unlock the puzzle arena.
- You must move exactly ONE matchstick to fix the equation.

3️⃣ STRICT ONE-TRY LIMIT:
- You only get ONE ATTEMPT to submit your answer on the website.
- The moment you press "Submit", your 6-digit code is permanently burned in our database, and you will be locked out. No second chances!

4️⃣ WINNER TAKES ALL:
- Only ONE person can win per round — whoever is the FIRST to submit the correct answer.
- If someone else beats you to it, your code is still burned (no refund), but you won't be marked as the winner — even if your answer was also correct.
- Once someone wins, the round closes. Watch this channel for the payout proof and the announcement of the next round.

Your payment reference is {reference} — don't forget to include it! Send your screenshot now and race to be first!"""

# Telegram user_ids currently expected to reply with withdrawal details next.
# Maps user_id -> the winning_code their withdrawal request will reference.
# NOTE: this is in-memory only — it resets if the process restarts. Fine for
# a short "click button, then reply" flow, but if someone clicks Withdraw
# and the service happens to redeploy before they answer, they'd need to
# click Withdraw again. Move this to the DB if that becomes a problem.
pending_withdrawals = {}

ADMIN_PAID_PATTERN = re.compile(r"^\s*PAID\s+(\d{6})\s*$", re.IGNORECASE)


def mask_payment_details(text: str) -> str:
    """Redacts long digit runs (phone numbers) in text meant for public
    posting, keeping only the last 3 digits visible, e.g. 0205499441 ->
    *******441. Leaves short numbers and names untouched."""

    def _mask(match):
        digits = match.group(0)
        if len(digits) <= 3:
            return digits
        return "*" * (len(digits) - 3) + digits[-3:]

    return re.sub(r"\d{6,}", _mask, text)


def get_unclaimed_winning_code(user_id: str):
    """Returns a winning code for this user that has no non-rejected
    withdrawal request against it yet, or None if they have no such win."""
    with contextlib.closing(get_conn()) as conn:
        rows = conn.execute(
            """
            SELECT winners.code AS code
            FROM winners
            JOIN codes ON codes.code = winners.code
            WHERE codes.telegram_user_id = ?
            ORDER BY winners.created_at ASC
            """,
            (user_id,),
        ).fetchall()

        for row in rows:
            claimed = conn.execute(
                "SELECT 1 FROM withdrawals WHERE winning_code = ? AND status != 'rejected'",
                (row["code"],),
            ).fetchone()
            if not claimed:
                return row["code"]

    return None


async def myid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        f"Your Telegram ID (as seen by this bot): {user.id}"
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # Deep-link referral capture: t.me/YourBot?start=REF4821 arrives here as
    # context.args == ["REF4821"]. Only counts once per user, ever, and never
    # for someone referring themselves.
    if context.args:
        referral_code = context.args[0].strip()
        with db_lock, contextlib.closing(get_conn()) as conn:
            owner = conn.execute(
                "SELECT telegram_user_id FROM referral_codes WHERE referral_code = ?", (referral_code,)
            ).fetchone()
            already_referred = conn.execute(
                "SELECT 1 FROM referral_events WHERE referred_user_id = ?", (str(user.id),)
            ).fetchone()

            if owner and owner["telegram_user_id"] != str(user.id) and not already_referred:
                with conn:
                    conn.execute(
                        "INSERT INTO referral_events (referral_code, referred_user_id, status) "
                        "VALUES (?, ?, 'pending')",
                        (referral_code, str(user.id)),
                    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎟️ Buy Game Ticket", callback_data="buy_ticket")],
        [InlineKeyboardButton("💰 Withdraw Winnings", callback_data="withdraw")],
        [InlineKeyboardButton("📢 My Referrals", callback_data="referrals")],
    ])
    await update.message.reply_text(
        "Welcome to Brain Boss! What would you like to do?",
        reply_markup=keyboard,
    )


async def handle_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user

    if query.data == "buy_ticket":
        with db_lock, contextlib.closing(get_conn()) as conn, conn:
            reference = get_or_create_reference(conn, str(user.id))
        await query.message.reply_text(build_rules_text(reference))
        return

    if query.data == "withdraw":
        # A user replying to answer a previous withdrawal request shouldn't
        # be able to start a second one on top of it.
        if user.id in pending_withdrawals:
            await query.message.reply_text(
                "I'm still waiting for your withdrawal details from your last request — "
                "please send your Mobile Money Number, Registered Name, and Network."
            )
            return

        winning_code = get_unclaimed_winning_code(str(user.id))

        if not winning_code:
            await query.message.reply_text(
                "❌ You currently do not have any pending winnings to withdraw. Keep playing to win!"
            )
            return

        pending_withdrawals[user.id] = {
            "username": user.username or user.full_name,
            "winning_code": winning_code,
        }
        await query.message.reply_text(
            "🏆 You have winnings available! Please reply with your:\n"
            "- Mobile Money Number\n"
            "- Registered Name\n"
            "- Network (MTN / Telecel / AT)\n\n"
            "Send all three in one message."
        )
        return

    if query.data == "referrals":
        with db_lock, contextlib.closing(get_conn()) as conn, conn:
            referral_code = get_or_create_referral_code(conn, str(user.id))
            completed_count = conn.execute(
                "SELECT COUNT(*) AS n FROM referral_events WHERE referral_code = ? AND status = 'completed'",
                (referral_code,),
            ).fetchone()["n"]

        link = build_referral_link(referral_code)
        slots_filled = completed_count % 3
        progress_bar = "".join("✅" if i < slots_filled else "❌" for i in range(3))
        share_message = (
            f"Yo! I'm trapped in this 5 GHS puzzle vault trying to win the mega jackpot. "
            f"Click my link to join and help me break out! 🔐👇\n{link}"
        )

        await query.message.reply_text(
            f"📢 Your referral link:\n{link}\n\n"
            f"Progress to your next free entry: [{progress_bar}]\n"
            f"({slots_filled}/3 — every 3 friends who join and pay their entry fee earns you a 50% discount code)\n\n"
            f"Share this message with friends:\n\n{share_message}"
        )
        return


async def handle_admin_payout_proof(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str):
    """Admin sent a photo captioned 'PAID <code>' — mark that withdrawal
    paid and repost the same photo to the announcement channel as proof."""
    message = update.message

    with db_lock, contextlib.closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT * FROM withdrawals WHERE winning_code = ? AND status = 'pending' "
            "ORDER BY created_at DESC LIMIT 1",
            (code,),
        ).fetchone()

        if not row:
            await message.reply_text(f"No pending withdrawal found for code {code}.")
            return

        with conn:
            conn.execute(
                "UPDATE withdrawals SET status = 'paid', paid_at = datetime('now') WHERE id = ?",
                (row["id"],),
            )

    masked_details = mask_payment_details(row["payment_details"])
    photo_file_id = message.photo[-1].file_id
    announcement = (
        "🏆 PAYOUT CONFIRMED!\n\n"
        f"Winning code {code} has been paid out via Mobile Money. ✅\n"
        f"Recipient: {masked_details}\n\n"
        "🔥 Congratulations! Watch this space for the next round."
    )

    if not ANNOUNCEMENT_CHANNEL_ID:
        await message.reply_text(
            "Marked as paid. (ANNOUNCEMENT_CHANNEL_ID isn't set, so nothing was posted publicly.)"
        )
        return

    try:
        await context.bot.send_photo(
            chat_id=ANNOUNCEMENT_CHANNEL_ID, photo=photo_file_id, caption=announcement
        )
        await message.reply_text("Posted payout proof to the channel ✅")
    except Exception:
        logger.exception("Failed to post payout proof to channel")
        await message.reply_text(
            "Marked as paid in the database, but posting to the channel failed — check the logs."
        )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user = update.effective_user

    # --- Admin branch: posting proof of a payout, not a payment screenshot ---
    if user.id in ADMIN_USER_IDS:
        caption = (message.caption or "").strip()
        admin_match = ADMIN_PAID_PATTERN.match(caption)
        if admin_match:
            await handle_admin_payout_proof(update, context, admin_match.group(1))
            return

    await message.reply_text("Got your screenshot — checking it now, one moment...")

    try:
        photo = message.photo[-1]  # highest resolution variant
        tg_file = await context.bot.get_file(photo.file_id)
        image_bytes = bytes(await tg_file.download_as_bytearray())
    except Exception:
        logger.exception("Failed to download photo from Telegram")
        await message.reply_text("Couldn't download that image — please try sending it again.")
        return

    try:
        parsed_text = ocr_image(image_bytes)
    except Exception as exc:
        logger.exception("OCR request failed")
        if user.id in ADMIN_USER_IDS:
            await message.reply_text(f"[DEBUG — admin only] OCR request failed:\n{exc}")
        else:
            await message.reply_text(
                "Payment screenshot not recognized. Please make sure the amount is 5 GHS and try again."
            )
        return

    is_valid, txn_id, reason, amount_tier, reference_code = validate_receipt_text(parsed_text)
    logger.info(
        "OCR validation for user %s: valid=%s reason=%s tier=%s ref=%s",
        user.id, is_valid, reason, amount_tier, reference_code,
    )

    if not is_valid:
        REASON_MESSAGES = {
            "Transaction appears pending, not completed.": (
                "That payment still looks pending, not completed. "
                "Send a screenshot once it shows as successful."
            ),
            "Payment was not sent to the correct recipient.": (
                f"That payment doesn't appear to be sent to {RECIPIENT_NAME_TOKENS[0].title()} "
                f"{RECIPIENT_NAME_TOKENS[1].title()} {RECIPIENT_NAME_TOKENS[2].title()} ({RECIPIENT_NUMBER}). "
                "Double-check the number and try again — or better, get a payment reference from "
                "'Buy Game Ticket' in the menu and include it next time."
            ),
        }

        # Admins get the real internal reason + raw OCR text so screenshots
        # can be debugged instantly from Telegram, without digging through
        # Render's logs each time.
        if user.id in ADMIN_USER_IDS:
            snippet = parsed_text.strip()[:800] or "(OCR returned empty text)"
            await message.reply_text(
                f"[DEBUG — admin only]\nreason: {reason}\namount_tier: {amount_tier}\nreference_code: {reference_code}\n\n"
                f"Raw OCR text:\n{snippet}"
            )
            return

        await message.reply_text(
            REASON_MESSAGES.get(
                reason,
                "Payment screenshot not recognized. Please make sure the amount is 5 GHS "
                "(or 2.50 GHS if you're redeeming a discount code) and try again.",
            )
        )
        return

    discount_row = None
    reference_row = None

    with db_lock, contextlib.closing(get_conn()) as conn:
        existing = conn.execute(
            "SELECT code FROM codes WHERE transaction_id = ?", (txn_id,)
        ).fetchone()

        if existing:
            await message.reply_text(
                "This payment has already been used to generate an access code. "
                "Check your earlier messages for it, or make a new payment."
            )
            return

        # A reference code found on the receipt must actually belong to this
        # user and be unused — otherwise it's not proof of anything.
        if reference_code:
            reference_row = conn.execute(
                "SELECT * FROM payment_references WHERE reference = ?", (reference_code,)
            ).fetchone()

            if (
                not reference_row
                or reference_row["status"] != "unused"
                or reference_row["telegram_user_id"] != str(user.id)
            ):
                await message.reply_text(
                    f"We found reference {reference_code} on this receipt, but it doesn't match "
                    "an active reference for your account. Tap 'Buy Game Ticket' in the menu to "
                    "get a fresh reference, then try again."
                )
                return

        # A half-price (2.50 GHS) payment only counts if this user actually
        # holds an unused discount code — otherwise it's just an underpayment.
        if amount_tier == "half":
            discount_row = conn.execute(
                "SELECT * FROM discounts WHERE telegram_user_id = ? AND status = 'unused' "
                "ORDER BY created_at ASC LIMIT 1",
                (str(user.id),),
            ).fetchone()

            if not discount_row:
                await message.reply_text(
                    "That looks like a discounted (2.50 GHS) payment, but we don't have an "
                    "active discount code on file for your account. Pay the full 5 GHS entry "
                    "fee, or double-check you're using the Telegram account your discount was issued to."
                )
                return

        with conn:
            is_first_ever_code = (
                conn.execute("SELECT COUNT(*) AS n FROM codes WHERE telegram_user_id = ?", (str(user.id),)).fetchone()["n"]
                == 0
            )

            code = generate_unique_code(conn)
            conn.execute(
                """
                INSERT INTO codes (code, transaction_id, status, telegram_user_id)
                VALUES (?, ?, 'valid', ?)
                """,
                (code, txn_id, str(user.id)),
            )
            if discount_row:
                conn.execute(
                    "UPDATE discounts SET status = 'used', used_at = datetime('now') WHERE code = ?",
                    (discount_row["code"],),
                )
            if reference_row:
                conn.execute(
                    "UPDATE payment_references SET status = 'used', used_at = datetime('now') WHERE reference = ?",
                    (reference_code,),
                )

            # Referral completion: only counts on this user's very first
            # ever paid entry, so someone can't rack up repeat "referrals"
            # for their own referrer by just paying again.
            referral_reward_code = None
            referral_owner_id = None
            if is_first_ever_code:
                pending_referral = conn.execute(
                    "SELECT * FROM referral_events WHERE referred_user_id = ? AND status = 'pending'",
                    (str(user.id),),
                ).fetchone()
                if pending_referral:
                    conn.execute(
                        "UPDATE referral_events SET status = 'completed', completed_at = datetime('now') "
                        "WHERE id = ?",
                        (pending_referral["id"],),
                    )
                    owner_row = conn.execute(
                        "SELECT telegram_user_id FROM referral_codes WHERE referral_code = ?",
                        (pending_referral["referral_code"],),
                    ).fetchone()
                    if owner_row:
                        completed_count = conn.execute(
                            "SELECT COUNT(*) AS n FROM referral_events WHERE referral_code = ? AND status = 'completed'",
                            (pending_referral["referral_code"],),
                        ).fetchone()["n"]
                        if completed_count % 3 == 0:
                            referral_owner_id = owner_row["telegram_user_id"]
                            referral_reward_code = generate_discount_code(conn)
                            conn.execute(
                                "INSERT INTO discounts (code, telegram_user_id, percent_off, source) "
                                "VALUES (?, ?, 50, 'referral_bonus')",
                                (referral_reward_code, referral_owner_id),
                            )

    if referral_reward_code and referral_owner_id:
        send_telegram_dm(
            referral_owner_id,
            f"🎉 3 friends joined through your referral link! Here's a 50% discount code "
            f"for your next entry:\n\n{referral_reward_code}\n\n"
            f"Pay 2.50 GHS instead of 5 GHS, and send the screenshot as usual — "
            f"we'll recognize the discount automatically.",
        )

    prefix = ""
    if discount_row:
        prefix += f"Discount applied! 🎉 ({discount_row['code']})\n\n"
    if reference_row:
        prefix += f"Reference {reference_code} matched ✅\n\n"

    await message.reply_text(
        f"{prefix}"
        f"Payment confirmed! ✅\n\n"
        f"Your access code: {code}\n\n"
        f"Play here: {FRONTEND_URL}\n\n"
        f"This code works once — save it now."
    )


async def handle_other_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if user.id in pending_withdrawals:
        details = pending_withdrawals.pop(user.id)
        payment_details = update.message.text.strip()

        with db_lock, contextlib.closing(get_conn()) as conn, conn:
            conn.execute(
                """
                INSERT INTO withdrawals (user_id, username, winning_code, payment_details, status)
                VALUES (?, ?, ?, ?, 'pending')
                """,
                (str(user.id), details["username"], details["winning_code"], payment_details),
            )

        await update.message.reply_text(
            "✅ Your withdrawal request has been received! Our admin team will process your payment shortly."
        )
        return

    await update.message.reply_text(
        "Send a screenshot of your successful 5 GHS Mobile Money payment to get your access code, "
        "or use /start to see the menu."
    )


async def newround_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_USER_IDS:
        return  # silently ignore non-admins

    with db_lock, contextlib.closing(get_conn()) as conn, conn:
        conn.execute(
            "UPDATE game_round SET status = 'open', winner_code = NULL, "
            "opened_at = datetime('now'), closed_at = NULL WHERE id = 1"
        )

    await update.message.reply_text("✅ New round is now open.")
    send_channel_message(
        text="🔥 A NEW ROUND HAS STARTED! Pay your 5 GHS entry fee and race to be the "
        "FIRST to solve today's matchstick riddle. Only one winner takes the prize — good luck!"
    )


def build_telegram_app() -> Application:
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("myid", myid_command))
    application.add_handler(CommandHandler("newround", newround_command))
    application.add_handler(CallbackQueryHandler(handle_menu_callback))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(~filters.PHOTO & ~filters.COMMAND, handle_other_messages))
    return application


def run_bot():
    """Runs the Telegram bot's polling loop in this (background) thread."""
    import asyncio

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    application = build_telegram_app()
    logger.info("Starting Telegram bot polling...")
    # stop_signals=None: signal handlers can only be installed on the main
    # thread, and this runs on a background thread.
    application.run_polling(stop_signals=None, close_loop=False)


# -----------------------------------------------------------------------
# Flask API
# -----------------------------------------------------------------------

app = Flask(__name__)
CORS(app, origins=[FRONTEND_URL] if FRONTEND_URL != "https://your-site.netlify.app" else "*")


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.post("/api/verify-code")
def verify_code():
    payload = request.get_json(silent=True) or {}
    code = str(payload.get("code", "")).strip()

    if not re.fullmatch(r"\d{6}", code):
        return jsonify({"valid": False, "message": "Enter a valid 6-digit code."}), 400

    # Admin test codes: always valid, never touch the payments DB, never
    # affect round-winning or the jackpot. Everything about the test session
    # (module + answer) is signed into the token itself since there's no
    # persistent `codes` row for these.
    if code in TEST_ACCESS_CODES:
        play_token = serializer.dumps({"code": code, "test": True})
        return jsonify({"valid": True, "play_token": play_token, "referral_link": None, "test": True})

    with db_lock, contextlib.closing(get_conn()) as conn:
        row = conn.execute("SELECT * FROM codes WHERE code = ?", (code,)).fetchone()

        if not row:
            return jsonify({"valid": False, "message": "Invalid or expired code."}), 404

        if row["status"] != "valid":
            return jsonify({"valid": False, "message": "This code has already been used."}), 410

        play_token = serializer.dumps({"code": code})
        referral_code = get_or_create_referral_code(conn, row["telegram_user_id"]) if row["telegram_user_id"] else None

        with conn:
            conn.execute(
                "UPDATE codes SET play_token = ? WHERE code = ?",
                (play_token, code),
            )

    referral_link = build_referral_link(referral_code) if referral_code else None
    return jsonify({"valid": True, "play_token": play_token, "referral_link": referral_link})


@app.get("/api/jackpot")
def jackpot():
    with contextlib.closing(get_conn()) as conn:
        total_codes = conn.execute("SELECT COUNT(*) AS n FROM codes").fetchone()["n"]
    return jsonify({"amount": total_codes * ENTRY_FEE_GHS, "currency": "GHS"})


@app.post("/api/start-game")
def start_game():
    """
    Generates (or re-serves, if already generated for this session) the
    puzzle instance the player will see — module type + safe puzzle_json,
    with the correct answer held server-side only. This is what makes
    answer-checking tamper-proof: the browser never independently decides
    what "correct" means.
    """
    payload = request.get_json(silent=True) or {}
    code = str(payload.get("code", "")).strip()
    play_token = str(payload.get("play_token", "")).strip()

    if not re.fullmatch(r"\d{6}", code) or not play_token:
        return jsonify({"success": False, "message": "Missing or malformed request."}), 400

    try:
        token_data = serializer.loads(play_token, max_age=PLAY_TOKEN_MAX_AGE_SECONDS)
    except (SignatureExpired, BadSignature):
        return jsonify({"success": False, "message": "Invalid or expired session. Re-enter your code."}), 401

    if token_data.get("code") != code:
        return jsonify({"success": False, "message": "Token does not match code."}), 401

    is_test = bool(token_data.get("test"))

    if is_test:
        # No DB row for test sessions — generate fresh each time and sign
        # the answer into a new token, which the client will use to submit.
        module = random.choice(MODULE_NAMES)
        puzzle_json, correct_answer = generate_puzzle_for_module(module)
        new_token = serializer.dumps({"code": code, "test": True, "module": module, "answer": correct_answer})
        return jsonify({"success": True, "module": module, "puzzle": puzzle_json, "play_token": new_token})

    with db_lock, contextlib.closing(get_conn()) as conn:
        row = conn.execute("SELECT * FROM codes WHERE code = ?", (code,)).fetchone()

        if not row:
            return jsonify({"success": False, "message": "Invalid code."}), 404
        if row["play_token"] != play_token:
            return jsonify({"success": False, "message": "This session is no longer valid."}), 401
        if row["status"] != "valid":
            return jsonify({"success": False, "message": "This code has already been used."}), 410

        # Idempotent: if a puzzle was already generated for this code (e.g.
        # the page was refreshed), return the SAME one rather than a fresh
        # random pick — otherwise someone could "reroll" for an easier puzzle.
        if row["module"] and row["puzzle_json"]:
            return jsonify({"success": True, "module": row["module"], "puzzle": json.loads(row["puzzle_json"])})

        module = random.choice(MODULE_NAMES)
        puzzle_json, correct_answer = generate_puzzle_for_module(module)

        with conn:
            conn.execute(
                "UPDATE codes SET module = ?, puzzle_json = ?, correct_answer = ?, "
                "puzzle_generated_at = datetime('now') WHERE code = ?",
                (module, json.dumps(puzzle_json), correct_answer, code),
            )

    return jsonify({"success": True, "module": module, "puzzle": puzzle_json})


@app.get("/api/cipher-audio/<code>")
def cipher_audio(code):
    """
    Streams synthesized speech of the audio-cipher's secret word. The word
    text itself is never sent to the client in any other form — only this
    audio. Works for both real codes (answer stored in DB) and test codes
    (answer signed into the token, passed as a query param since this is a
    simple GET request).
    """
    code = str(code).strip()

    if code in TEST_ACCESS_CODES:
        token = request.args.get("play_token", "")
        try:
            token_data = serializer.loads(token, max_age=PLAY_TOKEN_MAX_AGE_SECONDS)
        except (SignatureExpired, BadSignature):
            return jsonify({"message": "Invalid session."}), 401
        if token_data.get("code") != code or token_data.get("module") != "audio_cipher":
            return jsonify({"message": "No active cipher for this session."}), 400
        word = token_data.get("answer", "")
    else:
        with contextlib.closing(get_conn()) as conn:
            row = conn.execute("SELECT module, correct_answer FROM codes WHERE code = ?", (code,)).fetchone()
        if not row or row["module"] != "audio_cipher":
            return jsonify({"message": "No active cipher for this code."}), 400
        word = row["correct_answer"]

    if not word:
        return jsonify({"message": "No cipher word available."}), 400

    try:
        buffer = io.BytesIO()
        gTTS(text=word, lang="en", slow=False).write_to_fp(buffer)
        buffer.seek(0)
        return Response(buffer.read(), mimetype="audio/mpeg")
    except Exception:
        logger.exception("Cipher audio generation failed")
        return jsonify({"message": "Audio generation failed."}), 500


@app.post("/api/submit-answer")
def submit_answer():
    payload = request.get_json(silent=True) or {}
    code = str(payload.get("code", "")).strip()
    play_token = str(payload.get("play_token", "")).strip()
    answer = str(payload.get("answer", "")).strip()
    time_taken = payload.get("time_taken")

    if not re.fullmatch(r"\d{6}", code) or not play_token:
        return jsonify({"success": False, "message": "Missing or malformed request."}), 400

    try:
        token_data = serializer.loads(play_token, max_age=PLAY_TOKEN_MAX_AGE_SECONDS)
    except SignatureExpired:
        return jsonify({"success": False, "message": "Your session expired. Re-enter your code."}), 401
    except BadSignature:
        return jsonify({"success": False, "message": "Invalid play token."}), 401

    if token_data.get("code") != code:
        return jsonify({"success": False, "message": "Token does not match code."}), 401

    is_test = bool(token_data.get("test"))

    # --- Test sessions: check answer, report result, touch nothing else. ---
    if is_test:
        module = token_data.get("module", "")
        correct_answer = token_data.get("answer", "")
        is_correct = check_answer(module, correct_answer, answer)
        message = (
            "🧪 [TEST MODE] Correct! (not counted toward any real round)"
            if is_correct
            else "🧪 [TEST MODE] Not correct. (not counted toward any real round)"
        )
        return jsonify({"success": True, "correct": is_correct, "won": False, "discount_code": None, "message": message, "test": True})

    with db_lock, contextlib.closing(get_conn()) as conn:
        row = conn.execute("SELECT * FROM codes WHERE code = ?", (code,)).fetchone()

        if not row:
            return jsonify({"success": False, "message": "Invalid code."}), 404

        if row["play_token"] != play_token:
            return jsonify({"success": False, "message": "This session is no longer valid."}), 401

        if not row["module"] or row["correct_answer"] is None:
            return jsonify({"success": False, "message": "No active puzzle for this code — call start-game first."}), 400

        # Atomic burn: the WHERE status='valid' clause means only one
        # concurrent request can ever succeed here, even under a race.
        with conn:
            cursor = conn.execute(
                "UPDATE codes SET status = 'burned', burned_at = datetime('now') "
                "WHERE code = ? AND status = 'valid'",
                (code,),
            )

        if cursor.rowcount == 0:
            return jsonify({"success": False, "message": "This code has already been used."}), 410

        # The server checks the answer — never trusts a client-reported result.
        is_correct = check_answer(row["module"], row["correct_answer"], answer)
        won_round = False
        discount_code = None

        if is_correct:
            # Atomic race for "first correct answer wins": only the request
            # that actually flips the round from 'open' to 'closed' is the
            # winner, even if two people submit the right answer at the
            # same instant.
            with conn:
                round_cursor = conn.execute(
                    "UPDATE game_round SET status = 'closed', winner_code = ?, "
                    "closed_at = datetime('now') WHERE id = 1 AND status = 'open'",
                    (code,),
                )
            won_round = round_cursor.rowcount == 1

            if won_round:
                with conn:
                    conn.execute(
                        "INSERT INTO winners (code, formula) VALUES (?, ?)",
                        (code, f"module={row['module']} time={time_taken}"),
                    )
            else:
                # Correct, but beaten to it — an honest, transparent reward:
                # a flat 50% discount code for their next entry. This is
                # shown plainly as a discount, never as a partially-revealed
                # "almost won" prize.
                with conn:
                    discount_code = generate_discount_code(conn)
                    conn.execute(
                        "INSERT INTO discounts (code, telegram_user_id, percent_off, source) "
                        "VALUES (?, ?, 50, 'runner_up')",
                        (discount_code, row["telegram_user_id"]),
                    )

    if is_correct and won_round:
        message = "🏆 You escaped the vault — and you're the winner of this round! Check the bot menu to withdraw your winnings."
    elif is_correct and not won_round:
        message = (
            "You escaped the vault — but someone else got there first this round. "
            f"No prize this time, but here's 50% off your next entry: {discount_code}. "
            "Send it with your next payment screenshot to redeem it."
        )
    else:
        message = "🔒 TRAPPED IN THE VAULT — that wasn't it."

    if won_round:
        send_channel_message(
            text="🎉 WE HAVE A WINNER! Someone just broke out of today's Brain Boss vault "
            "first and locked in the prize. Payout proof coming soon — stay tuned!"
        )

    if discount_code and row["telegram_user_id"]:
        send_telegram_dm(
            row["telegram_user_id"],
            f"Nice work — you escaped the vault! Someone just beat you to it this round, "
            f"so no prize this time. But here's a 50% discount for your next entry:\n\n"
            f"{discount_code}\n\n"
            f"Pay 2.50 GHS instead of 5 GHS next time and send the screenshot as usual — "
            f"we'll recognize the discount automatically.",
        )

    return jsonify({
        "success": True,
        "correct": is_correct,
        "won": won_round,
        "discount_code": discount_code,
        "message": message,
    })


def run_daily_reset_loop():
    """
    Background loop: once every 24 hours, automatically reopens the game
    round (same effect as the admin /newround command) and announces it —
    a fresh reason to come back each day, with no admin action needed.
    Reset time defaults to midnight UTC; override with DAILY_RESET_HOUR_UTC.
    """
    reset_hour = int(os.environ.get("DAILY_RESET_HOUR_UTC", "0"))

    while True:
        now = datetime.now(timezone.utc)
        next_reset = now.replace(hour=reset_hour, minute=0, second=0, microsecond=0)
        if next_reset <= now:
            next_reset = next_reset + timedelta(days=1)
        sleep_seconds = (next_reset - now).total_seconds()
        logger.info("Daily reset scheduled in %.0f seconds", sleep_seconds)
        time.sleep(max(sleep_seconds, 1))

        try:
            with db_lock, contextlib.closing(get_conn()) as conn, conn:
                conn.execute(
                    "UPDATE game_round SET status = 'open', winner_code = NULL, "
                    "opened_at = datetime('now'), closed_at = NULL WHERE id = 1"
                )
            send_channel_message(
                text="🌅 A NEW DAY, A NEW VAULT! Today's round is open — pay your 5 GHS entry "
                "and race to be the FIRST to escape. Only one winner takes the prize. Good luck!"
            )
            logger.info("Daily round reset completed")
        except Exception:
            logger.exception("Daily reset failed")


# -----------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------

if __name__ == "__main__":
    init_db()

    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    reset_thread = threading.Thread(target=run_daily_reset_loop, daemon=True)
    reset_thread.start()

    logger.info("Starting Flask API on port %s", PORT)
    app.run(host="0.0.0.0", port=PORT)
