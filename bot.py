"""AI-ассистент для Telegram Business на DeepSeek V4 Flash (OpenModel).

Запуск:
    pip install -r requirements.txt
    # заполнить .env (см. .env.example)
    python bot.py

Требования со стороны Telegram:
- На аккаунте владельца включён Telegram Premium.
- В приложении: Настройки → Telegram для бизнеса → Чат-боты →
  указать этого бота и дать ему права «Отвечать на сообщения».
"""
import asyncio
import logging
import os
from collections import defaultdict, deque

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from dotenv import load_dotenv
from anthropic import AsyncAnthropic

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


BOT_TOKEN   = _require("TELEGRAM_BOT_TOKEN")
AI_API_KEY  = _require("AI_API_KEY")
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.openmodel.ai")  # api, не console!
AI_MODEL    = os.getenv("AI_MODEL", "deepseek-v4-flash")
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "Ты — деловой ассистент в Telegram. Отвечай по-русски, "
    "формально, кратко и по делу. Если не уверен — честно скажи об этом.",
)
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "10"))
MAX_TOKENS    = int(os.getenv("MAX_TOKENS", "1024"))

ai  = AsyncAnthropic(api_key=AI_API_KEY, base_url=AI_BASE_URL)
bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()

# Память диалогов: chat_id -> последние реплики (роль/текст).
_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=HISTORY_LIMIT * 2))


def _extract_text(msg) -> str:
    """Финальный текст из ответа Anthropic, без блоков 'thinking'."""
    parts = []
    for block in msg.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    text = "\n".join(p.strip() for p in parts if p and p.strip())
    return text or "(пустой ответ модели)"


async def ask_ai(chat_id: int, user_text: str) -> str:
    """Отправляет реплику пользователя в модель с учётом истории диалога."""
    history = _history[chat_id]
    # Anthropic: только user/assistant внутри messages, system — отдельный параметр
    messages = list(history) + [{"role": "user", "content": user_text}]

    msg = await ai.messages.create(
        model=AI_MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=messages,
    )
    answer = _extract_text(msg)

    # Сохраняем в историю и user, и assistant
    history.append({"role": "user",      "content": user_text})
    history.append({"role": "assistant", "content": answer})
    return answer


@dp.business_message(F.text)
async def handle_business_message(message: Message) -> None:
    """Сообщение от клиента в бизнес-чате."""
    connection_id = message.business_connection_id
    log.info("Бизнес-сообщение от %s: %s", message.chat.id, message.text)

    try:
        answer = await ask_ai(message.chat.id, message.text)
    except Exception:
        log.exception("Ошибка обращения к ИИ")
        answer = "Секунду, передам коллеге и вернусь с ответом."

    # КЛЮЧЕВОЕ: business_connection_id — чтобы ответ ушёл от ИМЕНИ владельца
    await bot.send_message(
        chat_id=message.chat.id,
        text=answer,
        business_connection_id=connection_id,
    )


@dp.message(F.text == "/start")
async def handle_start(message: Message) -> None:
    """Проверка, что бот жив."""
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
