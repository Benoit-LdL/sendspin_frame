"""Update checks / updates from Home Assistant (no root, apt or MQTT broker needed)."""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap

from sendspin_frame import update_helper as helper
from sendspin_frame.config import MqttConfig, UpdatesConfig
from sendspin_frame.mqtt import MqttBridge, make_update_entities
from sendspin_frame.updates import Report, UpdateManager

APT_SIM = textwrap.dedent("""\
    Inst libc6 [2.36-9+deb12u3] (2.36-9+deb12u4 Debian:12.5/stable [arm64])
    Conf libc6 (2.36-9+deb12u4 Debian:12.5/stable [arm64])
    Inst raspberrypi-sys-mods [20231127] (20240101 Raspbian:bookworm [all])
    Inst linux-image-6.6.31+rpt-rpi-v8 (1:6.6.31-1+rpt1 Raspbian:bookworm [arm64])
    Remv old-package [1.0]
""")


def test_helper_parses_apt_simulation():
    pkgs = helper.parse_apt_simulation(APT_SIM)
    assert [p["name"] for p in pkgs] == ["libc6", "raspberrypi-sys-mods",
                                         "linux-image-6.6.31+rpt-rpi-v8"]
    assert pkgs[0]["installed"] == "2.36-9+deb12u3" and pkgs[0]["latest"] == "2.36-9+deb12u4"
    assert pkgs[2]["installed"] is None  # a new dependency


def test_helper_skips_pinned_and_itself():
    pins = helper.exact_pins(["sendspin==9.1.0", "pillow>=10.1", 'aiomqtt>=2.3; python_version>"3"',
                              "Some_Thing == 1.0"])
    assert pins == {"sendspin", "some-thing"}
    listing = [
        {"name": "sendspin", "version": "9.1.0", "latest_version": "9.2.0"},
        {"name": "pillow", "version": "10.1.0", "latest_version": "11.0.0"},
        {"name": "sendspin-frame", "version": "0.1.0", "latest_version": "9.9"},
    ]
    out = helper.outdated_python_packages(listing, pins)
    assert out == [{"name": "pillow", "installed": "10.1.0", "latest": "11.0.0"}]


def test_helper_parses_update_conf():
    conf = helper.parse_update_conf('# comment\nORIGIN="https://x/y.git"\nBRANCH=main\n'
                                    'COMMIT=abc\n\nbad line\n')
    assert conf == {"ORIGIN": "https://x/y.git", "BRANCH": "main", "COMMIT": "abc"}


def test_helper_usage_and_root_check():
    assert helper.main(["bogus"]) == 2
    assert helper.main([]) == 2


def test_report_counts():
    r = Report.from_dict({
        "system": {"packages": [{"name": "a", "installed": "1", "latest": "2"}],
                   "reboot_required": True},
        "python": {"packages": [{"name": "pillow", "installed": "1", "latest": "2"},
                                {"name": "numpy", "installed": "1", "latest": "2"}]},
        "app": {"installed": "abc", "latest": "def", "behind": 3, "latest_change": "Fix"},
        "errors": [],
    })
    assert (r.system_count, r.app_count, r.reboot_required) == (1, 3, True)
    # A part that failed stays unknown instead of showing 0.
    assert Report.from_dict({}).app_count is None and Report.from_dict({}).system_count is None
    assert Report.from_dict({"python": {"packages": []}, "app": {"behind": None}}).app_count == 0


def _fake_helper(tmp_path, report: dict, fail_action: str | None = None) -> list[str]:
    """A stand-in for `sudo sendspin-frame-update`: logs the actions it gets to a file."""
    script = tmp_path / "helper.py"
    log = tmp_path / "actions.log"
    script.write_text(textwrap.dedent(f"""\
        import json, sys
        action = sys.argv[1]
        open({str(log)!r}, "a").write(action + "\\n")
        if action == {fail_action!r}:
            print("boom", file=sys.stderr); sys.exit(3)
        if action == "check":
            print({json.dumps(report)!r})
        """))
    return [sys.executable, "-I", str(script)]


def _actions(tmp_path) -> list[str]:
    return (tmp_path / "actions.log").read_text().split()


def test_manager_check_and_update(tmp_path):
    report = {
        "system": {"packages": [{"name": "libc6", "installed": "1", "latest": "2"}],
                   "reboot_required": False},
        "python": {"packages": [{"name": "pillow", "installed": "10", "latest": "11"}]},
        "app": {"installed": "abc1234", "latest": "def5678", "behind": 2, "latest_change": "x"},
        "errors": ["python: uv not found"],
    }
    changes = []
    m = UpdateManager(UpdatesConfig(), _fake_helper(tmp_path, report),
                      on_change=lambda: changes.append(m.status))

    async def go():
        assert m.system_count() is None  # unknown before the first check
        assert await m.check()
        assert m.system_count() == 1 and m.app_count() == 2
        assert m.system_attributes()["packages"] == ["libc6 1 -> 2"]
        attrs = m.app_attributes()
        assert attrs["python_packages"] == ["pillow 10 -> 11"]
        assert (attrs["installed_version"], attrs["commits_behind"]) == ("abc1234", 2)
        assert m.status_text() == "Idle"
        assert m.status_attributes()["check_errors"] == ["python: uv not found"]
        assert m.status_attributes()["last_check"].endswith("+00:00")
        assert changes == ["checking", "idle"]
        assert await m.update("system")
        assert _actions(tmp_path) == ["check", "system", "check"]
        assert changes[2:] == ["updating_system", "idle", "checking", "idle"]

    asyncio.run(go())


def test_manager_reports_failures_and_ignores_requests_while_busy(tmp_path):
    m = UpdateManager(UpdatesConfig(), _fake_helper(tmp_path, {}, fail_action="app"))

    async def go():
        assert not await m.update("app")
        assert m.status_text() == "Failed" and "boom" in m.error
        assert _actions(tmp_path) == ["app"]  # no re-check after a failure
        # Buttons do nothing while an action runs.
        task = asyncio.ensure_future(m.update("system"))
        await asyncio.sleep(0)
        assert m.busy
        m.request_update("app")
        m.request_check()
        assert await task
        await asyncio.sleep(0.2)
        assert _actions(tmp_path) == ["app", "system", "check"]

    asyncio.run(go())


def test_manager_handles_missing_helper():
    m = UpdateManager(UpdatesConfig(), ["/nonexistent/sendspin-frame-update"])
    assert not asyncio.run(m.check())
    assert m.status == "failed" and "cannot run" in m.error


def test_update_entities_discovery(tmp_path):
    m = UpdateManager(UpdatesConfig(), _fake_helper(tmp_path, {}))
    ents = make_update_entities(m)
    b = MqttBridge(MqttConfig(host="x"), "TV", ents)
    by_key = {e.key: e for e in ents}
    p = json.loads(b.discovery_payload(by_key["system_updates"]))
    assert p["state_topic"] == "sendspin_frame/tv/system_updates"
    assert p["json_attributes_topic"] == "sendspin_frame/tv/system_updates/attributes"
    assert p["unit_of_measurement"] == "packages" and "command_topic" not in p
    assert b.discovery_topic(by_key["system_updates"]) == "homeassistant/sensor/tv/system_updates/config"
    p = json.loads(b.discovery_payload(by_key["update_system"]))
    assert p["command_topic"] == "sendspin_frame/tv/update_system/set"
    assert p["payload_press"] == "PRESS" and "state_topic" not in p
    assert json.loads(b.discovery_payload(by_key["update_status"]))["entity_category"] == "diagnostic"
    # Unknown counts aren't published (the retained value stays); the status always is.
    assert by_key["system_updates"].state_payload() is None
    assert by_key["update_status"].state_payload() == "Idle"
    assert not by_key["update_system"].restorable and by_key["update_system"].has_command
