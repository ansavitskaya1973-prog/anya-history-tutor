"""
Одноразовый скрипт: заменяет в существующем vector store устаревшие версии
файлов prakticheskoe_zadanie_bilet_NN.pdf на свежие из materials/primery_otvetov/.

Логика (по образцу update_two_bilety.py для bilet_18.txt / bilet_21.txt):
1. Листает все файлы текущего vector store с пагинацией.
2. Для каждого: client.files.retrieve(file_id).filename — если имя начинается
   с "prakticheskoe_zadanie_bilet_" и оканчивается на ".pdf" — это кандидат на замену.
3. Удаляет связь с vector store + удаляет сам файл из OpenAI Files.
4. Загружает свежие PDF из materials/primery_otvetov/ с purpose="assistants".
5. Привязывает новые file_ids к тому же vector store через file_batch.
"""

import os
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

BASE_DIR = Path(__file__).resolve().parent
PRAKTIKA_DIR = BASE_DIR / "materials" / "primery_otvetov"


def is_target_filename(filename: str) -> bool:
    return (
        filename.startswith("prakticheskoe_zadanie_bilet_")
        and filename.endswith(".pdf")
    )


def find_old_file_ids() -> dict[str, str]:
    """filename -> file_id для всех практических заданий в vector store."""
    found = {}
    after = None
    while True:
        kwargs = {"vector_store_id": vector_store_id, "limit": 100}
        if after:
            kwargs["after"] = after
        page = client.vector_stores.files.list(**kwargs)
        for vs_file in page.data:
            file_obj = client.files.retrieve(vs_file.id)
            if is_target_filename(file_obj.filename):
                found[file_obj.filename] = vs_file.id
        if not page.has_more:
            break
        after = page.data[-1].id
    return found


def wait_for_batch(batch_id: str):
    while True:
        batch = client.vector_stores.file_batches.retrieve(
            vector_store_id=vector_store_id,
            batch_id=batch_id,
        )
        counts = batch.file_counts
        print(
            f"[i] batch={batch.status} | completed={counts.completed}, "
            f"in_progress={counts.in_progress}, failed={counts.failed}, total={counts.total}"
        )
        if batch.status in ("completed", "failed", "cancelled"):
            return batch
        time.sleep(3)


def main():
    print(f"[i] Vector store: {vector_store_id}")
    print("[i] Ищу старые prakticheskoe_zadanie_bilet_*.pdf в vector store...")
    old_ids = find_old_file_ids()
    print(f"[i] Найдено старых файлов: {len(old_ids)}")

    for filename, file_id in old_ids.items():
        client.vector_stores.files.delete(
            vector_store_id=vector_store_id,
            file_id=file_id,
        )
        client.files.delete(file_id)
        print(f"[i] Удалён старый: {filename} ({file_id})")

    new_pdfs = sorted(PRAKTIKA_DIR.glob("prakticheskoe_zadanie_bilet_*.pdf"))
    print(f"[i] Свежих PDF на диске: {len(new_pdfs)}")

    new_ids = []
    for pdf in new_pdfs:
        with open(pdf, "rb") as f:
            uploaded = client.files.create(file=f, purpose="assistants")
        new_ids.append(uploaded.id)
        print(f"[i] Загружен в Files (assistants): {pdf.name} -> {uploaded.id}")

    batch = client.vector_stores.file_batches.create(
        vector_store_id=vector_store_id,
        file_ids=new_ids,
    )
    print(f"[i] Создан batch: {batch.id}")
    final = wait_for_batch(batch.id)

    print("\n=== ИТОГ ===")
    print(f"Удалено старых: {len(old_ids)}")
    print(f"Добавлено новых: {len(new_ids)}")
    print(f"Финальный статус batch: {final.status}")
    if final.file_counts.failed > 0:
        print("[!] Часть файлов не обработалась.")


if __name__ == "__main__":
    main()
