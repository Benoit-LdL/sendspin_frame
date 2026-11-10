"""CPU, memory, uptime and network figures, read straight from /proc and /sys (Linux)."""

from __future__ import annotations

import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Stats:
    cpu_pct: float | None
    ram_pct: float | None
    temp_c: float | None

    def text(self) -> str:
        parts = []
        if self.cpu_pct is not None:
            parts.append(f"CPU {self.cpu_pct:.0f}%")
        if self.ram_pct is not None:
            parts.append(f"RAM {self.ram_pct:.0f}%")
        if self.temp_c is not None:
            parts.append(f"{self.temp_c:.0f}°C")
        return "   ".join(parts)


class StatsReader:
    """CPU use is measured between two calls, so call `read()` at a steady interval."""

    def __init__(self, proc: str = "/proc", sys: str = "/sys") -> None:
        self.proc = Path(proc)
        self.sys = Path(sys)
        self.thermal = Path(sys) / "class/thermal/thermal_zone0/temp"
        self._last_cpu: tuple[int, int] | None = self._cpu_times()

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
        return Stats(self._cpu(), self._ram(), self._temp())

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
            "uptime": self._uptime(),
            "ip_address": self._ip() if interface else None,
            "mac_address": self._mac(interface),
            "wifi_signal": self._wifi_dbm(interface),
        }
        return {k: round(v, 1) if isinstance(v, float) else v for k, v in values.items()}
