"""Сторож голосовых служб: следит, чтобы разговор не замирал молча.

Проверяет каждые 30 секунд:
  * порт 6006 — распознавание (GigaAM);
  * порт 6007 — журнал отладки;
  * порт 8901 — тёплая озвучка;
  * процесс окна с лицом (call_live.py);
  * процесс моста озвучки (tts_bridge.py).

Если что-то упало — перезапускает только это звено и пишет в журнал
`avatar\\_watchdog2.log`. Если журнал плагина молчит дольше 3 минут при
включённом режиме — считает, что разговор замер, и перезапускает мост.

Запускается задачей DshWatchdog (pythonw, без окна).
"""
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path

WS = Path(r"<WORKSPACE>")
VOICE = Path(r"<DSH_VOICE>")
LOG = WS / "avatar" / "_watchdog2.log"
PLUGIN_LOG = VOICE / "conversation-debug.log"

CHECK_EVERY = 30          # как часто проверяем
SUSPECT_AFTER = 90        # молчание, после которого начинаем подозревать
STOP_AFTER = 180          # молчание, после которого перезапускаем
MAX_RESTARTS = 3          # попыток подряд на одно звено
COOLDOWN = 600            # пауза между сериями перезапусков, секунды

CREATE_NO_WINDOW = 0x08000000

SERVICES = {
    "распознавание": {
        "port": 6006,
        "launcher": VOICE / "start-gigaam.cmd",
        "process": None,
    },
    "журнал отладки": {
        "port": 6007,
        "launcher": VOICE / "start-debug.cmd",
        "process": None,
    },
    "озвучка": {
        "port": 8901,
        "launcher": VOICE / "edge-warm-start.cmd",
        "process": None,
    },
    "окно с лицом": {
        "port": None,
        "launcher": None,
        "process": "call_live.py",
        "task": "DshWindow",
    },
    "мост озвучки": {
        "port": None,
        "launcher": None,
        "process": "tts_bridge.py",
        "task": "DshBridge",
    },
}


def note(text):
    stamp = datetime.now().strftime("%H:%M:%S")
    try:
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {text}\n")
    except OSError:
        pass


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


def process_alive(marker):
    """Ищем процесс по куску командной строки (без psutil — он не всегда есть)."""
    try:
        import psutil
    except ImportError:
        return True          # без psutil не мешаем работать
    for proc in psutil.process_iter(["pid", "cmdline"]):
        cmdline = " ".join(proc.info.get("cmdline") or [])
        if marker in cmdline:
            return True
    return False


def plugin_mode():
    """Включён ли разговорный режим по последней строке журнала плагина."""
    try:
        with open(PLUGIN_LOG, "rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - 60000))
            tail = handle.read().decode("utf-8", "ignore")
    except OSError:
        return None
    state = None
    for line in tail.splitlines():
        if '"message":"state -> ' in line:
            state = line.split('"message":"state -> ', 1)[1].split('"', 1)[0]
    return state


def launch(name, info):
    """Перезапуск одного звена."""
    if info.get("launcher"):
        if not info["launcher"].exists():
            note(f"{name}: нет файла запуска {info['launcher'].name}")
            return False
        subprocess.Popen(
            ["cmd.exe", "/c", str(info["launcher"])],
            creationflags=CREATE_NO_WINDOW,
        )
        note(f"{name}: перезапуск через {info['launcher'].name}")
        return True
    task = info.get("task")
    if task:
        subprocess.run(["schtasks", "/End", "/TN", task],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)
        time.sleep(1)
        subprocess.run(["schtasks", "/Run", "/TN", task],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)
        note(f"{name}: перезапуск задачи {task}")
        return True
    return False


def main():
    note("сторож запущен")
    restarts = {name: 0 for name in SERVICES}
    last_restart = {name: 0.0 for name in SERVICES}
    silent_since = None

    while True:
        try:
            now = time.monotonic()

            for name, info in SERVICES.items():
                broken = False
                if info["port"] and not port_open(info["port"]):
                    broken = True
                if info["process"] and not process_alive(info["process"]):
                    broken = True
                if not broken:
                    continue

                if restarts[name] >= MAX_RESTARTS and now - last_restart[name] < COOLDOWN:
                    continue      # не молотим по кругу
                if now - last_restart[name] < 45:
                    continue      # даём подняться

                note(f"{name}: не отвечает — поднимаю")
                if launch(name, info):
                    restarts[name] += 1
                    last_restart[name] = now

            # Тишина в журнале при включённом режиме — разговор замер.
            state = plugin_mode()
            # Тишина считается подозрительной только когда плагин ждёт или
            # думает. Во время речи он вообще может долго не писать строк —
            # это нормально, и ночью сторож зря ругался (22.10).
            active = state in ("listening", "waiting", "thinking", "recording")
            if active:
                try:
                    written = PLUGIN_LOG.stat().st_mtime
                except OSError:
                    written = 0
                if silent_since is None or written > silent_since:
                    silent_since = written or now
                quiet = now - silent_since
                if quiet >= SUSPECT_AFTER and quiet < SUSPECT_AFTER + CHECK_EVERY * 2:
                    note(f"журнал молчит {int(quiet)} с при состоянии {state}")
                if quiet >= STOP_AFTER:
                    note(f"разговор замер ({int(quiet)} с) — перезапускаю мост")
                    launch("мост озвучки", SERVICES["мост озвучки"])
                    silent_since = None
            else:
                silent_since = None
        except Exception as error:      # noqa: BLE001
            note(f"сбой сторожа: {type(error).__name__} {error}")

        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    main()
