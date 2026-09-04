import os
import json
import time
import uuid
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

# Настройки GigaChat (как в вашем рабочем коде)
OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
GIGACHAT_API_URL = "https://api.giga.chat/v1"
VERIFY_SSL = False
GIGACHAT_MODEL = "GigaChat-3-Ultra"

SYSTEM_PERSONA = (
    "Ты — дружелюбный собеседник, отвечаешь на русском языке. "
    "Отвечай кратко (1-3 предложения), по делу и вежливо."
)

# Кэш OAuth-токена (токен действует ~30 минут)
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
    """Получает OAuth-токен GigaChat через Basic Auth (как в вашем рабочем коде)."""
    # Если есть валидный кэшированный токен — используем его
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
    # Токен обычно действует ~30 минут, кэшируем с запасом
    expires_in = data.get("expires_in", 1800)
    _token_cache["token"] = token
    _token_cache["expires_at"] = now + expires_in

    print(f"🔑 Получен новый GigaChat OAuth-токен (действует {expires_in}с)")
    return token


# ==========================================
# GigaChat: генерация текста через REST API
# ==========================================
def ask_gigachat(prompt: str, history: list = None) -> str:
    """Отправляет запрос в GigaChat через REST API (OpenAI-совместимый)."""
    token = get_gigachat_token()

    messages = [{"role": "system", "content": SYSTEM_PERSONA}]
    if history:
        messages.extend(history[-20:])  # Берём последние 20 сообщений контекста
    messages.append({"role": "user", "content": prompt})

    resp = requests.post(
        f"{GIGACHAT_API_URL}/chat/completions",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={
            "model": GIGACHAT_MODEL,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 800,
        },
        verify=VERIFY_SSL,
        timeout=60,
    )

    if resp.status_code == 401:
        # Токен протух, сбрасываем кэш и пробуем ещё раз
        print("⚠️ GigaChat токен протух, получаю новый...")
        _token_cache["token"] = None
        _token_cache["expires_at"] = 0
        token = get_gigachat_token()
        resp = requests.post(
            f"{GIGACHAT_API_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "model": GIGACHAT_MODEL,
                "messages": messages,
                "temperature": 0.7,
                "max_tokens": 800,
            },
            verify=VERIFY_SSL,
            timeout=60,
        )

    resp.raise_for_status()
    answer = resp.json()["choices"][0]["message"]["content"].strip()
    print(f"✅ GigaChat ответила (модель: {GIGACHAT_MODEL})")
    return answer


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
    return {"marker": 0, "replied_messages": [], "history": {}}


def save_state(state):
    if "replied_messages" in state:
        state["replied_messages"] = state["replied_messages"][-200:]
    # Ограничиваем историю для каждого чата
    if "history" in state:
        for chat_id in list(state["history"].keys()):
            state["history"][chat_id] = state["history"][chat_id][-20:]
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


def get_sender_id(m: dict):
    sender = m.get("sender") or {}
    return sender.get("user_id")


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
# Отправка сообщений в MAX (с комментариями)
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
# Принудительное чтение чата/канала
# ==========================================
def force_read_channel(chat_id: str, state: dict):
    print(f"🔍 Принудительное чтение чата: {chat_id}")

    bot_id = get_bot_id()
    print(f"🤖 ID бота: {bot_id}")

    r = max_get("/messages", {"chat_id": chat_id, "count": 20})
    print(f"📡 GET /messages → статус {r.status_code}")
    print(f"📄 Сырой ответ: {r.text[:1000]}")

    if r.status_code != 200:
        print("❌ Не удалось прочитать историю.")
        return

    messages = (r.json() or {}).get("messages") or []
    print(f"📥 Получено сообщений: {len(messages)}")

    replied = set(str(x) for x in state.get("replied_messages", []))

    valid = []
    for m in messages:
        if get_sender_id(m) == bot_id:
            continue
        text = get_text(m)
        msg_id = get_message_id(m)
        if not text or not msg_id:
            continue
        if str(msg_id) in replied:
            continue
        ts = m.get("timestamp") or 0
        valid.append((ts, str(msg_id), text))

    valid.sort(key=lambda x: x[0], reverse=True)
    to_reply = valid[:3]
    print(f"💬 Отвечаю на {len(to_reply)} сообщений")

    # Получаем историю диалога для этого чата
    chat_history_key = str(chat_id)
    history = state.setdefault("history", {}).get(chat_history_key, [])

    for ts, msg_id, text in reversed(to_reply):
        print(f"Обработка: [{msg_id}] {text[:60]}...")
        try:
            answer = ask_gigachat(text, history=history)
            ok = send_message(chat_id, answer, reply_to=msg_id)
            if ok:
                state["replied_messages"].append(msg_id)
                # Добавляем в историю диалога
                history.append({"role": "user", "content": text})
                history.append({"role": "assistant", "content": answer})
                # Ограничиваем историю 20 последними репликами (10 пар)
                if len(history) > 20:
                    del history[:-20]
                state["history"][chat_history_key] = history
                save_state(state)
        except Exception as e:
            print(f"Ошибка при генерации/отправке: {e}")
        time.sleep(1.1)


# ==========================================
# Стандартный режим (через updates)
# ==========================================
def process_updates(state: dict):
    marker = state.get("marker", 0)
    print(f"Запуск обработки. Текущий marker: {marker}")

    r = max_get("/updates", {"marker": marker, "limit": 100})
    if r.status_code != 200:
        print(f"Ошибка API MAX: {r.status_code} - {r.text[:500]}")
        return

    updates = (r.json() or {}).get("updates", [])
    if not updates:
        print("Новых сообщений нет.")
        return

    print(f"Получено обновлений: {len(updates)}")
    for update in updates:
        update_id = update.get("update_id") or update.get("id")
        message = update.get("message") or update.get("data") or {}
        if not message:
            if update_id: marker = max(marker, int(update_id) + 1)
            continue

        text = get_text(message)
        recipient = message.get("recipient") or {}
        chat_id = recipient.get("chat_id")
        user_id = recipient.get("user_id")
        target = chat_id or user_id

        if text and target:
            print(f"Обработка запроса: {text[:60]}...")
            chat_history_key = str(target)
            history = state.setdefault("history", {}).get(chat_history_key, [])
            try:
                answer = ask_gigachat(text, history=history)
                send_message(target, answer)
                history.append({"role": "user", "content": text})
                history.append({"role": "assistant", "content": answer})
                if len(history) > 20:
                    del history[:-20]
                state["history"][chat_history_key] = history
            except Exception as e:
                print(f"Ошибка: {e}")

        if update_id:
            marker = max(marker, int(update_id) + 1)

    state["marker"] = marker


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
            force_read_channel(FORCE_CHAT_ID, state)
        else:
            process_updates(state)
    finally:
        save_state(state)
        print("✅ Обработка завершена. Состояние сохранено.")


if __name__ == "__main__":
    main()
