"""
Brain Boss — backend (Flask API + Telegram bot), for Render.

GAME MODEL (as of this rebuild):
  5 sequential rounds per play session. Every round is answered — a wrong
  or timed-out round does NOT end the run early, it just doesn't count.
  After round 5, total correct answers (0-5) determines a FIXED reward:
    4/5 correct -> 20 GHS
    5/5 correct -> 50 GHS
    otherwise    -> 0 GHS
  This replaced an earlier "first correct answer wins the whole pool"
  design. That single-winner/race-condition logic has been fully removed —
  it doesn't apply to a fixed-tier reward model.

  IMPORTANT — flagged to the operator and accepted as-is: paying out 4x-10x
  the entry fee to every player who reaches that score is only sustainable
  if 4/5 and 5/5 stay rare. With a small, fixed pool of puzzles per module,
  scores will very likely climb once answers get shared in the community.
  Revisit this if payouts start outpacing entry fees.

SECURITY MODEL:
  Every round's puzzle is generated server-side, with the correct answer
  held server-side (in the `codes` row for real games, in an in-memory
  session dict for test games). The browser only renders puzzle_json and
  reports the player's raw answer — it never decides correct/incorrect.

Run locally:
    python main.py

Environment variables (set these in Render's dashboard, or a local .env):
    TELEGRAM_BOT_TOKEN      - from @BotFather
    OCR_SPACE_API_KEY       - from ocr.space
    FRONTEND_URL            - your Netlify site, e.g. https://brainboss.netlify.app
    SECRET_KEY              - any long random string, used to sign play_tokens
    ADMIN_USER_IDS          - comma-separated Telegram user IDs allowed to confirm payouts
                              and who receive live session-completion analytics
    ANNOUNCEMENT_CHANNEL_ID - your channel's chat ID (bot must be an admin there)
    TELEGRAM_BOT_USERNAME   - your bot's @username (no @), used for referral deep links
    TEST_ACCESS_CODES       - comma-separated free test codes (default: 900001)
    PORT                    - provided automatically by Render
"""

import os
import re
import io
import json
import time
import uuid
import random
import sqlite3
import logging
import threading
import contextlib
from datetime import datetime, timezone, timedelta

import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

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
OCR_SPACE_API_KEY = os.environ.get("OCR_SPACE_API_KEY", "helloworld")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://your-site.netlify.app")
SECRET_KEY = os.environ.get("SECRET_KEY", "")
PORT = int(os.environ.get("PORT", 5000))

ADMIN_USER_IDS = {
    int(part.strip())
    for part in os.environ.get("ADMIN_USER_IDS", "").split(",")
    if part.strip().isdigit()
}
ANNOUNCEMENT_CHANNEL_ID = os.environ.get("ANNOUNCEMENT_CHANNEL_ID", "").strip() or None
TELEGRAM_BOT_USERNAME = os.environ.get("TELEGRAM_BOT_USERNAME", "").strip()

# Free, always-valid test code(s) for the operator only. Never touch the
# payments DB, never affect real jackpot/reward stats.
TEST_ACCESS_CODES = set(
    (os.environ.get("TEST_ACCESS_CODES", "") or "900001").split(",")
)

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is required.")
if not SECRET_KEY:
    raise RuntimeError(
        "SECRET_KEY environment variable is required (used to sign play_tokens). "
        "Set it to any long random string in Render's dashboard."
    )

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brain_boss.db")
PLAY_TOKEN_MAX_AGE_SECONDS = 30 * 60  # a full 5-round session gets 30 minutes
ENTRY_FEE_GHS = 5
REWARD_TABLE = {5: 50, 4: 20}  # total_correct -> GHS reward; anything else = 0

RECIPIENT_NAME_TOKENS = ["PRINCE", "KWABENA", "BOATENG"]
RECIPIENT_NUMBER = "0205499441"
RECIPIENT_DISPLAY_NAME = " ".join(t.title() for t in RECIPIENT_NAME_TOKENS)

serializer = URLSafeTimedSerializer(SECRET_KEY, salt="brain-boss-play-token")
db_lock = threading.Lock()

# In-memory state for test sessions only (never persisted, never affects
# real data). Keyed by a random session_id signed into the test play_token.
TEST_SESSIONS = {}
test_sessions_lock = threading.Lock()


def _telegram_api_post(method: str, data: dict):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}", data=data, timeout=15)
    except Exception:
        logger.exception("Telegram API call failed: %s", method)


def send_channel_message(text=None, photo_file_id=None, caption=None):
    if not ANNOUNCEMENT_CHANNEL_ID:
        logger.info("ANNOUNCEMENT_CHANNEL_ID not set — skipping channel post.")
        return
    if photo_file_id:
        _telegram_api_post("sendPhoto", {"chat_id": ANNOUNCEMENT_CHANNEL_ID, "photo": photo_file_id, "caption": caption or ""})
    else:
        _telegram_api_post("sendMessage", {"chat_id": ANNOUNCEMENT_CHANNEL_ID, "text": text or ""})


def send_telegram_dm(chat_id: str, text: str):
    _telegram_api_post("sendMessage", {"chat_id": chat_id, "text": text})


def notify_admins(text: str):
    for admin_id in ADMIN_USER_IDS:
        send_telegram_dm(admin_id, text)


# -----------------------------------------------------------------------
# Database
# -----------------------------------------------------------------------

def get_conn():
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
                telegram_username TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                burned_at TEXT,
                puzzles_json TEXT                        -- all 5 rounds' {module,puzzle,answer}, generated once
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS round_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                round_index INTEGER NOT NULL,
                correct INTEGER NOT NULL,
                time_taken REAL,
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(code, round_index)
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                user_id TEXT,
                telegram_username TEXT,
                total_correct_answers INTEGER NOT NULL,
                total_time_spent REAL,
                reward_amount INTEGER NOT NULL DEFAULT 0,
                claimed INTEGER NOT NULL DEFAULT 0,
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
                submission_id INTEGER,
                winning_code TEXT,
                payment_details TEXT NOT NULL,
                reward_amount INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',   -- 'pending' | 'paid' | 'rejected'
                created_at TEXT DEFAULT (datetime('now')),
                paid_at TEXT
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS discounts (
                code TEXT PRIMARY KEY,
                telegram_user_id TEXT NOT NULL,
                percent_off INTEGER NOT NULL DEFAULT 50,
                source TEXT,
                status TEXT NOT NULL DEFAULT 'unused',
                created_at TEXT DEFAULT (datetime('now')),
                used_at TEXT
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_references (
                reference TEXT PRIMARY KEY,               -- e.g. "BB4909"
                telegram_user_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'unused',
                created_at TEXT DEFAULT (datetime('now')),
                used_at TEXT
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS referral_codes (
                telegram_user_id TEXT PRIMARY KEY,
                referral_code TEXT UNIQUE NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS referral_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referral_code TEXT NOT NULL,
                referred_user_id TEXT UNIQUE NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT DEFAULT (datetime('now')),
                completed_at TEXT
            )
            """
        )

        # Migrations for DBs created by earlier versions of this schema.
        for stmt in [
            "ALTER TABLE codes ADD COLUMN telegram_username TEXT",
            "ALTER TABLE codes ADD COLUMN puzzles_json TEXT",
            "ALTER TABLE withdrawals ADD COLUMN submission_id INTEGER",
            "ALTER TABLE withdrawals ADD COLUMN reward_amount INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE withdrawals ADD COLUMN paid_at TEXT",
        ]:
            try:
                conn.execute(stmt)
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
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    for _ in range(50):
        candidate = "SAVE-" + "".join(random.choice(alphabet) for _ in range(6))
        row = conn.execute("SELECT 1 FROM discounts WHERE code = ?", (candidate,)).fetchone()
        if not row:
            return candidate
    raise RuntimeError("Could not generate a unique discount code after 50 attempts.")


def generate_reference_code(conn) -> str:
    for _ in range(50):
        candidate = "BB" + f"{random.randint(0, 9999):04d}"
        row = conn.execute("SELECT 1 FROM user_references WHERE reference = ?", (candidate,)).fetchone()
        if not row:
            return candidate
    raise RuntimeError("Could not generate a unique reference code after 50 attempts.")


def get_or_create_reference(conn, telegram_user_id: str) -> str:
    row = conn.execute(
        "SELECT reference FROM user_references WHERE telegram_user_id = ? AND status = 'unused' "
        "ORDER BY created_at DESC LIMIT 1",
        (telegram_user_id,),
    ).fetchone()
    if row:
        return row["reference"]
    reference = generate_reference_code(conn)
    conn.execute(
        "INSERT INTO user_references (reference, telegram_user_id, status) VALUES (?, ?, 'unused')",
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


def build_bot_link() -> str:
    if not TELEGRAM_BOT_USERNAME:
        return "(set TELEGRAM_BOT_USERNAME)"
    return f"https://t.me/{TELEGRAM_BOT_USERNAME}"


# -----------------------------------------------------------------------
# OCR + payment validation
# -----------------------------------------------------------------------

def ocr_image(image_bytes: bytes) -> str:
    response = requests.post(
        "https://api.ocr.space/parse/image",
        files={"file": ("receipt.jpg", image_bytes)},
        data={"apikey": OCR_SPACE_API_KEY, "language": "eng", "isOverlayRequired": False, "OCREngine": 2, "scale": True},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("IsErroredOnProcessing"):
        raise ValueError(f"OCR failed: {data.get('ErrorMessage') or ['Unknown OCR error']}")
    parsed_results = data.get("ParsedResults") or []
    if not parsed_results:
        raise ValueError("OCR returned no parsed results.")
    return parsed_results[0].get("ParsedText", "")


def check_recipient(text: str) -> bool:
    digits_only = re.sub(r"\D", "", text)
    local9 = RECIPIENT_NUMBER[1:]
    number_match = RECIPIENT_NUMBER in digits_only or ("233" + local9) in digits_only or local9 in digits_only
    upper_text = text.upper()
    name_hits = sum(1 for token in RECIPIENT_NAME_TOKENS if token in upper_text)
    return number_match or name_hits >= 2


FULL_AMOUNT_PATTERN = re.compile(
    r"(?:\bGHS\s*5(?:\.0{1,2})?\b)|(?:\b5(?:\.0{1,2})?\s*GHS\b)|(?:\b5\.00\b)|(?:\b5\.0\b)", re.IGNORECASE
)
HALF_AMOUNT_PATTERN = re.compile(
    r"(?:\bGHS\s*2\.50\b)|(?:\b2\.50\s*GHS\b)|(?:\bGHS\s*2\.5\b)|(?:\b2\.5\s*GHS\b)|(?:\b2\.50\b)", re.IGNORECASE
)
SUCCESS_PATTERN = re.compile(
    r"\b(success(?:ful)?|completed|confirmed|you\s+have\s+sent|you\s+have\s+received|sent\s+to|"
    r"payment\s+received|transaction\s+successful)\b",
    re.IGNORECASE,
)
PENDING_PATTERN = re.compile(
    r"\b(pending|kindly\s+wait|processing|awaiting\s+confirmation|request\s+received)\b", re.IGNORECASE
)
REJECT_PATTERN = re.compile(r"\b(bundle|airtime|data\s+purchase)\b", re.IGNORECASE)

TXN_ID_PATTERNS = [
    re.compile(r"(?:trans(?:action)?\.?\s*id)[:\s]*([A-Za-z0-9\-\.]{6,25})", re.IGNORECASE),
    re.compile(r"(?:financial\s*trans(?:action)?\.?\s*id)[:\s]*([A-Za-z0-9\-\.]{6,25})", re.IGNORECASE),
    re.compile(r"(?:ref(?:erence)?\.?\s*(?:no\.?|number)?)[:\s]*([A-Za-z0-9\-\.]{6,25})", re.IGNORECASE),
    re.compile(r"(?:external\s*(?:trans(?:action)?)?\.?\s*id)[:\s]*([A-Za-z0-9\-\.]{6,25})", re.IGNORECASE),
    re.compile(r"\b(\d{10,20})\b"),
]

REFERENCE_PATTERN = re.compile(r"\bBB[\s\-]?(\d{4})\b", re.IGNORECASE)


def validate_receipt_text(text: str):
    """Returns (is_valid, transaction_id, reason, amount_tier, reference_code)."""
    if REJECT_PATTERN.search(text):
        return False, None, "This looks like a bundle or airtime purchase, not a payment to us.", None, None

    if PENDING_PATTERN.search(text):
        return False, None, "Transaction appears pending, not completed.", None, None

    if not SUCCESS_PATTERN.search(text):
        return False, None, "No success confirmation found on the receipt.", None, None

    if FULL_AMOUNT_PATTERN.search(text):
        amount_tier = "full"
    elif HALF_AMOUNT_PATTERN.search(text):
        amount_tier = "half"
    else:
        return False, None, "Could not confirm the payment amount (5 GHS, or 2.50 GHS with a discount code).", None, None

    ref_match = REFERENCE_PATTERN.search(text)
    reference_code = f"BB{ref_match.group(1)}" if ref_match else None

    # STRICT reference check per spec: if no reference is found at all,
    # reject outright and tell the player to include it — no silent
    # fallback to fuzzy recipient matching this time.
    if not reference_code:
        return (
            False, None,
            "Your payment reference wasn't found on this receipt. Make sure you entered your exact "
            "BB code in the MoMo 'Reference' or 'Note' field, then send a fresh screenshot.",
            None, None,
        )

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
# ROUND CONFIG + server-authoritative puzzle generation & answer checking
# -----------------------------------------------------------------------

ROUND_CONFIG = [
    {"module": "sequence_trap", "label": "Round 1", "base": 20, "bonus_target": 1, "bonus_amount": 10},
    {"module": "matchstick", "label": "Round 2", "base": 25, "bonus_target": 2, "bonus_amount": 10},
    {"module": "detective", "label": "Round 3", "base": 20, "bonus_target": 4, "bonus_amount": 5},
    {"module": "spot_glitch", "label": "Round 4", "base": 20, "bonus_target": 4, "bonus_amount": 5},
    {"module": "trap_question", "label": "Round 5 — Final Challenge", "base": 20, "bonus_target": None, "bonus_amount": 0},
]
TOTAL_ROUNDS = len(ROUND_CONFIG)


def generate_sequence_trap():
    use_multiply = random.random() < 0.5
    k = random.randint(2, 3) if use_multiply else random.randint(3, 8)
    start = random.randint(1, 6)
    terms = [start]
    for _ in range(3):
        start = start * k if use_multiply else start + k
        terms.append(start)
    correct = terms[3] * k if use_multiply else terms[3] + k
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


def generate_matchstick():
    # Fixed classic puzzle: 6+4=4  ->  5+4=9 (one stick moved). The client
    # already knows this fixed starting layout; nothing secret to send.
    return {}, "5+4=9"


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


TRAP_QUESTIONS = [
    {
        "question": "A rooster lays an egg right on the peak of a barn roof. Which way does it roll — left or right?",
        "accepted": {"NEITHER", "ROOSTERSDONTLAYEGGS", "ROOSTERSDONOTLAYEGGS", "ROOSTERSCANTLAYEGGS"},
    },
    {
        "question": "If a plane crashes exactly on the border of two countries, where do they bury the survivors?",
        "accepted": {"NEITHER", "SURVIVORS", "YOUDONTBURYSURVIVORS", "THEYRENOTDEAD", "THEYREALIVE"},
    },
]


def generate_trap_question():
    scenario = random.choice(TRAP_QUESTIONS)
    return {"question": scenario["question"]}, "|".join(sorted(scenario["accepted"]))


def generate_puzzle_for_module(module: str):
    return {
        "sequence_trap": generate_sequence_trap,
        "matchstick": generate_matchstick,
        "detective": generate_detective,
        "spot_glitch": generate_spot_glitch,
        "trap_question": generate_trap_question,
    }[module]()


def generate_all_rounds():
    """Returns a list of TOTAL_ROUNDS dicts: {module, puzzle, answer}."""
    rounds = []
    for cfg in ROUND_CONFIG:
        puzzle, answer = generate_puzzle_for_module(cfg["module"])
        rounds.append({"module": cfg["module"], "puzzle": puzzle, "answer": answer})
    return rounds


def check_answer(module: str, correct_answer: str, submitted_answer: str) -> bool:
    submitted = str(submitted_answer or "").strip()
    correct = str(correct_answer or "").strip()

    if module == "detective":
        return submitted.lower() == correct.lower()

    if module == "matchstick":
        return submitted.replace(" ", "") == correct.replace(" ", "")

    if module == "trap_question":
        norm = re.sub(r"[^A-Z]", "", submitted.upper())
        accepted = set(correct.split("|"))
        return norm in accepted

    return submitted == correct  # sequence_trap, spot_glitch: exact match


def compute_time_budget(round_index: int, results: list) -> int:
    """results is a list of TOTAL_ROUNDS entries, each True/False/None."""
    budget = ROUND_CONFIG[round_index]["base"]
    for i, cfg in enumerate(ROUND_CONFIG):
        if cfg["bonus_target"] == round_index and results[i] is True:
            budget += cfg["bonus_amount"]
    return budget


def get_real_results(conn, code: str) -> list:
    results = [None] * TOTAL_ROUNDS
    for row in conn.execute("SELECT round_index, correct FROM round_results WHERE code = ?", (code,)):
        results[row["round_index"]] = bool(row["correct"])
    return results


# -----------------------------------------------------------------------
# Telegram bot handlers
# -----------------------------------------------------------------------

def build_rules_text(reference: str) -> str:
    return f"""🧠 WELCOME TO BRAIN BOSS ARENA 🏆
Here are the official rules for today's challenge:

1️⃣ ENTRY FEE & PAYMENT:
- Entry fee: 5 GHS.
- Send payment to: {RECIPIENT_NUMBER} ({RECIPIENT_DISPLAY_NAME}).
- ⚠️ IMPORTANT: Enter this exact reference in the MoMo "Reference" or "Note" field when you pay: {reference}
- Upload your clean screenshot receipt afterward. (MTN, Telecel, AT supported; airtime/data screenshots rejected).

2️⃣ THE CHALLENGE WEBSITE:
- Get your 6-digit access code upon verification to unlock the arena.

3️⃣ STRICT ONE-TRY LIMIT:
- The moment you press "Submit" on the final round, your code is burned permanently.

4️⃣ FIXED REWARDS SYSTEM:
- 4 Correct Answers = 20 GHS!
- ALL 5 Correct Answers = 50 GHS!

Your payment reference is {reference} — don't forget to include it! Send your screenshot now!"""


pending_withdrawals = {}  # telegram user_id -> {username, submission_id, reward_amount}
ADMIN_PAID_PATTERN = re.compile(r"^\s*PAID\s+(\d{6})\s*$", re.IGNORECASE)


def mask_payment_details(text: str) -> str:
    def _mask(match):
        digits = match.group(0)
        return digits if len(digits) <= 3 else "*" * (len(digits) - 3) + digits[-3:]
    return re.sub(r"\d{6,}", _mask, text)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

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
                        "INSERT INTO referral_events (referral_code, referred_user_id, status) VALUES (?, ?, 'pending')",
                        (referral_code, str(user.id)),
                    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎟️ Buy Game Ticket", callback_data="buy_ticket")],
        [InlineKeyboardButton("💰 Withdraw Winnings", callback_data="withdraw")],
        [InlineKeyboardButton("📢 My Referrals", callback_data="referrals")],
    ])
    await update.message.reply_text("Welcome to Brain Boss! What would you like to do?", reply_markup=keyboard)


async def myid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Your Telegram ID (as seen by this bot): {update.effective_user.id}")


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
        if user.id in pending_withdrawals:
            await query.message.reply_text(
                "I'm still waiting for your withdrawal details from your last request — "
                "please send your Mobile Money Number, Registered Name, and Network."
            )
            return

        with contextlib.closing(get_conn()) as conn:
            submission = conn.execute(
                "SELECT * FROM submissions WHERE user_id = ? AND reward_amount > 0 AND claimed = 0 "
                "ORDER BY created_at ASC LIMIT 1",
                (str(user.id),),
            ).fetchone()

        if not submission:
            await query.message.reply_text("❌ You currently do not have any pending winnings to withdraw. Keep playing to win!")
            return

        pending_withdrawals[user.id] = {
            "username": user.username or user.full_name,
            "submission_id": submission["id"],
            "code": submission["code"],
            "reward_amount": submission["reward_amount"],
        }
        await query.message.reply_text(
            f"🏆 You have {submission['reward_amount']} GHS available to withdraw! Please reply with your:\n"
            "- Mobile Money Number\n- Registered Name\n- Network (MTN / Telecel / AT)\n\nSend all three in one message."
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
            f"Yo! I'm playing Brain Boss for real cash prizes. Click my link to join! 🔐👇\n{link}"
        )
        await query.message.reply_text(
            f"📢 Your referral link:\n{link}\n\n"
            f"Progress to your next 50% discount: [{progress_bar}] ({slots_filled}/3)\n\n"
            f"Share this message with friends:\n\n{share_message}"
        )
        return


async def handle_admin_payout_proof(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str):
    message = update.message
    with db_lock, contextlib.closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT * FROM withdrawals WHERE winning_code = ? AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
            (code,),
        ).fetchone()
        if not row:
            await message.reply_text(f"No pending withdrawal found for code {code}.")
            return
        with conn:
            conn.execute("UPDATE withdrawals SET status = 'paid', paid_at = datetime('now') WHERE id = ?", (row["id"],))

    masked_details = mask_payment_details(row["payment_details"])
    photo_file_id = message.photo[-1].file_id
    announcement = (
        f"🏆 PAYOUT CONFIRMED!\n\nCode {code} was paid out {row['reward_amount']} GHS via Mobile Money. ✅\n"
        f"Recipient: {masked_details}\n\n🔥 Congratulations!"
    )
    if not ANNOUNCEMENT_CHANNEL_ID:
        await message.reply_text("Marked as paid. (ANNOUNCEMENT_CHANNEL_ID isn't set, so nothing was posted publicly.)")
        return
    try:
        await context.bot.send_photo(chat_id=ANNOUNCEMENT_CHANNEL_ID, photo=photo_file_id, caption=announcement)
        await message.reply_text("Posted payout proof to the channel ✅")
    except Exception:
        logger.exception("Failed to post payout proof to channel")
        await message.reply_text("Marked as paid, but posting to the channel failed — check the logs.")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user = update.effective_user

    if user.id in ADMIN_USER_IDS:
        caption = (message.caption or "").strip()
        admin_match = ADMIN_PAID_PATTERN.match(caption)
        if admin_match:
            await handle_admin_payout_proof(update, context, admin_match.group(1))
            return

    await message.reply_text("Got your screenshot — checking it now, one moment...")

    try:
        photo = message.photo[-1]
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
            await message.reply_text("Payment screenshot not recognized. Please make sure the amount is 5 GHS and try again.")
        return

    is_valid, txn_id, reason, amount_tier, reference_code = validate_receipt_text(parsed_text)
    logger.info("OCR validation for user %s: valid=%s reason=%s tier=%s ref=%s", user.id, is_valid, reason, amount_tier, reference_code)

    if not is_valid:
        if user.id in ADMIN_USER_IDS:
            snippet = parsed_text.strip()[:800] or "(OCR returned empty text)"
            await message.reply_text(f"[DEBUG — admin only]\nreason: {reason}\namount_tier: {amount_tier}\n\nRaw OCR text:\n{snippet}")
            return
        await message.reply_text(reason or "Payment screenshot not recognized. Please try again.")
        return

    discount_row = None
    reference_row = None

    with db_lock, contextlib.closing(get_conn()) as conn:
        existing = conn.execute("SELECT code FROM codes WHERE transaction_id = ?", (txn_id,)).fetchone()
        if existing:
            await message.reply_text("This payment has already been used to generate an access code.")
            return

        reference_row = conn.execute("SELECT * FROM user_references WHERE reference = ?", (reference_code,)).fetchone()
        if not reference_row or reference_row["status"] != "unused" or reference_row["telegram_user_id"] != str(user.id):
            await message.reply_text(
                f"We found reference {reference_code} on this receipt, but it doesn't match an active "
                "reference for your account. Tap 'Buy Game Ticket' in the menu to get a fresh reference, then try again."
            )
            return

        if amount_tier == "half":
            discount_row = conn.execute(
                "SELECT * FROM discounts WHERE telegram_user_id = ? AND status = 'unused' ORDER BY created_at ASC LIMIT 1",
                (str(user.id),),
            ).fetchone()
            if not discount_row:
                await message.reply_text(
                    "That looks like a discounted (2.50 GHS) payment, but we don't have an active discount "
                    "code on file for your account. Pay the full 5 GHS entry fee instead."
                )
                return

        with conn:
            is_first_ever_code = conn.execute(
                "SELECT COUNT(*) AS n FROM codes WHERE telegram_user_id = ?", (str(user.id),)
            ).fetchone()["n"] == 0

            code = generate_unique_code(conn)
            conn.execute(
                "INSERT INTO codes (code, transaction_id, status, telegram_user_id, telegram_username) "
                "VALUES (?, ?, 'valid', ?, ?)",
                (code, txn_id, str(user.id), user.username or user.full_name),
            )
            conn.execute("UPDATE user_references SET status = 'used', used_at = datetime('now') WHERE reference = ?", (reference_code,))
            if discount_row:
                conn.execute("UPDATE discounts SET status = 'used', used_at = datetime('now') WHERE code = ?", (discount_row["code"],))

            referral_reward_code = None
            referral_owner_id = None
            if is_first_ever_code:
                pending_referral = conn.execute(
                    "SELECT * FROM referral_events WHERE referred_user_id = ? AND status = 'pending'", (str(user.id),)
                ).fetchone()
                if pending_referral:
                    conn.execute(
                        "UPDATE referral_events SET status = 'completed', completed_at = datetime('now') WHERE id = ?",
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
                                "INSERT INTO discounts (code, telegram_user_id, percent_off, source) VALUES (?, ?, 50, 'referral_bonus')",
                                (referral_reward_code, referral_owner_id),
                            )

    if referral_reward_code and referral_owner_id:
        send_telegram_dm(
            referral_owner_id,
            f"🎉 3 friends joined through your referral link! Here's a 50% discount code for your next entry:\n\n"
            f"{referral_reward_code}\n\nPay 2.50 GHS instead of 5 GHS, and send the screenshot as usual.",
        )

    prefix = ""
    if discount_row:
        prefix += f"Discount applied! 🎉 ({discount_row['code']})\n\n"
    prefix += f"Reference {reference_code} matched ✅\n\n"

    await message.reply_text(
        f"{prefix}Payment confirmed! ✅\n\nYour access code: {code}\n\nPlay here: {FRONTEND_URL}\n\nThis code works once — save it now."
    )


async def handle_other_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if user.id in pending_withdrawals:
        details = pending_withdrawals.pop(user.id)
        payment_details = update.message.text.strip()

        with db_lock, contextlib.closing(get_conn()) as conn, conn:
            conn.execute(
                "INSERT INTO withdrawals (user_id, username, submission_id, winning_code, payment_details, reward_amount, status) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending')",
                (str(user.id), details["username"], details["submission_id"], details["code"],
                 payment_details, details["reward_amount"]),
            )
            conn.execute("UPDATE submissions SET claimed = 1 WHERE id = ?", (details["submission_id"],))

        await update.message.reply_text("✅ Your withdrawal request has been received! Our admin team will process your payment shortly.")
        return

    await update.message.reply_text("Send a screenshot of your successful 5 GHS Mobile Money payment to get your access code, or use /start to see the menu.")


def build_telegram_app() -> Application:
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("myid", myid_command))
    application.add_handler(CallbackQueryHandler(handle_menu_callback))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(~filters.PHOTO & ~filters.COMMAND, handle_other_messages))
    return application


def run_bot():
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    application = build_telegram_app()
    logger.info("Starting Telegram bot polling...")
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

    if code in TEST_ACCESS_CODES:
        session_id = uuid.uuid4().hex
        with test_sessions_lock:
            TEST_SESSIONS[session_id] = {"rounds": generate_all_rounds(), "results": [None] * TOTAL_ROUNDS}
        play_token = serializer.dumps({"code": code, "test": True, "session_id": session_id})
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
            conn.execute("UPDATE codes SET play_token = ? WHERE code = ?", (play_token, code))

    referral_link = build_referral_link(referral_code) if referral_code else None
    return jsonify({"valid": True, "play_token": play_token, "referral_link": referral_link})


@app.get("/api/jackpot")
def jackpot():
    with contextlib.closing(get_conn()) as conn:
        total_codes = conn.execute("SELECT COUNT(*) AS n FROM codes").fetchone()["n"]
    return jsonify({"amount": total_codes * ENTRY_FEE_GHS, "currency": "GHS"})


def _safe_puzzle(round_entry: dict) -> dict:
    """Strips the answer out before sending a round to the client."""
    return {"module": round_entry["module"], "puzzle": round_entry["puzzle"]}


@app.post("/api/start-game")
def start_game():
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

    if token_data.get("test"):
        with test_sessions_lock:
            session = TEST_SESSIONS.get(token_data.get("session_id"))
        if not session:
            return jsonify({"success": False, "message": "Test session expired — re-enter your code."}), 401
        time_budget = compute_time_budget(0, session["results"])
        return jsonify({"success": True, "round_index": 0, "total_rounds": TOTAL_ROUNDS,
                         "time_budget": time_budget, **_safe_puzzle(session["rounds"][0])})

    with db_lock, contextlib.closing(get_conn()) as conn:
        row = conn.execute("SELECT * FROM codes WHERE code = ?", (code,)).fetchone()
        if not row:
            return jsonify({"success": False, "message": "Invalid code."}), 404
        if row["play_token"] != play_token:
            return jsonify({"success": False, "message": "This session is no longer valid."}), 401
        if row["status"] != "valid":
            return jsonify({"success": False, "message": "This code has already been used."}), 410

        if row["puzzles_json"]:
            rounds = json.loads(row["puzzles_json"])
        else:
            rounds = generate_all_rounds()
            with conn:
                conn.execute("UPDATE codes SET puzzles_json = ? WHERE code = ?", (json.dumps(rounds), code))

        results = get_real_results(conn, code)

    # Idempotent resume: serve whichever round hasn't been answered yet.
    next_index = next((i for i, r in enumerate(results) if r is None), 0)
    time_budget = compute_time_budget(next_index, results)
    return jsonify({"success": True, "round_index": next_index, "total_rounds": TOTAL_ROUNDS,
                     "time_budget": time_budget, **_safe_puzzle(rounds[next_index])})


def _finalize_test_session(session_id, results, rounds, time_taken_final):
    total_correct = sum(1 for r in results if r)
    reward = REWARD_TABLE.get(total_correct, 0)
    with test_sessions_lock:
        TEST_SESSIONS.pop(session_id, None)
    message = (
        f"🧪 [TEST MODE] Run complete — {total_correct}/{TOTAL_ROUNDS} correct. "
        f"Would have earned {reward} GHS. (not counted toward any real stats)"
    )
    notify_admins(
        f"📊 [TEST MODE] SESSION COMPLETED!\n🎯 Correct: {total_correct}/{TOTAL_ROUNDS}\n💰 Would-be reward: {reward} GHS"
    )
    return total_correct, reward, message


@app.post("/api/submit-round")
def submit_round():
    payload = request.get_json(silent=True) or {}
    code = str(payload.get("code", "")).strip()
    play_token = str(payload.get("play_token", "")).strip()
    round_index = payload.get("round_index")
    answer = str(payload.get("answer", "")).strip()
    time_taken = payload.get("time_taken")

    if not re.fullmatch(r"\d{6}", code) or not play_token or round_index is None:
        return jsonify({"success": False, "message": "Missing or malformed request."}), 400
    round_index = int(round_index)
    if round_index < 0 or round_index >= TOTAL_ROUNDS - 1:
        return jsonify({"success": False, "message": "Use /api/submit-score for the final round."}), 400

    try:
        token_data = serializer.loads(play_token, max_age=PLAY_TOKEN_MAX_AGE_SECONDS)
    except (SignatureExpired, BadSignature):
        return jsonify({"success": False, "message": "Your session expired. Re-enter your code."}), 401
    if token_data.get("code") != code:
        return jsonify({"success": False, "message": "Token does not match code."}), 401

    if token_data.get("test"):
        with test_sessions_lock:
            session = TEST_SESSIONS.get(token_data.get("session_id"))
            if not session:
                return jsonify({"success": False, "message": "Test session expired — re-enter your code."}), 401
            if any(r is None for r in session["results"][:round_index]):
                return jsonify({"success": False, "message": "Complete the previous rounds first."}), 400
            if session["results"][round_index] is not None:
                return jsonify({"success": False, "message": "This round was already answered."}), 400

            entry = session["rounds"][round_index]
            is_correct = check_answer(entry["module"], entry["answer"], answer)
            session["results"][round_index] = is_correct
            next_budget = compute_time_budget(round_index + 1, session["results"])
            next_puzzle = _safe_puzzle(session["rounds"][round_index + 1])

        return jsonify({"success": True, "correct": is_correct, "round_index": round_index + 1,
                         "time_budget": next_budget, "test": True, **next_puzzle})

    with db_lock, contextlib.closing(get_conn()) as conn:
        row = conn.execute("SELECT * FROM codes WHERE code = ?", (code,)).fetchone()
        if not row:
            return jsonify({"success": False, "message": "Invalid code."}), 404
        if row["play_token"] != play_token:
            return jsonify({"success": False, "message": "This session is no longer valid."}), 401
        if row["status"] != "valid":
            return jsonify({"success": False, "message": "This code has already been used."}), 410
        if not row["puzzles_json"]:
            return jsonify({"success": False, "message": "No active game for this code — call start-game first."}), 400

        rounds = json.loads(row["puzzles_json"])
        results = get_real_results(conn, code)

        if any(r is None for r in results[:round_index]):
            return jsonify({"success": False, "message": "Complete the previous rounds first."}), 400
        if results[round_index] is not None:
            return jsonify({"success": False, "message": "This round was already answered."}), 400

        entry = rounds[round_index]
        is_correct = check_answer(entry["module"], entry["answer"], answer)

        with conn:
            conn.execute(
                "INSERT INTO round_results (code, round_index, correct, time_taken) VALUES (?, ?, ?, ?)",
                (code, round_index, int(is_correct), time_taken),
            )

        results[round_index] = is_correct
        next_budget = compute_time_budget(round_index + 1, results)
        next_puzzle = _safe_puzzle(rounds[round_index + 1])

    return jsonify({"success": True, "correct": is_correct, "round_index": round_index + 1,
                     "time_budget": next_budget, **next_puzzle})


@app.post("/api/submit-score")
def submit_score():
    """Final round submission — grades round 4 (index TOTAL_ROUNDS-1), then
    burns the code (real games only), tallies the whole run, and notifies admins."""
    payload = request.get_json(silent=True) or {}
    code = str(payload.get("code", "")).strip()
    play_token = str(payload.get("play_token", "")).strip()
    answer = str(payload.get("answer", "")).strip()
    time_taken = payload.get("time_taken")
    final_index = TOTAL_ROUNDS - 1

    if not re.fullmatch(r"\d{6}", code) or not play_token:
        return jsonify({"success": False, "message": "Missing or malformed request."}), 400

    try:
        token_data = serializer.loads(play_token, max_age=PLAY_TOKEN_MAX_AGE_SECONDS)
    except (SignatureExpired, BadSignature):
        return jsonify({"success": False, "message": "Your session expired. Re-enter your code."}), 401
    if token_data.get("code") != code:
        return jsonify({"success": False, "message": "Token does not match code."}), 401

    if token_data.get("test"):
        with test_sessions_lock:
            session = TEST_SESSIONS.get(token_data.get("session_id"))
            if not session:
                return jsonify({"success": False, "message": "Test session expired — re-enter your code."}), 401
            if any(r is None for r in session["results"][:final_index]):
                return jsonify({"success": False, "message": "Complete the previous rounds first."}), 400

            entry = session["rounds"][final_index]
            is_correct = check_answer(entry["module"], entry["answer"], answer)
            session["results"][final_index] = is_correct
            total_correct, reward, message = _finalize_test_session(
                token_data.get("session_id"), session["results"], session["rounds"], time_taken
            )

        return jsonify({"success": True, "correct": is_correct, "total_correct": total_correct,
                         "reward_amount": reward, "message": message, "test": True})

    with db_lock, contextlib.closing(get_conn()) as conn:
        row = conn.execute("SELECT * FROM codes WHERE code = ?", (code,)).fetchone()
        if not row:
            return jsonify({"success": False, "message": "Invalid code."}), 404
        if row["play_token"] != play_token:
            return jsonify({"success": False, "message": "This session is no longer valid."}), 401
        if not row["puzzles_json"]:
            return jsonify({"success": False, "message": "No active game for this code."}), 400

        rounds = json.loads(row["puzzles_json"])
        results = get_real_results(conn, code)
        if any(r is None for r in results[:final_index]):
            return jsonify({"success": False, "message": "Complete the previous rounds first."}), 400

        # Atomic one-try burn: only the request that flips valid->burned proceeds.
        with conn:
            cursor = conn.execute(
                "UPDATE codes SET status = 'burned', burned_at = datetime('now') WHERE code = ? AND status = 'valid'",
                (code,),
            )
        if cursor.rowcount == 0:
            return jsonify({"success": False, "message": "This code has already been used."}), 410

        entry = rounds[final_index]
        is_correct = check_answer(entry["module"], entry["answer"], answer)

        with conn:
            conn.execute(
                "INSERT INTO round_results (code, round_index, correct, time_taken) VALUES (?, ?, ?, ?)",
                (code, final_index, int(is_correct), time_taken),
            )

        results[final_index] = is_correct
        total_correct = sum(1 for r in results if r)
        reward = REWARD_TABLE.get(total_correct, 0)

        all_times = [r["time_taken"] for r in conn.execute("SELECT time_taken FROM round_results WHERE code = ?", (code,))]
        total_time = round(sum(t for t in all_times if t is not None), 1)

        with conn:
            conn.execute(
                "INSERT INTO submissions (code, user_id, telegram_username, total_correct_answers, "
                "total_time_spent, reward_amount) VALUES (?, ?, ?, ?, ?, ?)",
                (code, row["telegram_user_id"], row["telegram_username"], total_correct, total_time, reward),
            )

    if reward > 0:
        message = f"🎉 Run complete — {total_correct}/{TOTAL_ROUNDS} correct! You earned {reward} GHS. Use 'Withdraw Winnings' in the bot menu to claim it."
    else:
        message = f"Run complete — {total_correct}/{TOTAL_ROUNDS} correct. No reward this time (need 4 or 5 correct). Better luck next round!"

    notify_admins(
        "📊 PLAYER SESSION COMPLETED!\n"
        f"👤 User: {row['telegram_username'] or row['telegram_user_id']}\n"
        f"🔑 Code: {code}\n"
        f"🎯 Correct: {total_correct} / {TOTAL_ROUNDS}\n"
        f"⏱️ Time: {total_time}s\n"
        f"💰 Reward Earned: {reward} GHS"
    )

    return jsonify({"success": True, "correct": is_correct, "total_correct": total_correct,
                     "reward_amount": reward, "message": message})


def run_daily_digest_loop():
    """Once every 24h, posts a public activity digest to the channel — keeps
    it feeling alive without needing a round to reset (there is no longer a
    single shared 'round' in the fixed-reward scoring model)."""
    post_hour = int(os.environ.get("DAILY_DIGEST_HOUR_UTC", "8"))
    while True:
        now = datetime.now(timezone.utc)
        next_post = now.replace(hour=post_hour, minute=0, second=0, microsecond=0)
        if next_post <= now:
            next_post += timedelta(days=1)
        time.sleep(max((next_post - now).total_seconds(), 1))

        try:
            since = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
            with contextlib.closing(get_conn()) as conn:
                stats = conn.execute(
                    "SELECT COUNT(*) AS runs, COALESCE(SUM(reward_amount),0) AS paid, "
                    "COALESCE(SUM(CASE WHEN reward_amount > 0 THEN 1 ELSE 0 END),0) AS winners "
                    "FROM submissions WHERE created_at >= ?",
                    (since,),
                ).fetchone()
            send_channel_message(
                text="🌅 DAILY BRAIN BOSS DIGEST\n\n"
                f"🎮 Runs played (last 24h): {stats['runs']}\n"
                f"🏆 Players who earned a reward: {stats['winners']}\n"
                f"💰 Total paid out: {stats['paid']} GHS\n\n"
                f"Pay your 5 GHS entry and see if you can beat today's arena! {build_bot_link()}"
            )
            logger.info("Daily digest posted")
        except Exception:
            logger.exception("Daily digest failed")


# -----------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------

if __name__ == "__main__":
    init_db()

    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    digest_thread = threading.Thread(target=run_daily_digest_loop, daemon=True)
    digest_thread.start()

    logger.info("Starting Flask API on port %s", PORT)
    app.run(host="0.0.0.0", port=PORT)
