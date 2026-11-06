#!/usr/bin/env bash
# Install / update sendspin-frame on Raspberry Pi OS Lite (64-bit, Bookworm or Trixie).
#
#   git clone https://github.com/Benoit-LdL/sendspin_frame.git
#   cd sendspin_frame && sudo ./install.sh
#
# Re-running it updates the app and keeps your /etc/sendspin-frame/config.toml.
# Options:  --no-boot-tweaks   don't touch /boot/firmware/cmdline.txt
set -euo pipefail

APP_DIR=/opt/sendspin-frame
CONF_DIR=/etc/sendspin-frame
SVC_USER=sendspin
FONT_DIR=/usr/local/share/fonts/inter
INTER_URL=https://github.com/rsms/inter/releases/download/v4.1/Inter-4.1.zip
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOOT_TWEAKS=1
[[ "${1:-}" == "--no-boot-tweaks" ]] && BOOT_TWEAKS=0

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

[[ $EUID -eq 0 ]] || { echo "Please run with sudo."; exit 1; }
if [[ "$(uname -m)" != "aarch64" ]]; then
    echo "This needs the 64-bit Raspberry Pi OS (aarch64); found $(uname -m)."
    echo "The Python audio/crypto libraries have no prebuilt 32-bit ARM packages."
    exit 1
fi

say "Installing system packages"
apt-get update -qq
apt-get install -y -qq libportaudio2 alsa-utils fonts-dejavu-core curl unzip ca-certificates

if ! command -v uv >/dev/null 2>&1; then
    say "Installing uv (Python package manager)"
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh
fi

say "Creating service user '$SVC_USER'"
if ! id "$SVC_USER" >/dev/null 2>&1; then
    useradd --system --user-group --home-dir /var/lib/sendspin-frame --no-create-home \
        --shell /usr/sbin/nologin "$SVC_USER"
fi
usermod -a -G audio,video,tty "$SVC_USER"

say "Installing the app into $APP_DIR"
mkdir -p "$APP_DIR"
# Copy the source (not .git / venvs) so the checkout can be deleted or updated freely.
tar -C "$SRC_DIR" --exclude=.git --exclude=.venv --exclude=__pycache__ --exclude=previews \
    -cf - . | tar -C "$APP_DIR" -xf -
# sendspin needs Python >= 3.12; uv downloads one if the system Python is older (Bookworm).
export UV_PYTHON_INSTALL_DIR="$APP_DIR/python"
uv venv --quiet --allow-existing --python ">=3.12" "$APP_DIR/.venv"
uv pip install --quiet --python "$APP_DIR/.venv/bin/python" "$APP_DIR"

if [[ ! -f "$FONT_DIR/Inter-Regular.ttf" ]]; then
    say "Installing the Inter font"
    tmp="$(mktemp -d)"
    curl -LsSf -o "$tmp/inter.zip" "$INTER_URL"
    unzip -q -o "$tmp/inter.zip" -d "$tmp/inter"
    mkdir -p "$FONT_DIR"
    for w in Light Regular Medium SemiBold Bold; do
        cp "$tmp/inter/extras/ttf/Inter-$w.ttf" "$FONT_DIR/"
    done
    cp "$tmp/inter/LICENSE.txt" "$FONT_DIR/" 2>/dev/null || true
    rm -rf "$tmp"
fi

say "Configuration"
mkdir -p "$CONF_DIR"
if [[ ! -f "$CONF_DIR/config.toml" ]]; then
    cp "$APP_DIR/config.example.toml" "$CONF_DIR/config.toml"
    echo "Created $CONF_DIR/config.toml - edit it to add your Photo Frames link."
else
    echo "Keeping existing $CONF_DIR/config.toml"
fi

say "Setting up the systemd service"
cp "$APP_DIR/systemd/sendspin-frame.service" /etc/systemd/system/
# The screen belongs to sendspin-frame now: no login prompt on the HDMI console.
# (SSH is unaffected. Undo with: sudo systemctl enable --now getty@tty1)
systemctl disable --now getty@tty1.service >/dev/null 2>&1 || true
systemctl daemon-reload
systemctl enable sendspin-frame.service >/dev/null

if [[ $BOOT_TWEAKS -eq 1 ]]; then
    CMDLINE=/boot/firmware/cmdline.txt
    [[ -f $CMDLINE ]] || CMDLINE=/boot/cmdline.txt
    if [[ -f $CMDLINE ]]; then
        say "Boot options: no console blanking, no blinking cursor, no logo ($CMDLINE)"
        cp -n "$CMDLINE" "$CMDLINE.sendspin-frame.bak"
        for opt in consoleblank=0 vt.global_cursor_default=0 logo.nologo; do
            grep -qw -- "$opt" "$CMDLINE" || sed -i "1 s/\$/ $opt/" "$CMDLINE"
        done
    fi
fi

systemctl restart sendspin-frame.service

say "Done!"
cat <<EOF
  * Status / logs:   systemctl status sendspin-frame    journalctl -u sendspin-frame -f
  * Configuration:   sudo nano $CONF_DIR/config.toml && sudo systemctl restart sendspin-frame
  * Audio outputs:   sudo -u $SVC_USER $APP_DIR/.venv/bin/sendspin audio-devices list
  * The player shows up in Music Assistant automatically (Sendspin provider, via mDNS).
EOF
[[ $BOOT_TWEAKS -eq 1 ]] && echo "  * Reboot once so the boot options take effect:  sudo reboot"
exit 0
