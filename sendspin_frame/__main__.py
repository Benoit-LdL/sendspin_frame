"""Entry point.

    sendspin-frame [--frame-config FILE] [any `sendspin daemon` option ...]

Everything that isn't a --frame-* option is handed to the upstream `sendspin daemon` command,
so --name, --audio-device, --url, --output-delay-ms, --settings-dir ... all work as documented
in the sendspin CLI.
"""

from __future__ import annotations

import argparse
import os
import sys

DEFAULT_CONFIG = "/etc/sendspin-frame/config.toml"


def main() -> int:
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--frame-config", default=os.environ.get("SENDSPIN_FRAME_CONFIG", DEFAULT_CONFIG))
    ap.add_argument("--frame-device", help='override display.device, e.g. "png:/tmp/screen.png"')
    ap.add_argument("--frame-help", action="store_true")
    ours, rest = ap.parse_known_args()
    if ours.frame_help:
        print(__doc__)
        ap.print_help()
        return 0

    from sendspin import cli as sendspin_cli
    from sendspin.daemon import daemon as daemon_mod

    from .config import load_config
    from .daemon import FrameDaemon, FrameRuntime

    cfg = load_config(ours.frame_config)
    if ours.frame_device:
        cfg.display.device = ours.frame_device

    FrameDaemon.runtime = FrameRuntime(cfg)
    daemon_mod.SendspinDaemon = FrameDaemon  # picked up by `sendspin daemon` at run time
    # Options from the config file first, so command line options can override them.
    from_config = []
    if cfg.sendspin.name:
        from_config += ["--name", cfg.sendspin.name]
    if cfg.sendspin.audio_device:
        from_config += ["--audio-device", cfg.sendspin.audio_device]
    if cfg.sendspin.server_url:
        from_config += ["--url", cfg.sendspin.server_url]
    if cfg.sendspin.delay_ms:
        from_config += ["--output-delay-ms", str(cfg.sendspin.delay_ms)]
    from_config += cfg.sendspin.extra_args
    sys.argv = ["sendspin", "daemon", *from_config, *rest]
    return sendspin_cli.main()


if __name__ == "__main__":
    sys.exit(main())
