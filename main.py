import os
import re
from fastapi import FastAPI, Request
from openai import AsyncOpenAI

app = FastAPI()

client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SYSTEM_PROMPT = (
    "Ты — умный и глубокий собеседник, встроенный в голосовой помощник Алиса. "
    "Отвечай развернуто, умнее и точнее стандартных шаблонов, но при этом четко и емко. "
    "Избегай лишней 'воды'. НЕ используй Markdown-разметку (звездочки, решетки, списки), "
    "пиши простой текст, предназначенный для чтения вслух. Длина ответа — не более 3-4 предложений."
)


def clean_text_for_speech(text: str) -> str:
    text = re.sub(r"[\*\_\#\`\~]", "", text)
    text = re.sub(r"\n+", " ", text)
    return text.strip()


@app.post("/webhook")
async def yandex_alice_webhook(request: Request):
    data = await request.json()

    version = data.get("version", "1.0")
    session = data.get("session", {})
    user_request = data.get("request", {})

    is_new_session = session.get("new", False)

    if is_new_session:
        text_response = "Привет! Я твой обновленный умный помощник. О чем хотим поговорить?"
    else:
        command = user_request.get("command", "").strip()

        if not command:
            text_response = "Я вас не услышала. Повторите, пожалуйста."
        else:
            try:
                response = await client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": command},
                    ],
                    max_tokens=250,
                    timeout=3.5,
                )
                raw_text = response.choices[0].message.content
                text_response = clean_text_for_speech(raw_text)

            except Exception as e:
                print(f"Ошибка ИИ: {e}")
                text_response = "Извините, не удалось быстро получить ответ от сервера. Попробуйте спросить еще раз."

    return {
        "version": version,
        "session": session,
        "response": {"text": text_response, "end_session": False},
    }
