"""Сборка длинной звуковой дорожки для Ditto — новый материал (31.10).

Больше фраз (24) и длинные паузы (4 с), тишина по краям 15 с: из неё Ditto
рисует живой покой, из речи потом нарезаются клипы.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

FFMPEG = r"C:\Users\ipdnv\dsh-voice\ffmpeg\bin\ffmpeg.exe"
TTS = "http://127.0.0.1:8901/tts"
WORK = Path(r"C:\ditto_work\master3")

PHRASES = [
    "Да.",
    "Хорошо, я здесь.",
    "Привет. Как у тебя дела?",
    "Сейчас всё спокойно, можно поговорить.",
    "Я слушаю тебя внимательно.",
    "Расскажи, что у тебя сегодня было.",
    "Это интересная мысль, давай разберёмся.",
    "Смотри, вот что мы можем сделать.",
    "Сначала я проверю журналы, а потом скажу точно.",
    "Если что-то пойдёт не так, я сразу увижу.",
    "Не переживай, у нас всё получится.",
    "Мне тоже нравится, как это выглядит.",
    "Правда? Я не ожидала такого результата.",
    "Давай попробуем ещё раз, уже спокойнее.",
    "Хорошо, я всё поняла.",
    "Тогда начнём с самого простого.",
    "Я готова, когда ты скажешь.",
    "Спасибо, что подождал.",
    "Всё работает, можно продолжать.",
    "Как ты думаешь, что лучше сделать сейчас?",
    "Да, согласна с тобой.",
    "Давай сделаем так, как ты хочешь.",
    "Я проверю и вернусь с ответом.",
    "Ну вот, кажется, всё получилось.",
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--silence", type=float, default=15.0, help="тишина в начале и в конце, сек")
    parser.add_argument("--pause", type=float, default=4.0, help="пауза между фразами, сек")
    parser.add_argument("--suffix", type=str, default="v3", help="суффикс имён файлов")
    args = parser.parse_args()

    WORK.mkdir(parents=True, exist_ok=True)
    parts = []
    for index, text in enumerate(PHRASES, start=1):
        mp3 = WORK / f"p{index}.mp3"
        wav = WORK / f"p{index}.wav"
        body = WORK / f"p{index}.json"
        body.write_text(json.dumps({"text": text, "voice": "ru-RU-SvetlanaNeural"},
                                   ensure_ascii=False), encoding="utf-8")
        subprocess.run(["curl.exe", "-s", "-m", "60", "-o", str(mp3),
                        "-H", "Content-Type: application/json",
                        "--data-binary", "@" + str(body), TTS], capture_output=True)
        subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(mp3),
                        "-ar", "16000", "-ac", "1", str(wav)], capture_output=True)
        parts.append(wav)

    pause_file = WORK / "sil_pause.wav"
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi",
                    "-i", "anullsrc=r=16000:cl=mono", "-t", str(args.pause), str(pause_file)],
                   capture_output=True)

    long_file = WORK / "sil_long.wav"
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi",
                    "-i", "anullsrc=r=16000:cl=mono", "-t", str(args.silence), str(long_file)],
                   capture_output=True)

    lines = ["file '{}'".format(long_file.as_posix())]
    for wav in parts:
        lines.append("file '{}'".format(wav.as_posix()))
        lines.append("file '{}'".format(pause_file.as_posix()))
    lines.append("file '{}'".format(long_file.as_posix()))

    listing = WORK / "list.txt"
    listing.write_text("\n".join(lines), encoding="utf-8")

    master = WORK / f"master_{args.suffix}.wav"
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "concat", "-safe", "0",
                    "-i", str(listing), "-ar", "16000", "-ac", "1", str(master)],
                   capture_output=True)

    probe = subprocess.run([FFMPEG, "-hide_banner", "-i", str(master)],
                           capture_output=True, text=True)
    for line in probe.stderr.splitlines():
        if "Duration" in line:
            print("длительность:", line.split("Duration:")[1].split(",")[0].strip())
    print("фраз:", len(parts), "тишина по краям:", args.silence, "пауза:", args.pause)
    print("файл:", master)


if __name__ == "__main__":
    main()
