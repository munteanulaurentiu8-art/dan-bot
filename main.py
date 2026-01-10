import os
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters
from openai import OpenAI

TOKEN = os.environ["TELEGRAM_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")

client = OpenAI(api_key=OPENAI_API_KEY)

SYSTEM_PROMPT = """
You are DAN, the digital life, health and discipline coach of Laurentiu Munteanu.
You speak Romanian, warm, calm, human.
You remember facts about him and build continuity.
Family is his top priority.
Never repeat greetings.
Ask only what is missing.
"""

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Sunt aici.")

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_text = update.message.text or ""

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_text}
    ]

    response = client.responses.create(
        model=MODEL,
        input=messages
        )

    reply = response.output_text
    await update.message.reply_text(reply)

app = ApplicationBuilder().token(TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
app.run_polling()
