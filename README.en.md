# DSH Avatar — a talking face for DeepSeek Harness on Windows

A companion avatar for [DeepSeek Harness](https://github.com/deepseek-ai): a
separate borderless window with a face that speaks together with the voice
answer. No GPU at runtime, no neural networks — just pre-rendered clips and
careful synchronisation with the audio.

Built for an old laptop: the GPU is busy with other jobs, so the face is played
from prepared clips instead of being rendered in real time.

![Demo: the face speaks and returns to idle](demo/demo.gif)

[Higher quality video (MP4)](demo/demo.mp4) · [Screenshot](docs/screenshot.png)

## What it does

- **Always-on-top face** — frameless, transparent background, sits above other windows.
- **Synced to sound** — frames start on the "playback started" event, not when a
  file appears; clip rate is stretched to the length of the phrase.
- **One clip per reply** — the clip is not restarted for every phrase: it keeps
  going from the same position, so there is no jump and no idle flash between phrases.
- **Smooth enter and exit** — a two-frame cross-fade when speech starts and when it
  returns to idle.
- **Idle loop** — a separate frame cycle while the assistant is silent.
- **Service watchdog** — watches ASR, TTS, the window and the bridge, restarts
  only what died, keeps a log.
- **Daily restore points** — automatic copies before risky edits.

## How it works

```
microphone → local ASR → DSH → answer
                                │
                        TTS (edge-tts)
                                │
                         bridge (tts_bridge.py)
                                │
                        face window (call_live.py)
```

The window does not touch audio: it reads the plugin log and the queue of files,
picks a clip by phrase length and draws frames at 25 fps.

## Layout

| File | Purpose |
|---|---|
| `avatar/call_live.py` | the face window: clips, idle, sync |
| `avatar/tts_bridge.py` | bridge between the app and the file queue |
| `avatar/watchdog.py` | service watchdog |
| `avatar/backup_day.py` | restore points |
| `clips-maker.py` | renders new clips from audio |

## Requirements

- Windows 10/11, Python 3.12 (a venv is used here).
- DeepSeek Harness with the voice mode and TTS plugins enabled.
- A clip set in `avatar/_clips/talk_XX/*.jpg` (not shipped — make clips from
  your own portrait).

## Install (short)

1. Copy `avatar/` into your DSH workspace.
2. Replace the placeholder paths at the top of each file: `<WORKSPACE>`,
   `<DSH_VOICE>`, `<DSH_PROFILE>`.
3. Start `call_live.py` from Task Scheduler (pythonw, at logon).
4. Put the clips and one portrait into `avatar/`.
5. Enable the voice mode and say a phrase.

Details: [English install guide](docs/install.en.md) ·
[Подробная инструкция (русский)](docs/установка.md).

## If the voice is gone

The most common trouble: the reply arrives as text, but nothing is spoken.

1. **Is the TTS service alive?** Open `http://127.0.0.1:8901/health` — it should
   answer `{"ok": true}`. If not, start the warm TTS server.
2. **Does synthesis work?** Run `edge-tts.exe` manually on any text. The error
   `NoAudioReceived` means Microsoft returned nothing: it is the network or a rate
   limit, not our code. Retrying a minute later usually works.
3. **The plugin went silent after an error.** The conversation log shows
   "a failed synthesis stays silent" — TTS will not come back by itself, restart the app.
4. **Voice works but the face does not move.** The bridge is not connected: it should
   log "подключились" (connected). Check that it runs and found the app.

## Limitations

- Clips do not know the words: only length and general mood match; lip shapes do
  not follow specific phonemes.
- Requires DeepSeek Harness: the window reads its log and TTS files.
- Windows-only: uses WinAPI for the window and visibility checks.

## License

MIT — see [LICENSE](LICENSE).
