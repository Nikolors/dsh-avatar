"""Шестая пачка клипов: разной длины и разного настроения.

Задача от пользователя (22.10): чтобы анимация точнее ложилась на длину
фразы, а мимика была разнообразнее. Поэтому берём фразы трёх групп длины
(короткие, средние, длинные) и внутри каждой — от тихих до громких: громкая
фраза даёт более энергичную артикуляцию, тихая — спокойную.

Клипы кладутся прямо в avatar\_clips\talk_105... чтобы окно подхватило их
при следующем запуске. Запускать: python tmp-make-clips-hi6.py
"""
import json
import os
import pickle
import subprocess
import time
import wave

import numpy as np

WS = r'<WORKSPACE>'
REPO = os.path.join(WS, 'ditto', 'ditto-talkinghead-main')
PY = os.path.join(WS, 'liveportrait', 'venv', 'Scripts', 'python.exe')
FFMPEG = r'<DSH_VOICE>\ffmpeg\bin\ffmpeg.exe'
CFG_DIR = os.path.join(REPO, 'checkpoints', 'ditto_cfg')
SRC = os.path.join(WS, 'avatar', 'ana-brunette-01.png')
CALL = os.path.join(WS, 'avatar', '_call')
CLIPS = os.path.join(WS, 'avatar', '_clips')
TMP = os.path.join(WS, 'avatar', '_clips_next6')
os.makedirs(TMP, exist_ok=True)

# --- отбираем фразы ------------------------------------------------------
rows = []
for name in os.listdir(CALL):
    if not name.endswith('.wav'):
        continue
    path = os.path.join(CALL, name)
    try:
        with wave.open(path, 'rb') as handle:
            seconds = handle.getnframes() / handle.getframerate()
            data = np.frombuffer(handle.readframes(handle.getnframes()), dtype=np.int16)
    except Exception:  # noqa: BLE001
        continue
    if not len(data):
        continue
    rms = float(np.sqrt((data.astype('float64') ** 2).mean())) / 32768.0
    rows.append((name, seconds, rms))

GROUPS = [
    ('короткие', 1.0, 2.2, 14),
    ('средние', 2.6, 4.2, 14),
    ('длинные', 5.0, 7.5, 10),
]

plan = []
for label, low, high, count in GROUPS:
    bucket = [r for r in rows if low <= r[1] <= high]
    bucket.sort(key=lambda item: item[2])          # от тихих к громким
    if not bucket:
        continue
    picked = []
    step = max(1, len(bucket) // count)
    for index in range(0, len(bucket), step):
        picked.append(bucket[index])
        if len(picked) >= count:
            break
    for name, seconds, rms in picked:
        plan.append((label, name, seconds, rms))

print(json.dumps({'фраз отобрано': len(plan)}, ensure_ascii=False), flush=True)

# --- настройки Ditto под наше качество ----------------------------------
cfg = pickle.load(open(os.path.join(CFG_DIR, 'v0.4_hubert_cfg_hybrid.pkl'), 'rb'))
cfg['default_kwargs']['max_size'] = 768
cfg['default_kwargs']['sampling_timesteps'] = 20
cfg_path = os.path.join(CFG_DIR, 'v0.4_hubert_cfg_hybrid_768s20.pkl')
pickle.dump(cfg, open(cfg_path, 'wb'))

env = dict(os.environ)
env['PATH'] = r'<DSH_VOICE>\ffmpeg\bin' + os.pathsep + env.get('PATH', '')
env['PYTHONIOENCODING'] = 'utf-8'
env['DITTO_WARP_BATCH'] = '1'

first = int(os.environ.get('CLIP_START', '105'))
for offset, (label, wav, seconds, rms) in enumerate(plan):
    name = 'talk_%02d' % (first + offset)
    raw = os.path.join(TMP, name + '-raw.mp4')
    final = os.path.join(TMP, name + '.mp4')
    frames_dir = os.path.join(CLIPS, name)
    started = time.time()
    proc = subprocess.run(
        [PY, '-u', 'inference.py', '--data_root', './checkpoints/ditto_trt_rtx50',
         '--cfg_pkl', cfg_path, '--audio_path', os.path.join(CALL, wav),
         '--source_path', SRC, '--output_path', raw],
        cwd=REPO, capture_output=True, text=True, encoding='utf-8',
        errors='replace', env=env)
    if proc.returncode != 0 or not os.path.exists(raw):
        print(json.dumps({'clip': name, 'ok': False,
                          'error': (proc.stderr or '')[-200:]}, ensure_ascii=False), flush=True)
        continue
    subprocess.run([FFMPEG, '-y', '-loglevel', 'error', '-i', raw,
                    '-vf', 'scale=374:374:flags=lanczos,crop=320:374', '-an',
                    '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18', final],
                   capture_output=True)
    os.makedirs(frames_dir, exist_ok=True)
    for old in os.listdir(frames_dir):
        os.remove(os.path.join(frames_dir, old))
    subprocess.run([FFMPEG, '-y', '-loglevel', 'error', '-i', final, '-qscale:v', '2',
                    os.path.join(frames_dir, '%04d.jpg')], capture_output=True)
    count = len([f for f in os.listdir(frames_dir) if f.endswith('.jpg')])
    os.remove(raw)
    print(json.dumps({'clip': name, 'группа': label,
                      'фраза': wav, 'длина': round(seconds, 1),
                      'кадров': count, 'секунд': round(time.time() - started, 1)},
                     ensure_ascii=False), flush=True)

print('готово', flush=True)
