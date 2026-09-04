import os
import json
import time
import uuid
import re
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==========================================
# Конфигурация
# ==========================================
BOT_TOKEN = os.getenv("MAX_BOT_TOKEN")
API_BASE = os.getenv("MAX_API_BASE", "https://platform-api2.max.ru")

GIGACHAT_CREDENTIALS = os.getenv("GIGACHAT_CREDENTIALS")

FORCE_CHAT_ID = os.getenv("FORCE_CHAT_ID")
STATE_FILE = "state.json"
MAX_TEXT_LEN = 4000

# Настройки GigaChat
OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
GIGACHAT_API_URL = "https://api.giga.chat/v1"
VERIFY_SSL = False
GIGACHAT_MODEL = "GigaChat-3-Ultra"

# Кэш OAuth-токена
_token_cache = {"token": None, "expires_at": 0}


# ==========================================
# Промпты для GigaChat
# ==========================================
ANALYSIS_SYSTEM_PROMPT = """Ты — умный ассистент, который участвует в групповом чате.
Тебе дают последние сообщения из чата. Твоя задача:

1. Проанализировать тематику беседы
2. Определить, есть ли в последних сообщениях вопрос, на который ты можешь ответить
3. Если есть конкретный вопрос — дай краткий и полезный ответ на него
4. Если вопросы неясные или это просто болтовня — дай общий комментарий к дискуссии
5. Если участвовать нечего — верни пустой ответ

ВАЖНО: Отвечай строго в формате JSON:
{
  "should_reply": true или false,
  "message_id": "ID сообщения на которое отвечаешь (если есть конкретный вопрос) или null",
  "answer": "Твой ответ или комментарий"
}

Если не нужно отвечать вообще, верни: {"should_reply": false, "message_id": null, "answer": ""}
"""

DIRECT_QUESTION_PROMPT = """Ты — ассистент в групповом чате. К тебе обратились напрямую (упомянули или ответили на твоё сообщение).

Ответь на обращение кратко, по делу и вежливо. Учитывай контекст беседы.

ВАЖНО: Отвечай строго в формате JSON:
{
  "answer": "Твой ответ"
}
"""


# ==========================================
# HTTP-хелперы для MAX
# ==========================================
def max_get(path, params):
    headers = {"Authorization": BOT_TOKEN}
    try:
        return requests.get(f"{API_BASE}{path}", params=params, headers=headers, timeout=60)
    except requests.exceptions.SSLError:
        print("⚠️ SSL-ошибка MAX, повторяю без проверки сертификата")
        return requests.get(f"{API_BASE}{path}", params=params, headers=headers, timeout=60, verify=False)


def max_post(path, params, body):
    headers = {"Authorization": BOT_TOKEN, "Content-Type": "application/json"}
    try:
        return requests.post(f"{API_BASE}{path}", params=params, json=body, headers=headers, timeout=60)
    except requests.exceptions.SSLError:
        print("⚠️ SSL-ошибка MAX, повторяю без проверки сертификата")
        return requests.post(f"{API_BASE}{path}", params=params, json=body, headers=headers, timeout=60, verify=False)


# ==========================================
# GigaChat: OAuth-токен
# ==========================================
def get_gigachat_token() -> str:
    now = time.time()
    if _token_cache["token"] and _token_cache["expires_at"] > now + 60:
        return _token_cache["token"]

    resp = requests.post(
        OAUTH_URL,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "RqUID": str(uuid.uuid4()),
            "Authorization": f"Basic {GIGACHAT_CREDENTIALS}",
        },
        data={"scope": "GIGACHAT_API_PERS"},
        verify=VERIFY_SSL,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    token = data["access_token"]
    expires_in = data.get("expires_in", 1800)
    _token_cache["token"] = token
    _token_cache["expires_at"] = now + expires_in

    print(f"🔑 Получен новый GigaChat OAuth-токен (действует {expires_in}с)")
    return token


# ==========================================
# GigaChat: отправка запроса
# ==========================================
def call_gigachat(system_prompt: str, user_prompt: str, max_tokens: int = 1000) -> str:
    """Универсальный вызов GigaChat REST API."""
    token = get_gigachat_token()

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ]

    def do_request(tok):
        return requests.post(
            f"{GIGACHAT_API_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {tok}",
                "Content-Type": "application/json",
            },
            json={
                "model": GIGACHAT_MODEL,
                "messages": messages,
                "temperature": 0.7,
                "max_tokens": max_tokens,
            },
            verify=VERIFY_SSL,
            timeout=90,
        )

    resp = do_request(token)

    if resp.status_code == 401:
        print("⚠️ GigaChat токен протух, получаю новый...")
        _token_cache["token"] = None
        _token_cache["expires_at"] = 0
        token = get_gigachat_token()
        resp = do_request(token)

    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def parse_json_response(raw: str) -> dict:
    """Извлекает JSON из ответа GigaChat (может быть обёрнут в ```json ... ```)."""
    json_match = re.search(r'\{[\s\S]*\}', raw)
    if not json_match:
        return {}
    try:
        return json.loads(json_match.group(0))
    except json.JSONDecodeError:
        return {}


# ==========================================
# Работа с состоянием
# ==========================================
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "marker": 0,
        "processed_message_ids": [],
        "last_general_analysis_timestamp": 0
    }


def save_state(state):
    # Ограничиваем список обработанных ID (последние 1000)
    if "processed_message_ids" in state:
        state["processed_message_ids"] = state["processed_message_ids"][-1000:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ==========================================
# Разбор сообщений MAX
# ==========================================
def get_text(m: dict) -> str:
    body = m.get("body") or {}
    return (body.get("text") or m.get("text") or "").strip()


def get_message_id(m: dict):
    body = m.get("body") or {}
    return m.get("message_id") or body.get("mid") or m.get("mid") or m.get("id")


def get_sender_info(m: dict) -> dict:
    sender = m.get("sender") or {}
    return {
        "user_id": sender.get("user_id"),
        "name": sender.get("name") or sender.get("first_name") or "Неизвестный",
        "is_bot": sender.get("is_bot", False),
        "username": sender.get("username") or ""
    }


def get_reply_to_mid(m: dict):
    """Возвращает message_id, на который это сообщение отвечает (или None)."""
    link = m.get("link") or {}
    if link.get("type") == "reply":
        nested = link.get("message") or {}
        return nested.get("mid") or nested.get("message_id")
    return None


def get_bot_info() -> dict:
    """Возвращает user_id и username бота."""
    try:
        r = max_get("/me", {})
        if r.status_code == 200:
            data = r.json()
            user = data.get("user") or data
            return {
                "user_id": user.get("user_id") or data.get("user_id"),
                "username": user.get("username") or "",
                "name": user.get("name") or ""
            }
    except Exception as e:
        print(f"Не удалось получить инфо бота: {e}")
    return {}


# ==========================================
# Отправка сообщений в MAX
# ==========================================
def send_message(chat_id, text: str, reply_to=None):
    params = {"chat_id": chat_id}
    body = {"text": text[:MAX_TEXT_LEN]}

    if reply_to:
        body["link"] = {"type": "reply", "payload": {"message_id": str(reply_to)}}

    r = max_post("/messages", params, body)

    if r.status_code != 200 and reply_to:
        print(f"⚠️ Ответ с link не прошёл ({r.status_code}): {r.text[:300]}")
        body.pop("link", None)
        r = max_post("/messages", params, body)

    if r.status_code != 200:
        print(f"❌ Ошибка отправки ({r.status_code}): {r.text[:500]}")
        return False

    print(f"✅ Ответ отправлен (reply_to={reply_to})")
    return True


# ==========================================
# Проверка прямых обращений к боту
# ==========================================
def is_direct_to_bot(m: dict, bot_info: dict) -> bool:
    """
    Проверяет, является ли сообщение прямым обращением к боту:
    1. Ответ на сообщение бота (reply to bot)
    2. Упоминание username бота в тексте
    """
    bot_username = (bot_info.get("username") or "").lower()
    bot_name = (bot_info.get("name") or "").lower()

    # Упоминание в тексте
    text = get_text(m).lower()
    if bot_username and f"@{bot_username}" in text:
        return True
    if bot_username and bot_username in text:
        return True

    # Обращение по имени бота
    if bot_name and bot_name in text and len(bot_name) > 2:
        pattern = rf'\b{re.escape(bot_name)}\b'
        if re.search(pattern, text):
            return True

    return False


def is_reply_to_bot_message(m: dict, bot_message_ids: set) -> bool:
    """Проверяет, является ли сообщение ответом на сообщение бота."""
    reply_mid = get_reply_to_mid(m)
    return bool(reply_mid and reply_mid in bot_message_ids)


# ==========================================
# Главная логика
# ==========================================
def force_read_and_respond(chat_id: str, state: dict):
    print(f"🔍 Чтение чата: {chat_id}")

    bot_info = get_bot_info()
    print(f"🤖 Бот: id={bot_info.get('user_id')}, username={bot_info.get('username')}")

    # Загружаем список уже обработанных сообщений
    processed_ids = set(state.get("processed_message_ids", []))
    last_analysis_time = state.get("last_general_analysis_timestamp", 0)
    print(f"📋 Уже обработано сообщений: {len(processed_ids)}")
    print(f"⏰ Последний общий анализ: {time.strftime('%H:%M:%S', time.localtime(last_analysis_time)) if last_analysis_time else 'никогда'}")

    # Получаем последние 15 сообщений
    r = max_get("/messages", {"chat_id": chat_id, "count": 15})
    print(f"📡 GET /messages → статус {r.status_code}")

    if r.status_code != 200:
        print(f"❌ Не удалось прочитать историю: {r.text[:500]}")
        return

    messages_raw = (r.json() or {}).get("messages") or []
    print(f"📥 Получено сообщений: {len(messages_raw)}")

    # Сортируем по времени (старые → новые)
    messages_raw.sort(key=lambda x: x.get("timestamp", 0))

    # Первый проход: собираем ID сообщений самого бота
    bot_message_ids = set()
    for m in messages_raw:
        sender = get_sender_info(m)
        if sender["user_id"] == bot_info.get("user_id") or sender["is_bot"]:
            mid = get_message_id(m)
            if mid:
                bot_message_ids.add(str(mid))

    print(f"🤖 Найдено {len(bot_message_ids)} сообщений бота в истории")

    # Второй проход: обрабатываем сообщения
    direct_messages = []
    all_human_messages = []
    new_messages_for_analysis = []  # Сообщения новее последнего анализа

    for m in messages_raw:
        sender = get_sender_info(m)
        msg_id = get_message_id(m)
        text = get_text(m)
        timestamp = m.get("timestamp", 0)

        if not text or not msg_id:
            continue

        # Пропускаем сообщения самого бота
        if sender["user_id"] == bot_info.get("user_id") or sender["is_bot"]:
            continue

        msg_data = {
            "message_id": str(msg_id),
            "author_name": sender["name"],
            "text": text,
            "timestamp": timestamp,
            "is_direct": False,
            "is_reply_to_bot": False
        }

        all_human_messages.append(msg_data)

        # ======= ЗАЩИТА ОТ ПОВТОРОВ =======
        # Пропускаем уже обработанные сообщения
        if str(msg_id) in processed_ids:
            continue

        # Для общего анализа: пропускаем сообщения старше последнего анализа
        # (чтобы не анализировать одну и ту же беседу повторно)
        if timestamp <= last_analysis_time:
            continue

        new_messages_for_analysis.append(msg_data)

        # Проверяем прямое обращение
        if is_direct_to_bot(m, bot_info):
            msg_data["is_direct"] = True
            direct_messages.append(msg_data)
            continue

        # Проверяем ответ на сообщение бота
        if is_reply_to_bot_message(m, bot_message_ids):
            msg_data["is_reply_to_bot"] = True
            direct_messages.append(msg_data)

    print(f"👥 Всего сообщений от людей: {len(all_human_messages)}")
    print(f"🆕 Новых сообщений (после последнего анализа): {len(new_messages_for_analysis)}")
    print(f"🎯 Прямых обращений к боту (новых): {len(direct_messages)}")

    # ======= ОБРАБОТКА ПРЯМЫХ ОБРАЩЕНИЙ =======
    if direct_messages:
        print(f"⚡ Обрабатываю {len(direct_messages)} прямых обращений")
        
        # Формируем контекст из всей истории
        history_text = ""
        for msg in all_human_messages[-35:]:
            history_text += f"[ID: {msg['message_id']}] {msg['author_name']}: {msg['text']}\n"

        # Отвечаем на каждое прямое обращение
        for dm in direct_messages:
            print(f"\n📨 Прямое обращение [{dm['message_id']}] от {dm['author_name']}: {dm['text'][:80]}...")

            user_prompt = f"""Контекст последних сообщений в чате:
{history_text}

К тебе обратились напрямую:
{dm['author_name']}: {dm['text']}

Ответь кратко и по делу."""

            try:
                raw_answer = call_gigachat(DIRECT_QUESTION_PROMPT, user_prompt, max_tokens=600)
                result = parse_json_response(raw_answer)
                answer = result.get("answer", "").strip()

                if not answer:
                    # Если JSON не пришёл, берём сырой текст
                    answer = raw_answer.strip()
                    answer = re.sub(r'^```(?:json)?\s*', '', answer)
                    answer = re.sub(r'\s*```$', '', answer).strip()

                if answer:
                    print(f"💬 Ответ: {answer[:100]}...")
                    ok = send_message(chat_id, answer, reply_to=dm["message_id"])
                    if ok:
                        # ======= ДОБАВЛЯЕМ В ОБРАБОТАННЫЕ =======
                        state["processed_message_ids"].append(dm["message_id"])
                        save_state(state)
                    time.sleep(1.2)
                else:
                    print("⚠️ Пустой ответ от GigaChat")
            except Exception as e:
                print(f"❌ Ошибка обработки прямого обращения: {e}")

    # ======= ОБЩИЙ АНАЛИЗ БЕСЕДЫ (если не было прямых обращений) =======
    elif new_messages_for_analysis:
        print("\n🤔 Прямых обращений нет. Делаю общий анализ новых сообщений...")
        
        # Берём последние 35 новых сообщений для анализа
        recent = new_messages_for_analysis[-35:]
        history_text = ""
        for msg in recent:
            history_text += f"[ID: {msg['message_id']}] {msg['author_name']}: {msg['text']}\n"

        user_prompt = f"""Вот последние сообщения из группового чата:

{history_text}

Проанализируй эту беседу и реши, нужно ли тебе ответить.
Если есть конкретный вопрос — ответь на него (укажи message_id).
Если это общая дискуссия — дай комментарий.
Если участвовать нечего — верни should_reply: false.

Ответь СТРОГО в формате JSON:"""

        try:
            raw_answer = call_gigachat(ANALYSIS_SYSTEM_PROMPT, user_prompt, max_tokens=1000)
            print(f"📥 Сырой ответ GigaChat: {raw_answer[:300]}")
            
            result = parse_json_response(raw_answer)

            if not result:
                print("❌ Не удалось распарсить JSON из ответа GigaChat")
                # Обновляем время анализа, чтобы не анализировать эти же сообщения повторно
                state["last_general_analysis_timestamp"] = int(time.time() * 1000)
                save_state(state)
                return

            if not result.get("should_reply"):
                print("💭 GigaChat решила не отвечать на эту беседу")
                # Обновляем время анализа
                state["last_general_analysis_timestamp"] = int(time.time() * 1000)
                save_state(state)
                return

            answer = result.get("answer", "").strip()
            if not answer:
                print("💭 GigaChat вернула пустой ответ")
                state["last_general_analysis_timestamp"] = int(time.time() * 1000)
                save_state(state)
                return

            reply_to_msg_id = result.get("message_id")

            # Проверяем, что message_id существует в нашем списке
            valid_msg_ids = {m["message_id"] for m in recent}
            if reply_to_msg_id and reply_to_msg_id not in valid_msg_ids:
                print(f"⚠️ message_id {reply_to_msg_id} не найден, отправляю как обычный комментарий")
                reply_to_msg_id = None

            print(f"💬 Отправляю ответ (reply_to={reply_to_msg_id}): {answer[:100]}...")
            ok = send_message(chat_id, answer, reply_to=reply_to_msg_id)
            
            if ok:
                # ======= ОБНОВЛЯЕМ СОСТОЯНИЕ =======
                current_time_ms = int(time.time() * 1000)
                state["last_general_analysis_timestamp"] = current_time_ms
                
                # Добавляем все проанализированные сообщения в обработанные
                # (чтобы не анализировать их повторно)
                for msg in recent:
                    if msg["message_id"] not in state["processed_message_ids"]:
                        state["processed_message_ids"].append(msg["message_id"])
                
                save_state(state)
                print("✅ Ответ успешно отправлен и состояние обновлено")

        except Exception as e:
            print(f"❌ Ошибка при общем анализе: {e}")
    else:
        print("❌ Нет новых сообщений для анализа")

    save_state(state)


# ==========================================
# Главная функция
# ==========================================
def main():
    if not BOT_TOKEN:
        print("Критическая ошибка: не задан MAX_BOT_TOKEN.")
        return

    if not GIGACHAT_CREDENTIALS:
        print("Критическая ошибка: не задан GIGACHAT_CREDENTIALS.")
        return

    state = load_state()

    try:
        if FORCE_CHAT_ID:
            force_read_and_respond(FORCE_CHAT_ID, state)
        else:
            print("⚠️ FORCE_CHAT_ID не задан")
    finally:
        save_state(state)
        print("✅ Обработка завершена. Состояние сохранено.")


if __name__ == "__main__":
    main()
