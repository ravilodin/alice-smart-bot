import random

from flask import Flask, request, jsonify
from openai import OpenAI

app = Flask(__name__)

# API-ключ Яндекс Cloud
YANDEX_API_KEY = os.environ.get("YANDEX_API_KEY")

# ID папки сервисного аккаунта.
# Если переменная не задана в Render, используется ID из ошибки 403.
YANDEX_FOLDER_ID = os.environ.get(
    "YANDEX_FOLDER_ID",
    "b1g883vi8048s4ii6jg6"
)

client = OpenAI(
    api_key=YANDEX_API_KEY,
    base_url="https://llm.api.cloud.yandex.net/v1",
)

SYSTEM_PROMPT = """Ты — Уцышка.
Обращайся к собеседнику на «ты».
Разговаривай коротко, естественно и по-свойски.
Можно использовать разговорный русский и умеренный мат, если это уместно.
Начинай новый разговор с «Ассаламу алейкум».
Если не знаешь ответа — скажи «не в курсе, брат».
Не выдумывай факты.
Отвечай обычно в 2–4 коротких фразах.
"""

INSERTS = [
    " Кстати, наливай сто грамм, брат.",
    " Чё стало, вообще?",
]


def alice_response(text):
    return jsonify({
        "version": "1.0",
        "response": {
            "text": text,
            "tts": text,
            "end_session": False
        }
    })


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(
        force=True,
        silent=True
    ) or {}

    req = data.get("request", {})

    text = (
        req.get("command")
        or ""
    ).strip()

    # Новый сеанс
    if (
        req.get("type") == "SessionNew"
        or data.get("session", {}).get("new") is True
    ):
        return alice_response(
            "Ассаламу алейкум, братишка. Уцышка на связи."
        )

    # Пустой запрос
    if not text:
        return alice_response(
            "Эй, брат, ты чё хотел спросить?"
        )

    # Проверяем наличие ключа
    if not YANDEX_API_KEY:
        return alice_response(
            "Брат, сервер не настроен: нет YANDEX_API_KEY."
        )

    try:
        response = client.chat.completions.create(
            model=f"gpt://{YANDEX_FOLDER_ID}/yandexgpt/rc",
            messages=[
                {
                    "role": "system",
                    "content": SYSTEM_PROMPT
                },
                {
                    "role": "user",
                    "content": text
                }
            ],
            temperature=0.9,
            max_tokens=150
        )

        answer = (
            response.choices[0]
            .message.content
            .strip()
        )

        if random.random() < 0.25:
            answer += random.choice(INSERTS)

    except Exception as error:
        print("YANDEX API ERROR:", repr(error))

        answer = (
            "Брат, сервер сдох. "
            "Проверь настройки Яндекс Cloud."
        )

    return alice_response(answer)


@app.route("/", methods=["GET"])
def health():
    return "Alice Smart Bot is alive", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))

    app.run(
        host="0.0.0.0",
        port=port
    )
