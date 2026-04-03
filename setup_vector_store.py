import os
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    raise ValueError("OPENAI_API_KEY не найден в .env")

client = OpenAI(api_key=api_key)

BASE_DIR = Path(__file__).resolve().parent
MATERIALS_DIR = BASE_DIR / "materials"

FOLDERS_TO_UPLOAD = [
    MATERIALS_DIR / "bilety",
    MATERIALS_DIR / "primery_otvetov",
    MATERIALS_DIR / "official",
]

ALLOWED_EXTENSIONS = {".txt", ".pdf", ".docx", ".md"}


def collect_files():
    files = []
    for folder in FOLDERS_TO_UPLOAD:
        if not folder.exists():
            print(f"[!] Папка не найдена: {folder}")
            continue

        for path in folder.rglob("*"):
            if path.is_file() and path.suffix.lower() in ALLOWED_EXTENSIONS:
                files.append(path)

    return files


def upload_files(file_paths):
    uploaded_ids = []

    for file_path in file_paths:
        print(f"[i] Загружаю: {file_path}")
        with open(file_path, "rb") as f:
            uploaded = client.files.create(
                file=f,
                purpose="assistants"
            )
        uploaded_ids.append(uploaded.id)

    return uploaded_ids


def wait_for_batch(vector_store_id, batch_id):
    while True:
        batch = client.vector_stores.file_batches.retrieve(
            vector_store_id=vector_store_id,
            batch_id=batch_id
        )

        counts = batch.file_counts
        print(
            f"[i] Статус: {batch.status} | "
            f"completed={counts.completed}, "
            f"in_progress={counts.in_progress}, "
            f"failed={counts.failed}, "
            f"total={counts.total}"
        )

        if batch.status in ("completed", "failed", "cancelled"):
            return batch

        time.sleep(3)


def main():
    files_to_upload = collect_files()

    if not files_to_upload:
        print("[!] Подходящие файлы не найдены.")
        return

    print(f"[i] Найдено файлов для загрузки: {len(files_to_upload)}")

    uploaded_file_ids = upload_files(files_to_upload)
    print(f"[i] Загружено файлов: {len(uploaded_file_ids)}")

    vector_store = client.vector_stores.create(
        name="Anya Belarus History 9"
    )
    print(f"[i] Создан Vector Store: {vector_store.id}")

    batch = client.vector_stores.file_batches.create(
        vector_store_id=vector_store.id,
        file_ids=uploaded_file_ids
    )
    print(f"[i] Создан batch: {batch.id}")

    final_batch = wait_for_batch(vector_store.id, batch.id)

    print("\n=== ГОТОВО ===")
    print(f"VECTOR_STORE_ID={vector_store.id}")
    print(f"Финальный статус batch: {final_batch.status}")

    if final_batch.file_counts.failed > 0:
        print("[!] Часть файлов не обработалась. Но Vector Store создан.")


if __name__ == "__main__":
    main()
