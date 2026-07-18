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
import random
import sqlite3
import logging
import threading
import contextlib
from datetime import datetime, timezone

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

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is required.")
if not SECRET_KEY:
    raise RuntimeError(
        "SECRET_KEY environment variable is required (used to sign play_tokens). "
        "Set it to any long random string in Render's dashboard."
    )

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brain_boss.db")
PLAY_TOKEN_MAX_AGE_SECONDS = 10 * 60  # play_token expires 10 minutes after verify-code
WINNING_FORMULA = "5+4=9"

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
                burned_at TEXT
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

        # Migration for DBs created before paid_at existed on withdrawals.
        try:
            conn.execute("ALTER TABLE withdrawals ADD COLUMN paid_at TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
    logger.info("Database ready at %s", DB_PATH)


def generate_unique_code(conn) -> str:
    for _ in range(50):
        candidate = f"{random.randint(0, 999999):06d}"
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
# a starting point — MTN, Telecel, and AirtelTigo all phrase this
# differently, so widen it as you see more real receipts.
SUCCESS_PATTERN = re.compile(
    r"\b(success(?:ful)?|completed|you\s+have\s+sent|payment\s+received|transaction\s+successful)\b",
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


def validate_receipt_text(text: str):
    """
    Returns (is_valid: bool, transaction_id: str | None, reason: str, amount_tier: str | None)
    amount_tier is "full" (5 GHS) or "half" (2.50 GHS, requires a discount code
    to be honored — checked separately by the caller).
    """
    if PENDING_PATTERN.search(text):
        return False, None, "Transaction appears pending, not completed.", None

    if not SUCCESS_PATTERN.search(text):
        return False, None, "No success confirmation found on the receipt.", None

    if FULL_AMOUNT_PATTERN.search(text):
        amount_tier = "full"
    elif HALF_AMOUNT_PATTERN.search(text):
        amount_tier = "half"
    else:
        return False, None, "Could not confirm the payment amount (5 GHS, or 2.50 GHS with a discount code).", None

    if not check_recipient(text):
        return False, None, "Payment was not sent to the correct recipient.", None

    txn_id = None
    for pattern in TXN_ID_PATTERNS:
        match = pattern.search(text)
        if match:
            txn_id = match.group(1).strip()
            break

    if not txn_id:
        return False, None, "Could not find a transaction ID on the receipt.", None

    return True, txn_id, "ok", amount_tier


# -----------------------------------------------------------------------
# Telegram bot handlers
# -----------------------------------------------------------------------

RECIPIENT_DISPLAY_NAME = " ".join(t.title() for t in RECIPIENT_NAME_TOKENS)

RULES_TEXT = f"""🧠 WELCOME TO BRAIN BOSS ARENA 🏆
Here are the official rules for today's matchstick challenge:

1️⃣ ENTRY FEE & PAYMENT:
- The entry fee is exactly 5 GHS.
- Send payment to: {RECIPIENT_NUMBER} ({RECIPIENT_DISPLAY_NAME}).
- Upload your clean screenshot receipt here. Our automated system reads MTN, Telecel, and AT networks. (Note: Data bundle or airtime purchase screenshots will be automatically rejected).

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

Send your payment screenshot now to get your access code and race to be first!"""

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


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎟️ Buy Game Ticket", callback_data="buy_ticket")],
        [InlineKeyboardButton("💰 Withdraw Winnings", callback_data="withdraw")],
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
        await query.message.reply_text(RULES_TEXT)
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
    except Exception:
        logger.exception("OCR request failed")
        await message.reply_text(
            "Payment screenshot not recognized. Please make sure the amount is 5 GHS and try again."
        )
        return

    is_valid, txn_id, reason, amount_tier = validate_receipt_text(parsed_text)
    logger.info(
        "OCR validation for user %s: valid=%s reason=%s tier=%s", user.id, is_valid, reason, amount_tier
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
                "Double-check the number and try again."
            ),
        }
        await message.reply_text(
            REASON_MESSAGES.get(
                reason,
                "Payment screenshot not recognized. Please make sure the amount is 5 GHS "
                "(or 2.50 GHS if you're redeeming a discount code) and try again.",
            )
        )
        return

    discount_row = None

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

    if discount_row:
        await message.reply_text(
            f"Discount applied! 🎉 ({discount_row['code']})\n\n"
            f"Payment confirmed! ✅\n\n"
            f"Your access code: {code}\n\n"
            f"Play here: {FRONTEND_URL}\n\n"
            f"This code works once — save it now."
        )
    else:
        await message.reply_text(
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

    with db_lock, contextlib.closing(get_conn()) as conn:
        row = conn.execute("SELECT * FROM codes WHERE code = ?", (code,)).fetchone()

        if not row:
            return jsonify({"valid": False, "message": "Invalid or expired code."}), 404

        if row["status"] != "valid":
            return jsonify({"valid": False, "message": "This code has already been used."}), 410

        play_token = serializer.dumps({"code": code})

        with conn:
            conn.execute(
                "UPDATE codes SET play_token = ? WHERE code = ?",
                (play_token, code),
            )

    return jsonify({"valid": True, "play_token": play_token})


@app.post("/api/submit-answer")
def submit_answer():
    payload = request.get_json(silent=True) or {}
    code = str(payload.get("code", "")).strip()
    play_token = str(payload.get("play_token", "")).strip()
    formula = str(payload.get("formula", "")).strip().replace(" ", "")

    if not re.fullmatch(r"\d{6}", code) or not play_token:
        return jsonify({"success": False, "message": "Missing or malformed request."}), 400

    # 1. Validate the signed token itself (signature + expiry).
    try:
        token_data = serializer.loads(play_token, max_age=PLAY_TOKEN_MAX_AGE_SECONDS)
    except SignatureExpired:
        return jsonify({"success": False, "message": "Your session expired. Re-enter your code."}), 401
    except BadSignature:
        return jsonify({"success": False, "message": "Invalid play token."}), 401

    if token_data.get("code") != code:
        return jsonify({"success": False, "message": "Token does not match code."}), 401

    with db_lock, contextlib.closing(get_conn()) as conn:
        row = conn.execute("SELECT * FROM codes WHERE code = ?", (code,)).fetchone()

        if not row:
            return jsonify({"success": False, "message": "Invalid code."}), 404

        # Defense in depth: token must also match what's currently on file
        # for this code (guards against a stale/regenerated token being replayed).
        if row["play_token"] != play_token:
            return jsonify({"success": False, "message": "This session is no longer valid."}), 401

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

        is_correct = formula == WINNING_FORMULA
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
                        (code, formula),
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
        message = "🏆 Correct — and you're the winner of this round! Check the bot menu to withdraw your winnings."
    elif is_correct and not won_round:
        message = (
            "Correct — but someone else solved it first this round. "
            f"No prize this time, but here's 50% off your next entry: {discount_code}. "
            "Send it with your next payment screenshot to redeem it."
        )
    else:
        message = "That wasn't the right formula."

    if won_round:
        send_channel_message(
            text="🎉 WE HAVE A WINNER! Someone just solved today's Brain Boss matchstick "
            "riddle first and locked in the prize. Payout proof coming soon — stay tuned!"
        )

    if discount_code and row["telegram_user_id"]:
        send_telegram_dm(
            row["telegram_user_id"],
            f"Nice work — 5+4=9 was correct! Someone just beat you to it this round, "
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


# -----------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------

if __name__ == "__main__":
    init_db()

    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    logger.info("Starting Flask API on port %s", PORT)
    app.run(host="0.0.0.0", port=PORT)
