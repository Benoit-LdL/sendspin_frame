"""CPU, memory, uptime and network figures, read straight from /proc and /sys (Linux).

Also, for Home Assistant: how long the audio process's event loop was held up (LoopTimer) and
why sendspin-frame last (re)started (RunRecord).
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Stats:
    cpu_pct: float | None
    ram_pct: float | None
    temp_c: float | None
    wifi_dbm: float | None = None  # None on a wired connection

    def items(self) -> tuple[tuple[str, str], ...]:
        """(icon, value) pairs for the screen; icons: cpu, ram, temp, wifi (see renderer)."""
        parts = []
        if self.cpu_pct is not None:
            parts.append(("cpu", f"{self.cpu_pct:.0f}%"))
        if self.ram_pct is not None:
            parts.append(("ram", f"{self.ram_pct:.0f}%"))
        if self.temp_c is not None:
            parts.append(("temp", f"{self.temp_c:.0f}°C"))
        if self.wifi_dbm is not None:
            parts.append(("wifi", f"{wifi_quality(self.wifi_dbm)}%"))
        return tuple(parts)


def wifi_quality(dbm: float) -> int:
    """Signal level as a 0-100 % quality, the usual way: -100 dBm = 0 %, -50 dBm or better =
    100 %."""
    return max(0, min(100, round(2 * (dbm + 100))))


class StatsReader:
    """CPU use is measured between two calls, so call `read()` at a steady interval."""

    def __init__(self, proc: str = "/proc", sys: str = "/sys") -> None:
        self.proc = Path(proc)
        self.sys = Path(sys)
        self.thermal = Path(sys) / "class/thermal/thermal_zone0/temp"
        self._last_cpu: tuple[int, int] | None = self._cpu_times()
        self._boot: float | None = None

    def _cpu_times(self) -> tuple[int, int] | None:
        try:
            fields = (self.proc / "stat").read_text().split("\n", 1)[0].split()[1:]
            values = [int(v) for v in fields]
        except (OSError, ValueError, IndexError):
            return None
        idle = values[3] + (values[4] if len(values) > 4 else 0)  # idle + iowait
        return idle, sum(values[:8])  # leave out guest time, already counted in user

    def _cpu(self) -> float | None:
        now = self._cpu_times()
        last, self._last_cpu = self._last_cpu, now
        if now is None or last is None or now[1] <= last[1]:
            return None
        return 100.0 * (1 - (now[0] - last[0]) / (now[1] - last[1]))

    def _meminfo(self) -> tuple[int, int] | None:
        """(total, available) in kB."""
        info: dict[str, int] = {}
        try:
            for line in (self.proc / "meminfo").read_text().splitlines():
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    info[key] = int(rest.split()[0])
        except (OSError, ValueError, IndexError):
            return None
        total, avail = info.get("MemTotal"), info.get("MemAvailable")
        if not total or avail is None:
            return None
        return total, avail

    def _ram(self) -> float | None:
        mem = self._meminfo()
        return None if mem is None else 100.0 * (mem[0] - mem[1]) / mem[0]

    def _temp(self) -> float | None:
        try:
            return int(self.thermal.read_text().strip()) / 1000
        except (OSError, ValueError):
            return None

    def read(self) -> Stats:
        return Stats(self._cpu(), self._ram(), self._temp(), self._wifi_dbm(self._interface()))

    # ------------------------------------------------------------------ for Home Assistant

    def _cpu_mhz(self) -> float | None:
        try:
            path = self.sys / "devices/system/cpu/cpu0/cpufreq/scaling_cur_freq"
            return int(path.read_text().strip()) / 1000  # kHz
        except (OSError, ValueError):
            return None

    def _uptime(self) -> int | None:
        try:
            return int(float((self.proc / "uptime").read_text().split()[0]))
        except (OSError, ValueError, IndexError):
            return None

    def _last_boot(self) -> str | None:
        """When the Pi started, as an ISO time (Home Assistant shows "2 days ago").

        Worked out from the uptime, so it follows the clock being set after boot (the Pi has
        no clock of its own); a jitter of a few seconds is ignored, so the value stays put.
        """
        up = self._uptime()
        if up is None:
            return None
        boot = time.time() - up
        if self._boot is None or abs(boot - self._boot) > 60:
            self._boot = boot
        return datetime.fromtimestamp(self._boot, UTC).isoformat(timespec="seconds")

    def _interface(self) -> str | None:
        """The network interface of the default route (the lowest metric wins)."""
        best: tuple[int, str] | None = None
        try:
            for line in (self.proc / "net/route").read_text().splitlines()[1:]:
                f = line.split()  # Iface Destination Gateway Flags RefCnt Use Metric Mask ...
                if len(f) < 7 or f[1] != "00000000" or not int(f[3], 16) & 1:
                    continue  # not a default route, or down
                if best is None or int(f[6]) < best[0]:
                    best = int(f[6]), f[0]
        except (OSError, ValueError):
            return None
        return best[1] if best else None

    def _mac(self, interface: str | None) -> str | None:
        if not interface:
            return None
        try:
            return (self.sys / "class/net" / interface / "address").read_text().strip() or None
        except OSError:
            return None

    def _wifi_dbm(self, interface: str | None) -> float | None:
        """Signal level of `interface`; None on a wired connection."""
        try:
            for line in (self.proc / "net/wireless").read_text().splitlines()[2:]:
                name, _, rest = line.partition(":")
                if name.strip() == interface:
                    return float(rest.split()[2].rstrip("."))  # status, link, level, noise
        except (OSError, ValueError, IndexError):
            pass
        return None

    @staticmethod
    def _ip() -> str | None:
        """Our address on the default route. Connecting a UDP socket sends nothing."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("192.0.2.1", 9))
                return s.getsockname()[0]
        except OSError:
            return None

    def details(self) -> dict[str, Any]:
        """Everything for the Home Assistant sensors, by entity key (None = unknown).

        CPU use is measured since the previous call, like `read()`.
        """
        mem = self._meminfo()
        interface = self._interface()
        values = {
            "cpu_temperature": self._temp(),
            "cpu_frequency": self._cpu_mhz(),
            "cpu_use": self._cpu(),
            "ram_use": None if mem is None else 100.0 * (mem[0] - mem[1]) / mem[0],
            "free_memory": None if mem is None else mem[1] / 1024,  # MB
            "uptime": self._last_boot(),
            "ip_address": self._ip() if interface else None,
            "mac_address": self._mac(interface),
            "wifi_signal": self._wifi_dbm(interface),
        }
        return {k: round(v, 1) if isinstance(v, float) else v for k, v in values.items()}


class LoopTimer:
    """How long the asyncio loop (the audio process's) was held up: Home Assistant's "Loop time".

    Like ESPHome's loop time it shows when something keeps the process from doing its work. Here
    it's the longest delay of a tick, so a few ms is normal; a high value means the audio may
    stutter.
    """

    TICK_S = 0.5

    def __init__(self) -> None:
        self._worst: float | None = None

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            start = loop.time()
            await asyncio.sleep(self.TICK_S)
            self.add((loop.time() - start - self.TICK_S) * 1000)

    def add(self, late_ms: float) -> None:
        self._worst = late_ms if self._worst is None else max(self._worst, late_ms)

    def take(self) -> float | None:
        """The longest delay (ms) since the previous call; None before the first tick."""
        worst, self._worst = self._worst, None
        return None if worst is None else round(max(0.0, worst), 1)


class RunRecord:
    """Why sendspin-frame (re)started: Home Assistant's "Reset reason".

    A small JSON file (in the settings directory) remembers the boot, the installed version and
    whether the last run stopped cleanly. Stopping the service, rebooting and updating all stop
    it cleanly; a power cut or a crash doesn't.
    """

    def __init__(self, path: Path | str, version: str,
                 boot_id_file: str = "/proc/sys/kernel/random/boot_id") -> None:
        self.path = Path(path)
        self.version = version
        try:
            self.boot_id = Path(boot_id_file).read_text().strip()
        except OSError:
            self.boot_id = ""
        self._data: dict[str, Any] = {}

    def start(self) -> str:
        """Record this start and return why it happened."""
        try:
            last = json.loads(self.path.read_text())
            if not isinstance(last, dict):
                raise ValueError
        except (OSError, ValueError):
            last = None
        self._data = {"boot_id": self.boot_id, "version": self.version, "clean": False}
        self._write()
        return self.reason(last)

    def reason(self, last: dict[str, Any] | None) -> str:
        if last is None:
            return "First start"
        same_boot = bool(self.boot_id) and last.get("boot_id") == self.boot_id
        if last.get("restart_requested") and not same_boot:
            return "Restart from Home Assistant"
        if not last.get("clean"):
            return "sendspin-frame crashed" if same_boot else "Power loss or crash"
        if not same_boot:
            return "Pi restarted"
        if last.get("version") != self.version:
            return "sendspin-frame updated"
        return "sendspin-frame restarted"

    def restart_requested(self, requested: bool = True) -> None:
        self._data["restart_requested"] = requested
        self._write()

    def stopped(self) -> None:
        self._data["clean"] = True
        self._write()

    def _write(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data))
            os.replace(tmp, self.path)
        except OSError:
            pass  # only the reset reason is lost
