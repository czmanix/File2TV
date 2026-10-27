# File2TV

Přehrání videa z počítače na TV v domácí síti (DLNA + Google Cast) – náhrada omezeného „Vysílat do zařízení“ z Windows.

- Najde v síti zapnuté televize a další DLNA přehrávače videa (LG, Samsung, ...)
  i zařízení s Google cast (Google TV, Nvidia Shield, Chromecast, Nest Hub); vynechá zvukové přehrávače
  (Sonos, skupiny reproduktorů). Vypnuté DLNA TV umí zkusit zapnout přes Wake-on-LAN – jen
  když se to zapne v nastavení (výchozí vypnuto) a TV má povolené zapnutí přes síť; Chromecasty ne.
- Google Cast: přehrávač Googlu bere jen MP4/WebM, takže MKV se vždy přebalí do MP4 (obraz beze
  změny) a titulky jdou zvlášť jako WebVTT (i ty vložené ve videu – vytáhnou se při přebalení).
  Shield přes Chromecast Dolby Vision nepřijme (ověřeno, hraje HDR10). Chromecast nehlásí, co
  umí, proto má aplikace tabulku známých modelů: Nest Hub neumí HEVC a Dolby zvuk (ověřeno –
  AC3/E-AC3 převede na AAC stereo, u HEVC rovnou řekne, že to nepůjde), „puk“ 1.–3. generace
  umí jen H.264.
- Video jde do TV beze změny, TV si ho stahuje z malého HTTP serveru v aplikaci (přetáčení funguje).
- Jen když je potřeba, ffmpeg video **bez překódování obrazu** přebalí (do `%TEMP%\File2TV`):
  vybraný zvuk a titulky dá první a jako výchozí, zvuk DTS/TrueHD/Opus převede na AC3/AAC, Dolby Atmos v eAC3 nechává,
  titulky ležící vedle videa (`Film.srt`, `Film.cs.srt`, kódování CP1250 i UTF-8) vloží.
  Další video ve frontě se připravuje už během přehrávání předchozího.
- Dolby Vision (profil 5 a 8) se před odesláním přes DLNA přebalí do MP4 se záznamem DV
  (dvcC, značka hvc1) – z MKV a TS televize Dolby Vision často nezapnou a hrají jen HDR10.
  Titulky jsou pak uvnitř MP4 (mov_text).
  Samsung Dolby Vision neumí: profil 8 dostane beze změny (hraje jako HDR10), u profilu 5
  aplikace upozorní na špatné barvy.
- V nastavení jde vypnout převod TrueHD/DTS na Dolby Digital (pro TV a přehrávače, které je umí,
  např. Kodi – zůstane Atmos v TrueHD) a přebalení Dolby Vision do MP4. Obojí je výchozí zapnuté;
  omezení konkrétních zařízení (Nest Hub, Samsung) platí vždy.
- Obrázkové titulky z Blu-ray/DVD (PGS) TV přes DLNA neukáže – aplikace na to upozorní.
- Fronta, pauza, přetáčení (i šipkami), hlasitost (pokud ji TV přes DLNA nabízí), výběr zvuku
  a titulků, „Odeslat do → File2TV“ v Průzkumníku, ikona v oznamovací oblasti. Křížek během
  přehrávání okno jen schová. PC během přehrávání neusne (obrazovka zhasnout smí).

Nastavení a log: `%APPDATA%\File2TV\` (`config.json`, `file2tv.log`).

## Jazyky

Aplikace mluví jazykem Windows (čeština, angličtina, slovenština, němčina, polština, francouzština,
španělština, italština; jinak anglicky), v nastavení jde jazyk změnit. Texty jsou
v `tvcast/locales/<kód>.json` – klíče `ui.*` pro okno, ostatní pro hlášky z Pythonu; chybějící
text se vezme z angličtiny. Názvy jazyků zvukových stop a titulků dává prohlížeč (`Intl.DisplayNames`),
takže nejsou potřeba v překladech. Nový jazyk = nový JSON + záznam v `i18n.LANGUAGES`.
Překlady mimo češtinu a angličtinu jsou strojové – rodilý mluvčí je může vylepšit.

## Vývoj

Windows 10/11, Python 3.13:

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.\get-ffmpeg.ps1
.venv\Scripts\python main.py [video …]
```

`get-ffmpeg.ps1` stáhne do `ffmpeg\` [BtbN ffmpeg 9.0 LGPL shared](https://github.com/BtbN/FFmpeg-Builds/releases)
bez ffplay (~75 MB, licence v `ffmpeg\LICENSE.txt`); do Gitu nepatří. Bez něj aplikace použije ffmpeg z PATH.

## Sestavení

`.\build.ps1` → `dist\File2TV\` (~190 MB, celou složku jde zkopírovat kamkoli nebo zazipovat),
ffmpeg ze složky `ffmpeg\` se zkopíruje vedle .exe.

První spuštění: Windows se zeptá na firewall – povolit pro soukromou síť. Když TV video nenačte,
nabídne aplikace tlačítko „Povolit ve firewallu“ (Windows se zeptá na oprávnění správce).

## Přenos na macOS / Linux

Aplikace je zatím jen pro Windows a jinde nebyla vyzkoušená. Jádro je přenositelné: hledání
a ovládání přehrávačů (DLNA, Chromecast), HTTP server, rozhodování o přebalení (ffmpeg), okno
(pywebview má na Macu WebKit) a překlady. Přepsat je potřeba tohle:

| Místo | Teď (Windows) | Náhrada jinde |
|---|---|---|
| `winutil.KeepAwake` | `PowerCreateRequest` / `PowerSetRequest` | macOS: `caffeinate -i` (nebo IOKit), Linux: `systemd-inhibit` |
| `winutil.set_sendto` | zástupce ve složce SendTo (PowerShell, WScript.Shell) | macOS: „Otevřít v aplikaci“ / Služby Finderu; Linux: `.desktop` soubor |
| `winutil.allow_firewall` | `netsh` přes UAC | macOS se zeptá sám – tlačítko skrýt; Linux podle distribuce |
| `winutil.mac_address` | `SendARP` (iphlpapi) | `arp -n <ip>` nebo `ip neigh` |
| `winutil.CONFIG`, `SENDTO` | `%APPDATA%` | `~/Library/Application Support/File2TV`, `~/.config/File2TV` |
| `upnp.local_addresses` | adresy z `getaddrinfo(gethostname())` | na Macu často jen 127.0.0.1 – použít třeba balíček `ifaddr` |
| `icon.tray` + `App.run` | pystray ve vlastním vlákně | na Macu musí běžet v hlavním vlákně (tam je pywebview) – ikonu v liště řešit jinak, nebo vynechat |
| `icon.dark_mode` | registr Windows | WebView se řídí systémem sám, jen barva okna před načtením |
| `i18n.system_language` | `GetUserDefaultUILanguage` | už má záložní `locale` |
| `media.tool` | hledá `ffmpeg.exe` vedle aplikace, jinak PATH | bez `.exe`; na Macu stačí `brew install ffmpeg` |
| `build.ps1`, `get-ffmpeg.ps1` | PowerShell, PyInstaller `--windowed` → `.exe` | shell skript, PyInstaller → `.app`; ffmpeg pro danou platformu |

Na Macu je pro šíření potřeba aplikaci podepsat a notarizovat (Apple Developer účet), jinak ji uživatel
musí povolit ručně (pravé tlačítko → Otevřít). Ověřené chování konkrétních TV a přehrávačů, které by se
při přepisu nemělo rozbít, je v `CLAUDE.md`.
