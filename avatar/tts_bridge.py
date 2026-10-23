"""Мост между голосовым плагином и окном: слушает поток озвучки.

Без Tk и без окна: подписывается на /dsh-tts/stream, каждую произнесённую
фразу превращает в wav и дописывает путь в файл _phrases.txt. Окно
(call_live.py) читает этот файл и показывает губы под тот же звук.

Запуск:
    pythonw tts_bridge.py
"""

import base64
import csv
import io
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

WORK = Path(__file__).resolve().parent
FFMPEG = r"<DSH_VOICE>\ffmpeg\bin\ffmpeg.exe"
QUEUE_FILE = WORK / "_phrases.txt"
ORIGIN_FILE = WORK / "_origin.txt"
LOG_FILE = WORK / "_bridge.log"
CREATE_NO_WINDOW = 0x08000000
# Под pythonw любая консольная утилита (ffmpeg, netstat, tasklist) открывает
# окно командной строки и отбирает фокус у голосового режима. Просим Windows
# не создавать окно для всех запусков сразу.
_real_run = subprocess.run


def _quiet_run(*args, **kwargs):
    kwargs.setdefault("creationflags", CREATE_NO_WINDOW)
    return _real_run(*args, **kwargs)


subprocess.run = _quiet_run
FALLBACK_ORIGINS = ["http://127.0.0.1:64810", "http://127.0.0.1:60101", "http://127.0.0.1:49579"]


def note(text):
    try:
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(time.strftime("%H:%M:%S ") + str(text) + "\n")
    except Exception:  # noqa: BLE001
        pass


def origin_works(origin, timeout=2.0):
    try:
        request = urllib.request.Request(origin + "/dsh-tts/stream", headers={"Accept": "text/event-stream"})
        with urllib.request.urlopen(request, timeout=timeout) as answer:
            kind = (answer.headers.get("Content-Type") or "").lower()
            return answer.status == 200 and "text/event-stream" in kind
    except Exception:  # noqa: BLE001
        return False


def saved_origin():
    """Порт DSH меняется при каждом перезапуске, поэтому помним удачный."""
    try:
        text = ORIGIN_FILE.read_text(encoding="utf-8").strip()
        return text or None
    except Exception:  # noqa: BLE001
        return None


def remember_origin(origin):
    try:
        ORIGIN_FILE.write_text(origin, encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def all_local_ports(skip=("6011", "6012", "6006", "6007")):
    """Все порты на 127.0.0.1, которые кто-то слушает: последняя попытка поиска."""
    ports = []
    try:
        net = subprocess.run(["netstat", "-ano"], capture_output=True, timeout=20).stdout.decode("cp866", "ignore")
        for line in net.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
                address, port = parts[1].rsplit(":", 1)
                if address.startswith("127.0.0.1") and port.isdigit() and port not in skip:
                    if port not in ports:
                        ports.append(port)
    except Exception:  # noqa: BLE001
        pass
    return ports



def dsh_ports():
    """Порты, которые слушает сам DSH: PID его процессов + netstat."""
    pids = set()
    try:
        raw = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, timeout=20).stdout
        for row in csv.reader(io.StringIO(raw.decode("cp866", "ignore"))):
            if len(row) >= 2 and "deepseek" in row[0].lower():
                pids.add(row[1])
    except Exception:  # noqa: BLE001
        pass
    ports = []
    try:
        net = subprocess.run(["netstat", "-ano"], capture_output=True, timeout=20).stdout.decode("cp866", "ignore")
        for line in net.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
                if pids and parts[4] not in pids:
                    continue
                port = parts[1].rsplit(":", 1)[-1]
                if port.isdigit() and port not in ports:
                    ports.append(port)
    except Exception:  # noqa: BLE001
        pass
    return ports


def find_origin():
    remembered = saved_origin()
    if remembered and origin_works(remembered):
        return remembered
    for candidate in FALLBACK_ORIGINS:
        if origin_works(candidate):
            remember_origin(candidate)
            return candidate
    for port in dsh_ports():
        candidate = f"http://127.0.0.1:{port}"
        if origin_works(candidate):
            remember_origin(candidate)
            return candidate
    # Последняя попытка: перебрать всё, что слушает localhost. Так находится
    # и новый порт DSH после перезапуска, и нестандартные сборки.
    for port in all_local_ports():
        candidate = f"http://127.0.0.1:{port}"
        if origin_works(candidate, 0.6):
            remember_origin(candidate)
            return candidate
    return None


def prepare(item):
    folder = Path(tempfile_dir())
    folder.mkdir(exist_ok=True)
    tag = str(item.get("id") or int(time.time() * 1000))[-12:]
    mp3 = folder / f"{tag}.mp3"
    wav = folder / f"{tag}.wav"
    mp3.write_bytes(base64.b64decode(item["audioBase64"]))
    subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(mp3), "-ac", "1", "-ar", "16000", str(wav)],
                   check=True, creationflags=CREATE_NO_WINDOW)
    note("готово: " + wav.name)
    with QUEUE_FILE.open("a", encoding="utf-8") as handle:
        handle.write(str(wav) + "\n")


def tempfile_dir():
    """Своя папка вместо системной: временная папка может исчезнуть,
    если процесс запущен из чужой сессии (так и случилось 19.10)."""
    return str(WORK / "_call")


def main():
    while True:
        origin = find_origin()
        if origin is None:
            note("origin не найден")
            time.sleep(3)
            continue
        note("подключение к " + origin)
        try:
            request = urllib.request.Request(origin + "/dsh-tts/stream", headers={"Accept": "text/event-stream"})
            with urllib.request.urlopen(request, timeout=300) as answer:
                note("подключились")
                event = None
                for raw in answer:
                    line = raw.decode("utf-8", "ignore").rstrip("\n")
                    if line.startswith("event:"):
                        event = line.split(":", 1)[1].strip()
                    elif line.startswith("data:") and event == "utterance":
                        item = json.loads(line.split(":", 1)[1].strip())
                        if item.get("audioBase64"):
                            note("фраза " + str(item.get("id")))
                            try:
                                prepare(item)
                            except Exception as error:  # noqa: BLE001
                                note("ошибка подготовки: " + str(error)[:160])
        except Exception as error:  # noqa: BLE001
            note("поток: " + str(error)[:150])
            time.sleep(2)


if __name__ == "__main__":
    main()
