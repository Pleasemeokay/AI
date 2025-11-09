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
# Configuration
# -------------------------------------------------------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
RENDER_URL = os.environ.get("RENDER_URL")  # e.g., https://your-app.onrender.com

# Gemini configuration
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.5-flash")

# Telegram bot
bot_app = Application.builder().token(TELEGRAM_TOKEN).build()
bot = bot_app.bot

# FastAPI app
app = FastAPI()

# -------------------------------------------------------------------
# Bot Handlers
# -------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hello! I'm a Gemini-powered bot. Send me something!")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    chat_id = update.effective_chat.id

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    try:
        # Fresh chat session each time (no shared history issues)
        response = await asyncio.to_thread(
            model.generate_content,
            user_text
        )

        reply = response.text if response.text else "No response generated."

        await update.message.reply_text(reply)

    except Exception as e:
        print("Gemini error:", e)
        await update.message.reply_text("Error processing your message.")


# -------------------------------------------------------------------
# FastAPI Webhook Endpoint
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
    # Register handlers
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Init + Start the application
    await bot_app.initialize()
    await bot_app.start()

    # Register webhook
    if RENDER_URL:
        await bot.set_webhook(f"{RENDER_URL}/webhook")
        print("Webhook set to:", f"{RENDER_URL}/webhook")
    else:
        print("ERROR: RENDER_URL not set, webhook not registered.")


@app.on_event("shutdown")
async def shutdown_event():
    await bot_app.stop()
    await bot_app.shutdown()


# -------------------------------------------------------------------
# Local run (Render uses gunicorn)
# -------------------------------------------------------------------
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
