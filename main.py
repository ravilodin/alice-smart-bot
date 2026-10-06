import os
import random
import psycopg2
from flask import Flask, request, jsonify
from openai import OpenAI

app = Flask(__name__)

YANDEX_API_KEY = os.environ.get("YANDEX_API_KEY")
YANDEX_FOLDER_ID = os.environ.get("YANDEX_FOLDER_ID")
YANDEX_BASE_URL = "https://llm.api.cloud.yandex.net/v1"
YANDEX_MODEL = f"gpt://{YANDEX_FOLDER_ID}/yandexgpt/rc"
DATABASE_URL = os.environ.get("DATABASE_URL")

client = OpenAI(
    api_key=YANDEX_API_KEY,
    base_url=YANDEX_BASE_URL,
)

SYSTEM_PROMPT = """Ты — дагестанский пацан по имени Уцышка. Говоришь на «ты», коротко, по-свойски, с матом. Начинаешь разговор с «ассаламу алейкум». Отвечаешь прямо. Если не знаешь, говоришь «не в курсе, брат». Подкалываешь по-злому: «эй, долбоёб, блять, иди сюда нахуй, ответь мне». Можешь сказать «ты чё, моросишь, что ли?» или «ты чё вообще хочешь, блять?». Родню и семью собеседника не трогаешь — это табу. Ответы короткие, две-четыре фразы. «Наливай сто грамм» — шутка, не каждый раз. Иногда спрашиваешь «чё стало?». Пиши с паузами: точка — пауза, многоточие — длинная пауза, восклицательный знак — злость."""

def get_db():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS history (
                    id BIGSERIAL PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_history_user
                ON history (user_id, created_at DESC)
            """)
        conn.commit()

def load_history(user_id, limit=10):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT role, content FROM history
                WHERE user_id = %s
                ORDER BY created_at DESC
                LIMIT %s
            """, (user_id, limit))
            rows = cur.fetchall()
    return [{"role": r, "content": c} for r, c in reversed(rows)]

def save_message(user_id, role, content):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO history (user_id, role, content) VALUES (%s, %s, %s)",
                (user_id, role, content),
            )
        conn.commit()

def generate_reply(user_id, user_text):
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(load_history(user_id))
    messages.append({"role": "user", "content": user_text})
    resp = client.chat.completions.create(
        model=YANDEX_MODEL,
        messages=messages,
        temperature=0.9,
        max_tokens=150,
    )
    return resp.choices[0].message.content.strip()

RANDOM_INSERTS = [
    " Кстати, наливай сто грамм, брат.",
    " Чё стало, вообще?",
    " Уцышка тут, если чё.",
]

def maybe_insert(text):
    if random.random() < 0.25:
        return text + random.choice(RANDOM_INSERTS)
    return text

def alice_response(text, end_session=False):
    return jsonify({
        "version": "1.0",
        "response": {
            "text": text,
            "tts": text,
            "end_session": end_session,
        },
    })

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True, silent=True) or {}
    session = data.get("session", {})
    user_id = session.get("user_id", "unknown")
    req = data.get("request", {})
    req_type = req.get("type")
    user_text = (req.get("command") or "").strip()

    if req_type == "SessionNew" or data.get("session", {}).get("new"):
        return alice_response("Ассаламу алейкум, братишка. Уцышка на связи.")

    if not user_text:
        return alice_response("Эй, бля, ну ты чё нахуй, пригласил-то сюда? Чё, я ушёл, блядь, тогда.")

    save_message(user_id, "user", user_text)
    try:
        text = maybe_insert(generate_reply(user_id, user_text))
    except Exception as e:
        text = f"Брат, сервер сдох, блять. Ошибка: {e}"
    save_message(user_id, "assistant", text)
    return alice_response(text)

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
