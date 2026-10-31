"""Сборка превью нового материала: покой + несколько клипов (31.10)."""
from __future__ import annotations

import subprocess
from pathlib import Path

FFMPEG = r"C:\Users\ipdnv\dsh-voice\ffmpeg\bin\ffmpeg.exe"
ROOT = Path(r"C:\work\workspace\avatar")
OUT = Path(r"C:\work\workspace\avatar\_preview_v3.mp4")
PARTS = [
    (ROOT / "_idleV3best", 25, 6.0, "покой"),
    (ROOT / "_clipsV3" / "talk_04", 50, None, "talk_04"),
    (ROOT / "_clipsV3" / "talk_05", 50, None, "talk_05"),
    (ROOT / "_clipsV3" / "talk_16", 50, None, "talk_16"),
    (ROOT / "_clipsV3" / "talk_08", 50, None, "talk_08"),
    (ROOT / "_idleV3best", 25, 4.0, "покой"),
]


def main() -> None:
    tmp = Path(r"C:\work\workspace\.tmp-clips\preview")
    tmp.mkdir(parents=True, exist_ok=True)
    files = []
    for i, (folder, fps, seconds, label) in enumerate(PARTS, start=1):
        pattern = "idle_%04d.jpg" if folder.name.startswith("_idle") else "%04d.jpg"
        out = tmp / f"part{i:02d}.mp4"
        cmd = [FFMPEG, "-y", "-v", "error", "-framerate", str(fps),
               "-i", str(folder / pattern), "-c:v", "libx264", "-crf", "18",
               "-pix_fmt", "yuv420p", "-r", "25"]
        if seconds:
            cmd += ["-t", str(seconds)]
        cmd.append(str(out))
        subprocess.run(cmd, capture_output=True)
        files.append(out)
        print("часть:", label, "->", out.name)
    listing = tmp / "list.txt"
    listing.write_text("\n".join(f"file '{f.as_posix()}'" for f in files), encoding="utf-8")
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "concat", "-safe", "0",
                    "-i", str(listing), "-c:v", "libx264", "-crf", "18",
                    "-pix_fmt", "yuv420p", str(OUT)], capture_output=True)
    print("готово:", OUT, OUT.stat().st_size // 1024, "КБ")


if __name__ == "__main__":
    main()
