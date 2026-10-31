"""Нарезка клипов из рендера Ditto (28.10).

Ditto отдаёт квадрат (1254x1254). Обрезаем его по пропорции окна и режем по
паузам, которые мы сами заложили в звук. Покой берём из паузы.

Запуск:
  python tools/cut_ditto.py <рендер.mp4> <звук.wav> <папка клипов> <папка покоя>
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

FFMPEG = r"C:\Users\ipdnv\dsh-voice\ffmpeg\bin\ffmpeg.exe"
WORK = Path(r"C:\work\workspace\.tmp-clips\ditto-cut")
SIZE = (320, 374)
FPS = 25
MIN_CLIP = 1.2


def duration_of(path: Path) -> float:
    probe = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)],
                           capture_output=True, text=True)
    for line in probe.stderr.splitlines():
        if "Duration:" in line:
            h, m, s = line.split("Duration:")[1].split(",")[0].strip().split(":")
            return int(h) * 3600 + int(m) * 60 + float(s)
    return 0.0


def main() -> None:
    if len(sys.argv) < 5:
        raise SystemExit(__doc__)
    render = Path(sys.argv[1])
    audio = Path(sys.argv[2])
    clips_dir = Path(sys.argv[3])
    idle_dir = Path(sys.argv[4])

    WORK.mkdir(parents=True, exist_ok=True)
    # Квадрат обрезаем по пропорции окна, по центру.
    width = round(1254 * SIZE[0] / SIZE[1])
    offset = (1254 - width) // 2
    master = WORK / "master.mp4"
    subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(render),
                    "-vf", f"crop={width}:1254:{offset}:0,scale={SIZE[0]}:{SIZE[1]}",
                    "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "16", str(master)], capture_output=True)
    total = duration_of(master)
    print(f"мастер: {total:.1f} с")

    detect = subprocess.run([FFMPEG, "-hide_banner", "-i", str(audio), "-af",
                             "silencedetect=noise=-34dB:d=0.30", "-f", "null", "NUL"],
                            capture_output=True, text=True)
    starts = [float(x) for x in re.findall(r"silence_start: ([0-9.]+)", detect.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([0-9.]+)", detect.stderr)]
    pauses = []
    for index, start in enumerate(starts):
        end = ends[index] if index < len(ends) else total
        pauses.append((start, end))
    print("пауз:", len(pauses))

    cuts = [0.0]
    for start, end in pauses:
        middle = (start + end) / 2.0
        if middle - cuts[-1] < MIN_CLIP:
            continue
        if total - middle < MIN_CLIP:
            break
        cuts.append(middle)
    cuts.append(total)

    if clips_dir.exists():
        shutil.rmtree(clips_dir)
    clips_dir.mkdir(parents=True)
    made = 0
    for index in range(len(cuts) - 1):
        start, end = cuts[index], cuts[index + 1]
        if end - start < MIN_CLIP:
            continue
        made += 1
        folder = clips_dir / f"talk_{made:02d}"
        folder.mkdir()
        subprocess.run([FFMPEG, "-y", "-v", "error", "-ss", f"{start:.3f}",
                        "-t", f"{end - start:.3f}", "-i", str(master), "-r", str(FPS),
                        "-q:v", "2", str(folder / "%04d.jpg")], capture_output=True)
        print(f"  {folder.name}: {end - start:.1f} с")
    print("клипов:", made)

    if idle_dir.exists():
        shutil.rmtree(idle_dir)
    idle_dir.mkdir(parents=True)
    best = max(pauses, key=lambda p: p[1] - p[0]) if pauses else (0.0, 1.2)
    start = best[0] + 0.05
    length = max(0.8, (best[1] - best[0]) - 0.1)
    subprocess.run([FFMPEG, "-y", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{length:.3f}",
                    "-i", str(master), "-r", str(FPS), "-q:v", "2",
                    str(idle_dir / "idle_%04d.jpg")], capture_output=True)
    print(f"покой: {len(list(idle_dir.glob('idle_*.jpg')))} кадров из паузы {best[0]:.2f}-{best[1]:.2f}")


if __name__ == "__main__":
    main()
