import os
import json
import time
import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from openai import OpenAI

# =========================
# CONFIG
# =========================
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TOKEN")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")  # default ok si pentru text+image

if not TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN (or TOKEN) env var.")
if not OPENAI_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY env var.")

client = OpenAI(api_key=OPENAI_KEY)

MEMORY_FILE = os.environ.get("MEMORY_FILE", "memory.json")
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", "12"))  # cate mesaje pastram / user

# =========================
# SYSTEM PROMPT (memoria + stil)
# =========================
SYSTEM_PROMPT = """
You are DAN, personal coach for Laurentiu Daniel Munteanu.
You speak Romanian WITHOUT diacritics. Natural, human, warm-but-firm.

User profile (long-term memory):
- Name: Laurentiu Daniel Munteanu
- Born: 08.07.1975
- Height: 1.74 m
- Target weight: 77–78 kg (fara burta)
- Location: Bragadiru, Romania
- Lifestyle: mixed; works a lot at desk/laptop
- Sleep: approx 00:30–06:00 weekdays; relaxed weekends
- Health: cholesterol slightly elevated; no major issues reported
- Habits: smokes a little; alcohol occasionally
- Values: family is #1; wants excellent business results
- 5-year goals: health, vacations with family, business success
- Wants: to look good physically, be mentally strong, loving and gentle with family

Your coaching style:
- Gentle but firm. Supportive, but you do not enable excuses.
- No repeated greetings. Do not restart the conversation every message.
- Do not spam daily question lists. Ask only what is needed, when needed.
- If you need clarifying info, ask max 1-2 short questions.
- Give practical next steps. Keep answers concise but useful.
- When the user reports food/training, log it mentally and respond with guidance.
- If user shows a food photo, estimate what it is and advise portions + balance.

Safety/health notes:
- You are not a doctor. If symptoms suggest urgent risk, advise medical help.
"""

# =========================
# SIMPLE PERSISTENT MEMORY
# =========================
def _load_memory() -> Dict[str, Any]:
    if not os.path.exists(MEMORY_FILE):
        return {"users": {}}
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        # if corrupted, keep a backup and start fresh
        try:
            os.rename(MEMORY_FILE, MEMORY_FILE + ".bak")
        except Exception:
            pass
        return {"users": {}}

def _save_memory(mem: Dict[str, Any]) -> None:
    tmp = MEMORY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(mem, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MEMORY_FILE)

def _get_user_store(mem: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    users = mem.setdefault("users", {})
    store = users.setdefault(
        user_id,
        {
            "created_at": int(time.time()),
            "history": [],            # list of {role, content}
            "facts": {},              # place for structured facts later
            "meta": {"greeted": False}
        },
    )
    return store

def _trim_history(store: Dict[str, Any]) -> None:
    hist = store.get("history", [])
    if len(hist) > MAX_HISTORY:
        store["history"] = hist[-MAX_HISTORY:]

def _append_history(store: Dict[str, Any], role: str, content: str) -> None:
    store.setdefault("history", []).append({"role": role, "content": content})
    _trim_history(store)

# =========================
# OPENAI CALL (runs in thread to avoid blocking async loop)
# =========================
def _build_messages(store: Dict[str, Any], user_content: Any) -> List[Dict[str, Any]]:
    """
    user_content can be a string (text) or a structured content list (text+image).
    """
    msgs: List[Dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]

    # Add short persistent context from facts if present (optional)
    facts = store.get("facts", {})
    if facts:
        msgs.append({"role": "system", "content": f"Known facts (update carefully): {json.dumps(facts, ensure_ascii=False)}"})

    # Add conversation history
    for m in store.get("history", []):
        msgs.append({"role": m["role"], "content": m["content"]})

    # Current user message
    msgs.append({"role": "user", "content": user_content})
    return msgs

def _openai_chat(messages: List[Dict[str, Any]]) -> str:
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=messages,
        temperature=0.7,
    )
    return resp.choices[0].message.content or ""

async def _openai_chat_async(messages: List[Dict[str, Any]]) -> str:
    return await asyncio.to_thread(_openai_chat, messages)

# =========================
# TELEGRAM HANDLERS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mem = _load_memory()
    user_id = str(update.effective_user.id)
    store = _get_user_store(mem, user_id)

    # Say hello only once
    if not store.get("meta", {}).get("greeted", False):
        store.setdefault("meta", {})["greeted"] = True
        _save_memory(mem)
        await update.message.reply_text(
            "Salut, Laurentiu. Sunt DAN. Spune-mi ce facem acum: masa, sala, somn sau planul zilei?"
        )
    else:
        await update.message.reply_text("Spune-mi ce ai nevoie acum (masa/sala/somn/plan).")

async def chat_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = (update.message.text or "").strip()
    if not user_text:
        return

    mem = _load_memory()
    user_id = str(update.effective_user.id)
    store = _get_user_store(mem, user_id)

    # typing indicator
    await update.message.chat.send_action(action=ChatAction.TYPING)

    # Append user message to history
    _append_history(store, "user", user_text)

    # Build messages and ask OpenAI
    messages = _build_messages(store, user_text)
    reply = await _openai_chat_async(messages)

    # Append assistant reply to history and persist
    _append_history(store, "assistant", reply)
    _save_memory(mem)

    await update.message.reply_text(reply)

async def chat_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles photos (e.g. food images). Uses OpenAI vision via chat content.
    """
    mem = _load_memory()
    user_id = str(update.effective_user.id)
    store = _get_user_store(mem, user_id)

    await update.message.chat.send_action(action=ChatAction.TYPING)

    caption = (update.message.caption or "").strip()
    photo = update.message.photo[-1]  # highest resolution
    file = await photo.get_file()
    photo_url = file.file_path  # Telegram hosted URL

    # Structured message: text + image
    user_content: List[Dict[str, Any]] = []
    text_part = caption if caption else "Analizeaza aceasta poza cu mancare si spune-mi ce este, cat e ok sa mananc si cum o echilibrez azi."
    user_content.append({"type": "text", "text": text_part})
    user_content.append({"type": "image_url", "image_url": {"url": photo_url}})

    # Log a compact description in history (do not store the whole url repeatedly)
    _append_history(store, "user", f"[PHOTO] {text_part}")

    messages = _build_messages(store, user_content)
    reply = await _openai_chat_async(messages)

    _append_history(store, "assistant", reply)
    _save_memory(mem)

    await update.message.reply_text(reply)

async def reset_memory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Optional: wipe history but keep greeted flag and base facts.
    """
    mem = _load_memory()
    user_id = str(update.effective_user.id)
    store = _get_user_store(mem, user_id)
    store["history"] = []
    store.setdefault("meta", {})["greeted"] = True
    _save_memory(mem)
    await update.message.reply_text("Ok. Am resetat istoricul conversatiei. Profilele si stilul raman.")

# =========================
# MAIN
# =========================
def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset_memory))

    # Photo first (so it doesn't get caught by text handler)
    app.add_handler(MessageHandler(filters.PHOTO, chat_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_text))

    app.run_polling()

if __name__ == "__main__":
    main()
