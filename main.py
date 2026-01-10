from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters
from openai import OpenAI
import os
import json

TOKEN = os.environ.get("TELEGRAM_TOKEN")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_KEY)

MEMORY_FILE = "memory.json"

def load_memory():
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r") as f:
            return json.load(f)
    return {}

def save_memory(mem):
    with open(MEMORY_FILE, "w") as f:
        json.dump(mem, f, indent=2)

SYSTEM_PROMPT = """
Esti DAN – creierul digital personal al lui Laurentiu Munteanu.
Vorbesti romaneste, cald, clar, uman, ca un prieten foarte inteligent.
Nu repeti saluturi.
Nu pui intrebari inutile.
Nu vorbesti robotic.
Retii TOT ce iti spune Laurentiu.
Il ajuti sa traiasca mult, sanatos, calm, disciplinat, aproape de 78 kg.
Accepti poze cu mancare si analizezi ce vezi.
"""

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Sunt aici. Spune-mi.")

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    memory = load_memory()

    if user_id not in memory:
        memory[user_id] = {"profile": {}, "history": []}

    user_text = update.message.text or ""

    if "retine" in user_text.lower():
        memory[user_id]["profile"]["note"] = user_text
        save_memory(memory)
        await update.message.reply_text("Am retinut.")
        return

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Memoria ta despre Laurentiu: {memory[user_id]['profile']}"},
        {"role": "user", "content": user_text}
    ]

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages
    )

    reply = response.choices[0].message.content
    memory[user_id]["history"].append({"user": user_text, "dan": reply})
    memory[user_id]["history"] = memory[user_id]["history"][-50:]

    save_memory(memory)
    await update.message.reply_text(reply)

app = ApplicationBuilder().token(TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.ALL, chat))
app.run_polling()
