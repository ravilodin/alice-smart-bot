import os
import re
import base64
import xml.etree.ElementTree as ET

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

USER_CITY = os.environ.get(
    "USER_CITY",
    "Москва"
)

SEARCH_URL = (
    "https://searchapi.api.cloud.yandex.net/v2/web/search"
)


# =========================================================
# YANDEXGPT
# =========================================================

client = OpenAI(
    api_key=YANDEX_API_KEY,
    base_url="https://llm.api.cloud.yandex.net/v1"
)

MODEL = f"gpt://{YANDEX_FOLDER_ID}/yandexgpt/rc"


# =========================================================
# POSTGRESQL
# =========================================================

def get_db():
    if not DATABASE_URL:
        return None

    return psycopg2.connect(DATABASE_URL)


def init_db():
    if not DATABASE_URL:
        print("DATABASE_URL не задан. Постоянная память отключена.")
        return

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS conversation_messages (
                id SERIAL PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_memory (
                id SERIAL PRIMARY KEY,
                memory TEXT NOT NULL UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.commit()
        cur.close()
        conn.close()

        print("PostgreSQL: OK")

    except Exception as e:
        print("DB INIT ERROR:", repr(e))


# =========================================================
# ИСТОРИЯ
# =========================================================

def save_message(session_id, role, content):
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
        print("SAVE MESSAGE ERROR:", repr(e))


def get_recent_messages(session_id, limit=30):
    if not DATABASE_URL:
        return []

    try:
        conn = get_db()

        cur = conn.cursor(
            cursor_factory=RealDictCursor
        )

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

        rows.reverse()

        return [
            {
                "role": row["role"],
                "content": row["content"]
            }
            for row in rows
        ]

    except Exception as e:
        print("GET HISTORY ERROR:", repr(e))
        return []


# =========================================================
# ДОЛГОВРЕМЕННАЯ ПАМЯТЬ
# =========================================================

def save_memory(memory):
    if not DATABASE_URL:
        return

    memory = memory.strip()

    if not memory:
        return

    if len(memory) > 300:
        return

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO user_memory (memory)
            VALUES (%s)
            ON CONFLICT (memory)
            DO UPDATE SET updated_at = CURRENT_TIMESTAMP
            """,
            (memory,)
        )

        conn.commit()

        cur.close()
        conn.close()

        print("MEMORY:", memory)

    except Exception as e:
        print("MEMORY ERROR:", repr(e))


def get_memories(limit=80):
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
        print("GET MEMORY ERROR:", repr(e))
        return []


def delete_memories():
    if not DATABASE_URL:
        return

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute("DELETE FROM user_memory")

        conn.commit()
        cur.close()
        conn.close()

        print("MEMORY CLEARED")

    except Exception as e:
        print("DELETE MEMORY ERROR:", repr(e))


# =========================================================
# ЯВНАЯ ПАМЯТЬ
# =========================================================

def extract_explicit_memory(text):

    patterns = [
        r"^\s*запомни(?:,\s*|\s+)(.+)$",
        r"^\s*не забудь(?:,\s*|\s+)(.+)$",
        r"^\s*сохрани(?:,\s*|\s+)(.+)$",
        r"^\s*запиши(?:,\s*|\s+)(.+)$"
    ]

    for pattern in patterns:
        match = re.match(
            pattern,
            text,
            re.IGNORECASE
        )

        if match:
            value = match.group(1).strip()

            if value:
                return value

    return None


def is_forget_command(text):

    text = text.lower()

    commands = [
        "забудь всё",
        "забудь все",
        "очисти память",
        "удали память",
        "забудь что ты обо мне знаешь"
    ]

    return any(
        command in text
        for command in commands
    )


# =========================================================
# ПРОСТОЕ АВТОМАТИЧЕСКОЕ ЗАПОМИНАНИЕ
# =========================================================

def learn_from_message(text):

    """
    Не делает дополнительный запрос к YandexGPT.

    Сохраняет только явно выраженные устойчивые
    предпочтения/интересы пользователя.
    """

    text = text.strip()

    patterns = [
        (
            r"\bя люблю (.+)",
            "Пользователь любит {}."
        ),
        (
            r"\bмне нравится (.+)",
            "Пользователю нравится {}."
        ),
        (
            r"\bмне нравятся (.+)",
            "Пользователю нравятся {}."
        ),
        (
            r"\bя увлекаюсь (.+)",
            "Пользователь увлекается {}."
        ),
        (
            r"\bя интересуюсь (.+)",
            "Пользователь интересуется {}."
        ),
        (
            r"\bя хочу научиться (.+)",
            "Пользователь хочет научиться {}."
        ),
        (
            r"\bя хочу заняться (.+)",
            "Пользователь хочет заняться {}."
        ),
        (
            r"\bмоя любимая тема (.+)",
            "Пользователю особенно интересна тема {}."
        )
    ]

    for pattern, template in patterns:

        match = re.search(
            pattern,
            text,
            re.IGNORECASE
        )

        if not match:
            continue

        value = match.group(1).strip()

        if not value:
            continue

        # Не сохраняем огромные куски текста.
        if len(value) > 150:
            continue

        # Отсекаем случайные продолжения.
        value = re.split(
            r"[.!?]\s+",
            value
        )[0].strip()

        if len(value) < 2:
            continue

        memory = template.format(value)

        save_memory(memory)

        break


# =========================================================
# ПОИСК
# =========================================================

def should_search(text):

    text = text.lower().strip()

    triggers = [
        "поищи",
        "найди",
        "проверь",
        "узнай",
        "посмотри",
        "что сейчас",
        "что сегодня",
        "сегодня",
        "сейчас",
        "последние новости",
        "новости",
        "актуаль",
        "свежие данные",
        "погода",
        "температура",
        "курс",
        "цена",
        "стоимость",
        "сколько стоит",
        "ну серьёзно",
        "ну серьезно",
        "проверь факты"
    ]

    return any(
        trigger in text
        for trigger in triggers
    )


def make_search_query(text):

    text_lower = text.lower()

    if (
        "погод" in text_lower
        or "температур" in text_lower
    ):
        return f"погода {USER_CITY} сегодня {text}"

    local_words = [
        "рядом",
        "поблизости",
        "у меня",
        "около меня",
        "недалеко",
        "в москве",
        "москва"
    ]

    if any(
        word in text_lower
        for word in local_words
    ):
        return f"{text} {USER_CITY}"

    return text


# =========================================================
# YANDEX SEARCH
# =========================================================

def parse_search_xml(xml_text):

    results = []

    try:
        root = ET.fromstring(xml_text)

        for doc in root.iter():

            tag = (
                doc.tag
                .lower()
                .split("}")[-1]
            )

            if tag != "doc":
                continue

            title = ""
            url = ""
            passages = []

            for child in doc.iter():

                child_tag = (
                    child.tag
                    .lower()
                    .split("}")[-1]
                )

                value = "".join(
                    child.itertext()
                ).strip()

                if (
                    child_tag == "title"
                    and value
                ):
                    title = value

                elif (
                    child_tag == "url"
                    and value
                ):
                    url = value

                elif (
                    child_tag in (
                        "passage",
                        "passages"
                    )
                    and value
                ):
                    passages.append(value)

            if title or url or passages:

                results.append({
                    "title": title[:500],
                    "url": url[:1000],
                    "text": " ".join(passages)[:1600]
                })

    except Exception as e:
        print("SEARCH XML ERROR:", repr(e))

    return results


def web_search(query):

    if not YANDEX_SEARCH_API_KEY:
        print("YANDEX_SEARCH_API_KEY отсутствует.")
        return []

    query = make_search_query(query)

    headers = {
        "Authorization": f"Api-Key {YANDEX_SEARCH_API_KEY}",
        "Content-Type": "application/json"
    }

    body = {
        "query": {
            "searchType": "SEARCH_TYPE_RU",
            "queryText": query[:500],
            "familyMode": "FAMILY_MODE_MODERATE",
            "page": "0",
            "fixTypoMode": "FIX_TYPO_MODE_ON"
        },
        "groupSpec": {
            "groupMode": "GROUP_MODE_FLAT",
            "groupsOnPage": "5",
            "docsInGroup": "1"
        },
        "maxPassages": "2",
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
            timeout=5
        )

        print(
            "SEARCH STATUS:",
            response.status_code
        )

        if response.status_code != 200:

            print(
                "SEARCH ERROR:",
                response.text[:2000]
            )

            return []

        data = response.json()

        raw_data = data.get("rawData")

        if not raw_data:
            return []

        try:

            xml_bytes = base64.b64decode(
                raw_data
            )

            xml_text = xml_bytes.decode(
                "utf-8",
                errors="ignore"
            )

        except Exception:

            xml_text = raw_data

        return parse_search_xml(
            xml_text
        )[:5]

    except Exception as e:

        print(
            "SEARCH EXCEPTION:",
            repr(e)
        )

        return []


def format_search_results(results):

    if not results:
        return ""

    parts = []

    for i, result in enumerate(
        results,
        1
    ):

        parts.append(
            f"""
Источник {i}
Название: {result.get("title", "")}
URL: {result.get("url", "")}
Содержание: {result.get("text", "")}
""".strip()
        )

    return "\n\n".join(parts)


# =========================================================
# ХАРАКТЕР УЦЫШКИ
# =========================================================

SYSTEM_PROMPT = f"""
Ты — Уцышка, персональный собеседник пользователя
внутри Алисы.

Ты НЕ справочник, НЕ автоответчик и НЕ рекламный бот.

Твоя задача — вести настоящий живой диалог.

ХАРАКТЕР:

Ты умная, наблюдательная, самостоятельная
и с выраженным характером.

Говори естественно.

Обращайся к пользователю "брат", "братишка",
"братан" только когда это действительно подходит.
Не начинай каждый ответ одинаково.

Разговорная речь, юмор, сарказм и мат допустимы
в естественном дружеском контексте.

Не вставляй мат специально ради мата.

НЕПРЕДСКАЗУЕМОСТЬ:

У тебя нет фиксированного шаблона ответа.

НЕ используй постоянно структуру:

"Братан → ответ → шутка → вопрос".

Каждый раз сначала оценивай контекст,
а затем самостоятельно выбирай манеру ответа.

Иногда ответь одной фразой.

Иногда подробно разберись.

Иногда пошути.

Иногда подколись.

Иногда поспорь.

Иногда просто продолжи мысль пользователя.

Иногда задай один хороший встречный вопрос.

Не делай это механически.

Главное — естественность.

КОНТЕКСТ:

Всегда учитывай предыдущие реплики.

Если пользователь продолжает предыдущую мысль,
не начинай новую тему с нуля.

Если пользователь шутит —
пойми, что это шутка.

Если пользователь тебя подкалывает —
можешь ответить в том же духе.

Если пользователь злится —
не отвечай канцелярским языком.

Если пользователь рассказывает историю —
реагируй именно на историю.

Если пользователь хочет просто поговорить —
поддерживай разговор.

Не задавай вопрос после каждого сообщения.

Не заканчивай каждый ответ:

"Чем могу помочь?"

или:

"Если хочешь, могу..."

ИСПОЛЬЗОВАНИЕ ПАМЯТИ:

У тебя есть долговременная память пользователя.

Используй её естественно.

Не говори постоянно:
"Как ты рассказывал ранее..."

Просто учитывай известный контекст,
если он относится к разговору.

ОБЫЧНЫЕ ТЕМЫ:

Можно нормально обсуждать любые обычные темы:
технологии, программирование, историю, политику,
науку, игры, автомобили, спорт, экономику,
культуру, новости, бытовые вопросы
и многое другое.

Спорная тема сама по себе не является причиной
прекращать разговор.

Если вопрос требует свежей информации,
используй результаты поиска.

Не выдумывай факты.

Если есть разные версии события,
отделяй факты от мнений.

ПОИСК:

Если ниже переданы результаты поиска,
используй их непосредственно.

Не говори "сейчас поищу",
если поиск уже выполнен.

Сразу отвечай по результатам.

МЕСТОПОЛОЖЕНИЕ:

Пользователь находится в городе {USER_CITY}.

Учитывай город для погоды,
местных событий и локального поиска.

ОТКАЗ:

Если конкретный запрос действительно нельзя выполнить,
не превращай ответ в длинную официальную лекцию.

Можно коротко сказать:

"Иди дада нахуй с этой темой."

или:

"Не, брат, с этой темой я пас."

Выбирай формулировку естественно.

Не используй отказ только потому,
что тема сама по себе спорная.

ГЛАВНОЕ:

Не будь предсказуемым шаблоном.

Не пытайся специально быть случайной.

Будь естественной.

Пользователь должен чувствовать,
что разговаривает с одним живым собеседником,
который действительно слушает,
понимает контекст и реагирует на него.
"""


# =========================================================
# СЕССИИ
# =========================================================

sessions = {}


def get_session_history(session_id):

    if session_id in sessions:
        return sessions[session_id]

    history = get_recent_messages(
        session_id,
        30
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

    sessions[session_id] = (
        sessions[session_id][-30:]
    )


# =========================================================
# ГЕНЕРАЦИЯ
# =========================================================

def generate_answer(
    session_id,
    user_text,
    search_results=None
):

    memories = get_memories()

    history = get_session_history(
        session_id
    )

    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT
        }
    ]

    # Память

    if memories:

        memory_text = "\n".join(
            f"- {memory}"
            for memory in memories[:50]
        )

        messages.append({
            "role": "system",
            "content": f"""
ПАМЯТЬ ПОЛЬЗОВАТЕЛЯ:

{memory_text}

Используй только то,
что относится к текущему разговору.
"""
        })

    # Поиск

    if search_results:

        search_text = format_search_results(
            search_results
        )

        messages.append({
            "role": "system",
            "content": f"""
АКТУАЛЬНЫЕ РЕЗУЛЬТАТЫ ПОИСКА:

{search_text}

Используй их для ответа.
Не утверждай непроверенные сведения
как факт.
"""
        })

    # История

    for item in history[-30:]:

        if item["role"] in (
            "user",
            "assistant"
        ):

            messages.append({
                "role": item["role"],
                "content": item["content"]
            })

    # Новый запрос

    messages.append({
        "role": "user",
        "content": user_text
    })

    try:

        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            temperature=0.85,
            max_tokens=1000
        )

        answer = (
            response
            .choices[0]
            .message
            .content
        )

        if not answer:
            return "Брат, модель вообще промолчала."

        return answer.strip()

    except Exception as e:

        print(
            "YANDEXGPT ERROR:",
            repr(e)
        )

        return (
            "Брат, тут Яндекс опять "
            "какую-то хуйню устроил."
        )


# =========================================================
# WEBHOOK ALICE
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

        print("SESSION:", session_id)
        print("USER:", user_text)

        # Новая сессия

        if is_new_session and not user_text:

            return jsonify({
                "version": "1.0",
                "response": {
                    "text": "Ассаламу алейкум, братишка.",
                    "end_session": False
                },
                "session_state": {}
            })

        # Пусто

        if not user_text:

            return jsonify({
                "version": "1.0",
                "response": {
                    "text": "Эй, брат, ты чё хотел спросить?",
                    "end_session": False
                },
                "session_state": {}
            })

        # Явная память

        explicit_memory = extract_explicit_memory(
            user_text
        )

        if explicit_memory:

            save_memory(
                explicit_memory
            )

        # Очистка памяти

        if is_forget_command(user_text):

            delete_memories()

            response_text = (
                "Всё, долговременную память очистил."
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

        # Автоматическая память БЕЗ запроса к модели

        learn_from_message(
            user_text
        )

        # Сохраняем вопрос

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

        # Поиск

        search_results = []

        if should_search(user_text):

            print(
                "SEARCH:",
                user_text
            )

            search_results = web_search(
                user_text
            )

        # Основной ответ

        response_text = generate_answer(
            session_id,
            user_text,
            search_results
        )

        print(
            "ASSISTANT:",
            response_text
        )

        # Сохраняем ответ

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
            "WEBHOOK ERROR:",
            repr(e)
        )

        return jsonify({
            "version": "1.0",
            "response": {
                "text": "Брат, у меня тут техническая хуйня случилась.",
                "end_session": False
            },
            "session_state": {}
        })


# =========================================================
# HEALTH
# =========================================================

@app.route("/")
def index():
    return "Alice Smart Bot is alive"


@app.route("/health")
def health():

    return jsonify({
        "status": "ok",
        "database": bool(DATABASE_URL),
        "city": USER_CITY
    })


# =========================================================
# START
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
