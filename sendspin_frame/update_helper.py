"""Root helper: checks for and installs updates. Run as `sendspin-frame-update <action>`.

The service runs as the unprivileged `sendspin` user, so install.sh puts a tiny wrapper at
/usr/local/sbin/sendspin-frame-update (which runs this module with the venv's Python) and a
sudoers rule that lets the service user run exactly that command as root, see
/etc/sudoers.d/sendspin-frame.

Actions:
    check     apt-get update, then print a JSON report (stdout) of what can be updated:
              Raspberry Pi OS packages, Python packages in the venv, and sendspin-frame itself
              (new commits on the branch it was installed from).
    system    apt-get full-upgrade + autoremove.
    app       Fetch the newest sendspin-frame into /usr/local/src/sendspin-frame and run its
              install.sh (which upgrades the Python packages and restarts the service). Without
              a known git origin, it only upgrades the Python packages and restarts.

`system` and `app` run detached, as a transient systemd unit (sendspin-frame-update.service),
so they survive the restart of sendspin-frame that an update brings, and so stopping
sendspin-frame can never interrupt dpkg half-way. The unit name doubles as a lock: a second
update can't start while one runs. `check` runs in the foreground and prints the report.

/etc/sendspin-frame/update.conf (written by install.sh) tells where the code came from:
    ORIGIN=https://github.com/.../sendspin_frame.git
    BRANCH=main
    COMMIT=<installed commit>
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, requires
from pathlib import Path
from typing import Any

APP_DIR = Path("/opt/sendspin-frame")
VENV_PYTHON = APP_DIR / ".venv/bin/python"
UPDATE_CONF = Path("/etc/sendspin-frame/update.conf")
SRC_DIR = Path("/usr/local/src/sendspin-frame")
UNIT = "sendspin-frame-update"
SERVICE = "sendspin-frame.service"
REBOOT_REQUIRED = Path("/var/run/reboot-required")
DIST_NAME = "sendspin-frame"
# Don't fail on another apt process: wait up to this long for its lock.
APT_LOCK_WAIT_S = 600

# `apt-get -s full-upgrade` lines: "Inst libc6 [2.36-9] (2.36-9+deb12u4 Debian:12.4 [arm64])".
# New dependencies have no "[old]" part.
_INST_RE = re.compile(r"^Inst (\S+)(?: \[([^\]]*)\])? \((\S+)")


def log(msg: str) -> None:
    print(msg, flush=True)


def run(cmd: list[str], *, check: bool = True, capture: bool = False,
        env: dict[str, str] | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    full_env.setdefault("HOME", "/root")  # uv keeps its cache under $HOME
    full_env["DEBIAN_FRONTEND"] = "noninteractive"
    if env:
        full_env.update(env)
    return subprocess.run(cmd, check=check, env=full_env, cwd=cwd, text=True,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None)


# --------------------------------------------------------------------------- pure helpers


def parse_apt_simulation(text: str) -> list[dict[str, str | None]]:
    """Packages that `apt-get -s full-upgrade` would install or upgrade."""
    found = []
    for line in text.splitlines():
        m = _INST_RE.match(line)
        if m:
            found.append({"name": m.group(1), "installed": m.group(2), "latest": m.group(3)})
    return found


def exact_pins(requirements: list[str] | None) -> set[str]:
    """Names of dependencies pinned with `==` (we can't upgrade those, so don't count them)."""
    pins = set()
    for req in requirements or []:
        m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*==", req.split(";")[0])
        if m:
            pins.add(m.group(1).lower().replace("_", "-"))
    return pins


def outdated_python_packages(listing: list[dict[str, Any]], pinned: set[str]) -> list[dict]:
    """Keep what `uv pip install --upgrade` would actually change (not pins, not ourselves)."""
    skip = pinned | {DIST_NAME}
    return [
        {"name": p["name"], "installed": p.get("version"), "latest": p.get("latest_version")}
        for p in listing
        if p.get("name", "").lower().replace("_", "-") not in skip
    ]


def parse_update_conf(text: str) -> dict[str, str]:
    conf = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        conf[key.strip()] = value.strip().strip('"')
    return conf


# --------------------------------------------------------------------------- checks


def read_update_conf() -> dict[str, str]:
    try:
        return parse_update_conf(UPDATE_CONF.read_text())
    except OSError:
        return {}


def uv_path() -> str:
    return shutil.which("uv") or "/usr/local/bin/uv"


def check_system() -> dict[str, Any]:
    lock = f"-oDPkg::Lock::Timeout={APT_LOCK_WAIT_S}"
    run(["apt-get", "-qq", lock, "update"])
    out = run(["apt-get", "-qq", "-s", lock, "full-upgrade"], capture=True).stdout
    return {"packages": parse_apt_simulation(out), "reboot_required": REBOOT_REQUIRED.exists()}


def check_python() -> dict[str, Any]:
    out = run([uv_path(), "pip", "list", "--outdated", "--format", "json",
               "--python", str(VENV_PYTHON)], capture=True).stdout
    try:
        pinned = exact_pins(requires(DIST_NAME))
    except PackageNotFoundError:
        pinned = set()
    return {"packages": outdated_python_packages(json.loads(out or "[]"), pinned)}


def git(*args: str, capture: bool = True) -> str:
    return run(["git", "-C", str(SRC_DIR), *args], capture=capture).stdout.strip()


def sync_source(conf: dict[str, str]) -> None:
    """Make /usr/local/src/sendspin-frame a fresh copy of the branch we were installed from."""
    origin, branch = conf["ORIGIN"], conf.get("BRANCH") or "main"
    if not (SRC_DIR / ".git").is_dir():
        SRC_DIR.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", "-q", "--branch", branch, origin, str(SRC_DIR)])
    else:
        git("remote", "set-url", "origin", origin)
        git("fetch", "-q", "origin", branch)


def check_app(conf: dict[str, str]) -> dict[str, Any] | None:
    if not conf.get("ORIGIN"):
        return None
    sync_source(conf)
    branch = conf.get("BRANCH") or "main"
    latest = git("rev-parse", "--short", f"origin/{branch}")
    installed = conf.get("COMMIT") or ""
    behind: int | None = None
    if installed:
        try:
            behind = int(git("rev-list", "--count", f"{installed}..origin/{branch}"))
        except (subprocess.CalledProcessError, ValueError):
            behind = None  # installed from a commit this remote doesn't know
    return {
        "installed": installed[:7] or None,
        "latest": latest,
        "behind": behind,
        "latest_change": git("log", "-1", "--format=%s", f"origin/{branch}"),
    }


def check() -> dict[str, Any]:
    report: dict[str, Any] = {"system": None, "python": None, "app": None, "errors": []}
    conf = read_update_conf()
    for key, fn in (("system", check_system), ("python", check_python),
                    ("app", lambda: check_app(conf))):
        try:
            report[key] = fn()
        except (subprocess.CalledProcessError, OSError, ValueError) as exc:
            msg = str(exc)
            if isinstance(exc, subprocess.CalledProcessError):
                tail = (exc.stderr or exc.stdout or "").strip().splitlines()
                if tail:
                    msg += f": {tail[-1].strip()}"
            report["errors"].append(f"{key}: {msg}")
    return report


# --------------------------------------------------------------------------- updates


def update_system() -> None:
    lock = f"-oDPkg::Lock::Timeout={APT_LOCK_WAIT_S}"
    log("==> apt-get update")
    run(["apt-get", "-qq", lock, "update"])
    log("==> apt-get full-upgrade")
    run(["apt-get", "-y", "-qq", lock, "-o", "Dpkg::Options::=--force-confdef",
         "-o", "Dpkg::Options::=--force-confold", "full-upgrade"])
    log("==> apt-get autoremove")
    run(["apt-get", "-y", "-qq", lock, "autoremove"])
    if REBOOT_REQUIRED.exists():
        log("A reboot is required to finish this update.")


def update_app() -> None:
    conf = read_update_conf()
    if conf.get("ORIGIN"):
        branch = conf.get("BRANCH") or "main"
        log(f"==> Fetching {conf['ORIGIN']} ({branch}) into {SRC_DIR}")
        sync_source(conf)
        git("checkout", "-q", "-B", branch, f"origin/{branch}")
        git("reset", "-q", "--hard", f"origin/{branch}")
        log(f"==> Running install.sh (now at {git('rev-parse', '--short', 'HEAD')})")
        # Installs new dependencies, upgrades the Python packages, restarts the service.
        run(["bash", str(SRC_DIR / "install.sh")], cwd=SRC_DIR)
    else:
        log("==> No git origin recorded in update.conf: upgrading the Python packages only")
        run([uv_path(), "pip", "install", "--quiet", "--upgrade",
             "--python", str(VENV_PYTHON), str(APP_DIR)])
        log(f"==> Restarting {SERVICE}")
        run(["systemctl", "restart", SERVICE])


def run_detached(action: str) -> int:
    """Run `action` as a transient systemd unit and wait for it; its output goes to the journal."""
    if not shutil.which("systemd-run"):
        print("systemd-run not found: updates need systemd", file=sys.stderr)
        return 1
    cmd = ["systemd-run", "--unit", UNIT, "--description", f"sendspin-frame update ({action})",
           "--wait", "--collect", "--quiet", "--property=Type=exec", "--property=Nice=10",
           "--", sys.executable, "-I", "-m", "sendspin_frame.update_helper", "--detached", action]
    proc = subprocess.run(cmd, check=False)
    if proc.returncode:
        print(f"update '{action}' failed (exit {proc.returncode}); see "
              f"journalctl -u {UNIT}", file=sys.stderr)
    return proc.returncode


def main(argv: list[str] | None = None) -> int:
    args = [a for a in (sys.argv[1:] if argv is None else argv)]
    detached = "--detached" in args
    args = [a for a in args if a != "--detached"]
    if len(args) != 1 or args[0] not in ("check", "system", "app"):
        print("usage: sendspin-frame-update check|system|app", file=sys.stderr)
        return 2
    action = args[0]
    if os.geteuid() != 0:
        print("sendspin-frame-update must run as root", file=sys.stderr)
        return 1
    if action == "check":
        print(json.dumps(check()))
        return 0
    if not detached:
        return run_detached(action)
    try:
        update_system() if action == "system" else update_app()
    except subprocess.CalledProcessError as exc:
        log(f"FAILED: {' '.join(exc.cmd)} exited with {exc.returncode}")
        return exc.returncode or 1
    log("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
