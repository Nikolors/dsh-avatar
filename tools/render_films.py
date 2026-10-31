"""Рендер «фильмов» Ditto: каждый ролик отдельно, тишина по краям (31.10).

Все ролики начинаются от одного портрета, поэтому первые кадры совпадают.
Запуск: python tools/render_films.py [начало] [конец]
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PY = r"C:\ditto_work\venv\Scripts\python.exe"
REPO = r"C:\ditto_work\ditto-talkinghead-main"
CFG = "./checkpoints/ditto_cfg/v0.4_hubert_cfg_pytorch.pkl"
SRC = r"C:\work\workspace\avatar\ana-brunette-01.png"
FILMS = Path(r"C:\ditto_work\films")
LOG = Path(r"C:\ditto_work\films\render.log")


def main() -> None:
    first = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    last = int(sys.argv[2]) if len(sys.argv) > 2 else 24
    env = os.environ.copy()
    env["PATH"] = r"C:\Users\ipdnv\dsh-voice\ffmpeg\bin;" + env.get("PATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    for index in range(first, last + 1):
        wav = FILMS / f"film_{index:02d}.wav"
        out = FILMS / f"film_{index:02d}.mp4"
        if not wav.exists() or out.exists():
            continue
        started = __import__("time").time()
        result = subprocess.run(
            [PY, "inference.py", "--data_root", "./checkpoints", "--cfg_pkl", CFG,
             "--audio_path", str(wav), "--source_path", SRC, "--output_path", str(out)],
            cwd=REPO, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
        took = __import__("time").time() - started
        line = f"{index:02d}: {'ok' if out.exists() else 'FAIL'} за {took:.0f} с"
        print(line, flush=True)
        with LOG.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    print("готово", flush=True)


if __name__ == "__main__":
    main()
