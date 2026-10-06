import os
import random
from flask import Flask, request, jsonify
from openai import OpenAI

app = Flask(__name__)

client = OpenAI(
    api_key=os.environ.get("YANDEX_API_KEY"),
    base_url="https://llm.api.cloud.yandex.net/v1",
)

SYSTEM_PROMPT = """Ты — дагестанский пацан по имени Уцышка. Говоришь на «ты», коротко, по-свойски, с матом. Начинаешь с «ассаламу алейкум». Если не знаешь — «не в курсе, брат». Подкалываешь по-злому, родню не трогаешь. Ответы короткие, две-четыре фразы."""

INSERTS = [
    " Кстати, наливай сто грамм, брат.",
    " Чё стало, вообще?",
]

def alice(text):
    return jsonify({
        "version": "1.0",
        "response": {"text": text, "tts": text, "end_session": False},
    })

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True, silent=True) or {}
    req = data.get("request", {})
    text = (req.get("command") or "").strip()
    if req.get("type") == "SessionNew" or data.get("session", {}).get("new"):
        return alice("Ассаламу алейкум, братишка. Уцышка на связи.")
    if not text:
        return alice("Эй, бля, ну ты чё нахуй, пригласил-то сюда? Чё, я ушёл, блядь, тогда.")
    try:
        resp = client.chat.completions.create(
            model=f"gpt://{os.environ.get('YANDEX_FOLDER_ID')}/yandexgpt/rc",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            temperature=0.9,
            max_tokens=150,
        )
        answer = resp.choices[0].message.content.strip()
        if random.random() < 0.25:
            answer += random.choice(INSERTS)
    except Exception as e:
        answer = f"Брат, сервер сдох. Ошибка: {e}"
    return alice(answer)
