"""Профиль рта по каждому клипу: где рот открыт, где закрыт, как часто двигается.

Открытость считаем по средней яркости области рта: открытый рот даёт тёмную
полость, закрытый — светлую кожу и губы. Значения нормируем в 0..1.

Результат — avatar\_clip_mouth.json:
  "talk_60": {"frames": 249, "length_s": 9.96, "closed": 12,
              "rate": 4.2, "profile": [0.13, 0.15, ...]}
Использование:
  python clip_profiles.py <папка с клипами> <файл json>
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np
from PIL import Image

# Доли кадра: область рта (нижняя треть по центру).
BOX = (0.30, 0.60, 0.70, 0.82)
FPS = float(os.environ.get('CLIP_FPS', '50'))


def mouth_profile(frames: list[Path]) -> np.ndarray:
    values = []
    for path in frames:
        try:
            image = Image.open(path).convert("L")
        except Exception:  # noqa: BLE001
            values.append(np.nan)
            continue
        w, h = image.size
        box = (int(w * BOX[0]), int(h * BOX[1]), int(w * BOX[2]), int(h * BOX[3]))
        crop = image.crop(box)
        values.append(float(np.asarray(crop, dtype=np.float32).mean()))
    data = np.asarray(values, dtype=np.float32)
    if np.all(np.isnan(data)):
        return data
    baseline = np.nanmax(data)                 # самый светлый кадр = рот закрыт
    spread = max(1e-3, baseline - np.nanmin(data))
    return (baseline - data) / spread          # 0 = закрыт, 1 = максимально открыт


def count_openings(profile: np.ndarray, threshold: float = 0.35) -> int:
    openings = 0
    inside = False
    for value in profile:
        if np.isnan(value):
            continue
        if not inside and value >= threshold:
            openings += 1
            inside = True
        elif inside and value < threshold * 0.6:
            inside = False
    return openings


def main() -> None:
    clips_dir = Path(sys.argv[1])
    out_path = Path(sys.argv[2])
    result = {}
    for folder in sorted(p for p in clips_dir.iterdir() if p.is_dir()):
        if not folder.is_dir():
            continue
        frames = sorted(folder.glob("*.jpg")) or sorted(folder.glob("*.png"))
        if not frames:
            continue
        profile = mouth_profile(frames)
        length = len(frames) / FPS
        openings = count_openings(profile)
        closed_index = int(np.nanargmin(profile)) if not np.all(np.isnan(profile)) else 0
        result[folder.name] = {
            "frames": len(frames),
            "length_s": round(length, 3),
            "closed": closed_index,
            "openings": openings,
            "rate": round(openings / max(0.2, length), 3),
            "profile": [round(float(v), 3) if not np.isnan(v) else 0.0 for v in profile],
        }
        print(f"{folder.name}: {len(frames)} кадров, {length:.1f} с, открытий {openings}, "
              f"темп {openings / max(0.2, length):.1f}/с", flush=True)

    out_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    print(f"сохранено: {out_path} ({len(result)} клипов)", flush=True)


if __name__ == "__main__":
    main()
