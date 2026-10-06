import os
import re
import base64
import xml.etree.ElementTree as ET
from datetime import datetime

import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify
from openai import OpenAI


# =========================================================
# НАСТРОЙКИ
# =========================================================

app = Flask(__name__)

YANDEX_API_KEY = os.environ.get("YANDEX_API_KEY")
YANDEX_FOLDER_ID = os.environ.get(
    "YANDEX_FOLDER_ID",
    "b1g883vi8048s4ii6jg6"
)

YANDEX_SEARCH_API_KEY = os.environ.get(
    "YANDEX_SEARCH_API_KEY",
    YANDEX_API_KEY
)

DATABASE_URL = os.environ.get("DATABASE_URL")

# Местоположение пользователя.
# Лучше задавать через Render Environment Variables.
USER_LOCATION = os.environ.get(
    "USER_LOCATION",
    "Москва, ул. Перерова, 28"
)

# Для поиска используем город, а не полный адрес.
SEARCH_LOCATION = "Москва"


# =========================================================
# YANDEXGPT
# =========================================================

client = OpenAI(
    api_key=YANDEX_API_KEY,
    base_url="https://llm.api.cloud.yandex.net/v1"
)

MODEL = f"gpt://{YANDEX_FOLDER_ID}/yandexgpt/rc"


# =========================================================
# БАЗА ДАННЫХ
# =========================================================

def get_db():
    if not DATABASE_URL:
        return None

    return psycopg2.connect(DATABASE_URL)


def init_db():
    """
    Создаёт таблицы, если их ещё нет.
    """

    if not DATABASE_URL:
        print("DATABASE_URL не задан. Память будет работать только временно.")
        return

    try:
        conn = get_db()
        cur = conn.cursor()

        # Все сообщения пользователя и бота
        cur.execute("""
            CREATE TABLE IF NOT EXISTS conversation_messages (
                id SERIAL PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Долговременная память
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_memory (
                id SERIAL PRIMARY KEY,
                memory TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.commit()

        cur.close()
        conn.close()

        print("PostgreSQL: таблицы готовы.")

    except Exception as e:
        print("Ошибка инициализации БД:", e)


# =========================================================
# СОХРАНЕНИЕ СООБЩЕНИЙ
# =========================================================

def save_message(session_id, role, content):
    """
    Сохраняет сообщение в PostgreSQL.
    """

    if not DATABASE_URL:
        return

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO conversation_messages
            (session_id, role, content)
            VALUES (%s, %s, %s)
            """,
            (session_id, role, content)
        )

        conn.commit()

        cur.close()
        conn.close()

    except Exception as e:
        print("Ошибка сохранения сообщения:", e)


# =========================================================
# ЗАГРУЗКА ПОСЛЕДНИХ СООБЩЕНИЙ
# =========================================================

def get_recent_messages(session_id, limit=30):
    """
    Возвращает последние сообщения конкретной сессии.
    """

    if not DATABASE_URL:
        return []

    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute(
            """
            SELECT role, content
            FROM conversation_messages
            WHERE session_id = %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (session_id, limit)
        )

        rows = cur.fetchall()

        cur.close()
        conn.close()

        # Мы получили DESC, поэтому разворачиваем обратно.
        rows.reverse()

        return [
            {
                "role": row["role"],
                "content": row["content"]
            }
            for row in rows
        ]

    except Exception as e:
        print("Ошибка загрузки истории:", e)
        return []


# =========================================================
# ДОЛГОВРЕМЕННАЯ ПАМЯТЬ
# =========================================================

def save_memory(memory_text):
    """
    Сохраняет важный факт о пользователе.
    """

    if not DATABASE_URL:
        return

    memory_text = memory_text.strip()

    if not memory_text:
        return

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO user_memory (memory)
            VALUES (%s)
            """,
            (memory_text,)
        )

        conn.commit()

        cur.close()
        conn.close()

        print("Сохранено в память:", memory_text)

    except Exception as e:
        print("Ошибка сохранения памяти:", e)


def get_memories(limit=100):
    """
    Получает долговременную память.
    """

    if not DATABASE_URL:
        return []

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            SELECT memory
            FROM user_memory
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            (limit,)
        )

        rows = cur.fetchall()

        cur.close()
        conn.close()

        return [row[0] for row in rows]

    except Exception as e:
        print("Ошибка получения памяти:", e)
        return []


def delete_memories():
    """
    Удаляет сохранённую долговременную память.
    """

    if not DATABASE_URL:
        return

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute("DELETE FROM user_memory")

        conn.commit()

        cur.close()
        conn.close()

        print("Долговременная память очищена.")

    except Exception as e:
        print("Ошибка удаления памяти:", e)


# =========================================================
# РАСПОЗНАВАНИЕ КОМАНДЫ "ЗАПОМНИ"
# =========================================================

def extract_memory(text):
    """
    Если пользователь пишет:
    "Запомни, что я люблю..."
    "Не забудь, что..."
    
    возвращает текст для долговременной памяти.
    """

    patterns = [
        r"^\s*запомни(?:,\s*|\s+)(.+)$",
        r"^\s*не забудь(?:,\s*|\s+)(.+)$",
        r"^\s*запиши в память(?:,\s*|\s+)(.+)$",
        r"^\s*сохрани в память(?:,\s*|\s+)(.+)$",
    ]

    for pattern in patterns:
        match = re.match(pattern, text, re.IGNORECASE)

        if match:
            memory = match.group(1).strip()

            if memory:
                return memory

    return None


# =========================================================
# ОПРЕДЕЛЕНИЕ КОМАНДЫ "ЗАБУДЬ"
# =========================================================

def is_forget_command(text):
    phrases = [
        "забудь всё",
        "забудь все",
        "очисти память",
        "удали память",
        "забудь что ты обо мне знаешь"
    ]

    text_lower = text.lower()

    return any(
        phrase in text_lower
        for phrase in phrases
    )


# =========================================================
# ПОИСК В ИНТЕРНЕТЕ
# =========================================================

SEARCH_URL = "https://searchapi.api.cloud.yandex.net/v2/web/search"


def should_search(text):
    """
    Определяем, стоит ли обращаться к интернету.
    """

    text_lower = text.lower().strip()

    search_triggers = [
        "?",
        "поищи",
        "найди",
        "проверь",
        "узнай",
        "посмотри",
        "новости",
        "новости сегодня",
        "сегодня",
        "сейчас",
        "последние",
        "актуаль",
        "свеж",
        "погода",
        "температура",
        "курс",
        "цена",
        "стоимость",
        "когда",
        "сколько",
        "кто сейчас",
        "где сейчас",
        "ну серьёзно",
        "ну серьезно"
    ]

    return any(
        trigger in text_lower
        for trigger in search_triggers
    )


def make_search_query(text):
    """
    Улучшает поисковый запрос.
    """

    text_lower = text.lower()

    # Погоду ищем именно по Москве.
    if "погод" in text_lower or "температур" in text_lower:
        return f"погода Москва сегодня {text}"

    # Если пользователь спрашивает о московском/местном,
    # добавляем город.
    local_words = [
        "рядом",
        "поблизости",
        "в москве",
        "москва",
        "у меня",
        "около меня",
        "недалеко"
    ]

    if any(word in text_lower for word in local_words):
        return f"{text} Москва"

    return text


def parse_search_xml(xml_text):
    """
    Пытается достать из XML название, URL и текст результата.
    """

    results = []

    try:
        root = ET.fromstring(xml_text)

        for doc in root.iter():
            tag = doc.tag.lower().split("}")[-1]

            if tag != "doc":
                continue

            title = ""
            url = ""
            passages = []

            for child in doc.iter():
                child_tag = child.tag.lower().split("}")[-1]

                value = "".join(
                    child.itertext()
                ).strip()

                if child_tag == "title" and value:
                    title = value

                elif child_tag == "url" and value:
                    url = value

                elif child_tag in ("passage", "passages") and value:
                    passages.append(value)

            if title or url or passages:

                results.append({
                    "title": title[:500],
                    "url": url[:1000],
                    "text": " ".join(passages)[:1500]
                })

    except Exception as e:
        print("Ошибка разбора XML поиска:", e)

    return results


def web_search(query):
    """
    Выполняет поиск Yandex Search API.
    """

    if not YANDEX_SEARCH_API_KEY:
        print("YANDEX_SEARCH_API_KEY не задан.")
        return []

    search_query = make_search_query(query)

    headers = {
        "Authorization": f"Api-Key {YANDEX_SEARCH_API_KEY}",
        "Content-Type": "application/json"
    }

    body = {
        "query": {
            "searchType": "SEARCH_TYPE_RU",
            "queryText": search_query[:500],
            "familyMode": "FAMILY_MODE_MODERATE",
            "page": "0",
            "fixTypoMode": "FIX_TYPO_MODE_ON"
        },
        "groupSpec": {
            "groupMode": "GROUP_MODE_FLAT",
            "groupsOnPage": "6",
            "docsInGroup": "1"
        },
        "maxPassages": "3",
        "region": "225",
        "l10n": "LOCALIZATION_RU",
        "folderId": YANDEX_FOLDER_ID,
        "responseFormat": "FORMAT_XML"
    }

    try:

        response = requests.post(
            SEARCH_URL,
            headers=headers,
            json=body,
            timeout=15
        )

        print(
            "Search status:",
            response.status_code
        )

        if response.status_code != 200:
            print(
                "Search error:",
                response.text[:3000]
            )
            return []

        data = response.json()

        raw_data = data.get("rawData")

        if not raw_data:
            print("Search: rawData отсутствует.")
            return []

        try:
            xml_bytes = base64.b64decode(raw_data)
            xml_text = xml_bytes.decode(
                "utf-8",
                errors="ignore"
            )
        except Exception:
            xml_text = raw_data

        results = parse_search_xml(xml_text)

        print(
            "Search results:",
            len(results)
        )

        return results[:6]

    except Exception as e:
        print("Ошибка интернет-поиска:", e)
        return []


# =========================================================
# ФОРМИРОВАНИЕ РЕЗУЛЬТАТОВ ПОИСКА
# =========================================================

def format_search_results(results):

    if not results:
        return ""

    parts = []

    for i, result in enumerate(results, 1):

        title = result.get("title", "")
        url = result.get("url", "")
        text = result.get("text", "")

        parts.append(
            f"""
Результат {i}:
Название: {title}
URL: {url}
Текст: {text}
""".strip()
        )

    return "\n\n".join(parts)


# =========================================================
# СИСТЕМНЫЙ ПРОМПТ УЦЫШКИ
# =========================================================

SYSTEM_PROMPT = f"""
Ты — Уцышка, персональный голосовой помощник пользователя внутри Алисы.

Твой стиль:
- говори по-русски;
- обращайся к пользователю естественно: "брат", "братишка", "братан" — но не в каждом сообщении;
- общайся как живой умный собеседник;
- можешь иногда использовать лёгкий мат, если это естественно по контексту;
- не превращай каждый ответ в набор ругательств;
- можешь пошутить или подколоть пользователя;
- если пользователь ошибается — прямо скажи об этом;
- не повторяй постоянно одни и те же фразы;
- не начинай каждый ответ с приветствия;
- приветствие используй только в начале новой сессии;
- отвечай непосредственно на вопрос.

ВАЖНО:
Если ниже предоставлены результаты интернет-поиска,
используй их для ответа.

Если вопрос требует актуальной информации,
не выдумывай ответ.

Если поисковые результаты противоречат твоим знаниям,
ориентируйся на актуальные результаты поиска и объясняй неопределённость.

Фраза "ну серьёзно?" означает:
пользователь хочет получить максимально точный и фактический ответ,
а не отговорку.

Местоположение пользователя:
{USER_LOCATION}

При вопросах о местных событиях, погоде и подобных вещах
учитывай, что пользователь находится в Москве.

Не сообщай полный адрес без необходимости.
Для обычного поиска достаточно использовать город Москва.

У тебя есть долговременная память и история разговора.
Используй их для поддержания контекста.

Но не утверждай, что ты реально переобучил модель.
Ты используешь сохранённый контекст пользователя.
"""


# =========================================================
# СЕССИИ
# =========================================================

# Небольшой временный кэш.
# Настоящая история хранится PostgreSQL.
sessions = {}


def get_session_history(session_id):

    if session_id in sessions:
        return sessions[session_id]

    history = get_recent_messages(
        session_id,
        limit=30
    )

    sessions[session_id] = history

    return history


def add_session_message(
    session_id,
    role,
    content
):

    if session_id not in sessions:
        sessions[session_id] = []

    sessions[session_id].append({
        "role": role,
        "content": content
    })

    # Не держим бесконечную историю в RAM.
    sessions[session_id] = sessions[session_id][-30:]


# =========================================================
# ГЕНЕРАЦИЯ ОТВЕТА
# =========================================================

def generate_answer(
    session_id,
    user_text,
    search_results=None
):

    memories = get_memories()

    history = get_session_history(session_id)

    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT
        }
    ]

    # -----------------------------------------------------
    # ПАМЯТЬ
    # -----------------------------------------------------

    if memories:

        memory_text = "\n".join(
            f"- {memory}"
            for memory in memories
        )

        messages.append({
            "role": "system",
            "content": f"""
Долговременная память пользователя:

{memory_text}

Используй эту информацию, когда она действительно относится
к текущему разговору.
"""
        })

    # -----------------------------------------------------
    # ИНТЕРНЕТ
    # -----------------------------------------------------

    if search_results:

        formatted = format_search_results(
            search_results
        )

        messages.append({
            "role": "system",
            "content": f"""
Вот результаты свежего интернет-поиска:

{formatted}

Ответь пользователю на основании этих результатов.
Не говори "сейчас поищу", "ща узнаю" или подобное —
поиск уже выполнен.
"""
        })

    # -----------------------------------------------------
    # ИСТОРИЯ
    # -----------------------------------------------------

    for item in history[-30:]:

        role = item["role"]
        content = item["content"]

        if role in ("user", "assistant"):
            messages.append({
                "role": role,
                "content": content
            })

    # Текущее сообщение
    messages.append({
        "role": "user",
        "content": user_text
    })

    try:

        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            temperature=0.7,
            max_tokens=1000
        )

        answer = response.choices[0].message.content

        if not answer:
            return "Брат, модель почему-то вернула пустой ответ."

        return answer.strip()

    except Exception as e:

        print("Ошибка YandexGPT:", e)

        return (
            "Брат, с ЯндексGPT сейчас какая-то херня. "
            "Посмотри лог Render — там будет причина."
        )


# =========================================================
# АЛИСА WEBHOOK
# =========================================================

@app.route(
    "/webhook",
    methods=["POST"]
)
def webhook():

    try:

        data = request.get_json(
            force=True,
            silent=True
        ) or {}

        request_data = data.get(
            "request",
            {}
        )

        session_data = data.get(
            "session",
            {}
        )

        session_id = session_data.get(
            "session_id",
            "default"
        )

        is_new_session = session_data.get(
            "new",
            False
        )

        user_text = request_data.get(
            "command",
            ""
        ).strip()

        print(
            "Session:",
            session_id
        )

        print(
            "User:",
            user_text
        )

        # -------------------------------------------------
        # НОВАЯ СЕССИЯ
        # -------------------------------------------------

        if is_new_session and not user_text:

            response_text = (
                "Ассаламу алейкум, братишка. "
                "Уцышка на связи."
            )

            return jsonify({
                "version": "1.0",
                "response": {
                    "text": response_text,
                    "end_session": False
                },
                "session_state": {}
            })

        # -------------------------------------------------
        # ПУСТОЙ ЗАПРОС
        # -------------------------------------------------

        if not user_text:

            response_text = (
                "Эй, брат, ты чё хотел спросить?"
            )

            return jsonify({
                "version": "1.0",
                "response": {
                    "text": response_text,
                    "end_session": False
                },
                "session_state": {}
            })

        # -------------------------------------------------
        # ЗАПОМИНАНИЕ
        # -------------------------------------------------

        memory = extract_memory(user_text)

        if memory:

            save_memory(memory)

            save_message(
                session_id,
                "user",
                user_text
            )

            response_text = (
                f"Запомнил, брат. Буду учитывать: {memory}"
            )

            save_message(
                session_id,
                "assistant",
                response_text
            )

            add_session_message(
                session_id,
                "user",
                user_text
            )

            add_session_message(
                session_id,
                "assistant",
                response_text
            )

            return jsonify({
                "version": "1.0",
                "response": {
                    "text": response_text,
                    "end_session": False
                },
                "session_state": {}
            })

        # -------------------------------------------------
        # УДАЛЕНИЕ ПАМЯТИ
        # -------------------------------------------------

        if is_forget_command(user_text):

            delete_memories()

            response_text = (
                "Хорошо, долговременную память очистил."
            )

            save_message(
                session_id,
                "user",
                user_text
            )

            save_message(
                session_id,
                "assistant",
                response_text
            )

            return jsonify({
                "version": "1.0",
                "response": {
                    "text": response_text,
                    "end_session": False
                },
                "session_state": {}
            })

        # -------------------------------------------------
        # СОХРАНЯЕМ USER MESSAGE
        # -------------------------------------------------

        save_message(
            session_id,
            "user",
            user_text
        )

        add_session_message(
            session_id,
            "user",
            user_text
        )

        # -------------------------------------------------
        # ИНТЕРНЕТ-ПОИСК
        # -------------------------------------------------

        search_results = []

        if should_search(user_text):

            print(
                "Выполняю поиск:",
                user_text
            )

            search_results = web_search(
                user_text
            )

        # -------------------------------------------------
        # ОТВЕТ YANDEXGPT
        # -------------------------------------------------

        response_text = generate_answer(
            session_id,
            user_text,
            search_results
        )

        print(
            "Assistant:",
            response_text
        )

        # -------------------------------------------------
        # СОХРАНЯЕМ ОТВЕТ
        # -------------------------------------------------

        save_message(
            session_id,
            "assistant",
            response_text
        )

        add_session_message(
            session_id,
            "assistant",
            response_text
        )

        # -------------------------------------------------
        # ОТВЕТ АЛИСЕ
        # -------------------------------------------------

        return jsonify({
            "version": "1.0",
            "response": {
                "text": response_text,
                "end_session": False
            },
            "session_state": {}
        })

    except Exception as e:

        print(
            "ОШИБКА WEBHOOK:",
            repr(e)
        )

        return jsonify({
            "version": "1.0",
            "response": {
                "text": (
                    "Брат, у меня тут техническая "
                    "жопа случилась. Проверь лог Render."
                ),
                "end_session": False
            },
            "session_state": {}
        })


# =========================================================
# HEALTH CHECK
# =========================================================

@app.route("/")
def index():

    return "Alice Smart Bot is alive"


@app.route("/health")
def health():

    return jsonify({
        "status": "ok",
        "database": bool(DATABASE_URL),
        "location": SEARCH_LOCATION
    })


# =========================================================
# ЗАПУСК
# =========================================================

init_db()


if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            5000
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
