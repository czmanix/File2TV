"""Okno aplikace (pywebview), ikona v oznamovací oblasti a jediná běžící instance.

Síť a přehrávání běží v asyncio smyčce ve vlastním vlákně, okno v hlavním vlákně.
Druhé spuštění (třeba z „Odeslat do“) jen předá soubory běžící instanci.
"""

import asyncio
import contextlib
import json
import logging
import socket
import sys
import threading
from pathlib import Path

import aiohttp
import webview
from webview.dom import DOMEventHandler

from . import i18n, icon, media, winutil
from .i18n import t
from .cast import CastDiscovery
from .player import Player
from .server import MediaServer
from .upnp import Discovery, Upnp

log = logging.getLogger(__name__)
INSTANCE_PORT = 47653  # jen na 127.0.0.1, předání souborů běžící instanci
UI = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)) / "ui" / "index.html"
def video_filter() -> tuple[str, str]:
    return (t("dialog.videos") + " (" + ";".join(f"*{s}" for s in sorted(media.VIDEO_SUFFIXES)) + ")",
            t("dialog.all_files") + " (*.*)")


def forward_to_running(files: list[str]) -> bool:
    """Předá soubory už běžící instanci. False = žádná neběží."""
    try:
        with socket.create_connection(("127.0.0.1", INSTANCE_PORT), timeout=1) as s:
            s.sendall(json.dumps({"files": files}).encode() + b"\n")
            return s.recv(16) == b"ok"
    except OSError:
        return False


class App:
    def __init__(self, files: list[str]) -> None:
        self.initial_files = files
        self.cfg = winutil.load_config()
        self.loop = asyncio.new_event_loop()
        self.window: webview.Window | None = None
        self.tray = None
        self.player: Player | None = None
        self.quitting = False
        self._push_pending = False
        self._ready = threading.Event()

    # --- asyncio vlákno ---

    def _loop_thread(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._main())

    async def _main(self) -> None:
        media.clean_temp()
        async with aiohttp.ClientSession() as session:
            upnp = Upnp(session)
            server = MediaServer()
            await server.start()
            on_devices = lambda: self.player and self.player.devices_changed()  # noqa: E731
            discovery = Discovery(upnp, on_devices)
            casts = CastDiscovery(discovery.devices, on_devices)  # Chromecasty do stejného seznamu
            self.player = Player(discovery, server, self.cfg, self._schedule_push)
            with contextlib.suppress(Exception):
                await asyncio.to_thread(casts.start)
            self._ready.set()
            instance = None
            with contextlib.suppress(OSError):
                instance = await asyncio.start_server(self._on_instance, "127.0.0.1", INSTANCE_PORT)
            if self.initial_files:
                asyncio.create_task(self.player.add(self.initial_files, play=True))
            try:
                await discovery.run()
            finally:
                if instance:
                    instance.close()
                await self.player.stop()
                await asyncio.to_thread(casts.stop)
                await server.stop()
                media.clean_temp()

    async def _on_instance(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(Exception):
            msg = json.loads(await reader.readline())
            writer.write(b"ok")
            await writer.drain()
            self.show()
            if files := msg.get("files"):
                await self.player.add(files, play=self.player.state == "idle")
        writer.close()

    def _schedule_push(self) -> None:
        if not self._push_pending:
            self._push_pending = True
            self.loop.call_later(0.08, self._push)

    def _push(self) -> None:
        self._push_pending = False
        if not self.window:
            return
        data = json.dumps(self.player.snapshot(), ensure_ascii=False)
        self.loop.run_in_executor(None, self._run_js, f"window.app && app.update({data})")
        if self.tray:
            self.tray.update_menu()

    def _run_js(self, js: str) -> None:
        with contextlib.suppress(Exception):
            self.window.run_js(js)

    def call(self, coro, timeout: float = 30):
        """Spustí akci přehrávače z vlákna okna a počká na výsledek."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    # --- okno ---

    def show(self) -> None:
        if self.window:
            with contextlib.suppress(Exception):
                self.window.show()
                self.window.restore()

    def _on_closing(self) -> bool:
        # Křížek okno jen schová, dokud se něco přehrává; přehrávání pak běží dál z oznamovací oblasti.
        if self.quitting or not self.player or self.player.state == "idle":
            self._shutdown()
            return True
        threading.Thread(target=self.window.hide, daemon=True).start()
        return False

    def _on_loaded(self) -> None:
        doc = self.window.dom.document
        doc.events.dragover += DOMEventHandler(lambda e: None, True, True)
        doc.events.drop += DOMEventHandler(self._on_drop, True, True)

    def _on_drop(self, event: dict) -> None:
        files = [f.get("pywebviewFullPath") for f in event.get("dataTransfer", {}).get("files", [])]
        if files := [f for f in files if f]:
            asyncio.run_coroutine_threadsafe(self.player.add(files), self.loop)

    def quit(self) -> None:
        self.quitting = True
        if self.window:
            self.window.destroy()

    def _shutdown(self) -> None:
        self.quitting = True
        if self.tray:
            with contextlib.suppress(Exception):
                self.tray.stop()
        with contextlib.suppress(Exception):
            self.call(self.player.stop(), 6)
        self.loop.call_soon_threadsafe(lambda: [t.cancel() for t in asyncio.all_tasks(self.loop)])

    def run(self) -> None:
        threading.Thread(target=self._loop_thread, name="asyncio", daemon=True).start()
        self._ready.wait(10)
        self.window = webview.create_window(
            winutil.APP_NAME, str(UI), js_api=Api(self), width=460, height=760, min_size=(380, 560),
            background_color="#1c1c1e" if icon.dark_mode() else "#f3f3f3")
        self.window.events.closing += self._on_closing
        self.window.events.loaded += self._on_loaded
        self.tray = icon.tray(self)
        threading.Thread(target=self.tray.run, name="tray", daemon=True).start()
        webview.start(private_mode=False, storage_path=str(winutil.CONFIG.parent / "webview"),
                      icon=str(icon.ico_path()))


class Api:
    """Volání z JavaScriptu (window.pywebview.api.*)."""

    def __init__(self, app: App) -> None:
        self._app = app

    def _p(self) -> Player:
        return self._app.player

    def state(self) -> dict:
        return self._app.call(self._snapshot())

    async def _snapshot(self) -> dict:
        return self._p().snapshot()

    def add_files(self) -> None:
        files = self._app.window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True, file_types=video_filter())
        if files:
            self._app.call(self._p().add(list(files)), 120)

    def add_folder(self) -> None:
        folder = self._app.window.create_file_dialog(webview.FileDialog.FOLDER)
        if folder:
            self._app.call(self._p().add(list(folder)), 300)

    def play(self, item_id: str) -> None:
        self._app.call(self._p().play_item(item_id))

    def toggle(self) -> None:
        self._app.call(self._p().toggle())

    def stop(self) -> None:
        self._app.call(self._p().stop())

    def step(self, delta: int) -> None:
        self._app.call(self._p().step(int(delta)))

    def seek(self, seconds: float) -> None:
        self._app.call(self._p().seek(float(seconds)))

    def volume(self, value: int) -> None:
        self._app.call(self._p().set_volume(int(value)))

    def mute(self, on: bool) -> None:
        self._app.call(self._p().set_mute(bool(on)))

    def remove(self, item_id: str) -> None:
        self._app.call(self._p().remove(item_id))

    def clear(self) -> None:
        self._app.call(self._p().clear())

    def move(self, item_id: str, to: int) -> None:
        self._app.call(self._p().move(item_id, int(to)))

    def tracks(self, item_id: str, audio: str | None, sub: str) -> None:
        self._app.call(self._p().set_tracks(item_id, audio, sub))

    def select_device(self, udn: str) -> None:
        self._app.call(self._p().select_device(udn))

    def wake(self, udn: str) -> None:
        self._app.call(self._p().wake(udn), 90)

    def forget(self, udn: str) -> None:
        self._app.call(self._p().forget_device(udn))

    def refresh(self) -> None:
        self._app.loop.call_soon_threadsafe(self._p().discovery.refresh)

    def dismiss(self) -> None:
        self._app.call(self._async(self._p().dismiss_notice))

    async def _async(self, fn, *args):
        return fn(*args)

    def settings(self) -> dict:
        return {"sendto": winutil.sendto_installed(), "langs": ", ".join(i18n.preferred_langs(self._app.cfg)),
                "langsDefault": ", ".join(i18n.default_langs()), "wol": bool(self._app.cfg.get("wake_on_lan")),
                "uiLang": self._app.cfg.get("ui_lang", "auto"), "systemLang": i18n.system_language(),
                "languages": i18n.LANGUAGES}

    def strings(self) -> dict:
        """Texty okna v jazyce aplikace (+ kód jazyka pro názvy jazyků stop přes Intl)."""
        return {"lang": i18n.language(), "strings": i18n.ui_strings()}

    def set_ui_lang(self, code: str) -> dict:
        self._app.cfg["ui_lang"] = code if code in i18n.LANGUAGES else "auto"
        winutil.save_config(self._app.cfg)
        i18n.set_language(i18n.resolve(self._app.cfg["ui_lang"]))
        return self.strings()

    def set_sendto(self, on: bool) -> dict:
        try:
            winutil.set_sendto(bool(on))
        except OSError as e:
            return {"error": str(e)}
        return self.settings()

    def set_wol(self, on: bool) -> dict:
        self._app.cfg["wake_on_lan"] = bool(on)
        winutil.save_config(self._app.cfg)
        self._app.loop.call_soon_threadsafe(self._p()._changed)  # tlačítka u vypnutých TV
        return self.settings()

    def set_langs(self, text: str) -> dict:
        langs = [p.strip().lower() for p in text.replace(";", ",").split(",") if p.strip()]
        # Prázdné nebo stejné jako výchozí = zase se řídit jazykem aplikace.
        custom = bool(langs) and langs != i18n.default_langs()
        self._app.cfg["langs"] = langs if custom else []
        self._app.cfg["langs_custom"] = custom
        winutil.save_config(self._app.cfg)
        return self.settings()

    def firewall(self) -> dict:
        try:
            winutil.allow_firewall()
        except OSError as e:
            return {"error": str(e)}
        self.dismiss()
        return {}


def main() -> None:
    files = [str(Path(a).resolve()) for a in sys.argv[1:] if not a.startswith("-")]
    if forward_to_running(files):
        return
    frozen = getattr(sys, "frozen", False)
    had_old_sendto = winutil.migrate_old_name(replace_shortcut=frozen)
    winutil.CONFIG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s", handlers=[
        logging.FileHandler(winutil.CONFIG.parent / "file2tv.log", mode="w", encoding="utf-8"),
        *([logging.StreamHandler()] if sys.stderr else [])])
    cfg = winutil.load_config()
    i18n.set_language(i18n.resolve(cfg.get("ui_lang")))
    if frozen and (not cfg.get("sendto_offered") or winutil.sendto_installed() or had_old_sendto):
        # Při prvním spuštění přidá „Odeslat do → File2TV“ (v nastavení jde vypnout),
        # později jen opraví cestu, kdyby se program přesunul (nebo přejmenoval).
        def sendto() -> None:
            with contextlib.suppress(OSError):
                winutil.set_sendto(True)
        threading.Thread(target=sendto, daemon=True).start()
        cfg["sendto_offered"] = True
        winutil.save_config(cfg)
    App(files).run()
