"""The equalizer response graph.

A ``tk.Canvas`` that draws the EQ the way FxSound does: a logarithmic
frequency axis, a linear decibel axis, one draggable point per band with its
value labelled above it, and a filled area under the curve.

Two decisions worth stating:

* The x axis is logarithmic, because that is how people hear. An octave is an
  equal distance on screen, so a 100 Hz boost and a 10 kHz cut look the same
  width, which is what makes the curve readable as "shape" rather than as
  "mostly the top end".
* Dragging moves a point vertically only. Horizontal dragging would let bands
  cross each other, and a curve whose points are out of order cannot be
  rendered as a line, so it would silently lose points.
"""

from __future__ import annotations

import math
import tkinter as tk
from typing import Callable

from .theme import (
    FAINT, GOLD, GOLD_DEEP, IDLE, MUTED, PANEL, PAPER, RIDGE, fonts,
)

#: Boost/cut limits, matching the FxSound CLI and the DSP.
GAIN_MIN_DB = -12.0
GAIN_MAX_DB = 12.0

#: The band counts the equalizer offers.
BAND_COUNTS = (5, 10, 15, 20, 31)

#: How close to a point (in pixels) a press must land to grab it.
_GRAB_RADIUS = 14


def format_frequency(hz: float) -> str:
    """A compact axis label: ``25``, ``1.6k``, ``16k``."""
    if hz >= 1000.0:
        value = hz / 1000.0
        text = f"{value:.1f}".rstrip("0").rstrip(".")
        return f"{text}k"
    if hz < 100.0:
        text = f"{hz:.1f}".rstrip("0").rstrip(".")
        return text
    return f"{hz:.0f}"


def format_gain(db: float) -> str:
    """A point label: ``+6``, ``-11``, ``0``."""
    value = int(round(db))
    if value == 0:
        return "0"
    return f"{value:+d}"


class EQGraph(tk.Canvas):
    """Draggable equalizer curve.

    ``on_change(index, gain_db)`` fires continuously while a point is dragged;
    the owner is expected to push the value to the audio engine and must not
    feed it straight back, or the drag will fight the redraw.
    """

    HEIGHT = 190

    def __init__(self, master, on_change: Callable[[int, float], None],
                 bg: str = PANEL) -> None:
        super().__init__(master, height=self.HEIGHT, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self._on_change = on_change
        self._bg = bg
        self._frequencies: list[float] = []
        self._gains: list[float] = []
        self._drag_index: int | None = None
        self._points: list[tuple[float, float]] = []
        self._enabled = True

        self.bind("<Configure>", lambda _e: self.redraw())
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Double-Button-1>", self._reset_point)

    # ------------------------------------------------------------- state --

    def set_curve(self, frequencies: list[float], gains: list[float],
                  enabled: bool = True) -> None:
        """Replace the curve. Does not fire ``on_change``."""
        count = min(len(frequencies), len(gains))
        self._frequencies = [float(f) for f in frequencies[:count]]
        self._gains = [max(GAIN_MIN_DB, min(GAIN_MAX_DB, float(g))) for g in gains[:count]]
        self._enabled = enabled
        self.redraw()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self.configure(cursor="hand2" if enabled else "arrow")
        self.redraw()

    @property
    def gains(self) -> list[float]:
        return list(self._gains)

    # ------------------------------------------------------------ drawing --

    def redraw(self) -> None:
        self.delete("all")
        width = self.winfo_width()
        height = self.winfo_height()
        if width <= 1 or height <= 1 or not self._frequencies:
            return

        left, right = 30.0, width - 12.0
        top, bottom = 16.0, height - 24.0
        if bottom <= top:
            return
        span = bottom - top
        low = min(self._frequencies)
        high = max(self._frequencies)
        if high <= low:
            high = low * 2.0
        log_low, log_high = math.log10(low), math.log10(high)

        def x_for(freq: float) -> float:
            return left + (math.log10(freq) - log_low) / (log_high - log_low) * (right - left)

        def y_for(db: float) -> float:
            return top + (GAIN_MAX_DB - db) / (GAIN_MAX_DB - GAIN_MIN_DB) * span

        colour = GOLD if self._enabled else IDLE
        muted = MUTED if self._enabled else FAINT

        # Horizontal reference lines at 0 dB and the limits.
        for db in (GAIN_MAX_DB, 0.0, GAIN_MIN_DB):
            y = y_for(db)
            self.create_line(left, y, right, y, fill=RIDGE,
                             dash=(3, 4) if db == 0.0 else (1, 5))
        self.create_text(2, y_for(GAIN_MAX_DB) + 6, text="+12", anchor="nw",
                         fill=FAINT, font=fonts().tiny)
        self.create_text(2, y_for(0.0) + 3, text="0", anchor="nw",
                         fill=FAINT, font=fonts().tiny)
        self.create_text(2, y_for(GAIN_MIN_DB) - 2, text="-12", anchor="sw",
                         fill=FAINT, font=fonts().tiny)

        # A vertical guide per band, with its frequency underneath.
        for freq in self._frequencies:
            x = x_for(freq)
            self.create_line(x, top, x, bottom, fill=RIDGE, dash=(2, 5))
            self.create_text(x, bottom + 5, text=format_frequency(freq),
                             anchor="n", fill=muted, font=fonts().tiny)

        points = [(x_for(f), y_for(g))
                  for f, g in zip(self._frequencies, self._gains)]
        self._points = points

        if len(points) >= 2:
            # Filled area under the curve, then the curve itself on top.
            polygon = points + [(points[-1][0], y_for(0.0)),
                                (points[0][0], y_for(0.0))]
            self.create_polygon(polygon, fill=GOLD_DEEP, outline="")
            self.create_line(points, fill=colour, width=2, smooth=False)

        # Handles last so they sit above the fill and stay grabbable.
        radius = 5 if self._enabled else 4
        for index, (x, y) in enumerate(points):
            self.create_oval(x - radius, y - radius, x + radius, y + radius,
                             fill=colour, outline=self._bg, width=2)
            label = format_gain(self._gains[index])
            self.create_text(x, y - radius - 5, text=label, anchor="s",
                             fill=PAPER if self._enabled else FAINT,
                             font=fonts().code_small)

    # ------------------------------------------------------------- input --

    def _nearest(self, x: float, y: float) -> int | None:
        best: int | None = None
        best_distance = _GRAB_RADIUS
        for index, (px, py) in enumerate(self._points):
            distance = math.hypot(px - x, py - y)
            # A vertical-only drag means horizontal distance should not
            # disqualify a point that is vertically right on top of it.
            if abs(px - x) > 60:
                continue
            if distance < best_distance or (best is None and abs(py - y) < 8):
                best = index
                best_distance = distance
        return best

    def _gain_at(self, y: float) -> float:
        height = self.winfo_height() or self.HEIGHT
        top, bottom = 16.0, height - 24.0
        if bottom <= top:
            return 0.0
        fraction = (y - top) / (bottom - top)
        value = GAIN_MAX_DB - fraction * (GAIN_MAX_DB - GAIN_MIN_DB)
        # The CLI works in whole-ish tenths; matching that keeps the number on
        # screen identical to the number FxSound stores.
        return round(max(GAIN_MIN_DB, min(GAIN_MAX_DB, value)) * 2.0) / 2.0

    def _press(self, event) -> None:
        if not self._enabled:
            return
        self._drag_index = self._nearest(event.x, event.y)
        if self._drag_index is not None:
            self._apply(event.y)

    def _drag(self, event) -> None:
        if self._drag_index is None:
            return
        self._apply(event.y)

    def _release(self, _event) -> None:
        self._drag_index = None

    def _reset_point(self, event) -> None:
        """Double-click a point to flatten just that band."""
        if not self._enabled:
            return
        index = self._nearest(event.x, event.y)
        if index is not None and self._gains[index] != 0.0:
            self._gains[index] = 0.0
            self.redraw()
            self._on_change(index, 0.0)

    def _apply(self, y: float) -> None:
        if self._drag_index is None:
            return
        value = self._gain_at(y)
        if value == self._gains[self._drag_index]:
            return
        self._gains[self._drag_index] = value
        self.redraw()
        self._on_change(self._drag_index, value)
