"""Сборка дорожек «фильмов»: тишина — фраза — тишина (31.10).

Каждый ролик генерируется Ditto отдельно, поэтому начинается от одного и того
же портрета: первые кадры у всех роликов совпадают, последние близки.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

FFMPEG = r"C:\Users\ipdnv\dsh-voice\ffmpeg\bin\ffmpeg.exe"
SRC = Path(r"C:\ditto_work\master3")
OUT = Path(r"C:\ditto_work\films")
HEAD = 2.0
TAIL = 3.0


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    head = OUT / "sil_head.wav"
    tail = OUT / "sil_tail.wav"
    for name, seconds in (("sil_head.wav", HEAD), ("sil_tail.wav", TAIL)):
        subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi",
                        "-i", "anullsrc=r=16000:cl=mono", "-t", str(seconds),
                        str(OUT / name)], capture_output=True)
    made = 0
    for index in range(1, 25):
        phrase = SRC / f"p{index}.wav"
        if not phrase.exists():
            continue
        target = OUT / f"film_{index:02d}.wav"
        subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(head), "-i", str(phrase),
                        "-i", str(tail), "-filter_complex",
                        "[0:a][1:a][2:a]concat=n=3:v=0:a=1", "-ar", "16000", "-ac", "1",
                        str(target)], capture_output=True)
        made += 1
    print("дорожек:", made, "→", OUT)


if __name__ == "__main__":
    main()
