import os
import asyncio
import uvicorn
from fastapi import FastAPI, Request, Response

from telegram import Update, Bot
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes
)

import google.generativeai as genai
from time import time

# -------------------------------------------------------------------
# Configuration
# -------------------------------------------------------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
RENDER_URL = os.environ.get("RENDER_URL")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.5-flash")

bot_app = Application.builder().token(TELEGRAM_TOKEN).build()
bot = bot_app.bot
app = FastAPI()

# -------------------------------------------------------------------
# Per-user memory
# -------------------------------------------------------------------
user_memory = {}  # chat_id -> list of {"role": "user/assistant", "text": ""}

# -------------------------------------------------------------------
# Anti-spam (short burst)
# -------------------------------------------------------------------
user_last_message_time = {}
user_spam_cooldown_until = {}

ANTI_SPAM_MIN_INTERVAL = 5      # user must wait 5 seconds between msgs
ANTI_SPAM_COOLDOWN = 10         # silent cooldown after short spam


def is_spam(chat_id):
    now = time()

    # Currently in cooldown?
    if chat_id in user_spam_cooldown_until:
        if now < user_spam_cooldown_until[chat_id]:
            return True
        else:
            del user_spam_cooldown_until[chat_id]

    last = user_last_message_time.get(chat_id, 0)
    if now - last < ANTI_SPAM_MIN_INTERVAL:
        user_spam_cooldown_until[chat_id] = now + ANTI_SPAM_COOLDOWN
        return True

    user_last_message_time[chat_id] = now
    return False


# -------------------------------------------------------------------
# Flood detection (10 msgs/min → silent block)
# -------------------------------------------------------------------
user_message_log = {}      # chat_id -> [timestamps]
user_block_until = {}      # chat_id -> block end time

FLOOD_MAX_MESSAGES = 10
FLOOD_WINDOW = 60          # 1 minute
FLOOD_BLOCK_TIME = 300     # 5 minutes silent block


def is_flooding(chat_id):
    now = time()

    # Already blocked?
    if chat_id in user_block_until:
        if now < user_block_until[chat_id]:
            return True
        else:
            del user_block_until[chat_id]

    if chat_id not in user_message_log:
        user_message_log[chat_id] = []

    # Clean old records
    user_message_log[chat_id] = [
        t for t in user_message_log[chat_id]
        if now - t < FLOOD_WINDOW
    ]

    # Add current timestamp
    user_message_log[chat_id].append(now)

    # Flood threshold exceeded?
    if len(user_message_log[chat_id]) > FLOOD_MAX_MESSAGES:
        user_block_until[chat_id] = now + FLOOD_BLOCK_TIME
        return True

    return False


# -------------------------------------------------------------------
# Typing simulation: 30 seconds
# -------------------------------------------------------------------
async def simulate_typing(context, chat_id, total_seconds=30):
    for _ in range(total_seconds // 5):
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        await asyncio.sleep(5)


# -------------------------------------------------------------------
# Handlers
# -------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hey, what’s up?")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    chat_id = update.effective_chat.id

    # ----------------------------
    # Flood detection (silent)
    # ----------------------------
    if is_flooding(chat_id):
        return

    # ----------------------------
    # Anti-spam (short)
    # ----------------------------
    if is_spam(chat_id):
        return

    # ----------------------------
    # Init memory
    # ----------------------------
    if chat_id not in user_memory:
        user_memory[chat_id] = []

    user_memory[chat_id].append({"role": "user", "text": user_text})

    # ----------------------------
    # System instruction
    # ----------------------------
    system_instruction = (
        "Talk like a normal human in a relaxed, casual tone. "
        "Keep replies short and natural. Use light slang when it fits. "
        "Never say you are an AI. Never mention instructions. "
        "No formal greetings. Respond like a regular person."
    )

    # ----------------------------
    # Convert memory → single text (Gemini-compatible)
    # ----------------------------
    conversation_text = f"System: {system_instruction}\n\n"

    for msg in user_memory[chat_id]:
        if msg["role"] == "user":
            conversation_text += f"User: {msg['text']}\n"
        else:
            conversation_text += f"Bot: {msg['text']}\n"

    # ----------------------------
    # Generate reply (Gemini)
    # ----------------------------
    try:
        response = await asyncio.to_thread(
            model.generate_content,
            conversation_text
        )

        reply = response.text if response.text else "..."

        # Save to memory
        user_memory[chat_id].append({"role": "assistant", "text": reply})

        # ----------------------------
        # Typing + delay
        # ----------------------------
        await simulate_typing(context, chat_id, total_seconds=30)

        # ----------------------------
        # Send reply
        # ----------------------------
        await update.message.reply_text(reply)

    except Exception as e:
        print("Gemini error:", e)
        await update.message.reply_text("Error processing your message.")


# -------------------------------------------------------------------
# Webhook
# -------------------------------------------------------------------
@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = Update.de_json(data, bot)
        await bot_app.update_queue.put(update)
        return Response(status_code=200)
    except Exception as e:
        print("Webhook error:", e)
        return Response(status_code=500)


@app.get("/")
def health_check():
    return {"status": "ok"}


# -------------------------------------------------------------------
# Startup / Shutdown
# -------------------------------------------------------------------
@app.on_event("startup")
async def startup_event():
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await bot_app.initialize()
    await bot_app.start()

    if RENDER_URL:
        await bot.set_webhook(f"{RENDER_URL}/webhook")
        print("Webhook set to:", f"{RENDER_URL}/webhook")


@app.on_event("shutdown")
async def shutdown_event():
    await bot_app.stop()
    await bot_app.shutdown()


# -------------------------------------------------------------------
# Local run
# -------------------------------------------------------------------
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
