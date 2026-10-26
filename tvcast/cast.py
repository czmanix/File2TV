"""Chromecast (Google Cast): televize s Google TV, Nvidia Shield, Chromecast…

Video přehrává výchozí přehrávač Googlu (Default Media Receiver) – na všech zařízeních
stejný, proto bere jen MP4/WebM a titulky jen zvlášť jako WebVTT. Hledání přes mDNS
(pychromecast); spojení se zařízením se otevře až při prvním použití.
"""

import asyncio
import contextlib
import logging
import time

import pychromecast
from pychromecast.controllers.media import MEDIA_PLAYER_STATE_BUFFERING, MEDIA_PLAYER_STATE_PAUSED, \
    MEDIA_PLAYER_STATE_PLAYING
from pychromecast.discovery import CastBrowser, SimpleCastListener

from .upnp import Status

log = logging.getLogger(__name__)

MIMES = ("video/mp4", "video/webm")
DEFAULT_RECEIVER = "CC1AD845"
STATES = {MEDIA_PLAYER_STATE_PLAYING: "playing", MEDIA_PLAYER_STATE_PAUSED: "paused",
          MEDIA_PLAYER_STATE_BUFFERING: "loading"}
# Jazyky titulků pro přehrávač (BCP 47) z kódů ffprobe.
BCP47 = {"cze": "cs", "slo": "sk", "eng": "en", "ger": "de", "fre": "fr", "spa": "es", "ita": "it", "pol": "pl",
         "rus": "ru", "hun": "hu", "jpn": "ja", "kor": "ko", "chi": "zh"}
NO_DOLBY_VISION = ("SHIELD Android TV", "Google Nest Hub", "Google Nest Hub Max", "Chromecast")
# Co umí zařízení, která to o sobě nehlásí (Chromecast nemá obdobu DLNA GetProtocolInfo).
# Nest Hub (ověřeno): HEVC nepřehraje, Dolby zvuk (AC3/E-AC3) nedekóduje – hraje bez zvuku.
# „Puk“ (1.–3. generace) umí jen H.264; Dolby zvuk pošle do TV přes HDMI.
SMALL_AUDIO = frozenset({"aac", "mp3", "opus", "vorbis", "flac"})
MODEL_CAPS = {
    "Google Nest Hub": ({"h264", "vp8", "vp9"}, SMALL_AUDIO),
    "Google Nest Hub Max": ({"h264", "vp8", "vp9"}, SMALL_AUDIO),
    "Chromecast": ({"h264", "vp8"}, SMALL_AUDIO | {"ac3", "eac3"}),
}


class CastDevice:
    kind = "cast"
    subtitle_mime = "text/vtt"
    embeds_subtitles = False  # titulky uvnitř videa přehrávač Googlu neukáže
    location = ""
    sink: list[str] = []

    def __init__(self, info, zconf) -> None:
        self.info = info
        self.zconf = zconf
        self.udn = f"cast:{info.uuid}"
        self.name = info.friendly_name or "Chromecast"
        self.host = info.host
        self.manufacturer = info.manufacturer or "Google"
        self.model = f"{info.model_name or 'Chromecast'} · Chromecast"
        self.seen = time.monotonic()
        self._cc: pychromecast.Chromecast | None = None
        self._lock = asyncio.Lock()
        self._loaded = ""  # adresa videa, které jsme poslali

    # --- co zařízení umí ---

    @property
    def caps_key(self) -> tuple:
        return self.kind, self.info.model_name

    def accepts(self, mime: str) -> bool:
        return mime.lower() in MIMES

    def mime_for(self, *candidates: str) -> str | None:
        return next((c for c in candidates if c.lower() in MIMES), None)

    @property
    def dolby_vision(self) -> bool | None:
        # Shield (ověřeno, Dolby Vision v nastavení zapnuté) přes Chromecast pošle jen HDR10 –
        # u MP4 s dvh1 i hvc1; Dolby Vision tam umí jen aplikace (Netflix, Plex…). Jinde nevíme.
        return False if self.info.model_name in NO_DOLBY_VISION else None

    @property
    def video_codecs(self) -> set[str] | None:
        """Kodeky obrazu, které zařízení přehraje; None = nevíme (TV s Google TV, Shield…)."""
        caps = MODEL_CAPS.get(self.info.model_name)
        return caps[0] if caps else None

    @property
    def audio_codecs(self) -> frozenset[str] | None:
        """Kodeky zvuku; None = běžná sada TV (media.TV_AUDIO)."""
        caps = MODEL_CAPS.get(self.info.model_name)
        return frozenset(caps[1]) if caps else None

    # --- spojení ---

    async def _connect(self) -> pychromecast.Chromecast:
        async with self._lock:
            if self._cc and self._cc.socket_client.is_connected:
                return self._cc

            def connect():
                cc = pychromecast.get_chromecast_from_cast_info(self.info, self.zconf, tries=2, timeout=8)
                cc.wait(timeout=10)
                return cc

            self._cc = await asyncio.to_thread(connect)
            return self._cc

    def disconnect(self) -> None:
        if self._cc:
            with contextlib.suppress(Exception):
                self._cc.disconnect(timeout=2)
            self._cc = None

    async def _media(self):
        return (await self._connect()).media_controller

    # --- ovládání ---

    async def load(self, url: str, title: str, mime: str, subtitle_url: str | None = None,
                   subtitle_lang: str = "", start_at: float = 0) -> None:
        cc = await self._connect()
        if cc.status and cc.status.app_id == DEFAULT_RECEIVER:
            # Přehrávač schovaný tlačítkem Zpět (Shield) hraje dál jen zvuk a obraz visí v BUFFERING –
            # proto ho zavřeme a play_media ho spustí znovu na popředí.
            await asyncio.to_thread(cc.quit_app)
            for _ in range(20):
                if not cc.status or cc.status.app_id != DEFAULT_RECEIVER:
                    break
                await asyncio.sleep(0.25)
        mc = cc.media_controller
        self._loaded = url
        await asyncio.to_thread(
            mc.play_media, url, mime, title=title, stream_type="BUFFERED", current_time=start_at or None,
            subtitles=subtitle_url, subtitles_lang=BCP47.get(subtitle_lang, subtitle_lang or "cs"),
            subtitles_mime="text/vtt")
        await asyncio.to_thread(mc.block_until_active, 20)

    async def play(self) -> None:
        await asyncio.to_thread((await self._media()).play)

    async def pause(self) -> None:
        await asyncio.to_thread((await self._media()).pause)

    async def stop(self) -> None:
        await asyncio.to_thread((await self._media()).stop)

    async def seek(self, seconds: float) -> None:
        await asyncio.to_thread((await self._media()).seek, seconds)

    async def status(self) -> Status:
        cc = await self._connect()
        # Stav se musí vyžádat: po přetočení Shield nahlásí jen BUFFERING a dál mlčí.
        # Odpověď dorazí do příštího dotazu (přehrávač se ptá každou sekundu).
        with contextlib.suppress(Exception):
            await asyncio.to_thread(cc.media_controller.update_status)
        if cc.status and cc.status.app_id and cc.status.app_id != DEFAULT_RECEIVER:
            return Status("stopped", uri="jiná aplikace")  # třeba Netflix spuštěný z ovladače
        s = cc.media_controller.status
        if s.content_id and s.content_id != self._loaded:
            # Stav předchozího videa (přehrávač se teprve spouští), nebo video poslané odjinud.
            return Status("loading", uri=s.content_id)
        if s.player_state == "IDLE":
            if s.idle_reason == "ERROR":
                return Status("error", uri=s.content_id or "")
            return Status("stopped", s.adjusted_current_time, s.duration, s.content_id or "",
                          finished=s.idle_reason == "FINISHED" if s.idle_reason else None)
        return Status(STATES.get(s.player_state, "loading"), s.adjusted_current_time, s.duration, s.content_id or "")

    async def volume(self) -> tuple[int | None, bool | None]:
        cc = await self._connect()
        if not cc.status:
            return None, None
        return round(cc.status.volume_level * 100), cc.status.volume_muted

    async def set_volume(self, value: int) -> None:
        cc = await self._connect()
        await asyncio.to_thread(cc.set_volume, max(0, min(100, value)) / 100)

    async def set_mute(self, mute: bool) -> None:
        cc = await self._connect()
        await asyncio.to_thread(cc.set_volume_muted, mute)


class CastDiscovery:
    """Hledá Chromecasty a přidává je do společného seznamu přehrávačů (Discovery.devices)."""

    def __init__(self, devices: dict, on_change) -> None:
        self.devices = devices
        self.on_change = on_change
        self.loop = asyncio.get_running_loop()
        self.zconf = None
        self.browser: CastBrowser | None = None
        self._forget: dict = {}  # uuid → naplánované vyřazení (asyncio.TimerHandle)

    def start(self) -> None:
        import zeroconf

        self.zconf = zeroconf.Zeroconf()
        listener = SimpleCastListener(lambda uuid, _service: self._threadsafe(self._add, uuid),
                                      lambda uuid, _service, _info: self._threadsafe(self._remove, uuid),
                                      lambda uuid, _service: self._threadsafe(self._add, uuid))
        self.browser = CastBrowser(listener, self.zconf)
        self.browser.start_discovery()

    def stop(self) -> None:
        for dev in list(self.devices.values()):
            if isinstance(dev, CastDevice):
                dev.disconnect()
        if self.browser:
            with contextlib.suppress(Exception):
                self.browser.stop_discovery()
        if self.zconf:
            with contextlib.suppress(Exception):
                self.zconf.close()

    def _threadsafe(self, fn, *args) -> None:
        self.loop.call_soon_threadsafe(fn, *args)

    def _add(self, uuid) -> None:
        if pending := self._forget.pop(uuid, None):
            pending.cancel()
        info = self.browser.devices.get(uuid)
        # Jen zařízení s obrazem: skupiny a reproduktory (cast_type audio/group) se nenabízí.
        if not info or info.cast_type != "cast":
            return
        udn = f"cast:{uuid}"
        if udn in self.devices:
            self.devices[udn].info = info
            self.devices[udn].host = info.host
            return
        self.devices[udn] = CastDevice(info, self.zconf)
        log.info("Nalezen Chromecast %s (%s %s, %s)", info.friendly_name, info.manufacturer, info.model_name, info.host)
        self.on_change()

    def _remove(self, uuid) -> None:
        # Při zavření a spuštění přehrávače se Chromecast v mDNS na chvíli odhlásí a hned vrátí –
        # vyřadí se, až když se do 30 s neozve (jinak by se přerušilo právě spouštěné video).
        if f"cast:{uuid}" in self.devices and uuid not in self._forget:
            self._forget[uuid] = self.loop.call_later(30, self._drop, uuid)

    def _drop(self, uuid) -> None:
        self._forget.pop(uuid, None)
        if dev := self.devices.pop(f"cast:{uuid}", None):
            dev.disconnect()
            log.info("Chromecast %s odešel ze sítě", dev.name)
            self.on_change()
