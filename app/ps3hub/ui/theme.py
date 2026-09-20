"""Visual language.

The palette comes from the PlayStation 3 XMB the headset was built for: a deep
navy field rather than grey, thin light type at large sizes, and one cool
accent. Colour is never the only carrier of meaning - every state that uses
colour also carries a word, so the interface stays readable for anyone who
cannot separate the greens from the ambers.
"""

from __future__ import annotations

import tkinter.font as tkfont
from tkinter import ttk

# ---------------------------------------------------------------- palette --

ABYSS = "#050D14"      # window background, deepest layer
DECK = "#0B1922"       # navigation rail
PANEL = "#102532"      # card surface
PANEL_HI = "#16303F"   # hovered / selected surface
RIDGE = "#1E3B4B"      # hairlines and borders

ICE = "#6FC9EE"        # the single accent
ICE_DEEP = "#2C6A88"
ICE_WASH = "#14303E"

PAPER = "#E6F0F5"      # primary text
MUTED = "#7F98A6"      # secondary text
FAINT = "#4E6674"      # disabled text

LIVE = "#5BD196"       # connected, healthy
WARN = "#EFB34A"       # attention
FAULT = "#EF6B6E"      # error
IDLE = "#3C5666"       # off, unknown

SELECTION = "#1D4457"

# ------------------------------------------------------------------- type --

_PREFERRED = ("Segoe UI", "Inter", "Noto Sans", "DejaVu Sans", "Helvetica")
_PREFERRED_LIGHT = ("Segoe UI Light", "Segoe UI", "Inter", "DejaVu Sans")
_PREFERRED_MONO = ("Cascadia Mono", "Consolas", "DejaVu Sans Mono", "Courier New")


def _pick(candidates: tuple[str, ...], available: set[str], fallback: str) -> str:
    for name in candidates:
        if name in available:
            return name
    return fallback


class Fonts:
    """Resolved font families and the type scale built from them."""

    def __init__(self) -> None:
        try:
            available = set(tkfont.families())
        except Exception:
            available = set()
        self.body = _pick(_PREFERRED, available, "TkDefaultFont")
        self.light = _pick(_PREFERRED_LIGHT, available, self.body)
        self.mono = _pick(_PREFERRED_MONO, available, "TkFixedFont")

        # A restrained scale. Display sizes use the light weight, the way the
        # XMB set its large type.
        self.display = (self.light, 34)
        self.headline = (self.light, 22)
        self.title = (self.body, 14, "bold")
        self.subtitle = (self.body, 11)
        self.base = (self.body, 10)
        self.strong = (self.body, 10, "bold")
        self.small = (self.body, 9)
        self.tiny = (self.body, 8)
        self.readout = (self.light, 27)
        self.code = (self.mono, 9)
        self.code_small = (self.mono, 8)


_fonts: Fonts | None = None


def fonts() -> Fonts:
    global _fonts
    if _fonts is None:
        _fonts = Fonts()
    return _fonts


# ------------------------------------------------------------------ radii --

RADIUS = 10
RADIUS_SMALL = 6
PAD = 16
PAD_SMALL = 8


def apply(root) -> ttk.Style:
    """Configure ttk so the standard widgets match the custom ones."""
    font = fonts()
    style = ttk.Style(root)
    try:
        style.theme_use("clam")  # the only built-in theme that honours colours
    except Exception:
        pass

    root.configure(background=ABYSS)

    style.configure(".", background=PANEL, foreground=PAPER,
                    fieldbackground=PANEL, font=font.base, borderwidth=0)

    style.configure("TFrame", background=ABYSS)
    style.configure("Panel.TFrame", background=PANEL)
    style.configure("Deck.TFrame", background=DECK)
    style.configure("Rail.TFrame", background=DECK)

    style.configure("TLabel", background=ABYSS, foreground=PAPER, font=font.base)
    style.configure("Panel.TLabel", background=PANEL, foreground=PAPER)
    style.configure("Muted.TLabel", background=ABYSS, foreground=MUTED, font=font.small)
    style.configure("PanelMuted.TLabel", background=PANEL, foreground=MUTED,
                    font=font.small)
    style.configure("Title.TLabel", background=ABYSS, foreground=PAPER, font=font.title)
    style.configure("PanelTitle.TLabel", background=PANEL, foreground=PAPER,
                    font=font.title)
    style.configure("Headline.TLabel", background=ABYSS, foreground=PAPER,
                    font=font.headline)
    style.configure("Readout.TLabel", background=PANEL, foreground=PAPER,
                    font=font.readout)
    style.configure("Code.TLabel", background=PANEL, foreground=ICE, font=font.code)
    style.configure("Accent.TLabel", background=PANEL, foreground=ICE, font=font.strong)
    style.configure("Warn.TLabel", background=PANEL, foreground=WARN, font=font.small)
    style.configure("Fault.TLabel", background=PANEL, foreground=FAULT, font=font.small)

    # Buttons ---------------------------------------------------------------
    style.configure("TButton", background=PANEL_HI, foreground=PAPER,
                    font=font.base, padding=(14, 7), relief="flat", borderwidth=0)
    style.map("TButton",
              background=[("pressed", ICE_DEEP), ("active", RIDGE),
                          ("disabled", PANEL)],
              foreground=[("disabled", FAINT)])

    style.configure("Accent.TButton", background=ICE_DEEP, foreground=PAPER,
                    font=font.strong, padding=(16, 8))
    style.map("Accent.TButton",
              background=[("pressed", ICE_WASH), ("active", "#37809F"),
                          ("disabled", PANEL_HI)],
              foreground=[("disabled", FAINT)])

    style.configure("Ghost.TButton", background=PANEL, foreground=MUTED,
                    font=font.small, padding=(10, 5))
    style.map("Ghost.TButton",
              background=[("active", PANEL_HI)],
              foreground=[("active", PAPER), ("disabled", FAINT)])

    style.configure("Danger.TButton", background=PANEL_HI, foreground=FAULT,
                    padding=(12, 6))
    style.map("Danger.TButton", background=[("active", "#3A1F24")])

    # Inputs ----------------------------------------------------------------
    style.configure("TEntry", fieldbackground=DECK, foreground=PAPER,
                    insertcolor=ICE, borderwidth=1, relief="flat", padding=6)
    style.map("TEntry", bordercolor=[("focus", ICE)],
              lightcolor=[("focus", ICE)], darkcolor=[("focus", ICE)])

    style.configure("TCombobox", fieldbackground=DECK, background=DECK,
                    foreground=PAPER, arrowcolor=ICE, borderwidth=1, padding=5)
    style.map("TCombobox",
              fieldbackground=[("readonly", DECK)],
              foreground=[("disabled", FAINT)],
              bordercolor=[("focus", ICE)])

    style.configure("TCheckbutton", background=PANEL, foreground=PAPER,
                    font=font.base, focuscolor=PANEL)
    style.map("TCheckbutton",
              background=[("active", PANEL)],
              foreground=[("disabled", FAINT)],
              indicatorcolor=[("selected", ICE), ("!selected", RIDGE)])

    style.configure("TSpinbox", fieldbackground=DECK, foreground=PAPER,
                    arrowcolor=ICE, borderwidth=1, padding=5)

    style.configure("TSeparator", background=RIDGE)

    # Scrollbars ------------------------------------------------------------
    style.configure("Vertical.TScrollbar", background=PANEL_HI, troughcolor=ABYSS,
                    borderwidth=0, arrowcolor=MUTED, relief="flat", width=11)
    style.map("Vertical.TScrollbar", background=[("active", RIDGE)])
    style.configure("Horizontal.TScrollbar", background=PANEL_HI, troughcolor=ABYSS,
                    borderwidth=0, arrowcolor=MUTED, relief="flat")

    # Notebook is unused, but a stray default would look wrong if added later.
    style.configure("TNotebook", background=ABYSS, borderwidth=0)
    style.configure("TNotebook.Tab", background=DECK, foreground=MUTED,
                    padding=(16, 8))
    style.map("TNotebook.Tab",
              background=[("selected", PANEL)], foreground=[("selected", PAPER)])

    return style


def round_rect(canvas, x1: float, y1: float, x2: float, y2: float,
               radius: float = RADIUS, **kwargs):
    """Draw a rounded rectangle on a Canvas.

    Tk has no native rounded rectangle; a smoothed polygon is the usual way
    and is visually indistinguishable at these radii.
    """
    radius = max(0.0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    points = [
        x1 + radius, y1, x2 - radius, y1, x2, y1,
        x2, y1 + radius, x2, y2 - radius, x2, y2,
        x2 - radius, y2, x1 + radius, y2, x1, y2,
        x1, y2 - radius, x1, y1 + radius, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, splinesteps=16, **kwargs)
