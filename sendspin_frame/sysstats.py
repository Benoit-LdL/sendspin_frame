"""CPU use, RAM use and CPU temperature, read straight from /proc and /sys (Linux)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


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

    def _ram(self) -> float | None:
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
        return 100.0 * (total - avail) / total

    def _temp(self) -> float | None:
        try:
            return int(self.thermal.read_text().strip()) / 1000
        except (OSError, ValueError):
            return None

    def read(self) -> Stats:
        return Stats(self._cpu(), self._ram(), self._temp())
