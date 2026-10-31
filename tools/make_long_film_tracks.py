"""Длинные ролики-фильмы (31.10): тишина — длинная речь — тишина.

Те же общие первый и последний кадры, что у коротких film_*, только речи
хватает на длинный ответ.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

FFMPEG = r"C:\Users\ipdnv\dsh-voice\ffmpeg\bin\ffmpeg.exe"
SRC = Path(r"C:\ditto_work\master3")
OUT = Path(r"C:\ditto_work\films")


def build(target_seconds: float, name: str) -> None:
    head = OUT / "sil_head.wav"
    tail = OUT / "sil_tail.wav"
    pieces = []
    total = 0.0
    index = 1
    while total < target_seconds:
        phrase = SRC / f"p{index}.wav"
        if not phrase.exists():
            index = 1
            phrase = SRC / f"p{index}.wav"
        pieces.append(phrase)
        total += 2.0  # примерная длина фразы с паузой
        index += 1
    listing = OUT / f"{name}_list.txt"
    lines = [f"file '{head.as_posix()}'"]
    pause = OUT / "sil_pause_short.wav"
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi",
                    "-i", "anullsrc=r=16000:cl=mono", "-t", "0.6", str(pause)],
                   capture_output=True)
    for piece in pieces:
        lines.append(f"file '{piece.as_posix()}'")
        lines.append(f"file '{pause.as_posix()}'")
    lines.append(f"file '{tail.as_posix()}'")
    listing.write_text("\n".join(lines), encoding="utf-8")
    target = OUT / f"{name}.wav"
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "concat", "-safe", "0",
                    "-i", str(listing), "-ar", "16000", "-ac", "1", str(target)],
                   capture_output=True)
    probe = subprocess.run([FFMPEG, "-hide_banner", "-i", str(target)],
                           capture_output=True, text=True)
    for line in probe.stderr.splitlines():
        if "Duration" in line:
            print(name, "длительность:", line.split("Duration:")[1].split(",")[0].strip())


if __name__ == "__main__":
    build(30.0, "film_long_30")
    build(45.0, "film_long_45")
