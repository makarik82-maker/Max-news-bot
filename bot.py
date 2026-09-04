import os
import json
import requests

# ==========================================
# Конфигурация
# ==========================================
BOT_TOKEN = os.getenv("MAX_BOT_TOKEN")
API_BASE = os.getenv("MAX_API_BASE", "https://botapi.max.ru")

ZAI_API_KEY = os.getenv("ZAI_API_KEY")
ZAI_MODEL = os.getenv("ZAI_MODEL", "glm-4-flash")
ZAI_BASE_URL = os.getenv("ZAI_BASE_URL", "https://api.z.ai/api/paas/v4")

# Режим принудительного чтения конкретного канала
FORCE_CHAT_ID = os.getenv("FORCE_CHAT_ID")  # например: -71777603207295

STATE_FILE = "state.json"
MAX_TEXT_LEN = 3900


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
    # Ограничиваем список обработанных ID, чтобы файл не разрастался
    if "replied_messages" in state:
        state["replied_messages"] = state["replied_messages"][-200:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ==========================================
# Разбор сообщений
# ==========================================
def get_text(message: dict) -> str:
    body = message.get("body") or {}
    return (body.get("text") or message.get("text") or "").strip()


def get_message_id(message: dict) -> str | None:
    """Получаем ID сообщения (может быть в разных полях)."""
    return (
        message.get("message_id") or
        message.get("id") or
        (message.get("recipients") or [{}])[0].get("message_id") or
        message.get("link")  # fallback
    )


def is_bot_message(message: dict) -> bool:
    sender = message.get("sender") or {}
    return bool(sender.get("is_bot") or sender.get("type") == "bot")


# ==========================================
# Z.AI
# ==========================================
def ask_zai(prompt: str) -> str:
    url = f"{ZAI_BASE_URL.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {ZAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": ZAI_MODEL,
        "messages": [
            {"role": "system", "content": "Ты полезный и вежливый ассистент в канале. Отвечай кратко, по делу и на том языке, на котором к тебе обратились."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.7,
    }
    response = requests.post(url, headers=headers, json=payload, timeout=60)
    if response.status_code != 200:
        print(f"Ошибка Z.AI API: {response.status_code} - {response.text[:500]}")
        response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"].strip()


# ==========================================
# Отправка в MAX (с поддержкой reply_to)
# ==========================================
def send_message(chat_id, text: str, reply_to_message_id: str | None = None):
    url = f"{API_BASE}/messages"
    headers = {
        "Authorization": BOT_TOKEN,
        "Content-Type": "application/json"
    }

    payload = {
        "text": text[:MAX_TEXT_LEN],
        "chat_id": chat_id,
    }
    
    # Если нужно ответить комментарием на конкретное сообщение
    if reply_to_message_id:
        payload["reply_to"] = reply_to_message_id

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=60)
        if response.status_code != 200:
            print(f"Ошибка отправки ({response.status_code}): {response.text[:500]}")
        else:
            print(f"✅ Ответ отправлен (reply_to={reply_to_message_id})")
    except Exception as e:
        print(f"Ошибка отправки в MAX: {e}")


# ==========================================
# Режим принудительного чтения канала
# ==========================================
def force_read_channel(chat_id: str, state: dict):
    """Читает последние сообщения из канала и отвечает на 3 последних."""
    print(f"🔍 Принудительное чтение канала: {chat_id}")

    headers = {"Authorization": BOT_TOKEN}
    replied_messages = set(state.get("replied_messages", []))

    try:
        # Запрашиваем последние 10 сообщений (чтобы было из чего выбрать 3 валидных)
        response = requests.get(
            f"{API_BASE}/messages",
            params={"chat_id": chat_id, "count": 10},
            headers=headers,
            timeout=60,
        )

        if response.status_code != 200:
            print(f"❌ Ошибка чтения канала: {response.status_code} - {response.text[:500]}")
            return

        messages = response.json().get("messages", [])
        print(f"📥 Получено сообщений: {len(messages)}")

        # Фильтруем: только от людей, не свои, не отвеченные ранее
        valid_messages = []
        for msg in messages:
            if is_bot_message(msg):
                continue
            text = get_text(msg)
            if not text:
                continue
            msg_id = get_message_id(msg)
            if not msg_id:
                continue
            if str(msg_id) in replied_messages:
                continue
            valid_messages.append((msg_id, text))

        # Сортируем по ID (больший ID = более новое сообщение)
        valid_messages.sort(key=lambda x: int(x[0]) if str(x[0]).isdigit() else 0, reverse=True)

        # Берём последние 3
        to_reply = valid_messages[:3]
        print(f"💬 Буду отвечать на {len(to_reply)} сообщений")

        # Отвечаем в обратном порядке: сначала на самое старое, потом на более новые
        for msg_id, text in reversed(to_reply):
            print(f"Обработка: [{msg_id}] {text[:50]}...")
            try:
                answer = ask_zai(text)
                send_message(chat_id, answer, reply_to_message_id=str(msg_id))
                state["replied_messages"].append(str(msg_id))
            except Exception as e:
                print(f"Ошибка при генерации/отправке: {e}")
                send_message(chat_id, "⚠️ Извините, произошла ошибка.", reply_to_message_id=str(msg_id))

    except Exception as e:
        print(f"Критическая ошибка при чтении канала: {e}")


# ==========================================
# Стандартный режим (через updates)
# ==========================================
def process_updates(state: dict):
    """Стандартный режим: получение новых сообщений через /updates."""
    headers = {"Authorization": BOT_TOKEN}
    marker = state.get("marker", 0)
    print(f"Запуск обработки. Текущий marker: {marker}")

    try:
        response = requests.get(
            f"{API_BASE}/updates",
            params={"marker": marker, "limit": 100},
            headers=headers,
            timeout=60,
        )

        if response.status_code != 200:
            print(f"Ошибка API MAX: {response.status_code} - {response.text}")
            return

        data = response.json()
        updates = data.get("updates", [])

        if not updates:
            print("Новых сообщений нет.")
            return

        print(f"Получено обновлений: {len(updates)}")

        for update in updates:
            update_id = update.get("update_id") or update.get("id")
            message = update.get("message") or update.get("data") or {}

            if not message or is_bot_message(message):
                if update_id:
                    marker = max(marker, int(update_id) + 1)
                continue

            text = get_text(message)
            recipient = message.get("recipient") or {}
            chat_id = recipient.get("chat_id") or message.get("chat_id")
            user_id = recipient.get("user_id") or message.get("user_id")
            target = chat_id or user_id

            if text and target:
                print(f"Обработка запроса: {text[:50]}...")
                try:
                    answer = ask_zai(text)
                    if chat_id:
                        send_message(chat_id, answer)
                    else:
                        send_message(user_id, answer)
                except Exception as e:
                    print(f"Ошибка: {e}")

            if update_id:
                marker = max(marker, int(update_id) + 1)

        state["marker"] = marker

    except Exception as e:
        print(f"Критическая ошибка: {e}")


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
    print(f"✅ Обработка завершена. Состояние сохранено.")


if __name__ == "__main__":
    main()
