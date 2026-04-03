import os
import re
import json
import logging
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("Полный ответ"), KeyboardButton("Кратко")],
        [KeyboardButton("Объясни просто"), KeyboardButton("Доп. вопросы")],
        [KeyboardButton("Проверить меня"), KeyboardButton("Сброс")],
    ],
    resize_keyboard=True,
    one_time_keyboard=False,
)

from prompts import SYSTEM_PROMPT, BILET_CONTEXT_TEMPLATE

load_dotenv()

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
VECTOR_STORE_ID = os.getenv("VECTOR_STORE_ID")

BASE_DIR = Path(__file__).resolve().parent
BILETY_DIR = BASE_DIR / "materials" / "bilety"

if not TG_BOT_TOKEN:
    raise ValueError("TG_BOT_TOKEN не найден в .env")

if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY не найден в .env")

if not VECTOR_STORE_ID:
    raise ValueError("VECTOR_STORE_ID не найден в .env")

client = OpenAI(api_key=OPENAI_API_KEY)

logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


INSTRUCTIONS = SYSTEM_PROMPT


def load_bilet_text(number: int) -> str | None:
    """Читает текст билета напрямую из файла bilet_NN.txt."""
    filename = BILETY_DIR / f"bilet_{number:02d}.txt"
    if filename.exists():
        return filename.read_text(encoding="utf-8")
    return None


def extract_bilet_number(user_text: str) -> int | None:
    """Извлекает номер билета из запроса пользователя (учитывает падежи)."""
    match = re.search(r'[Бб]илет[уае]?\s*[№#]?\s*(\d+)', user_text)
    if match:
        return int(match.group(1))
    return None


def is_quiz_request(user_text: str) -> bool:
    """Проверяет, просит ли пользователь режим проверки."""
    text_lower = user_text.lower()
    return "проверь" in text_lower and "билет" in text_lower


def call_openai(instructions: str, user_input: str, use_search: bool = True) -> str:
    """Общий вызов OpenAI API."""
    kwargs = dict(
        model="gpt-4.1-mini",
        instructions=instructions,
        input=user_input,
        temperature=0.2,
    )
    if use_search:
        kwargs["tools"] = [
            {
                "type": "file_search",
                "vector_store_ids": [VECTOR_STORE_ID],
                "max_num_results": 10,
            }
        ]
        kwargs["tool_choice"] = "auto"

    response = client.responses.create(**kwargs)

    answer_parts = []
    for item in response.output:
        if item.type == "message":
            for content in item.content:
                if content.type == "output_text":
                    answer_parts.append(content.text)

    if answer_parts:
        return "\n".join(answer_parts).strip()
    return ""


def generate_quiz_questions(bilet_num: int, bilet_text: str) -> list[str]:
    """Генерирует список вопросов для проверки по билету."""
    prompt = f"""На основе этого билета составь ровно 5 вопросов для устной проверки ученика.

Билет {bilet_num}:
{bilet_text}

Правила:
- Вопросы должны быть строго по содержанию этого билета.
- Вопросы должны проверять понимание, а не просто запоминание.
- Формулируй вопросы простым языком для 15-летнего подростка.
- Верни ТОЛЬКО JSON-массив из 5 строк, без пояснений.

Пример формата:
["Вопрос 1?", "Вопрос 2?", "Вопрос 3?", "Вопрос 4?", "Вопрос 5?"]"""

    result = call_openai(
        instructions="Ты генератор вопросов. Верни только JSON-массив строк.",
        user_input=prompt,
        use_search=False,
    )

    try:
        # Извлечь JSON из ответа
        match = re.search(r'\[.*\]', result, re.DOTALL)
        if match:
            questions = json.loads(match.group())
            if isinstance(questions, list) and len(questions) >= 3:
                return questions[:5]
    except (json.JSONDecodeError, ValueError):
        pass

    return [
        "Назови основную тему этого билета.",
        "Какие ключевые даты упоминаются в билете?",
        "Какие главные события или процессы описаны в билете?",
        "Почему эта тема важна для истории Беларуси?",
        "Что бы ты выделил как самое важное в этом билете?",
    ]


def evaluate_answer(bilet_text: str, question: str, student_answer: str) -> str:
    """Оценивает ответ ученика на вопрос по билету."""
    prompt = f"""Билет:
{bilet_text}

Вопрос: {question}

Ответ ученика: {student_answer}

Оцени ответ ученика. Будь доброжелательным репетитором для 15-летней девочки.
Формат:
1. Что верно в ответе (похвали конкретно).
2. Что не хватило или можно уточнить (если есть).
3. Как лучше сказать на экзамене (короткая подсказка).

Пиши коротко и по делу. Не упоминай файлы или источники."""

    return call_openai(
        instructions="Ты доброжелательный репетитор. Оцениваешь устный ответ ученика.",
        user_input=prompt,
        use_search=False,
    )


def generate_quiz_summary(bilet_num: int, qa_history: list[dict]) -> str:
    """Генерирует итог проверки."""
    history_text = ""
    for i, qa in enumerate(qa_history, 1):
        history_text += f"\nВопрос {i}: {qa['question']}\n"
        history_text += f"Ответ ученика: {qa['answer']}\n"

    prompt = f"""Билет {bilet_num}. Проверка завершена.

{history_text}

Дай краткий итог проверки:
1. Общая оценка (что ученик знает хорошо).
2. Над чем стоит поработать.
3. Совет для подготовки к экзамену.

Пиши доброжелательно, как репетитор для 15-летней девочки.
Не упоминай файлы или источники."""

    return call_openai(
        instructions="Ты доброжелательный репетитор. Подводишь итог проверки.",
        user_input=prompt,
        use_search=False,
    )


def ask_openai(user_text: str) -> str:
    """Обычный режим ответа (не тест)."""
    bilet_num = extract_bilet_number(user_text)
    bilet_text = load_bilet_text(bilet_num) if bilet_num else None

    input_parts = []
    if bilet_text:
        input_parts.append(BILET_CONTEXT_TEMPLATE.format(bilet_text=bilet_text))
    input_parts.append(user_text)

    result = call_openai(
        instructions=INSTRUCTIONS,
        user_input="\n".join(input_parts),
        use_search=True,
    )
    return result or "Не удалось получить ответ. Попробуй сформулировать вопрос чуть точнее."


BUTTON_MODES = {
    "полный ответ": "full",
    "кратко": "short",
    "объясни просто": "simple",
    "доп. вопросы": "extra",
    "проверить меня": "quiz",
}


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Привет! Я репетитор по истории Беларуси.\n\n"
        "Выбери действие кнопкой внизу, а потом напиши номер билета.\n\n"
        "Или просто напиши, например:\n"
        "Билет 1\n"
        "Билет 1 кратко\n"
        "Объясни просто билет 1\n"
        "Дополнительные вопросы к билету 1\n"
        "Проверь меня по билету 1"
    )
    await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Вот как со мной можно заниматься:\n\n"
        "1. Напиши номер билета:\n"
        "Билет 5\n\n"
        "2. Для краткого повторения:\n"
        "Билет 5 кратко\n\n"
        "3. Для простого объяснения:\n"
        "Объясни просто билет 5\n\n"
        "4. Для допвопросов:\n"
        "Дополнительные вопросы к билету 5\n\n"
        "5. Для тренировки:\n"
        "Проверь меня по билету 5\n\n"
        "6. Для мини-экзамена:\n"
        "Экзамен"
    )
    await update.message.reply_text(text)


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    await update.message.reply_text("Готово, начнем заново.", reply_markup=MAIN_KEYBOARD)


async def start_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE,
                     bilet_num: int, bilet_text: str) -> None:
    """Запускает режим проверки по билету."""
    await update.message.reply_text(
        f"Отлично! Проверяем билет {bilet_num}. Сейчас подготовлю вопросы..."
    )
    await update.message.chat.send_action("typing")

    questions = generate_quiz_questions(bilet_num, bilet_text)

    context.user_data["quiz"] = {
        "bilet_num": bilet_num,
        "bilet_text": bilet_text,
        "questions": questions,
        "current": 0,
        "history": [],
    }

    first_question = questions[0]
    await update.message.reply_text(
        f"Вопрос 1 из {len(questions)}:\n\n{first_question}"
    )


async def handle_quiz_answer(update: Update, context: ContextTypes.DEFAULT_TYPE,
                             user_text: str) -> None:
    """Обрабатывает ответ ученика в режиме проверки."""
    quiz = context.user_data["quiz"]
    current = quiz["current"]
    questions = quiz["questions"]
    bilet_text = quiz["bilet_text"]
    bilet_num = quiz["bilet_num"]

    question = questions[current]

    await update.message.chat.send_action("typing")

    evaluation = evaluate_answer(bilet_text, question, user_text)

    quiz["history"].append({
        "question": question,
        "answer": user_text,
    })

    quiz["current"] = current + 1

    if quiz["current"] < len(questions):
        next_num = quiz["current"] + 1
        next_question = questions[quiz["current"]]
        await update.message.reply_text(
            f"{evaluation}\n\n"
            f"---\n\n"
            f"Вопрос {next_num} из {len(questions)}:\n\n{next_question}"
        )
    else:
        await update.message.reply_text(evaluation)
        await update.message.chat.send_action("typing")

        summary = generate_quiz_summary(bilet_num, quiz["history"])
        await update.message.reply_text(
            f"---\n\n"
            f"Проверка по билету {bilet_num} завершена!\n\n"
            f"{summary}"
        )
        del context.user_data["quiz"]


async def execute_mode(update: Update, context: ContextTypes.DEFAULT_TYPE,
                       mode: str, bilet_num: int) -> None:
    """Выполняет выбранный режим для указанного билета."""
    bilet_text = load_bilet_text(bilet_num)
    if not bilet_text:
        await update.message.reply_text(
            f"Билет {bilet_num} не найден.", reply_markup=MAIN_KEYBOARD
        )
        return

    if mode == "quiz":
        await start_quiz(update, context, bilet_num, bilet_text)
        return

    mode_prompts = {
        "full": f"Билет {bilet_num}",
        "short": f"Билет {bilet_num} кратко",
        "simple": f"Объясни просто билет {bilet_num}",
        "extra": f"Дополнительные вопросы к билету {bilet_num}",
    }
    user_text = mode_prompts.get(mode, f"Билет {bilet_num}")

    await update.message.chat.send_action("typing")
    answer = ask_openai(user_text)
    await update.message.reply_text(answer, reply_markup=MAIN_KEYBOARD)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_text = update.message.text.strip()
    text_lower = user_text.lower()

    # Если пользователь в режиме проверки
    if "quiz" in context.user_data:
        if text_lower in ("стоп", "хватит", "выход", "отмена", "сброс"):
            del context.user_data["quiz"]
            await update.message.reply_text(
                "Проверка остановлена. Можешь продолжить заниматься!",
                reply_markup=MAIN_KEYBOARD,
            )
            return
        await handle_quiz_answer(update, context, user_text)
        return

    # Кнопка "Сброс"
    if text_lower == "сброс":
        context.user_data.clear()
        await update.message.reply_text(
            "Готово, начнем заново.", reply_markup=MAIN_KEYBOARD
        )
        return

    # Если ждём номер билета после нажатия кнопки
    if "pending_mode" in context.user_data:
        bilet_num = None
        # Попробовать извлечь номер из текста
        num_match = re.search(r'\d+', user_text)
        if num_match:
            bilet_num = int(num_match.group())

        if bilet_num:
            mode = context.user_data.pop("pending_mode")
            await execute_mode(update, context, mode, bilet_num)
        else:
            await update.message.reply_text(
                "Напиши номер билета (например, 1 или 15):",
                reply_markup=MAIN_KEYBOARD,
            )
        return

    # Нажатие кнопки режима
    if text_lower in BUTTON_MODES:
        mode = BUTTON_MODES[text_lower]
        context.user_data["pending_mode"] = mode
        await update.message.reply_text("Напиши номер билета:")
        return

    # Текстовый запрос: "Проверь меня по билету N"
    if is_quiz_request(user_text):
        bilet_num = extract_bilet_number(user_text)
        if bilet_num:
            bilet_text = load_bilet_text(bilet_num)
            if bilet_text:
                await start_quiz(update, context, bilet_num, bilet_text)
                return
            else:
                await update.message.reply_text(
                    f"Билет {bilet_num} не найден.", reply_markup=MAIN_KEYBOARD
                )
                return

    # Обычный режим
    await update.message.chat.send_action("typing")

    try:
        answer = ask_openai(user_text)
        await update.message.reply_text(answer, reply_markup=MAIN_KEYBOARD)
    except Exception as e:
        logger.exception("Ошибка при обработке сообщения")
        await update.message.reply_text(
            f"Произошла ошибка при обращении к ИИ:\n{e}",
            reply_markup=MAIN_KEYBOARD,
        )


def main():
    app = ApplicationBuilder().token(TG_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Бот запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()
