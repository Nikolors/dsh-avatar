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
import math
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

import speech_rhythm

WORK = Path(__file__).resolve().parent
LOG = Path(r"C:\Users\ipdnv\dsh-voice\conversation-debug.log")
W, H = 320, 374
FPS = 25.0
# Кадры клипов: 27.10 прогнали их через интерполяцию (ffmpeg minterpolate),
# поэтому в папках теперь вдвое больше кадров — 50 в секунду. Темп речи,
# звука и цикла покоя при этом остаётся 25.
CLIP_FPS = 50.0
TICK_MS = 12
START_LAG = 0.15      # задержка кадров от события «озвучка началась»
# Сколько держим последний кадр речи после реплики, прежде чем уйти в покой.
# Нужно, чтобы короткие паузы внутри ответа не давали мигание «покой-речь».
# Поднято с 0,6 до 1,2 с: между кусками озвучки плагин делает паузы, и окно
# успевало мигнуть покоем — это читалось как рывок (замер записи 26.10).
IDLE_GRACE = 1.2
# Сколько кадров смешиваем при входе в новую позу (6 кадров при 25 к/с).
FADE_FRAMES = 6
MAX_FADE = 10         # предел смешивания, если позы сильно разошлись
WRAP_FADE = 4         # кадров смешивания у стыка «конец клипа → начало» (50 к/с)
STRIP_ENTRY_BLEND = 0  # подмешивание не нужно: плёнка выбрана под текущую позу
PAUSE_BLEND = 4       # кадров смешивания при входе и выходе из паузы
# 30.10: во сколько раз медленнее двигается живой покой (3 — примерно 8 кадров
# в секунду). Слишком быстрое движение головы читалось как «пританцовывает».
# 31.10: новый покой из длинной тишины (250 кадров, 10 с) — при шаге 6 Николай
# сказал, что движение слишком медленное. Ставим натуральную скорость:
# цикл идёт вперёд-обратно, 20 с без шва.
IDLE_SLOW = 1
# 31.10: «цельный ролик на реплику» (идея Николая). Реплика играется одним
# клипом от первого кадра до последнего. ВКЛЮЧАЛИ и отключили: стык
# «покой → первый кадр ролика» вылез заметнее, чем при плавном входе
# (Николай: «видно, что это склейка»). Код оставлен, включается одним флагом.
FILM_MODE = True     # 31.10: ролики-фильмы с общим первым кадром
FILM_FADE = 8        # кадров смешивания при входе из покоя в новый ролик
CATCHUP_LIMIT = 2     # сколько кадров клип может догнать за одну отрисовку
END_TRIM = 0.08      # на сколько подрезаем конец фразы (почти ничего)

# Материал (31.10): «фильмы» — отдельные ролики Ditto с тишиной по краям.
# У них у всех один и тот же первый кадр (покой), поэтому вход в ролик и
# возврат в покой совпадают без смешивания.
FILMS_DIR = WORK / "_filmsD"
CLIPS_DIR = (FILMS_DIR if FILMS_DIR.exists()
             else (WORK / "_clipsD" if (WORK / "_clipsD").exists() else WORK / "_clips"))
# Покой — старый проверенный цикл. Вариант из клипов (_idle_new) проверяли
# 28.10: лицо в нём подвижнее, и покой читался как разговор. Не вернулись.
IDLE_DIR = (WORK / "_idleD") if (WORK / "_idleD").exists() else (WORK / "_idle")
QUEUE_FILE = WORK / "_phrases.txt"
SCORES_FILE = WORK / "_clip_scores.json"
MOUTH_FILE = ((WORK / "_clip_mouth_films.json") if FILMS_DIR.exists()
              else ((WORK / "_clip_mouth_D.json") if (WORK / "_clipsD").exists()
                    else (WORK / "_clip_mouth.json")))
TRANS_DIR = WORK / "_trans"               # переходы покой → клип (tools\build_transitions.py)
USE_TRANSITIONS = True                    # эксперимент: включаем переходы
TRANSITION_VARIANTS = 6                   # сколько плёнок перехода на клип
POSE_WEIGHT = 0.55                        # вес расхождения позы (0,9 дало хуже: замер 27.10)
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


def app_window_minimized():
    """Свёрнуто ли приложение (в трей или на панель задач).

    29.10: крестик сворачивает DSH в трей, приложение остаётся живым, и портрет
    висел поверх всех окон, хотя разговаривать было не с кем. Просто «окно не
    видно» проверять нельзя: когда браузер перекрывает приложение, оно тоже не
    на переднем плане, и раньше из-за этого портрет мигал (27.10). Отличаем так:
    свёрнутым считаем только случай, когда своего окна нет вовсе либо оно
    свёрнуто (IsIconic); перекрытое чужим окном приложение остаётся видимым.
    """
    try:
        user32 = ctypes.windll.user32
        alive = []

        def callback(hwnd, _param):
            try:
                length = user32.GetWindowTextLengthW(hwnd)
                if length <= 0:
                    return True
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                if buffer.value.endswith("DeepSeek Harness"):
                    if user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
                        alive.append(hwnd)
            except Exception:  # noqa: BLE001
                pass
            return True

        proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        user32.EnumWindows(proc_type(callback), 0)
        return not alive
    except Exception:  # noqa: BLE001
        return False


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


def last_barge_time():
    """Когда человек в последний раз перебивал ответ (по журналу плагина).

    Плагин пишет строку «barge-trigger» в момент, когда принял перебивание.
    Голос обрывается сразу, а клип в окне шёл до конца фразы — из-за этого
    анимация продолжала «говорить» после перебивания (замечено 24.10).
    Смотрим только свежий хвост журнала: строк столько же, сколько
    перебиваний, и они редкие.
    """
    best = None
    for line in plugin_log_tail(60000).splitlines():
        if '"message":"barge-trigger' not in line:
            continue
        try:
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
            continue
        # Новый формат журнала (27.10): состояние лежит отдельным полем.
        if '"plugin":"conversation"' in line and '"state":"' in line:
            value = line.split('"state":"', 1)[1].split('"', 1)[0]
            if value in ("listening", "recording", "thinking", "speaking", "waiting", "off"):
                best = value
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
            for folder in (sorted(CLIPS_DIR.glob("talk_*")) + sorted(CLIPS_DIR.glob("long_*"))
                           + sorted(CLIPS_DIR.glob("film_*"))):
                if folder.is_dir():
                    files = sorted(folder.glob("*.jpg"))
                    if files:
                        self.paths.append(files)
            # Длинные клипы (15–50 с), склеенные заранее с переходами RIFE:
            # ими закрывается целая реплика без смены клипа (28.10).
            long_dir = WORK / "_clipsLong"
            if long_dir.exists():
                for folder in sorted(long_dir.glob("long_*")):
                    if folder.is_dir():
                        files = sorted(folder.glob("*.jpg"))
                        if files:
                            self.paths.append(files)
        except OSError:
            self.paths = []
        # Оценки «насколько двигается рот» для каждого клипа (см. tools/score_clips.py)
        self.scores = {}
        try:
            if SCORES_FILE.exists():
                self.scores = json.loads(SCORES_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            self.scores = {}
        self.cache = collections.OrderedDict()
        self.rest_cache = {}     # клип -> кадр с закрытым ртом (для стыка)
        self._last_image = None  # последний отданный кадр: по нему ищем стык
        self._wrap_from = None   # кадр, на который перешли после конца клипа
        # Профиль рта: где в клипе рот открывается и закрывается (26.10).
        self.mouth = {}
        try:
            long_mouth = WORK / "_clip_mouth_long.json"
            if long_mouth.exists():
                self.mouth.update(json.loads(long_mouth.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            pass
        # Длинным клипам ставим заметную оценку подвижности: их собственная не
        # считалась, и они всегда проигрывали коротким (28.10).
        try:
            for path in self.paths:
                name = path[0].parent.name
                if name.startswith("long_"):
                    self.scores.setdefault(name, 2.0)
        except Exception:  # noqa: BLE001
            pass
        self.transition = []      # кадры перехода из покоя (RIFE)
        self._anchor_cache = None
        self.pose_cache = {}        # миниатюры первых кадров клипов
        self.trans_cache = {}
        self._wrap_blend = None   # с чего смешиваем переход к началу клипа
        self._pause_exit = None   # кадр, с которого плавно выходим из паузы
        self._pause_exit_left = 0
        self._blend_left = 0
        try:
            if MOUTH_FILE.exists():
                self.mouth = json.loads(MOUTH_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            self.mouth = {}
        self.pauses = []         # паузы текущей фразы (секунды от начала)
        self.pause_started = None  # когда началась текущая пауза речи
        self._retarget_at = None   # в какой паузе уже сменили клип
        self._switch_blend = 0     # кадров смешивания после смены клипа
        self._switch_from = None
        self.clip = None
        self.started = None
        self.offset = 0
        self.next_index = 0
        self.last_pick = None
        self.rate = 1.0          # во сколько раз ускоряем клип под длину фразы
        self.film_t0 = None      # 31.10: начало цельного ролика (ponятие «фильм»)
        self.film_rate = 1.0     # во сколько раз растянут клип под реплику
        self.film_last = None    # последний кадр ролика (он же покой)
        if self.paths:
            note(f"оценки клипов: {len(self.scores)} шт.")
            note(f"клипы речи: {len(self.paths)} шт., "
                 f"{sum(len(p) for p in self.paths)} кадров")

    def film_start(self, duration):
        """Цельный ролик на реплику: выбираем клип по длине и играем с кадра 0.

        Клип у нас нарезан по паузам, поэтому он сам содержит тишину в начале
        и в конце, то есть покой, речь и снова покой — как отдельный фильм.
        """
        if not self.paths:
            return False
        duration = max(0.8, float(duration or 0.0))
        best = None
        for index, frames in enumerate(self.paths):
            length = len(frames) / CLIP_FPS
            if length < 1.0:
                continue
            ratio = length / duration
            if ratio < 0.70 or ratio > 1.50:
                continue
            score = abs(ratio - 1.0)
            if best is None or score < best[0]:
                best = (score, index, ratio)
        if best is None:
            candidates = [(abs(len(f) / CLIP_FPS / duration - 1.0), i, len(f) / CLIP_FPS / duration)
                          for i, f in enumerate(self.paths) if len(f) > 24]
            if not candidates:
                return False
            best = min(candidates, key=lambda item: item[0])
        _, index, ratio = best
        clip = self.paths[index]
        self.clip = clip
        self.offset = 0
        self.next_index = 0
        self.rate = 1.0
        self.film_t0 = None
        self.film_rate = max(0.8, min(1.5, ratio))  # 31.10: не мелькать и не тянуть
        self.film_last = None
        self.last_pick = clip
        return True

    def film_take(self):
        """Кадр ролика по часам: ровно от первого до последнего, без склеек."""
        if not self.clip:
            return None
        now = time.monotonic()
        if self.film_t0 is None:
            self.film_t0 = now
        elapsed = now - self.film_t0
        index = int(elapsed * CLIP_FPS * self.film_rate)
        index = max(0, min(len(self.clip) - 1, index))
        self.next_index = index
        frame = self._frame(self.clip, index)
        if frame is not None:
            self.film_last = frame
        return frame

    def film_left(self):
        """Сколько секунд ролика ещё осталось (31.10)."""
        if not self.clip or getattr(self, "film_t0", None) is None:
            return 0.0
        used = (time.monotonic() - self.film_t0) * max(0.2, self.film_rate)
        return max(0.0, len(self.clip) / CLIP_FPS - used)

    def available(self):
        return bool(self.paths)

    def mouth_info(self, name):
        """Профиль рта клипа: где закрыт, где первое открытие, какой темп.

        Пороги считаем от самого профиля, а не абсолютные: у разных клипов
        разная яркость и контраст, и фиксированные 0,55 ловили то всё, то
        ничего (замер 27.10: все фразы выбирали один и тот же клип).
        """
        data = self.mouth.get(name)
        if not data:
            return None
        profile = data.get("profile") or []
        frames = len(profile) or data.get("frames") or 0
        if not frames:
            return None
        # сглаживаем: одиночные всплески от сжатия не считаем открытием
        smooth = []
        for index in range(frames):
            left = profile[max(0, index - 1)]
            right = profile[min(frames - 1, index + 1)]
            smooth.append((left + profile[index] + right) / 3.0)
        low = min(smooth)
        high = max(smooth)
        span_value = max(0.05, high - low)
        open_thr = low + span_value * 0.62
        close_thr = low + span_value * 0.32
        closed = int(data.get("closed") or 0)
        first_open = None
        inside = False
        openings = 0
        last_open_at = -99.0
        for index, value in enumerate(smooth):
            moment = index / CLIP_FPS
            if not inside and value >= open_thr and (moment - last_open_at) >= 0.15:
                inside = True
                openings += 1
                last_open_at = moment
                if first_open is None and index >= 3:
                    first_open = index
            elif inside and value <= close_thr:
                inside = False
        span = max(0.2, frames / CLIP_FPS)
        return {
            "closed": min(frames - 1, max(0, closed)),
            "first_open": first_open if first_open is not None else min(frames - 1, 3),
            "rate": openings / span,
            "json_first": int(data.get("first_open") or 0),
            "frames": frames,
        }

    def start(self, duration, current=None, rhythm=None, open_at=None):
        if not self.paths:
            return
        want = float(duration or 0.0)
        lengths = [len(clip) / CLIP_FPS for clip in self.paths]
        # Длинные клипы поднимаем в списке: иначе первые двадцать мест занимают
        # короткие, и до длинного дело не доходит (28.10).
        def length_rank(i):
            diff = abs(lengths[i] - want)
            if want <= 12.0 and lengths[i] >= 15.0:
                diff *= 0.35
            return diff
        order = sorted(range(len(self.paths)), key=length_rank)
        order = [i for i in order if i != self.last_pick] or order
        # Среди близких по длине предпочитаем те, где заметно работает рот.
        # Кандидаты: клипы не короче нужного (играем всё равно только длину
        # фразы) — среди них есть заметно более подвижные.
        # Главное — чтобы фраза уложилась в клип целиком: тогда он ни разу не
        # начнётся заново посреди речи, а именно этот переход и был заметен
        # (замер 27.10: 9 из 11 резких смен). Если такого клипа нет — берём
        # самый длинный из подходящих.
        # Считаем по остатку клипа после входного кадра: фраза должна
        # уложиться целиком, иначе клип начнётся заново посреди речи.
        def remaining(i):
            info = self.mouth_info(self.paths[i][0].parent.name) or {}
            entry = int(info.get("json_first") or 0)
            return max(0.3, (len(self.paths[i]) - entry) / CLIP_FPS)
        fits = [i for i in order if remaining(i) >= want * 1.02]
        fits_slow = [i for i in order if remaining(i) >= want * 0.80]
        longer = [i for i in order if lengths[i] >= want * 0.9]
        close = (fits or fits_slow or longer or order)[:20]
        want_rate = float((rhythm or {}).get("rate") or 0.0)

        # Непрерывный дубль (Ditto): весь материал — один ролик, и покой взят из
        # его начальной тишины. Значит реплику начинаем ровно с того кадра, что
        # идёт после покоя, и играем подряд — стыка не остаётся вовсе.
        whole = None
        for index, clip in enumerate(self.paths):
            if clip[0].parent.name == "long_61":
                whole = index
                break
        if whole is not None and want <= 55.0:
            self.clip = self.paths[whole]
            # 30.10: дубль теперь начинается прямо с речи — ведущую тишину срезали
            # при нарезке. Поэтому реплика стартует с кадра 0, а не с кадра покоя:
            # иначе окно показывало бы десять секунд молчания перед первой фразой.
            # Плавность входа даёт обычное подмешивание к последнему кадру покоя.
            self.offset = 0
            self.match_score = None
            self.pauses = []
            self.transition = []
            self.started = open_at if open_at is not None else time.monotonic()
            self.next_index = self.offset
            self.rate = 1.0
            self.want_total = want
            self._lengths = lengths
            self.recent = (getattr(self, "recent", []) + [whole])[-6:]
            self.last_pick = whole
            self.cache.clear()
            note("непрерывный дубль: играем с кадра %d" % self.offset)
            return

        # Главный путь: длинный клип на всю реплику. Фразы приходят по одной, и
        # будущую длину реплики мы не знаем, поэтому берём клип с запасом —
        # тогда внутри реплики не будет ни одной смены (28.10, замысел).
        long_pool = []
        for index, clip in enumerate(self.paths):
            name = clip[0].parent.name
            if not name.startswith("long_"):
                continue
            seconds = lengths[index]
            if seconds >= 20.0 and seconds >= want * 1.15:
                long_pool.append(index)
        if long_pool:
            recent_long = [i for i in long_pool if i not in getattr(self, "recent", [])]
            pool = recent_long or long_pool
            pool.sort(key=lambda i: abs(lengths[i] - max(25.0, want * 1.4)))
            pick_long = random.choice(pool[:3])
            name = self.paths[pick_long][0].parent.name
            self.clip = self.paths[pick_long]
            # Поиск входа по позе на длинных клипах дал всплеск (замер 28.10:
            # максимум 65), поэтому входим с нулевого кадра.
            self.offset = 0
            self.match_score = None
            self.pauses = list((rhythm or {}).get("pauses") or [])
            info = self.mouth_info(name) or {}
            self.transition = self.transition_frames(name)
            self.started = open_at if open_at is not None else time.monotonic()
            self.next_index = self.offset
            self.rate = min(1.15, max(0.9, want / max(0.5, lengths[pick_long])))
            self.want_total = want
            self._lengths = lengths
            self.recent = (getattr(self, "recent", []) + [pick_long])[-6:]
            self.last_pick = pick_long
            self.cache.clear()
            note("длинный клип на реплику: %s (%.0f с)" % (name, lengths[pick_long]))
            return

        # Выразительность важна, но клип не должен сильно отличаться по длине:
        # иначе скорость уезжает от естественной и губы «плывут». С 26.10
        # учитываем и темп рта: клип, где рот двигается в похожем ритме,
        # читается как настоящая речь.
        # Поза, в которой окно стоит сейчас: по ней же подбираем, с какого клипа
        # начинать, чтобы вход в реплику не давал скачка (замер 27.10).
        pose_target = None
        if current is not None:
            try:
                small = current.convert("L").resize((24, 24), Image.BILINEAR)
                pose_target = list(small.getdata())
            except Exception:  # noqa: BLE001
                pose_target = None

        def rank(i):
            name = self.paths[i][0].parent.name
            speed = want / max(0.1, lengths[i])
            # У длинных клипов нет своей оценки «подвижности рта»: считаем её
            # как медианную, иначе они всегда проигрывают коротким (28.10).
            baseline = getattr(self, "_score_baseline", None)
            if baseline is None:
                values = sorted(self.scores.values()) or [0.0]
                baseline = values[len(values) // 2] if values else 0.0
                self._score_baseline = baseline
            score = self.scores.get(name, baseline) - 0.5 * abs(speed - 1.0)
            # Длинный клип на короткую фразу: он накроет всю реплику, и менять
            # клип посреди речи не придётся (28.10). Небольшая надбавка, чтобы
            # длинные куски не вытесняли короткие полностью.
            if want <= 12.0 and lengths[i] >= 15.0:
                score += 0.35
            info = self.mouth_info(name)
            if info and want_rate:
                # Темп «открытий» в профиле клипа примерно вдвое реже слогов
                # (видно широкие открытия, а не каждую артикуляцию), поэтому
                # сравниваем с половиной слогового темпа и с учётом скорости.
                target = want_rate * 0.45
                score -= 0.35 * abs(info["rate"] * min(1.15, max(0.85, speed)) - target)
            if pose_target is not None:
                thumb = self.entry_thumb(name)
                if thumb is not None:
                    diff = sum(abs(a - b) for a, b in zip(pose_target, thumb)) / 576.0
                    score -= POSE_WEIGHT * diff
            return score

        close.sort(key=lambda i: -rank(i))
        best = close[0]
        # Разнообразие. Раньше, если предыдущий клип был почти так же
        # выразителен, он же и оставался — из-за этого две трети реплик
        # закрывали talk_60 и talk_11 (замер 26.10). Теперь берём одного из
        # пяти лучших, не повторяя последние шесть.
        top = close[:5]
        recent = getattr(self, "recent", [])
        fresh = [i for i in top if i not in recent] or top
        pick = random.choice(fresh)
        recent.append(pick)
        del recent[:-6]
        self.recent = recent
        self.last_pick = pick
        self.clip = self.paths[pick]
        self.offset = 0
        # Совпадение позы для стыка: при выравнивании по ритму мы его не
        # считаем, поэтому старое значение могло отключить смешивание и дать
        # резкий «щелчок» при входе в клип (замер 27.10: скачок x28).
        self.match_score = None
        # Паузы фразы: в них рот держим закрытым, чтобы не «жевать» в тишине.
        self.pauses = list((rhythm or {}).get("pauses") or [])
        info = self.mouth_info(self.clip[0].parent.name)
        self.transition = self.transition_frames(self.clip[0].parent.name)
        if info and rhythm:
            if self.transition:
                # С переходом начинаем ровно с кадра первого открытия рта:
                # до него идёт дорисованный переход из покоя (см. окно).
                self.offset = max(0, min(len(self.clip) - 2, int(info.get("json_first") or 0)))
            else:
                # Без перехода выравниваем первое открытие рта под первый слог.
                lead = info["first_open"] - int(round(float(rhythm.get("first") or 0.0) * CLIP_FPS))
                self.offset = max(0, min(len(self.clip) - 2, lead))
        elif current is not None:
            self.offset = self._closest_index(self.clip, current)
            # Чуть вперёд от найденного кадра: в первых кадрах клипа рот ещё
            # закрыт, и начало речи выглядело задержанным.
            self.offset = min(len(self.clip) - 1, self.offset + 3)
        self.started = open_at if open_at is not None else time.monotonic()
        self.next_index = self.offset
        clip_seconds = max(0.3, (len(self.clip) - self.offset) / CLIP_FPS)
        if want > 0:
            # Темп держим близким к естественному. При 0,55 губы отставали от
            # речи вдвое (замечание 26.10). Если фраза длиннее клипа, клип
            # доходит до конца и начинается снова — это незаметнее, чем
            # замедление рта.
            # Нижний предел замедления опущен до 0,78: лучше чуть медленнее, чем
            # начинать клип заново посреди фразы (замер 27.10).
            self.rate = min(1.15, max(0.78, want / clip_seconds))
        else:
            self.rate = 1.0
        # Длинная фраза целиком в один клип не влезает: держим параметры,
        # чтобы в паузе между словами перейти на следующий клип незаметно.
        self.want_total = want
        self._lengths = lengths
        self.cache.clear()
        self._wrap_from = None      # новый клип — стык считаем заново
        threading.Thread(target=self._warm, args=(self.clip,), daemon=True).start()

    def ensure(self, duration, current=None, rhythm=None):
        if self.clip is None:
            self.start(duration, current, rhythm)

    def _closest_index(self, clip, image, limit=None):
        """Кадр, больше всего похожий на текущую картинку.

        `limit` ограничивает поиск первой половиной клипа: при стыке нельзя
        выбирать кадр рядом с текущим, иначе окно «находит» само себя и
        замирает на последнем кадре (найдено 26.10 на записи окна).
        """
        try:
            small = image.convert("L").resize((24, 24), Image.BILINEAR)
            target = list(small.getdata())
            best, best_score = 0, None
            top = min(len(clip), limit) if limit else len(clip)
            step = max(1, top // 30)
            for index in range(0, max(1, top), step):
                try:
                    frame = Image.open(clip[index]).convert("L").resize((24, 24), Image.BILINEAR)
                except Exception:  # noqa: BLE001
                    continue
                score = sum(abs(a - b) for a, b in zip(target, list(frame.getdata())))
                if best_score is None or score < best_score:
                    best, best_score = index, score
            # Средняя разница на точку (0 — кадры совпали). Нужна, чтобы решать,
            # надо ли вообще смешивать стык: при хорошем совпадении смешивание
            # только даёт «двоение» двух поз (замечание 24.10).
            self.match_score = (best_score or 0) / 576.0
            return best
        except Exception:  # noqa: BLE001
            self.match_score = 0.0
            return 0

    def entry_thumb(self, name):
        """Миниатюра первого кадра клипа: по ней сравниваем позы."""
        key = str(name)
        if key in self.pose_cache:
            return self.pose_cache[key]
        thumb = None
        try:
            clip = next((c for c in self.paths if c[0].parent.name == key), None)
            if clip:
                info = self.mouth_info(key) or {}
                index = int(info.get("json_first") or 0)
                index = max(0, min(len(clip) - 1, index))
                image = self._frame(clip, index)
                if image is not None:
                    thumb = list(image.convert("L").resize((24, 24), Image.BILINEAR).getdata())
        except Exception:  # noqa: BLE001
            thumb = None
        self.pose_cache[key] = thumb
        return thumb

    def transition_variants(self, name):
        """Переходы от разных кадров покоя: {номер: [кадры]} (27.10)."""
        key = str(name)
        cached = self.trans_cache.get(key)
        if cached is not None:
            return cached
        variants = {}
        folder = TRANS_DIR / key
        try:
            if folder.is_dir():
                for sub in sorted(folder.glob("a*")):
                    if not sub.is_dir():
                        continue
                    frames = sorted(sub.glob("*.jpg")) + sorted(sub.glob("*.png"))
                    if frames:
                        try:
                            variants[int(sub.name[1:])] = frames
                        except ValueError:
                            continue
                if not variants:
                    frames = sorted(folder.glob("*.jpg")) + sorted(folder.glob("*.png"))
                    if frames:
                        variants[0] = frames
        except OSError:
            variants = {}
        self.trans_cache[key] = variants
        return variants

    def transition_frames(self, name):
        """Кадры перехода «покой → начало рта», просчитанные RIFE (27.10)."""
        if not USE_TRANSITIONS:
            return []
        variants = self.transition_variants(name)
        if not variants:
            return []
        return variants[min(variants)]

    def anchor_thumbs(self):
        """Миниатюры шести кадров покоя — по одному на каждый вариант перехода.

        Считаем один раз: раньше на каждую реплику открывались первые кадры всех
        шести плёнок, окно замирало на 0,6 с, и это читалось как рывок.
        """
        if self._anchor_cache is not None:
            return self._anchor_cache
        thumbs = {}
        try:
            frames = sorted(IDLE_DIR.glob("idle_*.jpg"))
            if frames:
                step = max(1, len(frames) // TRANSITION_VARIANTS)
                for index in range(1, TRANSITION_VARIANTS + 1):
                    path = frames[min(len(frames) - 1, (index - 1) * step)]
                    try:
                        image = Image.open(path).convert("L").resize((24, 24), Image.BILINEAR)
                        thumbs[index] = list(image.getdata())
                    except Exception:  # noqa: BLE001
                        continue
        except OSError:
            thumbs = {}
        self._anchor_cache = thumbs
        return thumbs

    def pick_transition(self, name, image):
        """Ближайший по позе переход: сравниваем с готовыми миниатюрами."""
        if not USE_TRANSITIONS:
            return []
        variants = self.transition_variants(name)
        if not variants:
            return []
        if image is None:
            return variants[min(variants)]
        try:
            small = image.convert("L").resize((24, 24), Image.BILINEAR)
            target = list(small.getdata())
            thumbs = self.anchor_thumbs()
            best, best_score = min(variants), None
            for number, data in thumbs.items():
                if number not in variants:
                    continue
                score = sum(abs(a - b) for a, b in zip(target, data))
                if best_score is None or score < best_score:
                    best, best_score = number, score
            return variants[best]
        except Exception:  # noqa: BLE001
            return variants[min(variants)]

    def _closest_mouth(self, clip, image, limit=None):
        """Кадр, у которого рот в такой же фазе, как сейчас.

        Позу головы сравнивать не нужно: камера неподвижна, голова почти не
        смещается. А вот скачок формы рта заметен — именно его Николай видит
        как «стык» (замер 27.10: голова 0,8 px, а рот меняется одним кадром).
        """
        try:
            box = (0.28, 0.58, 0.72, 0.84)
            w, h = image.size
            crop = image.crop((int(w * box[0]), int(h * box[1]),
                               int(w * box[2]), int(h * box[3]))).convert("L").resize((32, 16), Image.BILINEAR)
            target = list(crop.getdata())
            top = min(len(clip), limit) if limit else len(clip)
            step = max(1, top // 40)
            best, best_score = 0, None
            for index in range(0, max(1, top), step):
                frame = self._frame(clip, index)
                if frame is None:
                    continue
                small = frame.crop((int(w * box[0]), int(h * box[1]),
                                    int(w * box[2]), int(h * box[3]))).convert("L").resize((32, 16), Image.BILINEAR)
                score = sum(abs(a - b) for a, b in zip(target, list(small.getdata())))
                if best_score is None or score < best_score:
                    best, best_score = index, score
            return best
        except Exception:  # noqa: BLE001
            return 0

    def _closest_transition(self, clip, image, limit=None):
        """Кадр для возврата к началу клипа: похож и позой, и ртом.

        27.10: выбор только по рту давал заметный скачок головы (поза уезжала),
        выбор только по позе — скачок формы губ. Считаем обе половины.
        """
        try:
            w, h = image.size
            full = image.convert("L").resize((24, 24), Image.BILINEAR)
            full_target = list(full.getdata())
            mouth_box = (0.28, 0.58, 0.72, 0.84)
            mouth = image.crop((int(w * mouth_box[0]), int(h * mouth_box[1]),
                                int(w * mouth_box[2]), int(h * mouth_box[3]))).convert("L").resize((32, 16), Image.BILINEAR)
            mouth_target = list(mouth.getdata())
            top = min(len(clip), limit) if limit else len(clip)
            step = max(1, top // 40)
            best, best_score = 0, None
            for index in range(0, max(1, top), step):
                frame = self._frame(clip, index)
                if frame is None:
                    continue
                small = frame.convert("L").resize((24, 24), Image.BILINEAR)
                pose = sum(abs(a - b) for a, b in zip(full_target, list(small.getdata())))
                piece = frame.crop((int(w * mouth_box[0]), int(h * mouth_box[1]),
                                    int(w * mouth_box[2]), int(h * mouth_box[3]))).convert("L").resize((32, 16), Image.BILINEAR)
                lips = sum(abs(a - b) for a, b in zip(mouth_target, list(piece.getdata())))
                score = pose * 0.6 + lips * 0.4
                if best_score is None or score < best_score:
                    best, best_score = index, score
            return best
        except Exception:  # noqa: BLE001
            return 0

    def preload(self, clip, start, count):
        """Прогреваем первые кадры клипа заранее, чтобы стык не ждал диск."""
        for index in range(max(0, start), min(len(clip), start + count)):
            self._frame(clip, index)

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
        length = len(self.clip)
        # Продолжаем с того кадра, на котором остановились: между фразами окно
        # держит последний кадр речи, поэтому продолжение бесшовное. Поиск
        # «похожей позы» по всему клипу возвращал начало и давал рывок.
        resume_at = int(getattr(self, "next_index", 0) or 0)
        if resume_at >= length - 6 and current is not None:
            resume_at = min(length - 1, self._closest_index(self.clip, current) + 3)
        self.offset = max(0, min(length - 1, resume_at))
        self.started = time.monotonic()
        self.next_index = self.offset
        # Часы клипа между фразами больше не останавливаются (окно продолжает
        # показывать клип), поэтому сдвигать их на длительность паузы нельзя —
        # от этого позиция уходила в минус и кадры пропадали (28.10, 01:02).

    def _retarget(self, want, resume_at=0.0):
        """Меняем клип под остаток фразы. Вызывается только в паузе речи."""
        if not self.paths or want <= 0.5:
            return False
        lengths = getattr(self, "_lengths", None) or [len(c) / CLIP_FPS for c in self.paths]
        current_name = self.clip[0].parent.name if self.clip else ""

        def remaining(i):
            info = self.mouth_info(self.paths[i][0].parent.name) or {}
            entry = int(info.get("json_first") or 0)
            return max(0.3, (len(self.paths[i]) - entry) / CLIP_FPS)

        # Длинные клипы поднимаем в списке: иначе первые двадцать мест занимают
        # короткие, и до длинного дело не доходит (28.10).
        def length_rank(i):
            diff = abs(lengths[i] - want)
            if want <= 12.0 and lengths[i] >= 15.0:
                diff *= 0.35
            return diff
        order = sorted(range(len(self.paths)), key=length_rank)
        order = [i for i in order if self.paths[i][0].parent.name != current_name]
        ok = [i for i in order if remaining(i) >= want * 1.02]
        if not ok:
            # Остаток речи длиннее любого клипа: берём самый длинный и вернёмся
            # к смене в следующей паузе. Иначе на длинной фразе клип остаётся
            # один и лицо замирает (найдено 27.10).
            # Берём из самых длинных: короткий клип кончится раньше и потребует
            # ещё одной смены, а каждая смена — заметный стык (28.10).
            ok = sorted(order, key=lambda i: -remaining(i))[:6]
            if not ok:
                return False
        top = ok[:5]
        recent = getattr(self, "recent", [])
        fresh = [i for i in top if i not in recent] or top
        pick = random.choice(fresh)
        recent.append(pick)
        del recent[:-6]
        self.recent = recent
        self.last_pick = pick
        self.clip = self.paths[pick]
        info = self.mouth_info(self.clip[0].parent.name) or {}
        self.offset = max(0, min(len(self.clip) - 2, int(info.get("json_first") or 0)))
        clip_seconds = max(0.3, (len(self.clip) - self.offset) / CLIP_FPS)
        # На длинном остатке не растягиваем клип: пусть темп будет естественным.
        self.rate = min(1.15, max(0.92, want / clip_seconds))
        now = time.monotonic()
        # После смены отсчёт идёт от момента возобновления речи, поэтому
        # расписание пауз пересчитываем на новую ось: иначе паузы срабатывают
        # не там и лицо часто замирает (найдено 27.10, 23:20).
        if resume_at > 0:
            rebased = []
            for start_at, end_at in self.pauses:
                if end_at > resume_at:
                    rebased.append([max(0.0, start_at - resume_at), max(0.0, end_at - resume_at)])
            self.pauses = rebased
        # Смена клипа смешивается с последним показанным кадром: без этого
        # новый клип входит рывком.
        self._switch_blend = PAUSE_BLEND
        self._switch_from = getattr(self, "_last_image", None)
        self.started = now
        self.pause_started = now
        self.next_index = self.offset
        self._pause_exit = None
        self._pause_exit_left = 0
        # Сколько стоит сама смена: если долго, зритель видит замирание.
        warm = time.monotonic()
        try:
            self.preload(self.clip, self.offset, 12)
        except Exception:  # noqa: BLE001
            pass
        note("подготовка нового клипа: %.2f с" % (time.monotonic() - warm))
        return True

    def take(self):
        if self.clip is None or self.started is None:
            return None
        # В паузах речи рот закрыт: показываем спокойный кадр вместо «жевания».
        if self.pauses:
            elapsed = time.monotonic() - self.started
            for start_at, end_at in self.pauses:
                if start_at <= elapsed <= end_at:
                    # Длинная фраза: в паузе меняем клип на тот, что покроет
                    # остаток речи. Смена идёт в тишине, где рта не видно.
                    remainder = max(0.0, getattr(self, "want_total", 0.0) - end_at)
                    # Клип кончается раньше этой фразы: меняем заранее, в паузе,
                    # а не в тот момент, когда он упрётся в конец (28.10: на
                    # стыке окно замирало на полсекунды).
                    # Считаем по текущей позиции, а не по входному кадру: иначе
                    # кажется, что клип кончается, хотя он ещё идёт (28.10).
                    left_now = max(0.3, (len(self.clip) - getattr(self, "next_index", self.offset)) / CLIP_FPS)
                    if left_now < 3.0:
                        if self._retarget(remainder, end_at):
                            self._last_switch_at = time.monotonic()
                            note("клип меняем заранее: осталось %.1f с, новый %s"
                                 % (remainder, self.clip[0].parent.name))
                            continue
                    if getattr(self, "_paused_logged", None) != start_at:
                        self._paused_logged = start_at
                        note("пауза в речи: %.2f-%.2f с, остаток %.1f с, в клипе %.1f с, пауз всего %d"
                             % (start_at, end_at, remainder,
                                max(0.3, (len(self.clip) - self.offset) / CLIP_FPS), len(self.pauses)))
                    # Смену по «остатку речи» убрали: на длинной фразе остаток
                    # всегда больше любого клипа, и клип менялся в каждой паузе —
                    # это и давало рывки и вид «зависло» (28.10, 10:46). Меняем
                    # только когда клип действительно кончается (условие выше).
                    # В паузе часы клипа стоим: иначе после тишины он прыгает
                    # вперёд и часто сразу за конец — это и был видимый
                    # «перемонтаж» (найдено 27.10).
                    if self.pause_started is None:
                        self.pause_started = time.monotonic()
                    info = self.mouth_info(self.clip[0].parent.name)
                    closed = info["closed"] if info else 0
                    pause_frame = self._frame(self.clip, min(len(self.clip) - 1, max(0, closed)))
                    # Вход в паузу смешиваем: мгновенный переход на кадр с
                    # закрытым ртом читался как щелчок (замер 27.10).
                    if pause_frame is not None and info and elapsed > start_at + 0.05:
                        # В паузе не стоп-кадр, а медленное покачивание соседних
                        # кадров с закрытым ртом: лицо не «умирает» (27.10).
                        base = int(info.get("closed") or 0)
                        # Медленное движение вместо стоп-кадра: в паузах при
                        # чтении цифр окно замирало и это было видно (28.10).
                        shift = int(round(5.0 * math.sin((elapsed - start_at) * 2.2)))
                        index = max(0, min(len(self.clip) - 1, base + shift))
                        pause_frame = self._frame(self.clip, index) or pause_frame
                    if pause_frame is not None and self._last_image is not None:
                        into = min(1.0, (elapsed - start_at) * CLIP_FPS / max(1, PAUSE_BLEND))
                        try:
                            blended = Image.blend(self._last_image, pause_frame, max(0.0, into))
                        except Exception:  # noqa: BLE001
                            blended = pause_frame
                        # Запоминаем, чем кончилась пауза: с этого кадра будем
                        # плавно возвращаться в речь.
                        self._pause_exit = pause_frame
                        self._pause_exit_left = PAUSE_BLEND
                        return blended
                    return pause_frame
        if self.pause_started is not None:
            # Пауза кончилась. Часы клипа при этом НЕ сдвигаем: они идут всё
            # время, а сдвиг уводил позицию в минус и кадры пропадали
            # (найдено 28.10, 01:05).
            self.pause_started = None
        # Страховка: клип вот-вот кончится, а речь ещё идёт — меняем клип
        # сейчас, не дожидаясь паузы. Короткий стык лучше полусекундного
        # замирания (28.10, 00:31).
        try:
            left_frames = len(self.clip) - self.next_index
            remaining = getattr(self, "want_total", 0.0) - (time.monotonic() - self.started)
            if left_frames < 40 and remaining > 1.0 and self._retarget_at != "end":
                if self._retarget(max(2.0, remaining), 0.0):
                    self._retarget_at = "end"
                    note("клип кончается — меняем сразу, осталось %.1f с" % remaining)
                    return self.take()
        except Exception:  # noqa: BLE001
            pass
        position = int((time.monotonic() - self.started) * CLIP_FPS * self.rate) + self.offset
        if position < self.next_index:
            if self.next_index - position > 50:
                self.started = time.monotonic()
                self.next_index = self.offset
                position = self.offset
            else:
                self._none_streak = getattr(self, "_none_streak", 0) + 1
                if self._none_streak == 12:
                    note("кадров нет: позиция %s, ждём %s, темп %.2f, пауз %d, пауза_идёт=%s, прошло %.2f с"
                         % (position, self.next_index, self.rate, len(self.pauses),
                            self.pause_started is not None, time.monotonic() - self.started))
                return None
        elif position - self.next_index > CATCHUP_LIMIT:
            # Окно не успевает за 25 к/с и отстало: догоняем по два кадра
            # за отрисовку, а не прыгаем сразу на десять. Иначе на слабом
            # месте виден рывок головы (замечание 24.10).
            position = self.next_index + CATCHUP_LIMIT
        self.next_index = position + 1
        length = len(self.clip)
        if length <= 1:
            return self._frame(self.clip, 0)
        # Идём только вперёд. Раньше клип доигрывался назад (пинг-понг) — рот
        # «перематывался»; мягкое подмешивание у стыка выглядело как замирание
        # (замечания 26.10). Теперь на конце клипа переходим в тот кадр, чья
        # поза ближе всего к текущей: перехода почти не видно, потому что
        # голова остаётся на месте, а меняется только фаза рта.
        first = max(0, min(self.offset, length - 1))
        phase1 = length - first
        pos_in = position - first
        if pos_in < phase1:
            index = first + pos_in
            self._wrap_from = None
        else:
            if self._wrap_from is None:
                # Ищем только в первой половине клипа: кадр рядом с текущим
                # не годится, иначе цикл вырождается в один кадр.
                limit = max(2, length // 2)
                # Сначала пробуем по профилю рта — это мгновенно и не трогает
                # диск; раньше поиск декодировал десятки кадров и окно
                # запиналось на 0,6 с (замер 27.10).
                current_index = min(length - 1, first + pos_in)
                # Поиск кадра на стыке стоил полсекунды и читался как
                # зависание. Берём кадр по профилю рта; если профиля нет —
                # текущий, без обращения к диску (28.10, 00:30).
                pick = self._wrap_by_profile(self.clip[0].parent.name, current_index, limit)
                if pick is None:
                    pick = min(length - 2, current_index)
                if pick is None:
                    pick = self._closest_mouth(self.clip, self._last_image, limit) if self._last_image is not None else 0
                self._wrap_from = pick
                self._wrap_from = max(0, min(self._wrap_from, length - 2))
                note(f"стык клипа: продолжаем с кадра {self._wrap_from} из {length}")
                # Переход к началу клипа — мягкое смешивание. Без него здесь
                # был честный скачок одним кадром: на записи 27.10 в 50 к/с он
                # виден как «рез» посреди фразы (Николай: «режет, чётко видно»).
                if self._last_image is not None:
                    self._wrap_blend = self._last_image
                    self._blend_left = WRAP_FADE
            phase2 = max(1, length - self._wrap_from)
            index = self._wrap_from + (pos_in - phase1) % phase2
        frame = self._frame(self.clip, min(length - 1, index))
        # Выход из паузы: возвращаемся в речь не щелчком, а коротким смешиванием
        # от кадра с закрытым ртом (замер 27.10: скачок x10 в середине фразы).
        if self._pause_exit_left > 0 and self._pause_exit is not None and frame is not None:
            self._pause_exit_left -= 1
            weight = 1.0 - (self._pause_exit_left / float(max(1, PAUSE_BLEND)))
            try:
                frame = Image.blend(self._pause_exit, frame, max(0.0, min(1.0, weight)))
            except Exception:  # noqa: BLE001
                pass
        # Смешивание сразу после смены клипа в паузе: новый клип входит не
        # рывком, а за четыре кадра (правка 27.10, 23:20).
        if getattr(self, "_switch_blend", 0) > 0 and getattr(self, "_switch_from", None) is not None and frame is not None:
            self._switch_blend -= 1
            weight = 1.0 - (self._switch_blend / float(max(1, PAUSE_BLEND)))
            try:
                frame = Image.blend(self._switch_from, frame, max(0.0, min(1.0, weight)))
            except Exception:  # noqa: BLE001
                pass
        # Короткое смешивание сразу после перехода к началу клипа.
        if self._blend_left > 0 and self._wrap_blend is not None and frame is not None:
            self._blend_left -= 1
            weight = 1.0 - (self._blend_left / float(max(1, WRAP_FADE)))
            try:
                frame = Image.blend(self._wrap_blend, frame, max(0.0, min(1.0, weight)))
            except Exception:  # noqa: BLE001
                pass
        self._last_image = frame
        return frame

    def _wrap_by_profile(self, name, index, limit):
        """Кадр для возврата к началу по профилю рта: без чтения картинок.

        Ищем в первой половине клипа кадр, у которого рот открыт так же, как
        в текущем кадре. Профиль уже посчитан заранее, поэтому это мгновенно.
        """
        data = self.mouth.get(name)
        if not data:
            return None
        profile = data.get("profile") or []
        if len(profile) < 4:
            return None
        index = max(0, min(len(profile) - 1, index))
        target = profile[index]
        best, best_delta = None, None
        for candidate in range(0, max(2, min(limit, len(profile)))):
            delta = abs(profile[candidate] - target)
            if best_delta is None or delta < best_delta:
                best, best_delta = candidate, delta
        return best

    def _rest_index(self, clip):
        """Кадр, где рот закрыт: на него переходим, когда клип закончился.

        Похожесть считаем по нижней части кадра (губы) — самый спокойный кадр
        обычно ближе всего к первому. Результат запоминаем по клипу.
        """
        key = id(clip)
        if key in self.rest_cache:
            return self.rest_cache[key]
        best, best_score = 0, None
        try:
            import PIL.Image as _Image
            base = self._frame(clip, 0)
            if base is None:
                return 0
            w, h = base.size
            box = (int(w * 0.25), int(h * 0.55), int(w * 0.75), int(h * 0.85))
            target = base.crop(box).convert("L").resize((32, 16), _Image.BILINEAR)
            target_data = list(target.getdata())
            step = max(1, len(clip) // 60)
            for index in range(0, len(clip), step):
                frame = self._frame(clip, index)
                if frame is None:
                    continue
                small = frame.crop(box).convert("L").resize((32, 16), _Image.BILINEAR)
                score = sum(abs(a - b) for a, b in zip(target_data, list(small.getdata())))
                if best_score is None or score < best_score:
                    best, best_score = index, score
        except Exception:  # noqa: BLE001
            best = 0
        self.rest_cache[key] = best
        return best

    def stop(self):
        self.clip = None
        self.started = None
        self.held_at = None
        self._wrap_from = None
        self._last_image = None


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
        # 30.10: живой покой с настоящими кадрами пошёл слишком бодро — голова
        # качалась «в такт», как будто пританцовывает. Двигаем цикл втрое
        # медленнее: кадры сменяются примерно восемь раз в секунду вместо
        # двадцати пяти, движение читается как дыхание, а не как ритм.
        self.tick = getattr(self, "tick", 0) + 1
        if self.tick % IDLE_SLOW:
            return image
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
        # Ритм речи: слоги, первый слог и паузы — по звуку этой фразы (26.10).
        try:
            self.rhythm = speech_rhythm.analyse(str(self.wav))
        except Exception:  # noqa: BLE001
            self.rhythm = {}


# ------------------------------------------------------------------------- окно

class AvatarWindow:
    def __init__(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", "#0b0d12")
        x, y = self.saved_position()
        self.root.geometry(f"{W}x{H}+{x}+{y}")
        self.ensure_on_screen()
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
        self.film_idle = None      # 31.10: покой = последний кадр ролика
        self.idle_after = 0.0      # когда можно вернуться в покой
        self.alpha = 1.0           # текущая прозрачность окна
        self.alpha_target = 1.0    # к чему стремимся
        self.fade_src = None       # с чего начинаем плавный вход в клип
        self.fade_left = 0         # сколько кадров ещё смешивать
        self.fade_total = FADE_FRAMES  # сколько всего кадров в текущем стыке
        self.reply_started = False
        self.voice_state_now = None

        self.clip_mode = clip_source() == "clips"
        self.clips = ClipFeed()
        # 31.10: покой = первый кадр ролика (у всех роликов он одинаковый).
        if FILM_MODE and self.clips.paths:
            try:
                self.film_idle = Image.open(self.clips.paths[0][0]).convert("RGB")
            except Exception:  # noqa: BLE001
                self.film_idle = None
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

    def ensure_on_screen(self):
        """Если окно целиком за краем экрана — возвращаем его в видимую часть."""
        try:
            user32 = ctypes.windll.user32
            vx = user32.GetSystemMetrics(76)          # SM_XVIRTUALSCREEN
            vy = user32.GetSystemMetrics(77)          # SM_YVIRTUALSCREEN
            vw = user32.GetSystemMetrics(78)          # SM_CXVIRTUALSCREEN
            vh = user32.GetSystemMetrics(79)          # SM_CYVIRTUALSCREEN
            x = self.root.winfo_x()
            y = self.root.winfo_y()
            visible_x = max(x, vx) < min(x + W, vx + vw)
            visible_y = max(y, vy) < min(y + H, vy + vh)
            # немного видно — считаем, что окно на месте
            if visible_x and visible_y:
                return
            note(f"окно было за экраном ({x},{y}) — возвращаю на место")
            self.root.geometry(f"{W}x{H}+{vx + 100}+{vy + 200}")
            self.save_position()
        except Exception:  # noqa: BLE001
            pass

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

    def _pose_diff(self, first, second):
        """Насколько сильно различаются две позы (0 — одинаковые)."""
        try:
            a = first.convert("L").resize((32, 32), Image.BILINEAR)
            b = second.convert("L").resize((32, 32), Image.BILINEAR)
            total = sum(abs(x - y) for x, y in zip(a.getdata(), b.getdata()))
            return total / 1024.0
        except Exception:  # noqa: BLE001
            return 0.0

    def blend_into(self, frame):
        """Смешиваем новый кадр с прежним, растягивая стык при сильной смене позы.

        Первый кадр стыка раньше был полностью старым: около 0,2 с картинка
        стояла, а потом прыгала в новую позу. Теперь смешивание начинается
        сразу, а если позы очень разные — занимает больше кадров.
        """
        if self.fade_left <= 0 or self.fade_src is None or frame is None:
            return frame
        if self.fade_left == self.fade_total:
            diff = self._pose_diff(self.fade_src, frame)
            if diff > 6.0:
                extra = min(MAX_FADE - FADE_FRAMES, int(diff / 4.0))
                self.fade_total = FADE_FRAMES + max(0, extra)
                self.fade_left = self.fade_total
                note(f"стык поз: расхождение {diff:.0f}, смешиваем {self.fade_total} кадров")
        try:
            linear = 1.0 - (self.fade_left - 1) / float(max(1, self.fade_total))
            # Плавный разгон и торможение: линейное смешивание в конце
            # читается как рывок (замечание 24.10).
            weight = linear * linear * (3 - 2 * linear)
            frame = Image.blend(self.fade_src, frame, weight)
        except Exception:  # noqa: BLE001
            pass
        self.fade_left -= 1
        return frame

    def _breathe(self, image):
        """Микродвижение кадра: субпиксельный сдвиг по синусу.

        Даже когда клип стоит на месте закрытого рта, картинка чуть дышит, и
        полностью статичного кадра не бывает (28.10, жалоба на замирания).
        """
        if image is None:
            return image
        try:
            now = time.monotonic()
            # Дыхание должно быть еле заметным: 0,35 пикселя и медленнее.
            dx = 0.35 * math.sin(now * 1.1)
            dy = 0.28 * math.sin(now * 1.5 + 1.1)
            return image.transform(image.size, Image.AFFINE, (1, 0, dx, 0, 1, dy),
                                   resample=Image.BILINEAR)
        except Exception:  # noqa: BLE001
            return image

    def paint(self, image, status=None):
        if image is None:
            return
        gap = time.monotonic() - getattr(self, "last_paint_at", 0.0)
        if gap > 0.4:
            note("пауза в отрисовке %.2f с: фраза=%s, видно=%s, полоса=%d, очередь=%d, клип=%s"
                 % (gap, self.phrase is not None, self.visible,
                    len(getattr(self, "strip", []) or []), len(getattr(self, "queue", []) or []),
                    (self.clips.clip[0].parent.name if self.clips.clip else "нет")))
        image = self._breathe(image)
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

    def _film_anchor(self):
        """Общий покой равен первому кадру ролика (у всех роликов он одинаковый)."""
        clip = getattr(self.clips, "clip", None)
        if clip:
            try:
                return Image.open(clip[0]).convert("RGB")
            except Exception:  # noqa: BLE001
                pass
        return getattr(self, "film_idle", None) or self.last_painted

    def begin_film(self):
        """Цельный ролик на реплику (31.10): покой — речь — покой, без склеек."""
        phrase = self.phrase
        now = time.monotonic()
        duration = max(0.2, phrase.duration or 0.0)
        if (self.clips.clip is not None
                and getattr(self.clips, "film_t0", None) is not None
                and self.clips.film_left() < 0.8):
            # Ролик кончился, а речь продолжается: берём новый, иначе окно
            # держит последний кадр и выглядит как «анимация пропала».
            self.clips.clip = None
        if self.clips.clip is not None and getattr(self.clips, "film_t0", None) is not None:
            # Та же реплика, следующая фраза: ролик продолжается с того же места.
            self.fade_left = 0
            self.clip_open_at = now
            self.phrase_started = now
            self.phrase_until = now + max(0.2, duration - END_TRIM)
            self.hold = False
            self.reply_started = True
            self.show()
            return
        # Новая реплика: длина ролика — вся реплика целиком (текущая фраза + очередь).
        total = duration + sum((getattr(p, "duration", 0.0) or 0.0) for p in self.queue)
        # Вход без смешивания: у всех роликов один и тот же первый кадр (покой).
        self.fade_src = None
        self.fade_total = 0
        self.fade_left = 0
        self.strip = []
        self.strip_src = self.last_painted
        self.clip_open_at = now
        self.phrase_started = now
        self.phrase_until = now + max(0.2, duration - END_TRIM)
        self.hold = False
        self.reply_started = True
        self.idle_after = 0.0
        self.show()
        self.clips.film_start(total)
        clip = self.clips.clip
        note(f"фильм на реплику {total:.1f} с: клип "
             f"{clip[0].parent.name if clip else 'нет'}, "
             f"растяжение {self.clips.film_rate:.2f}")

    def begin(self):
        if not self.queue:
            self.phrase = None
            return
        self.phrase = self.queue.pop(0)
        if FILM_MODE:
            self.begin_film()
            return
        # Продолжение речи: клип уже показывается, перехода из покоя быть не
        # должно — плёнка кончается на входном кадре, а мы стоим в середине
        # клипа, и это давало рывок на каждой фразе (найдено 28.10, 00:12).
        continuing = bool(getattr(self, "hold", False) and self.clips.clip is not None)
        phrase = self.phrase
        entered = time.monotonic()
        # Переход от покоя к речи. Если для клипа есть дорисованный переход
        # (RIFE, 27.10) — показываем его; иначе короткое смешивание.
        self.fade_src = self.last_painted
        self.fade_total = FADE_FRAMES
        self.fade_left = FADE_FRAMES if self.fade_src is not None else 0
        self.strip = []
        self.strip_src = self.last_painted
        self.strip_start = 0.0
        self.clip_open_at = 0.0
        # Событие «озвучка началась» приходит чуть раньше самого звука,
        # поэтому кадры стартуют с небольшой задержкой, а конец почти не
        # подрезаем: иначе лицо заканчивало говорить раньше голоса.
        self.idle_after = 0.0
        self.phrase_started = time.monotonic() + START_LAG
        self.phrase_until = self.phrase_started + max(0.2, phrase.duration - END_TRIM)
        self.reply_started = True
        self.show()
        # Один клип на всю реплику: на каждой фразе новый клип давал заметный
        # скачок головы (замер 21.10: 8-10 медиан обычного движения).
        clip_len = (len(self.clips.clip) / CLIP_FPS) if self.clips.clip else 0.0
        too_short = bool(clip_len) and (phrase.duration or 0.0) > clip_len * 1.15
        rhythm = getattr(phrase, "rhythm", None) or {}
        # Когда рот должен открыться: начало звука плюс первый слог.
        # Раньше здесь ждали первый слог (0,3–0,5 с) и всё это время не рисовали
        # ни одного кадра — это и было «зависание 0,6 с» на каждой фразе.
        # Клип начинается с кадра первого открытия рта, поэтому ждать нечего
        # (28.10, 01:00).
        first_syllable = float(rhythm.get("first") or 0.0)
        open_at = self.phrase_started
        # Если от клипа осталось меньше трёх секунд, продолжаем уже нечем:
        # позиция упирается в конец и все следующие фразы показывают один и тот
        # же кадр — это и было «в конце всё зависло» (28.10, 10:43).
        if self.clips.clip is not None:
            left = (len(self.clips.clip) - getattr(self.clips, "next_index", 0)) / CLIP_FPS
            if left < 3.0:
                note("клип почти кончился (%.1f с) — берём новый" % left)
                self.clips.clip = None
        if self.clips.clip is None:
            self.clips.start(phrase.duration or 0.0, self.last_painted, rhythm, open_at=open_at)
        elif too_short:
            # Фраза заметно длиннее клипа: берём клип под её длину.
            was = clip_len
            self.hold = False
            self.clips.start(phrase.duration or 0.0, self.last_painted, rhythm, open_at=open_at)
            note(f"клип под фразу: было {was:.1f} с, нужно {phrase.duration:.1f} с")
        elif self.hold:
            self.hold = False
            self.clips.resume()      # продолжаем с того же места
        else:
            _t0 = time.monotonic()
            self.clips.resume(self.last_painted)
            note("продолжение клипа заняло %.2f с" % (time.monotonic() - _t0))
        # Качество совпадения позы с кадром клипа: если кадр почти тот же,
        # смешивать нечего — иначе на стыке видно две головы сразу.
        matched = getattr(self.clips, "match_score", None)
        if matched is not None:
            if matched < 2.5:
                # Раньше здесь смешивание отключалось совсем: считалось, что
                # кадры совпали и смешивать нечего. Но метрика сравнивает
                # уменьшенные картинки и не видит разницу в наклоне головы —
                # на записи 27.10 это дало резкий скачок х8 при входе в клип.
                # Держим короткое, но обязательное смешивание.
                self.fade_total = max(14, FADE_FRAMES // 2)
                self.fade_left = self.fade_total
            elif matched > 8.0:
                self.fade_total = min(MAX_FADE, FADE_FRAMES + int(matched / 3.0))
                self.fade_left = self.fade_total
                note(f"вход в клип: поза разошлась на {matched:.1f}, "
                     f"смешиваем {self.fade_total} кадров")
        # Дорисованный переход из покоя: показываем его перед речью, чтобы
        # рот открылся точно на первом слоге, а голова пришла плавно.
        # Переход выбираем по позе: плёнок несколько, от разных кадров покоя.
        strip = list(self.clips.pick_transition(
            self.clips.clip[0].parent.name if self.clips.clip else "",
            self.last_painted) or [])
        # Длинным клипам плёнки перехода пока не подходят: с ними вход стал
        # заметнее (замер 28.10: 10 смен против 5). Им оставляем смешивание.
        if self.clips.clip is not None and self.clips.clip[0].parent.name.startswith("long_"):
            strip = []
        if strip and self.clips.clip is not None and not continuing:
            # Грузим все кадры перехода сразу: если читать их по одному во
            # время показа, окно запинается и выдаёт кадр рывком (замер 27.10:
            # «зависание 0.6 с» и скачок x8,5 прямо на старте фразы).
            self.strip = self.load_strip(strip)
            self.strip_start = open_at - len(strip) / CLIP_FPS
            self.clip_open_at = open_at
            # Вход в плёнку тоже смешиваем: она начинается от кадра покоя, но
            # поза покоя в этот момент может отличаться, и стык виден (28.10).
            self.fade_total = 16
            self.fade_left = 16
            # Первые кадры клипа тоже заранее: стык не должен ждать диск.
            self.clips.preload(self.clips.clip, self.clips.offset, 10)
            note(f"переход из покоя: {len(strip)} кадров, старт {self.strip_start - time.monotonic():+.2f} с")
        else:
            self.strip = []
            self.clip_open_at = self.phrase_started
            if continuing:
                note("продолжаем клип без перехода из покоя, ждём до первого слога %.2f с"
                     % (self.clip_open_at - time.monotonic()))
        clip = self.clips.clip
        note(f"подготовка клипа {time.monotonic() - entered:.2f} с")
        note(f"фраза {phrase.wav.name}: клип "
             f"{clip[0].parent.name if clip else 'нет'}, вход {self.clips.offset}, "
             f"темп {self.clips.rate:.2f}, "
             f"длина {phrase.duration:.1f} с")
        rhythm = getattr(phrase, "rhythm", None) or {}
        if rhythm:
            note("ритм фразы: слогов {0}, темп {1:.1f}/с, первый слог {2:.2f} с, пауз {3}".format(
                rhythm.get("count", 0), rhythm.get("rate", 0.0),
                rhythm.get("first", 0.0), len(rhythm.get("pauses") or [])))
        self.status("Анастасия · говорит")

    def load_strip(self, paths):
        """Кадры перехода в память: двенадцать небольших картинок, грузим разом.

        Если читать их по одному во время показа, окно запинается и выдаёт
        кадр рывком (замер 27.10: «зависание 0.6 с» и скачок x8,5 на старте).
        """
        images = []
        for path in paths:
            try:
                images.append(Image.open(path).convert("RGB").copy())
            except Exception:  # noqa: BLE001
                continue
        return images

    def strip_frame(self, now):
        """Кадр перехода из покоя: до его начала держим прошлую картинку."""
        if not self.strip:
            return self.strip_src
        if now < self.strip_start:
            # До начала плёнки не держим один кадр, а продолжаем покачивание
            # покоя: иначе окно на полсекунды замирало (замер 27.10).
            frame = self.idle.next_frame() if self.idle.frames else None
            self.clips.last_idle_index = self.idle.index
            return frame if frame is not None else self.strip_src
        index = int((now - self.strip_start) * CLIP_FPS)
        index = max(0, min(len(self.strip) - 1, index))
        frame = self.strip[index]
        # Плёнка начинается с эталонного кадра покоя, а окно могло стоять на
        # другом — первые кадры перехода подмешиваем к текущему, иначе на входе
        # виден щелчок (замер 27.10).
        if index < STRIP_ENTRY_BLEND and self.strip_src is not None:
            weight = (index + 1) / float(STRIP_ENTRY_BLEND + 1)
            try:
                frame = Image.blend(self.strip_src, frame, weight)
            except Exception:  # noqa: BLE001
                pass
        return frame

    def status(self, text):
        if text != self.status_text:
            self.status_text = text
            self.canvas.itemconfigure(self.status_item, text=text)

    def end_reply(self):
        self.phrase = None
        remaining = bool(self.queue)
        self.hold = remaining
        if remaining:
            # Реплика продолжается: клип НЕ останавливаем. Раньше здесь держали
            # последний кадр, и в тишине лицо замирало — это и читалось как
            # «зависание» (28.10, 00:14). Пусть идёт: рот в паузе закрыт.
            note(f"кадров речи показано: {self.drawn}")
            self.drawn = 0
            self.wait_since = 0.0
            self.play_ready = False
            return
        self.clips.pause()
        note(f"кадров речи показано: {self.drawn}")
        self.drawn = 0
        # Не уходим в покой сразу: держим кадр ещё пару секунд, вдруг придёт
        # следующий кусок речи. Иначе на коротких фразах видно мигание.
        self.hold = True
        self.idle_after = time.monotonic() + IDLE_GRACE
        self.wait_since = 0.0
        self.play_ready = False

    def to_idle(self):
        """Плавный возврат в покой с кадра речи."""
        self.idle_after = 0.0
        self.hold = False
        self.reply_started = False
        # Реплика закончилась — забываем клип, чтобы следующая выбрала новый
        # под свой ритм. Иначе подряд идущие реплики продолжали один и тот же
        # клип (замер 27.10: три реплики подряд — talk_75).
        if FILM_MODE:
            # 31.10: конец ролика — его последний кадр, он же и есть покой.
            self.film_idle = self._film_anchor()
            self.clips.stop()
            self.status("Анастасия · онлайн")
            self.fade_src = self.last_painted
            self.fade_total = FILM_FADE if self.fade_src is not None else 0
            self.fade_left = self.fade_total
            return
        self.clips.stop()
        self.status("Анастасия · онлайн")
        # Стык с покоем: встаём на кадр цикла, ближайший к последнему кадру
        # речи. Иначе голова прыгает в другую позу — это и видно как
        # «ещё чуть-чуть анимации» после фразы (замечание 21.10).
        self.idle.set_like(self.last_painted)
        # Возврат в покой смешиваем заметно медленнее, чем стык фраз: поза покоя
        # отличается от позы речи, и на десяти кадрах (0,4 с) получался резкий
        # перескок (замечание 24.10). 30.10: растянули до двадцати кадров — это
        # 0,8 с, стык читается как естественное «лицо оседает», а не как склейка.
        self.fade_src = self.last_painted
        self.fade_total = 20
        self.fade_left = 20 if self.fade_src is not None else 0

    def interrupted(self):
        """Человек перебил: голос оборван, значит и анимация встаёт сразу."""
        if self.phrase is None:
            return
        self.phrase = None
        self.queue = []
        self.clips.pause()
        note(f"фраза прервана перебиванием, кадров речи показано: {self.drawn}")
        self.drawn = 0
        self.hold = False
        self.reply_started = False
        self.wait_since = 0.0
        self.play_ready = False
        self.to_idle()

    # --- основной такт ---------------------------------------------------

    def tick(self):
        started = time.monotonic()
        try:
            self.tick_once()
        except Exception as error:  # noqa: BLE001
            note(f"такт упал: {type(error).__name__} {error}")
        spent = time.monotonic() - started
        if spent > 0.25:
            note("долгий такт %.2f с: фраза=%s, полоса=%d, очередь=%d"
                 % (spent, self.phrase is not None, len(getattr(self, "strip", []) or []),
                    len(getattr(self, "queue", []) or [])))
        self.root.after(TICK_MS, self.tick)

    def tick_once(self):
        now = time.monotonic()
        _tick_started = now

        # Плавное проявление и уход окна (примерно 0,15 с).
        if abs(self.alpha - self.alpha_target) > 0.01:
            step = 0.08 if self.alpha_target > self.alpha else -0.08
            self.set_alpha(self.alpha + step)
            if self.alpha_target == 0.0 and self.alpha <= 0.01 and self.visible:
                self.root.withdraw()
                self.visible = False

        # Раз в пять секунд проверяем, что окно не уехало за край экрана.
        if now - getattr(self, "last_onscreen_check", 0.0) > 5.0:
            self.last_onscreen_check = now
            self.ensure_on_screen()

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
            # Раньше прятались, если окно приложения не видно (например, его
            # закрыл браузер). Из-за этого портрет исчезал и появлялся снова,
            # а появление читалось как резкий скачок анимации (замечание 27.10).
            # Теперь прячемся только если приложение действительно закрыто:
            # не отвечает его локальный порт. Выключение режима обрабатывается
            # отдельно выше.
            if self.visible and not app_port_alive():
                self.missed += 1
                if self.missed >= 2:
                    self.missed = 0
                    note("окно скрыто: приложение закрыто")
                    self.hide()
            elif self.visible and app_window_minimized():
                # Крестик сворачивает DSH в трей (29.10): порт жив, режим не
                # выключен, а портрет висел поверх всех окон. Прячем его, пока
                # приложение свёрнуто; развернёшь — покажется снова.
                self.minimized_missed = getattr(self, "minimized_missed", 0) + 1
                if self.minimized_missed >= 3:
                    self.minimized_missed = 0
                    note("окно скрыто: приложение свёрнуто в трей")
                    self.hide()
            else:
                self.missed = 0
                self.minimized_missed = 0

        self.read_phrases()

        # Перебивание: плагин принял его и оборвал голос, значит и клип
        # должен встать сразу, а не доигрывать фразу до конца. Проверяем
        # не чаще трёх раз в секунду: журнал читать часто незачем.
        if self.phrase is not None and now - getattr(self, "last_barge_check", 0.0) > 0.3:
            self.last_barge_check = now
            barge_at = last_barge_time()
            if barge_at is not None:
                # phrase_started — по монотонным часам; переводим его в
                # настенные, чтобы сравнить с отметкой из журнала.
                wall_started = time.time() - (now - self.phrase_started)
                if barge_at >= wall_started - 0.2:
                    self.interrupted()

        if self.phrase is None and self.idle_after and now >= self.idle_after:
            self.to_idle()

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

        if self.phrase is not None and now < self.clip_open_at:
            # До первого слога: играем дорисованный переход (или держим кадр).
            frame = self.strip_frame(now)
            if frame is not None:
                self.last_painted = frame
                self.photo = ImageTk.PhotoImage(frame)
                self.canvas.itemconfigure(self.image_id, image=self.photo)
            return

        if self.phrase is not None:
            since = now - self.last_paint_at
            if since > 0.6 and now - self.freeze_log_at > 3.0:
                self.freeze_log_at = now
                clip = self.clips.clip
                note(f"зависание: {since:.1f} с без кадров, клип "
                     f"{clip[0].parent.name if clip else 'нет'}, "
                     f"позиция {self.clips.next_index}")
            frame = self.clips.film_take() if FILM_MODE else self.clips.take()
            if frame is not None:
                self.paint(self.blend_into(frame))
                self.drawn += 1
        else:
            if not self.visible:
                return
            if self.hold and self.queue and not FILM_MODE:
                # Между фразами клип продолжаем, но у закрытого рта соседние
                # кадры почти не отличаются — глаз видит замирание. Добавляем
                # лёгкое покачивание вокруг текущего кадра (28.10, 00:26).
                clip = self.clips.clip
                if clip:
                    base = int(getattr(self.clips, "next_index", 0))
                    sway = int(round(6.0 * math.sin(time.monotonic() * 2.4)))
                    index = max(0, min(len(clip) - 1, base + sway))
                    # Запоминаем, на каком кадре на самом деле стоит лицо:
                    # с него и продолжим речь, иначе на входе будет рывок до
                    # шести кадров (28.10, утро).
                    self.clips.next_index = index
                    frame = self.clips._frame(clip, index)
                    if frame is not None:
                        self.paint(self.blend_into(frame))
                        return
                frame = self.clips.take()
                if frame is not None:
                    self.paint(self.blend_into(frame))
                return
            if now - self.last_idle >= 1.0 / FPS:
                self.last_idle = now
                if FILM_MODE:
                    # 31.10: покой — последний кадр ролика, с лёгким дыханием.
                    base = self.film_idle if getattr(self, "film_idle", None) is not None else self.last_painted
                    frame = self._breathe(base) if base is not None else None
                elif self.idle_plain:
                    frame = self.idle.frames[0] if self.idle.frames else None
                else:
                    frame = self.idle.next_frame()
                    self.clips.last_idle_index = self.idle.index
                if frame is not None:
                    self.paint(self.blend_into(frame))

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
