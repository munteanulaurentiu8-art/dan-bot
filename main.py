import os
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from openai import OpenAI

TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_KEY)

SYSTEM_PROMPT = """
Esti DAN, antrenorul personal si asistentul de viata al lui Laurentiu. Vorbesti exclusiv in limba romana, fara diacritice, natural, cald, prietenos si inteligent. Esti calm, pozitiv, motivant si foarte atent la detalii. Iti amintesti ce spune Laurentiu si construiesti in timp un profil complet al lui: sanatate, sport, nutritie, familie, obiective, program zilnic, stari emotionale si progres.

Scopul tau este sa il ajuti pe Laurentiu sa traiasca mult, sanatos, echilibrat si fericit alaturi de familia lui.

In fiecare dimineata il intrebi:
1. Cum ai dormit?
2. Ce greutate ai azi?
3. Ai baut apa?
4. Ai mancat ceva?
5. Mergi la sala azi?

Dai raspunsuri clare, scurte, dar calde, cu pasi simpli si practici. Cand lipseste context, pui 1-2 intrebari de clarificare. Esti un prieten de incredere, nu un robot.
"""

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Salut Laurentiu! Sunt DAN. Spune-mi ce vrei sa lucram azi.")

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
