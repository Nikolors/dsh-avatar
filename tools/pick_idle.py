"""Выбор самого спокойного участка покоя из кадров Ditto (31.10).

Считает разницу соседних кадров и яркость области рта, выбирает окно
заданной длины с наименьшим движением и закрытым ртом.

Запуск: python tools/pick_idle.py <папка кадров> <папка результата> [длина_сек]
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

FPS = 25
WINDOW_SEC = 6.0


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2])
    window_sec = float(sys.argv[3]) if len(sys.argv) > 3 else WINDOW_SEC
    frames = sorted(src.glob("idle_*.jpg"))
    if len(frames) < 10:
        raise SystemExit("мало кадров")
    win = int(window_sec * FPS)

    diffs = [0.0]
    mouths = []
    prev = None
    for f in frames:
        img = Image.open(f).convert("L")
        small = img.resize((48, 56))
        if prev is not None:
            d = ImageChops.difference(prev, small)
            diffs.append(sum(ImageStat.Stat(d).mean))
        prev = small
        h, w = img.size
        mouth = img.crop((int(w * 0.32), int(h * 0.52), int(w * 0.68), int(h * 0.70)))
        mouths.append(ImageStat.Stat(mouth).mean[0])

    mouth_med = sorted(mouths)[len(mouths) // 2]
    best = None
    for i in range(0, max(1, len(frames) - win)):
        motion = sum(diffs[i + 1:i + win]) / max(1, win - 1)
        dark = sum(mouths[i:i + win]) / win
        score = motion + abs(dark - mouth_med) * 0.05
        if best is None or score < best[0]:
            best = (score, i, motion, dark)
    _, start, motion, dark = best
    print(f"кадров: {len(frames)}, окно: {win} ({window_sec:.1f} с)")
    print(f"лучший участок: кадр {start} ({start / FPS:.1f} с), движение {motion:.2f}, яркость рта {dark:.0f}")

    dst.mkdir(exist_ok=True)
    for old in dst.glob("idle_*.jpg"):
        old.unlink()
    for n, f in enumerate(frames[start:start + win], start=1):
        target = dst / f"idle_{n:04d}.jpg"
        target.write_bytes(f.read_bytes())
    print("сохранено:", len(list(dst.glob('idle_*.jpg'))), "→", dst)


if __name__ == "__main__":
    main()
