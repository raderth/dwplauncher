"""Custom UI widgets used by the DWP Launcher."""

import io
import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional

try:
    from PIL import Image, ImageTk
except ImportError:
    Image = None
    ImageTk = None


class RoundedButton:
    def __init__(self, parent, width=200, height=48,
                 bg="#8b1a2f", fg="#f5f5f5", border="#b02040"):
        self._width = width
        self._height = height
        self._bg = bg
        self._fg = fg
        self._border = border
        self._click_callback: Optional[Callable] = None
        self._cursor = "arrow"

        self.widget = tk.Frame(parent, width=width, height=height,
                               bg=bg, bd=0, highlightthickness=0)
        self.widget.pack_propagate(False)

        self._label = tk.Label(self.widget, text="",
                               bg=bg, fg=fg,
                               font=("Segoe UI", 10, "bold"),
                               justify="center")
        self._label.pack(expand=True, fill="both")

        self._progress_bg = tk.Frame(self.widget, bg="#222222", height=8)
        self._progress_bg.place(relx=0.5, rely=0.84, anchor="center",
                                width=width - 16)
        self._progress_fill = tk.Frame(self._progress_bg, bg="#22cc44", height=8,
                                       width=0)
        self._progress_fill.place(x=0, y=0)

        for widget in (self.widget, self._label, self._progress_bg):
            widget.bind("<Button-1>", self._handle_click)
            widget.bind("<Enter>", self._apply_cursor)
            widget.bind("<Leave>", self._apply_cursor)

    def _apply_cursor(self, _event=None):
        self.widget.config(cursor=self._cursor)
        self._label.config(cursor=self._cursor)
        self._progress_bg.config(cursor=self._cursor)

    def _handle_click(self, event):
        if self._click_callback:
            self._click_callback(event)

    def bind_click(self, callback: Callable):
        self._click_callback = callback

    def set_cursor(self, cursor: str):
        self._cursor = cursor
        self._apply_cursor()

    def set_label(self, label: str, sublabel: str = ""):
        text = label if not sublabel else f"{label}\n{sublabel}"
        self._label.config(text=text)

    def set_progress(self, progress: float):
        progress = max(0.0, min(1.0, float(progress)))
        width = int((self._width - 16) * progress)
        self._progress_fill.config(width=width)


class ScrollFrame(tk.Frame):
    def __init__(self, parent, bg: str = None, **kwargs):
        super().__init__(parent, bg=bg, **kwargs)
        self._canvas = tk.Canvas(self, bg=bg, bd=0, highlightthickness=0)
        self._scrollbar = ttk.Scrollbar(self, orient="vertical",
                                        command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._scrollbar.set)

        self._scrollbar.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)

        self.inner = tk.Frame(self._canvas, bg=bg)
        self._window = self._canvas.create_window((0, 0), window=self.inner,
                                                 anchor="nw")

        self.inner.bind("<Configure>", self._on_frame_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)
        self._canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _on_frame_configure(self, event):
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self._canvas.itemconfigure(self._window, width=event.width)

    def _on_mousewheel(self, event):
        if event.delta:
            self._canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")


class ModRow(tk.Frame):
    def __init__(self, parent, mod, on_toggle: Callable[[object], None],
                 bg="#242424", fg="#e8e8e8", active_bg="#2a4a2a",
                 inactive_bg="#3a3a3a"):
        super().__init__(parent, bg=bg)
        self.mod = mod
        self.on_toggle = on_toggle
        self._icon_image = None

        self._build_row(bg, fg, active_bg, inactive_bg)

    def _build_row(self, bg, fg, active_bg, inactive_bg):
        icon_label = tk.Label(self, bg=bg)
        icon_label.pack(side="left", padx=8, pady=6)

        if getattr(self.mod, "icon_data", None) and Image is not None and ImageTk is not None:
            try:
                pil_img = Image.open(io.BytesIO(self.mod.icon_data)).convert("RGBA")
                pil_img = pil_img.resize((32, 32), Image.LANCZOS)
                self._icon_image = ImageTk.PhotoImage(pil_img)
                icon_label.config(image=self._icon_image)
            except Exception:
                icon_label.config(text="🎮", fg=fg, font=("Segoe UI", 12, "bold"))
        else:
            icon_label.config(text="🎮", fg=fg, font=("Segoe UI", 12, "bold"))

        text_frame = tk.Frame(self, bg=bg)
        text_frame.pack(side="left", fill="both", expand=True, padx=(0, 8), pady=6)

        name = getattr(self.mod, "name", getattr(self.mod, "filename", "Unknown"))
        version = getattr(self.mod, "version", "")
        enabled = getattr(self.mod, "enabled", False)

        tk.Label(text_frame, text=name, bg=bg, fg=fg,
                 font=("Segoe UI", 10, "bold"), anchor="w").pack(fill="x")
        info_text = f"{version}" if version else ""
        if info_text:
            tk.Label(text_frame, text=info_text, bg=bg, fg="#888888",
                     font=("Segoe UI", 8), anchor="w").pack(fill="x")

        state_text = "Enabled" if enabled else "Disabled"
        state_color = "#3ab04a" if enabled else "#888888"
        tk.Label(text_frame, text=state_text, bg=bg, fg=state_color,
                 font=("Segoe UI", 8), anchor="w").pack(fill="x", pady=(2, 0))

        btn_bg = active_bg if enabled else inactive_bg
        btn_text = "Disable" if enabled else "Enable"
        self._toggle_btn = tk.Label(self, text=btn_text, bg=btn_bg,
                                    fg=fg, font=("Segoe UI", 9, "bold"),
                                    cursor="hand2", padx=12, pady=6)
        self._toggle_btn.pack(side="right", padx=8, pady=6)
        self._toggle_btn.bind("<Button-1>", self._on_clicked)

    def _on_clicked(self, _event=None):
        if callable(self.on_toggle):
            self.on_toggle(self.mod)


class Toggle(tk.Checkbutton):
    def __init__(self, parent, text="", value=False,
                 command: Optional[Callable[[bool], None]] = None,
                 bg="#1a1a1a", fg="#e8e8e8"):
        self.var = tk.BooleanVar(value=value)
        super().__init__(parent, text=text, variable=self.var,
                         command=self._on_change, bg=bg, fg=fg,
                         activebackground=bg, selectcolor=bg,
                         borderwidth=0, highlightthickness=0,
                         font=("Segoe UI", 9))
        self._user_command = command
        self._callback = command

    def _on_change(self):
        if self._callback:
            self._callback(self.var.get())

    def is_on(self) -> bool:
        return bool(self.var.get())


class Slider(tk.Scale):
    def __init__(self, parent, from_=0.0, to=1.0, orient="horizontal",
                 value=0.0, command: Optional[Callable[[str], None]] = None,
                 bg="#1a1a1a", fg="#e8e8e8", troughcolor="#242424",
                 sliderlength=16, length=200, resolution=0.01):
        super().__init__(parent, from_=from_, to=to, orient=orient,
                         command=command, bg=bg, fg=fg,
                         troughcolor=troughcolor,
                         sliderlength=sliderlength, length=length,
                         resolution=resolution, showvalue=False)
        self.set(value)
