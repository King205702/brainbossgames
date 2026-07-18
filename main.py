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
    TELEGRAM_BOT_TOKEN   - from @BotFather
    OCR_SPACE_API_KEY    - from ocr.space
    FRONTEND_URL         - your Netlify site, e.g. https://brainboss.netlify.app
    SECRET_KEY           - any long random string, used to sign play_tokens
    PORT                 - provided automatically by Render
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

from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes, filters

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
    logger.info("Database ready at %s", DB_PATH)


def generate_unique_code(conn) -> str:
    for _ in range(50):
        candidate = f"{random.randint(0, 999999):06d}"
        row = conn.execute("SELECT 1 FROM codes WHERE code = ?", (candidate,)).fetchone()
        if not row:
            return candidate
    raise RuntimeError("Could not generate a unique 6-digit code after 50 attempts.")


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


# Amount patterns we accept: "5.00", "5.0", "5 GHS", "GHS 5", "GHS 5.00" etc.
AMOUNT_PATTERN = re.compile(
    r"(?:\bGHS\s*5(?:\.0{1,2})?\b)|(?:\b5(?:\.0{1,2})?\s*GHS\b)|(?:\b5\.00\b)|(?:\b5\.0\b)",
    re.IGNORECASE,
)
SUCCESS_PATTERN = re.compile(r"\bsuccess(?:ful)?\b", re.IGNORECASE)

# Transaction / reference ID patterns. MoMo providers format these
# differently — these cover common Ghanaian MTN/Telecel/AirtelTigo phrasing.
# ADJUST THESE to match real screenshots from your provider before going live.
TXN_ID_PATTERNS = [
    re.compile(r"(?:trans(?:action)?\.?\s*id)[:\s]*([A-Za-z0-9\-\.]{6,25})", re.IGNORECASE),
    re.compile(r"(?:financial\s*trans(?:action)?\.?\s*id)[:\s]*([A-Za-z0-9\-\.]{6,25})", re.IGNORECASE),
    re.compile(r"(?:ref(?:erence)?\.?\s*(?:no\.?|number)?)[:\s]*([A-Za-z0-9\-\.]{6,25})", re.IGNORECASE),
    re.compile(r"(?:external\s*(?:trans(?:action)?)?\.?\s*id)[:\s]*([A-Za-z0-9\-\.]{6,25})", re.IGNORECASE),
]


def validate_receipt_text(text: str):
    """
    Returns (is_valid: bool, transaction_id: str | None, reason: str)
    """
    if not SUCCESS_PATTERN.search(text):
        return False, None, "No success confirmation found on the receipt."

    if not AMOUNT_PATTERN.search(text):
        return False, None, "Could not confirm the 5 GHS payment amount."

    txn_id = None
    for pattern in TXN_ID_PATTERNS:
        match = pattern.search(text)
        if match:
            txn_id = match.group(1).strip()
            break

    if not txn_id:
        return False, None, "Could not find a transaction ID on the receipt."

    return True, txn_id, "ok"


# -----------------------------------------------------------------------
# Telegram bot handlers
# -----------------------------------------------------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome to Brain Boss! Pay your 5 GHS entry fee via Mobile Money, "
        "then send a screenshot of the successful payment confirmation here "
        "to receive your access code."
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user = update.effective_user

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

    is_valid, txn_id, reason = validate_receipt_text(parsed_text)
    logger.info("OCR validation for user %s: valid=%s reason=%s", user.id, is_valid, reason)

    if not is_valid:
        await message.reply_text(
            "Payment screenshot not recognized. Please make sure the amount is 5 GHS and try again."
        )
        return

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

        with conn:
            code = generate_unique_code(conn)
            conn.execute(
                """
                INSERT INTO codes (code, transaction_id, status, telegram_user_id)
                VALUES (?, ?, 'valid', ?)
                """,
                (code, txn_id, str(user.id)),
            )

    await message.reply_text(
        f"Payment confirmed! ✅\n\n"
        f"Your access code: {code}\n\n"
        f"Play here: {FRONTEND_URL}\n\n"
        f"This code works once — save it now."
    )


async def handle_other_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send a screenshot of your successful 5 GHS Mobile Money payment to get your access code."
    )


def build_telegram_app() -> Application:
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
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

        if is_correct:
            with conn:
                conn.execute(
                    "INSERT INTO winners (code, formula) VALUES (?, ?)",
                    (code, formula),
                )

    return jsonify({
        "success": True,
        "correct": is_correct,
        "message": "Correct! You are the Brain Boss." if is_correct else "That wasn't the right formula.",
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
