# File2TV – notes for Claude Code

Windows desktop app that plays local video files on a TV or another player in the home
network over **DLNA (UPnP AVTransport)** or **Google Cast**. Replacement for Windows
"Cast to Device" (which transcodes to WMV, squashes 16:9 to 4:3 and drops subtitles).
Principle: **never re-encode the picture.** The file goes to the TV unchanged; only when
needed ffmpeg *repacks* it (container, audio, subtitles).

Code comments and log messages are in Czech; user-facing texts are translated (see i18n).
The internal package is still called `tvcast` (the app was renamed to File2TV).

The README exists in two languages: `README.md` (English, shown by GitHub) and `README.cs.md`
(Czech). **Keep them in sync** – every change goes into both files.

## Layout

| File | What it does |
|---|---|
| `main.py` | Entry point → `tvcast.app.main()` |
| `tvcast/app.py` | pywebview window, JS API (`Api`), asyncio loop in a background thread, tray, single instance (TCP 127.0.0.1:47653 – a second launch forwards files, e.g. from Send To), startup/migration |
| `tvcast/player.py` | Queue (`Item`), playback pipeline: prepare → serve → `dev.load` → `_watch` (polls status every 1 s, end/stall/error detection), prefetch of the next item, keep-awake, Wake-on-LAN, notices |
| `tvcast/media.py` | ffprobe → `MediaInfo`/`Track`; `default_choice` (audio/subs by preferred languages); `plan()` decides remux/container/audio conversion/DV/subtitles; `ffmpeg_args`/`remux`/`extract_vtt` |
| `tvcast/upnp.py` | SSDP discovery (sends M-SEARCH from every local interface), device description, SOAP; `Device` = DLNA renderer; `Discovery` (search every 30 s, active probe of missing devices); `Status` shared with Cast |
| `tvcast/cast.py` | `CastDevice` (pychromecast, same interface as `upnp.Device`), `CastDiscovery` (mDNS), per-model capability table `MODEL_CAPS` |
| `tvcast/server.py` | aiohttp file server with Range, DLNA headers, CORS (Cast receiver), `CaptionInfo.sec` (Samsung) |
| `tvcast/winutil.py` | **Windows-only**: config (`%APPDATA%\File2TV\config.json`), keep-awake (PowerCreateRequest), Send To shortcut, firewall rule (netsh via UAC), MAC via SendARP, Wake-on-LAN |
| `tvcast/icon.py` | Tray icon (pystray) and the app icon drawn with Pillow; dark mode from the registry |
| `tvcast/i18n.py` | Language from Windows or settings, `t(key)`, `locales/<code>.json` |
| `ui/index.html` | Whole UI (HTML/CSS/JS in one file); gets state via `app.update(snapshot)` pushed from Python |

Device interface (both `upnp.Device` and `cast.CastDevice`): `kind`, `name`, `host`, `manufacturer`, `model`,
`caps_key`, `accepts(mime)`, `mime_for(*mimes)`, `dolby_vision` (False = can't, None = unknown),
`embeds_subtitles`, `subtitle_mime`, async `load/play/pause/stop/seek/status/volume/set_volume/set_mute`.
Cast devices may also have `video_codecs`/`audio_codecs` (known limits).

## Rules – don't break these

- **Delete only files the app created in `media.TEMP`** (`%TEMP%\File2TV`), never the user's videos;
  `player._is_temp` guards every unlink.
- **No picture re-encoding.** If a device can't decode the video codec, say so (`plan.error`), don't transcode.
- Every user-visible text goes through `t("key")` (Python) or `T("key")` (JS, keys `ui.*`) and must exist in
  **all 8** `tvcast/locales/*.json` (cs, en, sk, de, pl, fr, es, it) with the same `{placeholders}`.
  Track language names come from `Intl.DisplayNames` in the UI, not from the locales.
- UI style: only active/enabled states are blue (accent); buttons don't change their text when toggled.
- Settings that change repacking (`convert_hd_audio`, `dv_to_mp4`) are part of `Item.choice()` so a cached
  repack is rebuilt when they change.

## Verified on real devices (don't "fix" these back)

LG OLED CX (DLNA):
- Dolby Vision turns on **only from MP4** with the DV config record (`dvcC`) – tested MKV, TS, MP4 `dvh1`,
  MP4 `hvc1`: MKV and TS → HDR10 only, both MP4 → DV. ffmpeg writes `dvcC` only with `-strict unofficial`.
- **TrueHD over DLNA: picture plays, no audio.** Hence TrueHD/DTS → AC3 by default. E-AC3 (incl. Atmos) is fine.
- Reports `video/x-matroska`; `GetVolume` doesn't work (volume UI is hidden when `None`).
- `Play` may block until the TV starts downloading; a timeout without any HTTP request means firewall.
- H.264 4:4:4 (e.g. ffmpeg `testsrc` default) is not playable – make test videos with `-pix_fmt yuv420p`.
- Text subtitles inside MKV are shown; image subtitles (PGS/DVD) over DLNA are not.

Samsung (old and new, DLNA): calls MKV `video/x-mkv` (`Device.mime_for` handles aliases); no Dolby Vision
(DV profile 8 plays as HDR10, profile 5 would have wrong colours → warning).

Nvidia Shield Pro 2019 (Cast, Default Media Receiver):
- Only MP4/WebM; subtitles only as separate WebVTT (CORS header required).
- **Dolby Vision is not passed** (MP4 `dvh1` and `hvc1` → HDR10), even with DV enabled on the Shield.
- After a seek it reports `BUFFERING` once and goes silent → `CastDevice.status` calls `update_status()` every poll.
- After pressing Back the receiver stays in the background (audio only, picture stuck) → `load` quits the
  receiver app first so it starts in the foreground (+~5 s).
- When the receiver restarts the device briefly drops out of mDNS → removal is delayed 30 s.

Google Nest Hub (Cast): no HEVC, no AC3/E-AC3 decoding (silent) → audio to AAC stereo, HEVC → clear error.

Kodi on Android (DLNA renderer on the Shield):
- In the background it accepts the video, plays a few seconds of audio, position stalls, picture stays on the
  home screen → `_watch` warns after 10 s without progress (Kodi-specific message).
- Fully closed → connection refused/timeout; `Discovery._probe` removes it from the list right away.
- With audio passthrough it can pass TrueHD Atmos to an eARC soundbar (user turns off `convert_hd_audio`).

Windows / packaging:
- aiohttp `sendfile` (TransmitFile) fails for files > 2 GB (WinError 87) → `AIOHTTP_NOSENDFILE=1` before importing aiohttp.
- A venv's `python.exe` is a launcher; the process that listens (and needs the firewall rule) is the base `python.exe`.
- PyInstaller duplicates ffmpeg DLLs if bundled → `build.ps1` copies `ffmpeg\` next to the exe after the build.
- TrueHD inside MP4 needs `-strict experimental`. CP1250 `.srt` must be decoded (`-sub_charenc`), not copied.

## Working on it

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.\get-ffmpeg.ps1            # BtbN ffmpeg 9.0 LGPL shared → ffmpeg\ (not in git)
.venv\Scripts\python main.py [video …]
.\build.ps1                 # → dist\File2TV\File2TV.exe
```

- Log: `%APPDATA%\File2TV\file2tv.log` (overwritten on each start). Config: `%APPDATA%\File2TV\config.json`.
- Send a file to the running app like Send To does: `python -c "from tvcast.app import forward_to_running; forward_to_running([r'C:\path\video.mkv'])"`.
- `python -m pyflakes tvcast` should be clean. There's no test suite; the logic was tested with small scripts that
  build `media.plan()` against fake devices and with real playback. Don't overwrite the user's config from such
  scripts (patch `winutil.save_config`).
- The UI can be previewed in a browser: serve the repo root over HTTP, open `/ui/index.html`, mock
  `window.pywebview.api` and call `applyStrings(...)` + `app.update(state)`.

## Porting to macOS/Linux

See the "Porting to macOS / Linux" section in README.md – the Windows-specific parts are listed there.
