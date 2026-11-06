#!/usr/bin/env bash
# Apply display.resolution from the config to the kernel command line (needs a reboot).
#
#   sudo /opt/sendspin-frame/tools/apply-resolution.sh [config.toml]
#
# The Pi chooses the HDMI mode at boot from `video=HDMI-A-N:WxH@R` in cmdline.txt; it can't be
# changed at run time. install.sh runs this too.
#   display.resolution = ""            leave cmdline.txt alone
#   display.resolution = "auto"        remove our video= option (the screen's preferred mode)
#   display.resolution = "1280x720"    or "1920x1080", "1280x720@50", ...
set -euo pipefail

CONF="${1:-/etc/sendspin-frame/config.toml}"
PY=/opt/sendspin-frame/.venv/bin/python
[[ -x $PY ]] || PY=python3
[[ $EUID -eq 0 ]] || { echo "Please run with sudo."; exit 1; }

RES="$("$PY" - "$CONF" <<'EOF'
import sys, tomllib
try:
    with open(sys.argv[1], "rb") as fh:
        print(tomllib.load(fh).get("display", {}).get("resolution", ""))
except FileNotFoundError:
    print("")
EOF
)"
RES="${RES// /}"

if [[ -z $RES ]]; then
    echo "display.resolution is not set: boot resolution left unchanged."
    exit 0
fi

CMDLINE=/boot/firmware/cmdline.txt
[[ -f $CMDLINE ]] || CMDLINE=/boot/cmdline.txt
[[ -f $CMDLINE ]] || { echo "cmdline.txt not found"; exit 1; }

if [[ $RES != auto ]]; then
    if [[ ! $RES =~ ^[0-9]{3,4}x[0-9]{3,4}(@[0-9]{2,3})?$ ]]; then
        echo "Invalid display.resolution \"$RES\" (expected e.g. \"1280x720\", \"1920x1080@60\" or \"auto\")"
        exit 1
    fi
    [[ $RES == *@* ]] || RES="$RES@60"
    # Use the HDMI port a screen is connected to (HDMI-A-1 if none is detected).
    CONNECTOR=HDMI-A-1
    for status in /sys/class/drm/card*-HDMI-A-*/status; do
        [[ -f $status ]] && [[ "$(cat "$status")" == connected ]] || continue
        CONNECTOR="$(basename "$(dirname "$status")")"
        CONNECTOR="${CONNECTOR#card*-}"
        break
    done
fi

cp -n "$CMDLINE" "$CMDLINE.sendspin-frame.bak"
before="$(cat "$CMDLINE")"
# Drop any existing HDMI video= option, then add ours.
sed -i -E '1 s/ ?video=HDMI-A-[0-9]+:[^ ]*//g' "$CMDLINE"
[[ $RES == auto ]] || sed -i "1 s/\$/ video=$CONNECTOR:$RES/" "$CMDLINE"

if [[ "$(cat "$CMDLINE")" == "$before" ]]; then
    echo "Boot resolution already set ($RES)."
else
    echo "Boot resolution set to $RES${CONNECTOR:+ on $CONNECTOR}. Reboot to apply:  sudo reboot"
fi
