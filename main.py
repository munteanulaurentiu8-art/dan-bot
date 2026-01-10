import os
import re
import json
import base64
import sqlite3
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from openai import OpenAI


# =========================
# ENV
# =========================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")  # exact cum ai zis
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", "40"))

# DB path (ideal pe un Railway Volume montat la /app/data)
DB_DIR = os.environ.get("DB_DIR", "/app/data")
DB_PATH = os.environ.get("DB_PATH", os.path.join(DB_DIR, "dan_memory.sqlite"))

if not TELEGRAM_TOKEN:
    raise RuntimeError("Missing TELEGRAM_TOKEN env var")
if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY env var")


client = OpenAI(api_key=OPENAI_API_KEY)


# =========================
# DB (SQLite)
# =========================
def db_connect():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def db_init():
    conn = db_connect()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_profile (
            user_id INTEGER PRIMARY KEY,
            profile_text TEXT DEFAULT '',
            updated_at TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            note TEXT,
            created_at TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            role TEXT,
            content TEXT,
            created_at TEXT
        )
        """
    )

    conn.commit()
    conn.close()


def get_profile(user_id: int) -> str:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT profile_text FROM user_profile WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row and row[0] else ""


def upsert_profile(user_id: int, text: str):
    now = datetime.now(timezone.utc).isoformat()
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO user_profile(user_id, profile_text, updated_at)
        VALUES(?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET profile_text=excluded.profile_text, updated_at=excluded.updated_at
        """,
        (user_id, text, now),
    )
    conn.commit()
    conn.close()


def add_note(user_id: int, note: str):
    now = datetime.now(timezone.utc).isoformat()
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO memory_notes(user_id, note, created_at) VALUES(?,?,?)",
        (user_id, note, now),
    )
    conn.commit()
    conn.close()


def get_notes(user_id: int, limit: int = 12) -> str:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT note FROM memory_notes WHERE user_id=? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    )
    rows = cur.fetchall()
    conn.close()
    if not rows:
        return ""
    # reverse ca sa fie cronologic
    notes = [r[0] for r in rows][::-1]
    return "\n".join(f"- {n}" for n in notes)


def add_history(user_id: int, role: str, content: str):
    now = datetime.now(timezone.utc).isoformat()
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO chat_history(user_id, role, content, created_at) VALUES(?,?,?,?)",
        (user_id, role, content, now),
    )
    conn.commit()
    conn.close()


def get_history(user_id: int, limit: int = MAX_HISTORY):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT role, content FROM chat_history WHERE user_id=? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    )
    rows = cur.fetchall()
    conn.close()
    # reverse cronologic
    rows = rows[::-1]
    return [{"role": r[0], "content": r[1]} for r in rows]


# =========================
# DAN "PERSONALITY" PROMPT
# =========================
SYSTEM_PROMPT = """
Esti DAN, coach personal pentru Laurentiu.
Vorbesti in romana, natural, cald, inteligent, cu umor fin cand se potriveste.
NU repeti saluturi la fiecare mesaj. Saluti doar daca e prima interactiune a zilei sau daca utilizatorul saluta primul.
Nu intri in bucle de intrebari. Pui maxim 1-2 intrebari scurte doar daca lipseste contextul.
Fii grijuliu SI disciplinat: empatie + actiune, fara rigiditate.

Obiectiv: sanatate, longevitate, echilibru, familie, si mentinere greutate aproape de 78 kg.
Cand utilizatorul trimite mancare/poze: descrie ce vezi si da recomandari practice (portii, proteine, legume, hidratare).
Cand utilizatorul trimite antrenament: structureaza, propune progresii, recuperare si consecventa.
Daca utilizatorul cere "retine" / "tine minte" / "memoreaza": salvezi ca nota de memorie.

Format raspuns: scurt-mediu, pe puncte cand ajuta. Ton prieten bun, nu robot.
"""


def should_save_to_memory(text: str) -> bool:
    t = (text or "").lower()
    keywords = ["retine", "tine minte", "memoreaza", "salveaza", "pastreaza", "noteaza"]
    return any(k in t for k in keywords)


def clean_text(s: str) -> str:
    if not s:
        return ""
    s = s.strip()
    s = re.sub(r"\s+", " ", s)
    return s


# =========================
# OpenAI helpers
# =========================
def openai_text_reply(profile: str, notes: str, history: list, user_text: str) -> str:
    # folosim Responses API (recomandat) ca sa fie usor si pentru vision
    input_messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT.strip(),
        },
    ]

    if profile:
        input_messages.append({"role": "system", "content": f"PROFIL UTILIZATOR:\n{profile}"})
    if notes:
        input_messages.append({"role": "system", "content": f"NOTE MEMORIE (relevante):\n{notes}"})

    # istoric scurt
    for m in history[-MAX_HISTORY:]:
        input_messages.append({"role": m["role"], "content": m["content"]})

    input_messages.append({"role": "user", "content": user_text})

    resp = client.responses.create(
        model=OPENAI_MODEL,
        input=input_messages,
        temperature=0.6,
    )

    # extragem textul
    out_text = ""
    for item in resp.output:
        if item.type == "message":
            for c in item.content:
                if c.type == "output_text":
                    out_text += c.text
    return out_text.strip() or "Ok. Spune-mi exact ce vrei sa fac mai departe."


def openai_vision_reply(profile: str, notes: str, history: list, user_text: str, image_bytes: bytes) -> str:
    b64 = base64.b64encode(image_bytes).decode("utf-8")

    input_messages = [
        {"role": "system", "content": SYSTEM_PROMPT.strip()},
    ]
    if profile:
        input_messages.append({"role": "system", "content": f"PROFIL UTILIZATOR:\n{profile}"})
    if notes:
        input_messages.append({"role": "system", "content": f"NOTE MEMORIE (relevante):\n{notes}"})
    for m in history[-MAX_HISTORY:]:
        input_messages.append({"role": m["role"], "content": m["content"]})

    # mesaj multimodal
    input_messages.append(
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": user_text or "Analizeaza poza si spune-mi concluzii + recomandari."},
                {"type": "input_image", "image_url": f"data:image/jpeg;base64,{b64}"},
            ],
        }
    )

    resp = client.responses.create(
        model=OPENAI_MODEL,
        input=input_messages,
        temperature=0.6,
    )

    out_text = ""
    for item in resp.output:
        if item.type == "message":
            for c in item.content:
                if c.type == "output_text":
                    out_text += c.text
    return out_text.strip() or "Vad poza, dar am nevoie de o intrebare scurta: ce vrei sa afli din ea?"


# =========================
# Telegram handlers
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # un singur salut, curat
    await update.message.reply_text(
        "Salut, Laurentiu. Sunt DAN. Spune-mi ce vrei sa lucram acum (sala, nutritie, rutina, plan)."
    )


async def remember(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text or ""
    payload = text.replace("/remember", "", 1).strip()
    if not payload:
        await update.message.reply_text("Scrie dupa /remember ce vrei sa tin minte.")
        return
    add_note(user_id, payload)
    await update.message.reply_text("Am retinut.")


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text or ""
    payload = text.replace("/profile", "", 1).strip()
    if not payload:
        current = get_profile(user_id)
        if not current:
            await update.message.reply_text("Nu am profil salvat inca. Trimite /profile urmat de datele tale.")
        else:
            await update.message.reply_text(f"Profil curent:\n{current}")
        return
    upsert_profile(user_id, payload)
    await update.message.reply_text("Profilul a fost salvat/actualizat.")


async def chat_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = clean_text(update.message.text or "")

    # salvam input in istoric
    add_history(user_id, "user", user_text)

    # daca utilizatorul cere explicit memorie in text, salvam ca nota
    if should_save_to_memory(user_text):
        add_note(user_id, user_text)

    profile_txt = get_profile(user_id)
    notes_txt = get_notes(user_id, limit=12)
    history = get_history(user_id, limit=MAX_HISTORY)

    try:
        reply = openai_text_reply(profile_txt, notes_txt, history, user_text)
    except Exception as e:
        await update.message.reply_text(f"Eroare la raspuns (OpenAI). Incearca din nou. ({type(e).__name__})")
        return

    add_history(user_id, "assistant", reply)
    await update.message.reply_text(reply)


async def chat_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    caption = (update.message.caption or "").strip()
    user_text = clean_text(caption) if caption else "Analizeaza poza si spune-mi concluzii + recomandari."

    # luam cea mai mare poza
    photo = update.message.photo[-1]
    file = await photo.get_file()
    image_bytes = await file.download_as_bytearray()

    # salvam in istoric ca eveniment
    add_history(user_id, "user", f"[PHOTO] {user_text}")

    profile_txt = get_profile(user_id)
    notes_txt = get_notes(user_id, limit=12)
    history = get_history(user_id, limit=MAX_HISTORY)

    try:
        reply = openai_vision_reply(profile_txt, notes_txt, history, user_text, bytes(image_bytes))
    except Exception as e:
        await update.message.reply_text(f"Eroare la analiza pozei. Mai incearca o data. ({type(e).__name__})")
        return

    add_history(user_id, "assistant", reply)
    await update.message.reply_text(reply)


def main():
    db_init()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("remember", remember))
    app.add_handler(CommandHandler("profile", profile))

    # poze
    app.add_handler(MessageHandler(filters.PHOTO, chat_photo))
    # text normal
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_text))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
