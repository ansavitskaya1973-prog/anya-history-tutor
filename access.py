"""
Контроль доступа к Telegram-боту Anya History Tutor.

Логика:
- ADMIN_USER_IDS из .env — список Telegram user_id с полным админ-доступом
  (через запятую). Эти пользователи всегда работают без ограничений и видят
  админ-команды.
- ACCESS_CODE из .env — единый код доступа для одобрения новых учеников.
- TRIAL_LIMIT из .env — количество бесплатных запросов (по умолчанию 7).
- bot_users.db — SQLite-база, хранит статусы пользователей и счётчик
  использованных пробных вопросов.
"""

import os
import sqlite3
import logging
from pathlib import Path
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "bot_users.db"

TRIAL_LIMIT = int(os.getenv("TRIAL_LIMIT", "7"))
ACCESS_CODE = os.getenv("ACCESS_CODE", "")

_admin_ids_raw = os.getenv("ADMIN_USER_IDS", "")
ADMIN_USER_IDS = {
    int(x.strip()) for x in _admin_ids_raw.split(",") if x.strip().lstrip("-").isdigit()
}

STATUS_TRIAL = "trial"
STATUS_APPROVED = "approved"
STATUS_BLOCKED = "blocked"


# ---------------------------
# БАЗОВЫЕ ОПЕРАЦИИ С БД
# ---------------------------

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Создаёт таблицу users, если её ещё нет."""
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                username    TEXT,
                first_name  TEXT,
                status      TEXT NOT NULL DEFAULT 'trial',
                trial_used  INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT NOT NULL,
                approved_at TEXT
            )
            """
        )
        conn.commit()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------
# ПРОВЕРКИ И ЧТЕНИЕ
# ---------------------------

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS


def ensure_user(user_id: int, username: str | None, first_name: str | None) -> dict:
    """Возвращает запись пользователя, создавая её при первом обращении.
    Параллельно обновляет username/first_name, если они изменились в Telegram.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()

        if row is None:
            conn.execute(
                "INSERT INTO users (user_id, username, first_name, status, trial_used, created_at) "
                "VALUES (?, ?, ?, ?, 0, ?)",
                (user_id, username, first_name, STATUS_TRIAL, _now_iso()),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
        else:
            if row["username"] != username or row["first_name"] != first_name:
                conn.execute(
                    "UPDATE users SET username = ?, first_name = ? WHERE user_id = ?",
                    (username, first_name, user_id),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM users WHERE user_id = ?", (user_id,)
                ).fetchone()

        return dict(row)


def get_user(user_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None


def list_all_users() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM users ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_stats() -> dict:
    """Сводка: сколько пользователей в каждом статусе и сколько вопросов задано всего."""
    result = {
        STATUS_TRIAL: 0,
        STATUS_APPROVED: 0,
        STATUS_BLOCKED: 0,
        "questions_total": 0,
        "users_total": 0,
    }
    with _connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS cnt, COALESCE(SUM(trial_used), 0) AS questions "
            "FROM users GROUP BY status"
        ).fetchall()
        for r in rows:
            result[r["status"]] = r["cnt"]
            result["users_total"] += r["cnt"]
            result["questions_total"] += r["questions"]
    return result


# ---------------------------
# ИЗМЕНЕНИЕ СОСТОЯНИЯ
# ---------------------------

def try_increment_trial(user_id: int, limit: int) -> tuple[bool, int]:
    """Атомарно увеличивает счётчик пробных вопросов, если лимит ещё не достигнут.

    Возвращает (успех, текущее значение trial_used).
    Если пользователь не в статусе trial или лимит исчерпан — возвращает (False, текущее значение).
    """
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE users SET trial_used = trial_used + 1 "
            "WHERE user_id = ? AND status = ? AND trial_used < ?",
            (user_id, STATUS_TRIAL, limit),
        )
        conn.commit()
        row = conn.execute(
            "SELECT trial_used FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        current = row["trial_used"] if row else 0
        return (cur.rowcount > 0, current)


def approve_user(user_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET status = ?, approved_at = ? WHERE user_id = ?",
            (STATUS_APPROVED, _now_iso(), user_id),
        )
        conn.commit()


def revoke_user(user_id: int) -> None:
    """Возвращает пользователя обратно в trial-статус и сбрасывает счётчик."""
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET status = ?, trial_used = 0, approved_at = NULL WHERE user_id = ?",
            (STATUS_TRIAL, user_id),
        )
        conn.commit()


def block_user(user_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET status = ? WHERE user_id = ?",
            (STATUS_BLOCKED, user_id),
        )
        conn.commit()


def validate_code(code: str) -> bool:
    if not ACCESS_CODE:
        return False
    return code.strip() == ACCESS_CODE
