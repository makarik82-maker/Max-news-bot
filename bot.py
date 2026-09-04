import os
import json
import requests

# --- Конфигурация из переменных окружения GitHub ---
BOT_TOKEN = os.getenv("MAX_BOT_TOKEN")
API_BASE = os.getenv("MAX_API_BASE", "https://botapi.max.ru")

ZAI_API_KEY = os.getenv("ZAI_API_KEY")
ZAI_MODEL = os.getenv("ZAI_MODEL", "glm-5-flash")
ZAI_BASE_URL = os.getenv(
    "ZAI_BASE_URL",
    "https://api.z.ai/api/paas/v4"
)

STATE_FILE = "state.json"


# --- Работа с состоянием ---
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


# --- Разбор сообщений из MAX ---
def get_text(message: dict) -> str:
    body = message.get("body") or {}
    return (body.get("text") or message.get("text") or "").strip()


def get_target(message: dict) -> dict | None:
    recipient = message.get("recipient") or {}

    chat_id = recipient.get("chat_id") or message.get("chat_id")
    user_id = recipient.get("user_id") or message.get("user_id")

    if chat_id:
        return {"chat_id": chat_id}

    if user_id:
        return {"user_id": user_id}

    return None


def is_bot_message(message: dict) -> bool:
    sender = message.get("sender") or {}
    return bool(sender.get("is_bot") or sender.get("type") == "bot")


# --- Запрос к Z.AI ---
def ask_zai(prompt: str) -> str:
    url = f"{ZAI_BASE_URL.rstrip('/')}/chat/completions"

    headers = {
        "Authorization": f"Bearer {ZAI_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": ZAI_MODEL,
        "messages": [
            {
                "role": "system",
                "content": "Ты полезный ассистент в чате. Отвечай кратко и по делу."
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        "temperature": 0.7,
    }

    response = requests.post(
        url,
        headers=headers,
        json=payload,
        timeout=60,
    )

    if response.status_code != 200:
        print("Ошибка Z.AI API:")
        print(response.status_code)
        print(response.text[:1000])
        response.raise_for_status()

    data = response.json()

    return data["choices"][0]["message"]["content"].strip()


# --- Отправка сообщения в MAX ---
def send_message(target: dict, text: str):
    url = f"{API_BASE}/messages"

    params = {
        "access_token": BOT_TOKEN,
    }

    params.update(target)

    payload = {
        "text": text[:3900],
    }

    try:
        requests.post(
            url,
            params=params,
            json=payload,
            timeout=30,
        )
    except Exception as e:
        print(f"Ошибка отправки в MAX: {e}")


# --- Основной процесс ---
def process_updates():
    if not BOT_TOKEN:
        print("Ошибка: не задан MAX_BOT_TOKEN")
        return

    if not ZAI_API_KEY:
        print("Ошибка: не задан ZAI_API_KEY")
        return

    state = load_state()
    marker = state.get("marker", 0)

    try:
        response = requests.get(
            f"{API_BASE}/updates",
            params={
                "marker": marker,
                "limit": 100,
                "access_token": BOT_TOKEN,
            },
            timeout=30,
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
            target = get_target(message)

            if text and target:
                print(f"Обработка запроса: {text[:50]}...")

                try:
                    answer = ask_zai(text)
                    send_message(target, answer)
                except Exception as e:
                    print(f"Ошибка при генерации или отправке: {e}")
                    send_message(
                        target,
                        "Извините, не удалось получить ответ от модели."
                    )

            if update_id:
                marker = max(marker, int(update_id) + 1)

    except Exception as e:
        print(f"Критическая ошибка при получении апдейтов: {e}")

    state["marker"] = marker
    save_state(state)
    print(f"Состояние сохранено. Новый marker: {marker}")


if __name__ == "__main__":
    process_updates()
