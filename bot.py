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

# Кэш OAuth-токена
_token_cache = {"token": None, "expires_at": 0}


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
# GigaChat: получение OAuth-токена
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
# GigaChat: анализ беседы через REST API
# ==========================================
def ask_gigachat_analysis(messages_for_analysis: list) -> dict:
    """
    Отправляет список сообщений в GigaChat и получает JSON-ответ.
    Возвращает словарь: {"should_reply": bool, "message_id": str|None, "answer": str}
    """
    token = get_gigachat_token()

    # Формируем промпт с историей сообщений
    history_text = ""
    for msg in messages_for_analysis:
        author = msg.get("author_name", "Неизвестный")
        text = msg.get("text", "").strip()
        msg_id = msg.get("message_id", "")
        if text:
            history_text += f"[ID: {msg_id}] {author}: {text}\n"

    user_prompt = f"""Вот последние сообщения из группового чата:

{history_text}

Проанализируй эту беседу и реши, нужно ли тебе ответить.
Если есть конкретный вопрос — ответь на него.
Если это общая дискуссия — дай комментарий.
Если участвовать нечего — верни should_reply: false.

Ответь СТРОГО в формате JSON:"""

    messages = [
        {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
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
                "max_tokens": 1000,
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
    raw_answer = resp.json()["choices"][0]["message"]["content"].strip()
    print(f"📥 Сырой ответ GigaChat: {raw_answer[:300]}")

    # Парсим JSON из ответа (может быть обёрнут в ```json ... ```)
    json_match = re.search(r'\{[\s\S]*\}', raw_answer)
    if not json_match:
        print(f"❌ Не удалось найти JSON в ответе GigaChat")
        return {"should_reply": False, "message_id": None, "answer": ""}

    try:
        result = json.loads(json_match.group(0))
        print(f"✅ GigaChat вернула решение: should_reply={result.get('should_reply')}")
        return result
    except json.JSONDecodeError as e:
        print(f"❌ Ошибка парсинга JSON: {e}")
        return {"should_reply": False, "message_id": None, "answer": ""}


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
    return {"marker": 0, "last_analysis_time": 0}


def save_state(state):
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
        "is_bot": sender.get("is_bot", False)
    }


def get_bot_id():
    try:
        r = max_get("/me", {})
        if r.status_code == 200:
            data = r.json()
            return data.get("user_id") or (data.get("user") or {}).get("user_id")
    except Exception as e:
        print(f"Не удалось получить ID бота: {e}")
    return None


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
# Принудительное чтение и анализ чата
# ==========================================
def force_read_and_analyze(chat_id: str, state: dict):
    print(f"🔍 Чтение и анализ чата: {chat_id}")

    bot_id = get_bot_id()
    print(f"🤖 ID бота: {bot_id}")

    # Получаем больше сообщений (10), чтобы после фильтрации осталось ~5
    r = max_get("/messages", {"chat_id": chat_id, "count": 10})
    print(f"📡 GET /messages → статус {r.status_code}")

    if r.status_code != 200:
        print(f"❌ Не удалось прочитать историю: {r.text[:500]}")
        return

    messages_raw = (r.json() or {}).get("messages") or []
    print(f"📥 Получено сообщений: {len(messages_raw)}")

    # Фильтруем и готовим для анализа
    messages_for_analysis = []
    for m in messages_raw:
        sender = get_sender_info(m)
        
        # Пропускаем сообщения от бота
        if sender["user_id"] == bot_id or sender["is_bot"]:
            continue
        
        text = get_text(m)
        msg_id = get_message_id(m)
        
        if not text or not msg_id:
            continue

        messages_for_analysis.append({
            "message_id": str(msg_id),
            "author_name": sender["name"],
            "text": text,
            "timestamp": m.get("timestamp", 0)
        })

    # Сортируем по времени (старые первыми, новые последними)
    messages_for_analysis.sort(key=lambda x: x["timestamp"])

    # Берём последние 35
    messages_for_analysis = messages_for_analysis[-35:]
    print(f"📊 Анализирую {len(messages_for_analysis)} сообщений")

    if not messages_for_analysis:
        print("❌ Нет сообщений для анализа")
        return

    # Запрашиваем анализ у GigaChat
    try:
        result = ask_gigachat_analysis(messages_for_analysis)
    except Exception as e:
        print(f"❌ Ошибка при запросе к GigaChat: {e}")
        return

    # Проверяем, нужно ли отвечать
    if not result.get("should_reply"):
        print("💭 GigaChat решила не отвечать на эту беседу")
        return

    answer = result.get("answer", "").strip()
    if not answer:
        print("💭 GigaChat вернула пустой ответ")
        return

    # Определяем, на какое сообщение отвечать (если есть)
    reply_to_msg_id = result.get("message_id")

    # Проверяем, что message_id действительно существует в нашем списке
    valid_msg_ids = {m["message_id"] for m in messages_for_analysis}
    if reply_to_msg_id and reply_to_msg_id not in valid_msg_ids:
        print(f"⚠️ message_id {reply_to_msg_id} не найден в истории, отправляю как обычный комментарий")
        reply_to_msg_id = None

    # Отправляем ответ
    print(f"💬 Отправляю ответ (reply_to={reply_to_msg_id}): {answer[:100]}...")
    ok = send_message(chat_id, answer, reply_to=reply_to_msg_id)

    if ok:
        # Обновляем время последнего анализа
        state["last_analysis_time"] = time.time()
        save_state(state)
        print("✅ Ответ успешно отправлен")


# ==========================================
# Стандартный режим (через updates) — отключён
# ==========================================
def process_updates(state: dict):
    print("⚠️ Стандартный режим updates отключён. Используйте FORCE_CHAT_ID.")
    # Можно оставить пустым или реализовать по аналогии


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
            force_read_and_analyze(FORCE_CHAT_ID, state)
        else:
            process_updates(state)
    finally:
        save_state(state)
        print("✅ Обработка завершена. Состояние сохранено.")


if __name__ == "__main__":
    main()
