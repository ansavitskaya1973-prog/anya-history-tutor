"""
Одноразовый скрипт: скачивает актуальные практические задания за 2025-2026 уч. год
с сайта Министерства образования (adu.by) и заменяет локальные PDF в materials/primery_otvetov/.

На adu.by опубликованы билеты 1-7, 9-18, 20-25 (без 8 и 19 — те разрабатываются на районном
уровне, для них в репо есть локальные _frunzenskiy.docx).

Старые PDF не удаляются, а перемещаются в materials/primery_otvetov/_old/ — на случай отката.
"""

import shutil
from pathlib import Path
from urllib.request import Request, urlopen

BASE_URL = "https://www.adu.by/images/2026/05/Edinye_prakticeskie_zadania_Istoria_Bel/rus/Bilet_{n}_rus.pdf"
BILET_NUMBERS = list(range(1, 8)) + list(range(9, 19)) + list(range(20, 26))  # 1-7, 9-18, 20-25

BASE_DIR = Path(__file__).resolve().parent
TARGET_DIR = BASE_DIR / "materials" / "primery_otvetov"
BACKUP_DIR = TARGET_DIR / "_old"


def backup_existing():
    BACKUP_DIR.mkdir(exist_ok=True)
    moved = 0
    for old_pdf in TARGET_DIR.glob("prakticheskoe_zadanie_bilet_*.pdf"):
        shutil.move(str(old_pdf), str(BACKUP_DIR / old_pdf.name))
        moved += 1
    print(f"[i] Перемещено в _old/: {moved} старых PDF")


def download_file(url: str, target: Path) -> int:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=60) as resp:
        data = resp.read()
    target.write_bytes(data)
    return len(data)


def main():
    if not TARGET_DIR.exists():
        raise FileNotFoundError(f"Не найдена папка: {TARGET_DIR}")

    print(f"[i] Цель: {TARGET_DIR}")
    backup_existing()

    downloaded = 0
    failed = []
    for n in BILET_NUMBERS:
        url = BASE_URL.format(n=n)
        target = TARGET_DIR / f"prakticheskoe_zadanie_bilet_{n:02d}.pdf"
        try:
            size = download_file(url, target)
            old = BACKUP_DIR / target.name
            old_size = old.stat().st_size if old.exists() else None
            mark = ""
            if old_size is not None:
                diff_pct = abs(size - old_size) / old_size * 100
                if diff_pct > 20:
                    mark = f"  [!] размер отличается на {diff_pct:.0f}% (было {old_size}, стало {size})"
                elif diff_pct > 0:
                    mark = f"  (было {old_size}, стало {size})"
                else:
                    mark = "  идентичный размер"
            else:
                mark = "  [NEW] новый файл (раньше не было)"
            print(f"[i] Билет {n:02d}: скачано {size} байт{mark}")
            downloaded += 1
        except Exception as e:
            print(f"[!] Билет {n:02d}: ошибка — {e}")
            failed.append(n)

    print("\n=== ИТОГ ===")
    print(f"Скачано: {downloaded} из {len(BILET_NUMBERS)}")
    if failed:
        print(f"Не скачались: {failed}")
    print("Пропущены: 8 и 19 (не публикуются централизованно)")
    print(f"Резерв старых: {BACKUP_DIR}")


if __name__ == "__main__":
    main()
