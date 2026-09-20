"""Actions that a headset input can trigger.

Everything the user can bind lives in a registry, so adding a capability later
means appending one ``ActionDefinition`` and writing one handler. The mapping
UI builds its editors from each action's parameter schema rather than
hard-coding forms, which is what keeps "additional functions in the future"
from turning into a UI rewrite.

Key injection uses ``SendInput`` through ctypes. That keeps the dependency
list at exactly one package (hidapi) and avoids shipping a native helper.

An honest note that the UI repeats to the user: binding an action to, say,
Volume up does **not** stop the headset changing its own volume. The receiver
acts on its controls in hardware and only tells the PC afterwards. Mappings
run *in addition to* the headset's built-in behaviour, they do not replace it.
"""

from __future__ import annotations

import ctypes
import os
import shlex
import subprocess
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .applog import get_logger

log = get_logger("actions")

IS_WINDOWS = os.name == "nt"

#: Never fire an action more than this many times from one input burst.
MAX_REPEAT = 6


# ------------------------------------------------------------------ win32 --

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG), ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


if IS_WINDOWS:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    _user32.SendInput.restype = wintypes.UINT
else:
    _user32 = None


VK = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "return": 0x0D,
    "shift": 0x10, "ctrl": 0x11, "control": 0x11, "alt": 0x12,
    "pause": 0x13, "capslock": 0x14, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "pageup": 0x21, "pagedown": 0x22, "end": 0x23,
    "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "insert": 0x2D, "delete": 0x2E,
    "win": 0x5B, "lwin": 0x5B, "rwin": 0x5C, "apps": 0x5D,
    "numlock": 0x90, "scrolllock": 0x91,
    "volume_mute": 0xAD, "volume_down": 0xAE, "volume_up": 0xAF,
    "media_next": 0xB0, "media_prev": 0xB1,
    "media_stop": 0xB2, "media_play_pause": 0xB3,
    "launch_mail": 0xB4, "launch_media": 0xB5,
    "browser_back": 0xA6, "browser_forward": 0xA7, "browser_refresh": 0xA8,
    "browser_home": 0xAC,
    "printscreen": 0x2C,
    ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF,
    "`": 0xC0, "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
}
for _i in range(1, 25):
    VK[f"f{_i}"] = 0x6F + _i
for _c in "abcdefghijklmnopqrstuvwxyz":
    VK[_c] = ord(_c.upper())
for _d in "0123456789":
    VK[_d] = ord(_d)

MODIFIERS = {"ctrl", "control", "alt", "shift", "win", "lwin", "rwin"}

#: Keys that must carry the extended-key flag to be recognised correctly.
EXTENDED_VKS = {
    0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB2, 0xB3, 0xB4, 0xB5,
    0xA6, 0xA7, 0xA8, 0xAC,
    0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E,
    0x5B, 0x5C, 0x5D, 0x90, 0x2C,
}


class ActionError(RuntimeError):
    """Raised when an action cannot be carried out."""


def parse_combo(combo: str) -> list[int]:
    """Turn ``"ctrl+shift+m"`` into an ordered list of virtual-key codes."""
    if not combo or not combo.strip():
        raise ActionError("No keys specified.")
    parts = [p.strip().lower() for p in combo.replace(" ", "").split("+") if p.strip()]
    if not parts:
        raise ActionError("No keys specified.")
    codes: list[int] = []
    for part in parts:
        code = VK.get(part)
        if code is None:
            raise ActionError(f"Unknown key: {part!r}")
        codes.append(code)
    modifier_count = sum(1 for p in parts if p in MODIFIERS)
    if modifier_count == len(parts) and len(parts) > 1:
        raise ActionError("A shortcut needs at least one non-modifier key.")
    return codes


def describe_combo(combo: str) -> str:
    """Human-facing rendering of a shortcut, for the mapping list."""
    pretty = {
        "ctrl": "Ctrl", "control": "Ctrl", "alt": "Alt", "shift": "Shift",
        "win": "Win", "lwin": "Win", "rwin": "Win", "esc": "Esc",
        "escape": "Esc", "enter": "Enter", "return": "Enter", "space": "Space",
    }
    parts = [p.strip() for p in combo.split("+") if p.strip()]
    return " + ".join(pretty.get(p.lower(), p.upper() if len(p) == 1 else p.title())
                      for p in parts)


class KeySender:
    """Sends synthetic key presses. A no-op stub off Windows."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled and IS_WINDOWS
        self._lock = threading.Lock()
        self.sent = 0
        #: Recorded on non-Windows platforms so behaviour stays testable.
        self.simulated: list[tuple[int, ...]] = []

    def _send(self, inputs: list[INPUT]) -> None:
        if not inputs:
            return
        array = (INPUT * len(inputs))(*inputs)
        sent = _user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT))
        if sent != len(inputs):
            err = ctypes.get_last_error()
            raise ActionError(f"SendInput rejected the key event (WinError {err})")

    def tap(self, codes: Iterable[int]) -> None:
        """Press keys in order, release in reverse order."""
        codes = list(codes)
        if not codes:
            return
        with self._lock:
            self.sent += 1
            if not self.enabled:
                self.simulated.append(tuple(codes))
                log.info("Key injection unavailable here; would send %s", codes)
                return
            events: list[INPUT] = []
            for code in codes:
                events.append(self._event(code, down=True))
            for code in reversed(codes):
                events.append(self._event(code, down=False))
            self._send(events)

    @staticmethod
    def _event(code: int, down: bool) -> INPUT:
        flags = 0
        if code in EXTENDED_VKS:
            flags |= KEYEVENTF_EXTENDEDKEY
        if not down:
            flags |= KEYEVENTF_KEYUP
        item = INPUT()
        item.type = INPUT_KEYBOARD
        item.ki = KEYBDINPUT(wVk=code, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0)
        return item


# ----------------------------------------------------------------- schema --


@dataclass(frozen=True)
class ParamSpec:
    """Describes one configurable field so the UI can render it generically."""

    name: str
    label: str
    kind: str = "text"  # text | int | keys | path | choice
    default: Any = ""
    help: str = ""
    choices: tuple[str, ...] = ()
    minimum: int = 0
    maximum: int = 100


@dataclass(frozen=True)
class ActionDefinition:
    id: str
    label: str
    category: str
    description: str
    handler: Callable[["ActionContext", dict[str, Any]], None]
    params: tuple[ParamSpec, ...] = ()
    #: Whether a multi-step input should fire this action multiple times.
    honors_repeat: bool = False

    def defaults(self) -> dict[str, Any]:
        return {p.name: p.default for p in self.params}


class ActionCategory:
    NONE = "None"
    MEDIA = "Media"
    VOLUME = "System volume"
    KEYBOARD = "Keyboard"
    LAUNCH = "Launch"
    HUB = "This app"


ACTIONS: dict[str, ActionDefinition] = {}


def register(definition: ActionDefinition) -> ActionDefinition:
    if definition.id in ACTIONS:
        raise ValueError(f"Duplicate action id: {definition.id}")
    ACTIONS[definition.id] = definition
    return definition


@dataclass
class ActionContext:
    """Services an action handler may use."""

    keys: KeySender
    notify: Callable[[str, str], None] = lambda level, message: None
    show_window: Callable[[], None] = lambda: None
    extras: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------- handlers --


def _noop(ctx: ActionContext, params: dict[str, Any]) -> None:
    return None


def _tap(code: int) -> Callable[[ActionContext, dict[str, Any]], None]:
    def handler(ctx: ActionContext, params: dict[str, Any]) -> None:
        ctx.keys.tap([code])
    return handler


def _combo(ctx: ActionContext, params: dict[str, Any]) -> None:
    ctx.keys.tap(parse_combo(str(params.get("keys", ""))))


def _volume_step(code: int) -> Callable[[ActionContext, dict[str, Any]], None]:
    def handler(ctx: ActionContext, params: dict[str, Any]) -> None:
        try:
            steps = int(params.get("steps", 1))
        except (TypeError, ValueError):
            steps = 1
        steps = max(1, min(10, steps))
        for _ in range(steps):
            ctx.keys.tap([code])
    return handler


def _launch(ctx: ActionContext, params: dict[str, Any]) -> None:
    path = str(params.get("path", "")).strip()
    if not path:
        raise ActionError("No program selected.")
    raw_args = str(params.get("args", "")).strip()
    try:
        args = shlex.split(raw_args, posix=False) if raw_args else []
    except ValueError as exc:
        raise ActionError(f"Could not read the arguments: {exc}") from exc
    try:
        subprocess.Popen([path, *args], close_fds=True)
    except OSError as exc:
        raise ActionError(f"Could not start {path}: {exc}") from exc


def _notify(ctx: ActionContext, params: dict[str, Any]) -> None:
    message = str(params.get("message", "")).strip() or "Headset input received"
    ctx.notify("info", message)


def _show_window(ctx: ActionContext, params: dict[str, Any]) -> None:
    ctx.show_window()


ACTION_NONE = "none"

register(ActionDefinition(
    ACTION_NONE, "Do nothing", ActionCategory.NONE,
    "Leave this input unbound. The headset still does whatever it does in hardware.",
    _noop,
))

register(ActionDefinition(
    "media.play_pause", "Play or pause", ActionCategory.MEDIA,
    "Sends the play/pause media key.", _tap(VK["media_play_pause"]),
))
register(ActionDefinition(
    "media.next", "Next track", ActionCategory.MEDIA,
    "Sends the next-track media key.", _tap(VK["media_next"]), honors_repeat=True,
))
register(ActionDefinition(
    "media.previous", "Previous track", ActionCategory.MEDIA,
    "Sends the previous-track media key.", _tap(VK["media_prev"]), honors_repeat=True,
))
register(ActionDefinition(
    "media.stop", "Stop playback", ActionCategory.MEDIA,
    "Sends the stop media key.", _tap(VK["media_stop"]),
))

register(ActionDefinition(
    "system.volume_up", "Turn system volume up", ActionCategory.VOLUME,
    "Raises the Windows master volume.", _volume_step(VK["volume_up"]),
    params=(ParamSpec("steps", "Steps per press", "int", 1,
                      "How many Windows volume increments to send.", minimum=1, maximum=10),),
    honors_repeat=True,
))
register(ActionDefinition(
    "system.volume_down", "Turn system volume down", ActionCategory.VOLUME,
    "Lowers the Windows master volume.", _volume_step(VK["volume_down"]),
    params=(ParamSpec("steps", "Steps per press", "int", 1,
                      "How many Windows volume increments to send.", minimum=1, maximum=10),),
    honors_repeat=True,
))
register(ActionDefinition(
    "system.mute_toggle", "Mute or unmute system audio", ActionCategory.VOLUME,
    "Toggles the Windows master mute.", _tap(VK["volume_mute"]),
))

register(ActionDefinition(
    "keyboard.combo", "Press a keyboard shortcut", ActionCategory.KEYBOARD,
    "Sends any shortcut you record, such as Ctrl + Shift + M.", _combo,
    params=(ParamSpec("keys", "Shortcut", "keys", "ctrl+shift+m",
                      "Click Record and press the shortcut, or type it as ctrl+shift+m."),),
))

register(ActionDefinition(
    "app.launch", "Open a program", ActionCategory.LAUNCH,
    "Starts a program or file.", _launch,
    params=(
        ParamSpec("path", "Program", "path", "", "The program or file to open."),
        ParamSpec("args", "Arguments", "text", "", "Optional command line arguments."),
    ),
))

register(ActionDefinition(
    "hub.notify", "Show a message in the app", ActionCategory.HUB,
    "Displays a message in the status bar. Useful while you are testing a binding.",
    _notify,
    params=(ParamSpec("message", "Message", "text", "Headset input received",
                      "What to display."),),
))
register(ActionDefinition(
    "hub.show_window", "Bring this window to the front", ActionCategory.HUB,
    "Restores and focuses the hub window.", _show_window,
))


def action_definition(action_id: str) -> ActionDefinition:
    definition = ACTIONS.get(action_id)
    if definition is None:
        raise ActionError(f"Unknown action: {action_id}")
    return definition


def actions_by_category() -> dict[str, list[ActionDefinition]]:
    grouped: dict[str, list[ActionDefinition]] = {}
    for definition in ACTIONS.values():
        grouped.setdefault(definition.category, []).append(definition)
    return grouped


def describe_action(action_id: str, params: dict[str, Any] | None = None) -> str:
    """A short label for the mappings list, including the interesting parameter."""
    definition = ACTIONS.get(action_id)
    if definition is None:
        return f"Unknown action ({action_id})"
    params = params or {}
    if action_id == "keyboard.combo":
        keys = str(params.get("keys", "")).strip()
        return f"Press {describe_combo(keys)}" if keys else definition.label
    if action_id == "app.launch":
        path = str(params.get("path", "")).strip()
        return f"Open {os.path.basename(path)}" if path else definition.label
    if action_id in ("system.volume_up", "system.volume_down"):
        try:
            steps = int(params.get("steps", 1))
        except (TypeError, ValueError):
            steps = 1
        if steps > 1:
            return f"{definition.label} ({steps} steps)"
    return definition.label


class ActionRunner:
    """Executes actions off the HID thread, with per-action error isolation."""

    def __init__(self, context: ActionContext, max_repeat: int = MAX_REPEAT) -> None:
        self.context = context
        self.max_repeat = max_repeat
        self.executed = 0
        self.failed = 0
        self.last_error: str = ""

    def run(self, action_id: str, params: dict[str, Any] | None, repeat: int = 1) -> bool:
        if action_id == ACTION_NONE:
            return True
        try:
            definition = action_definition(action_id)
        except ActionError as exc:
            self._fail(str(exc))
            return False

        merged = definition.defaults()
        merged.update(params or {})
        times = max(1, min(self.max_repeat, repeat)) if definition.honors_repeat else 1

        for index in range(times):
            try:
                definition.handler(self.context, merged)
            except ActionError as exc:
                self._fail(f"{definition.label}: {exc}")
                return False
            except Exception as exc:  # a broken binding must not kill the app
                log.exception("Action %s raised", action_id)
                self._fail(f"{definition.label}: {exc}")
                return False
            if index + 1 < times:
                time.sleep(0.012)  # let the target app see distinct presses

        self.executed += 1
        log.info("Ran %s%s", action_id, f" x{times}" if times > 1 else "")
        return True

    def _fail(self, message: str) -> None:
        self.failed += 1
        self.last_error = message
        log.error("Action failed: %s", message)
        try:
            self.context.notify("error", message)
        except Exception:
            pass
