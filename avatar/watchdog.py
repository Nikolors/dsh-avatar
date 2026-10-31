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
import os
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path

WS = Path(r"C:\work\workspace")
VOICE = Path(r"C:\Users\ipdnv\dsh-voice")
LOG = WS / "avatar" / "_watchdog2.log"
PLUGIN_LOG = VOICE / "conversation-debug.log"

CHECK_EVERY = 30          # как часто проверяем
SUSPECT_AFTER = 90        # молчание, после которого начинаем подозревать
STOP_AFTER = 180          # молчание, после которого перезапускаем
MAX_RESTARTS = 3          # попыток подряд на одно звено
COOLDOWN = 600            # пауза между сериями перезапусков, секунды

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
WINDOW_PYTHONW = WS / "liveportrait" / "venv" / "Scripts" / "pythonw.exe"
WINDOW_SCRIPT = WS / "avatar" / "call_live.py"

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


def marker_variants(marker):
    """Варианты имени скрипта в командной строке.

    25.10: задача DshWindow запускает окно по короткому (8.3) пути —
    `CALL_L~1.PY`, поэтому поиск по «call_live.py» не находил живой процесс,
    и сторож каждые 10 минут считал окно упавшим и перезапускал его.
    """
    marker = marker.lower()
    variants = {marker}
    if marker.endswith(".py"):
        variants.add(marker[:-3])
    if "call_live" in marker:
        variants.update({"call_live", "call_l~1.py", "call_live.py"})
    if "tts_bridge" in marker:
        variants.update({"tts_bridge", "tts_br~1.py"})
    if "watch_avatar" in marker:
        variants.update({"watch_avatar", "watch_a~1.py"})
    return variants


def process_alive(marker):
    """Ищем процесс по куску командной строки (без psutil — он не всегда есть)."""
    try:
        import psutil
    except ImportError:
        return True          # без psutil не мешаем работать
    variants = marker_variants(marker)
    for proc in psutil.process_iter(["pid", "cmdline"]):
        cmdline = " ".join(proc.info.get("cmdline") or []).lower()
        if any(variant in cmdline for variant in variants):
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
            continue
        # Новый формат (27.10): состояние отдельным полем JSON.
        if '"plugin":"conversation"' in line and '"state":"' in line:
            value = line.split('"state":"', 1)[1].split('"', 1)[0]
            if value in ("listening", "recording", "thinking", "speaking", "waiting", "off"):
                state = value
    return state


# Один сторож на машину. Без этого задача планировщика запускала второй
# экземпляр, сторожа начинали убивать детей друг друга и перезапускать их по
# кругу (найдено 27.10 поздно вечером: по два окна и по два моста).
_MUTEX = None


def single_instance() -> bool:
    """True, если этот экземпляр первый; иначе процесс должен выйти."""
    global _MUTEX
    # Замок на файле, а не проверка pid: два сторожа стартуют в одну секунду,
    # и проверка «файл есть?» их не различает (найдено 27.10, 23:00).
    lock_path = WS / "avatar" / "_watchdog.lock"
    try:
        import msvcrt
        handle = open(lock_path, "a+")
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        _MUTEX = handle          # держим открытым, пока живём
    except OSError:
        return False
    except Exception:  # noqa: BLE001 - защита не должна мешать работе
        return True
    return True


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
    if name == "окно с лицом":
        # Свой запуск, без планировщика: он после серии перезапусков падает.
        try:
            import psutil
            for proc in psutil.process_iter(["pid", "cmdline"]):
                cmdline = " ".join(proc.info.get("cmdline") or [])
                if "call_live.py" in cmdline:
                    proc.kill()
                    note(f"{name}: остановлен зависший процесс {proc.info['pid']}")
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
        subprocess.Popen(
            [str(WINDOW_PYTHONW), str(WINDOW_SCRIPT)],
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
        note("окно с лицом: запущено сторожем")
        return True

    task = info.get("task")
    if task:
        # Старый процесс мог зависнуть и держать мьютекс: без этого новый
        # экземпляр молча выходит, и окно остаётся мёртвым.
        try:
            import psutil
            for proc in psutil.process_iter(["pid", "cmdline"]):
                cmdline = " ".join(proc.info.get("cmdline") or [])
                if "call_live.py" in cmdline or "tts_bridge.py" in cmdline:
                    proc.kill()
                    note(f"{name}: остановлен зависший процесс {proc.info['pid']}")
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
        subprocess.run(["schtasks", "/End", "/TN", task],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)
        time.sleep(1)
        subprocess.run(["schtasks", "/Run", "/TN", task],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)
        note(f"{name}: перезапуск задачи {task}")
        return True
    return False



def avatar_window_visible():
    """Есть ли на экране видимое окно с лицом (заголовок «tk»)."""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        found = []

        def callback(hwnd, _param):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if buffer.value == "tk":
                found.append(hwnd)
                return False
            return True

        proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows(proc(callback), 0)
        return bool(found)
    except Exception:  # noqa: BLE001
        return True


def main():
    if not single_instance():
        note("второй сторож не нужен — выхожу")
        return
    note("сторож запущен")
    restarts = {name: 0 for name in SERVICES}
    last_restart = {name: 0.0 for name in SERVICES}
    silent_since = None
    window_hidden_since = None

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

                # Окно с лицом: если оно уже на экране, второй экземпляр
                # не нужен, даже если поиск по командной строке не сработал.
                if name == "окно с лицом" and avatar_window_visible():
                    continue

                if restarts[name] >= MAX_RESTARTS and now - last_restart[name] < COOLDOWN:
                    continue      # не молотим по кругу
                if now - last_restart[name] < 45:
                    continue      # даём подняться

                note(f"{name}: не отвечает — поднимаю")
                if launch(name, info):
                    restarts[name] += 1
                    last_restart[name] = now

            # Окно с лицом: процесс может быть жив, а на экране его нет.
            state_now = plugin_mode()
            if state_now not in (None, "off") and not avatar_window_visible():
                if window_hidden_since is None:
                    window_hidden_since = now
                elif now - window_hidden_since > 30:
                    # Только записываем: перезапуск по одному признаку «не видно»
                    # гонял окно по кругу каждые две минуты (разбор 27.10, 23:35).
                    # Реальный перезапуск — когда нет самого процесса.
                    note("окно с лицом не видно %d с, но процесс жив — не трогаю"
                         % int(now - window_hidden_since))
                    window_hidden_since = now - 30 + 120
            else:
                window_hidden_since = None

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
