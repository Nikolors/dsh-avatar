# Installation (English)

Short version of [`установка.md`](установка.md) for the first release.

## What you need

- Windows 10 or 11, Python 3.12 (a virtual environment is recommended).
- **DeepSeek Harness** with the voice mode and TTS plugins enabled.
- `ffmpeg` and `edge-tts` — the bridge calls them by absolute path.
- A clip set: `avatar/_clips/talk_XX/*.jpg` and an idle cycle `avatar/_idle/idle_*.jpg`.
  They are **not** shipped — clips are made from your own portrait.

## Steps

1. **Copy the code.** Put the `avatar/` folder into your DSH workspace
   (for example `C:\work\workspace\avatar`).

2. **Replace the placeholder paths.** At the top of each file in `avatar/`:

   | Placeholder | Meaning |
   |---|---|
   | `<WORKSPACE>` | your DSH workspace folder |
   | `<DSH_VOICE>` | the `dsh-voice` folder (ffmpeg, edge-tts, plugin) |
   | `<DSH_PROFILE>` | profile folder, usually `C:\Users\<you>\.dsh\profiles\desktop` |

3. **Put the material in place.** The window expects:

   - `avatar/_clips/talk_01 … talk_NN` — one folder per speech clip, frames `0001.jpg …`;
   - `avatar/_idle` — the idle frame cycle;
   - optional `avatar/_idle_mode.txt` with `pingpong` (default) or `loop`.

   `clips-maker.py` shows how clips are cut from a rendered video.

4. **Start the two processes.** From Task Scheduler, at logon, as `pythonw.exe`:

   ```
   pythonw "<WORKSPACE>\avatar\call_live.py"
   pythonw "<WORKSPACE>\avatar\tts_bridge.py"
   ```

   `watchdog.py` can be scheduled as well: it restarts whatever died.

5. **Check it.** Enable the voice mode and say a phrase. The face should start
   moving when the answer is spoken and return to idle when it ends.

## Logs

- `avatar/_live.log` — the window: phrases, clips, frames, idle.
- `avatar/_bridge.log` — the bridge: which phrase file it wrote.
- `avatar/_watchdog2.log` — the watchdog: what it restarted.

If something is silent, see the "If the voice is gone" section in the main
[README](../README.en.md).
