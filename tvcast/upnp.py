"""UPnP/DLNA: hledání přehrávačů (MediaRenderer) v síti a jejich ovládání.

Televize (LG, Samsung, Sony, Philips, Panasonic…) i další přehrávače se ohlásí na
SSDP dotaz. Ze zařízení se berou jen ty, které umí video (podle ConnectionManager
GetProtocolInfo), takže třeba Sonos se nenabídne.
"""

import asyncio
import contextlib
import logging
import re
import socket
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree
from xml.sax.saxutils import escape, quoteattr

import aiohttp

log = logging.getLogger(__name__)

SSDP = ("239.255.255.250", 1900)
SEARCH_TARGETS = ("urn:schemas-upnp-org:device:MediaRenderer:1", "urn:schemas-upnp-org:service:AVTransport:1")
NS = {"d": "urn:schemas-upnp-org:device-1-0"}
# Streamování s přetáčením (Range); bez toho některé TV soubor „nerozpoznají“.
DLNA_FEATURES = "DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000"


class UpnpError(Exception):
    """Přehrávač požadavek odmítl (UPnP chyba)."""

    def __init__(self, action: str, code: str, description: str) -> None:
        super().__init__(f"{action}: {description or 'chyba'} ({code})")
        self.code = code
        self.description = description


@dataclass
class Status:
    """Stav přehrávače společný pro DLNA i Chromecast."""

    state: str  # playing / paused / loading / stopped / error
    position: float | None = None
    duration: float | None = None
    uri: str = ""  # co přehrávač právě hraje ("" = nehlásí)
    finished: bool | None = None  # dohrálo do konce? None = přehrávač to nehlásí


DLNA_STATES = {"PLAYING": "playing", "PAUSED_PLAYBACK": "paused", "TRANSITIONING": "loading"}


@dataclass
class Device:
    """DLNA přehrávač. Stejné rozhraní (load, status, …) má i cast.CastDevice."""

    udn: str
    name: str
    host: str
    location: str
    manufacturer: str = ""
    model: str = ""
    services: dict[str, tuple[str, str]] = field(default_factory=dict)  # AVTransport → (serviceType, controlURL)
    sink: list[str] = field(default_factory=list)  # protocolInfo, které přehrávač přijme
    seen: float = 0.0
    upnp: "Upnp | None" = field(default=None, repr=False, compare=False)

    kind = "dlna"
    subtitle_mime = "text/srt"  # titulky zvlášť (Samsung); jinak se vkládají do MKV
    embeds_subtitles = True  # titulky uvnitř MKV/MP4 TV ukáže

    @property
    def caps_key(self) -> tuple:
        return self.kind, tuple(self.sink), self.manufacturer

    async def load(self, url: str, title: str, mime: str, subtitle_url: str | None = None,
                   subtitle_lang: str = "", start_at: float = 0) -> None:
        await self.upnp.load(self, url, title, mime, subtitle_url)  # od místa start_at se přetočí až po rozehrání

    async def play(self) -> None:
        await self.upnp.play(self)

    async def pause(self) -> None:
        await self.upnp.pause(self)

    async def stop(self) -> None:
        await self.upnp.stop(self)

    async def seek(self, seconds: float) -> None:
        await self.upnp.seek(self, seconds)

    async def status(self) -> Status:
        state, status = await self.upnp.transport(self)
        pos, dur, uri = await self.upnp.position(self)
        mapped = "error" if status == "ERROR_OCCURRED" else DLNA_STATES.get(state, "stopped")
        return Status(mapped, pos, dur, uri)

    async def volume(self) -> tuple[int | None, bool | None]:
        return await self.upnp.volume(self)

    async def set_volume(self, value: int) -> None:
        await self.upnp.set_volume(self, value)

    async def set_mute(self, mute: bool) -> None:
        await self.upnp.set_mute(self, mute)

    def accepts(self, mime: str) -> bool | None:
        """Přijme přehrávač tento typ souboru? None = nehlásí to."""
        if not self.sink:
            return None
        return any(p.split(":")[2].lower() == mime.lower() for p in self.sink if p.count(":") >= 3)

    @property
    def dolby_vision(self) -> bool | None:
        """Umí TV Dolby Vision? Přes DLNA to nehlásí; Samsung ho nemá nikdy, u ostatních nevíme."""
        return False if "samsung" in self.manufacturer.lower() else None

    def mime_for(self, *candidates: str) -> str | None:
        """První z kandidátů, který přehrávač zná – v jeho zápisu (Samsung chce video/x-mkv)."""
        listed = {p.split(":")[2].lower(): p.split(":")[2] for p in self.sink if p.count(":") >= 3}
        for c in candidates:
            if c.lower() in listed:
                return listed[c.lower()]
        return None


def local_addresses() -> list[str]:
    """IPv4 adresy tohoto PC v místních sítích (bez linkových a smyčky)."""
    addrs = {ai[4][0] for ai in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    return sorted(a for a in addrs if not a.startswith(("127.", "169.254.")))


def local_ip_for(host: str) -> str:
    """Adresa tohoto PC, pod kterou ho zařízení vidí."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((host, 9))
        return s.getsockname()[0]


def _ssdp_search(timeout: float) -> set[str]:
    """Pošle M-SEARCH ze všech síťových rozhraní (jinak by šel jen do výchozí sítě,
    třeba do WSL) a vrátí adresy popisů zařízení."""
    socks = []
    for addr in local_addresses():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(addr))
            s.bind((addr, 0))
            s.setblocking(False)
            for st in SEARCH_TARGETS:
                msg = (f"M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
                       f"MX: 2\r\nST: {st}\r\n\r\n").encode()
                s.sendto(msg, SSDP)
            socks.append(s)
        except OSError:
            s.close()
    locations = set()
    deadline = time.monotonic() + timeout
    try:
        import select
        while socks and (left := deadline - time.monotonic()) > 0:
            ready, _, _ = select.select(socks, [], [], left)
            for s in ready:
                with contextlib.suppress(OSError):
                    data = s.recv(4096)
                    if m := re.search(rb"(?im)^location:\s*(\S+)", data):
                        locations.add(m.group(1).decode(errors="replace"))
    finally:
        for s in socks:
            s.close()
    return locations


def _service_key(service_type: str) -> str:
    # urn:schemas-upnp-org:service:AVTransport:1 → AVTransport
    parts = service_type.split(":")
    return parts[-2] if len(parts) >= 2 else service_type


def _parse_description(location: str, xml: bytes) -> Device | None:
    root = ElementTree.fromstring(xml)
    base = root.findtext("d:URLBase", namespaces=NS) or location
    for dev in root.iter(f"{{{NS['d']}}}device"):
        services = {}
        for svc in dev.findall("d:serviceList/d:service", NS):
            stype = svc.findtext("d:serviceType", "", NS)
            services[_service_key(stype)] = (stype, urljoin(base, svc.findtext("d:controlURL", "", NS)))
        if "AVTransport" in services:
            return Device(
                udn=dev.findtext("d:UDN", "", NS) or location,
                name=(dev.findtext("d:friendlyName", "", NS) or "").strip() or urlparse(location).hostname,
                host=urlparse(location).hostname or "",
                location=location,
                manufacturer=(dev.findtext("d:manufacturer", "", NS) or "").strip(),
                model=(dev.findtext("d:modelName", "", NS) or "").strip(),
                services=services,
            )
    return None


class Upnp:
    """SOAP volání na přehrávač."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session

    async def call(self, dev: Device, service: str, action: str, args: dict[str, str] | None = None,
                   timeout: float = 8) -> dict[str, str]:
        stype, url = dev.services[service]
        body = "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in (args or {}).items())
        xml = ('<?xml version="1.0" encoding="utf-8"?>'
               '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
               's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
               f'<u:{action} xmlns:u="{stype}">{body}</u:{action}></s:Body></s:Envelope>')
        headers = {"Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{stype}#{action}"'}
        async with self.session.post(url, data=xml.encode(), headers=headers,
                                     timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            text = await r.text(errors="replace")
            if r.status >= 400:
                code = re.search(r"<errorCode>(\d+)</errorCode>", text)
                desc = re.search(r"<errorDescription>(.*?)</errorDescription>", text, re.S)
                raise UpnpError(action, code.group(1) if code else str(r.status),
                                desc.group(1).strip() if desc else "")
        out = {}
        with contextlib.suppress(ElementTree.ParseError):
            for el in ElementTree.fromstring(text).iter():
                if not list(el):
                    out[el.tag.rsplit("}", 1)[-1]] = (el.text or "").strip()
        return out

    async def describe(self, location: str) -> Device | None:
        async with self.session.get(location, timeout=aiohttp.ClientTimeout(total=4)) as r:
            dev = _parse_description(location, await r.read())
        if dev and "ConnectionManager" in dev.services:
            with contextlib.suppress(Exception):
                info = await self.call(dev, "ConnectionManager", "GetProtocolInfo", timeout=4)
                dev.sink = [p.strip() for p in info.get("Sink", "").split(",") if p.strip()]
        return dev

    # --- AVTransport ---

    async def load(self, dev: Device, url: str, title: str, mime: str, subtitle_url: str | None = None) -> None:
        # Přehrávač, který ještě drží předchozí video (nebo jeho chybový dialog), nové
        # nemusí přijmout (701 „Transition not available“) – proto ho nejdřív zastavíme.
        with contextlib.suppress(Exception):
            await self.call(dev, "AVTransport", "Stop", {"InstanceID": "0"}, timeout=3)
        meta = didl(title, url, mime, subtitle_url)
        for attempt in range(2):
            try:
                await self.call(dev, "AVTransport", "SetAVTransportURI",
                                {"InstanceID": "0", "CurrentURI": url, "CurrentURIMetaData": meta})
                break
            except UpnpError as e:
                if e.code != "701" or attempt:
                    raise
                await asyncio.sleep(1.5)
        await self.play(dev)

    async def play(self, dev: Device) -> None:
        await self.call(dev, "AVTransport", "Play", {"InstanceID": "0", "Speed": "1"}, timeout=15)

    async def pause(self, dev: Device) -> None:
        await self.call(dev, "AVTransport", "Pause", {"InstanceID": "0"})

    async def stop(self, dev: Device) -> None:
        await self.call(dev, "AVTransport", "Stop", {"InstanceID": "0"}, timeout=4)

    async def seek(self, dev: Device, seconds: float) -> None:
        await self.call(dev, "AVTransport", "Seek", {"InstanceID": "0", "Unit": "REL_TIME", "Target": hms(seconds)})

    async def transport(self, dev: Device) -> tuple[str, str]:
        r = await self.call(dev, "AVTransport", "GetTransportInfo", {"InstanceID": "0"}, timeout=4)
        return r.get("CurrentTransportState", ""), r.get("CurrentTransportStatus", "")

    async def position(self, dev: Device) -> tuple[float | None, float | None, str]:
        """(pozice, délka, adresa přehrávaného videa)."""
        r = await self.call(dev, "AVTransport", "GetPositionInfo", {"InstanceID": "0"}, timeout=4)
        return seconds(r.get("RelTime")), seconds(r.get("TrackDuration")), r.get("TrackURI", "")

    # --- RenderingControl ---

    async def volume(self, dev: Device) -> tuple[int | None, bool | None]:
        if "RenderingControl" not in dev.services:
            return None, None
        args = {"InstanceID": "0", "Channel": "Master"}
        vol = mute = None
        with contextlib.suppress(Exception):
            vol = int((await self.call(dev, "RenderingControl", "GetVolume", args, timeout=3)).get("CurrentVolume", ""))
        with contextlib.suppress(Exception):
            mute = (await self.call(dev, "RenderingControl", "GetMute", args, timeout=3)).get("CurrentMute") in ("1", "true")
        return vol, mute

    async def set_volume(self, dev: Device, value: int) -> None:
        await self.call(dev, "RenderingControl", "SetVolume",
                        {"InstanceID": "0", "Channel": "Master", "DesiredVolume": str(value)})

    async def set_mute(self, dev: Device, mute: bool) -> None:
        await self.call(dev, "RenderingControl", "SetMute",
                        {"InstanceID": "0", "Channel": "Master", "DesiredMute": "1" if mute else "0"})


class Discovery:
    """Udržuje seznam přehrávačů v síti: pravidelně je hledá a nedostupné vyřadí."""

    INTERVAL = 30
    FORGET_AFTER = 75

    def __init__(self, upnp: Upnp, on_change) -> None:
        self.upnp = upnp
        self.on_change = on_change
        self.devices: dict[str, Device] = {}
        self.searching = False
        self._wake = asyncio.Event()
        self._own = set(local_addresses())

    def refresh(self) -> None:
        self._wake.set()

    async def run(self) -> None:
        while True:
            await self.search()
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self.INTERVAL)

    async def search(self) -> None:
        self.searching = True
        self.on_change()
        try:
            locations = await asyncio.to_thread(_ssdp_search, 3)
            known = {d.location: d for d in self.devices.values()}
            now = time.monotonic()

            async def check(location: str) -> None:
                if location in known:
                    known[location].seen = now
                    return
                try:
                    dev = await self.upnp.describe(location)
                except Exception as e:  # noqa: BLE001 - cizí zařízení v síti
                    log.debug("Popis %s nejde načíst: %s", location, e)
                    return
                if not dev or dev.host in self._own:
                    return  # třeba přehrávač samotných Windows
                if dev.sink and not any(":video/" in p.lower() for p in dev.sink):
                    return  # jen zvuk (Sonos, reproduktory)
                dev.seen = now
                dev.upnp = self.upnp
                self.devices[dev.udn] = dev
                log.info("Nalezen přehrávač %s (%s %s, %s)", dev.name, dev.manufacturer, dev.model, dev.host)

            await asyncio.gather(*(check(loc) for loc in locations))
            # Kdo se tentokrát neozval, toho zkusíme přímo: ukončená aplikace (Kodi) odmítne spojení
            # hned a nemusí 75 s strašit v seznamu jako dostupná. Chromecasty se odhlašují samy.
            missing = [d for d in self.devices.values() if d.kind == "dlna" and d.seen < now]
            await asyncio.gather(*(self._probe(d, now) for d in missing))
            for udn, dev in list(self.devices.items()):
                if dev.kind == "dlna" and now - dev.seen > self.FORGET_AFTER:
                    del self.devices[udn]
        finally:
            self.searching = False
            self.on_change()


    async def _probe(self, dev: Device, now: float) -> None:
        """Přehrávač, který neodpověděl na hledání: odpoví-li do 2 s jeho popis, žije; jinak
        (odmítnuté spojení, ticho) se hned vyřadí. Byl-li to jen výpadek sítě, vrátí se při
        příštím hledání (do 30 s)."""
        try:
            async with self.upnp.session.get(dev.location, timeout=aiohttp.ClientTimeout(total=2)) as r:
                if r.status < 400:
                    dev.seen = now
                    return
        except Exception:  # noqa: BLE001 - odmítnuto, vypršel čas, síť nedostupná
            pass
        log.info("Přehrávač %s neodpovídá – vyřazen ze seznamu", dev.name)
        self.devices.pop(dev.udn, None)

    def forget_if_gone(self, dev: Device) -> None:
        """Volá přehrávání při chybě spojení: hned ověřit, jestli přehrávač ještě žije."""
        if dev.kind == "dlna":
            asyncio.create_task(self._recheck(dev))

    async def _recheck(self, dev: Device) -> None:
        await self._probe(dev, time.monotonic())
        self.on_change()


def didl(title: str, url: str, mime: str, subtitle_url: str | None = None) -> str:
    """Popis videa pro SetAVTransportURI. Titulky navíc jako samostatný soubor:
    Samsung (sec:CaptionInfoEx) i další TV, které berou druhý <res> s text/srt."""
    sub = ""
    if subtitle_url:
        sub = (f'<res protocolInfo="http-get:*:text/srt:*">{escape(subtitle_url)}</res>'
               f'<sec:CaptionInfoEx sec:type="srt">{escape(subtitle_url)}</sec:CaptionInfoEx>'
               f'<sec:CaptionInfo sec:type="srt">{escape(subtitle_url)}</sec:CaptionInfo>')
    return ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
            'xmlns:sec="http://www.sec.co.kr/">'
            f'<item id="0" parentID="-1" restricted="1"><dc:title>{escape(title)}</dc:title>'
            '<upnp:class>object.item.videoItem</upnp:class>'
            f'<res protocolInfo={quoteattr(f"http-get:*:{mime}:{DLNA_FEATURES}")}>{escape(url)}</res>'
            f'{sub}</item></DIDL-Lite>')


def hms(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 3600}:{sec // 60 % 60:02d}:{sec % 60:02d}"


def seconds(value: str | None) -> float | None:
    if not value or not (m := re.fullmatch(r"(\d+):(\d{1,2}):(\d{1,2})(?:\.\d+)?", value.strip())):
        return None
    h, mi, s = map(int, m.groups())
    return h * 3600 + mi * 60 + s
