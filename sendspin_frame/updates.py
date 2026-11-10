"""Update checks and updates, driven from Home Assistant (see mqtt.py for the entities).

All the privileged work happens in the root helper (update_helper.py, run through sudo). This
side runs the checks on a schedule, keeps the last report, starts updates when a button is
pressed in Home Assistant, and tells the MQTT bridge when anything changed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .config import UpdatesConfig

logger = logging.getLogger(__name__)

STARTUP_DELAY_S = 90  # let the network and the player settle first
CHECK_TIMEOUT_S = 15 * 60
UPDATE_TIMEOUT_S = 3 * 60 * 60

STATUS_TEXT = {
    "idle": "Idle",
    "checking": "Checking",
    "updating_system": "Updating system packages",
    "updating_app": "Updating sendspin-frame",
    "failed": "Failed",
}


@dataclass
class Report:
    """What the helper's `check` found. Lists hold {"name", "installed", "latest"} dicts."""

    # None = that part of the check failed (see `errors`), so the count is unknown.
    system: list[dict[str, Any]] | None = None
    reboot_required: bool = False
    python: list[dict[str, Any]] | None = None
    app: dict[str, Any] | None = None  # installed, latest, behind, latest_change; None = n/a
    errors: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Report:
        system, python = data.get("system"), data.get("python")
        return cls(
            system=None if system is None else list(system.get("packages") or []),
            reboot_required=bool((system or {}).get("reboot_required")),
            python=None if python is None else list(python.get("packages") or []),
            app=data.get("app") or None,
            errors=[str(e) for e in data.get("errors") or []],
        )

    @property
    def app_behind(self) -> int:
        return int((self.app or {}).get("behind") or 0)

    @property
    def system_count(self) -> int | None:
        return None if self.system is None else len(self.system)

    @property
    def app_count(self) -> int | None:
        """Things the "Update sendspin-frame" button would change: packages + the app itself."""
        if self.python is None:
            return None
        return len(self.python) + (1 if self.app_behind > 0 else 0)


class UpdateManager:
    def __init__(self, cfg: UpdatesConfig, helper: list[str],
                 on_change: Callable[[], None] | None = None) -> None:
        self.cfg = cfg
        self.helper = helper  # e.g. ["sudo", "-n", "/usr/local/sbin/sendspin-frame-update"]
        self.on_change = on_change or (lambda: None)
        self.status = "idle"
        self.error: str | None = None
        self.report: Report | None = None
        self.last_check: float | None = None  # time.time()
        self._lock = asyncio.Lock()
        self._wake: asyncio.Event | None = None
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ values for the entities

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    def system_count(self) -> int | None:
        return self.report.system_count if self.report else None

    def system_attributes(self) -> dict[str, Any]:
        r = self.report
        return {
            "packages": [_fmt(p) for p in r.system or []] if r else [],
            "reboot_required": r.reboot_required if r else False,
            "last_check": self.last_check_iso(),
        }

    def app_count(self) -> int | None:
        return self.report.app_count if self.report else None

    def app_attributes(self) -> dict[str, Any]:
        r = self.report
        app = (r.app if r else None) or {}
        return {
            "python_packages": [_fmt(p) for p in r.python or []] if r else [],
            "installed_version": app.get("installed"),
            "latest_version": app.get("latest"),
            "commits_behind": app.get("behind"),
            "latest_change": app.get("latest_change"),
            "last_check": self.last_check_iso(),
        }

    def status_text(self) -> str:
        return STATUS_TEXT[self.status]

    def status_attributes(self) -> dict[str, Any]:
        return {
            "error": self.error,
            "last_check": self.last_check_iso(),
            "check_errors": list(self.report.errors) if self.report else [],
        }

    def last_check_iso(self) -> str | None:
        if self.last_check is None:
            return None
        return datetime.fromtimestamp(self.last_check, tz=UTC).isoformat(timespec="seconds")

    # ------------------------------------------------------------------ scheduling

    async def run(self) -> None:
        """Check on start-up and then every `check_hours` (or only on request when 0)."""
        self._wake = asyncio.Event()
        delay: float | None = STARTUP_DELAY_S
        while True:
            try:
                async with asyncio.timeout(delay):
                    await self._wake.wait()
            except TimeoutError:
                pass
            self._wake.clear()
            await self.check()
            delay = self.cfg.check_hours * 3600 if self.cfg.check_hours > 0 else None

    def request_check(self) -> None:
        """"Check for updates" button."""
        if self.busy:
            logger.info("Updates: busy (%s), check request ignored", self.status)
            return
        if self._wake is not None:
            self._wake.set()
        else:
            self._spawn(self.check())

    def request_update(self, what: str) -> None:
        """"Update system packages" / "Update sendspin-frame" buttons ("system" / "app")."""
        if self.busy:
            logger.info("Updates: busy (%s), '%s' update request ignored", self.status, what)
            return
        self._spawn(self.update(what))

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------ actions

    async def check(self) -> bool:
        if self.busy:
            return False
        async with self._lock:
            self._set_status("checking")
            code, out, err = await self._helper("check", CHECK_TIMEOUT_S)
            if code != 0:
                self._set_status("failed", f"check failed: {err or out or f'exit {code}'}")
                return False
            try:
                self.report = Report.from_dict(json.loads(out))
            except (ValueError, TypeError) as exc:
                self._set_status("failed", f"check gave no report: {exc}")
                return False
            self.last_check = time.time()
            for e in self.report.errors:
                logger.warning("Updates: check: %s", e)
            r = self.report
            logger.info("Updates: %s system package(s), %s Python package(s), app %s",
                        "?" if r.system is None else r.system_count,
                        "?" if r.python is None else len(r.python),
                        f"{r.app_behind} commit(s) behind" if r.app else "n/a")
            self._set_status("idle")
            return True

    async def update(self, what: str) -> bool:
        if what not in ("system", "app"):
            raise ValueError(what)
        if self.busy:
            return False
        async with self._lock:
            self._set_status(f"updating_{what}")
            logger.info("Updates: starting '%s' update", what)
            code, out, err = await self._helper(what, UPDATE_TIMEOUT_S)
            if code != 0:
                # After an "app" update the service is restarted, which also ends up here;
                # the new instance then does its own check.
                self._set_status("failed", f"{what} update failed: {err or out or f'exit {code}'}")
                return False
            logger.info("Updates: '%s' update done", what)
            self._set_status("idle")
        await self.check()
        return True

    async def _helper(self, action: str, timeout: float) -> tuple[int, str, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.helper, action, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except OSError as exc:
            return 1, "", f"cannot run {self.helper[0]}: {exc}"
        try:
            async with asyncio.timeout(timeout):
                out, err = await proc.communicate()
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return 1, "", f"timed out after {timeout:.0f}s"
        return proc.returncode or 0, out.decode(errors="replace").strip(), \
            _last_line(err.decode(errors="replace"))

    def _set_status(self, status: str, error: str | None = None) -> None:
        self.status, self.error = status, error
        if error:
            logger.warning("Updates: %s", error)
        self.on_change()


def _fmt(p: dict[str, Any]) -> str:
    installed, latest = p.get("installed"), p.get("latest")
    if installed and latest:
        return f"{p['name']} {installed} -> {latest}"
    return f"{p['name']} {latest or installed or ''}".strip()


def _last_line(text: str) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return lines[-1].strip() if lines else ""
