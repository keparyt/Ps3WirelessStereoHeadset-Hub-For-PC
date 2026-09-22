#!/usr/bin/env python3
"""Launcher for the PS3 Wireless Stereo Headset Hub."""
from ps3hub.report_actions import install as install_report_actions

install_report_actions()

from ps3hub.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
