"""Короткие ролики-фильмы (31.10): 0.4 с тишины — короткая фраза — 0.8 с тишины."""
from __future__ import annotations

import subprocess
from pathlib import Path

FFMPEG = r"C:\Users\ipdnv\dsh-voice\ffmpeg\bin\ffmpeg.exe"
SRC = Path(r"C:\ditto_work\master3")
OUT = Path(r"C:\ditto_work\films")

SHORT = [1, 2, 15, 17, 18, 21, 24, 4, 6, 8, 12, 16]


def main() -> None:
    head = OUT / "sil_head_short.wav"
    tail = OUT / "sil_tail_short.wav"
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi",
                    "-i", "anullsrc=r=16000:cl=mono", "-t", "0.4", str(head)],
                   capture_output=True)
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi",
                    "-i", "anullsrc=r=16000:cl=mono", "-t", "0.8", str(tail)],
                   capture_output=True)
    for number in SHORT:
        phrase = SRC / f"p{number}.wav"
        target = OUT / f"film_s{number:02d}.wav"
        subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(head), "-i", str(phrase),
                        "-i", str(tail), "-filter_complex",
                        "[0:a][1:a][2:a]concat=n=3:v=0:a=1", "-ar", "16000", "-ac", "1",
                        str(target)], capture_output=True)
    print("коротких дорожек:", len(SHORT))


if __name__ == "__main__":
    main()
