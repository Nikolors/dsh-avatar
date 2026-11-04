"""Ежедневная точка возврата: копия всего, что мы правим руками.

Кладёт файлы в `voice-STABLE\\<дата>-день\\` и удаляет копии старше двух недель.
Запускается задачей `DshDailyBackup` каждый день поздно вечером; можно и руками:

    python avatar\\backup_day.py
"""
from __future__ import annotations

import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

WS = Path(r"<WORKSPACE>")
VOICE = Path(r"<DSH_VOICE>")
PROFILE = Path(r"<DSH_PROFILE>\profiles\desktop")
ROOT = WS / "voice-STABLE"

# Что копируем: (откуда, куда внутри папки дня)
FILES = [
    (WS / "avatar" / "call_live.py", "avatar/call_live.py"),
    (WS / "avatar" / "tts_bridge.py", "avatar/tts_bridge.py"),
    (WS / "avatar" / "watchdog.py", "avatar/watchdog.py"),
    (WS / "avatar" / "stream_probe.py", "avatar/stream_probe.py"),
    (VOICE / "conversation-plugin" / "lib" / "client.js", "plugin/client.js"),
    (VOICE / "edge-warm-server.py", "voice/edge-warm-server.py"),
    (PROFILE / "cordis.patch.yml", "profile/cordis.patch.yml"),
    (WS / "ПАМЯТКА.md", "ПАМЯТКА.md"),
]

KEEP_DAYS = 14
LOG = WS / "voice-STABLE" / "_backup.log"


def note(text: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{stamp} {text}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def main() -> None:
    import sys

    # Без аргумента — вечерняя точка возврата «<дата>-день».
    # С аргументом — срочная копия перед кардинальным изменением:
    #   python avatar\\backup_day.py before-restart
    tag = sys.argv[1].strip() if len(sys.argv) > 1 else ''
    today = date.today().isoformat()
    if tag:
        folder = f"{today}-{datetime.now().strftime('%H%M')}-{tag}"
    else:
        folder = f"{today}-день"
    target = ROOT / folder
    copied = 0
    for source, relative in FILES:
        if not source.exists():
            note(f"пропуск: нет файла {source}")
            continue
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied += 1
    note(f"точка возврата {target.name}: файлов {copied}" + (' (перед изменением)' if tag else ''))

    # Чистим старое, чтобы папка не распухала.
    limit = date.today() - timedelta(days=KEEP_DAYS)
    for folder in ROOT.glob("*-день"):
        try:
            folder_date = date.fromisoformat(folder.name.replace("-день", ""))
        except ValueError:
            continue
        if folder_date < limit:
            shutil.rmtree(folder, ignore_errors=True)
            note(f"удалена старая копия {folder.name}")


if __name__ == "__main__":
    main()
