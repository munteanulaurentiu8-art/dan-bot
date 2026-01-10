import os
import re
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
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

DB_PATH = os.getenv("DB_PATH", "dan_memory.db").strip()
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "40").strip())  # context conversational
MAX_MEMORY_ITEMS = int(os.getenv("MAX_MEMORY_ITEMS", "80").strip())  # facts/rules

client = OpenAI(api_key=OPENAI_KEY)

# -----------------------------
# DB init + helpers
# -----------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
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
    conn.execute(
        """CREATE TABLE IF NOT EXISTS memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            ts TEXT NOT NULL
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


def memory_upsert(user_id: str, key: str, value: str):
    key = key.strip().lower()
    value = value.strip()

    conn = db()
    # If key exists -> update latest row by inserting a new one (audit trail),
    # then we will load latest per key.
    conn.execute(
        "INSERT INTO memory (user_id, key, value, ts) VALUES (?, ?, ?, ?)",
        (user_id, key, value, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def memory_delete_key(user_id: str, key: str):
    key = key.strip().lower()
    conn = db()
    conn.execute("DELETE FROM memory WHERE user_id=? AND key=?", (user_id, key))
    conn.commit()
    conn.close()


def memory_clear(user_id: str):
    conn = db()
    conn.execute("DELETE FROM memory WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def memory_get_latest(user_id: str, max_items: int):
    """
    Returns latest value per key (deduplicated), limited to max_items keys.
    """
    conn = db()
    # Get latest per key
    cur = conn.execute(
        """
        SELECT key, value, MAX(id) as mid
        FROM memory
        WHERE user_id=?
        GROUP BY key
        ORDER BY mid DESC
        LIMIT ?
        """,
        (user_id, max_items),
    )
    rows = cur.fetchall()
    conn.close()

    # Keep order (most recent keys first)
    items = [(k, v) for (k, v, _mid) in rows]
    return items


def memory_as_text(user_id: str, max_items: int):
    items = memory_get_latest(user_id, max_items)
    if not items:
        return "(nu exista memorie salvata inca)"
    lines = []
    for k, v in items:
        lines.append(f"- {k}: {v}")
    return "\n".join(lines)

# -----------------------------
# Prompt builder
# -----------------------------
def build_system_prompt(user_id: str) -> str:
    mem = memory_as_text(user_id, MAX_MEMORY_ITEMS)

    return f"""
You are DAN, the personal coach of Laurentiu.
Language: Romanian (user may write without diacritics; reply can be with or without diacritics, but keep it natural).

Personality:
- Caring but disciplined. Warm, human, not robotic.
- No loops: do NOT greet every message, do NOT repeat daily questions.
- Respond directly to what the user asks. Ask at most 1 clarification only if necessary.
- Give practical steps: food, training, schedule, health.

Time:
- If user states date/time, treat it as truth for "today/now".

LONG-TERM MEMORY (facts/rules saved by user):
{mem}

Food photo:
- Identify foods, estimate portions, tell if OK for goals, suggest 2-3 concrete adjustments.
- If missing context, ask only 1 short question.

Health/gym safety:
- Prioritize safe guidance for shoulder/neck/knee/back.
- If pain is unusual/severe/persistent, recommend medical evaluation.

Output style:
- Helpful, not too short, not too long.
""".strip()

# -----------------------------
# OpenAI call
# -----------------------------
def ask_openai(model: str, messages):
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
    )
    return (resp.choices[0].message.content or "").strip()

# -----------------------------
# Memory parsing rules
# -----------------------------
RETINE_PATTERNS = [
    r"^\s*retine\s*:\s*(.+)$",
    r"^\s*memoreaza\s*:\s*(.+)$",
    r"^\s*tine\s+minte\s*:\s*(.+)$",
]

FORGET_PATTERNS = [
    r"^\s*uita\s*:\s*(.+)$",
    r"^\s*sterge\s*:\s*(.+)$",
]

def parse_retine(text: str):
    """
    Accept formats:
      retine: cheie = valoare
      retine: cheie: valoare
      retine: un text liber (se salveaza ca 'nota')
    """
    for pat in RETINE_PATTERNS:
        m = re.match(pat, text, flags=re.IGNORECASE)
        if m:
            payload = m.group(1).strip()
            # try key-value split
            if " = " in payload:
                k, v = payload.split(" = ", 1)
                return k.strip(), v.strip()
            if ":" in payload:
                k, v = payload.split(":", 1)
                return k.strip(), v.strip()
            # fallback
            return "nota", payload
    return None

def parse_forget(text: str):
    for pat in FORGET_PATTERNS:
        m = re.match(pat, text, flags=re.IGNORECASE)
        if m:
            key = m.group(1).strip()
            return key
    return None

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
            "Spune-mi direct: azi e sala sau pauza? Si ce ai mancat pana acum?"
        )
    else:
        msg = "Spune-mi ce vrei acum (mancare / sala / program / sanatate)."

    await update.message.reply_text(msg)
    save_message(user_id, "assistant", msg)


async def memory_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    txt = memory_as_text(user_id, MAX_MEMORY_ITEMS)
    await update.message.reply_text(f"Memoria salvata:\n{txt}")


async def forget_all_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    memory_clear(user_id)
    await update.message.reply_text("Am sters toata memoria salvata pentru tine.")


async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_text = (update.message.text or "").strip()
    if not user_text:
        return

    # 1) Handle "retine:" (real memory)
    ret = parse_retine(user_text)
    if ret:
        k, v = ret
        memory_upsert(user_id, k, v)
        reply = f"Am retinut (memorie reala): {k} = {v}"
        await update.message.reply_text(reply)
        save_message(user_id, "user", user_text)
        save_message(user_id, "assistant", reply)
        return

    # 2) Handle "uita:" / "sterge:" key
    fk = parse_forget(user_text)
    if fk:
        memory_delete_key(user_id, fk)
        reply = f"Ok. Am sters din memorie cheia: {fk}"
        await update.message.reply_text(reply)
        save_message(user_id, "user", user_text)
        save_message(user_id, "assistant", reply)
        return

    # Normal chat
    save_message(user_id, "user", user_text)

    history = load_history(user_id, MAX_HISTORY)
    system_prompt = build_system_prompt(user_id)

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)

    try:
        reply = ask_openai(MODEL, messages)
    except Exception as e:
        reply = f"Problema tehnica (OpenAI). Detalii: {str(e)[:180]}"

    await update.message.reply_text(reply)
    save_message(user_id, "assistant", reply)


async def photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    question = (update.message.caption or "").strip()
    if not question:
        question = "Analizeaza poza cu mancarea: ce este, portii aproximative, si daca e ok pentru obiectivul meu."

    tg_photo = update.message.photo[-1]
    file = await context.bot.get_file(tg_photo.file_id)

    try:
        file_bytes = await file.download_as_bytearray()
        img_b64 = base64.b64encode(file_bytes).decode("utf-8")
        mime = "image/jpeg"

        save_message(user_id, "user", f"[PHOTO] {question}")

        history = load_history(user_id, MAX_HISTORY)
        system_prompt = build_system_prompt(user_id)

        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history)

        # Multimodal message
        messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": question},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}},
                ],
            }
        )

        reply = ask_openai(MODEL, messages)

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

    # Ensure DB initialized
    conn = db()
    conn.close()

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("memory", memory_cmd))
    app.add_handler(CommandHandler("forgetall", forget_all_cmd))

    app.add_handler(MessageHandler(filters.PHOTO, photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))

    app.run_polling()

if __name__ == "__main__":
    main()
