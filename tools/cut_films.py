"""Нарезка «фильмов» в кадры окна: кроп, масштаб, интерполяция до 50 к/с.

Запуск: python tools/cut_films.py [шаблон]
Результат: avatar\\_filmsD\\<имя ролика>\\*.jpg (320x374, 50 к/с)
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

FFMPEG = r"C:\Users\ipdnv\dsh-voice\ffmpeg\bin\ffmpeg.exe"
SRC = Path(r"C:\ditto_work\films")
OUT = Path(r"C:\work\workspace\avatar\_filmsD")
SIZE = (320, 374)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    pattern = sys.argv[1] if len(sys.argv) > 1 else "film_*.mp4"
    width = round(1254 * SIZE[0] / SIZE[1])
    offset = (1254 - width) // 2
    vf = (f"crop={width}:1254:{offset}:0,scale={SIZE[0]}:{SIZE[1]},"
          "minterpolate=fps=50:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1")
    for movie in sorted(SRC.glob(pattern)):
        if movie.name.endswith(".tmp.mp4"):
            continue
        folder = OUT / movie.stem
        folder.mkdir(exist_ok=True)
        for old in folder.glob("*.jpg"):
            old.unlink()
        subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(movie),
                        "-vf", vf, "-q:v", "2", str(folder / "%04d.jpg")],
                       capture_output=True)
        print(movie.name, "->", len(list(folder.glob("*.jpg"))), "кадров", flush=True)
    print("готово", flush=True)


if __name__ == "__main__":
    main()
