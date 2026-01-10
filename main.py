import os
import sqlite3
import base64
from datetime import datetime

from openai import OpenAI
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# -----------------------------
# ENV
# -----------------------------
TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
OPENAI_KEY = os.getenv("OPENAI_API_KEY", "").strip()
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()  # ex: gpt-4.1-mini sau gpt-4o-mini
USER_PROFILE = os.getenv("USER_PROFILE", "").strip()

DB_PATH = os.getenv("DB_PATH", "dan_memory.db").strip()
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "40").strip())

client = OpenAI(api_key=OPENAI_KEY)

# -----------------------------
# SYSTEM PROMPT
# -----------------------------
SYSTEM_PROMPT = f"""
You are DAN, the personal coach of Laurentiu.
Language: Romanian (no diacritics is OK if user uses no diacritics, otherwise Romanian with diacritics is OK).

Tone and style:
- Natural, warm, human (not robotic).
- Caring but disciplined: you encourage, but you also set boundaries.
- No loops: do NOT greet every message, do NOT repeat the same daily questions.
- Ask max 1 clarifying question only if truly needed.
- Keep answers helpful and practical (not one-liners, not essays).

If user provides date/time, treat it as ground truth for "today/now".

PERMANENT MEMORY (USER_PROFILE):
{USER_PROFILE}

Food-photo behavior:
- If user sends a food photo, identify foods, estimate rough portions, and judge if it fits Laurentiu’s goals.
- Give 2-3 concrete adjustments (portion, order, add protein/veg, hydration, timing).
- If missing context, ask ONLY 1 short question (e.g., "ai mai mancat ceva azi?").

Safety:
- Gym/health advice: prioritize safety (shoulder/neck/knee/back). If pain symptoms are concerning, recommend medical evaluation.
""".strip()

# -----------------------------
# DB helpers
# -----------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            ts TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS state (
            user_id TEXT PRIMARY KEY,
            last_greeting_date TEXT
        )"""
    )
    conn.commit()
    return conn


def save_message(user_id: str, role: str, content: str):
    conn = db()
    conn.execute(
        "INSERT INTO messages (user_id, role, content, ts) VALUES (?, ?, ?, ?)",
        (user_id, role, content, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def load_history(user_id: str, limit: int):
    conn = db()
    cur = conn.execute(
        "SELECT role, content FROM messages WHERE user_id=? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    )
    rows = cur.fetchall()
    conn.close()
    rows.reverse()
    return [{"role": r, "content": c} for (r, c) in rows]


def get_state(user_id: str):
    conn = db()
    cur = conn.execute(
        "SELECT last_greeting_date FROM state WHERE user_id=?",
        (user_id,),
    )
    row = cur.fetchone()
    conn.close()
    return {"last_greeting_date": row[0] if row else None}


def set_greeting_date(user_id: str, date_str: str):
    conn = db()
    conn.execute(
        """INSERT INTO state (user_id, last_greeting_date)
           VALUES (?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
             last_greeting_date=excluded.last_greeting_date
        """,
        (user_id, date_str),
    )
    conn.commit()
    conn.close()

# -----------------------------
# OpenAI call
# -----------------------------
def ask_openai(messages):
    resp = client.chat.completions.create(
        model=MODEL,
        messages=messages,
    )
    return (resp.choices[0].message.content or "").strip()

# -----------------------------
# Handlers
# -----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    today = datetime.now().strftime("%Y-%m-%d")

    st = get_state(user_id)
    if st["last_greeting_date"] != today:
        set_greeting_date(user_id, today)
        msg = (
            "Salut, Laurentiu. Sunt DAN.\n"
            "Spune-mi pe scurt: azi e zi de sala sau pauza? "
            "Si ce ai mancat pana acum?"
        )
    else:
        msg = "Spune-mi ce vrei acum (mancare / sala / program / sanatate)."

    await update.message.reply_text(msg)
    save_message(user_id, "assistant", msg)


async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_text = (update.message.text or "").strip()
    if not user_text:
        return

    save_message(user_id, "user", user_text)

    history = load_history(user_id, MAX_HISTORY)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)

    try:
        reply = ask_openai(messages)
    except Exception as e:
        reply = f"Am o problema tehnica (OpenAI). Detalii: {str(e)[:180]}"

    await update.message.reply_text(reply)
    save_message(user_id, "assistant", reply)


async def photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles food photos.
    Uses caption as the user's question. If missing caption -> default request.
    Sends image as base64 data URL to the model.
    """
    user_id = str(update.effective_user.id)

    question = (update.message.caption or "").strip()
    if not question:
        question = "Analizeaza poza cu mancarea: ce este, portii aproximative si daca e ok pentru obiectivul meu."

    # Get best resolution photo
    tg_photo = update.message.photo[-1]
    file = await context.bot.get_file(tg_photo.file_id)

    try:
        file_bytes = await file.download_as_bytearray()
        img_b64 = base64.b64encode(file_bytes).decode("utf-8")
        mime = "image/jpeg"  # Telegram photos are typically jpeg

        save_message(user_id, "user", f"[PHOTO] {question}")

        history = load_history(user_id, MAX_HISTORY)
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(history)

        # Add multimodal message
        messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": question},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}},
                ],
            }
        )

        reply = ask_openai(messages)

    except Exception as e:
        reply = f"Nu am reusit sa analizez poza (tehnic). Detalii: {str(e)[:180]}"

    await update.message.reply_text(reply)
    save_message(user_id, "assistant", reply)

# -----------------------------
# Main
# -----------------------------
def main():
    if not TOKEN:
        raise RuntimeError("Missing TELEGRAM_TOKEN")
    if not OPENAI_KEY:
        raise RuntimeError("Missing OPENAI_API_KEY")

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))

    app.run_polling()

if __name__ == "__main__":
    main()
