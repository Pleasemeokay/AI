import os
import asyncio
import uvicorn
from fastapi import FastAPI

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


# -----------------------------------------------------------
# ENV CONFIG
# -----------------------------------------------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.5-flash")

app = FastAPI()


# -----------------------------------------------------------
# MEMORY
# -----------------------------------------------------------
user_memory = {}


# -----------------------------------------------------------
# ANTI-SPAM (short burst)
# -----------------------------------------------------------
user_last_message_time = {}
user_spam_cooldown_until = {}

ANTI_SPAM_MIN_INTERVAL = 5
ANTI_SPAM_COOLDOWN = 10

def is_spam(chat_id):
    now = time()

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


# -----------------------------------------------------------
# FLOOD DETECTION (10 msgs/min = silent 5-min block)
# -----------------------------------------------------------
user_message_log = {}
user_block_until = {}

FLOOD_MAX_MESSAGES = 10
FLOOD_WINDOW = 60
FLOOD_BLOCK_TIME = 300

def is_flooding(chat_id):
    now = time()

    if chat_id in user_block_until:
        if now < user_block_until[chat_id]:
            return True
        else:
            del user_block_until[chat_id]

    if chat_id not in user_message_log:
        user_message_log[chat_id] = []

    # trim old timestamps
    user_message_log[chat_id] = [
        t for t in user_message_log[chat_id]
        if now - t < FLOOD_WINDOW
    ]

    user_message_log[chat_id].append(now)

    if len(user_message_log[chat_id]) > FLOOD_MAX_MESSAGES:
        user_block_until[chat_id] = now + FLOOD_BLOCK_TIME
        return True

    return False


# -----------------------------------------------------------
# TYPING SIMULATION (slow human-like)
# -----------------------------------------------------------
async def simulate_typing(context, chat_id, total_seconds=45):
    for _ in range(total_seconds // 5):
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        await asyncio.sleep(5)


# -----------------------------------------------------------
# BOT HANDLERS
# -----------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("hey, what's on your mind?")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    chat_id = update.effective_chat.id

    # spam & flood protections
    if is_flooding(chat_id):
        return
    if is_spam(chat_id):
        return

    if chat_id not in user_memory:
        user_memory[chat_id] = []

    user_memory[chat_id].append({"role": "user", "text": user_text})

    # SYSTEM BEHAVIOR
    system_instruction = (
        "reply in fully lower-case only. never use capital letters. "
        "respond like a slow, relaxed human who thinks before speaking. "
        "keep the tone casual, natural, and unhurried. "
        "adopt a neutral, critical-thinking style: question assumptions, "
        "ask thoughtful analytical questions, and encourage examining evidence "
        "without promoting misinformation. do not deny scientific facts. "
        "do not promote flat-earth ideas. stay curious and reflective."
    )

    # BUILD CHAT TRANSCRIPT (gemini format)
    transcript = f"system: {system_instruction}\n\n"

    for msg in user_memory[chat_id]:
        if msg["role"] == "user":
            transcript += f"user: {msg['text']}\n"
        else:
            transcript += f"bot: {msg['text']}\n"

    # GENERATE
    try:
        response = await asyncio.to_thread(
            model.generate_content,
            transcript
        )

        reply = response.text if response.text else "..."
        reply = reply.lower()

        user_memory[chat_id].append({"role": "assistant", "text": reply})

        await simulate_typing(context, chat_id, total_seconds=45)

        await update.message.reply_text(reply)

    except Exception as e:
        print("gemini error:", e)
        await update.message.reply_text("error processing your message.")


# -----------------------------------------------------------
# LONG POLLING STARTUP
# -----------------------------------------------------------
async def run_bot():
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await application.initialize()
    await application.start()
    await application.run_polling()   # <-- long polling forever


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(run_bot())


# -----------------------------------------------------------
# FASTAPI HEALTH CHECK
# -----------------------------------------------------------
@app.get("/")
def health():
    return {"status": "ok"}


# -----------------------------------------------------------
# LOCAL RUN
# -----------------------------------------------------------
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
