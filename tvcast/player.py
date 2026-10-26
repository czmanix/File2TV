"""Fronta videí a jejich přehrávání na vybraném přehrávači."""

import asyncio
import contextlib
import itertools
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import i18n, media, winutil
from .i18n import t
from .server import MediaServer
from .upnp import Device, Discovery, UpnpError, local_ip_for

log = logging.getLogger(__name__)

ACTIVE = ("playing", "paused", "loading")
START_TIMEOUT = 25  # s, než TV video rozehraje
WAKE_TIMEOUT = 45  # s, než se vypnutá TV po Wake-on-LAN ohlásí v síti
STALL_AFTER = 10  # s „přehrávání“ bez posunu pozice, než aplikace upozorní
_ids = itertools.count(1)


def _can_wake(udn: str, known: dict) -> bool:
    """Zapnutí přes Wake-on-LAN zkusíme jen u DLNA TV se známou MAC adresou. Chromecasty
    (Google TV, Shield…) bývají v pohotovosti dosažitelné a TV si zapnou samy přes HDMI-CEC;
    jestli TV na WoL zareaguje, záleží na jejím nastavení – ověřit to předem nejde."""
    return bool(known.get("mac")) and not udn.startswith("cast:")


def _is_kodi(dev) -> bool:
    return "kodi" in f"{dev.manufacturer} {dev.model}".lower() or "xbmc" in dev.manufacturer.lower()


def _reason(e: UpnpError) -> str:
    return e.description or t("err.code", code=e.code)


def _is_temp(path: Path) -> bool:
    """Soubor v pracovní složce aplikace (přebalené video, titulky) – smí se mazat."""
    try:
        return path.resolve().parent == media.TEMP.resolve()
    except OSError:
        return False


def _device_rank(dev) -> int:
    """Pořadí pro automatický výběr: TV přes DLNA, pak Chromecast/Google TV/Shield, displeje Nest Hub poslední."""
    if dev.kind == "dlna":
        return 0
    return 2 if "hub" in dev.model.lower() else 1


class PlayError(Exception):
    def __init__(self, text: str, action: str | None = None) -> None:
        super().__init__(text)
        self.action = action  # tlačítko u hlášky (firewall)


@dataclass
class Item:
    path: Path
    id: str = field(default_factory=lambda: str(next(_ids)))
    info: media.MediaInfo | None = None
    audio: str | None = None
    sub: str = "none"
    status: str = "probing"  # probing / ready / error / done
    error: str = ""
    progress: float | None = None  # postup přebalování 0..1
    prepared: tuple[tuple, Path, media.Plan] | None = None  # (výběr, soubor, plán)
    prep_task: asyncio.Task | None = None

    @property
    def title(self) -> str:
        return self.path.stem

    def choice(self, dev: Device | None, prefs: dict) -> tuple:
        return self.audio, self.sub, dev.caps_key if dev else (), tuple(sorted(prefs.items()))

    def as_dict(self, current: bool) -> dict:
        i = self.info
        plan_warnings = self.prepared[2].warnings if self.prepared else []
        return {
            "id": self.id, "title": self.title, "status": self.status, "error": self.error,
            "progress": self.progress, "current": current,
            "summary": i.summary if i else "", "duration": i.duration if i else 0,
            "audio": self.audio, "sub": self.sub,
            "audioTracks": [t.as_dict() for t in i.audio] if i else [],
            "subTracks": [t.as_dict() for t in i.subs] if i else [],
            "warnings": plan_warnings,
        }


class Player:
    """Ovládá přehrávače přes jejich společné rozhraní (upnp.Device pro DLNA, cast.CastDevice)."""

    def __init__(self, discovery: Discovery, server: MediaServer, cfg: dict, on_change) -> None:
        self.discovery = discovery
        self.server = server
        self.cfg = cfg
        self.on_change = on_change
        self.queue: list[Item] = []
        self.current: Item | None = None
        self.device_udn: str | None = cfg.get("device")
        self.state = "idle"  # idle / waking / preparing / loading / playing / paused
        self.position: float | None = None
        self.duration: float | None = None
        self.volume: int | None = None
        self.muted: bool | None = None
        self.notice: dict | None = None
        self.keep_awake = winutil.KeepAwake()
        self._task: asyncio.Task | None = None
        self._played_key: str | None = None
        self._sub_key: str | None = None
        self._sub_file: Path | None = None
        self._last_volume_poll = 0.0

    # --- stav pro GUI ---

    @property
    def prefs(self) -> dict:
        """Volby přebalení z nastavení (výchozí zapnuto) – viz media.plan."""
        return {"convert_hd_audio": self.cfg.get("convert_hd_audio", True), "dv_to_mp4": self.cfg.get("dv_to_mp4", True)}

    @property
    def wake_enabled(self) -> bool:
        """Zapínání vypnutých TV (Wake-on-LAN) – jen když ho uživatel zapne v nastavení."""
        return bool(self.cfg.get("wake_on_lan"))

    @property
    def device(self) -> Device | None:
        return self.discovery.devices.get(self.device_udn or "")

    def snapshot(self) -> dict:
        devices = []
        for udn, dev in self.discovery.devices.items():
            devices.append({"udn": udn, "name": dev.name, "model": f"{dev.manufacturer} {dev.model}".strip(),
                            "host": dev.host, "online": True})
        for udn, known in self.cfg.get("devices", {}).items():
            if udn not in self.discovery.devices:
                devices.append({"udn": udn, "name": known.get("name", "?"), "model": known.get("model", ""),
                                "host": known.get("host", ""), "online": False,
                                "canWake": self.wake_enabled and _can_wake(udn, known)})
        selected = next((d for d in devices if d["udn"] == self.device_udn), None)
        return {
            "devices": devices, "searching": self.discovery.searching, "device": self.device_udn,
            "deviceName": selected["name"] if selected else None,
            "deviceOnline": bool(selected and selected["online"]),
            "state": self.state, "position": self.position, "duration": self.duration,
            "volume": self.volume, "muted": self.muted, "notice": self.notice,
            "current": self.current.id if self.current else None,
            "queue": [it.as_dict(it is self.current) for it in self.queue],
        }

    def _changed(self) -> None:
        self.keep_awake.set(self.state != "idle" or any(it.prep_task and not it.prep_task.done() for it in self.queue))
        self.on_change()

    def _notify(self, text: str, kind: str = "error", action: str | None = None) -> None:
        log.info("Hláška (%s): %s", kind, text)
        self.notice = {"text": text, "kind": kind, "action": action, "at": time.time()}
        self._changed()

    def dismiss_notice(self) -> None:
        self.notice = None
        self._changed()

    # --- přehrávače ---

    def devices_changed(self) -> None:
        """Volá Discovery: zapamatuje si nalezené přehrávače (kvůli zapnutí přes WoL)."""
        known = self.cfg.setdefault("devices", {})
        changed = False
        for udn, dev in self.discovery.devices.items():
            entry = known.get(udn, {})
            new = {"name": dev.name, "model": f"{dev.manufacturer} {dev.model}".strip(), "host": dev.host,
                   "mac": entry.get("mac")}
            if not new["mac"]:
                new["mac"] = winutil.mac_address(dev.host)
            if new != entry:
                known[udn] = new
                changed = True
        # Dokud si uživatel přehrávač nevybral, vybírá se ten nejvhodnější z nalezených. Chromecasty
        # se najdou dřív než DLNA TV, proto se výběr může ještě zlepšit (ne během přehrávání).
        if not self.cfg.get("device_chosen") and self.discovery.devices and self.state == "idle":
            best = min(self.discovery.devices.values(), key=_device_rank)
            current = self.discovery.devices.get(self.device_udn or "")
            if not current or _device_rank(best) < _device_rank(current):
                self.device_udn = best.udn
                self.cfg["device"] = self.device_udn
                changed = True
        if changed:
            winutil.save_config(self.cfg)
        self._changed()

    async def select_device(self, udn: str) -> None:
        if udn == self.device_udn:
            return
        resume = self.current if self.state in ("playing", "paused", "loading", "preparing") else None
        position = self.position or 0
        await self.stop()
        self.device_udn = udn
        self.cfg["device"] = udn
        self.cfg["device_chosen"] = True
        winutil.save_config(self.cfg)
        self.volume = self.muted = None
        self._changed()
        if resume:
            self._start(resume, position)

    async def forget_device(self, udn: str) -> None:
        self.cfg.get("devices", {}).pop(udn, None)
        if self.device_udn == udn:
            self.device_udn = None
            self.cfg["device"] = None
        winutil.save_config(self.cfg)
        self._changed()

    async def _ensure_device(self) -> Device:
        """Vybraný přehrávač; vypnutou TV zkusí zapnout přes Wake-on-LAN."""
        if dev := self.device:
            return dev
        if not self.device_udn:
            raise PlayError(t("err.no_tv"))
        known = self.cfg.get("devices", {}).get(self.device_udn, {})
        if not (self.wake_enabled and _can_wake(self.device_udn, known)):
            raise PlayError(t("err.tv_offline", name=known.get("name", "TV")))
        self.state = "waking"
        self._changed()
        deadline = time.monotonic() + WAKE_TIMEOUT
        while time.monotonic() < deadline:
            await asyncio.to_thread(winutil.wake_on_lan, known["mac"], known.get("host"))
            await self.discovery.search()
            if dev := self.device:
                await asyncio.sleep(3)  # přehrávač v TV naběhne chvíli po síti
                return dev
            await asyncio.sleep(2)
        raise PlayError(t("err.wake_failed", name=known.get("name", "TV")))

    async def wake(self, udn: str) -> None:
        if udn != self.device_udn:
            await self.select_device(udn)
        try:
            await self._ensure_device()
        except PlayError as e:
            self._notify(str(e))
        finally:
            if self.state == "waking":
                self.state = "idle"
            self._changed()

    # --- fronta ---

    async def add(self, paths: list[str], play: bool = False) -> None:
        files = []
        for p in map(Path, paths):
            if p.is_dir():
                files += sorted(f for f in p.rglob("*") if f.suffix.lower() in media.VIDEO_SUFFIXES)
            elif p.is_file():
                files.append(p)
        items = [Item(f) for f in files if f.suffix.lower() in media.VIDEO_SUFFIXES or len(files) == 1]
        log.info("Do fronty: %s", ", ".join(it.path.name for it in items) or "nic")
        if not items:
            self._notify(t("info.no_video"), "info")
            return
        self.queue += items
        self._changed()
        await asyncio.gather(*(self._probe(it) for it in items))
        first = next((it for it in items if it.status == "ready"), None)
        if first and (play or self.state == "idle"):
            self._start(first)

    async def _probe(self, item: Item) -> None:
        try:
            item.info = await asyncio.to_thread(media.probe, item.path)
            item.audio, item.sub = media.default_choice(item.info, i18n.preferred_langs(self.cfg))
            item.status = "ready"
        except Exception as e:  # noqa: BLE001
            item.status, item.error = "error", str(e)
        self._changed()

    async def remove(self, item_id: str) -> None:
        item = self._item(item_id)
        if not item:
            return
        if item is self.current:
            await self.stop()
        self.queue.remove(item)
        self._discard(item)
        self._changed()

    async def clear(self) -> None:
        for item in [it for it in self.queue if it is not self.current]:
            self.queue.remove(item)
            self._discard(item)
        self._changed()

    async def move(self, item_id: str, to: int) -> None:
        item = self._item(item_id)
        if item:
            self.queue.remove(item)
            self.queue.insert(max(0, min(to, len(self.queue))), item)
            self._changed()

    async def set_tracks(self, item_id: str, audio: str | None, sub: str) -> None:
        item = self._item(item_id)
        if not item or (item.audio, item.sub) == (audio, sub):
            return
        item.audio, item.sub = audio, sub
        if item.prep_task and not item.prep_task.done():
            item.prep_task.cancel()
        self._changed()
        if item is self.current and self.state in ("playing", "paused"):
            self._start(item, self.position or 0)  # znovu s jinou stopou, od stejného místa

    def _item(self, item_id: str) -> Item | None:
        return next((it for it in self.queue if it.id == item_id), None)

    def _discard(self, item: Item) -> None:
        if item.prep_task and not item.prep_task.done():
            item.prep_task.cancel()
        self._discard_prepared(item)

    def _discard_prepared(self, item: Item) -> None:
        """Smaže jen to, co aplikace sama vytvořila v TEMP – nikdy původní video."""
        if item.prepared:
            _, path, plan = item.prepared
            for temp in (path, plan.vtt):
                if temp and _is_temp(temp):
                    with contextlib.suppress(OSError):
                        temp.unlink()
            item.prepared = None

    # --- ovládání ---

    async def play_item(self, item_id: str) -> None:
        if item := self._item(item_id):
            if item.status == "error":
                return
            self._start(item)

    async def toggle(self) -> None:
        dev = self.device
        if self.state == "playing" and dev:
            await self._safe(dev.pause())
            self.state = "paused"
        elif self.state == "paused" and dev:
            await self._safe(dev.play())
            self.state = "playing"
        elif self.state == "idle":
            item = self.current if self.current and self.current.status != "error" else None
            item = item or next((it for it in self.queue if it.status == "ready"), None)
            if item:
                self._start(item)
        self._changed()

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if (dev := self.device) and self.state in ("playing", "paused", "loading"):
            await self._safe(dev.stop())
        self._release()
        self.state = "idle"
        self.position = None
        if self.current and self.current.status == "ready" and self.current.progress is not None:
            self.current.progress = None
        self._changed()

    async def step(self, delta: int) -> None:
        """Další/předchozí video ve frontě (předchozí: do 5 s od začátku, jinak znovu od začátku)."""
        if not self.current:
            return
        idx = self.queue.index(self.current)
        if delta < 0 and (self.position or 0) > 5:
            await self.seek(0)
            return
        for it in self.queue[idx + delta::delta] if delta > 0 else reversed(self.queue[:idx]):
            if it.status != "error":
                self._start(it)
                return

    async def seek(self, seconds: float) -> None:
        if (dev := self.device) and self.state in ("playing", "paused"):
            self.position = seconds
            self._changed()
            await self._safe(dev.seek(seconds))

    async def set_volume(self, value: int) -> None:
        if dev := self.device:
            self.volume = value
            await self._safe(dev.set_volume(value))

    async def set_mute(self, mute: bool) -> None:
        if dev := self.device:
            self.muted = mute
            await self._safe(dev.set_mute(mute))
            self._changed()

    async def _safe(self, coro) -> None:
        try:
            await coro
        except UpnpError as e:
            log.info("TV odmítla %s", e)
            self._notify(t("err.refused_cmd", reason=_reason(e)))
        except Exception as e:  # noqa: BLE001
            log.warning("Příkaz pro TV selhal: %r", e)
            self._notify(t("err.not_responding"))

    # --- přehrávání ---

    def _start(self, item: Item, start_at: float = 0) -> None:
        old, self._task = self._task, None
        if old and not old.done():
            old.cancel()
        self._task = asyncio.create_task(self._run(item, start_at, old))

    async def _run(self, item: Item | None, start_at: float, previous: asyncio.Task | None) -> None:
        if previous:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await previous
        self.notice = None
        while item:
            self.current = item
            self.position, self.duration = start_at or None, item.info.duration if item.info else None
            try:
                ended = await self._play_one(item, start_at)
            except asyncio.CancelledError:
                raise
            except PlayError as e:
                self._release()
                self.state = "idle"
                self._notify(str(e), action=e.action)
                return
            except Exception as e:  # noqa: BLE001
                log.exception("Přehrávání selhalo")
                self._release()
                self.state = "idle"
                self._notify(t("err.playback_failed", error=e))
                return
            self._release()
            self.state = "idle"
            self.position = None
            if not ended:
                self._changed()
                return
            item.status = "done"
            self._discard(item)
            start_at = 0
            item = self._next_after(item)
            self._changed()
        self.current = None
        self._changed()

    def _next_after(self, item: Item) -> Item | None:
        if item not in self.queue:
            return None
        return next((it for it in self.queue[self.queue.index(item) + 1:] if it.status == "ready"), None)

    def _release(self) -> None:
        for key in (self._played_key, self._sub_key):
            if key:
                self.server.remove(key)
        if self._sub_key and (sub := self._sub_file) and _is_temp(sub):
            with contextlib.suppress(OSError):
                sub.unlink()  # převedené titulky (SRT/WebVTT) pro tuto jednu projekci
        self._played_key = self._sub_key = self._sub_file = None

    async def _prepare(self, item: Item, dev: Device) -> tuple[Path, media.Plan]:
        """Soubor pro TV – původní, nebo přebalený (sdílí se s přípravou dopředu)."""
        choice = item.choice(dev, self.prefs)
        if item.prepared and item.prepared[0] == choice and item.prepared[1].exists():
            return item.prepared[1], item.prepared[2]
        plan = media.plan(item.info, item.audio, item.sub, dev, **self.prefs)
        if plan.error:
            raise media.MediaError(plan.error)  # rovnou, ať se zbytečně nepřebaluje
        if not plan.remux and not plan.extract:
            item.prepared = (choice, item.path, plan)
            return item.path, plan
        if not (item.prep_task and not item.prep_task.done() and getattr(item.prep_task, "choice", None) == choice):
            item.prep_task = asyncio.create_task(self._build(item, plan, choice))
            item.prep_task.choice = choice
        return await asyncio.shield(item.prep_task)

    async def _build(self, item: Item, plan: media.Plan, choice: tuple) -> tuple[Path, media.Plan]:
        """Přebalí video a/nebo z něj vytáhne titulky pro Chromecast (s ukazatelem postupu)."""
        self._discard_prepared(item)
        stamp = f"{item.id}-{int(time.time())}"
        out = media.TEMP / f"{stamp}.{plan.container}" if plan.remux else item.path
        vtt = media.TEMP / f"{stamp}.vtt" if plan.extract else None
        item.progress = 0.0
        self._changed()
        last = 0.0

        def progress(p: float) -> None:
            nonlocal last
            item.progress = p
            if p - last >= 0.01:
                last = p
                self._changed()

        try:
            if plan.remux:
                await media.remux(item.info, item.audio, item.sub, plan, out, progress, vtt)
            elif vtt:
                await media.extract_vtt(item.info, plan.sidecar, vtt, progress)
        finally:
            item.progress = None
            self._changed()
        plan.vtt = vtt
        item.prepared = (choice, out, plan)
        return out, plan

    def _prefetch(self, after: Item, dev: Device) -> None:
        """Další video ve frontě, které je potřeba přebalit, se připraví už během přehrávání."""
        nxt = self._next_after(after)
        if not nxt or not nxt.info:
            return
        plan = media.plan(nxt.info, nxt.audio, nxt.sub, dev, **self.prefs)
        busy = nxt.prep_task and not nxt.prep_task.done()
        if (plan.remux or plan.extract) and not plan.error and not busy and not (nxt.prepared and nxt.prepared[0] == nxt.choice(dev, self.prefs)):
            nxt.prep_task = asyncio.create_task(self._build(nxt, plan, nxt.choice(dev, self.prefs)))
            nxt.prep_task.choice = nxt.choice(dev, self.prefs)
            nxt.prep_task.add_done_callback(lambda t: t.cancelled() or t.exception())

    async def _play_one(self, item: Item, start_at: float) -> bool:
        """Přehraje jedno video. True = dohrálo do konce, False = skončilo jinak."""
        dev = await self._ensure_device()
        self.state = "preparing"
        self._changed()
        try:
            path, plan = await self._prepare(item, dev)
        except media.MediaError as e:
            raise PlayError(str(e)) from e
        for warning in plan.warnings:
            self._notify(warning, "warning")
        log.info("Přehrávám %s na %s (%s%s)", item.path.name, dev.name, plan.mime,
                 f", přebaleno: {', '.join(plan.reasons)}" if plan.remux else "")

        host_ip = local_ip_for(dev.host)
        key, url = self.server.add(path, plan.mime, host_ip, f"{item.title}.{plan.container}")
        self._played_key = key
        sub_url, sub_lang = await self._subtitles(dev, item, plan, key, host_ip)

        self.state = "loading"
        self._changed()
        try:
            await dev.load(url, item.title, plan.mime, sub_url, sub_lang, start_at)
        except UpnpError as e:
            raise PlayError(t("err.refused_video", reason=_reason(e))) from e
        except TimeoutError:
            if key not in self.server.hits and dev.kind == "dlna":
                raise PlayError(self._firewall_text(), "firewall") from None
            # Některé TV potvrdí Play až po načtení videa – necháme rozhodnout stav přehrávače.
        except OSError as e:
            self.discovery.forget_if_gone(dev)  # třeba ukončené Kodi – ať v seznamu nesvítí zeleně
            raise PlayError(t("err.device_not_responding", name=dev.name, error=e)) from e
        except Exception as e:  # noqa: BLE001 - chyby pychromecastu
            log.warning("Chromecast video nepřijal: %r", e)
            raise PlayError(t("err.cast_rejected", name=dev.name, error=e or type(e).__name__)) from e
        return await self._watch(dev, item, key, url, start_at)

    async def _subtitles(self, dev: Device, item: Item, plan: media.Plan, key: str, host_ip: str) -> tuple[str | None, str]:
        """Titulky zvlášť: WebVTT pro Chromecast, SRT pro TV, které je tak berou (Samsung)."""
        src = plan.vtt if dev.kind == "cast" else None
        track = plan.sidecar if dev.kind == "cast" else None
        try:
            if dev.kind == "cast" and track and track.file:
                src = await asyncio.to_thread(media.subtitle_utf8, track.file, key, "webvtt")
            elif dev.kind == "dlna" and plan.subtitle_file:
                src = await asyncio.to_thread(media.subtitle_utf8, plan.subtitle_file, key)
        except media.MediaError as e:
            log.warning("Titulky: %s", e)
            self._notify(str(e), "warning")
            return None, ""
        if not src:
            return None, ""
        self._sub_key, sub_url = self.server.add(src, dev.subtitle_mime, host_ip, f"{item.title}{src.suffix}")
        # Vytažené WebVTT (plan.vtt) patří k přebalenému videu a smaže se s ním; převod ze souboru hned.
        self._sub_file = src if src != plan.vtt else None
        if dev.kind == "dlna":
            self.server.captions[key] = sub_url
        lang = (track.lang if track else "") or next((s.lang for s in item.info.subs if s.id == item.sub), "")
        return sub_url, lang

    def _firewall_text(self) -> str:
        return t("err.firewall")

    async def _watch(self, dev: Device, item: Item, key: str, url: str, start_at: float) -> bool:
        started = time.monotonic()
        played, idle, errors = False, 0, 0
        last_pos = 0.0
        # Přehrávač hlásí „hraje“, ale pozice stojí: typicky Kodi na Androidu běžící jen na pozadí
        # (přijme video, chvíli hraje zvuk a obraz zůstane na domovské obrazovce).
        moving_pos, moving_since, stall_warned = None, time.monotonic(), False
        while True:
            await asyncio.sleep(1)
            try:
                st = await dev.status()
                errors = 0
            except Exception:  # noqa: BLE001 - TV se vypnula nebo odpojila
                errors += 1
                if errors >= 4:
                    raise PlayError(t("err.stopped_responding", name=dev.name)) from None
                continue
            if st.uri and st.uri != url and played:
                log.info("Na TV se přehrává něco jiného (%s)", st.uri)
                return False
            if st.state in ACTIVE:
                if not played and st.state != "loading":
                    played = True
                    log.info("TV přehrává (%s)", st.state)
                    self._prefetch(item, dev)
                    if start_at > 5 and dev.kind == "dlna":  # Chromecast začne rovnou od start_at
                        with contextlib.suppress(Exception):
                            await dev.seek(start_at)
                idle = 0
                if st.position is not None and (st.position > 0 or not start_at):
                    self.position = last_pos = st.position
                if st.duration:
                    self.duration = st.duration
                now = time.monotonic()
                if st.state != "playing" or st.position is None or st.position != moving_pos:
                    moving_pos, moving_since = st.position, now
                elif played and not stall_warned and now - moving_since > STALL_AFTER:
                    stall_warned = True
                    log.info("%s hlásí přehrávání, ale pozice stojí na %.0f s", dev.name, st.position)
                    self._notify(t("warn.kodi_background" if _is_kodi(dev) else "warn.stalled", name=dev.name),
                                 "warning")
                self.state = "paused" if st.state == "paused" else "playing" if played else "loading"
                await self._poll_volume(dev)
                self._changed()
                continue
            idle += 1
            if not played:
                waited = time.monotonic() - started
                if st.state == "error" or (waited > 4 and idle >= 4 and key in self.server.hits):
                    raise PlayError(self._format_error(item))
                if waited > START_TIMEOUT:
                    if key not in self.server.hits:
                        raise PlayError(self._firewall_text(), "firewall")
                    raise PlayError(self._format_error(item))
                continue
            if idle >= 2 or st.finished is not None:
                # Dohrálo, nebo to někdo zastavil ovladačem TV? Chromecast to hlásí, u DLNA podle pozice.
                total = self.duration or (item.info.duration if item.info else 0)
                ended = st.finished if st.finished is not None else bool(total) and last_pos >= total - 20
                log.info("Konec přehrávání %s (%s, pozice %.0f / %.0f s)", item.path.name,
                         "dohrálo" if ended else "zastaveno", last_pos, total)
                return ended

    def _format_error(self, item: Item) -> str:
        detail = item.info.summary if item.info else ""
        return t("err.format", detail=f" ({detail})" if detail else "")

    async def _poll_volume(self, dev: Device) -> None:
        if time.monotonic() - self._last_volume_poll < 5:
            return
        self._last_volume_poll = time.monotonic()
        vol, mute = await dev.volume()
        if vol is not None:
            self.volume = vol
        if mute is not None:
            self.muted = mute
