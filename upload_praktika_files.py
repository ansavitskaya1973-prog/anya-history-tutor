"""
Одноразовый скрипт: загружает PDF практических заданий в OpenAI Files API
с purpose="user_data" — чтобы потом подавать их напрямую в input Responses API
для разбора моделью с vision (gpt-4.1-mini).

На выходе — praktika_file_ids.json: {"1": "file-XXX", "2": "file-YYY", ...}.

Идемпотентность: если JSON уже есть, и для номера билета file_id указан и
файл существует в OpenAI Files — не перезагружает.

Использование: после download_praktika_from_adu.py (или после ручного обновления
PDF), запустить `python upload_praktika_files.py`.
"""

import json
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    raise ValueError("OPENAI_API_KEY не найден в .env")

client = OpenAI(api_key=api_key)

BASE_DIR = Path(__file__).resolve().parent
PRAKTIKA_DIR = BASE_DIR / "materials" / "primery_otvetov"
IDS_FILE = BASE_DIR / "praktika_file_ids.json"

FILENAME_RE = re.compile(r"prakticheskoe_zadanie_bilet_(\d{2})\.pdf$")


def load_existing_ids() -> dict[str, str]:
    if IDS_FILE.exists():
        return json.loads(IDS_FILE.read_text(encoding="utf-8"))
    return {}


def save_ids(ids: dict[str, str]):
    IDS_FILE.write_text(
        json.dumps(ids, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def file_exists_in_openai(file_id: str) -> bool:
    try:
        client.files.retrieve(file_id)
        return True
    except Exception:
        return False


def main():
    if not PRAKTIKA_DIR.exists():
        raise FileNotFoundError(f"Не найдена папка: {PRAKTIKA_DIR}")

    ids = load_existing_ids()
    pdfs = sorted(
        p for p in PRAKTIKA_DIR.glob("prakticheskoe_zadanie_bilet_*.pdf")
        if FILENAME_RE.search(p.name)
    )

    print(f"[i] Найдено PDF: {len(pdfs)}")
    print(f"[i] Уже в JSON: {len(ids)}")

    uploaded = 0
    skipped = 0
    for pdf in pdfs:
        m = FILENAME_RE.search(pdf.name)
        bilet_num = str(int(m.group(1)))  # "07" -> "7"

        existing_id = ids.get(bilet_num)
        if existing_id and file_exists_in_openai(existing_id):
            print(f"[i] Билет {bilet_num}: уже загружен ({existing_id}), пропуск")
            skipped += 1
            continue

        with open(pdf, "rb") as f:
            uploaded_obj = client.files.create(file=f, purpose="user_data")
        ids[bilet_num] = uploaded_obj.id
        save_ids(ids)  # пишем сразу, чтобы не потерять при сбое
        print(f"[i] Билет {bilet_num}: загружен -> {uploaded_obj.id}")
        uploaded += 1

    print("\n=== ИТОГ ===")
    print(f"Загружено новых: {uploaded}")
    print(f"Пропущено (уже были): {skipped}")
    print(f"Всего в {IDS_FILE.name}: {len(ids)}")


if __name__ == "__main__":
    main()
