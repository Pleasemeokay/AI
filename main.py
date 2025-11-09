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

# -------------------------------------------------------------------
# 📜 Configuration
# -------------------------------------------------------------------
# Get credentials from Railway Environment Variables
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# This is the public URL Railway gives your service
# e.g., https://my-bot-app.up.railway.app
RAILWAY_PUBLIC_URL = os.environ.get("RAILWAY_PUBLIC_URL")

# 💎 Gemini configuration
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-pro-latest")

# 🤖 Telegram bot
bot_app = Application.builder().token(TELEGRAM_TOKEN).build()
bot = bot_app.bot

# 🚀 FastAPI app
app = FastAPI()

# -------------------------------------------------------------------
# 🤖 Bot Handlers
# -------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a welcome message when the /start command is issued."""
    await update.message.reply_text("Hello! I'm a Gemini-powered bot. Send me a message!")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles text messages and replies with a Gemini response."""
    user_text = update.message.text
    chat_id = update.effective_chat.id

    # Show "typing..." action
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    try:
        # Send text to Gemini
        # Note: Using to_thread for the blocking (non-async) SDK call
        response = await asyncio.to_thread(
            model.generate_content,
            user_text
        )

        reply = response.text
        
        # Handle empty responses
        if not reply:
            reply = "I'm not sure how to respond to that."

        await update.message.reply_text(reply)

    except Exception as e:
        print(f"Error processing Gemini request: {e}")
        await update.message.reply_text("Sorry, I encountered an error while processing your message.")


# -------------------------------------------------------------------
# 🌐 FastAPI Webhook Endpoint
# -------------------------------------------------------------------
@app.post("/webhook")
async def telegram_webhook(request: Request):
    """This endpoint receives updates from Telegram."""
    try:
        data = await request.json()
        update = Update.de_json(data, bot)
        await bot_app.update_queue.put(update)
        return Response(status_code=200)
    except Exception as e:
        print(f"Webhook error: {e}")
        return Response(status_code=500)

@app.get("/")
def health_check():
    """A simple health check endpoint."""
    return {"status": "ok", "bot_initialized": bot_app.initialized}

# -------------------------------------------------------------------
# 🚀 Startup / Shutdown Events
# -------------------------------------------------------------------
@app.on_event("startup")
async def startup_event():
    """On startup, register handlers and set the webhook."""
    # Register handlers
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Initialize the bot application
    await bot_app.initialize()
    # Start the background tasks (like processing updates from the queue)
    await bot_app.start()

    # Register webhook
    if RAILWAY_PUBLIC_URL:
        webhook_url = f"{RAILWAY_PUBLIC_URL}/webhook"
        await bot.set_webhook(webhook_url)
        print(f"Webhook set to: {webhook_url}")
    else:
        print("ERROR: RAILWAY_PUBLIC_URL not set. Webhook not registered.")

@app.on_event("shutdown")
async def shutdown_event():
    """On shutdown, stop the bot application."""
    await bot_app.stop()
    await bot_app.shutdown()
