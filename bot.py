import os
import json
import requests
from gigachat import GigaChat

# --- Конфигурация из переменных окружения GitHub ---
BOT_TOKEN = os.getenv("MAX_BOT_TOKEN")
API_BASE = os.getenv("MAX_API_BASE", "https://botapi.max.ru") # Уточни URL, если он другой
GIGACHAT_CREDENTIALS = os.getenv("GIGACHAT_CREDENTIALS")
GIGACHAT_MODEL = os.getenv("GIGACHAT_MODEL", "GigaChat")
STATE_FILE = "state.json"

# --- Работа с состоянием (marker) ---
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"marker": 0}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# --- Логика бота ---
def get_text(message: dict) -> str:
    body = message.get("body") or {}
    return (body.get("text") or message.get("text") or "").strip()

def get_target(message: dict) -> dict | None:
    recipient = message.get("recipient") or {}
    chat_id = recipient.get("chat_id") or message.get("chat_id")
    user_id = recipient.get("user_id") or message.get("user_id")
    if chat_id: return {"chat_id": chat_id}
    if user_id: return {"user_id": user_id}
    return None

def is_bot_message(message: dict) -> bool:
    sender = message.get("sender") or {}
    return bool(sender.get("is_bot") or sender.get("type") == "bot")

def ask_gigachat(prompt: str) -> str:
    # В GitHub Actions SSL обычно работает нормально, но если будут ошибки, верни verify_ssl=False
    with GigaChat(credentials=GIGACHAT_CREDENTIALS, model=GIGACHAT_MODEL) as client:
        response = client.chat(prompt)
        return response.choices[0].message.content.strip()

def send_message(target: dict, text: str):
    url = f"{API_BASE}/messages"
    params = {"access_token": BOT_TOKEN}
    params.update(target)
    payload = {"text": text[:3900]}
    
    try:
        requests.post(url, params=params, json=payload, timeout=30)
    except Exception as e:
        print(f"Ошибка отправки в MAX: {e}")

def process_updates():
    if not BOT_TOKEN or not GIGACHAT_CREDENTIALS:
        print("Ошибка: Не заданы токены в переменных окружения.")
        return

    state = load_state()
    marker = state.get("marker", 0)

    try:
        # Запрашиваем обновления
        response = requests.get(
            f"{API_BASE}/updates",
            params={"marker": marker, "limit": 100, "access_token": BOT_TOKEN},
            timeout=30
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
                if update_id: marker = max(marker, int(update_id) + 1)
                continue

            text = get_text(message)
            target = get_target(message)

            if text and target:
                print(f"Обработка запроса: {text[:50]}...")
                try:
                    answer = ask_gigachat(text)
                    send_message(target, answer)
                except Exception as e:
                    print(f"Ошибка при генерации/отправке: {e}")
                    send_message(target, "Произошла ошибка при генерации ответа.")

            if update_id:
                marker = max(marker, int(update_id) + 1)

    except Exception as e:
        print(f"Критическая ошибка при получении апдейтов: {e}")

    # Сохраняем новый маркер
    state["marker"] = marker
    save_state(state)
    print(f"Состояние сохранено. Новый marker: {marker}")

if __name__ == "__main__":
    process_updates()
