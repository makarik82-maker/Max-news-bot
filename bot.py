import os
import json
import time
import requests
import urllib3
from gigachat import GigaChat
from gigachat.models import Chat, Messages, Roles  # <-- ДОБАВЛЕНО: импорты моделей GigaChat

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==========================================
# Конфигурация
# ==========================================
BOT_TOKEN = os.getenv("MAX_BOT_TOKEN")
API_BASE = os.getenv("MAX_API_BASE", "https://platform-api2.max.ru")

GIGACHAT_CREDENTIALS = os.getenv("GIGACHAT_CREDENTIALS")
GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
GIGACHAT_MODEL = os.getenv("GIGACHAT_MODEL", "GigaChat")

FORCE_CHAT_ID = os.getenv("FORCE_CHAT_ID")
STATE_FILE = "state.json"
MAX_TEXT_LEN = 4000


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
# Работа с состоянием
# ==========================================
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"marker": 0, "replied_messages": []}


def save_state(state):
    if "replied_messages" in state:
        state["replied_messages"] = state["replied_messages"][-200:]
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
# GigaChat (с автоматическим подбором модели)
# ==========================================
def ask_gigachat(prompt: str) -> str:
    models_to_try = ["GigaChat", "GigaChat-Pro", "GigaChat-Max", "GigaChat-Lite"]

    system_prompt = (
        "Ты полезный и вежливый ассистент в чате. "
        "Отвечай кратко, по делу и на том языке, на котором к тебе обратились."
    )

    if GIGACHAT_MODEL and GIGACHAT_MODEL not in models_to_try:
        models_to_try.insert(0, GIGACHAT_MODEL)

    # ИСПРАВЛЕНО: формируем правильный объект Chat для SDK GigaChat
    payload = Chat(
        messages=[
            Messages(role=Roles.SYSTEM, content=system_prompt),
            Messages(role=Roles.USER, content=prompt)
        ]
    )

    for model in models_to_try:
        try:
            with GigaChat(
                credentials=GIGACHAT_CREDENTIALS,
                scope=GIGACHAT_SCOPE,
                model=model,
                verify_ssl_certs=False,
            ) as client:
                response = client.chat(payload)
                answer = response.choices[0].message.content.strip()
                print(f"✅ GigaChat ответила через модель: {model}")
                return answer

        except Exception as e:
            err_str = str(e)
            print(f"⚠️ Ошибка с моделью {model}: {err_str[:200]}")

            if "model" in err_str.lower() or "not found" in err_str.lower() or "does not exist" in err_str.lower():
                print("Пробую следующую модель...")
                continue
            elif "auth" in err_str.lower() or "401" in err_str:
                print("❌ Ошибка авторизации GigaChat. Проверьте GIGACHAT_CREDENTIALS.")
                break
            elif "timeout" in err_str.lower() or "overloaded" in err_str.lower():
                print("⏱️ Таймаут или перегруз, пробую снова через 2 сек...")
                time.sleep(2)
                continue
            else:
                continue

    raise Exception("Ни одна из моделей GigaChat не сработала")


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

    for ts, msg_id, text in reversed(to_reply):
        print(f"Обработка: [{msg_id}] {text[:60]}...")
        try:
            answer = ask_gigachat(text)
            ok = send_message(chat_id, answer, reply_to=msg_id)
            if ok:
                state["replied_messages"].append(msg_id)
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
            try:
                answer = ask_gigachat(text)
                send_message(target, answer)
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
