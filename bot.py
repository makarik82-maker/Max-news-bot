import os
import json
import requests

# ==========================================
# Конфигурация из переменных окружения GitHub
# ==========================================
BOT_TOKEN = os.getenv("MAX_BOT_TOKEN")
API_BASE = os.getenv("MAX_API_BASE", "https://botapi.max.ru")

ZAI_API_KEY = os.getenv("ZAI_API_KEY")
ZAI_MODEL = os.getenv("ZAI_MODEL", "glm-4-flash") # Можно заменить на glm-4-flash или другую доступную модель
ZAI_BASE_URL = os.getenv(
    "ZAI_BASE_URL",
    "https://api.z.ai/api/paas/v4"
)

STATE_FILE = "state.json"
MAX_TEXT_LEN = 3900 # Максимальная длина сообщения в MAX


# ==========================================
# Работа с состоянием (маркером сообщений)
# ==========================================
def load_state():
    """Загружает последний обработанный ID сообщения из файла."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"marker": 0}


def save_state(state):
    """Сохраняет обновленный ID сообщения в файл."""
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ==========================================
# Разбор сообщений из MAX
# ==========================================
def get_text(message: dict) -> str:
    """Извлекает текст сообщения."""
    body = message.get("body") or {}
    return (body.get("text") or message.get("text") or "").strip()


def get_target(message: dict) -> dict | None:
    """Определяет, куда отправлять ответ (chat_id или user_id)."""
    recipient = message.get("recipient") or {}

    chat_id = recipient.get("chat_id") or message.get("chat_id")
    user_id = recipient.get("user_id") or message.get("user_id")

    if chat_id:
        return {"chat_id": chat_id}
    if user_id:
        return {"user_id": user_id}

    return None


def is_bot_message(message: dict) -> bool:
    """Проверяет, отправлено ли сообщение самим ботом (чтобы избежать циклов)."""
    sender = message.get("sender") or {}
    return bool(sender.get("is_bot") or sender.get("type") == "bot")


# ==========================================
# Интеграция с Z.AI (OpenAI-совместимый API)
# ==========================================
def ask_zai(prompt: str) -> str:
    """Отправляет запрос в Z.AI и возвращает текстовый ответ."""
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
                "content": "Ты полезный и вежливый ассистент в чате. Отвечай кратко, по делу и на том языке, на котором к тебе обратились."
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
        print(f"Status Code: {response.status_code}")
        print(f"Response: {response.text[:1000]}")
        response.raise_for_status()

    data = response.json()
    return data["choices"][0]["message"]["content"].strip()


# ==========================================
# Отправка сообщений в MAX
# ==========================================
def send_message(target: dict, text: str):
    """Отправляет сгенерированный ответ обратно в чат MAX."""
    url = f"{API_BASE}/messages"

    params = {
        "access_token": BOT_TOKEN,
    }
    params.update(target)

    payload = {
        "text": text[:MAX_TEXT_LEN],
    }

    try:
        response = requests.post(
            url,
            params=params,
            json=payload,
            timeout=30,
        )
        if response.status_code != 200:
            print(f"Ошибка отправки в MAX ({response.status_code}): {response.text[:500]}")
    except Exception as e:
        print(f"Ошибка отправки в MAX: {e}")


# ==========================================
# Основной процесс (запуск GitHub Actions)
# ==========================================
def process_updates():
    """Главная функция: получает апдейты, обрабатывает их и сохраняет состояние."""
    if not BOT_TOKEN:
        print("Критическая ошибка: не задан MAX_BOT_TOKEN в переменных окружения.")
        return

    if not ZAI_API_KEY:
        print("Критическая ошибка: не задан ZAI_API_KEY в переменных окружения.")
        return

    state = load_state()
    marker = state.get("marker", 0)

    print(f"Запуск обработки. Текущий marker: {marker}")

    try:
        # 1. Запрашиваем новые сообщения из MAX
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
            save_state(state)
            return

        print(f"Получено обновлений: {len(updates)}")

        # 2. Обрабатываем каждое сообщение
        for update in updates:
            update_id = update.get("update_id") or update.get("id")
            message = update.get("message") or update.get("data") or {}

            # Пропускаем пустые события или сообщения от самого бота
            if not message or is_bot_message(message):
                if update_id:
                    marker = max(marker, int(update_id) + 1)
                continue

            text = get_text(message)
            target = get_target(message)

            if text and target:
                print(f"Обработка запроса: {text[:50]}...")

                try:
                    # Запрос к ИИ
                    answer = ask_zai(text)
                    # Отправка ответа в чат
                    send_message(target, answer)
                except Exception as e:
                    print(f"Ошибка при генерации или отправке: {e}")
                    send_message(
                        target,
                        "⚠️ Извините, произошла ошибка при генерации ответа."
                    )

            # Сдвигаем маркер
            if update_id:
                marker = max(marker, int(update_id) + 1)

    except Exception as e:
        print(f"Критическая ошибка при получении апдейтов: {e}")

    # 3. Сохраняем новый маркер в файл, чтобы не обработать эти сообщения повторно
    state["marker"] = marker
    save_state(state)
    print(f"Обработка завершена. Новый marker сохранен: {marker}")


if __name__ == "__main__":
    process_updates()
