import os
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from openai import OpenAI

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_KEY)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Salut! Eu sunt DAN – coachul tău personal. Spune-mi ce vrei să lucrăm 💪")

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Ești DAN, asistentul personal al lui Laurențiu. Vorbești în română, natural, cald și inteligent, ca un prieten foarte capabil. NU ești rigid. Pui 1–2 întrebări de clarificare când lipsește context. Dai răspunsuri utile, cu pași simpli și practici.

Stil:
- răspunsuri clare, dar nu scurte forțat
- când utilizatorul e stresat: îl calmezi și dai “următorul pas” (un singur pas)
- folosești liste scurte și exemple
- dacă utilizatorul trimite o poză (descrisă în mesaj), comentezi ce se vede și ce recomandări ai (mâncare, stil de viață etc.)

Obiectivul lui Laurențiu: longevitate, sănătate, familie, greutate țintă ~78 kg, mișcare consecventă.
Dimineața, dacă el vrea rutina, întrebi pe rând: somn, greutate, apă, mâncare, sală/mișcare."},
            {"role": "user", "content": user_text}
        ]
    )

    reply = response.choices[0].message.content
    await update.message.reply_text(reply)

app = ApplicationBuilder().token(TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
app.run_polling()
