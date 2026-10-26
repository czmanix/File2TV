"""Rozbor videa (ffprobe) a jeho příprava pro TV (ffmpeg, bez překódování obrazu).

Soubor jde do TV beze změny, pokud ho přehrávač bere a vybraný zvuk i titulky jsou
to, co si TV sama zvolí. Jinak se přebalí do MKV (nebo MP4, když TV MKV nezná):
vybraný zvuk a titulky jdou první a jako výchozí, nepodporovaný zvuk (DTS, TrueHD,
Opus…) se převede, titulky ležící vedle videa se vloží (CP1250 → UTF-8).
"""

import asyncio
import contextlib
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .i18n import t

VIDEO_SUFFIXES = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".m2ts", ".mts", ".webm", ".wmv", ".mpg", ".mpeg", ".flv"}
TV_AUDIO = {"aac", "ac3", "eac3", "mp3"}  # zvuk, který přehraje prakticky každá TV
SUB_SUFFIXES = {".srt": "srt", ".ass": "ass", ".ssa": "ass"}
IMAGE_SUBS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle"}  # z Blu-ray/DVD, TV přes DLNA neukážou
MIME = {".mkv": "video/x-matroska", ".mp4": "video/mp4", ".m4v": "video/mp4", ".avi": "video/x-msvideo",
        ".mov": "video/quicktime", ".ts": "video/mp2t", ".m2ts": "video/mp2t", ".mts": "video/mp2t",
        ".webm": "video/webm", ".wmv": "video/x-ms-wmv", ".mpg": "video/mpeg", ".mpeg": "video/mpeg"}
MKV_MIMES = ("video/x-matroska", "video/x-mkv", "video/mkv")
LANG_CODES = {"cs": "cze", "cz": "cze", "ces": "cze", "sk": "slo", "slk": "slo", "en": "eng", "de": "ger",
              "deu": "ger", "fr": "fre", "fra": "fre", "es": "spa", "it": "ita", "pl": "pol", "ru": "rus",
              "hu": "hun", "ja": "jpn", "ko": "kor", "zh": "chi", "zho": "chi"}
CHANNELS = {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}
TEMP = Path(tempfile.gettempdir()) / "File2TV"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # ffmpeg z aplikace bez konzole nemá otevírat okno


def tool(name: str) -> str:
    """ffmpeg/ffprobe přibalený k aplikaci, jinak z PATH."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    for p in (base / "ffmpeg" / f"{name}.exe", Path(sys.executable).parent / "ffmpeg" / f"{name}.exe"):
        if p.is_file():
            return str(p)
    if found := shutil.which(name):
        return found
    raise MediaError(t("err.no_ffmpeg", name=name))


class MediaError(Exception):
    pass


def norm_lang(code: str) -> str:
    code = (code or "").lower()
    return LANG_CODES.get(code, code)


@dataclass
class Track:
    id: str  # "a:3" / "s:5" (stream ve videu) nebo "x:0" (soubor vedle videa)
    kind: str  # audio / subtitle
    codec: str
    lang: str = ""
    title: str = ""
    channels: int = 0
    default: bool = False
    forced: bool = False
    index: int = -1  # číslo streamu ve videu
    file: Path | None = None  # titulky vedle videa

    @property
    def image(self) -> bool:
        return self.codec in IMAGE_SUBS

    @property
    def format(self) -> str:
        """Kodek a kanály, např. „AC3 5.1“ nebo „SRT“ (bez jazyka – ten přeloží okno)."""
        if self.kind == "audio":
            return f"{self.codec.upper()} {CHANNELS.get(self.channels, f'{self.channels}ch')}"
        return {"subrip": "SRT", "hdmv_pgs_subtitle": "PGS", "dvd_subtitle": "DVD"}.get(self.codec, self.codec.upper())

    @property
    def label(self) -> str:
        """Popis do logu; v okně ho skládá JavaScript v jazyce aplikace."""
        return " · ".join(p for p in (self.lang.upper(), self.title, self.format,
                                      self.file.name if self.file else "") if p)

    def as_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "lang": self.lang if self.lang != "und" else "",
                "title": self.title, "format": self.format, "forced": self.forced, "image": self.image,
                "file": self.file.name if self.file else None}


@dataclass
class MediaInfo:
    path: Path
    duration: float
    size: int
    video: dict
    audio: list[Track] = field(default_factory=list)
    subs: list[Track] = field(default_factory=list)
    attachments: list[int] = field(default_factory=list)
    video_streams: list[int] = field(default_factory=list)

    @property
    def summary(self) -> str:
        v = self.video
        parts = []
        if w := v.get("width"):  # podle šířky, filmy bývají ořezané na výšku (1920×800)
            parts.append("4K" if w >= 3800 else "1080p" if w >= 1900 else "720p" if w >= 1260 else f"{v['height']}p")
        if v.get("codec"):
            parts.append({"h264": "H.264", "hevc": "HEVC", "av1": "AV1", "vp9": "VP9", "mpeg4": "MPEG-4"}.get(v["codec"], v["codec"].upper()))
        if v.get("dv_profile"):
            parts.append("Dolby Vision")
        elif v.get("hdr"):
            parts.append("HDR")
        return " · ".join(parts)

    def track(self, track_id: str | None) -> Track | None:
        return next((t for t in self.audio + self.subs if t.id == track_id), None)


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          creationflags=NO_WINDOW)


def external_subs(video: Path) -> list[Path]:
    """Titulky vedle videa: Film.srt, Film.cs.srt, Film_en.ass… (ne „Film 2.srt“ k „Film“)."""
    stem = video.stem.lower()
    with contextlib.suppress(OSError):
        return sorted(p for p in video.parent.iterdir()
                      if p.suffix.lower() in SUB_SUFFIXES and p.stem.lower().startswith(stem)
                      and p.stem[len(stem):len(stem) + 1] in ("", ".", "_", "-"))
    return []


def _sub_file_lang(video: Path, sub: Path) -> str:
    tag = sub.stem[len(video.stem):].strip(" ._-").lower()
    return norm_lang(tag.split(".")[0]) if tag else ""


def probe(path: Path) -> MediaInfo:
    out = _run([tool("ffprobe"), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)])
    if out.returncode:
        raise MediaError(t("err.unreadable", detail=out.stderr.strip()[-200:] or t("err.unknown_format")))
    data = json.loads(out.stdout)
    fmt = data.get("format", {})
    info = MediaInfo(path=path, duration=float(fmt.get("duration") or 0), size=path.stat().st_size, video={})
    for s in data.get("streams", []):
        kind, tags, disp = s.get("codec_type"), s.get("tags", {}), s.get("disposition", {})
        lang = norm_lang(tags.get("language", ""))
        if kind == "video" and not disp.get("attached_pic"):  # obal alba by TV mohla vzít jako video
            info.video_streams.append(s["index"])
            if not info.video:
                dovi = next((d for d in s.get("side_data_list", []) if "dv_profile" in d), {})
                info.video = {"codec": s.get("codec_name", ""), "width": s.get("width", 0), "height": s.get("height", 0),
                              "hdr": s.get("color_transfer") in ("smpte2084", "arib-std-b67"),
                              "dv_profile": dovi.get("dv_profile"), "pix_fmt": s.get("pix_fmt", "")}
        elif kind == "audio":
            info.audio.append(Track(f"a:{s['index']}", "audio", s.get("codec_name", "?"), lang, tags.get("title", ""),
                                    s.get("channels", 0), bool(disp.get("default")), index=s["index"]))
        elif kind == "subtitle":
            info.subs.append(Track(f"s:{s['index']}", "subtitle", s.get("codec_name", "?"), lang, tags.get("title", ""),
                                   default=bool(disp.get("default")), forced=bool(disp.get("forced")), index=s["index"]))
        elif kind == "attachment":
            info.attachments.append(s["index"])
    if not info.video_streams:
        raise MediaError(t("err.no_video_stream"))
    for n, sub in enumerate(external_subs(path)):
        info.subs.append(Track(f"x:{n}", "subtitle", SUB_SUFFIXES[sub.suffix.lower()], _sub_file_lang(path, sub),
                               file=sub))
    return info


def tv_default_audio(info: MediaInfo) -> Track | None:
    """Zvuk, který si TV sama vybere: první výchozí, jinak první."""
    return next((t for t in info.audio if t.default), info.audio[0] if info.audio else None)


def tv_default_sub(info: MediaInfo) -> Track | None:
    return next((t for t in info.subs if t.default and not t.file and not t.image), None)


def default_choice(info: MediaInfo, langs: list[str]) -> tuple[str | None, str]:
    """Výchozí výběr (zvuk, titulky) podle preferovaných jazyků.

    Zvuk: výchozí stopa videa, pokud není v preferovaném jazyce a nějaká jiná je.
    Titulky: jen když zvuk není v prvním preferovaném jazyce – pak titulky
    v nejlepším preferovaném jazyce (soubory vedle videa mají přednost).
    """
    langs = [norm_lang(lang) for lang in langs]
    audio = tv_default_audio(info)
    for lang in langs:
        if audio and audio.lang == lang:
            break
        if match := next((t for t in info.audio if t.lang == lang), None):
            audio = match
            break
    sub = "none"
    if not (audio and langs and audio.lang == langs[0]):
        text_subs = [t for t in info.subs if not t.image]
        for lang in langs:
            if match := next((t for t in sorted(text_subs, key=lambda t: (t.file is None, t.forced)) if t.lang == lang), None):
                sub = match.id
                break
        else:
            unknown = [t for t in text_subs if t.file and not t.lang]  # Film.srt bez jazyka – dal ho tam uživatel
            if unknown:
                sub = unknown[0].id
            elif d := tv_default_sub(info):
                sub = d.id
    return audio.id if audio else None, sub


@dataclass
class Plan:
    remux: bool
    container: str  # mkv / mp4 / původní přípona
    mime: str
    reasons: list[str]
    warnings: list[str]
    subtitle_file: Path | None = None  # titulky vedle videa, nabídnou se TV i zvlášť
    embed_subs: bool = True  # titulky do videa (DLNA); Chromecast je bere jen zvlášť
    sidecar: Track | None = None  # titulky, které se pošlou zvlášť jako WebVTT (Chromecast)
    vtt: Path | None = None  # hotový WebVTT soubor (vyplní se při přípravě)
    audio_ok: frozenset = frozenset(TV_AUDIO)  # zvuk, který přehrávač přehraje bez převodu
    error: str = ""  # video na tomto přehrávači nepůjde (obraz by se musel překódovat)

    @property
    def extract(self) -> bool:
        """Titulky pro Chromecast je potřeba vytáhnout z videa (čte celý soubor)."""
        return bool(self.sidecar and not self.sidecar.file)


def plan(info: MediaInfo, audio_id: str | None, sub_id: str, dev) -> Plan:
    """Co je potřeba udělat, aby přehrávač video přehrál s vybraným zvukem a titulky.

    dev popisuje přehrávač: accepts(mime) -> bool|None, mime_for(*mimes) -> str|None,
    dolby_vision (False = neumí, None = nevíme), embeds_subtitles (ukáže titulky uvnitř videa).
    """
    accepts, mime_for, dolby_vision_tv = dev.accepts, dev.mime_for, dev.dolby_vision
    embeds = dev.embeds_subtitles
    audio_ok = frozenset(getattr(dev, "audio_codecs", None) or TV_AUDIO)
    video_ok = getattr(dev, "video_codecs", None)
    suffix = info.path.suffix.lower()
    # MKV má víc názvů (Samsung chce video/x-mkv) – pošle se pod tím, který TV hlásí.
    aliases = MKV_MIMES if suffix == ".mkv" else (MIME.get(suffix, "video/mp4"),)
    src_mime = mime_for(*aliases) or aliases[0]
    src_accepted = True if mime_for(*aliases) else accepts(src_mime)
    reasons, warnings = [], []
    audio = info.track(audio_id)
    sub = info.track(sub_id) if sub_id != "none" else None

    if audio and audio.codec not in audio_ok:
        reasons.append(f"zvuk {audio.codec.upper()} → {audio_target(audio, audio_ok)[0].upper()}")
    if audio and audio is not tv_default_audio(info):
        reasons.append("jiná zvuková stopa")
    if any(t.image for t in info.subs) and (not sub or sub.image) and all(t.image for t in info.subs if t is not sub):
        warnings.append(t("warn.image_subs", name=f"{info.path.stem}.srt"))
    if sub and sub.image:
        sub = None
    if not embeds:
        pass  # Chromecast: titulky jdou zvlášť, kvůli nim se video nepřebaluje
    elif sub and sub.file:
        reasons.append(f"titulky {sub.file.name}")
    elif sub and sub is not tv_default_sub(info):
        reasons.append("jiné titulky")
    elif not sub and tv_default_sub(info):
        reasons.append("bez titulků")
    error = ""
    if video_ok and (vcodec := info.video.get("codec")) not in video_ok:
        names = {"h264": "H.264", "hevc": "HEVC (H.265)", "vp8": "VP8", "vp9": "VP9", "av1": "AV1"}
        error = t("err.video_codec", name=dev.name, codec=names.get(vcodec, vcodec.upper()),
                  codecs=", ".join(names.get(c, c) for c in sorted(video_ok)))
    # Dolby Vision zapne LG (ověřeno na CX) jen u MP4 se záznamem dvcC – z MKV a TS hraje jen HDR10.
    # TV bez Dolby Vision přehraje profil 8 jako HDR10, přebalování je zbytečné. Profil 5 HDR10
    # základ nemá – bez Dolby Vision bude mít špatné barvy a pomohlo by jen překódování.
    dv = dolby_vision(info) and accepts("video/mp4") is not False and dolby_vision_tv is not False
    if dolby_vision(info) and dolby_vision_tv is False and info.video.get("dv_profile") == 5:
        warnings.append(t("warn.dv5"))
    if dv and suffix not in (".mp4", ".m4v"):
        reasons.append("Dolby Vision → MP4")
    if sub and sub.codec == "mov_text" and not dv and embeds:
        reasons.append("titulky MP4 → SRT")
    if src_accepted is False:
        reasons.append(f"{'Chromecast' if dev.kind == 'cast' else 'TV'} nebere {suffix}")

    extra = {"subtitle_file": sub.file if sub and sub.file else None, "embed_subs": embeds,
             "sidecar": None if embeds else sub, "audio_ok": audio_ok, "error": error}
    if not reasons:
        return Plan(False, suffix[1:], src_mime, [], warnings, **extra)
    mkv = mime_for(*MKV_MIMES)
    if (mkv or accepts("video/mp4") is None) and not dv:
        return Plan(True, "mkv", mkv or "video/x-matroska", reasons, warnings, **extra)
    return Plan(True, "mp4", mime_for("video/mp4") or "video/mp4", reasons, warnings, **extra)


def audio_target(track: Track, audio_ok: frozenset) -> tuple[str, str, int]:
    """(kodek, datový tok, kanály) pro převod zvuku: vícekanálový do AC3 5.1, umí-li ho
    přehrávač, jinak do AAC stereo (Nest Hub a podobné displeje Dolby zvuk nedekódují)."""
    if track.channels > 2 and "ac3" in audio_ok:
        return "ac3", "640k", min(track.channels, 6)
    return "aac", "256k", min(track.channels or 2, 2)


def dolby_vision(info: MediaInfo) -> bool:
    """Dolby Vision s HDR10/SDR základem v jedné stopě (profil 5 a 8), který jde přebalit do MP4."""
    return info.video.get("codec") == "hevc" and info.video.get("dv_profile") in (5, 8)


def sub_charenc(path: Path) -> str:
    try:
        path.read_bytes().decode("utf-8")
        return "UTF-8"
    except UnicodeDecodeError:
        return "CP1250"  # české titulky ze staršího Windows


def ffmpeg_args(info: MediaInfo, audio_id: str | None, sub_id: str, p: Plan, out: Path,
                vtt_out: Path | None = None) -> list[str]:
    """Příkaz ffmpeg pro přebalení. vtt_out: titulky pro Chromecast se vytáhnou ve stejném běhu."""
    audio = info.track(audio_id)
    sub = info.track(sub_id) if sub_id != "none" else None
    if sub and sub.image:
        sub = None
    mkv = p.container == "mkv"
    cmd = [tool("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", str(info.path)]
    if sub and sub.file and p.embed_subs:
        cmd += ["-sub_charenc", sub_charenc(sub.file), "-i", str(sub.file)]
    maps, opts = [], ["-c", "copy"]
    for idx in info.video_streams:
        maps += ["-map", f"0:{idx}"]
    # Vybraný zvuk první a výchozí, ostatní za ním (dají se přepnout na TV).
    audios = sorted(info.audio, key=lambda t: t is not audio)
    for n, track in enumerate(audios):
        maps += ["-map", f"0:{track.index}"]
        if track.codec not in p.audio_ok:
            codec, bitrate, channels = audio_target(track, p.audio_ok)
            opts += [f"-c:a:{n}", codec, f"-b:a:{n}", bitrate, f"-ac:a:{n}", str(channels)]
        opts += [f"-disposition:a:{n}", "default" if track is audio else "0"]
    # Titulky: vybrané první a výchozí; obrázkové do MP4 nejdou. Chromecast je bere jen zvlášť.
    subs = [t for t in info.subs if not t.file and (mkv or not t.image)] if p.embed_subs else []
    if sub and not sub.file:
        subs.sort(key=lambda t: t is not sub)
    n = 0
    if sub and sub.file and p.embed_subs:
        maps += ["-map", "1:0"]
        opts += [f"-c:s:{n}", (SUB_SUFFIXES[sub.file.suffix.lower()] if mkv else "mov_text"), f"-disposition:s:{n}", "default"]
        if sub.lang:
            opts += [f"-metadata:s:s:{n}", f"language={sub.lang}"]
        n += 1
    for track in subs:
        maps += ["-map", f"0:{track.index}"]
        if track.codec == "mov_text" and mkv:
            opts += [f"-c:s:{n}", "srt"]
        elif not mkv and track.codec != "mov_text":
            opts += [f"-c:s:{n}", "mov_text"]
        opts += [f"-disposition:s:{n}", "default" if track is sub else "0"]
        n += 1
    if mkv:
        for idx in info.attachments:  # písma pro titulky ASS
            maps += ["-map", f"0:{idx}"]
    elif dolby_vision(info):
        # Záznam dvcC zapíše ffmpeg do MP4 jen s -strict unofficial; hvc1 přehraje i TV bez Dolby Vision.
        opts += ["-strict", "unofficial", "-tag:v", "hvc1"]
    args = cmd + maps + opts + ["-progress", "pipe:1", "-nostats", str(out)]
    if vtt_out and sub and not sub.file:
        args += ["-map", f"0:{sub.index}", "-c:s", "webvtt", str(vtt_out)]
    return args


async def _ffmpeg(args: list[str], duration: float, on_progress, outputs: list[Path], what: str) -> None:
    """Spustí ffmpeg; on_progress(0..1) průběžně hlásí postup. Při chybě/zrušení výstupy smaže."""
    TEMP.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, creationflags=NO_WINDOW)
    try:
        async for line in proc.stdout:
            key, _, value = line.decode(errors="replace").strip().partition("=")
            if key == "out_time_us" and value.isdigit() and duration:
                on_progress(min(1.0, int(value) / 1e6 / duration))
        err = (await proc.stderr.read()).decode(errors="replace").strip()
        if await proc.wait():
            raise MediaError(f"{what}: {err[-300:]}")
    except BaseException:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        for out in outputs:
            with contextlib.suppress(OSError):
                out.unlink()
        raise


async def remux(info: MediaInfo, audio_id: str | None, sub_id: str, p: Plan, out: Path, on_progress,
                vtt_out: Path | None = None) -> None:
    args = ffmpeg_args(info, audio_id, sub_id, p, out, vtt_out)
    await _ffmpeg(args, info.duration, on_progress, [out] + ([vtt_out] if vtt_out else []), t("err.ffmpeg_remux"))


async def extract_vtt(info: MediaInfo, track: Track, out: Path, on_progress) -> None:
    """Titulky z videa jako WebVTT (Chromecast) – ffmpeg přitom přečte celý soubor."""
    args = [tool("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", str(info.path),
            "-map", f"0:{track.index}", "-c:s", "webvtt", "-progress", "pipe:1", "-nostats", str(out)]
    await _ffmpeg(args, info.duration, on_progress, [out], t("err.ffmpeg_subs"))


def clean_temp() -> None:
    """Úklid po minulém běhu (soubor, který zrovna jinde běží, smazat nejde)."""
    if TEMP.exists():
        for old in TEMP.iterdir():
            with contextlib.suppress(OSError):
                old.unlink()


def subtitle_utf8(src: Path, key: str, fmt: str = "srt") -> Path:
    """Kopie titulků v UTF-8 pro přehrávače, které je berou zvlášť: SRT (Samsung) nebo WebVTT (Chromecast)."""
    out = TEMP / f"{key}.{'vtt' if fmt == 'webvtt' else fmt}"
    TEMP.mkdir(parents=True, exist_ok=True)
    r = _run([tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-sub_charenc", sub_charenc(src),
              "-i", str(src), "-c:s", fmt, str(out)])
    if r.returncode:
        raise MediaError(t("err.subs_convert", detail=r.stderr.strip()[-200:]))
    return out
