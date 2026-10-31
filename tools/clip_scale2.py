"""Масштаб лица в клипах — вариант с аргументами (31.10).

Запуск: python tools/clip_scale2.py <папка клипов> <профили рта json> <результат json>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image


def face_scale(image: Image.Image) -> float | None:
    small = image.convert("RGB").resize((160, 187), Image.BILINEAR)
    data = np.asarray(small, dtype=np.float32)
    red, green, blue = data[..., 0], data[..., 1], data[..., 2]
    skin = (red > 95) & (green > 40) & (blue > 20) & (red > green + 15) & (red > blue + 15)
    rows = np.where(skin.any(axis=1))[0]
    cols = np.where(skin.any(axis=0))[0]
    if rows.size < 10 or cols.size < 10:
        return None
    height = (rows.max() - rows.min() + 1) / float(small.height)
    width = (cols.max() - cols.min() + 1) / float(small.width)
    return round(float((height + width) / 2.0), 4)


def main() -> None:
    if len(sys.argv) < 4:
        raise SystemExit(__doc__)
    clips = Path(sys.argv[1])
    mouth_path = Path(sys.argv[2])
    target = Path(sys.argv[3])
    mouth = json.loads(mouth_path.read_text(encoding="utf-8")) if mouth_path.exists() else {}
    result = {}
    for folder in sorted(p for p in clips.iterdir() if p.is_dir()):
        name = folder.name
        frames = sorted(folder.glob("*.jpg")) or sorted(folder.glob("*.png"))
        if not frames:
            continue
        info = mouth.get(name) or {}
        index = int(info.get("json_first") or 0)
        index = max(0, min(len(frames) - 1, index))
        try:
            with Image.open(frames[index]) as image:
                scale = face_scale(image)
        except Exception:  # noqa: BLE001
            scale = None
        if scale is not None:
            result[name] = scale
    target.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    values = sorted(result.values())
    if values:
        print("клипов измерено: %d, масштаб от %.2f до %.2f, медиана %.2f"
              % (len(result), values[0], values[-1], values[len(values) // 2]))
    else:
        print("не удалось измерить")


if __name__ == "__main__":
    main()
