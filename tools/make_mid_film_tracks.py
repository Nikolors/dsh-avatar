"""Ролики средней длины (31.10): тишина — речь на N секунд — тишина."""
from __future__ import annotations

import subprocess
from pathlib import Path

FFMPEG = r"C:\Users\ipdnv\dsh-voice\ffmpeg\bin\ffmpeg.exe"
SRC = Path(r"C:\ditto_work\master3")
OUT = Path(r"C:\ditto_work\films")


def build(name: str, speech_seconds: float) -> None:
    head = OUT / "sil_head.wav"
    tail = OUT / "sil_tail.wav"
    pause = OUT / "sil_pause_short.wav"
    pieces = []
    total = 0.0
    index = 1
    while total < speech_seconds:
        phrase = SRC / f"p{index}.wav"
        pieces.append(phrase)
        total += 3.8
        index += 1
        if index > 24:
            index = 1
    lines = [f"file '{head.as_posix()}'"]
    for piece in pieces:
        lines.append(f"file '{piece.as_posix()}'")
        lines.append(f"file '{pause.as_posix()}'")
    lines.append(f"file '{tail.as_posix()}'")
    listing = OUT / f"{name}_list.txt"
    listing.write_text("\n".join(lines), encoding="utf-8")
    target = OUT / f"{name}.wav"
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "concat", "-safe", "0",
                    "-i", str(listing), "-ar", "16000", "-ac", "1", str(target)],
                   capture_output=True)
    probe = subprocess.run([FFMPEG, "-hide_banner", "-i", str(target)],
                           capture_output=True, text=True)
    for line in probe.stderr.splitlines():
        if "Duration" in line:
            print(name, ":", line.split("Duration:")[1].split(",")[0].strip())


if __name__ == "__main__":
    build("film_len_15", 11.0)
    build("film_len_25", 20.0)
    build("film_len_35", 30.0)
