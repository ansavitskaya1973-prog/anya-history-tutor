# Anya History Tutor

Telegram-бот, помогающий школьнице готовиться к экзамену по истории Беларуси (9 класс) по билетам.

## Стек
Python 3.10+, python-telegram-bot, OpenAI API (`gpt-4.1-mini`) с `file_search` по vector store, python-dotenv, SQLite.

## Структура
- `main.py` — основной бот, обработчики команд и сообщений.
- `prompts.py` — системные промпты для OpenAI (`SYSTEM_PROMPT`, `BILET_CONTEXT_TEMPLATE`, `PRAKTIKA_INSTRUCTIONS`).
- `access.py` — контроль доступа (SQLite: статусы users, коды).
- `setup_vector_store.py` — создание нового vector store и загрузка всех материалов.
- `download_praktika_from_adu.py` — одноразовая утилита: качает актуальные практические задания с adu.by (старые в `_old/`).
- `upload_praktika_files.py` — одноразовая утилита: заливает PDF практических заданий в OpenAI Files (purpose=`user_data`), сохраняет mapping в `praktika_file_ids.json`.
- `update_praktika_in_vector_store.py` — одноразовая утилита: заменяет PDF практических заданий в существующем vector store.
- `praktika_file_ids.json` — `{номер_билета: openai_file_id}` для режима «Практическое задание». Коммитится.
- `materials/bilety/bilet_NN.txt` — **главный источник** ответов бота. При расхождениях с примерами ответов приоритет у этих файлов.
- `materials/primery_otvetov/` — примеры ответов (`primer_otveta_bilet_NN.docx`) и практические задания (`prakticheskoe_zadanie_bilet_NN.pdf`, актуальные с adu.by 2025-2026).
- `materials/official/` — официальные материалы.
- `bot_users.db` — SQLite-БД (в `.gitignore`, создаётся автоматически).

## .env (не коммитится)
Обязательные: `TG_BOT_TOKEN`, `OPENAI_API_KEY`, `VECTOR_STORE_ID`.
Контроль доступа: `ADMIN_USER_IDS`, `ACCESS_CODE`, `TRIAL_LIMIT`.

## Контроль доступа
- Админы из `ADMIN_USER_IDS` — полный доступ, без счётчика.
- Новые пользователи — `TRIAL_LIMIT` бесплатных запросов.
- После исчерпания — ввод `ACCESS_CODE` или кнопка «Запросить доступ» → заявка админу с inline-кнопками.
- Команды для всех: `/start`, `/help`, `/reset`, `/whoami`.
- Команды для админов: `/grant <id>`, `/revoke <id>`, `/block <id>`, `/users`, `/stats`.
- Платные действия: запрос билета (любой режим, включая «Практическое задание»), запуск quiz, свободный вопрос. Внутри quiz счётчик не тикает (списано на старте). Если для билета нет практического задания (8, 19 — «Наш край») — счётчик НЕ тикает, пользователь получает понятное сообщение.

## Режим «Практическое задание»
Отдельная кнопка в меню. Бот разбирает второй вопрос экзаменационного билета (источники + 4 задания). Использует vision: PDF задания подаётся в `Responses API` напрямую через `input_file` — модель `gpt-4.1-mini` видит и текст, и карты/таблицы/иллюстрации. file_id для каждого билета — в `praktika_file_ids.json`. Доступно для билетов 1-7, 9-18, 20-25 (всё, что публикует adu.by).

**Why:** бот рассчитан на одного пользователя, но может попасть к посторонним — нужен пробный режим + ручное одобрение, чтобы не платить за чужое использование OpenAI.

## Деплой
Прод на VPS, запуск через systemd-сервис `anya-history-tutor`. Логи: `journalctl -u anya-history-tutor`.

Детали SSH-доступа — в локальных конфигах разработчика, не в репо.

## Обновление текстов билетов
Бот отвечает **не из локальных файлов**, а через `file_search` по vector store OpenAI. После правки `materials/bilety/bilet_NN.txt`:

1. Файл в vector store нужно **заменить**: удалить старый `file-...` из store и из OpenAI Files, загрузить новый, привязать к тому же `VECTOR_STORE_ID`.
2. Если запустить `setup_vector_store.py` как есть — создастся **новый** vector store с новым ID, нужно будет вручную обновить `VECTOR_STORE_ID` в `.env` локально и на проде. Обычно это **не то, что нужно** — точечная замена лучше.
3. Локальный коммит и push в репо — для синхронизации, на runtime бота не влияет (он читает только vector store).
