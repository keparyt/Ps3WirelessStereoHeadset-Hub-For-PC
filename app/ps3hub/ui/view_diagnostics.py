"""Diagnostics: everything needed to work out why something is not appearing.

The proof of concept printed this to a console. A packaged application has no
console, so the same information lives here, and can be copied to the
clipboard in one click for a bug report.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from .. import APP_NAME, APP_VERSION
from ..applog import log_dir, recent
from ..device import HeadsetService, ServiceState
from ..protocol import TARGET_PID, TARGET_VID
from .theme import (
    ABYSS, DECK, FAINT, FAULT, ICE, LIVE, MUTED, PANEL, PAPER, RIDGE, WARN, fonts,
)
from .widgets import Banner, Card, KeyValue, ScrollFrame

LOG_TAIL = 400


class DiagnosticsView(tk.Frame):
    def __init__(self, master, service: HeadsetService) -> None:
        super().__init__(master, bg=ABYSS)
        self._service = service
        self._collection_rows: list[tk.Widget] = []
        self._unknown_rows: list[tk.Widget] = []
        self._collections_signature = ""
        self._unknown_signature = ""
        self._log_sequence = -1
        self._follow = tk.BooleanVar(value=True)
        self._build()

    def _build(self) -> None:
        font = fonts()
        scroller = ScrollFrame(self, bg=ABYSS)
        scroller.pack(fill="both", expand=True)
        root = scroller.interior
        root.columnconfigure(0, weight=1, uniform="diag")
        root.columnconfigure(1, weight=1, uniform="diag")

        # -- counters ---------------------------------------------------------
        counters = Card(root, "Session")
        counters.grid(row=0, column=0, sticky="nsew", padx=(0, 12), pady=(0, 12))
        self._kv = {
            "target": KeyValue(counters.body, "Target device",
                               f"VID 0x{TARGET_VID:04X} / PID 0x{TARGET_PID:04X}"),
            "backend": KeyValue(counters.body, "HID backend", "checking"),
            "receiver": KeyValue(counters.body, "Receiver", "not detected"),
            "readers": KeyValue(counters.body, "Open collections", "0"),
            "reports": KeyValue(counters.body, "Reports received", "0"),
            "status": KeyValue(counters.body, "Status reports", "0"),
            "bytes": KeyValue(counters.body, "Bytes received", "0"),
            "inputs": KeyValue(counters.body, "Inputs detected", "0"),
            "last_input": KeyValue(counters.body, "Last input", "none"),
            "last_raw": KeyValue(counters.body, "Last raw report", "--", mono=True),
        }
        for row in self._kv.values():
            row.pack(fill="x", pady=2)

        self._error_banner = Banner(counters.body, "", "error")

        # -- collections ------------------------------------------------------
        collections = Card(root, "HID collections",
                           "every collection the receiver exposes")
        collections.grid(row=0, column=1, sticky="nsew", pady=(0, 12))
        self._collections_box = tk.Frame(collections.body, bg=PANEL)
        self._collections_box.pack(fill="both", expand=True)
        self._collections_empty = tk.Label(
            self._collections_box,
            text="Nothing enumerated. Plug in the USB receiver.",
            bg=PANEL, fg=FAINT, font=font.small,
        )
        self._collections_empty.pack(anchor="w")

        # -- unknown reports ---------------------------------------------------
        unknown = Card(root, "Unrecognised reports",
                       "report shapes the decoder does not understand")
        unknown.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        self._unknown_box = tk.Frame(unknown.body, bg=PANEL)
        self._unknown_box.pack(fill="x")
        self._unknown_empty = tk.Label(
            self._unknown_box,
            text="None so far. Anything the decoder cannot identify is listed here "
                 "with its raw bytes, rather than being discarded.",
            bg=PANEL, fg=FAINT, font=font.small, wraplength=760, justify="left",
        )
        self._unknown_empty.pack(anchor="w")

        # -- log ---------------------------------------------------------------
        log_card = Card(root, "Log")
        log_card.grid(row=2, column=0, columnspan=2, sticky="nsew")
        root.rowconfigure(2, weight=1)

        controls = tk.Frame(log_card.body, bg=PANEL)
        controls.pack(fill="x", pady=(0, 8))
        ttk.Checkbutton(controls, text="Follow new entries",
                        variable=self._follow).pack(side="left")
        ttk.Button(controls, text="Copy log", style="Ghost.TButton",
                   command=self._copy).pack(side="right")
        ttk.Button(controls, text="Open log folder", style="Ghost.TButton",
                   command=self._open_folder).pack(side="right", padx=(0, 8))

        text_wrap = tk.Frame(log_card.body, bg=DECK, highlightthickness=1,
                             highlightbackground=RIDGE)
        text_wrap.pack(fill="both", expand=True)
        self._text = tk.Text(
            text_wrap, height=14, bg=DECK, fg=MUTED, font=font.code_small,
            insertbackground=ICE, relief="flat", wrap="none", padx=10, pady=8,
            selectbackground="#1D4457", borderwidth=0,
        )
        scrollbar = ttk.Scrollbar(text_wrap, orient="vertical",
                                  command=self._text.yview)
        self._text.configure(yscrollcommand=scrollbar.set, state="disabled")
        scrollbar.pack(side="right", fill="y")
        self._text.pack(side="left", fill="both", expand=True)

        self._text.tag_configure("ERROR", foreground=FAULT)
        self._text.tag_configure("WARNING", foreground=WARN)
        self._text.tag_configure("INFO", foreground=MUTED)
        self._text.tag_configure("DEBUG", foreground=FAINT)

        footer = tk.Frame(log_card.body, bg=PANEL)
        footer.pack(fill="x", pady=(8, 0))
        tk.Label(footer, text=f"{APP_NAME} {APP_VERSION} · logs in {log_dir()}",
                 bg=PANEL, fg=FAINT, font=font.tiny, anchor="w").pack(fill="x")

    # ------------------------------------------------------------- updating --

    def refresh(self, state: ServiceState) -> None:
        self._kv["backend"].set(
            "Ready" if state.backend_available else "Unavailable",
            LIVE if state.backend_available else FAULT,
        )
        self._kv["receiver"].set(
            "Connected" if state.receiver_present else "Not detected",
            LIVE if state.receiver_present else MUTED,
        )
        self._kv["readers"].set(str(state.readers_active))
        self._kv["reports"].set(f"{state.total_reports:,}")
        self._kv["status"].set(f"{state.status_reports:,}")
        self._kv["bytes"].set(f"{state.total_bytes:,}")
        self._kv["inputs"].set(f"{state.inputs_detected:,}")
        self._kv["last_input"].set(state.last_input or "none")
        self._kv["last_raw"].set(state.last_report_hex or "--")

        message = state.last_error or (
            "" if state.backend_available else state.backend_message
        )
        if message:
            self._error_banner.set(message, "error" if state.last_error else "warn")
            self._error_banner.pack(fill="x", pady=(10, 0))
        else:
            self._error_banner.pack_forget()

        self._render_collections(state)
        self._render_unknown()
        self._render_log()

    def _render_collections(self, state: ServiceState) -> None:
        signature = "|".join(
            f"{c.path}:{c.opened}:{c.reports}" for c in state.collections
        )
        if signature == self._collections_signature:
            return
        self._collections_signature = signature

        for widget in self._collection_rows:
            widget.destroy()
        self._collection_rows.clear()

        if not state.collections:
            self._collections_empty.pack(anchor="w")
            return
        self._collections_empty.pack_forget()

        font = fonts()
        for collection in sorted(state.collections, key=lambda c: c.usage_page):
            row = tk.Frame(self._collections_box, bg=PANEL)
            row.pack(fill="x", pady=2)
            self._collection_rows.append(row)

            dot = LIVE if collection.opened else FAINT
            marker = tk.Canvas(row, width=8, height=8, bg=PANEL,
                               highlightthickness=0, bd=0)
            marker.pack(side="left", pady=(5, 0))
            marker.create_oval(1, 1, 7, 7, fill=dot, outline="")

            label = collection.name
            if collection.is_status:
                label += "  (status)"
            tk.Label(row, text=label, bg=PANEL,
                     fg=PAPER if collection.opened else MUTED,
                     font=font.strong if collection.is_status else font.base,
                     anchor="w", width=26).pack(side="left", padx=(8, 0))
            tk.Label(row, text=f"{collection.reports} reports", bg=PANEL, fg=MUTED,
                     font=font.small, anchor="e").pack(side="right")
            tk.Label(row, text="open" if collection.opened else "not opened",
                     bg=PANEL, fg=LIVE if collection.opened else FAINT,
                     font=font.small, anchor="e").pack(side="right", padx=(0, 12))

    def _render_unknown(self) -> None:
        fingerprints = self._service.fingerprints()
        signature = "|".join(f"{f.key}:{f.count}" for f in fingerprints)
        if signature == self._unknown_signature:
            return
        self._unknown_signature = signature

        for widget in self._unknown_rows:
            widget.destroy()
        self._unknown_rows.clear()

        if not fingerprints:
            self._unknown_empty.pack(anchor="w")
            return
        self._unknown_empty.pack_forget()

        font = fonts()
        for fingerprint in fingerprints[:12]:
            row = tk.Frame(self._unknown_box, bg=PANEL)
            row.pack(fill="x", pady=3)
            self._unknown_rows.append(row)
            head = tk.Frame(row, bg=PANEL)
            head.pack(fill="x")
            tk.Label(head, text=fingerprint.label, bg=PANEL, fg=PAPER,
                     font=font.strong, anchor="w").pack(side="left")
            tk.Label(head, text=f"seen {fingerprint.count}x", bg=PANEL, fg=WARN,
                     font=font.small).pack(side="right")
            tk.Label(row, text=fingerprint.last_raw, bg=PANEL, fg=ICE,
                     font=font.code, anchor="w").pack(fill="x")

    def _render_log(self) -> None:
        from ..applog import ring

        handler = ring()
        sequence = handler.sequence
        if sequence == self._log_sequence:
            return
        self._log_sequence = sequence

        lines = list(recent(LOG_TAIL))
        self._text.configure(state="normal")
        self._text.delete("1.0", "end")
        for line in lines:
            tag = "INFO"
            for candidate in ("ERROR", "WARNING", "DEBUG"):
                if f" {candidate} " in line:
                    tag = candidate
                    break
            self._text.insert("end", line + "\n", tag)
        self._text.configure(state="disabled")
        if self._follow.get():
            self._text.see("end")

    # -------------------------------------------------------------- actions --

    def _copy(self) -> None:
        try:
            self.clipboard_clear()
            self.clipboard_append("\n".join(recent(LOG_TAIL)))
        except Exception:
            pass

    def _open_folder(self) -> None:
        import subprocess
        import os

        target = str(log_dir())
        try:
            if os.name == "nt":
                os.startfile(target)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", target])
        except Exception:
            pass
