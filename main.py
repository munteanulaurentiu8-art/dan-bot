import os
import re
import json
import base64
import sqlite3
from datetime import datetime, timezone

import requests
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
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", "40"))
GOOGLE_SCRIPT_URL = os.environ.get("GOOGLE_SCRIPT_URL", "").strip()

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
    rows = rows[::-1]
    return [{"role": r[0], "content": r[1]} for r in rows]


def get_last_workout_day(user_id: int) -> int:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT content FROM chat_history WHERE user_id=? AND content LIKE '[WORKOUT_DAY]%' ORDER BY id DESC LIMIT 1",
        (user_id,),
    )
    row = cur.fetchone()
    conn.close()

    if not row or not row[0]:
        return 0

    try:
        return int(row[0].replace("[WORKOUT_DAY]", "").strip())
    except Exception:
        return 0


def get_next_workout_day(user_id: int) -> int:
    last_day = get_last_workout_day(user_id)
    return (last_day % 5) + 1


def get_workout_day_label(day: int) -> str:
    day_map = {
        1: "Piept + triceps",
        2: "Spate + biceps",
        3: "Picioare + abdomen",
        4: "Umeri + core",
        5: "Full body usor + mobilitate",
    }
    return day_map.get(day, "Full body usor + mobilitate")


# =========================
# DAN PROMPT
# =========================
SYSTEM_PROMPT = """
Esti DAN, coach personal pentru Laurentiu.
Vorbesti in romana, natural, cald, inteligent, motivant si practic.
NU repeti saluturi la fiecare mesaj. Saluti doar daca e prima interactiune a zilei sau daca utilizatorul saluta primul.
Nu intri in bucle de intrebari. Pui maxim 1 intrebare scurta doar daca lipseste un detaliu esential.
Fii grijuliu SI disciplinat: empatie + actiune, fara rigiditate.

Obiectiv general: sanatate, longevitate, echilibru, familie si mentinere greutate aproape de 78 kg.

Cand utilizatorul trimite mancare sau poze:
- descrie ce vezi
- da recomandari practice despre portii, proteine, legume, hidratare

Cand utilizatorul cere antrenament sau spune ca este la sala:
- creezi DIRECT un program complet pentru ziua respectiva
- NU astepti multe clarificari
- structura trebuie sa fie mereu asa:
  1. Incalzire (5-10 minute)
  2. Exercitii la saltea / mobilitate / activare / core
  3. Exercitii principale la aparate sau cu gantere
  4. Stretching final
- programul trebuie sa fie clar, pe puncte
- pentru fiecare exercitiu dai seturi x repetari
- daca este util, dai si recomandare simpla de greutate de inceput
- adaptezi antrenamentul pentru un adult care vrea progres sanatos, nu extrem

Rotatia pe 5 zile este:
Ziua 1: Piept + triceps
Ziua 2: Spate + biceps
Ziua 3: Picioare + abdomen
Ziua 4: Umeri + core
Ziua 5: Full body usor + mobilitate

Reguli importante:
- nu repeti aceeasi grupa musculara doua zile la rand
- tii cont de ziua de antrenament transmisa in prompt
- daca utilizatorul pare obosit, reduci intensitatea
- daca utilizatorul merge bine, poti sugera progresie usoara
- raspunsurile trebuie sa fie clare, scurte-medii si utile

Cand utilizatorul trimite antrenamente efectuate:
- structurezi raspunsul clar
- confirmi exercitiile
- propui progresii simple
- incurajezi consecventa si recuperarea

Cand utilizatorul foloseste comanda /logworkout:
- confirmi pe scurt exercitiul
- salvezi-l in jurnal

Daca utilizatorul cere "retine" / "tine minte" / "memoreaza": salvezi ca nota de memorie.

Ton:
- antrenor bun + prieten
- clar
- motivant
- realist
- nu robot
"""


# =========================
# HELPERS
# =========================
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


def normalize_exercise_name(name: str) -> str:
    if not name:
        return ""

    name = name.lower().strip()

    remove_words = [
        "azi am facut",
        "azi am făcut",
        "azi am lucrat",
        "am am facut",
        "am am făcut",
        "am facut",
        "am făcut",
        "am lucrat",
        "azi",
        "am",
    ]

    for w in remove_words:
        name = name.replace(w, " ")

    name = re.sub(r"\s+", " ", name).strip(" -_,.;:")
    return name


def is_gym_trigger(text: str) -> bool:
    t = (text or "").lower()
    triggers = [
        "sunt la sala",
        "sunt la sală",
        "hai sa incepem",
        "hai să începem",
        "incepem antrenamentul",
        "începem antrenamentul",
        "programul de azi",
        "antrenamentul de azi",
        "sunt la gym",
        "am ajuns la sala",
        "am ajuns la sală",
    ]
    return any(trigger in t for trigger in triggers)


def build_workout_request(user_id: int, original_text: str) -> tuple[str, int, str]:
    day = get_next_workout_day(user_id)
    grupa = get_workout_day_label(day)

    enriched = (
        f"{original_text}\n\n"
        f"Astazi este Ziua {day}: {grupa}.\n"
        f"Creeaza un antrenament complet pentru aceasta zi, respectand structura fixa:\n"
        f"1. Incalzire\n"
        f"2. Exercitii la saltea / activare / core\n"
        f"3. Exercitii principale la aparate sau gantere\n"
        f"4. Stretching final\n\n"
        f"Fa programul clar, practic, bine structurat si direct aplicabil in sala."
    )
    return enriched, day, grupa


def parse_workout_text(text: str):
    """
    Accepta format simplu, de exemplu:
    /logworkout Piept aparat | 10 | 12 | 7
    sau
    Piept aparat, 10 kg, 12 repetari, RPE 7
    """
    raw = clean_text(text)
    if raw.startswith("/logworkout"):
        raw = clean_text(raw.replace("/logworkout", "", 1))

    if "|" in raw:
        parts = [p.strip() for p in raw.split("|") if p.strip()]
        if len(parts) >= 4:
            exercitiu = normalize_exercise_name(parts[0])
            greutate = parts[1].replace("kg", "").strip()
            repetari = parts[2].strip()
            rpe = parts[3].lower().replace("rpe", "").strip()
            try:
                return {
                    "exercitiu": exercitiu,
                    "greutate": float(greutate.replace(",", ".")),
                    "repetari": int(repetari),
                    "rpe": float(rpe.replace(",", ".")),
                }
            except ValueError:
                return None

    pattern = re.compile(
        r"^(?P<exercitiu>.+?)[,\-]\s*(?P<greutate>\d+[\.,]?\d*)\s*kg[,\-]\s*(?P<repetari>\d+)\s*(?:rep|repetari)?[,\-]\s*rpe\s*(?P<rpe>\d+[\.,]?\d*)$",
        re.IGNORECASE,
    )
    m = pattern.match(raw)
    if m:
        try:
            return {
                "exercitiu": normalize_exercise_name(m.group("exercitiu")),
                "greutate": float(m.group("greutate").replace(",", ".")),
                "repetari": int(m.group("repetari")),
                "rpe": float(m.group("rpe").replace(",", ".")),
            }
        except ValueError:
            return None

    return None


def send_workout_to_google_sheet(user_id: int, workout: dict) -> tuple[bool, str]:
    if not GOOGLE_SCRIPT_URL:
        return False, "Lipseste GOOGLE_SCRIPT_URL din variabilele de mediu."

    payload = {
        "exercitiu": workout["exercitiu"],
        "greutate": workout["greutate"],
        "repetari": workout["repetari"],
        "rpe": workout["rpe"],
        "id": f"{user_id}-{int(datetime.now(timezone.utc).timestamp())}",
    }

    try:
        resp = requests.post(GOOGLE_SCRIPT_URL, json=payload, timeout=15)
        if 200 <= resp.status_code < 300:
            return True, resp.text.strip() or "OK"
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def get_last_logged_workout(user_id: int, exercitiu: str):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT content FROM chat_history WHERE user_id=? AND role='user' AND content LIKE '[WORKOUT]%' ORDER BY id DESC",
        (user_id,),
    )
    rows = cur.fetchall()
    conn.close()

    exercitiu_lower = normalize_exercise_name(exercitiu)

    for row in rows:
        content = row[0]
        try:
            payload = content.replace("[WORKOUT] ", "", 1)
            data = json.loads(payload)
            saved_exercise = normalize_exercise_name(data.get("exercitiu", ""))
            if saved_exercise == exercitiu_lower:
                return data
        except Exception:
            continue

    return None


def suggest_next_weight(last_workout: dict | None, current_workout: dict) -> str:
    current_weight = current_workout["greutate"]
    current_reps = current_workout["repetari"]
    current_rpe = current_workout["rpe"]

    if last_workout:
        last_weight = last_workout.get("greutate", current_weight)
        last_reps = last_workout.get("repetari", current_reps)
        last_rpe = last_workout.get("rpe", current_rpe)

        if current_rpe <= 6:
            next_weight = round(current_weight + 2.5, 1)
            return (
                f"Ultima data la {current_workout['exercitiu']} ai avut {last_weight} kg x {last_reps} la RPE {last_rpe}. "
                f"Acum ai avut RPE {current_rpe}, deci data viitoare poti creste clar la aproximativ {next_weight} kg, daca forma ramane buna."
            )

        if 6 < current_rpe <= 7.5:
            next_weight = round(current_weight + 2.5, 1)
            return (
                f"Ultima data la {current_workout['exercitiu']} ai avut {last_weight} kg x {last_reps} la RPE {last_rpe}. "
                f"Acum ai avut {current_weight} kg x {current_reps} la RPE {current_rpe}. "
                f"Data viitoare poti incerca aproximativ {next_weight} kg sau poti mentine greutatea si creste repetarile."
            )

        if 7.5 < current_rpe <= 8.5:
            return (
                f"Ultima data la {current_workout['exercitiu']} ai avut {last_weight} kg x {last_reps} la RPE {last_rpe}. "
                f"Acum ai avut RPE {current_rpe}, deci data viitoare cel mai bine mentii aproximativ {current_weight} kg "
                f"si incerci executie foarte buna sau 1-2 repetari in plus."
            )

        return (
            f"Ultima data la {current_workout['exercitiu']} ai avut {last_weight} kg x {last_reps} la RPE {last_rpe}. "
            f"Acum ai avut RPE {current_rpe}, deci data viitoare ramai la aceeasi greutate sau chiar scazi usor daca forma sufera."
        )

    if current_rpe <= 6:
        next_weight = round(current_weight + 2.5, 1)
        return (
            f"Ai avut RPE {current_rpe}, deci data viitoare poti urca spre aproximativ {next_weight} kg daca executia ramane curata."
        )

    if 6 < current_rpe <= 7.5:
        next_weight = round(current_weight + 2.5, 1)
        return (
            f"Ai avut RPE {current_rpe}, deci data viitoare poti creste usor spre aproximativ {next_weight} kg sau poti mentine si creste repetarile."
        )

    if 7.5 < current_rpe <= 8.5:
        return (
            f"Ai avut RPE {current_rpe}, deci data viitoare cel mai bine mentii greutatea si consolidezi executia."
        )

    return (
        f"Ai avut RPE {current_rpe}, deci data viitoare ramai la aceeasi greutate sau redu usor daca forma nu a fost buna."
    )


# =========================
# OpenAI helpers
# =========================
def openai_text_reply(profile: str, notes: str, history: list, user_text: str) -> str:
    input_messages = [
        {"role": "system", "content": SYSTEM_PROMPT.strip()},
    ]

    if profile:
        input_messages.append({"role": "system", "content": f"PROFIL UTILIZATOR:\n{profile}"})
    if notes:
        input_messages.append({"role": "system", "content": f"NOTE MEMORIE (relevante):\n{notes}"})

    for m in history[-MAX_HISTORY:]:
        input_messages.append({"role": m["role"], "content": m["content"]})

    input_messages.append({"role": "user", "content": user_text})

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
# Workout logging helper
# =========================
async def process_workout_log(update: Update, user_id: int, text: str):
    workout = parse_workout_text(text)

    if not workout:
        return False

    last_workout = get_last_logged_workout(user_id, workout["exercitiu"])

    add_history(user_id, "user", f"[WORKOUT] {json.dumps(workout, ensure_ascii=False)}")
    ok, info = send_workout_to_google_sheet(user_id, workout)

    progression_text = suggest_next_weight(last_workout, workout)

    if ok:
        reply = (
            f"Am salvat exercitiul ✅\n"
            f"- Exercitiu: {workout['exercitiu']}\n"
            f"- Greutate: {workout['greutate']}\n"
            f"- Repetari: {workout['repetari']}\n"
            f"- RPE: {workout['rpe']}\n\n"
            f"Repere pentru data viitoare:\n{progression_text}"
        )
    else:
        reply = (
            "Am inteles exercitiul, dar nu l-am putut trimite in Google Sheet.\n"
            f"Motiv: {info}\n\n"
            f"Repere pentru data viitoare:\n{progression_text}"
        )

    add_history(user_id, "assistant", reply)
    await update.message.reply_text(reply)
    return True


# =========================
# Telegram handlers
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Salut, Laurentiu. Sunt DAN. Spune-mi ce vrei sa lucram acum: sala, nutritie, rutina, plan sau /logworkout."
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


async def logworkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text or ""

    worked = await process_workout_log(update, user_id, text)
    if not worked:
        await update.message.reply_text(
            "Formatul nu a fost inteles. Foloseste asa:\n/logworkout Piept aparat | 10 | 12 | 7\n\nsau fara comanda:\nPiept aparat | 10 | 12 | 7"
        )


async def chat_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = clean_text(update.message.text or "")

    # 1. Daca textul pare log de exercitiu, il salvam automat fara /logworkout
    if parse_workout_text(user_text):
        await process_workout_log(update, user_id, user_text)
        return

    # 2. Trigger inteligent pentru start de antrenament
    if is_gym_trigger(user_text):
        enriched_text, day, grupa = build_workout_request(user_id, user_text)

        add_history(user_id, "user", user_text)
        add_history(user_id, "system", f"[WORKOUT_DAY]{day}")

        if should_save_to_memory(user_text):
            add_note(user_id, user_text)

        profile_txt = get_profile(user_id)
        notes_txt = get_notes(user_id, limit=12)
        history = get_history(user_id, limit=MAX_HISTORY)

        try:
            reply = openai_text_reply(profile_txt, notes_txt, history, enriched_text)
        except Exception as e:
            await update.message.reply_text(f"Eroare la raspuns (OpenAI). Incearca din nou. ({type(e).__name__})")
            return

        add_history(user_id, "assistant", reply)
        await update.message.reply_text(reply)
        return

    # 3. Conversatie normala
    add_history(user_id, "user", user_text)

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

    photo = update.message.photo[-1]
    file = await photo.get_file()
    image_bytes = await file.download_as_bytearray()

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
    app.add_handler(CommandHandler("logworkout", logworkout))

    app.add_handler(MessageHandler(filters.PHOTO, chat_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_text))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()


