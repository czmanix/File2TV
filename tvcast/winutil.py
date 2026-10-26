"""Windows: bránění spánku, „Odeslat do“, firewall, MAC adresa a Wake-on-LAN."""

import ctypes
import ctypes.wintypes as wt
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

from .i18n import t

APP_NAME = "File2TV"
APPDATA = Path(os.environ.get("APPDATA", Path.home()))
CONFIG = APPDATA / "File2TV" / "config.json"
SENDTO = APPDATA / "Microsoft" / "Windows" / "SendTo" / f"{APP_NAME}.lnk"
# Dřív se aplikace jmenovala „TV Cast“ – nastavení a zástupce se převezmou.
OLD_CONFIG = APPDATA / "TVCast" / "config.json"
OLD_SENDTO = SENDTO.with_name("TV Cast.lnk")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def migrate_old_name(replace_shortcut: bool) -> bool:
    """Převezme nastavení po přejmenování; vrátí True, když byl „Odeslat do“ zapnutý.
    Starého zástupce smaže jen .exe, které hned vytvoří nového (ne vývojové spuštění)."""
    if not CONFIG.exists() and OLD_CONFIG.exists():
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.write_bytes(OLD_CONFIG.read_bytes())
    if not replace_shortcut or not OLD_SENDTO.exists():
        return False
    OLD_SENDTO.unlink(missing_ok=True)
    return True


def load_config() -> dict:
    # ui_lang: „auto“ = podle Windows. langs: vlastní pořadí jazyků zvuku a titulků, platí jen
    # s langs_custom – jinak se řídí jazykem aplikace (i18n.preferred_langs).
    cfg = {"device": None, "devices": {}, "ui_lang": "auto"}
    try:
        cfg |= json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


class KeepAwake:
    """Power request: PC neusne, dokud TV video stahuje (obrazovka zhasnout smí).
    Je vidět v `powercfg /requests` s důvodem."""

    POWER_REQUEST_SYSTEM_REQUIRED = 1

    class _Reason(ctypes.Structure):
        _fields_ = [("Version", wt.ULONG), ("Flags", wt.DWORD), ("SimpleReasonString", wt.LPWSTR)]

    def __init__(self) -> None:
        k = ctypes.windll.kernel32
        k.PowerCreateRequest.restype = wt.HANDLE
        self._reason = self._Reason(0, 1, t("power.reason", app=APP_NAME))  # POWER_REQUEST_CONTEXT_SIMPLE_STRING
        self._handle = k.PowerCreateRequest(ctypes.byref(self._reason))
        self.active = False

    def set(self, on: bool) -> None:
        if on == self.active or not self._handle:
            return
        k = ctypes.windll.kernel32
        (k.PowerSetRequest if on else k.PowerClearRequest)(wt.HANDLE(self._handle), self.POWER_REQUEST_SYSTEM_REQUIRED)
        self.active = on


def launch_command() -> tuple[str, str]:
    """(program, argumenty) pro spuštění aplikace – .exe, nebo Python se skriptem při vývoji."""
    if getattr(sys, "frozen", False):
        return sys.executable, ""
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    main = Path(__file__).resolve().parent.parent / "main.py"
    return str(pythonw if pythonw.exists() else sys.executable), f'"{main}"'


def _ps(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True, creationflags=NO_WINDOW)


def sendto_installed() -> bool:
    return SENDTO.exists()


def set_sendto(on: bool) -> None:
    if not on:
        SENDTO.unlink(missing_ok=True)
        return
    exe, args = launch_command()
    icon = sys.executable if getattr(sys, "frozen", False) else ""
    script = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{SENDTO}');"
              f"$s.TargetPath='{exe}';$s.Arguments='{args}';"
              f"$s.WorkingDirectory='{Path(exe).parent}';"
              + (f"$s.IconLocation='{icon},0';" if icon else "") + "$s.Save()")
    r = _ps(script)
    if r.returncode:
        raise OSError(r.stderr.strip() or t("err.shortcut"))


def firewall_program() -> str:
    """Program, který naslouchá – u venv je to základní python.exe, ne spouštěč ve venv."""
    if getattr(sys, "frozen", False):
        return sys.executable
    return str(Path(sys._base_executable if hasattr(sys, "_base_executable") else sys.executable))


def allow_firewall() -> None:
    """Pravidlo pro příchozí spojení z místní sítě; Windows se zeptá na oprávnění správce (UAC)."""
    prog = firewall_program()
    netsh = (f'advfirewall firewall add rule name="{APP_NAME}" dir=in action=allow '
             f'program="{prog}" enable=yes profile=private,domain remoteip=localsubnet')
    r = _ps(f"Start-Process netsh -Verb RunAs -WindowStyle Hidden -Wait -ArgumentList '{netsh}'")
    if r.returncode:
        raise OSError(t("err.firewall_cancelled"))


def mac_address(ip: str) -> str | None:
    """MAC adresa zařízení v místní síti (ARP), kvůli pozdějšímu zapnutí přes Wake-on-LAN."""
    mac = ctypes.c_uint64(0)
    size = wt.ULONG(6)
    try:
        dest = int.from_bytes(socket.inet_aton(ip), "little")
        if ctypes.windll.iphlpapi.SendARP(dest, 0, ctypes.byref(mac), ctypes.byref(size)):
            return None
    except OSError:
        return None
    raw = mac.value.to_bytes(8, "little")[:6]
    return ":".join(f"{b:02X}" for b in raw) if any(raw) else None


def wake_on_lan(mac: str, ip: str | None = None) -> None:
    packet = b"\xff" * 6 + bytes.fromhex(mac.replace(":", "").replace("-", "")) * 16
    targets = ["255.255.255.255"]
    if ip:
        targets.append(".".join(ip.split(".")[:3] + ["255"]))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for _ in range(3):
            for addr in targets:
                s.sendto(packet, (addr, 9))
