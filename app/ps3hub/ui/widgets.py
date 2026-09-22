"""Reusable widgets.

Tk ships nothing that looks like this, so the pieces that carry meaning are
drawn on a Canvas. Two deliberate choices:

* Panels are flat with a hairline border. Only the volume meter is rounded, so
  roundness marks the one element that matters most instead of decorating
  everything equally.
* The volume meter is ten discrete segments because the hardware has ten
  discrete steps. A smooth slider would imply a resolution the headset does
  not have.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable

from .theme import (
    ABYSS, DECK, FAINT, FAULT, ICE, ICE_DEEP, ICE_WASH, IDLE, LIVE, MUTED,
    PANEL, PANEL_HI, PAPER, RADIUS, RADIUS_SMALL, RIDGE, SELECTION, WARN,
    fonts, round_rect,
)


class Card(tk.Frame):
    """A flat surface with a hairline border and an optional heading."""

    def __init__(self, master, title: str = "", subtitle: str = "",
                 padding: int = 16, **kwargs) -> None:
        super().__init__(
            master, bg=PANEL, highlightthickness=1,
            highlightbackground=RIDGE, highlightcolor=RIDGE, bd=0, **kwargs
        )
        font = fonts()
        self._padding = padding
        self.header: tk.Frame | None = None
        self.title_label: tk.Label | None = None

        if title:
            self.header = tk.Frame(self, bg=PANEL)
            self.header.pack(fill="x", padx=padding, pady=(padding, 0))
            self.title_label = tk.Label(
                self.header, text=title, bg=PANEL, fg=PAPER, font=font.title,
                anchor="w",
            )
            self.title_label.pack(side="left")
            if subtitle:
                tk.Label(
                    self.header, text=subtitle, bg=PANEL, fg=MUTED,
                    font=font.small, anchor="w",
                ).pack(side="left", padx=(10, 0), pady=(3, 0))

        self.body = tk.Frame(self, bg=PANEL)
        self.body.pack(
            fill="both", expand=True, padx=padding,
            pady=(10 if title else padding, padding),
        )


class StatusPill(tk.Canvas):
    """A dot plus a word. The word is what carries the meaning."""

    HEIGHT = 26

    def __init__(self, master, text: str = "Unknown", color: str = IDLE,
                 bg: str = PANEL, width: int = 150) -> None:
        super().__init__(master, height=self.HEIGHT, width=width, bg=bg,
                         highlightthickness=0, bd=0)
        self._bg = bg
        self._text = text
        self._color = color
        self.bind("<Configure>", lambda _event: self._draw())
        self._draw()

    def set(self, text: str, color: str) -> None:
        if text == self._text and color == self._color:
            return
        self._text = text
        self._color = color
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        font = fonts()
        height = self.winfo_height() or self.HEIGHT
        width = self.winfo_width() or 150
        top = (height - 22) / 2
        text_width = font.strong[1] * 0.68 * len(self._text) + 40
        pill_width = min(width, max(74, text_width))
        round_rect(self, 0, top, pill_width, top + 22, RADIUS_SMALL + 4,
                   fill=self._shade(), outline="")
        cy = top + 11
        self.create_oval(11, cy - 3.5, 18, cy + 3.5, fill=self._color, outline="")
        self.create_text(26, cy, text=self._text, anchor="w", fill=PAPER,
                         font=font.strong)

    def _shade(self) -> str:
        return {
            LIVE: "#12332A", WARN: "#33290F", FAULT: "#361A1D",
            ICE: ICE_WASH, IDLE: "#132430",
        }.get(self._color, "#132430")


class VolumeLadder(tk.Canvas):
    """Ten segments representing the headset volume scale, plus the percentage.

    This is the hero element of the dashboard. It is honest by construction:
    you can count the steps the device actually has.
    """

    SEGMENTS = 10
    HEIGHT = 108

    def __init__(self, master, bg: str = PANEL) -> None:
        super().__init__(master, height=self.HEIGHT, bg=bg,
                         highlightthickness=0, bd=0)
        self._bg = bg
        self._level: int | None = None
        self._percent: int | None = None
        self._muted = False
        self.bind("<Configure>", lambda _event: self._draw())

    def set(self, level: int | None, percent: int | None, muted: bool = False) -> None:
        if (level, percent, muted) == (self._level, self._percent, self._muted):
            return
        self._level, self._percent, self._muted = level, percent, muted
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        font = fonts()
        width = self.winfo_width()
        if width <= 1:
            return

        known = self._level is not None
        reading = f"{self._percent}%" if self._percent is not None else "--"
        self.create_text(0, 8, text=reading, anchor="nw", fill=PAPER if known else FAINT,
                         font=font.display)
        caption = (
            f"level {min(max(self._level or 0, 0), self.SEGMENTS)} of {self.SEGMENTS}" if known
            else "waiting for the headset"
        )
        self.create_text(0, 56, text=caption, anchor="nw", fill=MUTED, font=font.small)

        top, bottom = 80, 100
        gap = 5
        seg_width = (width - gap * (self.SEGMENTS - 1)) / self.SEGMENTS
        for index in range(self.SEGMENTS):
            x1 = index * (seg_width + gap)
            x2 = x1 + seg_width
            filled = known and index < min(max(self._level or 0, 0), self.SEGMENTS)
            if filled:
                fill = IDLE if self._muted else self._segment_color(index)
            else:
                # Visible but clearly empty: the point of the ladder is that
                # you can see all ten headset volume levels while the receiver status is mapped onto them.
                fill = "#1A3543"
            round_rect(self, x1, top, x2, bottom, 3, fill=fill, outline="")

    def _segment_color(self, index: int) -> str:
        # The top two steps shift warm: on this headset they are genuinely loud.
        if index >= 8:
            return WARN
        return ICE


class BatteryGauge(tk.Canvas):
    """A battery outline that fills, with the number alongside."""

    HEIGHT = 46

    def __init__(self, master, bg: str = PANEL) -> None:
        super().__init__(master, height=self.HEIGHT, bg=bg,
                         highlightthickness=0, bd=0)
        self._percent: int | None = None
        self._charging = False
        self.bind("<Configure>", lambda _event: self._draw())

    def set(self, percent: int | None, charging: bool) -> None:
        if (percent, charging) == (self._percent, self._charging):
            return
        self._percent, self._charging = percent, charging
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        font = fonts()
        if self.winfo_width() <= 1:
            return

        x, y, w, h = 1, 12, 46, 22
        round_rect(self, x, y, x + w, y + h, 4, fill="", outline=RIDGE, width=1)
        self.create_rectangle(x + w + 1, y + 7, x + w + 4, y + h - 7,
                              fill=RIDGE, outline="")

        if self._charging:
            colour = ICE
            fraction = 1.0
        elif self._percent is None:
            colour = IDLE
            fraction = 0.0
        else:
            fraction = max(0.0, min(1.0, self._percent / 100))
            colour = FAULT if self._percent <= 10 else WARN if self._percent <= 25 else LIVE

        if fraction > 0:
            inner = (w - 6) * fraction
            round_rect(self, x + 3, y + 3, x + 3 + max(3, inner), y + h - 3, 2,
                       fill=colour, outline="")

        if self._charging:
            label, sub = "Charging", "level not reported while charging"
        elif self._percent is None:
            label, sub = "Unknown", "no reading yet"
        else:
            label, sub = f"{self._percent}%", "battery"
        self.create_text(x + w + 16, y + 3, text=label, anchor="nw", fill=PAPER,
                         font=font.strong)
        self.create_text(x + w + 16, y + 20, text=sub, anchor="nw", fill=MUTED,
                         font=font.tiny)


class BalanceBar(tk.Canvas):
    """Game/chat balance as a marker on a track."""

    HEIGHT = 44

    def __init__(self, master, bg: str = PANEL) -> None:
        super().__init__(master, height=self.HEIGHT, bg=bg,
                         highlightthickness=0, bd=0)
        self._value: int | None = None
        self.bind("<Configure>", lambda _event: self._draw())

    def set(self, value: int | None) -> None:
        if value == self._value:
            return
        self._value = value
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        font = fonts()
        width = self.winfo_width()
        if width <= 1:
            return

        self.create_text(0, 0, text="Game", anchor="nw", fill=MUTED, font=font.tiny)
        self.create_text(width, 0, text="Chat", anchor="ne", fill=MUTED, font=font.tiny)

        top = 18
        round_rect(self, 0, top, width, top + 8, 4, fill="#122531", outline="")
        if self._value is None:
            self.create_text(width / 2, top + 22, text="No reading yet", fill=FAINT,
                             font=font.tiny)
            return

        fraction = max(0.0, min(1.0, self._value / 100))
        x = fraction * width
        round_rect(self, 0, top, max(6, x), top + 8, 4, fill=ICE_DEEP, outline="")
        self.create_oval(x - 6, top - 2, x + 6, top + 10, fill=ICE, outline=PANEL,
                         width=2)
        self.create_text(width / 2, top + 22, text=f"{self._value}% toward chat",
                         fill=MUTED, font=font.tiny)


class NavButton(tk.Frame):
    """One entry in the left rail."""

    def __init__(self, master, text: str, glyph: str,
                 command: Callable[[], None]) -> None:
        super().__init__(master, bg=DECK, cursor="hand2")
        font = fonts()
        self._command = command
        self._selected = False

        self._marker = tk.Frame(self, bg=DECK, width=3)
        self._marker.pack(side="left", fill="y")

        self._inner = tk.Frame(self, bg=DECK)
        self._inner.pack(side="left", fill="both", expand=True)

        self._glyph = tk.Label(self._inner, text=glyph, bg=DECK, fg=MUTED,
                               font=(font.body, 13), width=3)
        self._glyph.pack(side="left", padx=(9, 0), pady=11)
        self._label = tk.Label(self._inner, text=text, bg=DECK, fg=MUTED,
                               font=font.base, anchor="w")
        self._label.pack(side="left", fill="x", expand=True)

        for widget in (self, self._inner, self._glyph, self._label):
            widget.bind("<Button-1>", self._click)
            widget.bind("<Enter>", self._enter)
            widget.bind("<Leave>", self._leave)

    def _click(self, _event=None) -> None:
        self._command()

    def _enter(self, _event=None) -> None:
        if not self._selected:
            self._paint(PANEL, PAPER)

    def _leave(self, _event=None) -> None:
        if not self._selected:
            self._paint(DECK, MUTED)

    def _paint(self, bg: str, fg: str) -> None:
        self._inner.configure(bg=bg)
        self._glyph.configure(bg=bg, fg=ICE if self._selected else fg)
        self._label.configure(bg=bg, fg=fg)

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        if selected:
            self._marker.configure(bg=ICE)
            self._paint(PANEL, PAPER)
            self._label.configure(font=(fonts().body, 10, "bold"))
        else:
            self._marker.configure(bg=DECK)
            self._paint(DECK, MUTED)
            self._label.configure(font=fonts().base)


class ScrollFrame(tk.Frame):
    """A vertically scrollable container with a themed scrollbar."""

    def __init__(self, master, bg: str = ABYSS) -> None:
        super().__init__(master, bg=bg)
        self._canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self._scroll = ttk.Scrollbar(self, orient="vertical",
                                     command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._on_scroll_set)

        self._scroll.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)

        self.interior = tk.Frame(self._canvas, bg=bg)
        self._window = self._canvas.create_window(
            (0, 0), window=self.interior, anchor="nw"
        )

        self.interior.bind("<Configure>", self._on_interior)
        self._canvas.bind("<Configure>", self._on_canvas)
        self.bind_all_wheel()

    def _on_scroll_set(self, first: str, last: str) -> None:
        # Hide the scrollbar when everything fits; a permanently visible empty
        # track is noise.
        if float(first) <= 0.0 and float(last) >= 1.0:
            self._scroll.pack_forget()
        else:
            self._scroll.pack(side="right", fill="y")
        self._scroll.set(first, last)

    def _on_interior(self, _event=None) -> None:
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas(self, event) -> None:
        self._canvas.itemconfigure(self._window, width=event.width)

    def bind_all_wheel(self) -> None:
        for widget in (self._canvas, self.interior):
            widget.bind("<MouseWheel>", self._wheel)       # Windows / macOS
            widget.bind("<Button-4>", self._wheel)         # X11 up
            widget.bind("<Button-5>", self._wheel)         # X11 down

    def _wheel(self, event) -> str:
        if getattr(event, "num", None) == 4:
            delta = -1
        elif getattr(event, "num", None) == 5:
            delta = 1
        else:
            delta = -1 if event.delta > 0 else 1
        self._canvas.yview_scroll(delta, "units")
        return "break"

    def scroll_to_top(self) -> None:
        self._canvas.yview_moveto(0.0)


class KeyValue(tk.Frame):
    """A label/value row used throughout the diagnostics view."""

    def __init__(self, master, label: str, value: str = "--",
                 bg: str = PANEL, mono: bool = False) -> None:
        super().__init__(master, bg=bg)
        font = fonts()
        tk.Label(self, text=label, bg=bg, fg=MUTED, font=font.small,
                 anchor="w", width=22).pack(side="left")
        self._value = tk.Label(
            self, text=value, bg=bg, fg=PAPER,
            font=font.code if mono else font.base, anchor="w", justify="left",
            wraplength=320,
        )
        self._value.pack(side="left", fill="x", expand=True)
        self.bind("<Configure>", self._on_resize)

    def _on_resize(self, event) -> None:
        # 22 characters of label plus padding are already spoken for.
        self._value.configure(wraplength=max(140, event.width - 160))

    def set(self, value: str, color: str = PAPER) -> None:
        if self._value.cget("text") != value:
            self._value.configure(text=value)
        if str(self._value.cget("fg")) != color:
            self._value.configure(fg=color)


class Banner(tk.Frame):
    """An inline message strip. Used for errors and for honest caveats."""

    def __init__(self, master, text: str = "", tone: str = "info") -> None:
        super().__init__(master, bg=PANEL)
        self._bar = tk.Frame(self, width=3, bg=ICE)
        self._bar.pack(side="left", fill="y")
        self._label = tk.Label(
            self, text=text, bg=PANEL, fg=MUTED, font=fonts().small,
            anchor="w", justify="left", wraplength=400,
        )
        self._label.pack(side="left", fill="x", expand=True, padx=(10, 8), pady=7)
        self.set_tone(tone)
        # Wrap to whatever width the banner actually gets. A fixed wraplength
        # silently truncates the text in the narrower cards.
        self.bind("<Configure>", self._on_resize)

    def _on_resize(self, event) -> None:
        self._label.configure(wraplength=max(160, event.width - 26))

    def set(self, text: str, tone: str | None = None) -> None:
        self._label.configure(text=text)
        if tone:
            self.set_tone(tone)

    def set_tone(self, tone: str) -> None:
        colour = {"info": ICE, "warn": WARN, "error": FAULT, "ok": LIVE}.get(tone, ICE)
        self._bar.configure(bg=colour)
        self._label.configure(fg=PAPER if tone in ("error", "warn") else MUTED)

    def set_wrap(self, pixels: int) -> None:
        self._label.configure(wraplength=max(200, pixels))
