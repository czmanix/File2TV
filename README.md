# File2TV

**English** | [Čeština](README.cs.md)

Play videos from a Windows PC on a TV in your home network (DLNA + Google Cast) – a replacement for the limited Windows "Cast to Device". File2TV tries to keep the best quality available (Dolby Vision, Dolby Atmos, TrueHD, ...) intact. Google Cast does not support files with Dolby Vision, plays them as HDR10.

- Finds TVs that are switched on and other DLNA video players on the network (LG, Samsung, …)
  as well as Google Cast devices (Google TV, Nvidia Shield, Chromecast, Nest Hub); audio-only players
  (Sonos, speaker groups) are skipped. Switched-off DLNA TVs can be woken over Wake-on-LAN – only
  if that's turned on in the settings (off by default) and the TV allows turning on over the network; not Chromecasts.
- Google Cast: Google's player accepts only MP4/WebM, so MKV is always repacked to MP4 (the picture
  unchanged) and subtitles go separately as WebVTT (including those embedded in the video – extracted while repacking).
  The Shield doesn't take Dolby Vision over Google Cast (verified, plays HDR10). Google Cast doesn't report
  what it can do, so the app has a table of known models: the Nest Hub can't do HEVC or Dolby audio (verified –
  AC3/E-AC3 is converted to AAC stereo, for HEVC it says right away that it won't work), the 1st–3rd generation
  "puck" plays H.264 only.
- The video goes to the TV unchanged; the TV downloads it from a small HTTP server in the app (seeking works).
- Only when needed, ffmpeg repacks the video **without re-encoding the picture** (into `%TEMP%\File2TV`):
  it puts the chosen audio and subtitles first and as default, converts DTS/TrueHD/Opus audio to AC3/AAC, keeps Dolby Atmos in E-AC3,
  and embeds subtitles lying next to the video (`Film.srt`, `Film.cs.srt`, CP1250 and UTF-8 encoding).
  The next video in the queue is prepared while the previous one is playing.
- Dolby Vision (profiles 5 and 8) is repacked to MP4 with the DV record (dvcC, hvc1 tag) before sending over DLNA –
  from MKV and TS, TVs often don't turn on Dolby Vision and play only HDR10.
  Subtitles are then inside the MP4 (mov_text).
  Samsung doesn't support Dolby Vision: profile 8 is sent unchanged (plays as HDR10), for profile 5
  the app warns about wrong colours.
- In the settings you can turn off converting TrueHD/DTS to Dolby Digital (for TVs and players that support them,
  e.g. Kodi – Atmos in TrueHD is kept) and repacking Dolby Vision to MP4. Both are on by default;
  limits of specific devices (Nest Hub, Samsung) always apply.
- Image-based subtitles from Blu-ray/DVD (PGS) aren't shown by TVs over DLNA – the app warns about it.
- Queue, pause, seeking (also with arrow keys), volume (if the TV offers it over DLNA), choice of audio
  and subtitles, "Send to → File2TV" in File Explorer, tray icon. Closing the window during
  playback only hides it. The PC doesn't go to sleep while playing (the screen may turn off).

Settings and log: `%APPDATA%\File2TV\` (`config.json`, `file2tv.log`).

## Languages

The app speaks the Windows language (Czech, English, Slovak, German, Polish, French,
Spanish, Italian; otherwise English); the language can be changed in the settings. Texts are
in `tvcast/locales/<code>.json` – keys `ui.*` for the window, the rest for messages from Python; a missing
text falls back to English. Names of audio and subtitle languages come from the browser engine (`Intl.DisplayNames`),
so they aren't needed in the translations. A new language = a new JSON + an entry in `i18n.LANGUAGES`.
Translations other than Czech and English are machine-made – native speakers are welcome to improve them.

## Development

Windows 10/11, Python 3.13:

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.\get-ffmpeg.ps1
.venv\Scripts\python main.py [video …]
```

`get-ffmpeg.ps1` downloads [BtbN ffmpeg 9.0 LGPL shared](https://github.com/BtbN/FFmpeg-Builds/releases)
without ffplay into `ffmpeg\` (~75 MB, licence in `ffmpeg\LICENSE.txt`); it doesn't belong in Git. Without it the app uses ffmpeg from PATH.

## Building

`.\build.ps1` → `dist\File2TV\` (~190 MB, the whole folder can be copied anywhere or zipped);
ffmpeg from the `ffmpeg\` folder is copied next to the .exe.

First launch: Windows asks about the firewall – allow it for private networks. If the TV can't load the video,
the app offers an "Allow in firewall" button (Windows asks for administrator permission).

## Porting to macOS / Linux

The app is Windows-only for now and hasn't been tried elsewhere. The core is portable: finding
and controlling players (DLNA, Chromecast), the HTTP server, the repacking decisions (ffmpeg), the window
(pywebview uses WebKit on a Mac) and the translations. This needs rewriting:

| Place | Now (Windows) | Replacement elsewhere |
|---|---|---|
| `winutil.KeepAwake` | `PowerCreateRequest` / `PowerSetRequest` | macOS: `caffeinate -i` (or IOKit), Linux: `systemd-inhibit` |
| `winutil.set_sendto` | shortcut in the SendTo folder (PowerShell, WScript.Shell) | macOS: "Open With" / Finder Services; Linux: a `.desktop` file |
| `winutil.allow_firewall` | `netsh` via UAC | macOS asks by itself – hide the button; Linux depends on the distribution |
| `winutil.mac_address` | `SendARP` (iphlpapi) | `arp -n <ip>` or `ip neigh` |
| `winutil.CONFIG`, `SENDTO` | `%APPDATA%` | `~/Library/Application Support/File2TV`, `~/.config/File2TV` |
| `upnp.local_addresses` | addresses from `getaddrinfo(gethostname())` | on a Mac often only 127.0.0.1 – use e.g. the `ifaddr` package |
| `icon.tray` + `App.run` | pystray in its own thread | on a Mac it must run in the main thread (where pywebview is) – handle the menu-bar icon differently, or leave it out |
| `icon.dark_mode` | Windows registry | the WebView follows the system by itself, only the window colour before loading |
| `i18n.system_language` | `GetUserDefaultUILanguage` | already has a `locale` fallback |
| `media.tool` | looks for `ffmpeg.exe` next to the app, otherwise PATH | without `.exe`; on a Mac `brew install ffmpeg` is enough |
| `build.ps1`, `get-ffmpeg.ps1` | PowerShell, PyInstaller `--windowed` → `.exe` | shell script, PyInstaller → `.app`; ffmpeg for that platform |

To distribute on a Mac the app has to be signed and notarized (Apple Developer account); otherwise the user
has to allow it manually (right-click → Open). The verified behaviour of specific TVs and players, which shouldn't
break when porting, is in `CLAUDE.md`.

## License

[MIT](LICENSE). Release builds bundle [FFmpeg](https://ffmpeg.org) ([BtbN build](https://github.com/BtbN/FFmpeg-Builds)), licensed under the GNU LGPL – its licence text is in `ffmpeg\LICENSE.txt` next to the application.
