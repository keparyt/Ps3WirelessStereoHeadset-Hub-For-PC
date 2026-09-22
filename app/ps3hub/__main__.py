"""Entry point: python -m ps3hub"""
from __future__ import annotations

import argparse
import logging
import sys


def main(argv: list[str] | None = None) -> int:
    from .applog import configure, get_logger

    configure(logging.INFO)
    log = get_logger("main")

    parser = argparse.ArgumentParser(
        prog="PS3HeadsetHub",
        description="PS3 Wireless Stereo Headset Hub",
    )
    parser.add_argument(
        "--tray",
        action="store_true",
        help="start minimized in the Windows system tray and keep running in the background",
    )
    args = parser.parse_args(argv)

    from . import APP_NAME, APP_VERSION
    log.info(
        "Starting %s %s on %s (tray=%s)",
        APP_NAME,
        APP_VERSION,
        sys.platform,
        args.tray,
    )

    try:
        from .report_actions import install as install_report_actions
        install_report_actions()
    except Exception:
        log.exception("The HID report/action layer could not be installed")
        _fatal("The HID report/action layer could not be loaded. See the log for details.")
        return 1

    try:
        from .ui.app import HubApp
    except Exception:
        log.exception("The interface could not be loaded")
        _fatal("The interface could not be loaded. See the log for details.")
        return 1

    try:
        app = HubApp(tray_mode=args.tray)
    except Exception:
        log.exception("The application could not start")
        _fatal("The application could not start. See the log for details.")
        return 1

    try:
        app.mainloop()
    except KeyboardInterrupt:
        log.info("Interrupted from the keyboard")
        try:
            app._shutdown()
        except Exception:
            pass
    return 0


def _fatal(message: str) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("PS3 Wireless Stereo Headset Hub", message)
        root.destroy()
    except Exception:
        print(message, file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
