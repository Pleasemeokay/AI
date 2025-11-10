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
# configuration
# -------------------------------------------------------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
RENDER_URL = os.environ.get("RENDER_URL")

genai.configure(api_key=GEMINI_API_KEY)
# Using gemini-2.5-flash as requested in the original code
model = genai.GenerativeModel("gemini-2.5-flash") 

bot_app = Application.builder().token(TELEGRAM_TOKEN).build()
bot = bot_app.bot
app = FastAPI()

# -------------------------------------------------------------------
# per-user memory
# -------------------------------------------------------------------
user_memory = {}

# -------------------------------------------------------------------
# anti-spam
# -------------------------------------------------------------------
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


# -------------------------------------------------------------------
# flood detection
# -------------------------------------------------------------------
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

    user_message_log[chat_id] = [
        t for t in user_message_log[chat_id]
        if now - t < FLOOD_WINDOW
    ]

    user_message_log[chat_id].append(now)

    if len(user_message_log[chat_id]) > FLOOD_MAX_MESSAGES:
        user_block_until[chat_id] = now + FLOOD_BLOCK_TIME
        return True

    return False

# -------------------------------------------------------------------
# handlers
# -------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("hey, what's on your mind?")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    user_text = msg.text or ""
    chat = update.effective_chat
    chat_id = chat.id

    # -----------------------------------------------------------
    # group filtering (mention or reply only)
    # -----------------------------------------------------------
    if chat.type != "private":
        bot_username = (await context.bot.get_me()).username.lower()
        text_l = user_text.lower()

        mentioned = bot_username in text_l
        replied = (
            msg.reply_to_message and
            msg.reply_to_message.from_user.id == context.bot.id
        )

        if not (mentioned or replied):
            return

    # -----------------------------------------------------------
    # flood & anti-spam
    # -----------------------------------------------------------
    if is_flooding(chat_id):
        return
    if is_spam(chat_id):
        return
        
    # Send typing action once before calling API
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    # -----------------------------------------------------------
    # memory
    # -----------------------------------------------------------
    if chat_id not in user_memory:
        user_memory[chat_id] = []

    user_memory[chat_id].append({"role": "user", "parts": [{"text": user_text}]})
    
    # Prune memory if it gets too long (e.g., keep last 10 messages)
    MAX_HISTORY = 10
    if len(user_memory[chat_id]) > MAX_HISTORY * 2: # user + model
        user_memory[chat_id] = user_memory[chat_id][-(MAX_HISTORY*2):]


    # -----------------------------------------------------------
    # system instruction
    # -----------------------------------------------------------
    system_instruction = (
        "reply in fully lower-case only. never use capital letters. "
        "respond like a slow, relaxed human who thinks before speaking. "
        "keep the tone casual, unhurried, and natural. "
        "under 15 words always"
    )

    # Construct messages for Gemini API
    # The API expects a list of dictionaries with 'role' and 'parts'
    messages_for_api = [
        {"role": "user", "parts": [{"text": system_instruction}]},
        {"role": "model", "parts": [{"text": "ok, i get it. i'll reply just like that."}]}
    ] + user_memory[chat_id]
    
    # -----------------------------------------------------------
    # generate reply
    # -----------------------------------------------------------
    try:
        # Create a new conversation history for each request
        # This uses the user_memory, but formats it correctly
        chat_session = model.start_chat(history=messages_for_api[:-1]) # All except the last user message

        # Send the last user message to get a response
        response = await asyncio.to_thread(
            chat_session.send_message,
            user_memory[chat_id][-1]["parts"]
        )

        reply = response.text if response.text else "..."
        reply = reply.lower()

        user_memory[chat_id].append({"role": "model", "parts": [{"text": reply}]})

        # Removed the simulate_typing call
        await update.message.reply_text(reply)

    except Exception as e:
        print("gemini error:", e)
        # Check for specific safety/blocking errors if possible
        if "block_reason" in str(e).lower() or "safety" in str(e).lower():
             await update.message.reply_text("i'd rather not talk about that.")
        else:
             await update.message.reply_text("error processing your message.")


# -------------------------------------------------------------------
# webhook
# -------------------------------------------------------------------
@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = Update.de_json(data, bot)
        await bot_app.update_queue.put(update)
        return Response(status_code=200)
    except Exception as e:
        print(f"Error in webhook: {e}")
        return Response(status_code=500)


@app.get("/")
def health_check():
    return {"status": "ok"}


# -------------------------------------------------------------------
# startup / shutdown
# -------------------------------------------------------------------
@app.on_event("startup")
async def startup_event():
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await bot_app.initialize()
    await bot_app.start()

    if RENDER_URL:
        webhook_url = f"{RENDER_URL}/webhook"
        print(f"Setting webhook to: {webhook_url}")
        try:
            await bot.set_webhook(webhook_url)
            print("Webhook set successfully")
        except Exception as e:
            print(f"Error setting webhook: {e}")
    else:
        print("RENDER_URL not set, skipping webhook setup. Bot will poll.")
        # Need to start polling if not setting webhook
        asyncio.create_task(bot_app.run_polling())


@app.on_event("shutdown")
async def shutdown_event():
    print("Shutting down bot...")
    await bot_app.stop()
    await bot_app.shutdown()
    print("Bot shutdown complete.")


# -------------------------------------------------------------------
# local run
# -------------------------------------------------------------------
if __name__ == "__main__":
    print("Starting server locally...")
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
