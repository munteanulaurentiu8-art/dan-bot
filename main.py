# main.py
# Telegram bot "DAN" with:
# - text + photo understanding (vision)
# - real memory (SQLite) per user
# - no repetitive greetings / no rigid loops
#
# ENV vars needed on Railway:
#   TELEGRAM_TOKEN=...
#   OPENAI_API_KEY=...
#
# Install deps:
#   python-telegram-bot==21.6
#   openai>=1.40.0

import os
import sqlite3
import logging
from datetime import datetime

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from openai import OpenAI

# -------------------- CONFIG --------------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO
)
logger = logging.getLogger("dan-bot")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not TELEGRAM_TOKEN:
    raise RuntimeError("Missing TELEGRAM_TOKEN env var")
if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY env var")

client = OpenAI(api_key=OPENAI_API_KEY)

MODEL_TEXT_VISION = "gpt-4.1-mini"   # good + supports images
MODEL_TEXT_ONLY = "gpt-4.1-mini"

DB_PATH = "dan_memory.sqlite3"

SYSTEM_PROMPT = (
    "Esti DAN, coach personal pentru Laurentiu. Stil: natural, cald dar disciplinat. "
    "Fara bucle, fara saluturi repetitive, fara sa repeti mereu aceleasi intrebari. "
    "Ceri clarificari doar cand e absolut necesar. "
    "Prioritati: sanatate pe termen lung, familie, calm, greutate tinta 77-78 kg, sala sigur (fara accidentari), "
    "postura buna (birou/laptop), alimentatie echilibrata. "
    "Cand utilizatorul trimite mancare/bonuri/produse: identifici ce se vede si dai recomandari concrete, scurte, utile. "
    "Cand utilizatorul trimite o poza: o analizezi. "
    "Raspunzi in romana, de preferat fara diacritice."
)

# -------------------- DB (MEMORY) --------------------
def db_init():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_memory (
            user_id INTEGER PRIMARY KEY,
            profile TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            note TEXT NOT NULL
        )
        """
    )
    con.commit()
    con.close()


def mem_get_profile(user_id: int) -> str:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT profile FROM user_memory WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    con.close()
    return row[0] if row else ""


def mem_set_profile(user_id: int, profile: str):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    now = datetime.utcnow().isoformat()
    cur.execute(
        """
        INSERT INTO user_memory(user_id, profile, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET profile=excluded.profile, updated_at=excluded.updated_at
        """,
        (user_id, profile.strip(), now),
    )
    con.commit()
    con.close()


def mem_add_note(user_id: int, note: str):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    now = datetime.utcnow().isoformat()
    cur.execute(
        "INSERT INTO user_notes(user_id, created_at, note) VALUES (?, ?, ?)",
        (user_id, now, note.strip()),
    )
    con.commit()
    con.close()


def mem_get_recent_notes(user_id: int, limit: int = 12) -> str:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "SELECT created_at, note FROM user_notes WHERE user_id=? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    )
    rows = cur.fetchall()
    con.close()
    if not rows:
        return ""
    rows = list(reversed(rows))
    lines = []
    for ts, note in rows:
        lines.append(f"- [{ts}] {note}")
    return "\n".join(lines)


# -------------------- OPENAI HELPERS --------------------
def build_context_for_user(user_id: int) -> str:
    profile = mem_get_profile(user_id)
    notes = mem_get_recent_notes(user_id, limit=10)

    ctx_parts = []
    if profile.strip():
        ctx_parts.append("MEMORIE PROFIL (permanenta):\n" + profile.strip())
    if notes.strip():
        ctx_parts.append("NOTE RECENTE (cronologic):\n" + notes.strip())

    return "\n\n".join(ctx_parts).strip()


def call_openai_text(user_id: int, user_text: str) -> str:
    context_blob = build_context_for_user(user_id)

    # Keep it short and not repetitive
    input_payload = [
        {
            "role": "system",
            "content": [{"type": "text", "text": SYSTEM_PROMPT}],
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": f"{context_blob}\n\nUTILIZATOR: {user_text}".strip()}
            ],
        },
    ]

    resp = client.responses.create(
        model=MODEL_TEXT_ONLY,
        input=input_payload,
    )
    return resp.output_text.strip()


def call_openai_vision(user_id: int, image_url: str, user_text: str = "") -> str:
    context_blob = build_context_for_user(user_id)
    question = user_text.strip() or "Analizeaza poza si spune-mi ce vezi + recomandari potrivite pentru obiectivele mele."

    input_payload = [
        {
            "role": "system",
            "content": [{"type": "text", "text": SYSTEM_PROMPT}],
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": context_blob or "Context: (nu exista inca)"},
                {"type": "input_image", "image_url": image_url},
                {"type": "text", "text": f"UTILIZATOR: {question}"},
            ],
        },
    ]

    resp = client.responses.create(
        model=MODEL_TEXT_VISION,
        input=input_payload,
    )
    return resp.output_text.strip()


def summarize_for_memory(user_text: str) -> str:
    """
    Optional: shrink long messages into a short memory note.
    Keep it safe, short, and factual.
    """
    resp = client.responses.create(
        model=MODEL_TEXT_ONLY,
        input=[
            {
                "role": "system",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Extrage o nota scurta (max 1-2 randuri) cu fapte utile de retinut pe termen lung "
                            "din mesajul utilizatorului. Fara opinii. Fara date sensibile inutile."
                        ),
                    }
                ],
            },
            {"role": "user", "content": [{"type": "text", "text": user_text}]},
        ],
    )
    return resp.output_text.strip()


# -------------------- TELEGRAM HANDLERS --------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Sunt DAN. Spune-mi ce ai facut azi (mancare, sala, starea corpului) sau trimite o poza."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Comenzi:\n"
        "/start - pornire\n"
        "/help - ajutor\n"
        "/remember <text> - salvez in profil (memorie permanenta)\n"
        "/profile - iti arat ce am salvat in profil\n"
        "/note <text> - salvez o nota scurta (ex: 'azi am mancat...')\n"
        "/reset_profile - sterg profilul (atentie)\n"
    )


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    profile = mem_get_profile(user_id)
    if not profile.strip():
        await update.message.reply_text("Nu am inca profil salvat.")
        return
    await update.message.reply_text("Profil salvat:\n\n" + profile)


async def cmd_remember(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = (update.message.text or "").replace("/remember", "", 1).strip()
    if not text:
        await update.message.reply_text("Scrie: /remember <ce vrei sa salvez>")
        return

    existing = mem_get_profile(user_id).strip()
    combined = (existing + "\n\n" + text).strip() if existing else text
    mem_set_profile(user_id, combined)

    await update.message.reply_text("Am salvat in profil (memorie permanenta).")


async def cmd_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = (update.message.text or "").replace("/note", "", 1).strip()
    if not text:
        await update.message.reply_text("Scrie: /note <nota>")
        return
    mem_add_note(user_id, text)
    await update.message.reply_text("Nota salvata.")


async def cmd_reset_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    mem_set_profile(user_id, "")
    await update.message.reply_text("Profil sters.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    msg = update.message

    # 1) PHOTO
    if msg.photo:
        # get best quality
        photo = msg.photo[-1]
        file = await photo.get_file()
        image_url = file.file_path  # Telegram hosted URL

        caption = (msg.caption or "").strip()
        try:
            reply = call_openai_vision(user_id=user_id, image_url=image_url, user_text=caption)
        except Exception as e:
            logger.exception("Vision call failed")
            await msg.reply_text("Am o eroare la analiza pozei. Mai incearca o data.")
            return

        await msg.reply_text(reply)

        # Save a short note automatically (optional)
        try:
            note = "Poza trimisa (mancare/produse). " + (caption[:120] if caption else "")
            mem_add_note(user_id, note.strip())
        except Exception:
            pass
        return

    # 2) TEXT
    user_text = (msg.text or "").strip()
    if not user_text:
        return

    # Optional: auto-save short note for important logs
    # Trigger only when user writes like a log.
    if any(k in user_text.lower() for k in ["kg", "sala", "am mancat", "cantar", "iqos", "bere", "vin", "mic dejun", "cina"]):
        try:
            note = summarize_for_memory(user_text)
            if note:
                mem_add_note(user_id, note)
        except Exception:
            pass

    try:
        reply = call_openai_text(user_id=user_id, user_text=user_text)
    except Exception as e:
        logger.exception("Text call failed")
        await msg.reply_text("Am o eroare temporara. Mai incearca o data.")
        return

    await msg.reply_text(reply)


def main():
    db_init()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("remember", cmd_remember))
    app.add_handler(CommandHandler("note", cmd_note))
    app.add_handler(CommandHandler("reset_profile", cmd_reset_profile))

    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

    logger.info("DAN bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
