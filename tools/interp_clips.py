"""Интерполяция клипов до 50 к/с (как делали 27.10 вручную).

Для каждой папки talk_* собирает кадры в видео 25 к/с, интерполирует ffmpeg
minterpolate до 50 к/с и раскладывает обратно в jpg. Старые кадры заменяются.

Запуск: python tools/interp_clips.py <папка с talk_*>
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

FFMPEG = r"C:\Users\ipdnv\dsh-voice\ffmpeg\bin\ffmpeg.exe"


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    root = Path(sys.argv[1])
    clips = sorted(root.glob("talk_*"))
    print("клипов:", len(clips))
    for folder in clips:
        frames = sorted(folder.glob("*.jpg"))
        if len(frames) < 2:
            continue
        tmp_in = folder.parent / f"_{folder.name}_in.mp4"
        tmp_out = folder.parent / f"_{folder.name}_out.mp4"
        subprocess.run([FFMPEG, "-y", "-v", "error", "-framerate", "25",
                        "-i", str(folder / "%04d.jpg"), "-c:v", "libx264",
                        "-crf", "16", "-pix_fmt", "yuv420p", str(tmp_in)],
                       capture_output=True)
        subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(tmp_in),
                        "-vf", "minterpolate=fps=50:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1",
                        "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", str(tmp_out)],
                       capture_output=True)
        new_dir = folder.parent / f"_{folder.name}_50"
        new_dir.mkdir(exist_ok=True)
        subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(tmp_out),
                        "-q:v", "2", str(new_dir / "%04d.jpg")], capture_output=True)
        made = len(list(new_dir.glob("*.jpg")))
        if made < 2:
            print(f"  {folder.name}: пропуск (не вышло)")
            shutil.rmtree(new_dir, ignore_errors=True)
            tmp_in.unlink(missing_ok=True)
            tmp_out.unlink(missing_ok=True)
            continue
        for f in frames:
            f.unlink(missing_ok=True)
        for f in sorted(new_dir.glob("*.jpg")):
            shutil.move(str(f), str(folder / f.name))
        new_dir.rmdir()
        tmp_in.unlink(missing_ok=True)
        tmp_out.unlink(missing_ok=True)
        print(f"  {folder.name}: {len(frames)} -> {made} кадров")
    print("готово")


if __name__ == "__main__":
    main()
