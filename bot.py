"""AI-ассистент для Telegram Business на DeepSeek V4 Flash (OpenModel).

Возможности:
- Отвечает клиентам в бизнес-чатах от имени владельца.
- Знания о владельце и кейсы берутся из файла cases.txt (персона «Дениса»).
  Файл можно править на лету — бот подхватит изменения без перезапуска.
- Самообучение: бот записывает новые полезные факты в memory.jsonl и
  использует их в следующих ответах.

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
import json
import logging
import os
import time
from collections import defaultdict, deque
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from dotenv import load_dotenv
from anthropic import AsyncAnthropic

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(),  # консоль (при ручном запуске)
        logging.FileHandler(BASE_DIR / "bot.log", encoding="utf-8"),  # фон/служба
    ],
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
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "10"))
MAX_TOKENS    = int(os.getenv("MAX_TOKENS", "1024"))

# Базовый стиль персоны. Подробные знания живут в cases.txt.
PERSONA_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "Ты — Денис, ассистент поддержки фронтлайна IQOS Uzbekistan по CRM Monolith "
    "и Telegram-боту. Отвечай коротко, на «ты», без воды, по делу — как живой "
    "коллега в рабочем чате. Опирайся на кейсы и правила ниже. Если в базе нет "
    "точного ответа — честно скажи и попроси скрин/шаги/время ошибки. "
    "Никогда не обещай сроков и не выдавай внутренние детали.",
)

# Кто владелец — его Telegram user id. Только он управляет обучением.
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
# Автообучение: бот сам извлекает новые факты из диалогов и сохраняет их.
AUTO_LEARN = os.getenv("AUTO_LEARN", "1") == "1"

KNOWLEDGE_FILE = BASE_DIR / os.getenv("KNOWLEDGE_FILE", "cases.txt")
MEMORY_FILE    = BASE_DIR / os.getenv("MEMORY_FILE", "memory.jsonl")

ai  = AsyncAnthropic(api_key=AI_API_KEY, base_url=AI_BASE_URL)
bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()

# Память диалогов: chat_id -> последние реплики (роль/текст).
_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=HISTORY_LIMIT * 2))


# --------------------------------------------------------------------------- #
# Знания: cases.txt (правится владельцем) + memory.jsonl (самообучение)
# --------------------------------------------------------------------------- #
_knowledge_cache = {"mtime": 0.0, "text": ""}


def load_knowledge() -> str:
    """Содержимое cases.txt с горячей перезагрузкой по времени изменения файла."""
    try:
        mtime = KNOWLEDGE_FILE.stat().st_mtime
    except FileNotFoundError:
        return ""
    if mtime != _knowledge_cache["mtime"]:
        _knowledge_cache["text"] = KNOWLEDGE_FILE.read_text(encoding="utf-8")
        _knowledge_cache["mtime"] = mtime
        log.info("Знания перезагружены из %s", KNOWLEDGE_FILE.name)
    return _knowledge_cache["text"]


def load_memory() -> list[dict]:
    """Выученные заметки из memory.jsonl (по одной JSON-записи на строку)."""
    if not MEMORY_FILE.exists():
        return []
    notes = []
    for line in MEMORY_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            notes.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return notes


def append_memory(note: str, source: str = "auto") -> None:
    """Добавляет новую заметку в память самообучения."""
    record = {"ts": int(time.time()), "source": source, "note": note.strip()}
    with MEMORY_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    log.info("Запомнил (%s): %s", source, note.strip()[:120])


def build_system_prompt() -> str:
    """Собирает полный системный промпт: персона + знания + выученное."""
    parts = [PERSONA_PROMPT]

    knowledge = load_knowledge()
    if knowledge:
        parts.append("=== БАЗА ЗНАНИЙ И КЕЙСОВ ===\n" + knowledge)

    notes = load_memory()
    if notes:
        learned = "\n".join(f"- {n['note']}" for n in notes[-50:])
        parts.append(
            "=== ВЫУЧЕННЫЕ ФАКТЫ (свежие дополнения, имеют приоритет) ===\n"
            + learned
        )
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
# Обращение к модели
# --------------------------------------------------------------------------- #
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
    messages = list(history) + [{"role": "user", "content": user_text}]

    msg = await ai.messages.create(
        model=AI_MODEL,
        max_tokens=MAX_TOKENS,
        system=build_system_prompt(),
        messages=messages,
    )
    answer = _extract_text(msg)

    history.append({"role": "user",      "content": user_text})
    history.append({"role": "assistant", "content": answer})
    return answer


async def maybe_learn(user_text: str, answer: str) -> None:
    """Самообучение: просит модель решить, есть ли в диалоге новый общий факт,
    который стоит запомнить, и если да — сохраняет его в память.

    Извлекаются ТОЛЬКО переиспользуемые знания (новые правила/кейсы/решения),
    без персональных данных конкретных клиентов.
    """
    if not AUTO_LEARN:
        return
    try:
        probe = await ai.messages.create(
            model=AI_MODEL,
            max_tokens=200,
            system=(
                "Ты — модуль памяти ассистента поддержки. Проанализируй обмен "
                "репликами. Реши, содержит ли он НОВОЕ переиспользуемое знание "
                "(правило, решение кейса, факт о процессе), которого ещё нет в "
                "базе и которое поможет в будущих похожих вопросах. "
                "НЕ запоминай персональные данные, номера, разовые жалобы и "
                "общие приветствия. Ответь СТРОГО одной строкой JSON: "
                '{"remember": true/false, "note": "краткий факт одной фразой"}.'
            ),
            messages=[{
                "role": "user",
                "content": f"Вопрос: {user_text}\nОтвет ассистента: {answer}",
            }],
        )
        raw = _extract_text(probe).strip()
        # Вырезаем JSON из возможной обёртки в markdown.
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1:
            return
        data = json.loads(raw[start:end + 1])
        if data.get("remember") and data.get("note"):
            append_memory(data["note"], source="auto")
    except Exception:
        log.exception("Сбой автообучения (не критично)")


# --------------------------------------------------------------------------- #
# Хендлеры
# --------------------------------------------------------------------------- #
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

    # Самообучение в фоне, чтобы не задерживать ответ клиенту.
    asyncio.create_task(maybe_learn(message.text, answer))


def _is_owner(message: Message) -> bool:
    return OWNER_ID != 0 and message.from_user and message.from_user.id == OWNER_ID


@dp.message(F.text.startswith("/learn"))
async def cmd_learn(message: Message) -> None:
    """Владелец вручную добавляет факт в память: /learn <текст>."""
    if not _is_owner(message):
        return
    fact = message.text[len("/learn"):].strip()
    if not fact:
        await message.answer("Напиши так: /learn <что запомнить>")
        return
    append_memory(fact, source="owner")
    await message.answer("Запомнил ✅")


@dp.message(F.text == "/memory")
async def cmd_memory(message: Message) -> None:
    """Показать выученные факты."""
    if not _is_owner(message):
        return
    notes = load_memory()
    if not notes:
        await message.answer("Память пока пустая.")
        return
    lines = [f"{i+1}. [{n['source']}] {n['note']}" for i, n in enumerate(notes[-30:])]
    await message.answer("Выученные факты:\n" + "\n".join(lines))


@dp.message(F.text == "/forget")
async def cmd_forget(message: Message) -> None:
    """Очистить всю выученную память."""
    if not _is_owner(message):
        return
    if MEMORY_FILE.exists():
        MEMORY_FILE.unlink()
    await message.answer("Память очищена 🧹")


@dp.message(F.text == "/reload")
async def cmd_reload(message: Message) -> None:
    """Принудительно перечитать cases.txt."""
    if not _is_owner(message):
        return
    _knowledge_cache["mtime"] = 0.0
    load_knowledge()
    await message.answer("База знаний перечитана ✅")


@dp.message(F.text == "/start")
async def handle_start(message: Message) -> None:
    """Проверка, что бот жив."""
    hint = ""
    if _is_owner(message):
        hint = ("\n\nКоманды для тебя:\n"
                "/learn <текст> — запомнить факт\n"
                "/memory — показать выученное\n"
                "/forget — очистить память\n"
                "/reload — перечитать cases.txt")
    await message.answer(
        "Я бизнес-ассистент. Подключи меня в Настройки → Telegram для бизнеса → "
        "Чат-боты, и я буду отвечать твоим клиентам автоматически." + hint
    )


async def main() -> None:
    log.info(
        "Запуск. Модель=%s, база=%s, автообучение=%s, owner=%s",
        AI_MODEL, AI_BASE_URL, AUTO_LEARN, OWNER_ID or "не задан",
    )
    load_knowledge()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Остановлено.")
