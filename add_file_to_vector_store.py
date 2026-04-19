"""
Добавить один файл к существующему vector store.

Использование:
    python add_file_to_vector_store.py <путь_к_файлу>

Пример:
    python add_file_to_vector_store.py materials/bilety/bilet_24.txt
"""
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

api_key = os.getenv("OPENAI_API_KEY")
vector_store_id = os.getenv("VECTOR_STORE_ID")

if not api_key:
    raise ValueError("OPENAI_API_KEY не найден в .env")
if not vector_store_id:
    raise ValueError("VECTOR_STORE_ID не найден в .env")

client = OpenAI(api_key=api_key)


def add_file(file_path: Path) -> None:
    if not file_path.exists():
        raise FileNotFoundError(f"Файл не найден: {file_path}")

    print(f"[i] Загружаю файл в OpenAI: {file_path}")
    with open(file_path, "rb") as f:
        uploaded = client.files.create(file=f, purpose="assistants")
    print(f"[i] Получен file_id: {uploaded.id}")

    print(f"[i] Добавляю file_id к vector store: {vector_store_id}")
    vs_file = client.vector_stores.files.create(
        vector_store_id=vector_store_id,
        file_id=uploaded.id,
    )
    print(f"[i] Добавлен. Статус: {vs_file.status}")

    while vs_file.status == "in_progress":
        time.sleep(2)
        vs_file = client.vector_stores.files.retrieve(
            vector_store_id=vector_store_id,
            file_id=uploaded.id,
        )
        print(f"[i] Статус обработки: {vs_file.status}")

    print("\n=== ГОТОВО ===")
    print(f"Файл: {file_path.name}")
    print(f"file_id: {uploaded.id}")
    print(f"Финальный статус: {vs_file.status}")


def main() -> None:
    if len(sys.argv) != 2:
        print("Использование: python add_file_to_vector_store.py <путь_к_файлу>")
        sys.exit(1)

    file_path = Path(sys.argv[1])
    add_file(file_path)


if __name__ == "__main__":
    main()
