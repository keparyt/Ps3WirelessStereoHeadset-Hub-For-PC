"""Mapping: bind headset controls to actions.

The workflow is deliberately the short one: press Listen, touch the control on
the headset, pick what it should do. The full list stays visible underneath so
nothing is hidden behind a mode.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, ttk
from typing import Any, Callable

from ..actions import (
    ACTION_NONE, ActionDefinition, ParamSpec, actions_by_category,
    action_definition, describe_combo, parse_combo,
)
from ..device import EventType, ServiceEvent
from ..inputs import InputDescriptor, inputs_by_category
from ..mappings import Mapping, Profile
from .theme import (
    ABYSS, DECK, FAINT, FAULT, ICE, ICE_WASH, IDLE, LIVE, MUTED, PANEL,
    PANEL_HI, PAPER, RIDGE, SELECTION, WARN, fonts,
)
from .widgets import Banner, Card, ScrollFrame

#: Modifier virtual keys, so the recorder can ignore a bare modifier press.
_MODIFIER_KEYSYMS = {
    "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
    "Super_L", "Super_R", "Meta_L", "Meta_R", "Caps_Lock", "Num_Lock",
}

_KEYSYM_ALIASES = {
    "Return": "enter", "Escape": "esc", "space": "space", "Prior": "pageup",
    "Next": "pagedown", "BackSpace": "backspace", "Delete": "delete",
    "Insert": "insert", "Home": "home", "End": "end", "Tab": "tab",
    "Left": "left", "Right": "right", "Up": "up", "Down": "down",
    "Print": "printscreen",
}


class InputRow(tk.Frame):
    """One selectable input in the list."""

    def __init__(self, master, descriptor: InputDescriptor,
                 on_select: Callable[[str], None]) -> None:
        super().__init__(master, bg=PANEL, cursor="hand2")
        font = fonts()
        self.descriptor = descriptor
        self._on_select = on_select
        self._selected = False

        self._marker = tk.Frame(self, width=3, bg=PANEL)
        self._marker.pack(side="left", fill="y")

        self._inner = tk.Frame(self, bg=PANEL)
        self._inner.pack(side="left", fill="x", expand=True, padx=(9, 10), pady=6)

        self._name = tk.Label(self._inner, text=descriptor.label, bg=PANEL,
                              fg=PAPER, font=font.strong, anchor="w", width=19)
        self._name.pack(side="left")
        self._action = tk.Label(self._inner, text="Not bound", bg=PANEL, fg=FAINT,
                                font=font.small, anchor="w")
        self._action.pack(side="left", fill="x", expand=True)

        for widget in (self, self._inner, self._name, self._action):
            widget.bind("<Button-1>", lambda _e: self._on_select(descriptor.id))
            widget.bind("<Enter>", self._enter)
            widget.bind("<Leave>", self._leave)

    def _paint(self, bg: str) -> None:
        for widget in (self, self._inner, self._name, self._action):
            widget.configure(bg=bg)

    def _enter(self, _event=None) -> None:
        if not self._selected:
            self._paint(PANEL_HI)

    def _leave(self, _event=None) -> None:
        if not self._selected:
            self._paint(PANEL)

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self._marker.configure(bg=ICE if selected else PANEL)
        self._paint(SELECTION if selected else PANEL)

    def set_mapping(self, mapping: Mapping | None) -> None:
        if mapping is None or mapping.action_id == ACTION_NONE:
            self._action.configure(text="Not bound", fg=FAINT)
        elif not mapping.enabled:
            self._action.configure(text=f"{mapping.action_label} (off)", fg=WARN)
        else:
            self._action.configure(text=mapping.action_label, fg=ICE)

    def flash(self) -> None:
        """Briefly highlight when this input is detected."""
        self._paint(ICE_WASH)
        self.after(280, lambda: self._paint(SELECTION if self._selected else PANEL))


class MappingView(tk.Frame):
    def __init__(
        self,
        master,
        profile_getter: Callable[[], Profile],
        on_changed: Callable[[], None],
        on_save: Callable[[], None],
        on_reset: Callable[[], None],
        on_import: Callable[[str], None],
        on_export: Callable[[str], None],
    ) -> None:
        super().__init__(master, bg=ABYSS)
        self._profile_getter = profile_getter
        self._on_changed = on_changed
        self._on_save = on_save
        self._on_reset = on_reset
        self._on_import = on_import
        self._on_export = on_export

        self._rows: dict[str, InputRow] = {}
        self._selected: str | None = None
        self._listening = False
        self._param_widgets: dict[str, Callable[[], Any]] = {}
        self._param_frames: list[tk.Widget] = []
        self._action_labels: dict[str, str] = {}
        self._action_ids: dict[str, str] = {}
        self._recording = False
        self._suspend_editor = False

        self._build()
        self.reload()

    # --------------------------------------------------------------- build --

    def _build(self) -> None:
        font = fonts()
        self.columnconfigure(0, weight=3, uniform="map")
        self.columnconfigure(1, weight=2, uniform="map")
        self.rowconfigure(1, weight=1)

        # -- learn strip -----------------------------------------------------
        learn = Card(self, padding=14)
        learn.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        strip = learn.body
        self._listen_button = ttk.Button(
            strip, text="Listen for input", style="Accent.TButton",
            command=self._toggle_listen,
        )
        self._listen_button.pack(side="left")

        # Pack the buttons before the label. Tk gives space in packing order,
        # so an expanding label declared first squeezes later siblings out.
        ttk.Button(strip, text="Save", style="Accent.TButton",
                   command=self._on_save).pack(side="right")
        ttk.Button(strip, text="Export", style="Ghost.TButton",
                   command=self._export).pack(side="right", padx=(0, 8))
        ttk.Button(strip, text="Import", style="Ghost.TButton",
                   command=self._import).pack(side="right", padx=(0, 8))
        ttk.Button(strip, text="Reset to defaults", style="Ghost.TButton",
                   command=self._on_reset).pack(side="right", padx=(0, 8))

        self._listen_label = tk.Label(
            strip,
            text="Then press a control on the headset.",
            bg=PANEL, fg=MUTED, font=font.base, anchor="w",
        )
        self._listen_label.pack(side="left", padx=(14, 12), fill="x", expand=True)

        # -- input list ------------------------------------------------------
        list_card = Card(self, "Headset inputs", padding=0)
        list_card.grid(row=1, column=0, sticky="nsew", padx=(0, 12))
        scroller = ScrollFrame(list_card.body, bg=PANEL)
        scroller.pack(fill="both", expand=True)
        container = scroller.interior
        container.configure(bg=PANEL)

        for category, descriptors in inputs_by_category().items():
            header = tk.Frame(container, bg=PANEL)
            header.pack(fill="x", pady=(10, 2))
            tk.Label(header, text=category, bg=PANEL, fg=MUTED,
                     font=font.small).pack(side="left", padx=(12, 0))
            tk.Frame(header, bg=RIDGE, height=1).pack(
                side="left", fill="x", expand=True, padx=(10, 12), pady=(7, 0)
            )
            for descriptor in descriptors:
                row = InputRow(container, descriptor, self._select)
                row.pack(fill="x")
                self._rows[descriptor.id] = row

        # -- editor ----------------------------------------------------------
        self._editor_card = Card(self, "Action")
        self._editor_card.grid(row=1, column=1, sticky="nsew")
        editor = self._editor_card.body

        self._selected_label = tk.Label(
            editor, text="Select an input", bg=PANEL, fg=PAPER, font=font.headline,
            anchor="w",
        )
        self._selected_label.pack(fill="x")
        self._selected_help = tk.Label(
            editor, text="Choose an input on the left, or press Listen and use the "
            "headset.", bg=PANEL, fg=MUTED, font=font.small, anchor="w",
            justify="left", wraplength=320,
        )
        self._selected_help.pack(fill="x", pady=(2, 12))

        self._enabled_var = tk.BooleanVar(value=True)
        self._enabled_check = ttk.Checkbutton(
            editor, text="This binding is active", variable=self._enabled_var,
            command=self._apply_enabled,
        )

        tk.Label(editor, text="Run this action", bg=PANEL, fg=MUTED,
                 font=font.small, anchor="w").pack(fill="x")
        self._action_combo = ttk.Combobox(editor, state="readonly", values=[])
        self._action_combo.pack(fill="x", pady=(3, 10))
        self._action_combo.bind("<<ComboboxSelected>>", self._apply_action)
        self._populate_actions()

        self._params_frame = tk.Frame(editor, bg=PANEL)
        self._params_frame.pack(fill="x")

        self._action_help = tk.Label(
            editor, text="", bg=PANEL, fg=MUTED, font=font.small, anchor="w",
            justify="left", wraplength=320,
        )
        self._action_help.pack(fill="x", pady=(10, 0))

        buttons = tk.Frame(editor, bg=PANEL)
        buttons.pack(fill="x", pady=(14, 0))
        self._test_button = ttk.Button(buttons, text="Test action",
                                       command=self._test, state="disabled")
        self._test_button.pack(side="left")
        self._clear_button = ttk.Button(buttons, text="Remove binding",
                                        style="Danger.TButton", command=self._clear,
                                        state="disabled")
        self._clear_button.pack(side="right")

        self._caveat = Banner(editor, "", "info")
        self._caveat.pack(fill="x", side="bottom", pady=(12, 0))
        self._caveat.pack_forget()

        self._test_callback: Callable[[str, dict[str, Any]], None] | None = None

    def set_test_callback(self, callback: Callable[[str, dict[str, Any]], None]) -> None:
        self._test_callback = callback
        self._test_button.configure(state="normal" if self._selected else "disabled")

    def _populate_actions(self) -> None:
        labels: list[str] = []
        for category, definitions in actions_by_category().items():
            for definition in definitions:
                label = (
                    definition.label if category == "None"
                    else f"{category} · {definition.label}"
                )
                labels.append(label)
                self._action_labels[definition.id] = label
                self._action_ids[label] = definition.id
        self._action_combo.configure(values=labels)

    # ------------------------------------------------------------ selection --

    def _select(self, input_id: str) -> None:
        self._selected = input_id
        for identifier, row in self._rows.items():
            row.set_selected(identifier == input_id)
        self._load_editor()

    def _load_editor(self) -> None:
        input_id = self._selected
        if input_id is None:
            return
        from ..inputs import describe_input

        descriptor = describe_input(input_id)
        profile = self._profile_getter()
        mapping = profile.get(input_id)

        self._suspend_editor = True
        self._selected_label.configure(text=descriptor.label)
        self._selected_help.configure(text=descriptor.description)

        action_id = mapping.action_id if mapping else ACTION_NONE
        self._action_combo.set(self._action_labels.get(action_id, ""))
        self._enabled_var.set(mapping.enabled if mapping else True)

        if action_id == ACTION_NONE:
            self._enabled_check.pack_forget()
        else:
            self._enabled_check.pack(fill="x", pady=(0, 10), before=self._action_combo)

        self._build_params(action_id, mapping.params if mapping else {})
        self._clear_button.configure(state="normal" if mapping else "disabled")
        self._test_button.configure(
            state="normal" if self._test_callback and action_id != ACTION_NONE
            else "disabled"
        )

        if descriptor.hardware_acts:
            self._caveat.set(
                f"{descriptor.label} is handled by the headset itself as well. Your "
                "action runs in addition to that; it cannot replace it.", "info",
            )
            self._caveat.pack(fill="x", pady=(12, 0))
        else:
            self._caveat.pack_forget()

        self._suspend_editor = False

    # -------------------------------------------------------------- params --

    def _build_params(self, action_id: str, values: dict[str, Any]) -> None:
        for widget in self._param_frames:
            widget.destroy()
        self._param_frames.clear()
        self._param_widgets.clear()

        try:
            definition = action_definition(action_id)
        except Exception:
            self._action_help.configure(text="")
            return
        self._action_help.configure(text=definition.description)

        for spec in definition.params:
            frame = tk.Frame(self._params_frame, bg=PANEL)
            frame.pack(fill="x", pady=(0, 8))
            self._param_frames.append(frame)
            self._build_param(frame, spec, values.get(spec.name, spec.default))

    def _build_param(self, parent: tk.Frame, spec: ParamSpec, value: Any) -> None:
        font = fonts()
        tk.Label(parent, text=spec.label, bg=PANEL, fg=MUTED, font=font.small,
                 anchor="w").pack(fill="x")

        if spec.kind == "int":
            var = tk.StringVar(value=str(value))
            spin = ttk.Spinbox(parent, from_=spec.minimum, to=spec.maximum,
                               textvariable=var, width=8)
            spin.pack(anchor="w", pady=(3, 0))
            var.trace_add("write", lambda *_: self._apply_params())

            def read_int(v=var, s=spec):
                try:
                    return max(s.minimum, min(s.maximum, int(v.get())))
                except (TypeError, ValueError):
                    return s.default
            self._param_widgets[spec.name] = read_int

        elif spec.kind == "keys":
            row = tk.Frame(parent, bg=PANEL)
            row.pack(fill="x", pady=(3, 0))
            var = tk.StringVar(value=str(value))
            entry = ttk.Entry(row, textvariable=var)
            entry.pack(side="left", fill="x", expand=True)
            record = ttk.Button(row, text="Record", width=8)
            record.pack(side="left", padx=(6, 0))
            preview = tk.Label(parent, text="", bg=PANEL, fg=ICE, font=font.small,
                               anchor="w")
            preview.pack(fill="x", pady=(3, 0))

            def update_preview(*_args) -> None:
                combo = var.get().strip()
                try:
                    parse_combo(combo)
                    preview.configure(text=f"Sends {describe_combo(combo)}", fg=ICE)
                except Exception as exc:
                    preview.configure(text=str(exc), fg=FAULT)
                self._apply_params()

            var.trace_add("write", update_preview)
            record.configure(command=lambda: self._record_combo(record, var))
            update_preview()
            self._param_widgets[spec.name] = lambda v=var: v.get().strip()

        elif spec.kind == "path":
            row = tk.Frame(parent, bg=PANEL)
            row.pack(fill="x", pady=(3, 0))
            var = tk.StringVar(value=str(value))
            ttk.Entry(row, textvariable=var).pack(side="left", fill="x", expand=True)
            ttk.Button(row, text="Browse", width=8,
                       command=lambda v=var: self._browse(v)).pack(side="left",
                                                                    padx=(6, 0))
            var.trace_add("write", lambda *_: self._apply_params())
            self._param_widgets[spec.name] = lambda v=var: v.get().strip()

        else:
            var = tk.StringVar(value=str(value))
            ttk.Entry(parent, textvariable=var).pack(fill="x", pady=(3, 0))
            var.trace_add("write", lambda *_: self._apply_params())
            self._param_widgets[spec.name] = lambda v=var: v.get()

        if spec.help:
            tk.Label(parent, text=spec.help, bg=PANEL, fg=FAINT, font=font.tiny,
                     anchor="w", justify="left", wraplength=300).pack(fill="x",
                                                                       pady=(2, 0))

    def _browse(self, var: tk.StringVar) -> None:
        chosen = filedialog.askopenfilename(
            title="Choose a program",
            filetypes=[("Programs", "*.exe"), ("All files", "*.*")],
        )
        if chosen:
            var.set(chosen)

    def _record_combo(self, button: ttk.Button, var: tk.StringVar) -> None:
        if self._recording:
            return
        self._recording = True
        original = button.cget("text")
        button.configure(text="Press keys")
        button.focus_set()

        def finish(binding_id: str) -> None:
            self._recording = False
            button.configure(text=original)
            try:
                self.winfo_toplevel().unbind("<KeyPress>", binding_id)
            except Exception:
                pass

        def on_key(event) -> str:
            if event.keysym in _MODIFIER_KEYSYMS:
                return "break"
            if event.keysym == "Escape":
                finish(binding_id)
                return "break"
            parts: list[str] = []
            if event.state & 0x0004:
                parts.append("ctrl")
            if event.state & 0x0008 or event.state & 0x0080:
                parts.append("alt")
            if event.state & 0x0001:
                parts.append("shift")
            key = _KEYSYM_ALIASES.get(event.keysym, event.keysym.lower())
            parts.append(key)
            candidate = "+".join(parts)
            try:
                parse_combo(candidate)
            except Exception:
                finish(binding_id)
                return "break"
            var.set(candidate)
            finish(binding_id)
            return "break"

        binding_id = self.winfo_toplevel().bind("<KeyPress>", on_key, add="+")

    # -------------------------------------------------------------- editing --

    def _apply_action(self, _event=None) -> None:
        if self._selected is None or self._suspend_editor:
            return
        label = self._action_combo.get()
        action_id = self._action_ids.get(label, ACTION_NONE)
        profile = self._profile_getter()
        if action_id == ACTION_NONE:
            profile.unbind(self._selected)
        else:
            profile.bind(self._selected, action_id)
        self._on_changed()
        self._load_editor()
        self.reload()

    def _apply_params(self) -> None:
        if self._selected is None or self._suspend_editor:
            return
        profile = self._profile_getter()
        mapping = profile.get(self._selected)
        if mapping is None:
            return
        params = {name: read() for name, read in self._param_widgets.items()}
        mapping.params.update(params)
        self._on_changed()
        row = self._rows.get(self._selected)
        if row is not None:
            row.set_mapping(mapping)

    def _apply_enabled(self) -> None:
        if self._selected is None or self._suspend_editor:
            return
        profile = self._profile_getter()
        profile.set_enabled(self._selected, self._enabled_var.get())
        self._on_changed()
        self.reload()

    def _clear(self) -> None:
        if self._selected is None:
            return
        self._profile_getter().unbind(self._selected)
        self._on_changed()
        self._load_editor()
        self.reload()

    def _test(self) -> None:
        if self._selected is None or self._test_callback is None:
            return
        mapping = self._profile_getter().get(self._selected)
        if mapping is None:
            return
        self._test_callback(mapping.action_id, dict(mapping.params))

    # ---------------------------------------------------------------- learn --

    def _toggle_listen(self) -> None:
        self._listening = not self._listening
        if self._listening:
            self._listen_button.configure(text="Stop listening")
            self._listen_label.configure(
                text="Listening. Press a control on the headset now.", fg=ICE
            )
        else:
            self._reset_listen()

    def _reset_listen(self) -> None:
        self._listening = False
        self._listen_button.configure(text="Listen for input")
        self._listen_label.configure(
            text="Then press a control on the headset.", fg=MUTED,
        )

    def on_event(self, event: ServiceEvent) -> None:
        if event.type != EventType.INPUT or event.input_event is None:
            return
        input_id = event.input_event.input_id
        row = self._rows.get(input_id)
        if row is not None:
            row.flash()
        if not self._listening:
            return
        self._select(input_id)
        self._listen_label.configure(
            text=f"Detected {event.input_event.label}. Now choose an action.", fg=LIVE
        )
        self._listening = False
        self._listen_button.configure(text="Listen for input")

    # --------------------------------------------------------------- import --

    def _import(self) -> None:
        chosen = filedialog.askopenfilename(
            title="Import bindings", filetypes=[("Binding profile", "*.json")]
        )
        if chosen:
            self._on_import(chosen)

    def _export(self) -> None:
        chosen = filedialog.asksaveasfilename(
            title="Export bindings", defaultextension=".json",
            initialfile="ps3-headset-bindings.json",
            filetypes=[("Binding profile", "*.json")],
        )
        if chosen:
            self._on_export(chosen)

    # --------------------------------------------------------------- reload --

    def reload(self) -> None:
        profile = self._profile_getter()
        for input_id, row in self._rows.items():
            row.set_mapping(profile.get(input_id))
        if self._selected is not None:
            self._load_editor()
