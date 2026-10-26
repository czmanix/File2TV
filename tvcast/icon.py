"""Ikona aplikace (kreslí se, žádný soubor navíc) a nabídka v oznamovací oblasti."""

import contextlib
import winreg
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

from . import winutil
from .i18n import t

ACCENT = (0, 103, 192)


def dark_mode() -> bool:
    with contextlib.suppress(OSError):
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    return False


def image(size: int = 256, playing: bool = False) -> Image.Image:
    """Obrazovka s tlačítkem Play (při přehrávání vyplněné)."""
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    w = max(2, s // 16)
    d.rounded_rectangle((s * .06, s * .14, s * .94, s * .76), radius=s * .1, fill=ACCENT if playing else None,
                        outline=ACCENT, width=w)
    d.rounded_rectangle((s * .3, s * .84, s * .7, s * .84 + w), radius=w / 2, fill=ACCENT)
    tri = [(s * .42, s * .3), (s * .42, s * .6), (s * .66, s * .45)]
    d.polygon(tri, fill=(255, 255, 255) if playing else ACCENT)
    return img


def ico_path() -> Path:
    path = winutil.CONFIG.parent / "file2tv.ico"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        image().save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])
    return path


def tray(app) -> pystray.Icon:
    def player():
        return app.player

    def label(_item) -> str:
        p = player()
        return t("tray.pause") if p and p.state == "playing" else t("tray.play")

    def call(coro_fn):
        return lambda *_: app.call(coro_fn())

    def text(key):  # texty se čtou při každém otevření nabídky – po změně jazyka hned platí
        return lambda _item: t(key)

    menu = pystray.Menu(
        pystray.MenuItem(text("tray.show"), lambda *_: app.show(), default=True),
        pystray.MenuItem(label, call(lambda: player().toggle()), enabled=lambda _: bool(player() and player().queue)),
        pystray.MenuItem(text("tray.stop"), call(lambda: player().stop()),
                         enabled=lambda _: bool(player() and player().state != "idle")),
        pystray.MenuItem(text("tray.next"), call(lambda: player().step(1)),
                         enabled=lambda _: bool(player() and player().current)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(text("tray.quit"), lambda *_: app.quit()),
    )
    return pystray.Icon("file2tv", image(64), winutil.APP_NAME, menu)
