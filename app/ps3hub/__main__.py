"""Entry point: python -m ps3hub"""
from __future__ import annotations

import logging
import sys


def main() -> int:
    from .applog import configure, get_logger

    configure(logging.INFO)
    log = get_logger("main")

    from . import APP_NAME, APP_VERSION
    log.info("Starting %s %s on %s", APP_NAME, APP_VERSION, sys.platform)

    try:
        from .ui.app import HubApp
    except Exception:
        log.exception("The interface could not be loaded")
        _fatal("The interface could not be loaded. See the log for details.")
        return 1

    try:
        app = HubApp()
    except Exception:
        log.exception("The application could not start")
        _fatal("The application could not start. See the log for details.")
        return 1

    try:
        app.mainloop()
    except KeyboardInterrupt:
        log.info("Interrupted from the keyboard")
        try:
            app._on_close()
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
