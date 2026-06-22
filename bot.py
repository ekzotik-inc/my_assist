"""AI-ассистент для Telegram Business.

Ловит сообщения клиентов в бизнес-чатах (Telegram для бизнеса), отправляет их
в ИИ-модель и отвечает клиенту от имени владельца аккаунта.

Запуск:
    pip install -r requirements.txt
    cp .env.example .env   # и впиши свои ключи
    python bot.py

Требования со стороны Telegram:
  - На аккаунте владельца включён Telegram Premium.
  - В приложении: Настройки → Telegram для бизнеса → Чат-боты → указать этого бота.
"""

import asyncio
import logging
import os
from collections import defaultdict, deque

# В корпоративных сетях трафик часто идёт через TLS-прокси с собственным
# корневым сертификатом. truststore заставляет Python доверять системному
# хранилищу сертификатов Windows, куда этот CA уже добавлен IT-отделом.
try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("ai-assistant")


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value or value.startswith("put-your"):
        raise RuntimeError(
            f"Не задана переменная окружения {name}. "
            f"Скопируй .env.example в .env и впиши значения."
        )
    return value


BOT_TOKEN = _require("TELEGRAM_BOT_TOKEN")
AI_API_KEY = _require("AI_API_KEY")
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.openmodel.app/v1")
AI_MODEL = os.getenv("AI_MODEL", "deepseek-v4-flash")
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "Ты — вежливый ассистент, отвечающий клиентам от имени владельца бизнеса. "
    "Отвечай кратко, по делу и дружелюбно на языке клиента.",
)
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "10"))

ai = AsyncOpenAI(api_key=AI_API_KEY, base_url=AI_BASE_URL)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Память диалогов: chat_id -> последние реплики (роль/текст).
_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=HISTORY_LIMIT * 2))


async def ask_ai(chat_id: int, user_text: str) -> str:
    """Отправляет реплику пользователя в модель с учётом истории диалога."""
    history = _history[chat_id]
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    completion = await ai.chat.completions.create(
        model=AI_MODEL,
        messages=messages,
    )
    answer = (completion.choices[0].message.content or "").strip()

    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": answer})
    return answer


@dp.business_message(F.text)
async def handle_business_message(message: Message) -> None:
    """Сообщение от клиента в бизнес-чате."""
    connection_id = message.business_connection_id
    log.info("Бизнес-сообщение от %s: %s", message.chat.id, message.text)

    try:
        answer = await ask_ai(message.chat.id, message.text)
    except Exception:  # noqa: BLE001 — логируем любую ошибку модели, не падая
        log.exception("Ошибка обращения к ИИ")
        return

    # Отвечаем клиенту ОТ ИМЕНИ владельца через business_connection_id.
    await bot.send_message(
        chat_id=message.chat.id,
        text=answer,
        business_connection_id=connection_id,
    )


@dp.message(F.text == "/start")
async def handle_start(message: Message) -> None:
    """Личное сообщение самому боту (не бизнес-чат) — для проверки, что он жив."""
    await message.answer(
        "Я бизнес-ассистент. Подключи меня в Настройки → Telegram для бизнеса → "
        "Чат-боты, и я буду отвечать твоим клиентам автоматически."
    )


async def main() -> None:
    log.info("Запуск. Модель=%s, база=%s", AI_MODEL, AI_BASE_URL)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Остановлено.")
