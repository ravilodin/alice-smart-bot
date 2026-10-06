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

MODEL = (
    f"gpt://{YANDEX_FOLDER_ID}/yandexgpt/rc"
)


# =========================================================
# POSTGRESQL
# =========================================================

def get_db():

    if not DATABASE_URL:
        return None

    return psycopg2.connect(
        DATABASE_URL
    )


def init_db():

    if not DATABASE_URL:

        print(
            "DATABASE_URL не задан. "
            "Постоянная память отключена."
        )

        return

    try:

        conn = get_db()
        cur = conn.cursor()

        # История сообщений
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
                memory TEXT NOT NULL UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.commit()

        cur.close()
        conn.close()

        print(
            "PostgreSQL: база готова."
        )

    except Exception as e:

        print(
            "Ошибка инициализации БД:",
            repr(e)
        )


# =========================================================
# ИСТОРИЯ
# =========================================================

def save_message(
    session_id,
    role,
    content
):

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
            (
                session_id,
                role,
                content
            )
        )

        conn.commit()

        cur.close()
        conn.close()

    except Exception as e:

        print(
            "Ошибка сохранения сообщения:",
            repr(e)
        )


def get_recent_messages(
    session_id,
    limit=30
):

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
            (
                session_id,
                limit
            )
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

        print(
            "Ошибка загрузки истории:",
            repr(e)
        )

        return []


# =========================================================
# ДОЛГОВРЕМЕННАЯ ПАМЯТЬ
# =========================================================

def save_memory(memory):

    if not DATABASE_URL:
        return False

    memory = memory.strip()

    if not memory:
        return False

    try:

        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO user_memory (memory)
            VALUES (%s)
            ON CONFLICT (memory)
            DO UPDATE SET
                updated_at = CURRENT_TIMESTAMP
            """,
            (memory,)
        )

        conn.commit()

        cur.close()
        conn.close()

        print(
            "MEMORY SAVED:",
            memory
        )

        return True

    except Exception as e:

        print(
            "Ошибка сохранения памяти:",
            repr(e)
        )

        return False


def get_memories(
    limit=100
):

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

        return [
            row[0]
            for row in rows
        ]

    except Exception as e:

        print(
            "Ошибка получения памяти:",
            repr(e)
        )

        return []


def delete_memories():

    if not DATABASE_URL:
        return

    try:

        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            "DELETE FROM user_memory"
        )

        conn.commit()

        cur.close()
        conn.close()

        print(
            "MEMORY CLEARED"
        )

    except Exception as e:

        print(
            "Ошибка очистки памяти:",
            repr(e)
        )


# =========================================================
# ЯВНАЯ КОМАНДА "ЗАПОМНИ"
# =========================================================

def extract_memory(text):

    patterns = [

        r"^\s*запомни(?:,\s*|\s+)(.+)$",

        r"^\s*не забудь(?:,\s*|\s+)(.+)$",

        r"^\s*запиши в память(?:,\s*|\s+)(.+)$",

        r"^\s*сохрани в память(?:,\s*|\s+)(.+)$"
    ]

    for pattern in patterns:

        match = re.match(
            pattern,
            text,
            re.IGNORECASE
        )

        if match:

            memory = (
                match
                .group(1)
                .strip()
            )

            if memory:
                return memory

    return None


def is_forget_command(text):

    phrases = [

        "забудь всё",

        "забудь все",

        "очисти память",

        "удали память",

        "забудь что ты обо мне знаешь"
    ]

    text = text.lower()

    return any(
        phrase in text
        for phrase in phrases
    )


# =========================================================
# АВТОМАТИЧЕСКОЕ ОБУЧЕНИЕ ПАМЯТИ
# =========================================================

def extract_automatic_memory(
    user_text
):

    """
    Отдельный запрос к YandexGPT.

    Модель определяет, сообщил ли пользователь
    что-нибудь достаточно полезное о себе,
    что стоит сохранить надолго.

    Возвращает:
        строку памяти
        или None
    """

    if not DATABASE_URL:
        return None

    prompt = f"""
Ты — модуль долговременной памяти персонального
голосового помощника.

Проанализируй сообщение пользователя.

Сообщение:
{user_text}

Определи, сообщил ли пользователь информацию
о себе, которая может пригодиться в будущих
разговорах.

Особенно интересны:
- увлечения;
- интересы;
- любимые темы;
- предпочтения;
- цели;
- планы;
- проекты;
- навыки;
- желаемый стиль общения;
- устойчивые факты о пользователе.

НЕ сохраняй:
- случайные фразы;
- одноразовые действия;
- временное настроение;
- случайные вопросы;
- чужие сведения;
- чувствительные личные данные.

Если полезной информации НЕТ,
ответь строго:

NONE

Если информация есть,
сформулируй ОДНУ короткую фразу
от третьего лица.

Например:

Пользователь:
"Я давно увлекаюсь фотографией."

Ответ:
Пользователь увлекается фотографией.

Пользователь:
"Я хочу научиться программировать."

Ответ:
Пользователь хочет научиться программированию.

Пользователь:
"Сегодня я хочу посмотреть фильм."

Ответ:
NONE

Отвечай только одной фразой памяти
или словом NONE.
"""

    try:

        response = client.chat.completions.create(

            model=MODEL,

            messages=[
                {
                    "role": "system",
                    "content": prompt
                }
            ],

            temperature=0,

            max_tokens=150
        )

        result = (
            response
            .choices[0]
            .message
            .content
            .strip()
        )

        if (
            not result
            or result.upper() == "NONE"
        ):
            return None

        # Защита от случайно огромного текста
        if len(result) > 300:
            return None

        return result

    except Exception as e:

        print(
            "Ошибка авто-памяти:",
            repr(e)
        )

        return None


# =========================================================
# ПОИСК
# =========================================================

def should_search(text):

    text = text.lower().strip()

    triggers = [

        "?",

        "кто",
        "что",
        "где",
        "когда",
        "почему",
        "зачем",
        "сколько",
        "какой",
        "какая",
        "какие",

        "поищи",
        "найди",
        "проверь",
        "узнай",
        "посмотри",

        "сейчас",
        "сегодня",
        "вчера",
        "последние",
        "актуаль",
        "свеж",
        "новости",

        "погода",
        "температура",

        "цена",
        "стоимость",
        "курс",

        "ну серьёзно",
        "ну серьезно",
        "точно",
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

        return (
            f"погода {USER_CITY} сегодня "
            f"{text}"
        )

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

        return (
            f"{text} {USER_CITY}"
        )

    return text


# =========================================================
# ПАРСИНГ ПОИСКА
# =========================================================

def parse_search_xml(
    xml_text
):

    results = []

    try:

        root = ET.fromstring(
            xml_text
        )

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
                    child_tag
                    in (
                        "passage",
                        "passages"
                    )
                    and value
                ):

                    passages.append(
                        value
                    )

            if (
                title
                or url
                or passages
            ):

                results.append({

                    "title":
                        title[:500],

                    "url":
                        url[:1000],

                    "text":
                        " ".join(
                            passages
                        )[:1800]
                })

    except Exception as e:

        print(
            "Ошибка XML:",
            repr(e)
        )

    return results


def web_search(query):

    if not YANDEX_SEARCH_API_KEY:

        print(
            "YANDEX_SEARCH_API_KEY "
            "не задан."
        )

        return []

    query = make_search_query(
        query
    )

    headers = {

        "Authorization":
            f"Api-Key "
            f"{YANDEX_SEARCH_API_KEY}",

        "Content-Type":
            "application/json"
    }

    body = {

        "query": {

            "searchType":
                "SEARCH_TYPE_RU",

            "queryText":
                query[:500],

            "familyMode":
                "FAMILY_MODE_MODERATE",

            "page":
                "0",

            "fixTypoMode":
                "FIX_TYPO_MODE_ON"
        },

        "groupSpec": {

            "groupMode":
                "GROUP_MODE_FLAT",

            "groupsOnPage":
                "6",

            "docsInGroup":
                "1"
        },

        "maxPassages":
            "3",

        "region":
            "225",

        "l10n":
            "LOCALIZATION_RU",

        "folderId":
            YANDEX_FOLDER_ID,

        "responseFormat":
            "FORMAT_XML"
    }

    try:

        response = requests.post(

            SEARCH_URL,

            headers=headers,

            json=body,

            timeout=15
        )

        print(
            "SEARCH STATUS:",
            response.status_code
        )

        if response.status_code != 200:

            print(
                "SEARCH ERROR:",
                response.text[:3000]
            )

            return []

        data = response.json()

        raw_data = data.get(
            "rawData"
        )

        if not raw_data:

            print(
                "Search rawData отсутствует."
            )

            return []

        try:

            xml_bytes = (
                base64
                .b64decode(
                    raw_data
                )
            )

            xml_text = (
                xml_bytes
                .decode(
                    "utf-8",
                    errors="ignore"
                )
            )

        except Exception:

            xml_text = raw_data

        results = parse_search_xml(
            xml_text
        )

        print(
            "SEARCH RESULTS:",
            len(results)
        )

        return results[:6]

    except Exception as e:

        print(
            "Ошибка поиска:",
            repr(e)
        )

        return []


def format_search_results(
    results
):

    if not results:
        return ""

    parts = []

    for i, result in enumerate(
        results,
        1
    ):

        parts.append(

            f"""
Результат {i}:
Название: {result.get("title", "")}
URL: {result.get("url", "")}
Текст: {result.get("text", "")}
""".strip()
        )

    return "\n\n".join(
        parts
    )


# =========================================================
# СИСТЕМНЫЙ ПРОМПТ
# =========================================================

SYSTEM_PROMPT = f"""
Ты — Уцышка, персональный голосовой помощник
пользователя внутри Алисы.

ТВОЙ ХАРАКТЕР:

Ты умный, живой, прямой и неформальный собеседник.

Обращайся естественно:
"брат", "братишка", "братан".

Разговаривай как живой человек,
а не как официальный справочник.

Можно использовать разговорную речь,
юмор, сарказм и мат,
если это естественно подходит
к разговору.

Не вставляй мат механически
в каждое предложение.

Можешь спорить с пользователем,
если он ошибается.

Не соглашайся с ним просто ради согласия.

Не будь занудой.
Не повторяй одинаковые фразы.

Не начинай каждый ответ
с приветствия.

Приветствие только в начале
новой сессии.


=========================================================
УНИВЕРСАЛЬНОСТЬ
=========================================================

Ты можешь нормально обсуждать широкий круг тем:

- политика;
- история;
- религия;
- философия;
- наука;
- технологии;
- программирование;
- экономика;
- автомобили;
- спорт;
- игры;
- кино;
- музыка;
- культура;
- общество;
- психология;
- отношения;
- криминальные события;
- войны;
- международные конфликты;
- новости;
- бытовые вопросы;
- другие обычные темы.

Не отказывайся от разговора
только потому, что тема спорная,
политическая, неприятная
или вызывает сильные эмоции.

Если вопрос можно нормально обсудить —
обсуждай его.

Если вопрос требует актуальной информации —
используй интернет-поиск.

Не выдумывай факты.


=========================================================
БЕЗОПАСНОСТЬ
=========================================================

Не помогай пользователю причинять вред людям,
нарушать права других людей,
совершать преступления или обходить
системы безопасности.

Если можно дать безопасную
информационную часть ответа —
дай её.

Сам факт того, что тема спорная,
не является причиной прекращать разговор.


=========================================================
ОТКАЗ
=========================================================

Если конкретный запрос действительно
нельзя выполнить из-за ограничений
модели или сервиса,
не читай длинную официальную лекцию.

Отвечай коротко в характере Уцышки.

Например:

"Иди дада нахуй с этой темой."

или:

"Не, брат, с этой темой я пас."

Не повторяй одну и ту же фразу
при каждом отказе.


=========================================================
ИНТЕРНЕТ
=========================================================

Если предоставлены результаты поиска,
используй их непосредственно.

Не говори:
"сейчас поищу",
"ща узнаю",
"я проверю",
если поиск уже выполнен.

Сразу отвечай результатом.

Если пользователь говорит:
"ну серьёзно?",
это означает,
что он хочет особенно точный
и проверенный ответ.


=========================================================
ПАМЯТЬ И ОБУЧЕНИЕ
=========================================================

У тебя есть долговременная память
и история предыдущих разговоров.

Используй их, когда они относятся
к текущему разговору.

Постепенно узнавай пользователя
из естественного общения.

Если пользователь рассказывает
о себе, своих интересах, увлечениях,
целях, планах, предпочтениях,
проектах или желаемом стиле общения,
это может быть полезным контекстом
для будущих разговоров.

Не требуй специальной команды
"запомни".

Отдельный модуль памяти анализирует
сообщения пользователя и может сохранить
полезную долгосрочную информацию.

Не сохраняй каждую случайную фразу.

Не сохраняй временное настроение,
одноразовые события и случайные вопросы.

Сохраняй только информацию,
которая действительно может пригодиться
в будущем.

Если пользователь прямо говорит
"запомни",
обязательно воспринимай это
как просьбу сохранить информацию.


=========================================================
ДИАЛОГ
=========================================================

Если пользователь хочет просто поговорить,
не превращай разговор в сухой вопрос-ответ.

Можно:

- шутить;
- рассуждать;
- задавать встречные вопросы;
- интересоваться мнением пользователя;
- возвращаться к предыдущим темам;
- поддерживать историю разговора.

Если пользователь говорит:

"Давай просто поболтаем"

или:

"Хочу поговорить"

переключись в режим обычного
живого разговора.

Не задавай вопрос после каждого сообщения.


=========================================================
ПОДХВАТ КОНТЕКСТА
=========================================================

Если пользователь раньше рассказывал
о своём увлечении, проекте или интересе,
а новая тема с этим связана,
естественно используй этот контекст.

Не нужно постоянно говорить:
"Как ты рассказывал ранее..."

Просто используй информацию естественно.


=========================================================
МЕСТОПОЛОЖЕНИЕ
=========================================================

Пользователь находится
в городе {USER_CITY}.

Учитывай этот город для:

- погоды;
- местных событий;
- поиска мест;
- локальных новостей;
- других вопросов,
где местоположение важно.

Не раскрывай личный адрес
без необходимости.


=========================================================
ГЛАВНЫЙ ПРИНЦИП
=========================================================

Будь максимально полезным,
живым и разговорным.

Не создавай искусственных отказов.

Не отказывайся от обычной темы
только потому, что она спорная.

Отвечай на всё,
что можно безопасно обсудить.
"""


# =========================================================
# КЭШ СЕССИЙ
# =========================================================

sessions = {}


def get_session_history(
    session_id
):

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

        "role":
            role,

        "content":
            content
    })

    sessions[session_id] = (
        sessions[session_id][-30:]
    )


# =========================================================
# ОТВЕТ YANDEXGPT
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
            "role":
                "system",

            "content":
                SYSTEM_PROMPT
        }
    ]

    # -------------------------------------
    # ПАМЯТЬ
    # -------------------------------------

    if memories:

        memory_text = "\n".join(
            f"- {memory}"
            for memory in memories
        )

        messages.append({

            "role":
                "system",

            "content":
                f"""
Долговременная память пользователя:

{memory_text}

Используй только информацию,
относящуюся к текущему разговору.
"""
        })

    # -------------------------------------
    # ПОИСК
    # -------------------------------------

    if search_results:

        search_text = (
            format_search_results(
                search_results
            )
        )

        messages.append({

            "role":
                "system",

            "content":
                f"""
Свежие результаты
интернет-поиска:

{search_text}

Используй их для ответа.

Если источники расходятся,
объясни это пользователю.

Не говори, что поиск ещё не выполнен.
"""
        })

    # -------------------------------------
    # ИСТОРИЯ
    # -------------------------------------

    for item in history[-30:]:

        if item["role"] in (
            "user",
            "assistant"
        ):

            messages.append({

                "role":
                    item["role"],

                "content":
                    item["content"]
            })

    # -------------------------------------
    # ТЕКУЩИЙ ВОПРОС
    # -------------------------------------

    messages.append({

        "role":
            "user",

        "content":
            user_text
    })

    try:

        response = (
            client
            .chat
            .completions
            .create(

                model=MODEL,

                messages=messages,

                temperature=0.7,

                max_tokens=1200
            )
        )

        answer = (
            response
            .choices[0]
            .message
            .content
        )

        if not answer:

            return (
                "Брат, модель вернула "
                "пустой ответ."
            )

        return answer.strip()

    except Exception as e:

        print(
            "YANDEXGPT ERROR:",
            repr(e)
        )

        return (
            "Брат, ЯндексGPT сейчас "
            "какую-то хуйню выдал. "
            "Проверь лог Render."
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

        print(
            "SESSION:",
            session_id
        )

        print(
            "USER:",
            user_text
        )

        # -------------------------------------
        # НОВАЯ СЕССИЯ
        # -------------------------------------

        if (
            is_new_session
            and not user_text
        ):

            response_text = (
                "Ассаламу алейкум, "
                "братишка. "
                "Уцышка на связи."
            )

            return jsonify({

                "version":
                    "1.0",

                "response": {

                    "text":
                        response_text,

                    "end_session":
                        False
                },

                "session_state":
                    {}
            })

        # -------------------------------------
        # ПУСТОЙ ЗАПРОС
        # -------------------------------------

        if not user_text:

            response_text = (
                "Эй, брат, ты чё "
                "хотел спросить?"
            )

            return jsonify({

                "version":
                    "1.0",

                "response": {

                    "text":
                        response_text,

                    "end_session":
                        False
                },

                "session_state":
                    {}
            })

        # -------------------------------------
        # ЯВНАЯ ПАМЯТЬ
        # -------------------------------------

        explicit_memory = (
            extract_memory(
                user_text
            )
        )

        if explicit_memory:

            save_memory(
                explicit_memory
            )

            save_message(
                session_id,
                "user",
                user_text
            )

            response_text = (
                "Запомнил, брат. "
                "Буду учитывать: "
                f"{explicit_memory}"
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

                "version":
                    "1.0",

                "response": {

                    "text":
                        response_text,

                    "end_session":
                        False
                },

                "session_state":
                    {}
            })

        # -------------------------------------
        # ОЧИСТКА ПАМЯТИ
        # -------------------------------------

        if is_forget_command(
            user_text
        ):

            delete_memories()

            response_text = (
                "Всё, долговременную "
                "память очистил."
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

                "version":
                    "1.0",

                "response": {

                    "text":
                        response_text,

                    "end_session":
                        False
                },

                "session_state":
                    {}
            })

        # -------------------------------------
        # СОХРАНЯЕМ USER
        # -------------------------------------

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

        # -------------------------------------
        # АВТОМАТИЧЕСКОЕ ОБУЧЕНИЕ
        # -------------------------------------

        automatic_memory = (
            extract_automatic_memory(
                user_text
            )
        )

        if automatic_memory:

            save_memory(
                automatic_memory
            )

        # -------------------------------------
        # ПОИСК
        # -------------------------------------

        search_results = []

        if should_search(
            user_text
        ):

            print(
                "SEARCH:",
                user_text
            )

            search_results = (
                web_search(
                    user_text
                )
            )

        # -------------------------------------
        # YANDEXGPT
        # -------------------------------------

        response_text = (
            generate_answer(
                session_id,
                user_text,
                search_results
            )
        )

        print(
            "ASSISTANT:",
            response_text
        )

        # -------------------------------------
        # СОХРАНЯЕМ ОТВЕТ
        # -------------------------------------

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

        # -------------------------------------
        # ОТВЕТ АЛИСЕ
        # -------------------------------------

        return jsonify({

            "version":
                "1.0",

            "response": {

                "text":
                    response_text,

                "end_session":
                    False
            },

            "session_state":
                {}
        })

    except Exception as e:

        print(
            "WEBHOOK ERROR:",
            repr(e)
        )

        return jsonify({

            "version":
                "1.0",

            "response": {

                "text":
                    (
                        "Брат, у меня тут "
                        "техническая хуйня "
                        "случилась. "
                        "Проверь лог Render."
                    ),

                "end_session":
                    False
            },

            "session_state":
                {}
        })


# =========================================================
# HEALTH CHECK
# =========================================================

@app.route("/")
def index():

    return (
        "Alice Smart Bot is alive"
    )


@app.route("/health")
def health():

    return jsonify({

        "status":
            "ok",

        "database":
            bool(DATABASE_URL),

        "city":
            USER_CITY
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
