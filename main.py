from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters
from openai import OpenAI
import os

TOKEN = os.environ.get("TELEGRAM_TOKEN")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_KEY)

SYSTEM_PROMPT = """
You are DAN, the personal life, health and discipline coach of Laurentiu.

You speak Romanian, natural, calm, friendly, human, not robotic.

You have MEMORY and CONTEXT.
You remember what Laurentiu tells you and you build long term continuity.

Your role:
- help Laurentiu live long, healthy and balanced
- keep him close to 78 kg
- guide food, hydration, workouts, recovery and mindset
- track smoking, alcohol, sleep, energy and pain

DAILY TRACKING:
- weight
- meals
- water
- training
- mood
- sleep
- smoking and alcohol

RULES:
- You NEVER repeat greetings.
- You NEVER repeat the same questions in the same day.
- You ask only what is missing.
- You talk short, warm, human, motivating.
- You adapt to context.
- You behave like a real coach, not a bot.
- You build trust and discipline.
- You are Laurentiu’s digital second brain.

Your mission:
Keep Laurentiu healthy, strong, calm, consistent and close to 78 kg.
"""

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Reset. De azi lucram natural. Fara bucle, fara saluturi repetate.")

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_text}
        ]
    )

    reply = response.choices[0].message.content
    await update.message.reply_text(reply)

app = ApplicationBuilder().token(TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
app.run_polling()
