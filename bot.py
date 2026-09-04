import os
import json
import time
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==========================================
# Конфигурация
# ==========================================
BOT_TOKEN = os.getenv("MAX_BOT_TOKEN")
API_BASE = os.getenv("MAX_API_BASE", "https://platform-api2.max.ru")

ZAI_API_KEY = os.getenv("ZAI_API_KEY")
ZAI_MODEL = os.getenv("ZAI_MODEL", "glm-4.7-flash")  # <-- Используем GLM-4.7-Flash
ZAI_BASE_URL = os.getenv("ZAI_BASE_URL", "https://api.z.ai/api/paas/v4")

FORCE_CHAT_ID = os.getenv("FORCE_CHAT_ID")

STATE_FILE = "state.json"
MAX_TEXT_LEN = 4000


# ==========================================
# HTTP-хелперы с фолбэком SSL
# ==========================================
def max_get(path, params):
    headers = {"Authorization": BOT_TOKEN}
    try:
        return requests.get(f"{API_BASE}{path}", params=params, headers=headers, timeout=60)
    except requests.exceptions.SSLError:
        print("⚠️ SSL-ошибка, повторяю без проверки сертификата")
        return requests.get(f"{API_BASE}{path}", params=params, headers=headers, timeout=60, verify=False)


def max_post(path, params, body):
    headers = {"Authorization": BOT_TOKEN, "Content-Type": "application/json"}
    try:
        return requests.post(f"{API_BASE}{path}", params=params, json=body, headers=headers, timeout=60)
    except requests.exceptions.SSLError:
        print("⚠️ SSL-ошибка, повторяю без проверки сертификата")
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
# Z.AI API (с автоматическим подбором модели)
# ==========================================
def ask_zai(prompt: str) -> str:
    """
    Отправляет запрос в Z.AI с моделью GLM-4.7-Flash.
    Если модель недоступна, автоматически пробует другие модели.
    """
    url = f"{ZAI_BASE_URL.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {ZAI_API_KEY}", "Content-Type": "application/json"}
    
    # Список моделей в порядке предпочтения
    # GLM-4.7-Flash — основная, остальные — фолбэк
    models_to_try = [
        "glm-4.7-flash",      # Основная модель
        "glm-5-flash",        # Фолбэк 1
        "glm-5",              # Фолбэк 2
        "glm-4-plus",         # Фолбэк 3
        "glm-4"               # Фолбэк 4
    ]
    
    for model in models_to_try:
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": "Ты полезный и вежливый ассистент в чате. Отвечай кратко, по делу и на том языке, на котором к тебе обратились."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "temperature": 0.7,
        }
        
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=60)
            
            if response.status_code == 200:
                print(f"✅ Z.AI ответила через модель: {model}")
                return response.json()["choices"][0]["message"]["content"].strip()
            
            elif "Unknown Model" in response.text:
                print(f"⚠️ Модель {model} недоступна, пробую следующую...")
                continue
            
            else:
                print(f"Ошибка Z.AI API с моделью {model}: {response.status_code} - {response.text[:300]}")
                break
                
        except Exception as e:
            print(f"Исключение при попытке с моделью {model}: {e}")
            continue
    
    raise Exception("Ни одна из моделей Z.AI не сработала")


# ==========================================
# Отправка сообщений в MAX (с комментариями)
# ==========================================
def send_message(chat_id, text: str, reply_to=None):
    params = {"chat_id": chat_id}
    body = {"text": text[:MAX_TEXT_LEN]}

    # Ответ-комментарий на конкретное сообщение
    if reply_to:
        body["link"] = {"type": "reply", "payload": {"message_id": str(reply_to)}}

    r = max_post("/messages", params, body)

    # Если сервер не принял link как reply — шлём без него
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
        print("❗ Убедитесь, что бот — АДМИНИСТРАТОР чата, иначе история не выдаётся.")
        return

    messages = (r.json() or {}).get("messages") or []
    print(f"📥 Получено сообщений: {len(messages)}")

    replied = set(str(x) for x in state.get("replied_messages", []))

    valid = []
    for m in messages:
        # Пропускаем сообщения от самого бота
        if get_sender_id(m) == bot_id:
            continue
        
        text = get_text(m)
        msg_id = get_message_id(m)
        
        if not text or not msg_id:
            continue
        
        # Пропускаем уже отвеченные
        if str(msg_id) in replied:
            continue
        
        ts = m.get("timestamp") or 0
        valid.append((ts, str(msg_id), text))

    # Сортируем по времени (новые первыми)
    valid.sort(key=lambda x: x[0], reverse=True)

    # Берём последние 3
    to_reply = valid[:3]
    print(f"💬 Отвечаю на {len(to_reply)} сообщений")

    # Отвечаем начиная со старого, чтобы новые ответы были ниже
    for ts, msg_id, text in reversed(to_reply):
        print(f"Обработка: [{msg_id}] {text[:60]}...")
        try:
            answer = ask_zai(text)
            ok = send_message(chat_id, answer, reply_to=msg_id)
            if ok:
                state["replied_messages"].append(msg_id)
        except Exception as e:
            print(f"Ошибка при генерации/отправке: {e}")
        
        # Задержка между сообщениями (лимит MAX: 2 сообщ/сек)
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
            if update_id:
                marker = max(marker, int(update_id) + 1)
            continue

        text = get_text(message)
        recipient = message.get("recipient") or {}
        chat_id = recipient.get("chat_id")
        user_id = recipient.get("user_id")
        target = chat_id or user_id

        if text and target:
            print(f"Обработка запроса: {text[:60]}...")
            try:
                answer = ask_zai(text)
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
    if not BOT_TOKEN or not ZAI_API_KEY:
        print("Критическая ошибка: не заданы токены.")
        return

    state = load_state()

    if FORCE_CHAT_ID:
        # Режим принудительного чтения канала
        force_read_channel(FORCE_CHAT_ID, state)
    else:
        # Стандартный режим
        process_updates(state)

    save_state(state)
    print("✅ Обработка завершена. Состояние сохранено.")


if __name__ == "__main__":
    main()
