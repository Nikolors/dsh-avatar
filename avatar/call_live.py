"""Окно разговора с лицом: готовые клипы «говорит» и спокойный цикл.

Как устроено:
- мост озвучки (`tts_bridge.py`) дописывает в `_phrases.txt` путь к wav каждой
  произнесённой фразы; окно читает новые строки и ставит фразы в очередь;
- пока фраза звучит, окно показывает кадры из `_clips` (клип выбирается по
  длине реплики), в паузах — спокойный цикл из `_idle`;
- видимость окна повторяет голосовой режим: он включён — окно есть, выключен
  или приложение ушло с экрана — окно спрятано.

Запуск: pythonw call_live.py
"""

import collections
import datetime
import ctypes
import io
import json
import os
import queue
import random
import struct
import sys
import threading
import time
import tkinter as tk
import urllib.request
import wave
from ctypes import wintypes
from pathlib import Path

from PIL import Image, ImageTk

WORK = Path(__file__).resolve().parent
LOG = Path(r"<DSH_VOICE>\conversation-debug.log")
W, H = 320, 374
FPS = 25.0
TICK_MS = 12
START_LAG = 0.15     # задержка кадров от события «озвучка началась»
END_TRIM = 0.08      # на сколько подрезаем конец фразы (почти ничего)

CLIPS_DIR = WORK / "_clips"
IDLE_DIR = WORK / "_idle"
QUEUE_FILE = WORK / "_phrases.txt"
ORIGIN_FILE = WORK / "_origin.txt"
POS_FILE = WORK / "_window_pos.txt"
LIVE_LOG = WORK / "_live.log"

_LIVE_LOCK = threading.Lock()


def note(text):
    line = f"{time.strftime('%H:%M:%S')} {text}"
    try:
        with _LIVE_LOCK, open(LIVE_LOG, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def read_text(path, default=""):
    try:
        return path.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return default


def clip_source():
    return read_text(WORK / "_source.txt", "clips").lower() or "clips"


def idle_mode():
    return read_text(WORK / "_idle_mode.txt", "pingpong").lower() or "pingpong"


# ---------------------------------------------------------------- окно Windows

def app_window_visible():
    """Есть ли на экране видимое окно приложения DeepSeek Harness."""
    try:
        user32 = ctypes.windll.user32
        found = []

        def callback(hwnd, _param):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                length = user32.GetWindowTextLengthW(hwnd)
                if length <= 0:
                    return True
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                if buffer.value.endswith("DeepSeek Harness"):
                    found.append(buffer.value)
                    return False
            except Exception:  # noqa: BLE001
                return True
            return True

        proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        user32.EnumWindows(proc_type(callback), 0)
        return bool(found)
    except Exception:  # noqa: BLE001
        return True


def app_port_alive():
    origin = read_text(ORIGIN_FILE)
    if not origin:
        return True
    try:
        request = urllib.request.Request(origin + "/dsh-tts/status")
        with urllib.request.urlopen(request, timeout=1.5) as answer:
            return answer.status == 200
    except Exception:  # noqa: BLE001
        return False


def plugin_log_tail(size=60000):
    try:
        with open(LOG, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            total = handle.tell()
            handle.seek(max(0, total - size))
            return handle.read().decode("utf-8", "ignore")
    except OSError:
        return ""


def last_playback_time():
    """Когда плагин последний раз включал озвучку (по часам системы).

    Нужно, чтобы окно не начинало двигать губами раньше звука: текст
    появляется сразу, а озвучка играет кусками позже. Считаем по времени,
    а не по числу событий: журнал прокручивается, и счётчик сбивал привязку.
    """
    best = None
    for line in plugin_log_tail(200000).splitlines():
        if '"message":"reply playback started' not in line:
            continue
        try:
            # Строка вида: 2026-10-22T13:22:17.236Z {"plugin":"conversation",...
            stamp = line.split(' ', 1)[0]
            moment = datetime.datetime.strptime(stamp[:23], "%Y-%m-%dT%H:%M:%S.%f")
            best = moment.replace(tzinfo=datetime.timezone.utc).timestamp()
        except Exception:  # noqa: BLE001
            continue
    return best


def voice_state():
    """Последнее состояние голосового режима из журнала плагина."""
    best = None
    for line in plugin_log_tail().splitlines():
        marker = '"message":"state -> '
        if marker in line:
            best = line.split(marker, 1)[1].split('"', 1)[0]
    return best


def wav_duration(path):
    """Длительность по слышимой речи.

    Заголовок файла включает тишину в конце, и кадры «договаривали» в
    пустоту. Ищем последний громкий сэмпл и берём его за конец фразы.
    """
    try:
        with wave.open(str(path), "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate() or 16000
            if handle.getsampwidth() != 2:
                return frames / float(rate)
            raw = handle.readframes(frames)
    except Exception:  # noqa: BLE001
        return 0.0
    total = frames / float(rate)
    if not raw:
        return total
    try:
        samples = struct.unpack("<%dh" % (len(raw) // 2), raw)
    except Exception:  # noqa: BLE001
        return total
    threshold = 700        # примерно -33 dBFS: тише этого считаем тишиной
    step = 160             # окно 10 мс
    last_loud = 0
    for start in range(len(samples) - step, -1, -step):
        window = samples[start:start + step]
        peak = max(abs(low) for low in window)
        if peak >= threshold:
            last_loud = start + step
            break
    if not last_loud:
        return total
    audible = last_loud / float(rate) + 0.15
    return min(total, audible)


# ------------------------------------------------------------------- кино, то есть клипы

class ClipFeed:
    """Готовые клипы «говорит»: показываем по часам от начала реплики."""

    def __init__(self):
        self.paths = []
        try:
            for folder in sorted(CLIPS_DIR.glob("talk_*")):
                if folder.is_dir():
                    files = sorted(folder.glob("*.jpg"))
                    if files:
                        self.paths.append(files)
        except OSError:
            self.paths = []
        self.cache = collections.OrderedDict()
        self.clip = None
        self.started = None
        self.offset = 0
        self.next_index = 0
        self.last_pick = None
        self.rate = 1.0          # во сколько раз ускоряем клип под длину фразы
        if self.paths:
            note(f"клипы речи: {len(self.paths)} шт., "
                 f"{sum(len(p) for p in self.paths)} кадров")

    def available(self):
        return bool(self.paths)

    def start(self, duration, current=None):
        if not self.paths:
            return
        want = float(duration or 0.0)
        lengths = [len(clip) / FPS for clip in self.paths]
        order = sorted(range(len(self.paths)), key=lambda i: abs(lengths[i] - want))
        order = [i for i in order if i != self.last_pick] or order
        pick = random.choice(order[:3])
        self.last_pick = pick
        self.clip = self.paths[pick]
        self.offset = 0
        if current is not None:
            self.offset = self._closest_index(self.clip, current)
            # Чуть вперёд от найденного кадра: в первых кадрах клипа рот ещё
            # закрыт, и начало речи выглядело задержанным.
            self.offset = min(len(self.clip) - 1, self.offset + 3)
        self.started = time.monotonic()
        self.next_index = self.offset
        # Подгоняем темп под длину фразы: клип должен закончиться вместе со
        # звуком, а не раньше и не позже. Сильно менять темп нельзя — лицо
        # начнёт «дёргаться», поэтому ограничиваем 0,75..1,3.
        clip_seconds = max(0.3, len(self.clip) / FPS)
        if want > 0:
            self.rate = min(1.3, max(0.75, want / clip_seconds))
        else:
            self.rate = 1.0
        self.cache.clear()
        threading.Thread(target=self._warm, args=(self.clip,), daemon=True).start()

    def ensure(self, duration, current=None):
        if self.clip is None:
            self.start(duration, current)

    def _closest_index(self, clip, image):
        try:
            small = image.convert("L").resize((24, 24), Image.BILINEAR)
            target = list(small.getdata())
            best, best_score = 0, None
            step = max(1, len(clip) // 30)
            for index in range(0, len(clip), step):
                try:
                    frame = Image.open(clip[index]).convert("L").resize((24, 24), Image.BILINEAR)
                except Exception:  # noqa: BLE001
                    continue
                score = sum(abs(a - b) for a, b in zip(target, list(frame.getdata())))
                if best_score is None or score < best_score:
                    best, best_score = index, score
            return best
        except Exception:  # noqa: BLE001
            return 0

    def _warm(self, clip):
        for index in range(len(clip)):
            self._frame(clip, index)
            time.sleep(0.002)

    def _frame(self, clip, index):
        key = (id(clip), index)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        try:
            image = Image.open(clip[index]).convert("RGB")
        except Exception:  # noqa: BLE001
            return None
        self.cache[key] = image
        while len(self.cache) > 400:
            self.cache.popitem(last=False)
        return image

    def pause(self):
        """Запоминаем, где остановились: пауза не должна сдвигать клип."""
        self.held_at = time.monotonic()

    def resume(self, current=None):
        """Продолжаем клип, по возможности с позы, близкой к текущей.

        Между фразами окно показывает покой, и продолжение клипа «с того же
        места» давало рывок: поза покоя не совпадала с позой клипа.
        """
        held = getattr(self, "held_at", None)
        self.held_at = None
        if self.clip is None or self.started is None:
            return
        if current is not None:
            self.offset = min(len(self.clip) - 1, self._closest_index(self.clip, current) + 3)
            self.started = time.monotonic()
            self.next_index = self.offset
            return
        if held:
            self.started += max(0.0, min(6.0, time.monotonic() - held))

    def take(self):
        if self.clip is None or self.started is None:
            return None
        position = int((time.monotonic() - self.started) * FPS * self.rate) + self.offset
        if position < self.next_index:
            if self.next_index - position > 50:
                self.started = time.monotonic()
                self.next_index = self.offset
                position = self.offset
            else:
                return None
        self.next_index = position + 1
        length = len(self.clip)
        if length <= 1:
            return self._frame(self.clip, 0)
        cycle = position % (2 * length - 2)
        index = cycle if cycle < length else 2 * length - 2 - cycle
        return self._frame(self.clip, index)

    def stop(self):
        self.clip = None
        self.started = None
        self.held_at = None


class IdleFeed:
    """Спокойный цикл: мягкое покачивание туда-обратно, без швов."""

    def __init__(self):
        self.frames = []
        try:
            for path in sorted(IDLE_DIR.glob("idle_*.jpg")):
                try:
                    self.frames.append(Image.open(path).convert("RGB"))
                except Exception:  # noqa: BLE001
                    continue
        except OSError:
            self.frames = []
        self.index = 0
        self.direction = 1
        if self.frames:
            note(f"спокойный цикл: {len(self.frames)} кадров")

    def set_like(self, image):
        if image is None or not self.frames:
            return
        try:
            small = image.convert("L").resize((24, 24), Image.BILINEAR)
            target = list(small.getdata())
            best, best_score = 0, None
            step = max(1, len(self.frames) // 60)
            for index in range(0, len(self.frames), step):
                frame = self.frames[index].convert("L").resize((24, 24), Image.BILINEAR)
                score = sum(abs(a - b) for a, b in zip(target, list(frame.getdata())))
                if best_score is None or score < best_score:
                    best, best_score = index, score
            self.index = best
        except Exception:  # noqa: BLE001
            pass

    def next_frame(self):
        if not self.frames:
            return None
        image = self.frames[self.index]
        step = self.index + self.direction
        if step >= len(self.frames) or step < 0:
            self.direction = -self.direction
            step = self.index + self.direction
        self.index = max(0, min(len(self.frames) - 1, step))
        return image


class Phrase:
    def __init__(self, wav):
        self.wav = Path(wav)
        self.duration = wav_duration(self.wav)
        self.drawn = 0


# ------------------------------------------------------------------------- окно

class AvatarWindow:
    def __init__(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", "#0b0d12")
        x, y = self.saved_position()
        self.root.geometry(f"{W}x{H}+{x}+{y}")
        self.canvas = tk.Canvas(self.root, width=W, height=H, highlightthickness=0, bg="#0b0d12")
        self.canvas.pack()
        self.image_id = self.canvas.create_image(0, 0, anchor="nw")
        self.status_item = self.canvas.create_text(16, H - 16, anchor="w",
                                              text="Анастасия · онлайн", fill="#eef3fb",
                                              font=("Segoe UI", 11, "bold"))
        self.root.withdraw()
        self.photo = None
        self.last_painted = None

        self.canvas.bind("<Button-1>", self.start_drag)
        self.canvas.bind("<B1-Motion>", self.do_drag)
        self.canvas.bind("<ButtonRelease-1>", self.end_drag)
        self.drag = None

        self.visible = False
        self.phrase = None
        self.queue = []
        self.phrase_until = 0.0
        self.last_paint_at = time.monotonic()
        self.freeze_log_at = 0.0
        self.missed = 0
        self.last_state_check = 0.0
        self.last_app_check = 0.0
        self.last_idle = 0.0
        self.last_state_log = 0.0
        self.status_text = ""
        self.drawn = 0
        self.pending_start = 0.0
        self.phrase_started = 0.0
        self.play_ready = False
        self.wait_since = 0.0
        self.arrived_at = 0.0
        self.hold = False          # между фразами держим кадр, а не покой
        self.alpha = 1.0           # текущая прозрачность окна
        self.alpha_target = 1.0    # к чему стремимся
        self.fade_src = None       # с чего начинаем плавный вход в клип
        self.fade_left = 0         # сколько кадров ещё смешивать
        self.reply_started = False
        self.voice_state_now = None

        self.clip_mode = clip_source() == "clips"
        self.clips = ClipFeed()
        self.idle_plain = idle_mode() == "static"
        self.idle = IdleFeed()

        self.position = 0
        try:
            self.position = QUEUE_FILE.stat().st_size
        except OSError:
            self.position = 0

        note(f"источник кадров: {'клипы' if self.clip_mode else 'живой поток'}")
        self.root.after(0, self.tick)

    def saved_position(self):
        try:
            parts = read_text(POS_FILE).split()
            x, y = int(parts[0]), int(parts[1])
            screen_w = self.root.winfo_screenwidth()
            screen_h = self.root.winfo_screenheight()
            return max(0, min(x, screen_w - W)), max(0, min(y, screen_h - H))
        except Exception:  # noqa: BLE001
            return 40, 120

    def start_drag(self, event):
        self.drag = (event.x_root, event.y_root, self.root.winfo_x(), self.root.winfo_y())

    def do_drag(self, event):
        if not self.drag:
            return
        sx, sy, wx, wy = self.drag
        self.root.geometry(f"+{wx + event.x_root - sx}+{wy + event.y_root - sy}")

    def end_drag(self, _event):
        if not self.drag:
            return
        self.drag = None
        try:
            POS_FILE.write_text(f"{self.root.winfo_x()} {self.root.winfo_y()}", encoding="utf-8")
        except OSError:
            pass

    def set_alpha(self, value):
        value = max(0.0, min(1.0, value))
        if abs(value - self.alpha) < 0.001:
            return
        self.alpha = value
        try:
            self.root.attributes("-alpha", value)
        except Exception:  # noqa: BLE001
            pass

    def show(self):
        if not self.visible:
            self.alpha = 0.0
            try:
                self.root.attributes("-alpha", 0.0)
            except Exception:  # noqa: BLE001
                pass
            self.root.deiconify()
            self.visible = True
            self.alpha_target = 1.0
        if self.last_painted is None and self.idle.frames:
            self.paint(self.idle.frames[0], "Анастасия · онлайн")

    def hide(self):
        if self.visible:
            self.alpha_target = 0.0

    def paint(self, image, status=None):
        if image is None:
            return
        self.last_painted = image
        self.last_paint_at = time.monotonic()
        self.photo = ImageTk.PhotoImage(image)
        self.canvas.itemconfigure(self.image_id, image=self.photo)
        if status and status != self.status_text:
            self.status_text = status
            self.canvas.itemconfigure(self.status_item, text=status)

    # --- очередь фраз ----------------------------------------------------

    def read_phrases(self):
        try:
            size = QUEUE_FILE.stat().st_size
        except OSError:
            return
        if size <= self.position:
            if size < self.position:
                self.position = size
            return
        try:
            with open(QUEUE_FILE, "r", encoding="utf-8") as handle:
                handle.seek(self.position)
                chunk = handle.read()
                self.position = handle.tell()
        except OSError:
            return
        for line in chunk.splitlines():
            line = line.strip()
            if line and Path(line).exists():
                if not self.queue and self.phrase is None:
                    # Запоминаем, когда пришёл первый файл реплики: по нему
                    # решаем, началась ли уже озвучка.
                    self.arrived_at = time.time()
                self.queue.append(Phrase(line))
                note(f"фраза в очередь: {Path(line).name} (ждут {len(self.queue)})")

    def begin(self):
        if not self.queue:
            self.phrase = None
            return
        self.phrase = self.queue.pop(0)
        phrase = self.phrase
        entered = time.monotonic()
        # Переход от покоя к речи: три кадра смешиваем с прошлой картинкой.
        self.fade_src = self.last_painted
        self.fade_left = 2 if self.fade_src is not None else 0
        # Событие «озвучка началась» приходит чуть раньше самого звука,
        # поэтому кадры стартуют с небольшой задержкой, а конец почти не
        # подрезаем: иначе лицо заканчивало говорить раньше голоса.
        self.phrase_started = time.monotonic() + START_LAG
        self.phrase_until = self.phrase_started + max(0.2, phrase.duration - END_TRIM)
        self.reply_started = True
        self.show()
        # Один клип на всю реплику: на каждой фразе новый клип давал заметный
        # скачок головы (замер 21.10: 8-10 медиан обычного движения).
        if self.clips.clip is None:
            self.clips.start(phrase.duration or 0.0, self.last_painted)
            self.clips.started += START_LAG      # кадры идут вместе со звуком
        elif self.hold:
            self.hold = False
            self.clips.resume()      # продолжаем с того же места
        else:
            self.clips.resume(self.last_painted)
        clip = self.clips.clip
        note(f"подготовка клипа {time.monotonic() - entered:.2f} с")
        note(f"фраза {phrase.wav.name}: клип "
             f"{clip[0].parent.name if clip else 'нет'}, вход {self.clips.offset}, "
             f"длина {phrase.duration:.1f} с")
        self.status("Анастасия · говорит")

    def status(self, text):
        if text != self.status_text:
            self.status_text = text
            self.canvas.itemconfigure(self.status_item, text=text)

    def end_reply(self):
        self.phrase = None
        remaining = bool(self.queue)
        self.hold = remaining
        if remaining:
            # Реплика продолжается: остаёмся на последнем кадре речи, чтобы
            # не было прыжка в покой и обратно.
            self.clips.pause()
            note(f"кадров речи показано: {self.drawn}")
            self.drawn = 0
            self.wait_since = 0.0
            self.play_ready = False
            return
        self.status("Анастасия · онлайн")
        self.clips.pause()
        note(f"кадров речи показано: {self.drawn}")
        self.drawn = 0
        # Стык с покоем: встаём на кадр цикла, ближайший к последнему кадру
        # речи. Иначе голова прыгает в другую позу — это и видно как
        # «ещё чуть-чуть анимации» после фразы (замечание 21.10).
        self.idle.set_like(self.last_painted)
        # Возврат в покой тоже смешиваем: иначе голова прыгает в другую позу.
        self.fade_src = self.last_painted
        self.fade_left = 2 if self.fade_src is not None else 0
        self.wait_since = 0.0
        self.play_ready = False
        if not self.queue:
            self.reply_started = False

    # --- основной такт ---------------------------------------------------

    def tick(self):
        try:
            self.tick_once()
        except Exception as error:  # noqa: BLE001
            note(f"такт упал: {type(error).__name__} {error}")
        self.root.after(TICK_MS, self.tick)

    def tick_once(self):
        now = time.monotonic()

        # Плавное проявление и уход окна (примерно 0,15 с).
        if abs(self.alpha - self.alpha_target) > 0.01:
            step = 0.08 if self.alpha_target > self.alpha else -0.08
            self.set_alpha(self.alpha + step)
            if self.alpha_target == 0.0 and self.alpha <= 0.01 and self.visible:
                self.root.withdraw()
                self.visible = False

        # Видимость по состоянию режима и по самому приложению.
        if now - self.last_state_check > 0.5:
            self.last_state_check = now
            state = voice_state()
            self.voice_state_now = state
            if state == "off":
                if self.visible:
                    note("окно скрыто: режим выключен")
                self.hide()
            elif state and not self.visible:
                # Показываемся только если приложение правда на экране.
                if app_window_visible() and app_port_alive():
                    self.show()

        if now - self.last_app_check > 2.0:
            self.last_app_check = now
            if self.visible and not (app_window_visible() and app_port_alive()):
                self.missed += 1
                if self.missed >= 2:
                    self.missed = 0
                    note("окно скрыто: приложение закрыто")
                    self.hide()
            else:
                self.missed = 0

        self.read_phrases()

        if self.phrase is None:
            if self.queue:
                # Ждём начало озвучки только для ПЕРВОЙ фразы реплики: там
                # разрыв в несколько секунд (текст готов сразу, звук позже).
                # Дальше куски идут подряд, и привязка к событию опаздывала:
                # событие случалось, пока играл предыдущий клип.
                if self.reply_started:
                    self.pending_start = 0.0
                    self.begin()
                    self.drawn = 0
                else:
                    played_at = last_playback_time()
                    if played_at is not None and self.arrived_at:
                        # Событие случилось после того, как пришёл файл, —
                        # значит звук уже пошёл, можно начинать.
                        self.play_ready = played_at >= self.arrived_at - 0.5
                    if self.wait_since == 0.0:
                        self.wait_since = now
                    if self.play_ready or now - self.wait_since > 2.5:
                        self.play_ready = False
                        self.wait_since = 0.0
                        self.pending_start = 0.0
                        self.begin()
                        self.drawn = 0
        elif now > self.phrase_until:
            self.end_reply()
            if self.queue:
                # Между фрагментами озвучки у плагина пауза: без неё губы
                # работали в тишине и анимация казалась длиннее голоса.
                self.pending_start = now + 0.15
        # Остановка только по длине фразы. Проверка «плагин больше не
        # говорит» оказывалась слишком строгой: состояние в журнале во время
        # озвучки бывает listening, и фраза обрывалась через секунду —
        # картинка заканчивалась, а голос продолжал (поймано 21.10).

        if self.phrase is not None and now < self.phrase_started:
            return      # звук ещё не пошёл: держим прошлый кадр

        if self.phrase is not None:
            since = now - self.last_paint_at
            if since > 0.6 and now - self.freeze_log_at > 3.0:
                self.freeze_log_at = now
                clip = self.clips.clip
                note(f"зависание: {since:.1f} с без кадров, клип "
                     f"{clip[0].parent.name if clip else 'нет'}, "
                     f"позиция {self.clips.next_index}")
            frame = self.clips.take()
            if frame is not None:
                if self.fade_left > 0 and self.fade_src is not None:
                    try:
                        weight = 1.0 - (self.fade_left / 4.0)
                        frame = Image.blend(self.fade_src, frame, weight)
                    except Exception:  # noqa: BLE001
                        pass
                    self.fade_left -= 1
                self.paint(frame)
                self.drawn += 1
        else:
            if not self.visible:
                return
            if self.hold and self.queue:
                return      # держим последний кадр речи: стыка не видно
            if now - self.last_idle >= 1.0 / FPS:
                self.last_idle = now
                if self.idle_plain:
                    frame = self.idle.frames[0] if self.idle.frames else None
                else:
                    frame = self.idle.next_frame()
                if frame is not None:
                    if self.fade_left > 0 and self.fade_src is not None:
                        try:
                            weight = 1.0 - (self.fade_left / 4.0)
                            frame = Image.blend(self.fade_src, frame, weight)
                        except Exception:  # noqa: BLE001
                            pass
                        self.fade_left -= 1
                    self.paint(frame)

        if now - self.last_state_log > 20.0:
            self.last_state_log = now
            note(f"состояние: фраза={'да' if self.phrase is not None else 'нет'}, "
                 f"живых={self.drawn > 0}, очередь={len(self.queue)}, "
                 f"покой={'статичный' if self.idle_plain else 'живой'}")

    def run(self):
        self.root.mainloop()


def tk_window_open():
    """Есть ли уже на экране окно с лицом (заголовок «tk»)."""
    try:
        user32 = ctypes.windll.user32
        found = []

        def callback(hwnd, _param):
            try:
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
            except Exception:  # noqa: BLE001
                return True
            return True

        proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        user32.EnumWindows(proc_type(callback), 0)
        return bool(found)
    except Exception:  # noqa: BLE001
        return False

def main():
    # Под pythonw нет консоли: print падает на None и убивает окно.
    if sys.stdout is None or sys.stderr is None:
        stream = open(LIVE_LOG, "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = stream
        if sys.stderr is None:
            sys.stderr = stream
    # Одна копия окна на систему. Две копии писали в журнал по два раза и
    # показывали разные кадры — поймано 21.10 после перезагрузки.
    try:
        kernel = ctypes.windll.kernel32
        kernel.CreateMutexW(None, False, "Local\\dsh-avatar-window")
        if kernel.GetLastError() == 183:      # ERROR_ALREADY_EXISTS
            note("окно уже запущено — вторая копия выходит")
            sys.exit(3)
    except Exception:  # noqa: BLE001
        pass

    # Вторая страховка: мьютекс может не пережить цепочку «пусковой pythonw ->
    # рабочий pythonw», поэтому смотрим, нет ли уже окна на экране.
    if tk_window_open():
        note("окно уже на экране — вторая копия выходит")
        sys.exit(3)

    avatar = AvatarWindow()
    avatar.drawn = 0
    note(f"--- старт окна, pid {os.getpid()}")
    avatar.run()


if __name__ == "__main__":
    main()
