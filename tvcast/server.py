"""HTTP server, ze kterého si TV video (a titulky) stahuje, s přetáčením (Range)."""

import os
import secrets
import time
from pathlib import Path
from urllib.parse import quote

# TransmitFile ve Windows (sendfile v aiohttp) neumí poslat víc než 2 GB najednou
# (WinError 87) a TV chce celý film od začátku do konce. Musí být před importem aiohttp.
os.environ["AIOHTTP_NOSENDFILE"] = "1"
from aiohttp import web  # noqa: E402

from .upnp import DLNA_FEATURES  # noqa: E402


class MediaServer:
    def __init__(self) -> None:
        self.files: dict[str, tuple[Path, str]] = {}  # klíč → (soubor, MIME)
        self.captions: dict[str, str] = {}  # klíč videa → adresa titulků (Samsung: CaptionInfo.sec)
        self.hits: dict[str, float] = {}  # klíč → kdy si soubor TV naposledy stahovala
        self.port = 0
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.add_routes([web.get("/{key}/{name}", self._serve),  # včetně HEAD
                        web.options("/{key}/{name}", self._preflight)])
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, "0.0.0.0", 0).start()
        self.port = self._runner.addresses[0][1]

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    def add(self, path: Path, mime: str, host_ip: str, name: str | None = None) -> tuple[str, str]:
        """Zpřístupní soubor; vrátí (klíč, adresa). Nový klíč pokaždé, aby TV
        nepřehrála starší soubor z mezipaměti."""
        key = secrets.token_hex(6)
        self.files[key] = (path, mime)
        return key, f"http://{host_ip}:{self.port}/{key}/{quote(name or path.name)}"

    def remove(self, key: str) -> None:
        self.files.pop(key, None)
        self.captions.pop(key, None)
        self.hits.pop(key, None)

    async def _preflight(self, _: web.Request) -> web.Response:
        return web.Response(headers={"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*",
                                     "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS"})

    async def _serve(self, request: web.Request) -> web.StreamResponse:
        key = request.match_info["key"]
        if key not in self.files:
            raise web.HTTPNotFound
        path, mime = self.files[key]
        self.hits[key] = time.monotonic()
        # CORS: přehrávač Chromecastu je webová aplikace a titulky si stahuje přes XHR.
        headers = {"Content-Type": mime, "transferMode.dlna.org": "Streaming",
                   "contentFeatures.dlna.org": DLNA_FEATURES, "Access-Control-Allow-Origin": "*"}
        if caption := self.captions.get(key):
            headers["CaptionInfo.sec"] = caption
        if mime.startswith("text/"):
            headers["transferMode.dlna.org"] = "Interactive"
            del headers["contentFeatures.dlna.org"]
        return web.FileResponse(path, headers=headers)
