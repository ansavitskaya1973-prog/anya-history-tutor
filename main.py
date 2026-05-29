import os
import re
import json
import logging
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

load_dotenv()

from prompts import SYSTEM_PROMPT, BILET_CONTEXT_TEMPLATE, PRAKTIKA_INSTRUCTIONS
from access import (
    init_db,
    is_admin,
    ensure_user,
    get_user,
    list_all_users,
    get_stats,
    try_increment_trial,
    approve_user,
    revoke_user,
    block_user,
    validate_code,
    ADMIN_USER_IDS,
    TRIAL_LIMIT,
    STATUS_TRIAL,
    STATUS_APPROVED,
    STATUS_BLOCKED,
)

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

PRAKTIKA_FILE_IDS_PATH = BASE_DIR / "praktika_file_ids.json"
if PRAKTIKA_FILE_IDS_PATH.exists():
    PRAKTIKA_FILE_IDS: dict[int, str] = {
        int(k): v
        for k, v in json.loads(PRAKTIKA_FILE_IDS_PATH.read_text(encoding="utf-8")).items()
    }
    logger.info(f"Загружено практических заданий: {len(PRAKTIKA_FILE_IDS)}")
else:
    logger.warning("praktika_file_ids.json не найден — режим 'практическое задание' будет недоступен")
    PRAKTIKA_FILE_IDS = {}

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("Полный ответ"), KeyboardButton("Кратко")],
        [KeyboardButton("Объясни просто"), KeyboardButton("Дополнительные вопросы")],
        [KeyboardButton("Практическое задание")],
        [KeyboardButton("Проверить меня"), KeyboardButton("Сменить билет")],
    ],
    resize_keyboard=True,
    one_time_keyboard=False,
)

BUTTON_MODES = {
    "полный ответ": "full",
    "кратко": "short",
    "объясни просто": "simple",
    "дополнительные вопросы": "extra",
    "практическое задание": "praktika",
    "проверить меня": "quiz",
}


def praktika_unavailable_message(bilet_num: int) -> str:
    return (
        f"Практическое задание для билета {bilet_num} пока недоступно — "
        f"Министерство образования не публикует его централизованно."
    )


def request_access_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📩 Запросить доступ", callback_data="request_access")]]
    )


def admin_decision_keyboard(target_user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Одобрить", callback_data=f"approve:{target_user_id}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{target_user_id}"),
            ]
        ]
    )


# ---------------------------
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ---------------------------

def load_bilet_text(number: int) -> str | None:
    filename = BILETY_DIR / f"bilet_{number:02d}.txt"
    if filename.exists():
        return filename.read_text(encoding="utf-8")
    return None


def extract_bilet_number(user_text: str) -> int | None:
    match = re.search(r"[Бб]илет[уае]?\s*[№#]?\s*(\d+)", user_text)
    if match:
        return int(match.group(1))

    if re.fullmatch(r"\d+", user_text.strip()):
        return int(user_text.strip())

    return None


def is_quiz_request(user_text: str) -> bool:
    text_lower = user_text.lower()
    return "проверь" in text_lower and "билет" in text_lower


def split_long_message(text: str, max_len: int = 3500) -> list[str]:
    if len(text) <= max_len:
        return [text]

    parts = []
    current = ""
    blocks = text.split("\n\n")

    for block in blocks:
        candidate = block if not current else current + "\n\n" + block
        if len(candidate) <= max_len:
            current = candidate
        else:
            if current:
                parts.append(current)
                current = ""

            if len(block) > max_len:
                lines = block.splitlines()
                temp = ""
                for line in lines:
                    candidate_line = line if not temp else temp + "\n" + line
                    if len(candidate_line) <= max_len:
                        temp = candidate_line
                    else:
                        if temp:
                            parts.append(temp)
                        if len(line) > max_len:
                            for i in range(0, len(line), max_len):
                                parts.append(line[i:i + max_len])
                            temp = ""
                        else:
                            temp = line
                if temp:
                    current = temp
                else:
                    current = ""
            else:
                current = block

    if current:
        parts.append(current)

    return parts


async def send_long_message(update: Update, text: str, reply_markup=None) -> None:
    chunks = split_long_message(text)

    for i, chunk in enumerate(chunks):
        if i == len(chunks) - 1:
            await update.message.reply_text(chunk, reply_markup=reply_markup)
        else:
            await update.message.reply_text(chunk)


CITATION_RE = re.compile(r"【[^】]*†[^】]*】")


def _strip_citations(text: str) -> str:
    return CITATION_RE.sub("", text).strip()


def call_openai(
    instructions: str,
    user_input: str,
    use_search: bool = True,
    attached_file_id: str | None = None,
) -> str:
    if attached_file_id:
        input_payload = [
            {
                "role": "user",
                "content": [
                    {"type": "input_file", "file_id": attached_file_id},
                    {"type": "input_text", "text": user_input},
                ],
            }
        ]
    else:
        input_payload = user_input

    kwargs = dict(
        model="gpt-4.1-mini",
        instructions=instructions,
        input=input_payload,
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
        return _strip_citations("\n".join(answer_parts).strip())
    return ""


def generate_quiz_questions(bilet_num: int, bilet_text: str) -> list[str]:
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
        match = re.search(r"\[.*\]", result, re.DOTALL)
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
    prompt = f"""Билет:
{bilet_text}

Вопрос: {question}

Ответ ученика: {student_answer}

Оцени ответ ученика. Будь доброжелательным репетитором для 15-летней девочки.

Формат:
1. Что верно в ответе.
2. Что можно уточнить или добавить.
3. Как лучше сказать на экзамене.

Пиши коротко, по делу и доброжелательно.
Не упоминай файлы, источники или технические детали."""

    return call_openai(
        instructions="Ты доброжелательный репетитор. Оцениваешь устный ответ ученика.",
        user_input=prompt,
        use_search=False,
    )


def generate_quiz_summary(bilet_num: int, qa_history: list[dict]) -> str:
    history_text = ""
    for i, qa in enumerate(qa_history, 1):
        history_text += f"\nВопрос {i}: {qa['question']}\n"
        history_text += f"Ответ ученика: {qa['answer']}\n"

    prompt = f"""Билет {bilet_num}. Проверка завершена.

{history_text}

Дай краткий итог проверки:
1. Что уже хорошо получается.
2. Над чем стоит поработать.
3. Совет для подготовки к экзамену.

Пиши доброжелательно и понятно для 15-летней ученицы.
Не упоминай файлы, источники или технические детали."""

    return call_openai(
        instructions="Ты доброжелательный репетитор. Подводишь итог проверки.",
        user_input=prompt,
        use_search=False,
    )


def format_simple_answer(text: str) -> str:
    text = text.strip()

    text = text.replace("Простое объяснение", "💡 Простое объяснение")
    text = text.replace("Как сказать на экзамене", "\n\n🗣 Как сказать на экзамене")
    text = text.replace("Что точно надо запомнить", "\n\n⭐ Что точно надо запомнить")

    return text


def ask_openai(user_text: str) -> str:
    bilet_num = extract_bilet_number(user_text)
    bilet_text = load_bilet_text(bilet_num) if bilet_num else None

    input_parts = []
    if bilet_text:
        input_parts.append(BILET_CONTEXT_TEMPLATE.format(bilet_text=bilet_text))
    input_parts.append(user_text)

    result = call_openai(
        instructions=SYSTEM_PROMPT,
        user_input="\n".join(input_parts),
        use_search=True,
    )
    return result or "Не удалось получить ответ. Попробуй сформулировать вопрос чуть точнее."


def ask_openai_mode(user_text: str, mode: str, bilet_num: int | None = None) -> str:
    bilet_text = load_bilet_text(bilet_num) if bilet_num else None

    if mode == "praktika":
        file_id = PRAKTIKA_FILE_IDS.get(bilet_num) if bilet_num else None
        if not file_id:
            return f"Практическое задание для билета {bilet_num} пока не подготовлено."

        # NOTE: bilet_NN.txt сюда НЕ подмешиваем. Внутри одного билета теоретический
        # и практический вопросы часто на разные темы — подмешивание текста билета
        # вместе с BILET_CONTEXT_TEMPLATE ("не подменяй тему") заставляло модель
        # отказываться разбирать PDF, видя в нём "подмену темы".
        result = call_openai(
            instructions=SYSTEM_PROMPT + "\n\n" + PRAKTIKA_INSTRUCTIONS,
            user_input=user_text,
            use_search=True,
            attached_file_id=file_id,
        )
        return result or "Не удалось получить ответ. Попробуй ещё раз."

    input_parts = []
    if bilet_text:
        input_parts.append(BILET_CONTEXT_TEMPLATE.format(bilet_text=bilet_text))

    mode_instructions = {
        "full": (
            "Дай полный связный ответ по билету. "
            "В конце добавь только блок 'Что важно запомнить'. "
            "Не добавляй дополнительные вопросы."
        ),
        "short": (
            "Дай краткий ответ в виде тезисов. "
            "Добавь блоки: 'Ключевые даты и имена' и 'Мини-шпаргалка'."
        ),
        "simple": (
            "Объясни тему очень простым языком. "
            "Пиши короткими абзацами по 2–3 предложения. "
            "Не делай сплошной текст. "
            "Используй структуру: "
            "'Простое объяснение', "
            "'Как сказать на экзамене', "
            "'Что точно надо запомнить'."
        ),
        "extra": (
            "Составь ТОЛЬКО дополнительные вопросы по этому билету. "
            "Не пересказывай полный билет. "
            "Не давай длинное объяснение темы. "
            "Не пиши вступление и не повторяй тему длинным текстом. "
            "Верни только список из 5–8 вопросов. "
            "После каждого вопроса обязательно напиши: "
            "'Краткий ответ: ...'"
        ),
    }

    input_parts.append(user_text)

    result = call_openai(
        instructions=SYSTEM_PROMPT + "\n\n" + mode_instructions.get(mode, ""),
        user_input="\n".join(input_parts),
        use_search=True,
    )

    return result or "Не удалось получить ответ. Попробуй ещё раз."


# ---------------------------
# TELEGRAM HANDLERS
# ---------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
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
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Вот как со мной можно заниматься:\n\n"
        "1. Напиши номер билета:\n"
        "Билет 5\nили просто: 5\n\n"
        "2. Для краткого повторения:\n"
        "Билет 5 кратко\n\n"
        "3. Для простого объяснения:\n"
        "Объясни просто билет 5\n\n"
        "4. Для дополнительных вопросов:\n"
        "Дополнительные вопросы к билету 5\n\n"
        "5. Для тренировки:\n"
        "Проверь меня по билету 5"
    )
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    await send_long_message(update, "Готово. Начнем заново.", reply_markup=MAIN_KEYBOARD)


async def start_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE, bilet_num: int, bilet_text: str) -> None:
    context.user_data["last_bilet_num"] = bilet_num

    await send_long_message(
        update,
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
    await send_long_message(
        update,
        f"Вопрос 1 из {len(questions)}:\n\n{first_question}"
    )


async def handle_quiz_answer(update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str) -> None:
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
        await send_long_message(
            update,
            f"{evaluation}\n\n---\n\nВопрос {next_num} из {len(questions)}:\n\n{next_question}"
        )
    else:
        await send_long_message(update, evaluation)
        await update.message.chat.send_action("typing")

        summary = generate_quiz_summary(bilet_num, quiz["history"])
        await send_long_message(
            update,
            f"---\n\nПроверка по билету {bilet_num} завершена!\n\n{summary}",
            reply_markup=MAIN_KEYBOARD
        )
        del context.user_data["quiz"]


async def execute_mode(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str, bilet_num: int) -> None:
    bilet_text = load_bilet_text(bilet_num)
    if not bilet_text:
        await send_long_message(
            update,
            f"Билет {bilet_num} не найден.",
            reply_markup=MAIN_KEYBOARD
        )
        return

    context.user_data["last_bilet_num"] = bilet_num

    if mode == "quiz":
        await start_quiz(update, context, bilet_num, bilet_text)
        return

    mode_prompts = {
        "full": f"Билет {bilet_num}",
        "short": f"Билет {bilet_num} кратко",
        "simple": f"Объясни просто билет {bilet_num}",
        "extra": f"Дополнительные вопросы к билету {bilet_num}",
        "praktika": f"Разбери практическое задание (второй вопрос) к билету {bilet_num}.",
    }
    user_text = mode_prompts.get(mode, f"Билет {bilet_num}")

    await update.message.chat.send_action("typing")
    answer = ask_openai_mode(user_text, mode=mode, bilet_num=bilet_num)

    if mode == "simple":
        answer = format_simple_answer(answer)

    await send_long_message(update, answer, reply_markup=MAIN_KEYBOARD)


async def _maybe_warn_remaining(update: Update, user_id: int) -> None:
    """После платного действия — предупреждаем, если осталось мало бесплатных вопросов."""
    if is_admin(user_id):
        return
    fresh = get_user(user_id)
    if not fresh or fresh["status"] != STATUS_TRIAL:
        return
    remaining = TRIAL_LIMIT - fresh["trial_used"]
    if remaining == 3:
        await update.message.reply_text("📊 Осталось 3 бесплатных вопроса.")
    elif remaining == 1:
        await update.message.reply_text("📊 Остался последний бесплатный вопрос.")
    elif remaining <= 0:
        await update.message.reply_text(
            "📊 Это был последний бесплатный вопрос. Чтобы продолжить — введи код доступа "
            "или нажми кнопку ниже.",
            reply_markup=request_access_keyboard(),
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    user_record = ensure_user(tg_user.id, tg_user.username, tg_user.first_name)
    user_text = update.message.text.strip()
    text_lower = user_text.lower()

    # Заблокированных — отправляем сразу (кроме админа)
    if user_record["status"] == STATUS_BLOCKED and not is_admin(tg_user.id):
        await update.message.reply_text("Доступ к боту закрыт.")
        return

    # Если ждём от пользователя код доступа — обрабатываем сообщение как код
    if context.user_data.get("awaiting_code"):
        if validate_code(user_text):
            approve_user(tg_user.id)
            context.user_data.pop("awaiting_code", None)
            await send_long_message(
                update,
                "✅ Готово! Доступ открыт. Теперь можно заниматься без ограничений.",
                reply_markup=MAIN_KEYBOARD,
            )
        else:
            await update.message.reply_text(
                "Код неверный. Попробуй ещё раз или нажми кнопку ниже, "
                "чтобы запросить доступ у Анастасии.",
                reply_markup=request_access_keyboard(),
            )
        return

    # Идущий quiz — продолжается без списания (списано на старте)
    if "quiz" in context.user_data:
        if text_lower in ("стоп", "хватит", "выход", "отмена", "сброс"):
            del context.user_data["quiz"]
            await send_long_message(
                update,
                "Проверка остановлена. Можно продолжать заниматься дальше!",
                reply_markup=MAIN_KEYBOARD,
            )
            return
        await handle_quiz_answer(update, context, user_text)
        return

    # Бесплатные служебные действия — не списываются
    if text_lower == "сброс":
        context.user_data.clear()
        await send_long_message(
            update,
            "Готово. Начнем заново.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    if text_lower == "сменить билет":
        context.user_data["pending_mode"] = "full"
        context.user_data["last_bilet_num"] = None
        await send_long_message(
            update,
            "Напиши номер билета:",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    # === Дальше — потенциально платные действия. ===
    async def _try_charge() -> bool:
        """Списать 1 запрос. При исчерпании — переводим в режим ожидания кода."""
        if is_admin(tg_user.id):
            return True
        fresh = get_user(tg_user.id) or user_record
        if fresh["status"] == STATUS_BLOCKED:
            await update.message.reply_text("Доступ к боту закрыт.")
            return False
        if fresh["status"] == STATUS_APPROVED:
            return True
        success, _ = try_increment_trial(tg_user.id, TRIAL_LIMIT)
        if not success:
            context.user_data["awaiting_code"] = True
            await send_long_message(
                update,
                f"Бесплатные {TRIAL_LIMIT} вопросов закончились.\n\n"
                "Чтобы продолжить, введи код доступа или нажми кнопку ниже, "
                "чтобы отправить заявку Анастасии.",
                reply_markup=request_access_keyboard(),
            )
            return False
        return True

    # Pending mode (ждём номер билета)
    if "pending_mode" in context.user_data:
        bilet_num = extract_bilet_number(user_text)
        if bilet_num:
            mode = context.user_data["pending_mode"]
            if mode == "praktika" and bilet_num not in PRAKTIKA_FILE_IDS:
                context.user_data.pop("pending_mode", None)
                await send_long_message(
                    update,
                    praktika_unavailable_message(bilet_num),
                    reply_markup=MAIN_KEYBOARD,
                )
                return
            if not await _try_charge():
                return
            context.user_data.pop("pending_mode", None)
            await execute_mode(update, context, mode, bilet_num)
            await _maybe_warn_remaining(update, tg_user.id)
        else:
            await send_long_message(
                update,
                "Напиши номер билета, например: 1 или 15.",
                reply_markup=MAIN_KEYBOARD,
            )
        return

    # Кнопка режима
    if text_lower in BUTTON_MODES:
        mode = BUTTON_MODES[text_lower]
        if context.user_data.get("last_bilet_num"):
            last_num = context.user_data["last_bilet_num"]
            if mode == "praktika" and last_num not in PRAKTIKA_FILE_IDS:
                await send_long_message(
                    update,
                    praktika_unavailable_message(last_num),
                    reply_markup=MAIN_KEYBOARD,
                )
                return
            if not await _try_charge():
                return
            await execute_mode(update, context, mode, last_num)
            await _maybe_warn_remaining(update, tg_user.id)
        else:
            context.user_data["pending_mode"] = mode
            await send_long_message(update, "Напиши номер билета:", reply_markup=MAIN_KEYBOARD)
        return

    # "Проверь меня по билету N"
    if is_quiz_request(user_text):
        bilet_num = extract_bilet_number(user_text)
        if bilet_num:
            bilet_text = load_bilet_text(bilet_num)
            if bilet_text:
                if not await _try_charge():
                    return
                await start_quiz(update, context, bilet_num, bilet_text)
                await _maybe_warn_remaining(update, tg_user.id)
                return
            else:
                await send_long_message(
                    update,
                    f"Билет {bilet_num} не найден.",
                    reply_markup=MAIN_KEYBOARD,
                )
                return

    # Просто число → полный ответ
    if re.fullmatch(r"\d+", user_text):
        bilet_num = int(user_text)
        if not await _try_charge():
            return
        await execute_mode(update, context, "full", bilet_num)
        await _maybe_warn_remaining(update, tg_user.id)
        return

    # Свободный вопрос ИИ
    if not await _try_charge():
        return

    await update.message.chat.send_action("typing")

    try:
        answer = ask_openai(user_text)
        await send_long_message(update, answer, reply_markup=MAIN_KEYBOARD)
        await _maybe_warn_remaining(update, tg_user.id)
    except Exception as e:
        logger.exception("Ошибка при обработке сообщения")
        await send_long_message(
            update,
            f"Произошла ошибка при обращении к ИИ:\n{e}",
            reply_markup=MAIN_KEYBOARD,
        )


# ---------------------------
# КОМАНДЫ ДОСТУПА
# ---------------------------

async def whoami_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    ensure_user(u.id, u.username, u.first_name)
    username = f"@{u.username}" if u.username else "—"
    role = "админ" if is_admin(u.id) else (get_user(u.id) or {}).get("status", "trial")
    await update.message.reply_text(
        f"Твой Telegram ID: {u.id}\n"
        f"Имя: {u.first_name or '—'}\n"
        f"Username: {username}\n"
        f"Статус: {role}"
    )


def _admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update.effective_user.id):
            await update.message.reply_text("Команда доступна только администраторам.")
            return
        return await func(update, context)

    wrapper.__name__ = func.__name__
    return wrapper


def _parse_target_id(args: list[str]) -> int | None:
    if not args:
        return None
    try:
        return int(args[0])
    except ValueError:
        return None


@_admin_only
async def grant_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target = _parse_target_id(context.args)
    if target is None:
        await update.message.reply_text("Использование: /grant <user_id>")
        return
    approve_user(target)
    await update.message.reply_text(f"✅ Пользователь {target} получил полный доступ.")
    try:
        await context.bot.send_message(
            chat_id=target,
            text="✅ Доступ открыт! Теперь можно заниматься без ограничений.",
            reply_markup=MAIN_KEYBOARD,
        )
    except Exception as e:
        logger.warning(f"Не удалось уведомить пользователя {target}: {e}")


@_admin_only
async def revoke_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target = _parse_target_id(context.args)
    if target is None:
        await update.message.reply_text("Использование: /revoke <user_id>")
        return
    revoke_user(target)
    await update.message.reply_text(f"Доступ пользователя {target} отозван (вернули в trial).")


@_admin_only
async def block_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target = _parse_target_id(context.args)
    if target is None:
        await update.message.reply_text("Использование: /block <user_id>")
        return
    block_user(target)
    await update.message.reply_text(f"🚫 Пользователь {target} заблокирован.")


@_admin_only
async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    users = list_all_users()
    if not users:
        await update.message.reply_text("Пользователей пока нет.")
        return
    lines = ["👥 Пользователи бота:\n"]
    for u in users:
        username = f"@{u['username']}" if u["username"] else "—"
        name = u["first_name"] or "—"
        lines.append(
            f"{u['user_id']} — {username} — {name} — "
            f"{u['status']} ({u['trial_used']}/{TRIAL_LIMIT})"
        )
    await send_long_message(update, "\n".join(lines))


@_admin_only
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = get_stats()
    text = (
        "📊 Статистика бота\n\n"
        f"Всего пользователей: {s['users_total']}\n"
        f"  • trial:    {s[STATUS_TRIAL]}\n"
        f"  • approved: {s[STATUS_APPROVED]}\n"
        f"  • blocked:  {s[STATUS_BLOCKED]}\n\n"
        f"Всего вопросов задано: {s['questions_total']}"
    )
    await update.message.reply_text(text)


# ---------------------------
# CALLBACK QUERY (inline-кнопки)
# ---------------------------

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    requester = update.effective_user

    if data == "request_access":
        rec = ensure_user(requester.id, requester.username, requester.first_name)
        username = f"@{requester.username}" if requester.username else "—"
        admin_text = (
            "📩 Запрос на доступ к боту\n\n"
            f"ID: {requester.id}\n"
            f"Имя: {requester.first_name or '—'}\n"
            f"Username: {username}\n"
            f"Использовано: {rec['trial_used']}/{TRIAL_LIMIT}\n"
            f"Статус: {rec['status']}"
        )

        if not ADMIN_USER_IDS:
            await query.edit_message_text(
                "Не удалось отправить заявку: у бота не настроены администраторы. "
                "Сообщи об этом владельцу бота лично."
            )
            return

        delivered = 0
        for admin_id in ADMIN_USER_IDS:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=admin_text,
                    reply_markup=admin_decision_keyboard(requester.id),
                )
                delivered += 1
            except Exception as e:
                logger.warning(f"Не удалось отправить заявку админу {admin_id}: {e}")

        if delivered:
            await query.edit_message_text(
                "Заявка отправлена. Анастасия скоро её рассмотрит — "
                "когда доступ будет открыт, бот сам тебе напишет."
            )
        else:
            await query.edit_message_text(
                "Не удалось доставить заявку администраторам. Попробуй позже или напиши Анастасии лично."
            )
        return

    if data.startswith("approve:") or data.startswith("reject:"):
        if not is_admin(requester.id):
            await query.answer("Только администратор может одобрять заявки.", show_alert=True)
            return

        action, _, target_str = data.partition(":")
        try:
            target_id = int(target_str)
        except ValueError:
            return

        original_text = query.message.text or "Заявка"

        if action == "approve":
            approve_user(target_id)
            try:
                await query.edit_message_text(original_text + "\n\n✅ Одобрено.")
            except Exception:
                pass
            try:
                await context.bot.send_message(
                    chat_id=target_id,
                    text="✅ Доступ открыт! Теперь можно заниматься без ограничений.",
                    reply_markup=MAIN_KEYBOARD,
                )
            except Exception as e:
                logger.warning(f"Не удалось уведомить пользователя {target_id}: {e}")
        else:
            try:
                await query.edit_message_text(original_text + "\n\n❌ Отклонено.")
            except Exception:
                pass
            try:
                await context.bot.send_message(
                    chat_id=target_id,
                    text="К сожалению, заявка на доступ к боту была отклонена.",
                )
            except Exception as e:
                logger.warning(f"Не удалось уведомить пользователя {target_id}: {e}")


def main():
    init_db()

    app = ApplicationBuilder().token(TG_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("reset", reset_command))

    # Доступные всем
    app.add_handler(CommandHandler("whoami", whoami_command))

    # Только для админов
    app.add_handler(CommandHandler("grant", grant_command))
    app.add_handler(CommandHandler("revoke", revoke_command))
    app.add_handler(CommandHandler("block", block_command))
    app.add_handler(CommandHandler("users", users_command))
    app.add_handler(CommandHandler("stats", stats_command))

    # Inline-кнопки (запрос доступа, одобрение/отклонение)
    app.add_handler(CallbackQueryHandler(handle_callback))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Бот запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()